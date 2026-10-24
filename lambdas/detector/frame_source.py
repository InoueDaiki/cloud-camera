"""1 分区間のフレームの取り方(差し替え可能)。

`FrameSource = (camera_id, start, end) -> FrameBatch` の形にしておき、handler は `handler.FRAME_SOURCE` を呼ぶ。
今は KVS の GetImages を使う(`getimages`)。GetImages は 1 枚 $0.00001 かかるので、S3 の clip.mp4 や
KVS の HLS 断片を PyAV でデコードする実装(`lambdas/common/frames.py` で試作中)に差し替えられるようにしている。

FrameBatch.frames の画像は次のどれでもよい(detect.Detector.detect が受け付ける):
JPEG / PNG の bytes、PIL.Image、HWC の RGB uint8 の numpy 配列。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from common import kvs, log
from common.timeutil import epoch_ms, from_epoch_ms


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


def getimages(camera_id: str, start: datetime, end: datetime) -> FrameBatch:
    """KVS の GetImages で [start, end) を 1 秒間隔の JPEG として取る(録画がなければ呼ばない)。

    サイズは環境変数 DETECT_INPUT_WIDTH(既定 640)と DETECT_IMAGE_HEIGHT(既定は幅 × 9/16)。
    GetImages は幅と高さを両方指定する必要がある(片方だけだと InvalidArgumentException)。
    """
    width = int(os.environ.get("DETECT_INPUT_WIDTH") or 640)
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
