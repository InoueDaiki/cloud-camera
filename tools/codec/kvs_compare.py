#!/usr/bin/env python3
"""実 KVS に H.264 と H.265 を流した結果を、カメラごとに集計する(docs/codec.md)。

デプロイ済みの環境(scripts/deploy.sh の PROJECT。例 cc-tmp-codec)の、指定した時間帯の値を読む。読み取りだけ。

  uv run --with boto3 python tools/codec/kvs_compare.py --project cc-tmp-codec \\
      --cameras cam-001,cam-002 --start 2026-10-24T06:30:00Z --end 2026-10-24T06:50:00Z

集めるもの(カメラごと):
- KVS の取り込みバイト: CloudWatch の PutMedia.IncomingBytes(合計)と、ListFragments の断片サイズの合計
- KVS の HLS(LIVE)のマスタープレイリストの CODECS(kvssink が正しい codec-id で送っているか)
- 区間の件(DynamoDB): bytes、video_duration_ms、detect_duration_ms、sprite_frames、frame_count、detect_count
- S3: clip.mp4、sprite.jpg、index.m3u8 のサイズ
"""

from __future__ import annotations

import argparse
import json
import statistics
import urllib.request
from datetime import UTC, datetime, timedelta

import boto3


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)


def iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def incoming_bytes(cw, stream: str, start: datetime, end: datetime) -> float:
    r = cw.get_metric_statistics(Namespace="AWS/KinesisVideo", MetricName="PutMedia.IncomingBytes",
                                 Dimensions=[{"Name": "StreamName", "Value": stream}],
                                 StartTime=start, EndTime=end, Period=60, Statistics=["Sum"])
    return sum(p["Sum"] for p in r["Datapoints"])


def fragments(kv, stream: str, start: datetime, end: datetime) -> list[dict]:
    ep = kv.get_data_endpoint(StreamName=stream, APIName="LIST_FRAGMENTS")["DataEndpoint"]
    am = boto3.client("kinesis-video-archived-media", endpoint_url=ep)
    out, token = [], None
    while True:
        kw = {"StreamName": stream, "MaxResults": 1000,
              "FragmentSelector": {"FragmentSelectorType": "PRODUCER_TIMESTAMP",
                                   "TimestampRange": {"StartTimestamp": start, "EndTimestamp": end}}}
        if token:
            kw = {"StreamName": stream, "NextToken": token}
        r = am.list_fragments(**kw)
        out += r["Fragments"]
        token = r.get("NextToken")
        if not token:
            return out


def hls_codecs(kv, stream: str) -> str | None:
    """LIVE の HLS セッションのマスタープレイリストから CODECS を読む(配信中のときだけ)。"""
    try:
        ep = kv.get_data_endpoint(StreamName=stream, APIName="GET_HLS_STREAMING_SESSION_URL")["DataEndpoint"]
        am = boto3.client("kinesis-video-archived-media", endpoint_url=ep)
        url = am.get_hls_streaming_session_url(StreamName=stream, PlaybackMode="LIVE",
                                               ContainerFormat="FRAGMENTED_MP4")["HLSStreamingSessionURL"]
        with urllib.request.urlopen(url, timeout=10) as r:  # noqa: S310 - KVS が返した https URL
            text = r.read().decode()
        for line in text.splitlines():
            if line.startswith("#EXT-X-STREAM-INF"):
                return line.split("CODECS=", 1)[1].split('"')[1] if "CODECS=" in line else line
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"
    return None


def minutes(ddb, table: str, camera: str, start: datetime, end: datetime) -> list[dict]:
    items, kw = [], {
        "TableName": table,
        "KeyConditionExpression": "camera_id = :c AND sk BETWEEN :a AND :b",
        "ExpressionAttributeValues": {":c": {"S": camera}, ":a": {"S": iso(start)},
                                      ":b": {"S": iso(end - timedelta(minutes=1))}},
    }
    while True:
        r = ddb.query(**kw)
        items += r["Items"]
        if "LastEvaluatedKey" not in r:
            return items
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def num(item: dict, key: str) -> float | None:
    v = item.get(key)
    return float(v["N"]) if v and "N" in v else None


def stats(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    v = sorted(values)
    return {"n": len(v), "mean": round(statistics.fmean(v), 1), "median": round(statistics.median(v), 1),
            "p90": round(v[min(len(v) - 1, int(len(v) * 0.9))], 1), "max": round(v[-1], 1)}


def s3_sizes(s3, bucket: str, camera: str, start: datetime, end: datetime) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {"clip.mp4": [], "sprite.jpg": [], "index.m3u8": []}
    t = start
    while t < end:
        prefix = f"{camera}/{t:%Y/%m/%d/%H/%M}/"
        for o in s3.list_objects_v2(Bucket=bucket, Prefix=prefix).get("Contents", []):
            name = o["Key"].rsplit("/", 1)[-1]
            if name in out:
                out[name].append(o["Size"])
        t += timedelta(minutes=1)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", required=True)
    ap.add_argument("--cameras", required=True)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    a = ap.parse_args()
    if not a.project.startswith("cc-tmp-"):
        raise SystemExit("--project は cc-tmp- で始まる一時環境だけ")
    start, end = parse_ts(a.start), parse_ts(a.end)
    sess = boto3.Session()
    acct = sess.client("sts").get_caller_identity()["Account"]
    cw, kv, ddb, s3 = (sess.client(n) for n in ("cloudwatch", "kinesisvideo", "dynamodb", "s3"))
    table, bucket = f"{a.project}-state", f"{a.project}-media-{acct}"
    seconds = (end - start).total_seconds()
    result = {}
    for cam in a.cameras.split(","):
        stream = f"{a.project}-{cam}"
        desc = kv.describe_stream(StreamName=stream)["StreamInfo"]
        frs = fragments(kv, stream, start, end)
        frag_bytes = sum(f["FragmentSizeInBytes"] for f in frs)
        inc = incoming_bytes(cw, stream, start, end)
        items = [i for i in minutes(ddb, table, cam, start, end) if i.get("video_status", {}).get("S") == "DONE"]
        sizes = s3_sizes(s3, bucket, cam, start, end)
        result[cam] = {
            "stream": stream, "media_type": desc.get("MediaType"), "hls_codecs": hls_codecs(kv, stream),
            "seconds": seconds,
            "putmedia_incoming_bytes": inc, "putmedia_kbps": round(inc * 8 / seconds / 1000, 1),
            "fragments": len(frs), "fragment_bytes": frag_bytes,
            "fragment_kbps": round(frag_bytes * 8 / seconds / 1000, 1),
            "minutes_done": len(items),
            "video_duration_ms": stats([v for i in items if (v := num(i, "video_duration_ms")) is not None]),
            "detect_duration_ms": stats([v for i in items if (v := num(i, "detect_duration_ms")) is not None]),
            "segment_bytes": stats([v for i in items if (v := num(i, "bytes")) is not None]),
            "sprite_frames": stats([v for i in items if (v := num(i, "sprite_frames")) is not None]),
            "detect_frames": stats([v for i in items if (v := num(i, "frame_count")) is not None]),
            "detect_count": stats([v for i in items if (v := num(i, "detect_count")) is not None]),
            "s3_bytes": {k: stats([float(x) for x in v]) for k, v in sizes.items()},
        }
    print(json.dumps(result, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
