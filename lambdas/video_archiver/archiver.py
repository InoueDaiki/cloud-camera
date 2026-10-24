"""1 分区間の動画退避(clip.mp4、index.m3u8、sprite.jpg を作って S3 に置く)。

方式(理由は README.md):
- 断片の一覧: ListFragments(プロデューサー時刻)。開始時刻が区間内の断片をその区間に割り当てる
- 動画: HLS ON_DEMAND(fMP4)セッションの init と断片をそのまま連結した fMP4。ffmpeg を使わない。
  区間の途中でコーデック(コーデックプライベートデータ)が変わると、KVS の HLS は新しい EXT-X-MAP(init)を出す。
  init ごとに部分に分け、clip.mp4、clip.1.mp4 … にする(docs/codec.md、docs/contracts.md「メディアのキー」)
- プレイリスト: clip.mp4 を EXT-X-BYTERANGE で参照する HLS(fMP4)
- スプライト: 作った clip.mp4 のキーフレーム(1 秒に 1 枚)を PyAV でデコードし、160×90 に縮めて 10×6 のシートにする
  (`common.frames`。GetImages は使わない。lambdas/common/FRAMES.md)
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

from common import MinuteResult, frames, kvs, log, max_rss_mb
from common.timeutil import epoch_ms, from_epoch_ms, media_prefix, to_utc

GAP_MS = 500
SPRITE_COLS, SPRITE_ROWS = frames.SPRITE_COLS, frames.SPRITE_ROWS
THUMB_W, THUMB_H = frames.THUMB_W, frames.THUMB_H
SPRITE_INTERVAL_MS = 1000
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
    init_url: str  # 最初の init
    # (断片番号, URL)。プレイリストの順(すべての部分)
    fragments: list[tuple[str, str]] = field(default_factory=list)
    # EXT-X-MAP ごとの部分 [(init の URL, [(断片番号, URL), ...]), ...]。KVS はコーデックプライベートデータが
    # 変わったところ(コーデックの切り替え、解像度の変更など)で新しい EXT-X-MAP を出す
    sections: list[tuple[str, list[tuple[str, str]]]] = field(default_factory=list)


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
    sections: list[tuple[str, list[tuple[str, str]]]] = []
    frags: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MAP"):
            uri = _attr(line, "URI")
            if uri:
                sections.append((urllib.parse.urljoin(base_url, uri), []))
        elif not line.startswith("#"):
            url = urllib.parse.urljoin(base_url, line)
            num = _fragment_number(url)
            if num is None:
                raise ArchiveError(f"fragment number not found in playlist uri: {line[:80]}")
            if not sections:
                raise ArchiveError("EXT-X-MAP not found (fMP4 ではない?)")
            frags.append((num, url))
            sections[-1][1].append((num, url))
    if not sections:
        raise ArchiveError("EXT-X-MAP not found (fMP4 ではない?)")
    return HlsMedia(init_url=sections[0][0], fragments=frags, sections=[x for x in sections if x[1]])


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
    codec: str | None = None  # init の sample entry から: "H265" / "H264"(不明なら None)


# init(moov)の stsd の最初のサンプルエントリの種類 → コーデック
_SAMPLE_ENTRY_CODEC = {b"hvc1": "H265", b"hev1": "H265", b"avc1": "H264", b"avc3": "H264"}


def codec_of_init(init: bytes) -> str | None:
    """fMP4 の init からコーデックを読む(stsd の直後のサンプルエントリの 4 文字)。"""
    i = init.find(b"stsd")
    while i >= 0:
        # 'stsd' の後ろ: version/flags(4)、entry_count(4)、エントリのサイズ(4)、エントリの種類(4)
        kind = init[i + 16:i + 20]
        if kind in _SAMPLE_ENTRY_CODEC:
            return _SAMPLE_ENTRY_CODEC[kind]
        i = init.find(b"stsd", i + 4)
    return None


def build_clips(stream: str, start: datetime, end: datetime, frags: list[kvs.Fragment]) -> list[Clip]:
    """HLS セッションの init と断片を、ListFragments で選んだ断片だけ連結する。

    プレイリストの EXT-X-MAP(init)ごとに 1 つの Clip にする(区間の途中でコーデックが変わったら 2 つ以上)。
    """
    # セッションの範囲は [start, end)。プレイリストに前後の断片が入っても、断片番号で絞り込む。
    media = open_hls(stream, start, end)
    wanted = {f.number: f for f in frags}
    sections = [(init, [(num, url) for num, url in fr if num in wanted]) for init, fr in media.sections]
    sections = [(init, sel) for init, sel in sections if sel]
    got = {num for _, sel in sections for num, _ in sel}
    missing = [n for n in wanted if n not in got]
    if missing:
        log("hls_missing_fragments", stream=stream, start=start, missing=len(missing), listed=len(wanted),
            playlist=len(media.fragments))
    if not sections:
        raise ArchiveError(f"no listed fragment in HLS playlist (listed={len(wanted)}, playlist={len(media.fragments)})")

    urls = [u for init, sel in sections for u in (init, *(x for _, x in sel))]
    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as ex:
        bodies = iter(list(ex.map(http_get, urls)))
    clips = []
    for _init, sel in sections:
        init = next(bodies)
        buf = io.BytesIO()
        buf.write(init)
        used = [wanted[num] for num, _ in sel]
        parts: list[list[int]] = []
        for dur in kvs.effective_durations(used, GAP_MS):
            body = next(bodies)
            parts.append([buf.tell(), len(body), dur])
            buf.write(body)
        clips.append(Clip(data=buf.getvalue(), init_length=len(init), parts=parts, fragments=used,
                          codec=codec_of_init(init)))
    if len(clips) > 1:
        log("clip_sections", stream=stream, start=start, sections=len(clips), codecs=[c.codec for c in clips])
    return clips


def build_clip(stream: str, start: datetime, end: datetime, frags: list[kvs.Fragment]) -> Clip:
    """build_clips の部分が 1 つのとき(検証用の道具から使う)。"""
    clips = build_clips(stream, start, end, frags)
    if len(clips) != 1:
        raise ArchiveError(f"区間の途中で init が変わっている({len(clips)} 部分)。build_clips を使う")
    return clips[0]


def clip_names(n: int) -> list[str]:
    """部分ごとのファイル名。1 つ目は clip.mp4(以前と同じ)、2 つ目から clip.1.mp4、clip.2.mp4 …"""
    return ["clip.mp4" if i == 0 else f"clip.{i}.mp4" for i in range(n)]


def _pdt(ms: int) -> str:
    return from_epoch_ms(ms).strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


def build_playlist(clip: Clip | list[Clip], clip_name: str | list[str] = "clip.mp4", gap_ms: int = GAP_MS) -> str:
    """clip.mp4 を BYTERANGE で参照する VOD プレイリスト。録画の切れ目には DISCONTINUITY を入れる。

    部分が 2 つ以上(区間の途中でコーデックが変わった)なら、部分の頭に DISCONTINUITY と EXT-X-MAP を入れて
    clip.1.mp4 … に切り替える。
    """
    clips = clip if isinstance(clip, list) else [clip]
    if isinstance(clip_name, list):
        names = clip_name
    else:
        names = [clip_name] if len(clips) == 1 else clip_names(len(clips))
    target = max(1, math.ceil(max(p[2] for c in clips for p in c.parts) / 1000))
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:7",
        f"#EXT-X-TARGETDURATION:{target}",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        "#EXT-X-INDEPENDENT-SEGMENTS",
    ]
    for i, (c, name) in enumerate(zip(clips, names)):
        if i > 0:
            lines.append("#EXT-X-DISCONTINUITY")
        lines.append(f'#EXT-X-MAP:URI="{name}",BYTERANGE="{c.init_length}@0"')
        prev_end = None
        for (off, length, dur), f in zip(c.parts, c.fragments):
            if prev_end is not None and f.producer_ms - prev_end > gap_ms:
                lines.append("#EXT-X-DISCONTINUITY")
                prev_end = None
            if prev_end is None:
                lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{_pdt(f.producer_ms)}")
            lines.append(f"#EXTINF:{dur / 1000:.3f},")
            lines.append(f"#EXT-X-BYTERANGE:{length}@{off}")
            lines.append(name)
            prev_end = f.producer_ms + dur
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- sprite


@dataclass
class Sprite:
    jpeg: bytes
    frames: int  # 実際に埋めたコマ数(0〜60)
    stats: dict = field(default_factory=dict)  # common.frames の統計(decode_ms、scale_ms、decoded など)
    error: str | None = None


def build_sprite(clip: Clip | list[Clip], start: datetime, end: datetime) -> Sprite:
    """clip.mp4 のキーフレームを 1 秒に 1 枚デコードし、10×6 のシートにする。

    コマ i(0〜59)は「区間の開始 + i 秒」の枠に入るキーフレーム(その秒の中の実際の時刻のフレーム)。
    キーフレームがない枠(録画なし)は黒。区間の頭の枠には、その秒に始まる断片のキーフレームが入る
    (前の区間の断片から続く部分は、この区間の clip に入っていないので使わない)。
    デコードに失敗したら全コマ黒のシートを返し、理由を error に入れる(動画の退避は続ける)。
    """
    s_ms, e_ms = epoch_ms(start), epoch_ms(end)
    clips = clip if isinstance(clip, list) else [clip]
    try:
        # 部分(コーデック)ごとにデコードして 1 枚のシートにまとめる。同じ枠は先の部分を優先
        got: dict[int, Any] = {}
        stats: dict[str, Any] = {}
        for c in clips:
            anchors = frames.anchors_from_fragments(c.parts, [f.producer_ms for f in c.fragments])
            r = frames.decode_keyframes(c.data, start_ms=s_ms, end_ms=e_ms, anchors=anchors,
                                        interval_ms=SPRITE_INTERVAL_MS, sizes={"sprite": (THUMB_W, THUMB_H)})
            for f in r.frames:
                got.setdefault(f.slot, f)
            for k, v in r.stats.items():
                num = isinstance(v, (int, float)) and not isinstance(v, bool)
                stats[k] = stats.get(k, 0) + v if num else v
        jpeg, n = frames.build_sprite([got[k] for k in sorted(got)], "sprite")
        return Sprite(jpeg, n, stats)
    except Exception as e:  # noqa: BLE001 - 壊れた断片などはスプライトだけの失敗にする
        jpeg, _ = frames.build_sprite([], "sprite")
        return Sprite(jpeg, 0, {}, f"{type(e).__name__}: {e}"[:1000])


# ---------------------------------------------------------------- 本体


@dataclass
class Archiver:
    bucket: str
    s3: Any = None
    stream_name: Callable[[str], str] = kvs.stream_name_for
    # テストで差し替えられるようにする
    list_fragments: Callable[..., list[kvs.Fragment]] = kvs.list_fragments
    # 部分(Clip)のリストを返す。Clip 1 つを返してもよい(テスト用)
    clip_builder: Callable[..., list[Clip] | Clip] = build_clips
    sprite_builder: Callable[[Clip | list[Clip], datetime, datetime], Sprite] = build_sprite

    def __post_init__(self):
        if self.s3 is None:
            self.s3 = boto3.client("s3")

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
        built = self.clip_builder(stream, start, end, frags)
        clips = built if isinstance(built, list) else [built]
        clip = clips[0]
        t["clip_ms"] = _ms(t1)

        t2 = time.monotonic()
        sp = self.sprite_builder(clips if len(clips) > 1 else clip, start, end)
        if sp.error:
            log("sprite_error", camera_id=camera_id, start=start, error=sp.error)
        t["sprite_ms"] = _ms(t2)
        sprite, sprite_frames, sprite_err = sp.jpeg, sp.frames, sp.error

        names = clip_names(len(clips))
        playlist = build_playlist(clips, names)
        prefix = media_prefix(camera_id, start)
        clip_keys = [prefix + n for n in names]
        keys: dict[str, Any] = {"clip": clip_keys[0], "playlist": prefix + "index.m3u8", "sprite": prefix + "sprite.jpg"}
        if len(clips) > 1:
            keys["clips"] = clip_keys
        t4 = time.monotonic()
        # 1. S3(clip → sprite → プレイリストの順。プレイリストは clip を参照するので最後)
        for k, c in zip(clip_keys, clips):
            self.s3.put_object(Bucket=self.bucket, Key=k, Body=c.data, ContentType="video/mp4")
        self.s3.put_object(Bucket=self.bucket, Key=keys["sprite"], Body=sprite, ContentType="image/jpeg")
        self.s3.put_object(Bucket=self.bucket, Key=keys["playlist"], Body=playlist.encode(),
                           ContentType="application/vnd.apple.mpegurl")
        t["s3_ms"] = _ms(t4)

        all_frags = [f for c in clips for f in c.fragments]
        segments = kvs.build_segments(all_frags, GAP_MS)
        hls: dict[str, Any] = {"init_length": clip.init_length, "parts": clip.parts}
        if len(clips) > 1:
            # 部分ごと(コーデックが変わった区間)。API はこれで部分ごとに EXT-X-DISCONTINUITY と EXT-X-MAP を切り替える
            hls["sections"] = [{"clip": k, "init_length": c.init_length, "parts": c.parts, "codec": c.codec or "",
                                "start_ms": c.fragments[0].producer_ms} for k, c in zip(clip_keys, clips)]
        attrs: dict[str, Any] = {
            "segments": segments,
            "files": keys,
            "hls": hls,
            "codecs": [c.codec or "" for c in clips],
            "fragment_count": len(all_frags),
            "bytes": sum(len(c.data) for c in clips),
            "sprite_frames": sprite_frames,
        }
        if sprite_err:
            attrs["sprite_error"] = sprite_err
        log(
            "minute_metrics",
            camera_id=camera_id,
            start=start,
            status="DONE",
            fragment_count=len(all_frags),
            listed_fragments=len(frags),
            clip_bytes=sum(len(c.data) for c in clips),
            sections=len(clips),
            codecs=[c.codec for c in clips],
            kvs_bytes=sum(f.size for f in all_frags),
            sprite_bytes=len(sprite),
            sprite_frames=sprite_frames,
            decode_ms=sp.stats.get("decode_ms"),
            scale_ms=sp.stats.get("scale_ms"),
            keyframes=sp.stats.get("keyframes"),
            decoded=sp.stats.get("decoded"),
            decode_lost=sp.stats.get("lost"),
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


__all__ = ["Archiver", "build_clip", "build_clips", "clip_names", "codec_of_init", "build_playlist", "build_sprite", "parse_media_playlist", "parse_master_playlist",
           "Clip", "Sprite", "ArchiveError", "env_int"]
