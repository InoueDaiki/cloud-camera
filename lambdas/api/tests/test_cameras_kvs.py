import json

import boto3
import pytest
from botocore.exceptions import ClientError
from conftest import event

import handler
from app import aws


@pytest.fixture
def iot(ddb, monkeypatch):
    # moto の iot-data はエンドポイント指定なしで動かす
    monkeypatch.setenv("IOT_DATA_ENDPOINT", "")
    iot = boto3.client("iot", region_name="ap-northeast-1")
    iot.create_thing(thingName="test-gw-001")
    data = boto3.client("iot-data", region_name="ap-northeast-1")
    data.update_thing_shadow(thingName="test-gw-001", payload=json.dumps({"state": {
        "desired": {"cameras": {"cam-001": {"onvif_url": "http://emulator:8080/onvif/device_service",
                                            "username": "admin", "password": "admin", "streaming": True}}},
        "reported": {"cameras": {"cam-001": {"streaming": True, "codec": "H265", "resolution": "1920x1080",
                                             "error": None}}},
    }}).encode())
    return data


def test_list_cameras(iot, ddb):
    ddb.put_item(Item={"camera_id": "cam-001", "sk": "CURSOR#VIDEO", "processed_until": "2026-10-24T10:25:00Z"})
    r = handler.lambda_handler(event("GET", "/api/cameras"), None)
    assert r["statusCode"] == 200, r["body"]
    cams = json.loads(r["body"])["cameras"]
    assert [c["camera_id"] for c in cams] == ["cam-001", "cam-002"]
    c1 = cams[0]
    assert c1["gateway_id"] == "gw-001"
    assert c1["stream_name"] == "test-cam-001"
    assert c1["reported"]["codec"] == "H265"
    assert c1["desired"]["streaming"] is True
    assert c1["desired"]["password"] == "***"  # 秘密は返さない
    assert c1["video_processed_until"] == "2026-10-24T10:25:00Z"
    assert c1["detect_processed_until"] is None
    assert cams[1]["reported"] is None
    assert c1["online"] is None  # シャドウに死活がなければ不明


@pytest.mark.parametrize("online, age_s, expected", [
    (True, 10, True),
    (True, 120, False),  # last_seen が古い = 異常終了
    (False, 10, False),
])
def test_list_cameras_online(iot, online, age_s, expected):
    from datetime import datetime, timedelta, timezone
    seen = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
    iot.update_thing_shadow(thingName="test-gw-001", payload=json.dumps(
        {"state": {"reported": {"online": online, "last_seen": seen}}}).encode())
    r = handler.lambda_handler(event("GET", "/api/cameras"), None)
    assert json.loads(r["body"])["cameras"][0]["online"] is expected


def test_set_streaming(iot):
    r = handler.lambda_handler(event("PUT", "/api/cameras/cam-001/streaming", body={"streaming": False}), None)
    assert r["statusCode"] == 200, r["body"]
    shadow = json.loads(iot.get_thing_shadow(thingName="test-gw-001")["payload"].read())
    desired = shadow["state"]["desired"]["cameras"]["cam-001"]
    assert desired["streaming"] is False
    assert desired["onvif_url"].startswith("http://emulator")  # 他の設定は残る


@pytest.mark.parametrize("body", [{}, {"streaming": "yes"}, None])
def test_set_streaming_bad_body(iot, body):
    ev = event("PUT", "/api/cameras/cam-001/streaming", body=body)
    r = handler.lambda_handler(ev, None)
    assert r["statusCode"] == 400
    assert set(json.loads(r["body"])["error"]) == {"code", "message"}


def test_routing_errors(ddb):
    r = handler.lambda_handler(event("GET", "/api/nope"), None)
    assert r["statusCode"] == 404 and json.loads(r["body"])["error"]["code"] == "NOT_FOUND"
    r = handler.lambda_handler(event("DELETE", "/api/cameras"), None)
    assert r["statusCode"] == 405 and json.loads(r["body"])["error"]["code"] == "METHOD_NOT_ALLOWED"
    r = handler.lambda_handler(event("POST", "/api/search"), None)
    assert r["statusCode"] == 400


class FakeHls:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def get_hls_streaming_session_url(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise ClientError({"Error": {"Code": self.error, "Message": "No fragments found"}}, "GetHLS")
        return {"HLSStreamingSessionURL": f"https://b-1234.kinesisvideo/hls/v1/getHLSMasterPlaylist.m3u8?SessionToken=t{len(self.calls)}"}


def test_kvs_hls_live(monkeypatch):
    stub = FakeHls()
    seen = []
    monkeypatch.setattr(aws, "kvs_archived_media", lambda s, a: (seen.append((s, a)), stub)[1])
    r = handler.lambda_handler(event("GET", "/api/cameras/cam-001/kvs-hls", {"mode": "LIVE"}), None)
    body = json.loads(r["body"])
    assert r["statusCode"] == 200 and body["url"].endswith("t1") and body["expires_at"].endswith("Z")
    kw = stub.calls[0]
    assert kw["StreamName"] == "test-cam-001" and kw["PlaybackMode"] == "LIVE"
    assert kw["ContainerFormat"] == "FRAGMENTED_MP4"
    assert seen[0] == ("test-cam-001", "GET_HLS_STREAMING_SESSION_URL")
    # 呼ぶたびに新しい URL
    r2 = handler.lambda_handler(event("GET", "/api/cameras/cam-001/kvs-hls", {}), None)
    assert json.loads(r2["body"])["url"].endswith("t2")


def test_kvs_hls_on_demand(monkeypatch):
    stub = FakeHls()
    monkeypatch.setattr(aws, "kvs_archived_media", lambda s, a: stub)
    r = handler.lambda_handler(event("GET", "/api/cameras/cam-001/kvs-hls",
                                     {"mode": "ON_DEMAND", "from": "2026-10-24T10:20:00Z",
                                      "to": "2026-10-24T10:30:00Z"}), None)
    assert r["statusCode"] == 200
    sel = stub.calls[0]["HLSFragmentSelector"]
    assert sel["FragmentSelectorType"] == "PRODUCER_TIMESTAMP"
    assert sel["TimestampRange"]["StartTimestamp"].isoformat() == "2026-10-24T10:20:00+00:00"
    r = handler.lambda_handler(event("GET", "/api/cameras/cam-001/kvs-hls", {"mode": "ON_DEMAND"}), None)
    assert r["statusCode"] == 400
    r = handler.lambda_handler(event("GET", "/api/cameras/cam-001/kvs-hls", {"mode": "X"}), None)
    assert r["statusCode"] == 400


def test_kvs_hls_no_fragments(monkeypatch):
    monkeypatch.setattr(aws, "kvs_archived_media", lambda s, a: FakeHls("ResourceNotFoundException"))
    r = handler.lambda_handler(event("GET", "/api/cameras/cam-001/kvs-hls", {"mode": "LIVE"}), None)
    assert r["statusCode"] == 404
    assert json.loads(r["body"])["error"]["code"] == "NO_FRAGMENTS"
