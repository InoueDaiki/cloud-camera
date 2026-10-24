"""KVS の読み出し系 API のヘルパー(動画の退避と物体検出で共用)。

- GetDataEndpoint の結果をプロセス内でキャッシュする(ウォームスタートで使い回す)。run_pipeline は処理済み位置の件にも保存し、
  別の実行環境の起動でも使い回す(export_endpoints / import_endpoints)。
- ListFragments / GetImages のページ送りをまとめる。同じ ProducerTimestamp の断片(再送の重複)は 1 つにする。
- ListFragments の結果から録画範囲(segments)とウォーターマークを作る。
"""

from __future__ import annotations

import base64
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import boto3
from botocore.config import Config

from .timeutil import epoch_ms, to_utc

# GetDataEndpoint の APIName
LIST_FRAGMENTS = "LIST_FRAGMENTS"
GET_IMAGES = "GET_IMAGES"
GET_HLS_STREAMING_SESSION_URL = "GET_HLS_STREAMING_SESSION_URL"

ENDPOINT_TTL_SECONDS = 3600
_BOTO_CONFIG = Config(retries={"max_attempts": 5, "mode": "standard"}, connect_timeout=5, read_timeout=30)

_lock = threading.Lock()
# (ストリーム, APIName) → (エンドポイント, 取得時刻 monotonic, 取得時刻 epoch 秒)
_endpoints: dict[tuple[str, str], tuple[str, float, float]] = {}
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
        _endpoints[key] = (ep, now, time.time())
    return ep


def export_endpoints(stream_name: str) -> dict[str, list]:
    """ストリームのキャッシュ済みエンドポイントを `{APIName: [endpoint, 取得時刻(epoch 秒)]}` で返す(期限内のものだけ)。

    run_pipeline が処理済み位置の件(`kvs_endpoints`)に保存し、次の起動(別の実行環境でも)で import_endpoints する。
    台数が多いと 1 つの実行環境が同じカメラを担当する確率が下がり、プロセス内のキャッシュが効かなくなるため
    (GetDataEndpoint はアカウントあたり 300 TPS が上限。docs/scale.md)。
    """
    now = time.monotonic()
    with _lock:
        return {
            api: [ep, int(wall)]
            for (s, api), (ep, mono, wall) in _endpoints.items()
            if s == stream_name and now - mono < ENDPOINT_TTL_SECONDS
        }


def import_endpoints(stream_name: str, data: Any) -> None:
    """export_endpoints の形の値をキャッシュに入れる。期限切れ、形の違うもの、手元の方が新しいものは無視する。"""
    if not isinstance(data, dict):
        return
    now_mono, now_wall = time.monotonic(), time.time()
    with _lock:
        for api, v in data.items():
            try:
                ep, fetched = str(v[0]), float(v[1])
            except (TypeError, ValueError, IndexError, KeyError):
                continue
            age = now_wall - fetched
            if not ep.startswith("https://") or age < 0 or age >= ENDPOINT_TTL_SECONDS:
                continue
            key = (stream_name, str(api))
            cur = _endpoints.get(key)
            if cur is not None and cur[2] >= fetched:
                continue
            _endpoints[key] = (ep, now_mono - age, fetched)


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
        _fragment_cache.clear()
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


# 1 回の起動の間だけ使う ListFragments(プロデューサー時刻)の結果。run_pipeline がウォーターマークを
# 求めるために取った「カーソル以降の断片」を置き、process_minute の list_fragments はここから返す(呼び出しを増やさない)
_fragment_cache: dict[str, tuple[int, int, list[Fragment]]] = {}


def set_fragment_cache(stream_name: str, start: datetime, end: datetime, fragments: list[Fragment]) -> None:
    """[start, end) の断片の一覧をキャッシュする(プロデューサー時刻、dedupe 済みのもの)。"""
    with _lock:
        _fragment_cache[stream_name] = (epoch_ms(start), epoch_ms(end), list(fragments))


def clear_fragment_cache(stream_name: str | None = None) -> None:
    with _lock:
        if stream_name is None:
            _fragment_cache.clear()
        else:
            _fragment_cache.pop(stream_name, None)


def dedupe_fragments(fragments: list[Fragment]) -> list[Fragment]:
    """同じ ProducerTimestamp の断片を 1 つにする(時刻順で返す)。

    回線断からの復旧時、kvssink は ACK を受け取れなかった断片を送り直すので、同じ時刻の断片が KVS に 2 つ入りうる。
    大きい方(途中で切れていない方)を残し、同じ大きさなら先に届いた方(断片番号の小さい方)を残す。
    """
    best: dict[int, Fragment] = {}
    for f in fragments:
        cur = best.get(f.producer_ms)
        if cur is None or (f.size, -int(f.number)) > (cur.size, -int(cur.number)):
            best[f.producer_ms] = f
    return sorted(best.values(), key=lambda x: (x.producer_ms, int(x.number)))


def list_fragments(stream_name: str, start: datetime, end: datetime, *, selector: str = "PRODUCER_TIMESTAMP") -> list[Fragment]:
    """プロデューサー時刻が [start, end) で始まる断片を、時刻順に返す(同じ時刻の重複は 1 つにする)。

    区間をまたぐ断片は「開始時刻が属する区間」に割り当てる、という方針をここで実現する。
    ListFragments の範囲指定の端の扱いに依存しないよう、開始時刻で改めて絞り込む。
    run_pipeline がキャッシュした範囲に収まるなら、API を呼ばずにキャッシュから返す。
    selector="SERVER_TIMESTAMP" なら、サーバー時刻(KVS に届いた時刻)が [start, end) の断片を返す(重複排除しない)。
    """
    start_ms, end_ms = epoch_ms(start), epoch_ms(end)
    if selector == "PRODUCER_TIMESTAMP":
        with _lock:
            hit = _fragment_cache.get(stream_name)
        if hit and hit[0] <= start_ms and end_ms <= hit[1]:
            return [f for f in hit[2] if start_ms <= f.producer_ms < end_ms]
    client = archived_media_client(stream_name, LIST_FRAGMENTS)
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
    if selector == "PRODUCER_TIMESTAMP":
        return dedupe_fragments(list(seen.values()))
    return sorted(seen.values(), key=lambda x: (x.producer_ms, int(x.number)))


def watermark_ms(fragments: list[Fragment]) -> int | None:
    """ウォーターマーク: 届いている断片のうち最も新しいものの終わり(プロデューサー時刻 + 長さ、epoch ミリ秒)。

    kvssink は断片を時刻順に送り、回線断のあとはバッファの未 ACK 分を古い順に送り直す。
    よって、ウォーターマークより前に始まる断片はすべて KVS に届いている(とみなせる)。
    """
    if not fragments:
        return None
    return max(f.end_ms for f in fragments)


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
    # 再送で同じ時刻の断片が 2 つあると、同じ時刻の画像が重なりうる。中身のある方を 1 枚残す
    best: dict[int, Image] = {}
    for im in out:
        cur = best.get(im.ts_ms)
        if cur is None or (cur.content is None and im.content is not None):
            best[im.ts_ms] = im
    return sorted(best.values(), key=lambda x: x.ts_ms)
