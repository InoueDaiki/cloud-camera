import io
import json
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import pytest
from botocore.exceptions import ClientError
from conftest import event

import handler
from app import aws, webrtc

ARN = "arn:aws:kinesisvideo:ap-northeast-1:123456789012:channel/test-cam-001/1761300000000"


def _err(code):
    return ClientError({"Error": {"Code": code, "Message": code}}, "op")


class FakeKv:
    def __init__(self):
        self.calls = []

    def describe_signaling_channel(self, ChannelName):
        self.calls.append(("describe", ChannelName))
        if ChannelName != "test-cam-001":
            raise _err("ResourceNotFoundException")
        return {"ChannelInfo": {"ChannelARN": ARN}}

    def get_signaling_channel_endpoint(self, ChannelARN, SingleMasterChannelEndpointConfiguration):
        self.calls.append(("endpoint", SingleMasterChannelEndpointConfiguration["Role"]))
        return {"ResourceEndpointList": [
            {"Protocol": "WSS", "ResourceEndpoint": "wss://v-1234.kinesisvideo.ap-northeast-1.amazonaws.com"},
            {"Protocol": "HTTPS", "ResourceEndpoint": "https://r-1234.kinesisvideo.ap-northeast-1.amazonaws.com"}]}


class FakeSignaling:
    def __init__(self):
        self.calls = []

    def get_ice_server_config(self, ChannelARN, ClientId, Service):
        self.calls.append((ChannelARN, ClientId, Service))
        return {"IceServerList": [{"Uris": ["turn:1-2-3-4.t-1.kinesisvideo.ap-northeast-1.amazonaws.com:443?transport=udp",
                                            "turns:1-2-3-4.t-1.kinesisvideo.ap-northeast-1.amazonaws.com:443?transport=tcp"],
                                   "Username": "u", "Password": "p", "Ttl": 300}]}


class FakeIotData:
    def __init__(self, shadow):
        self.shadow = shadow

    def get_thing_shadow(self, thingName):
        if self.shadow is None:
            raise _err("ResourceNotFoundException")
        return {"payload": io.BytesIO(json.dumps(self.shadow).encode())}


def _shadow(rtc=None, desired_rtc=None, online=True):
    rep_cam = {"streaming": True, "codec": "H265"}
    if rtc is not None:
        rep_cam["webrtc"] = rtc
    des_cam = {"streaming": True}
    if desired_rtc is not None:
        des_cam["webrtc"] = desired_rtc
    seen = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"state": {"reported": {"online": online, "last_seen": seen, "cameras": {"cam-001": rep_cam}},
                      "desired": {"cameras": {"cam-001": des_cam}}}}


CONNECTED = {"enabled": True, "channel": "test-cam-001", "state": "CONNECTED", "codecs": ["H265", "H264"], "viewers": 0}


@pytest.fixture
def fakes(monkeypatch):
    kv, sig = FakeKv(), FakeSignaling()
    iot = FakeIotData(_shadow(CONNECTED))

    def client(service, endpoint_url=None):
        if service == "kinesisvideo":
            return kv
        if service == "kinesis-video-signaling":
            assert endpoint_url == "https://r-1234.kinesisvideo.ap-northeast-1.amazonaws.com"
            return sig
        raise AssertionError(service)

    monkeypatch.setattr(aws, "client", client)
    monkeypatch.setattr(aws, "iot_data", lambda endpoint: iot)
    webrtc.reset_cache()
    yield kv, sig, iot
    webrtc.reset_cache()


def _get(cam="cam-001"):
    r = handler.lambda_handler(event("GET", f"/api/cameras/{cam}/webrtc"), None)
    return r["statusCode"], json.loads(r["body"])


def test_issues_signed_url_and_ice_servers(fakes):
    kv, sig, _ = fakes
    status, body = _get()
    assert status == 200, body
    assert body["channel_name"] == "test-cam-001" and body["channel_arn"] == ARN
    assert body["region"] == "ap-northeast-1" and body["codecs"] == ["H265", "H264"]
    assert body["client_id"].startswith("viewer-")
    assert body["wss_endpoint"] == "wss://v-1234.kinesisvideo.ap-northeast-1.amazonaws.com"
    u = urlparse(body["wss_url"])
    q = parse_qs(u.query)
    assert u.scheme == "wss" and u.netloc == "v-1234.kinesisvideo.ap-northeast-1.amazonaws.com"
    assert q["X-Amz-ChannelARN"] == [ARN] and q["X-Amz-ClientId"] == [body["client_id"]]
    assert q["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"] and q["X-Amz-Expires"] == ["299"]
    assert "/ap-northeast-1/kinesisvideo/aws4_request" in q["X-Amz-Credential"][0]
    assert "X-Amz-Signature" in q
    # 秘密鍵は URL に入らない
    assert "testing" not in body["wss_url"].split("X-Amz-Credential=")[0] + "".join(q["X-Amz-Signature"])
    # STUN + TURN(GetIceServerConfig は viewer のクライアント ID で呼ぶ)
    assert body["ice_servers"][0] == {"urls": ["stun:stun.kinesisvideo.ap-northeast-1.amazonaws.com:443"]}
    assert body["ice_servers"][1]["username"] == "u" and body["ice_servers"][1]["credential"] == "p"
    assert sig.calls == [(ARN, body["client_id"], "TURN")]
    assert body["expires_at"].endswith("Z")
    # 2 回目はチャネルの ARN とエンドポイントをキャッシュから使う。クライアント ID は毎回変える
    status, body2 = _get()
    assert status == 200 and body2["client_id"] != body["client_id"]
    assert [c[0] for c in kv.calls] == ["describe", "endpoint"]
    assert kv.calls[1] == ("endpoint", "VIEWER")


def test_presign_includes_session_token():
    from botocore.credentials import Credentials
    creds = Credentials("AKIDEXAMPLE", "secret", "token-xyz").get_frozen_credentials()
    url = webrtc.presign_wss(creds, "ap-northeast-1", "wss://v-1.example.com", ARN, "viewer-1")
    q = parse_qs(urlparse(url).query)
    assert q["X-Amz-Security-Token"] == ["token-xyz"]
    assert "secret" not in url


@pytest.mark.parametrize("shadow, code, status", [
    (_shadow(CONNECTED, desired_rtc={"enabled": False}), "WEBRTC_DISABLED", 409),
    (_shadow(CONNECTED, online=False), "CAMERA_OFFLINE", 409),
    (_shadow({**CONNECTED, "state": "ERROR"}), "WEBRTC_UNAVAILABLE", 409),
    (_shadow(None), "WEBRTC_UNAVAILABLE", 409),  # 古いエッジ(webrtc を返さない)
    (None, "WEBRTC_UNAVAILABLE", 409),
])
def test_rejects_when_not_available(fakes, shadow, code, status):
    kv, sig, iot = fakes
    iot.shadow = shadow
    st, body = _get()
    assert (st, body["error"]["code"]) == (status, code)
    # 早く断る(KVS には問い合わせない)
    assert kv.calls == [] and sig.calls == []


def test_unknown_camera_and_missing_channel(fakes, monkeypatch):
    st, body = _get("cam-999")
    assert (st, body["error"]["code"]) == (404, "CAMERA_NOT_FOUND")
    # Terraform がチャネルを作っていない(cam-002 のチャネルはない)
    kv, sig, iot = fakes
    iot.shadow = {"state": {"reported": {"cameras": {"cam-002": {"webrtc": CONNECTED}}}}}
    st, body = _get("cam-002")
    assert (st, body["error"]["code"]) == (404, "CHANNEL_NOT_FOUND")


def test_channel_prefix_env(monkeypatch):
    from app import config
    monkeypatch.setenv("WEBRTC_CHANNEL_PREFIX", "other-")
    assert config.load().channel_name("cam-001") == "other-cam-001"
    monkeypatch.delenv("WEBRTC_CHANNEL_PREFIX")
    assert config.load().channel_name("cam-001") == "test-cam-001"
