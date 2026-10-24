"""1 分区間の動画退避(clip.mp4、index.m3u8、sprite.jpg を作って S3 に置く)。

方式(理由は README.md):
- 断片の一覧: ListFragments(プロデューサー時刻)。開始時刻が区間内の断片をその区間に割り当てる
- 動画: HLS ON_DEMAND(fMP4)セッションの init と断片をそのまま連結した fMP4。ffmpeg を使わない
- プレイリスト: clip.mp4 を EXT-X-BYTERANGE で参照する HLS(fMP4)
- スプライト: GetImages(1 秒間隔、160×90)を Pillow で 10×6 のシートにする
"""

from __future__ import annotations

import io
import math
import os
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

import boto3

from common import MinuteResult, kvs, log, max_rss_mb
from common.timeutil import epoch_ms, from_epoch_ms, media_prefix, to_utc

GAP_MS = 500
SPRITE_COLS, SPRITE_ROWS = 10, 6
THUMB_W, THUMB_H = 160, 90
SPRITE_INTERVAL_MS = 1000
SPRITE_QUALITY = 70
HTTP_TIMEOUT = 20
DOWNLOAD_WORKERS = 8


class ArchiveError(Exception):
    pass


# ---------------------------------------------------------------- HTTP


def http_get(url: str, *, retries: int = 3) -> bytes:
    last: Exception | None = None
    for i in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as r:  # noqa: S310 - KVS が返した https URL
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.3 * (2**i))
    raise ArchiveError(f"GET failed: {urllib.parse.urlsplit(url).path}: {last}")


# ---------------------------------------------------------------- HLS


@dataclass
class HlsMedia:
    init_url: str
    # (断片番号, URL)。プレイリストの順
    fragments: list[tuple[str, str]] = field(default_factory=list)


def _attr(line: str, name: str) -> str | None:
    """`#EXT-X-MAP:URI="..."` のような属性リストから値を取る。"""
    _, _, rest = line.partition(":")
    for part in _split_attrs(rest):
        k, _, v = part.partition("=")
        if k.strip().upper() == name:
            return v.strip().strip('"')
    return None


def _split_attrs(s: str) -> list[str]:
    out, cur, q = [], [], False
    for ch in s:
        if ch == '"':
            q = not q
        if ch == "," and not q:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        out.append("".join(cur))
    return out


def _fragment_number(url: str) -> str | None:
    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    for k, v in qs.items():
        if k.lower() == "fragmentnumber" and v:
            return v[0]
    return None


def parse_media_playlist(text: str, base_url: str) -> HlsMedia:
    init_url = None
    frags: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MAP"):
            uri = _attr(line, "URI")
            if uri:
                init_url = urllib.parse.urljoin(base_url, uri)
        elif not line.startswith("#"):
            url = urllib.parse.urljoin(base_url, line)
            num = _fragment_number(url)
            if num is None:
                raise ArchiveError(f"fragment number not found in playlist uri: {line[:80]}")
            frags.append((num, url))
    if init_url is None:
        raise ArchiveError("EXT-X-MAP not found (fMP4 ではない?)")
    return HlsMedia(init_url=init_url, fragments=frags)


def parse_master_playlist(text: str, base_url: str) -> str:
    """マスタープレイリストから最初のメディアプレイリストの URL を返す。"""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF"):
            for nxt in lines[i + 1 :]:
                if not nxt.startswith("#"):
                    return urllib.parse.urljoin(base_url, nxt)
    # マスターでなくメディアプレイリストだった
    return base_url


def open_hls(stream: str, start: datetime, end: datetime) -> HlsMedia:
    client = kvs.archived_media_client(stream, kvs.GET_HLS_STREAMING_SESSION_URL)
    url = client.get_hls_streaming_session_url(
        StreamName=stream,
        PlaybackMode="ON_DEMAND",
        HLSFragmentSelector={
            "FragmentSelectorType": "PRODUCER_TIMESTAMP",
            "TimestampRange": {"StartTimestamp": to_utc(start), "EndTimestamp": to_utc(end)},
        },
        ContainerFormat="FRAGMENTED_MP4",
        DiscontinuityMode="NEVER",
        DisplayFragmentTimestamp="NEVER",
        MaxMediaPlaylistFragmentResults=1000,
        Expires=300,
    )["HLSStreamingSessionURL"]
    master = http_get(url).decode()
    media_url = parse_master_playlist(master, url)
    media = http_get(media_url).decode() if media_url != url else master
    return parse_media_playlist(media, media_url)


# ---------------------------------------------------------------- clip / playlist


@dataclass
class Clip:
    data: bytes
    init_length: int
    parts: list[list[int]]  # [offset, length, duration_ms]
    fragments: list[kvs.Fragment]  # 実際に入れた断片(時刻順)


def build_clip(stream: str, start: datetime, end: datetime, frags: list[kvs.Fragment]) -> Clip:
    """HLS セッションの init と断片を、ListFragments で選んだ断片だけ連結する。"""
    # セッションの範囲は [start, end)。プレイリストに前後の断片が入っても、断片番号で絞り込む。
    media = open_hls(stream, start, end)
    wanted = {f.number: f for f in frags}
    selected = [(num, url) for num, url in media.fragments if num in wanted]
    missing = [n for n in wanted if n not in {num for num, _ in selected}]
    if missing:
        log("hls_missing_fragments", stream=stream, start=start, missing=len(missing), listed=len(wanted),
            playlist=len(media.fragments))
    if not selected:
        raise ArchiveError(f"no listed fragment in HLS playlist (listed={len(wanted)}, playlist={len(media.fragments)})")

    urls = [media.init_url] + [u for _, u in selected]
    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as ex:
        bodies = list(ex.map(http_get, urls))
    init, rest = bodies[0], bodies[1:]
    buf = io.BytesIO()
    buf.write(init)
    used = [wanted[num] for num, _ in selected]
    parts: list[list[int]] = []
    for body, dur in zip(rest, kvs.effective_durations(used, GAP_MS)):
        parts.append([buf.tell(), len(body), dur])
        buf.write(body)
    return Clip(data=buf.getvalue(), init_length=len(init), parts=parts, fragments=used)


def _pdt(ms: int) -> str:
    return from_epoch_ms(ms).strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


def build_playlist(clip: Clip, clip_name: str = "clip.mp4", gap_ms: int = GAP_MS) -> str:
    """clip.mp4 を BYTERANGE で参照する VOD プレイリスト。録画の切れ目には DISCONTINUITY を入れる。"""
    target = max(1, math.ceil(max(p[2] for p in clip.parts) / 1000))
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:7",
        f"#EXT-X-TARGETDURATION:{target}",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        "#EXT-X-INDEPENDENT-SEGMENTS",
        f'#EXT-X-MAP:URI="{clip_name}",BYTERANGE="{clip.init_length}@0"',
    ]
    prev_end = None
    for (off, length, dur), f in zip(clip.parts, clip.fragments):
        if prev_end is not None and f.producer_ms - prev_end > gap_ms:
            lines.append("#EXT-X-DISCONTINUITY")
            prev_end = None
        if prev_end is None:
            lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{_pdt(f.producer_ms)}")
        lines.append(f"#EXTINF:{dur / 1000:.3f},")
        lines.append(f"#EXT-X-BYTERANGE:{length}@{off}")
        lines.append(clip_name)
        prev_end = f.producer_ms + dur
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- sprite


def build_sprite(images: list[kvs.Image], start: datetime) -> tuple[bytes, int]:
    """1 秒ごとのコマを 10×6 のシートにする。画像がない秒は黒。戻り値は (JPEG, 実際に埋めたコマ数)。"""
    from PIL import Image as PILImage

    sheet = PILImage.new("RGB", (THUMB_W * SPRITE_COLS, THUMB_H * SPRITE_ROWS), (0, 0, 0))
    base = epoch_ms(start)
    filled: set[int] = set()
    for im in images:
        if not im.content:
            continue
        slot = (im.ts_ms - base) // SPRITE_INTERVAL_MS
        if not 0 <= slot < SPRITE_COLS * SPRITE_ROWS or slot in filled:
            continue
        try:
            thumb = PILImage.open(io.BytesIO(im.content)).convert("RGB")
        except Exception as e:  # noqa: BLE001
            log("sprite_decode_error", ts_ms=im.ts_ms, error=str(e))
            continue
        if thumb.size != (THUMB_W, THUMB_H):
            thumb = thumb.resize((THUMB_W, THUMB_H))
        sheet.paste(thumb, ((slot % SPRITE_COLS) * THUMB_W, (slot // SPRITE_COLS) * THUMB_H))
        filled.add(slot)
    out = io.BytesIO()
    sheet.save(out, format="JPEG", quality=SPRITE_QUALITY, optimize=True)
    return out.getvalue(), len(filled)


# ---------------------------------------------------------------- 本体


@dataclass
class Archiver:
    bucket: str
    s3: Any = None
    stream_name: Callable[[str], str] = kvs.stream_name_for
    # テストで差し替えられるようにする
    list_fragments: Callable[..., list[kvs.Fragment]] = kvs.list_fragments
    clip_builder: Callable[..., Clip] = build_clip
    get_images: Callable[..., list[kvs.Image]] = kvs.get_images

    def __post_init__(self):
        if self.s3 is None:
            self.s3 = boto3.client("s3")

    def _images(self, stream: str, start: datetime, end: datetime, segments: list[list[int]]) -> tuple[list[kvs.Image], str | None]:
        """スプライト用の画像を GetImages で取る。

        まず区間全体 [start, end) を 1 回で取る(前の区間の断片が区間の頭にかかっていれば、その秒の絵も取れる)。
        GetImages は範囲の始まりの近くに断片がないと ResourceNotFoundException になる(実測。録画の途中開始など)。
        そのときは録画がある範囲ごとに「開始を秒に切り上げた時刻 〜 終わり」で取り直す
        (断片のちょうど開始時刻を指定すると MEDIA_ERROR が返ることがあったため切り上げる)。
        スプライトが作れなくても動画は退避する(エラーは文字列で返す)。
        """
        images: list[kvs.Image] = []
        errors: list[str] = []
        s_ms, e_ms = epoch_ms(start), epoch_ms(end)
        try:
            return self.get_images(stream, start, end, interval_ms=SPRITE_INTERVAL_MS, width=THUMB_W, height=THUMB_H), None
        except Exception as e:  # noqa: BLE001
            if "ResourceNotFound" not in type(e).__name__ and "ResourceNotFound" not in str(e):
                return [], f"{type(e).__name__}: {e}"[:1000]
        for seg_s, seg_e in segments:
            a = max(s_ms, -(-seg_s // SPRITE_INTERVAL_MS) * SPRITE_INTERVAL_MS)
            b = min(e_ms, seg_e)
            if a >= b:
                continue
            try:
                images += self.get_images(stream, from_epoch_ms(a), from_epoch_ms(b), interval_ms=SPRITE_INTERVAL_MS,
                                          width=THUMB_W, height=THUMB_H)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{type(e).__name__}: {e}"[:300])
        return images, ("; ".join(errors)[:1000] or None)

    def process_minute(self, camera_id: str, start: datetime, end: datetime) -> MinuteResult:
        t: dict[str, Any] = {}
        t0 = time.monotonic()
        stream = self.stream_name(camera_id)
        # 断片が出そろったか(ウォーターマーク)は run_pipeline が判定済み。一覧は run_pipeline が取った結果から返る
        frags = self.list_fragments(stream, start, end)
        t["list_ms"] = _ms(t0)
        if not frags:
            log("minute_metrics", camera_id=camera_id, start=start, status="EMPTY", fragment_count=0, **t)
            return MinuteResult("EMPTY", {"segments": [], "fragment_count": 0, "bytes": 0})

        t1 = time.monotonic()
        clip = self.clip_builder(stream, start, end, frags)
        t["clip_ms"] = _ms(t1)

        t2 = time.monotonic()
        images, sprite_err = self._images(stream, start, end, kvs.build_segments(frags, GAP_MS))
        if sprite_err:
            log("get_images_error", camera_id=camera_id, start=start, error=sprite_err)
        t["images_ms"] = _ms(t2)
        t3 = time.monotonic()
        sprite, sprite_frames = build_sprite(images, start)
        t["sprite_ms"] = _ms(t3)

        playlist = build_playlist(clip)
        prefix = media_prefix(camera_id, start)
        keys = {"clip": prefix + "clip.mp4", "playlist": prefix + "index.m3u8", "sprite": prefix + "sprite.jpg"}
        t4 = time.monotonic()
        # 1. S3(clip → sprite → プレイリストの順。プレイリストは clip を参照するので最後)
        self.s3.put_object(Bucket=self.bucket, Key=keys["clip"], Body=clip.data, ContentType="video/mp4")
        self.s3.put_object(Bucket=self.bucket, Key=keys["sprite"], Body=sprite, ContentType="image/jpeg")
        self.s3.put_object(Bucket=self.bucket, Key=keys["playlist"], Body=playlist.encode(),
                           ContentType="application/vnd.apple.mpegurl")
        t["s3_ms"] = _ms(t4)

        segments = kvs.build_segments(clip.fragments, GAP_MS)
        attrs: dict[str, Any] = {
            "segments": segments,
            "files": keys,
            "hls": {"init_length": clip.init_length, "parts": clip.parts},
            "fragment_count": len(clip.fragments),
            "bytes": len(clip.data),
            "sprite_frames": sprite_frames,
        }
        if sprite_err:
            attrs["sprite_error"] = sprite_err
        log(
            "minute_metrics",
            camera_id=camera_id,
            start=start,
            status="DONE",
            fragment_count=len(clip.fragments),
            listed_fragments=len(frags),
            clip_bytes=len(clip.data),
            kvs_bytes=sum(f.size for f in clip.fragments),
            sprite_bytes=len(sprite),
            sprite_frames=sprite_frames,
            images=len(images),
            recorded_ms=sum(e - s for s, e in segments),
            max_rss_mb=max_rss_mb(),
            total_ms=_ms(t0),
            **t,
        )
        return MinuteResult("DONE", attrs)


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


def env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    return int(v) if v else default


__all__ = ["Archiver", "build_clip", "build_playlist", "build_sprite", "parse_media_playlist", "parse_master_playlist",
           "Clip", "ArchiveError", "env_int"]
