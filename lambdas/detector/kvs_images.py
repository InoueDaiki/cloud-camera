"""KVS の GetImages で 1 分区間を 1 秒間隔の JPEG として取得する(画像は S3 に置かない)。"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import boto3

log = logging.getLogger(__name__)

_endpoint_clients: dict[str, object] = {}


@dataclass
class KvsImage:
    ts: datetime  # 画像の時刻(UTC、ProducerTimestamp 基準)
    data: bytes  # JPEG


def _archived_media(stream_name: str):
    """GET_IMAGES 用のエンドポイントのクライアント(ストリームごとにキャッシュ)。"""
    client = _endpoint_clients.get(stream_name)
    if client is None:
        kv = boto3.client("kinesisvideo")
        ep = kv.get_data_endpoint(StreamName=stream_name, APIName="GET_IMAGES")["DataEndpoint"]
        client = boto3.client("kinesis-video-archived-media", endpoint_url=ep)
        _endpoint_clients[stream_name] = client
    return client


def get_images(
    stream_name: str,
    start: datetime,
    end: datetime,
    *,
    width: int | None = 416,
    interval_ms: int = 1000,
    client=None,
) -> tuple[list[KvsImage], dict]:
    """[start, end) の画像を interval_ms ごとに取得する。

    WidthPixels だけを指定すると、KVS が元のアスペクト比を保って縮小する(16:9、幅 416 なら 416x234)。
    録画がない時刻(NO_MEDIA)やデコードできない時刻(MEDIA_ERROR)は数えて捨てる。
    """
    client = client or _archived_media(stream_name)
    params = {
        "StreamName": stream_name,
        "ImageSelectorType": "PRODUCER_TIMESTAMP",
        "StartTimestamp": start,
        "EndTimestamp": end,
        "SamplingInterval": interval_ms,
        "Format": "JPEG",
        "MaxResults": 100,
    }
    if width:
        params["WidthPixels"] = width
    images: list[KvsImage] = []
    errors: dict[str, int] = {}
    pages = 0
    while True:
        resp = client.get_images(**params)
        pages += 1
        for img in resp.get("Images", []):
            ts = img["TimeStamp"].astimezone(timezone.utc)
            if img.get("Error"):
                errors[img["Error"]] = errors.get(img["Error"], 0) + 1
                continue
            if not (start <= ts < end) or not img.get("ImageContent"):
                continue
            content = img["ImageContent"]
            data = base64.b64decode(content) if isinstance(content, str) else content
            images.append(KvsImage(ts=ts, data=data))
        token = resp.get("NextToken")
        if not token:
            break
        params["NextToken"] = token
    images.sort(key=lambda i: i.ts)
    return images, {"pages": pages, "errors": errors}
