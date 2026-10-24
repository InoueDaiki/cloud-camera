"""GET /api/cameras/{id}/webrtc: KVS WebRTC のライブを見るための情報(docs/contracts.md の「KVS WebRTC」)。

ブラウザには AWS の認証情報を渡さない。渡すのは、
- ConnectAsViewer の署名付き WSS URL(SigV4 のクエリ署名。299 秒で失効、このチャネルとこのクライアント ID だけに使える)
- ICE サーバー(KVS の STUN と、GetIceServerConfig が返す TURN の一時認証情報。300 秒で失効)
だけ。ブラウザは JS SDK の SignalingClient に「この URL を返すだけの signer」を渡して接続する。

先にシャドウを見て、WebRTC を止めているカメラ(desired.webrtc.enabled=false)や master が動いていない
(reported.webrtc.state が CONNECTED でない)カメラは 409 を返す。ビューワーはすぐ HLS に切り替えられる。
"""

from __future__ import annotations

import os
import secrets
import time
from datetime import timedelta
from urllib.parse import urlencode, urlparse

import boto3
from botocore.auth import SigV4QueryAuth
from botocore.awsrequest import AWSRequest
from botocore.exceptions import ClientError

from . import aws
from .cameras import _camera_state, _get_shadow, _online
from .config import Config
from .http import ApiError, not_found
from .timeutil import iso, now

# JS SDK の SigV4RequestSigner と同じ(KVS が受け付ける上限は公表されていない)
PRESIGN_EXPIRES = 299
# チャネルの ARN とエンドポイントは変わらないので、Lambda のコンテナ内でキャッシュする
# (DescribeSignalingChannel / GetSignalingChannelEndpoint はチャネルあたり 5 TPS)
_CHANNEL_TTL_S = 6 * 3600
_channel_cache: dict[str, tuple[float, str, str, str]] = {}


def reset_cache() -> None:
    _channel_cache.clear()


def _code(e: ClientError) -> str:
    return e.response.get("Error", {}).get("Code", "")


def _region() -> str:
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "ap-northeast-1"


def channel_endpoints(channel_name: str) -> tuple[str, str, str]:
    """(チャネル ARN, viewer の WSS エンドポイント, HTTPS エンドポイント)"""
    hit = _channel_cache.get(channel_name)
    if hit and time.monotonic() - hit[0] < _CHANNEL_TTL_S:
        return hit[1], hit[2], hit[3]
    kv = aws.client("kinesisvideo")
    try:
        arn = kv.describe_signaling_channel(ChannelName=channel_name)["ChannelInfo"]["ChannelARN"]
        eps = kv.get_signaling_channel_endpoint(
            ChannelARN=arn,
            SingleMasterChannelEndpointConfiguration={"Protocols": ["WSS", "HTTPS"], "Role": "VIEWER"},
        )["ResourceEndpointList"]
    except ClientError as e:
        if _code(e) == "ResourceNotFoundException":
            raise not_found(f"シグナリングチャネルがありません: {channel_name}", "CHANNEL_NOT_FOUND") from e
        raise
    by = {e["Protocol"]: e["ResourceEndpoint"] for e in eps}
    _channel_cache[channel_name] = (time.monotonic(), arn, by["WSS"], by["HTTPS"])
    return arn, by["WSS"], by["HTTPS"]


def presign_wss(credentials, region: str, wss_endpoint: str, channel_arn: str, client_id: str,
                expires: int = PRESIGN_EXPIRES) -> str:
    """ConnectAsViewer の署名付き URL(SigV4 クエリ署名、サービス名 kinesisvideo)。"""
    host = urlparse(wss_endpoint).netloc
    query = urlencode({"X-Amz-ChannelARN": channel_arn, "X-Amz-ClientId": client_id})
    req = AWSRequest(method="GET", url=f"https://{host}/?{query}", headers={"Host": host})
    SigV4QueryAuth(credentials, "kinesisvideo", region, expires=expires).add_auth(req)
    return "wss://" + req.url[len("https://"):]


def _check_camera(cfg: Config, camera_id: str) -> list[str]:
    """シャドウを見て、WebRTC で見られないカメラなら 409。見られるなら出せるコーデック(H.265 が先)を返す。"""
    gw = cfg.cameras.get(camera_id)
    if not gw:
        raise not_found(f"ゲートウェイが設定されていないカメラです: {camera_id}", "CAMERA_NOT_FOUND")
    shadow = _get_shadow(cfg, gw)
    reported, desired, _ = _camera_state(shadow, camera_id)
    want = (desired or {}).get("webrtc")
    if isinstance(want, dict) and want.get("enabled") is False:
        raise ApiError(409, "WEBRTC_DISABLED", "このカメラは WebRTC のライブを止めています")
    if _online(shadow) is False:
        raise ApiError(409, "CAMERA_OFFLINE", "カメラ(ゲートウェイ)がオフラインです")
    rtc = (reported or {}).get("webrtc")
    state = rtc.get("state") if isinstance(rtc, dict) else None
    if state != "CONNECTED":
        raise ApiError(409, "WEBRTC_UNAVAILABLE",
                       f"エッジの WebRTC が待ち受けていません(state={state or '不明'})")
    codecs = rtc.get("codecs")
    return [c for c in codecs if c in ("H265", "H264")] if isinstance(codecs, list) else []


def viewer_session(cfg: Config, camera_id: str) -> dict:
    codecs = _check_camera(cfg, camera_id)
    t0 = time.monotonic()
    channel = cfg.channel_name(camera_id)
    region = _region()
    arn, wss, https = channel_endpoints(channel)
    # 視聴者ごとに別のクライアント ID(master は視聴者をこれで区別する)
    client_id = f"viewer-{secrets.token_hex(6)}"
    try:
        ice = aws.client("kinesis-video-signaling", https).get_ice_server_config(
            ChannelARN=arn, ClientId=client_id, Service="TURN")["IceServerList"]
    except ClientError as e:
        if _code(e) == "ResourceNotFoundException":
            _channel_cache.pop(channel, None)  # チャネルを作り直したとき
            raise not_found(f"シグナリングチャネルがありません: {channel}", "CHANNEL_NOT_FOUND") from e
        raise
    ice_servers: list[dict] = [{"urls": [f"stun:stun.kinesisvideo.{region}.amazonaws.com:443"]}]
    ttl = PRESIGN_EXPIRES
    for s in ice:
        ice_servers.append({"urls": s["Uris"], "username": s["Username"], "credential": s["Password"]})
        ttl = min(ttl, int(s.get("Ttl", ttl)))
    creds = boto3.Session().get_credentials().get_frozen_credentials()
    return {
        "camera_id": camera_id,
        "channel_name": channel,
        "channel_arn": arn,
        "region": region,
        "client_id": client_id,
        "wss_endpoint": wss,
        "wss_url": presign_wss(creds, region, wss, arn, client_id),
        "ice_servers": ice_servers,
        "codecs": codecs,
        "expires_at": iso(now() + timedelta(seconds=ttl)),
        "issue_ms": round((time.monotonic() - t0) * 1000),
    }
