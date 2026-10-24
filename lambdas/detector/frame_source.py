"""1 分区間のフレームの取り方(差し替え可能)。

`FrameSource = (camera_id, start, end) -> FrameBatch` の形にしておき、handler は `handler.FRAME_SOURCE` を呼ぶ。
環境変数 `FRAME_SOURCE` で選ぶ(`select`):

- `clip`(既定): 動画の退避が S3 に置いた `clip.mp4` を読み、キーフレーム(1 秒に 1 枚)を PyAV でデコードする
  (`common.frames`、lambdas/common/FRAMES.md)。GetImages を使わない。区間の件の `video_status` で分岐する:
  なし → `NotReady`(動画の退避を待つ。待つ上限を過ぎたら GetImages)、`EMPTY` → 空、`FAILED` → GetImages(予備の経路)
- `getimages`: KVS の GetImages(1 枚 $0.00001)で 1 秒間隔の JPEG を取る(以前の方式。`clip` の予備の経路でもある)

FrameBatch.frames の画像は次のどれでもよい(detect.Detector.detect が受け付ける):
JPEG / PNG の bytes、PIL.Image、HWC の RGB uint8 の numpy 配列。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

import boto3

from common import NotReady, kvs, log, upstream_max_wait_seconds
from common.timeutil import epoch_ms, from_epoch_ms, iso, to_utc, utcnow


@dataclass
class FrameBatch:
    frames: list[tuple[datetime, Any]]  # (フレームの時刻 UTC, 画像)。時刻順、start ≤ ts < end
    stats: dict = field(default_factory=dict)  # ログ用(fetch_ms、fragments、image_errors など)


FrameSource = Callable[[str, datetime, datetime], FrameBatch]


def _is_not_found(e: Exception) -> bool:
    return "ResourceNotFound" in type(e).__name__ or "ResourceNotFound" in str(e)


def _get_images(stream: str, start: datetime, end: datetime, fragments, width: int, height: int) -> list:
    """GetImages で 1 秒間隔の JPEG を取る。

    GetImages は範囲の始まりの近くに断片がないと ResourceNotFoundException になる(実測。録画が区間の途中から
    始まるときなど)。そのときは録画がある範囲ごとに「開始を秒に切り上げた時刻 〜 終わり」で取り直す
    (断片のちょうど開始時刻を指定すると MEDIA_ERROR が返ることがあるため切り上げる。video_archiver と同じ)。
    """
    try:
        return kvs.get_images(stream, start, end, interval_ms=1000, width=width, height=height)
    except Exception as e:  # noqa: BLE001
        if not _is_not_found(e):
            raise
    out = []
    s_ms, e_ms = epoch_ms(start), epoch_ms(end)
    for seg_s, seg_e in kvs.build_segments(fragments):
        a = max(s_ms, -(-seg_s // 1000) * 1000)
        b = min(e_ms, seg_e)
        if a >= b:
            continue
        try:
            out += kvs.get_images(stream, from_epoch_ms(a), from_epoch_ms(b), interval_ms=1000,
                                  width=width, height=height)
        except Exception as e:  # noqa: BLE001
            if not _is_not_found(e):
                raise
            log("detect_segment_no_images", stream=stream, seg_start_ms=a, seg_end_ms=b)
    return sorted(out, key=lambda im: im.ts_ms)


def _input_width() -> int:
    return int(os.environ.get("DETECT_INPUT_WIDTH") or 640)


def getimages(camera_id: str, start: datetime, end: datetime) -> FrameBatch:
    """KVS の GetImages で [start, end) を 1 秒間隔の JPEG として取る(録画がなければ呼ばない)。

    サイズは環境変数 DETECT_INPUT_WIDTH(既定 640)と DETECT_IMAGE_HEIGHT(既定は幅 × 9/16)。
    GetImages は幅と高さを両方指定する必要がある(片方だけだと InvalidArgumentException)。
    """
    width = _input_width()
    height = int(os.environ.get("DETECT_IMAGE_HEIGHT") or round(width * 9 / 16))
    stream = kvs.stream_name_for(camera_id)
    t = time.perf_counter()
    fragments = kvs.list_fragments(stream, start, end)
    list_ms = (time.perf_counter() - t) * 1000
    images = _get_images(stream, start, end, fragments, width, height) if fragments else []
    # GetImages は終わりの時刻も含む(1 秒間隔で 61 枚)ので、start ≤ ts < end に絞る
    s_ms, e_ms = epoch_ms(start), epoch_ms(end)
    seen: set[int] = set()
    frames = []
    errors: dict[str, int] = {}
    for im in images:
        if im.error:
            errors[im.error] = errors.get(im.error, 0) + 1
            continue
        if im.content and s_ms <= im.ts_ms < e_ms and im.ts_ms not in seen:
            seen.add(im.ts_ms)
            frames.append((from_epoch_ms(im.ts_ms), im.content))
    return FrameBatch(frames, {
        "source": "getimages",
        "fragments": len(fragments),
        "images_requested": len(images),
        "image_errors": errors,
        "list_fragments_ms": round(list_ms, 1),
        "get_images_ms": round((time.perf_counter() - t) * 1000 - list_ms, 1),
        "image_size": [width, height],
    })


# ---------------------------------------------------------------- clip(S3 の clip.mp4)

_table = None
_s3 = None


def _state_table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(os.environ["STATE_TABLE"])
    return _table


def _s3_client():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


def _fallback(camera_id: str, start: datetime, end: datetime, reason: str, **extra) -> FrameBatch:
    """予備の経路(GetImages)。clip.mp4 が使えない区間だけ。"""
    log("detect_fallback", camera_id=camera_id, start=start, reason=reason, **extra)
    b = FALLBACK(camera_id, start, end)
    b.stats = {**b.stats, "fallback": reason, **extra}
    return b


def clipfile(camera_id: str, start: datetime, end: datetime, *, table=None, s3=None, now=utcnow) -> FrameBatch:
    """動画の退避が S3 に置いた clip.mp4 からキーフレームを 1 秒に 1 枚デコードする。

    区間の件(DynamoDB)の `video_status` で分岐する:
    - なし: 動画の退避がまだ処理していない → `NotReady`(試行回数に数えず、次の起動で再挑戦)。
      ただし区間の終わりから UPSTREAM_MAX_WAIT_SECONDS(既定 1800 秒)を過ぎたら GetImages で取る
      (run_pipeline の upstream_kind="VIDEO" と同じ上限。通常は run_pipeline が動画の退避の処理済み位置で止めるので、ここには来ない)
    - `EMPTY`: 録画なし → 空(GetImages も呼ばない)
    - `FAILED`: 動画の退避に失敗した → GetImages(予備の経路。費用は失敗した区間の分だけ)
    - `DONE`: S3 の `files.clip` を GET し、`hls.parts` と `segments` から各断片の時刻を求めてデコードする。
      デコードに失敗したら GetImages で取り直す

    フレームの時刻は、その秒の枠に入るキーフレームの実際の時刻(プロデューサー時刻)。GetImages(指定した時刻の直後のフレーム)
    より最大 1 秒後ろになる。サイズは幅 DETECT_INPUT_WIDTH(既定 640)、高さ DETECT_IMAGE_HEIGHT(既定は縦横比を保つ)。
    """
    from common import frames  # PyAV を使うのはこの経路だけ

    tbl = table or _state_table()
    t0 = time.perf_counter()
    item = tbl.get_item(Key={"camera_id": camera_id, "sk": iso(start)}, ConsistentRead=True).get("Item") or {}
    status = item.get("video_status")
    if status is None:
        late_s = (to_utc(now()) - to_utc(end)).total_seconds()
        wait = upstream_max_wait_seconds()
        if late_s < wait:
            raise NotReady("video_status がまだない(動画の退避を待つ)")
        return _fallback(camera_id, start, end, "video_missing", late_seconds=int(late_s))
    if status == "EMPTY":
        return FrameBatch([], {"source": "clip", "video_status": status})
    if status == "FAILED":
        return _fallback(camera_id, start, end, "video_failed")
    key = (item.get("files") or {}).get("clip")
    hls = item.get("hls") or {}
    if not key or not hls.get("parts"):
        return _fallback(camera_id, start, end, "clip_missing")

    bucket = os.environ["MEDIA_BUCKET"]
    t1 = time.perf_counter()
    body = (s3 or _s3_client()).get_object(Bucket=bucket, Key=key)["Body"].read()
    get_ms = (time.perf_counter() - t1) * 1000
    width = _input_width()
    h_env = os.environ.get("DETECT_IMAGE_HEIGHT")
    try:
        anchors = frames.anchors_from_hls(hls["parts"], item.get("segments") or [])
        r = frames.decode_keyframes(body, start_ms=epoch_ms(start), end_ms=epoch_ms(end), anchors=anchors,
                                    sizes={"detect": (width, int(h_env) if h_env else None)})
    except Exception as e:  # noqa: BLE001 - 壊れた clip は GetImages で取り直す
        return _fallback(camera_id, start, end, "decode_error", error=f"{type(e).__name__}: {e}"[:300])
    batch = [(from_epoch_ms(f.ts_ms), f.images["detect"]) for f in r.frames]
    st = r.stats
    return FrameBatch(batch, {
        "source": "clip",
        "video_status": status,
        "clip_bytes": len(body),
        "get_item_ms": round((t1 - t0) * 1000, 1),
        "s3_get_ms": round(get_ms, 1),
        "decode_ms": st.get("decode_ms"),
        "scale_ms": st.get("scale_ms"),
        "decode_total_ms": st.get("total_ms"),
        "keyframes": st.get("keyframes"),
        "decoded": st.get("decoded"),
        "decode_lost": st.get("lost"),
        "image_size": list(r.frames[0].images["detect"].size) if r.frames else None,
    })


# clip の予備の経路(差し替えはテスト用)
FALLBACK: FrameSource = getimages

SOURCES: dict[str, FrameSource] = {"clip": clipfile, "getimages": getimages}


def select(name: str | None) -> FrameSource:
    """環境変数 FRAME_SOURCE の値からフレーム源を選ぶ(既定 clip)。"""
    name = (name or "clip").strip().lower()
    if name not in SOURCES:
        raise ValueError(f"FRAME_SOURCE は {sorted(SOURCES)} のどれか: {name!r}")
    return SOURCES[name]
