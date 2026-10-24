"""GET /api/cameras/{id}/timeline

処理済み位置(CURSOR#VIDEO)より前は DynamoDB の区間の件、後は KVS の ListFragments から
録画範囲を組み立てる。S3 と KVS の境界はここで隠し、ビューワーは `source` だけ見ればよい。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from . import kvs, state
from .config import Config
from .http import bad_request
from .timeutil import ceil_minute, floor_minute, from_ms, iso, iso_auto, now, parse_ts, to_ms

MAX_RANGE = timedelta(hours=48)
DEFAULT_RANGE = timedelta(hours=1)
# 分をまたぐ segments をつなげるときの許容(区間の境界でちょうど切れている前提)
MERGE_GAP_MS = 1000


def _media_key(camera_id: str, minute: datetime, name: str) -> str:
    return f"{camera_id}/{minute:%Y/%m/%d/%H/%M}/{name}"


def _segments_of(item: dict, minute: datetime) -> list[tuple[int, int]]:
    segs = item.get("segments")
    if not segs:
        # segments がない DONE は 1 分まるごと録画ありとみなす
        s = to_ms(minute)
        return [(s, s + 60_000)]
    return [(int(a), int(b)) for a, b in segs]


def build(cfg: Config, camera_id: str, query: dict[str, str]) -> dict:
    t_now = now()
    end = parse_ts(query["to"], "to") if query.get("to") else t_now
    start = parse_ts(query["from"], "from") if query.get("from") else end - DEFAULT_RANGE
    if end <= start:
        raise bad_request("to は from より後にしてください")
    if end - start > MAX_RANGE:
        raise bad_request(f"範囲は {int(MAX_RANGE.total_seconds() // 3600)} 時間以内にしてください")

    cursors = state.get_cursors(cfg, [camera_id])[camera_id]
    video_pu = cursors["VIDEO"]
    detect_pu = cursors["DETECT"]

    lo_ms, hi_ms = to_ms(start), to_ms(end)
    recorded: list[dict] = []
    failed_ranges: list[tuple[int, int]] = []
    minutes: list[dict] = []

    # 1) 処理済み位置より前: DynamoDB の区間の件
    s3_ranges: list[tuple[int, int]] = []
    if video_pu is not None and video_pu > start:
        q_start = floor_minute(start)
        q_end = min(ceil_minute(end), floor_minute(video_pu))
        items = state.query_minutes(cfg, camera_id, q_start, q_end,
                                    ["video_status", "segments", "files.sprite"])
        for item in items:
            minute = parse_ts(item["sk"], "sk")
            status = item.get("video_status")
            m_ms = to_ms(minute)
            if status == "DONE":
                s3_ranges.extend(_segments_of(item, minute))
                sprite = (item.get("files") or {}).get("sprite") or _media_key(camera_id, minute, "sprite.jpg")
                if m_ms + 60_000 > lo_ms and m_ms < hi_ms:
                    minutes.append({"start": iso(minute), "sprite": cfg.media_url(sprite)})
            elif status == "FAILED":
                failed_ranges.append((m_ms, m_ms + 60_000))
        boundary_ms = to_ms(floor_minute(video_pu))
        for s, e in kvs.merge_ranges(s3_ranges, MERGE_GAP_MS):
            s, e = max(s, lo_ms), min(e, hi_ms, boundary_ms)
            if e > s:
                recorded.append({"start": iso_auto(from_ms(s)), "end": iso_auto(from_ms(e)), "source": "s3"})

    # 2) 処理済み位置より後: KVS の ListFragments(通常は lag + 1 分程度)
    kvs_start = max(start, video_pu) if video_pu is not None else start
    if kvs_start < end:
        for s, e in kvs.list_fragment_ranges(cfg, camera_id, kvs_start, end):
            recorded.append({"start": iso_auto(from_ms(s)), "end": iso_auto(from_ms(e)), "source": "kvs"})

    failed = []
    for s, e in kvs.merge_ranges(failed_ranges, 0):
        s, e = max(s, lo_ms), min(e, hi_ms)
        if e > s:
            failed.append({"start": iso_auto(from_ms(s)), "end": iso_auto(from_ms(e))})

    return {
        "camera_id": camera_id,
        "from": iso_auto(start),
        "to": iso_auto(end),
        "now": iso(t_now),
        "video_processed_until": iso(video_pu) if video_pu else None,
        "detect_processed_until": iso(detect_pu) if detect_pu else None,
        "recorded": recorded,
        "failed": failed,
        "minutes": minutes,
    }
