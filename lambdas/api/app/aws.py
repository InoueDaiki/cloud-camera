"""boto3 クライアントの生成。テストでは monkeypatch で差し替える。"""

from __future__ import annotations

import functools

import boto3
from botocore.config import Config as BotoConfig

_RETRY = BotoConfig(retries={"max_attempts": 3, "mode": "standard"}, connect_timeout=3, read_timeout=20)


@functools.cache
def client(service: str, endpoint_url: str | None = None):
    return boto3.client(service, endpoint_url=endpoint_url, config=_RETRY)


@functools.cache
def table(name: str):
    return boto3.resource("dynamodb", config=_RETRY).Table(name)


def iot_data(endpoint: str):
    if endpoint and not endpoint.startswith("http"):
        endpoint = f"https://{endpoint}"
    return client("iot-data", endpoint or None)


@functools.cache
def _kvs_endpoint(stream_name: str, api_name: str) -> str:
    resp = client("kinesisvideo").get_data_endpoint(StreamName=stream_name, APIName=api_name)
    return resp["DataEndpoint"]


def kvs_archived_media(stream_name: str, api_name: str):
    """ストリームごとのデータエンドポイントに向けた kinesis-video-archived-media クライアント。"""
    return client("kinesis-video-archived-media", _kvs_endpoint(stream_name, api_name))


def reset_caches() -> None:
    for f in (client, table, _kvs_endpoint):
        if hasattr(f, "cache_clear"):  # テストで差し替えられていることがある
            f.cache_clear()
