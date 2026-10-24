"""Kinesis Video Streams: ListFragments(録画範囲)と HLS セッション URL。"""

from __future__ import annotations

from datetime import datetime, timedelta

from botocore.exceptions import ClientError

from . import aws
from .config import Config
from .http import ApiError, bad_request, not_found
from .timeutil import from_ms, iso, now, parse_ts, to_ms

# 断片どうしの隙間がこれ以下ならつなげる(構想.md の segments と同じ考え方)
MERGE_GAP_MS = 500
HLS_EXPIRES_SECONDS = 3600
ON_DEMAND_MAX = timedelta(hours=24)  # KVS の ON_DEMAND の上限


def _code(e: ClientError) -> str:
    return e.response.get("Error", {}).get("Code", "")


def merge_ranges(ranges: list[tuple[int, int]], gap_ms: int = MERGE_GAP_MS) -> list[tuple[int, int]]:
    """[(開始ms, 終了ms)] を時刻順に並べ、隙間が gap_ms 以下のものをつなげる。"""
    out: list[list[int]] = []
    for s, e in sorted(ranges):
        if out and s - out[-1][1] <= gap_ms:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def list_fragment_ranges(cfg: Config, camera_id: str, start: datetime, end: datetime) -> list[tuple[int, int]]:
    """[start, end) の断片を ListFragments で取り、録画のある範囲(epoch ms)を返す。

    ストリームがない、または断片がないときは空。
    """
    if end <= start:
        return []
    stream = cfg.stream_name(camera_id)
    try:
        client = aws.kvs_archived_media(stream, "LIST_FRAGMENTS")
        kwargs = {
            "StreamName": stream,
            "MaxResults": 1000,
            "FragmentSelector": {
                "FragmentSelectorType": "PRODUCER_TIMESTAMP",
                "TimestampRange": {"StartTimestamp": start, "EndTimestamp": end},
            },
        }
        ranges: list[tuple[int, int]] = []
        while True:
            resp = client.list_fragments(**kwargs)
            for f in resp.get("Fragments", []):
                s = to_ms(f["ProducerTimestamp"])
                ranges.append((s, s + int(f.get("FragmentLengthInMilliseconds", 0))))
            token = resp.get("NextToken")
            if not token:
                break
            kwargs = {"StreamName": stream, "MaxResults": 1000, "NextToken": token}
    except ClientError as e:
        if _code(e) in ("ResourceNotFoundException", "NotAuthorizedException"):
            return []
        raise
    lo, hi = to_ms(start), to_ms(end)
    clipped = [(max(s, lo), min(e, hi)) for s, e in merge_ranges(ranges) if e > lo and s < hi]
    return [(s, e) for s, e in clipped if e > s]


def hls_session(cfg: Config, camera_id: str, query: dict[str, str]) -> dict:
    """GetHLSStreamingSessionURL。呼び出しごとに新しいセッション URL を発行する(URL の共有はスロットリングの元)。"""
    mode = (query.get("mode") or "LIVE").upper()
    if mode not in ("LIVE", "ON_DEMAND"):
        raise bad_request("mode は LIVE か ON_DEMAND を指定してください")
    stream = cfg.stream_name(camera_id)
    kwargs: dict = {
        "StreamName": stream,
        "PlaybackMode": mode,
        "ContainerFormat": "FRAGMENTED_MP4",
        "DisplayFragmentTimestamp": "ALWAYS",  # EXT-X-PROGRAM-DATE-TIME を付けてもらう
        "Expires": HLS_EXPIRES_SECONDS,
    }
    if mode == "ON_DEMAND":
        start = parse_ts(query.get("from"), "from")
        end = parse_ts(query.get("to"), "to")
        if end <= start:
            raise bad_request("to は from より後にしてください")
        if end - start > ON_DEMAND_MAX:
            raise bad_request("ON_DEMAND の範囲は 24 時間以内にしてください")
        kwargs["DiscontinuityMode"] = "ON_DISCONTINUITY"
        kwargs["MaxMediaPlaylistFragmentResults"] = 5000
        kwargs["HLSFragmentSelector"] = {
            "FragmentSelectorType": "PRODUCER_TIMESTAMP",
            "TimestampRange": {"StartTimestamp": start, "EndTimestamp": end},
        }
    else:
        kwargs["HLSFragmentSelector"] = {"FragmentSelectorType": "PRODUCER_TIMESTAMP"}
    issued = now()
    try:
        client = aws.kvs_archived_media(stream, "GET_HLS_STREAMING_SESSION_URL")
        resp = client.get_hls_streaming_session_url(**kwargs)
    except ClientError as e:
        code = _code(e)
        if code == "ResourceNotFoundException":
            # ストリームがない、または範囲に断片がない(LIVE なら配信していない)
            raise not_found(e.response["Error"].get("Message", "断片がありません"), "NO_FRAGMENTS") from e
        if code in ("UnsupportedStreamMediaTypeException", "NoDataRetentionException",
                    "MissingCodecPrivateDataException", "InvalidCodecPrivateDataException"):
            raise ApiError(409, code, e.response["Error"].get("Message", code)) from e
        if code in ("InvalidArgumentException",):
            raise bad_request(e.response["Error"].get("Message", code)) from e
        raise
    return {
        "url": resp["HLSStreamingSessionURL"],
        "expires_at": iso(issued + timedelta(seconds=HLS_EXPIRES_SECONDS)),
        "mode": mode,
    }


__all__ = ["list_fragment_ranges", "hls_session", "merge_ranges", "from_ms"]
