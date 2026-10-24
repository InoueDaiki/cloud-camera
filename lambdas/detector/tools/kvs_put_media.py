"""MKV(H.264、1 秒ごとのキーフレーム)を KVS の PutMedia で送る検証用ツール。

エッジやエミュレーターなしで、GetImages を実際の KVS で試すために使う。
プロデューサー時刻は --start(ISO 8601、UTC)から。--repeat で同じファイルを続けて送る。

  uv run python tools/kvs_put_media.py cc-tmp-detect-kvs person60.mkv --start 2026-10-24T03:20:00Z --repeat 3
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest


def put_media(stream: str, body: bytes, start: datetime) -> list[dict]:
    session = boto3.Session()
    ep = session.client("kinesisvideo").get_data_endpoint(StreamName=stream, APIName="PUT_MEDIA")["DataEndpoint"]
    url = ep + "/putMedia"
    headers = {
        "x-amzn-stream-name": stream,
        "x-amzn-fragment-timecode-type": "RELATIVE",
        "x-amzn-producer-start-timestamp": f"{start.timestamp():.3f}",
        "Content-Type": "application/octet-stream",
    }
    req = AWSRequest(method="POST", url=url, data=body, headers=headers)
    # 本文はストリーミングで読まれるので、署名には本文のハッシュを含めない(UNSIGNED-PAYLOAD)
    req.context["payload_signing_enabled"] = False
    SigV4Auth(session.get_credentials(), "kinesisvideo", session.region_name).add_auth(req)
    r = urllib.request.Request(url, data=body, headers=dict(req.headers), method="POST")
    acks = []
    try:
        resp = urllib.request.urlopen(r, timeout=300)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{e.code}: {e.read()[:500]!r}") from e
    with resp:
        buf = b""
        for chunk in iter(lambda: resp.read(4096), b""):
            buf += chunk
            # ACK は JSON が連続して届く
            while True:
                buf = buf.lstrip()
                if not buf:
                    break
                try:
                    obj, end = json.JSONDecoder().raw_decode(buf.decode())
                except ValueError:
                    break
                acks.append(obj)
                buf = buf.decode()[end:].encode()
    return acks


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("stream")
    ap.add_argument("mkv")
    ap.add_argument("--start", required=True)
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--seconds", type=int, default=60, help="1 ファイルの長さ(秒)")
    a = ap.parse_args()
    body = open(a.mkv, "rb").read()
    start = datetime.fromisoformat(a.start.replace("Z", "+00:00"))
    for i in range(a.repeat):
        t = start + timedelta(seconds=a.seconds * i)
        acks = put_media(a.stream, body, t)
        kinds: dict[str, int] = {}
        for x in acks:
            kinds[x.get("EventType", "?")] = kinds.get(x.get("EventType", "?"), 0) + 1
        errors = [x for x in acks if x.get("EventType") == "ERROR"]
        print(t.isoformat(), kinds, errors[:2])


if __name__ == "__main__":
    main()
