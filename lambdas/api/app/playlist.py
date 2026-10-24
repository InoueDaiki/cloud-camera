"""GET /api/cameras/{id}/vod.m3u8

区間の件の `hls`(init_length と parts)から、範囲内の DONE 区間を連結した HLS(fMP4)を組む。
区間ごとに clip.mp4 が別ファイルなので、区間の頭で EXT-X-DISCONTINUITY と EXT-X-MAP を切り替え、
EXT-X-PROGRAM-DATE-TIME で実時刻を示す。hls.js と Safari の両方で読める形にしている。
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from . import state
from .config import Config
from .http import bad_request, not_found
from .timeutil import ceil_minute, floor_minute, from_ms, iso, parse_ts

MAX_RANGE = timedelta(hours=3)  # 1 プレイリストに入れる上限(180 区間)


def _clip_key(camera_id: str, minute: datetime) -> str:
    return f"{camera_id}/{minute:%Y/%m/%d/%H/%M}/clip.mp4"


def build(cfg: Config, camera_id: str, query: dict[str, str]) -> str:
    start = parse_ts(query.get("from"), "from")
    end = parse_ts(query.get("to"), "to")
    if end <= start:
        raise bad_request("to は from より後にしてください")
    if end - start > MAX_RANGE:
        raise bad_request(f"範囲は {int(MAX_RANGE.total_seconds() // 60)} 分以内にしてください", "RANGE_TOO_LARGE")

    items = state.query_minutes(cfg, camera_id, floor_minute(start), ceil_minute(end),
                                ["video_status", "segments", "files.clip", "hls"])
    sections = []
    for item in items:
        if item.get("video_status") != "DONE":
            continue
        hls = item.get("hls") or {}
        parts = hls.get("parts") or []
        init_length = int(hls.get("init_length") or 0)
        if not parts or init_length <= 0:
            continue
        minute = parse_ts(item["sk"], "sk")
        key = (item.get("files") or {}).get("clip") or _clip_key(camera_id, minute)
        segs = item.get("segments") or []
        # 最初の断片の実時刻。segments があればその開始、なければ区間の開始
        first = from_ms(int(segs[0][0])) if segs else minute
        sections.append({
            "uri": cfg.media_url(key),
            "init_length": init_length,
            "parts": [(int(o), int(l), int(d)) for o, l, d in parts],
            "pdt": first,
        })
    if not sections:
        raise not_found("範囲内に S3 へ退避済みの録画がありません", "NO_RECORDING")
    return render(sections)


def render(sections: list[dict]) -> str:
    """sections: [{"uri", "init_length", "parts": [(offset, length, duration_ms)], "pdt": datetime}]"""
    max_ms = max(d for s in sections for _, _, d in s["parts"])
    target = max(1, math.ceil(max_ms / 1000))
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:7",
        f"#EXT-X-TARGETDURATION:{target}",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-DISCONTINUITY-SEQUENCE:0",
        "#EXT-X-INDEPENDENT-SEGMENTS",
    ]
    for i, s in enumerate(sections):
        if i > 0:
            lines.append("#EXT-X-DISCONTINUITY")
        lines.append(f'#EXT-X-MAP:URI="{s["uri"]}",BYTERANGE="{s["init_length"]}@0"')
        lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{iso(s['pdt'], ms=True)}")
        for offset, length, dur in s["parts"]:
            lines.append(f"#EXTINF:{dur / 1000:.3f},")
            lines.append(f"#EXT-X-BYTERANGE:{length}@{offset}")
            lines.append(s["uri"])
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"
