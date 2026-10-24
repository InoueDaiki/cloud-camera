"""ローカル開発用サーバー。handler.lambda_handler をそのまま HTTP で動かす(実 AWS に向けて使う)。

    cd lambdas/api
    CAMERAS='{"cam-001":"gw-001"}' MEDIA_BUCKET=... IOT_DATA_ENDPOINT=... \\
      mise exec -- uv run python local_server.py --port 8787

- /api/* は API Gateway のペイロード 2.0 のイベントに変換して handler に渡す
- /media/* は MEDIA_BUCKET の同じキーを S3 から返す(Range 対応)。
  vod.m3u8 の URI は /media/... なので、CloudFront なしでも hls.js で再生を試せる
ビューワー(Vite)からは server.proxy で /api と /media をこのサーバーに向ける。
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import handler  # noqa: E402
from app import aws  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, status: int, headers: dict, body: bytes) -> None:
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _media(self, path: str) -> None:
        bucket = os.environ.get("MEDIA_BUCKET")
        if not bucket:
            self._send(404, {"content-type": "text/plain"}, b"MEDIA_BUCKET is not set")
            return
        key = urllib.parse.unquote(path[len("/media/"):])
        kwargs = {"Bucket": bucket, "Key": key}
        rng = self.headers.get("Range")
        if rng:
            kwargs["Range"] = rng
        try:
            obj = aws.client("s3").get_object(**kwargs)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            status = 404 if code in ("NoSuchKey", "404") else 416 if code == "InvalidRange" else 502
            self._send(status, {"content-type": "text/plain"}, code.encode())
            return
        body = obj["Body"].read()
        headers = {"content-type": obj.get("ContentType", "application/octet-stream"),
                   "accept-ranges": "bytes"}
        if obj.get("ContentRange"):
            headers["content-range"] = obj["ContentRange"]
        self._send(206 if rng else 200, headers, body)

    def _api(self) -> None:
        url = urllib.parse.urlsplit(self.path)
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length).decode("utf-8") if length else ""
        query = dict(urllib.parse.parse_qsl(url.query, keep_blank_values=True))
        event = {
            "version": "2.0",
            "rawPath": url.path,
            "rawQueryString": url.query,
            "queryStringParameters": query or None,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "requestContext": {"http": {"method": self.command, "path": url.path}},
            "body": body or None,
            "isBase64Encoded": False,
        }
        resp = handler.lambda_handler(event, None)
        out = resp.get("body") or ""
        self._send(resp["statusCode"], resp.get("headers") or {}, out.encode("utf-8"))

    def _route(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/media/") and self.command in ("GET", "HEAD"):
            self._media(path)
        elif path.startswith("/api/"):
            self._api()
        else:
            self._send(404, {"content-type": "text/plain"}, b"not found")

    do_GET = do_PUT = do_POST = do_HEAD = do_DELETE = _route


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    a = p.parse_args()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"listening on http://{a.host}:{a.port}  (/api/*, /media/*)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
