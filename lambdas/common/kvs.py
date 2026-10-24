"""KVS の読み出し系 API のヘルパー(動画の退避と物体検出で共用)。

- GetDataEndpoint の結果をプロセス内でキャッシュする(ウォームスタートで使い回す)。
- ListFragments / GetImages のページ送りをまとめる。
- ListFragments の結果から録画範囲(segments)を作る。
"""

from __future__ import annotations

import base64
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import boto3
from botocore.config import Config

from .timeutil import epoch_ms, to_utc

# GetDataEndpoint の APIName
LIST_FRAGMENTS = "LIST_FRAGMENTS"
GET_IMAGES = "GET_IMAGES"
GET_CLIP = "GET_CLIP"
GET_HLS_STREAMING_SESSION_URL = "GET_HLS_STREAMING_SESSION_URL"
GET_MEDIA_FOR_FRAGMENT_LIST = "GET_MEDIA_FOR_FRAGMENT_LIST"

ENDPOINT_TTL_SECONDS = 3600
_BOTO_CONFIG = Config(retries={"max_attempts": 5, "mode": "standard"}, connect_timeout=5, read_timeout=30)

_lock = threading.Lock()
_endpoints: dict[tuple[str, str], tuple[str, float]] = {}
_clients: dict[str, Any] = {}
_kv_client: Any = None


def stream_name_for(camera_id: str, prefix: str | None = None) -> str:
    """カメラ ID から KVS ストリーム名を作る。接頭辞は環境変数 STREAM_NAME_PREFIX(例 `cloud-camera-`)。"""
    if prefix is None:
        prefix = os.environ.get("STREAM_NAME_PREFIX", "")
    return f"{prefix}{camera_id}"


def _kinesisvideo():
    global _kv_client
    if _kv_client is None:
        _kv_client = boto3.client("kinesisvideo", config=_BOTO_CONFIG)
    return _kv_client


def get_data_endpoint(stream_name: str, api_name: str) -> str:
    """GetDataEndpoint をキャッシュ付きで呼ぶ。"""
    key = (stream_name, api_name)
    now = time.monotonic()
    with _lock:
        hit = _endpoints.get(key)
        if hit and now - hit[1] < ENDPOINT_TTL_SECONDS:
            return hit[0]
    ep = _kinesisvideo().get_data_endpoint(StreamName=stream_name, APIName=api_name)["DataEndpoint"]
    with _lock:
        _endpoints[key] = (ep, now)
    return ep


def invalidate_endpoint(stream_name: str, api_name: str | None = None) -> None:
    with _lock:
        for k in list(_endpoints):
            if k[0] == stream_name and (api_name is None or k[1] == api_name):
                del _endpoints[k]


def clear_cache() -> None:
    """テスト用。"""
    global _kv_client
    with _lock:
        _endpoints.clear()
        _clients.clear()
        _kv_client = None


def archived_media_client(stream_name: str, api_name: str):
    """kinesis-video-archived-media のクライアント(データエンドポイントごとにキャッシュ)。"""
    ep = get_data_endpoint(stream_name, api_name)
    with _lock:
        c = _clients.get(ep)
    if c is None:
        c = boto3.client("kinesis-video-archived-media", endpoint_url=ep, config=_BOTO_CONFIG)
        with _lock:
            _clients[ep] = c
    return c


@dataclass(frozen=True)
class Fragment:
    number: str
    producer_ms: int  # ProducerTimestamp(epoch ミリ秒)
    server_ms: int  # ServerTimestamp(epoch ミリ秒)
    length_ms: int  # FragmentLengthInMilliseconds
    size: int  # FragmentSizeInBytes

    @property
    def end_ms(self) -> int:
        return self.producer_ms + self.length_ms


def list_fragments(stream_name: str, start: datetime, end: datetime, *, selector: str = "PRODUCER_TIMESTAMP") -> list[Fragment]:
    """プロデューサー時刻が [start, end) で始まる断片を、時刻順に返す。

    区間をまたぐ断片は「開始時刻が属する区間」に割り当てる、という方針をここで実現する。
    ListFragments の範囲指定の端の扱いに依存しないよう、開始時刻で改めて絞り込む。
    selector="SERVER_TIMESTAMP" なら、サーバー時刻(KVS に届いた時刻)が [start, end) の断片を返す。
    """
    client = archived_media_client(stream_name, LIST_FRAGMENTS)
    start_ms, end_ms = epoch_ms(start), epoch_ms(end)
    kwargs: dict[str, Any] = {
        "StreamName": stream_name,
        "MaxResults": 1000,
        "FragmentSelector": {
            "FragmentSelectorType": selector,
            "TimestampRange": {"StartTimestamp": to_utc(start), "EndTimestamp": to_utc(end)},
        },
    }
    seen: dict[str, Fragment] = {}
    while True:
        resp = client.list_fragments(**kwargs)
        for f in resp.get("Fragments", []):
            fr = Fragment(
                number=f["FragmentNumber"],
                producer_ms=epoch_ms(f["ProducerTimestamp"]),
                server_ms=epoch_ms(f["ServerTimestamp"]),
                length_ms=int(f.get("FragmentLengthInMilliseconds", 0)),
                size=int(f.get("FragmentSizeInBytes", 0)),
            )
            t = fr.producer_ms if selector == "PRODUCER_TIMESTAMP" else fr.server_ms
            if start_ms <= t < end_ms:
                seen[fr.number] = fr
        token = resp.get("NextToken")
        if not token:
            break
        kwargs = {"StreamName": stream_name, "NextToken": token, "MaxResults": 1000}
    return sorted(seen.values(), key=lambda x: (x.producer_ms, int(x.number)))


def minute_ready(
    stream_name: str,
    end: datetime,
    now: datetime,
    *,
    quiet_seconds: int = 30,
    max_wait_seconds: int = 600,
) -> tuple[bool, str]:
    """区間 [.., end) の断片が出そろったかを判定する(遅れて届く断片の取りこぼしを防ぐ)。

    プロデューサー(エッジ)の送信が遅れていると、区間の終わりから lag が過ぎても断片がまだ届いていない
    (実測: 送信側の CPU 不足で、届くのが 70 秒遅れた)。KVS へは時刻順に届くので、次の条件で「出そろった」とみなす。

    - 最近 quiet_seconds 秒に届いた断片のうち、プロデューサー時刻が end 以降のものがある(送信が end を越えた)
    - 最近 quiet_seconds 秒に届いた断片がない(送信が止まっている。止まる前の断片はすべて届いている)
    - 区間の終わりから max_wait_seconds 秒が過ぎた(待ちすぎ。そのとき届いている分で処理する)

    戻り値は (出そろったか, 理由)。ListFragments(サーバー時刻)を 1 回呼ぶ。
    """
    now = to_utc(now)
    waited = (now - to_utc(end)).total_seconds()
    if waited >= max_wait_seconds:
        return True, "max_wait"
    recent = list_fragments(
        stream_name, now - timedelta(seconds=quiet_seconds), now + timedelta(seconds=5), selector="SERVER_TIMESTAMP"
    )
    if not recent:
        return True, "quiet"
    newest = max(f.producer_ms for f in recent)
    if newest >= epoch_ms(end):
        return True, "passed"
    return False, f"producer_behind: newest producer {(epoch_ms(end) - newest) / 1000:.1f}s before end"


MAX_PAD_MS = 250


def effective_durations(fragments: list[Fragment], gap_ms: int = 500) -> list[int]:
    """断片ごとの実際の長さ(ミリ秒)。fragments は時刻順。

    FragmentLengthInMilliseconds は「最後のフレームの開始 − 最初のフレームの開始」で、最後の 1 フレーム分
    (15fps なら 67ms)短い(実測: 1 秒ごとの断片で 933ms)。そこで
    - 次の断片が gap_ms 以内に続くなら「次の開始 − この開始」
    - そうでなければ長さ + 隙間の中央値(1 フレーム分の推定。最大 MAX_PAD_MS)
    """
    slacks = sorted(
        n.producer_ms - f.end_ms for f, n in zip(fragments, fragments[1:]) if 0 <= n.producer_ms - f.end_ms <= gap_ms
    )
    pad = min(slacks[len(slacks) // 2], MAX_PAD_MS) if slacks else 0
    out = []
    for i, f in enumerate(fragments):
        nxt = fragments[i + 1] if i + 1 < len(fragments) else None
        if nxt is not None and 0 <= nxt.producer_ms - f.end_ms <= gap_ms:
            out.append(nxt.producer_ms - f.producer_ms)
        else:
            out.append(f.length_ms + pad)
    return out


def build_segments(fragments: list[Fragment], gap_ms: int = 500) -> list[list[int]]:
    """断片の [開始, 終了] を並べ、隙間が gap_ms 以内ならつないだ録画範囲(epoch ミリ秒)を返す。

    終了は effective_durations で補正した長さを使う。
    """
    frs = sorted(fragments, key=lambda x: x.producer_ms)
    segs: list[list[int]] = []
    for f, d in zip(frs, effective_durations(frs, gap_ms)):
        s, e = f.producer_ms, f.producer_ms + d
        if segs and s - segs[-1][1] <= gap_ms:
            segs[-1][1] = max(segs[-1][1], e)
        else:
            segs.append([s, e])
    return segs


@dataclass(frozen=True)
class Image:
    ts_ms: int
    content: bytes | None  # 取れなかったら None
    error: str | None


def get_images(
    stream_name: str,
    start: datetime,
    end: datetime,
    *,
    interval_ms: int = 1000,
    width: int | None = None,
    height: int | None = None,
    fmt: str = "JPEG",
    format_config: dict[str, str] | None = None,
) -> list[Image]:
    """GetImages(プロデューサー時刻)をページ送りして全部返す。時刻順。"""
    client = archived_media_client(stream_name, GET_IMAGES)
    kwargs: dict[str, Any] = {
        "StreamName": stream_name,
        "ImageSelectorType": "PRODUCER_TIMESTAMP",
        "StartTimestamp": to_utc(start),
        "EndTimestamp": to_utc(end),
        "SamplingInterval": interval_ms,
        "Format": fmt,
        "MaxResults": 100,
    }
    if width:
        kwargs["WidthPixels"] = width
    if height:
        kwargs["HeightPixels"] = height
    if format_config:
        kwargs["FormatConfig"] = format_config
    out: list[Image] = []
    while True:
        resp = client.get_images(**kwargs)
        for im in resp.get("Images", []):
            raw = im.get("ImageContent")
            content = None
            if raw:
                content = base64.b64decode(raw) if isinstance(raw, str) else bytes(raw)
            out.append(Image(ts_ms=epoch_ms(im["TimeStamp"]), content=content, error=im.get("Error")))
        token = resp.get("NextToken")
        if not token:
            break
        kwargs["NextToken"] = token
    out.sort(key=lambda x: x.ts_ms)
    return out
