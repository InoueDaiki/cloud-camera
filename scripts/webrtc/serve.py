"""KVS WebRTC の確認用ビューワーを配信し、視聴に必要な情報(署名付き WSS URL と ICE サーバー)を発行するローカルサーバー。

本実装では発行部分(issue_viewer_session)を API の Lambda(`GET /api/cameras/{id}/webrtc`)に移す想定。
ブラウザには AWS の認証情報を渡さない。渡すのは 5 分で失効する署名付き URL と、5 分で失効する TURN の認証情報だけ。

    mise exec -- uv run --with boto3 scripts/webrtc/serve.py --project cc-tmp-webrtc
    # → http://127.0.0.1:8765/?camera=cam-001

GET /api/cameras/{camera_id}/webrtc → {
    "channel_name", "channel_arn", "region", "client_id",
    "wss_url": 署名付きの ConnectAsViewer の URL(X-Amz-Expires=299),
    "ice_servers": [{"urls": [...], "username"?, "credential"?}],  # STUN 1 件 + TURN(GetIceServerConfig)
    "expires_at": ISO 8601(署名付き URL と TURN 認証情報の早い方)
}
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import time
import uuid
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode, urlparse

import boto3
from botocore.auth import SigV4QueryAuth
from botocore.awsrequest import AWSRequest

VIEWER_DIR = Path(__file__).resolve().parent / "viewer"
PRESIGN_EXPIRES = 299  # JS SDK の SigV4RequestSigner と同じ(KVS が受け付ける上限は未公表)

# チャネルの ARN とエンドポイントは変わらないのでキャッシュする(DescribeSignalingChannel 5 TPS/チャネルの節約)
_channel_cache: dict[str, tuple[str, str, str]] = {}


def channel_endpoints(kv, channel_name: str) -> tuple[str, str, str]:
    if channel_name not in _channel_cache:
        arn = kv.describe_signaling_channel(ChannelName=channel_name)["ChannelInfo"]["ChannelARN"]
        eps = kv.get_signaling_channel_endpoint(
            ChannelARN=arn,
            SingleMasterChannelEndpointConfiguration={"Protocols": ["WSS", "HTTPS"], "Role": "VIEWER"},
        )["ResourceEndpointList"]
        by = {e["Protocol"]: e["ResourceEndpoint"] for e in eps}
        _channel_cache[channel_name] = (arn, by["WSS"], by["HTTPS"])
    return _channel_cache[channel_name]


def presign_wss(session: boto3.Session, region: str, wss_endpoint: str, channel_arn: str, client_id: str) -> str:
    """ConnectAsViewer の署名付き URL(SigV4 クエリ署名、サービス名 kinesisvideo)。"""
    host = urlparse(wss_endpoint).netloc
    query = urlencode({"X-Amz-ChannelARN": channel_arn, "X-Amz-ClientId": client_id})
    req = AWSRequest(method="GET", url=f"https://{host}/?{query}", headers={"Host": host})
    creds = session.get_credentials().get_frozen_credentials()
    SigV4QueryAuth(creds, "kinesisvideo", region, expires=PRESIGN_EXPIRES).add_auth(req)
    return "wss://" + req.url[len("https://") :]


def issue_viewer_session(session: boto3.Session, region: str, channel_name: str) -> dict:
    kv = session.client("kinesisvideo", region_name=region)
    arn, wss, https = channel_endpoints(kv, channel_name)
    client_id = f"viewer-{uuid.uuid4().hex[:12]}"
    sig = session.client("kinesis-video-signaling", region_name=region, endpoint_url=https)
    ice = sig.get_ice_server_config(ChannelARN=arn, ClientId=client_id, Service="TURN")["IceServerList"]
    ice_servers = [{"urls": [f"stun:stun.kinesisvideo.{region}.amazonaws.com:443"]}]
    ttl = PRESIGN_EXPIRES
    for s in ice:
        ice_servers.append({"urls": s["Uris"], "username": s["Username"], "credential": s["Password"]})
        ttl = min(ttl, s.get("Ttl", ttl))
    return {
        "channel_name": channel_name,
        "channel_arn": arn,
        "region": region,
        "client_id": client_id,
        "wss_endpoint": wss,
        "wss_url": presign_wss(session, region, wss, arn, client_id),
        "ice_servers": ice_servers,
        "expires_at": (datetime.now(UTC) + timedelta(seconds=ttl)).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }


def make_handler(session: boto3.Session, region: str, project: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # noqa: N802 - 標準ライブラリの名前
            print(f"{self.address_string()} {fmt % args}", flush=True)

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            path = urlparse(self.path).path
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[:2] == ["api", "cameras"] and parts[3] == "webrtc":
                t0 = time.monotonic()
                try:
                    out = issue_viewer_session(session, region, f"{project}-{parts[2]}")
                    out["issue_ms"] = round((time.monotonic() - t0) * 1000)
                    self._send(200, json.dumps(out).encode(), "application/json")
                except Exception as e:  # 試作なので理由をそのまま返す
                    err = {"error": {"code": type(e).__name__, "message": str(e)}}
                    self._send(500, json.dumps(err).encode(), "application/json")
                return
            f = (VIEWER_DIR / (path.lstrip("/") or "index.html")).resolve()
            if not f.is_file() or VIEWER_DIR not in f.parents:
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, f.read_bytes(), mimetypes.guess_type(f.name)[0] or "application/octet-stream")

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", default="cc-tmp-webrtc", help="チャネル名の接頭辞({project}-{camera_id})")
    ap.add_argument("--profile", default="cloud-camera")
    ap.add_argument("--region", default="ap-northeast-1")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    session = boto3.Session(profile_name=a.profile, region_name=a.region)
    srv = ThreadingHTTPServer((a.host, a.port), make_handler(session, a.region, a.project))
    print(f"http://{a.host}:{a.port}/?camera=cam-001", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
