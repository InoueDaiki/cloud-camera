import json
import uuid

import boto3
import pytest
from conftest import event

import handler
from app import aws


class FakeIotData:
    def __init__(self):
        self.published: list[dict] = []

    def publish(self, **kw):
        self.published.append(kw)
        return {}


@pytest.fixture
def fake_iot(monkeypatch):
    fake = FakeIotData()
    monkeypatch.setattr(aws, "iot_data", lambda endpoint: fake)
    return fake


def _post(cam, body):
    r = handler.lambda_handler(event("POST", f"/api/cameras/{cam}/ptz", body=body), None)
    return r["statusCode"], json.loads(r["body"])


def test_post_publishes_cmd(fake_iot):
    status, body = _post("cam-001", {"op": "continuous", "pan": 0.5, "tilt": -0.25, "extra": "無視"})
    assert status == 202
    rid = body["request_id"]
    uuid.UUID(rid)
    [pub] = fake_iot.published
    assert pub["topic"] == "test/gw-001/cmd"  # {p}/{gw}/cmd
    assert pub["qos"] == 1
    msg = json.loads(pub["payload"])
    assert msg == {"action": "ptz", "camera_id": "cam-001", "request_id": rid, "op": "continuous",
                   "pan": 0.5, "tilt": -0.25, "timeout_ms": 10000}


@pytest.mark.parametrize("body, expected", [
    ({"op": "stop"}, {"op": "stop"}),
    ({"op": "home"}, {"op": "home"}),
    ({"op": "absolute", "pan": 1, "tilt": -1, "zoom": 0}, {"op": "absolute", "pan": 1.0, "tilt": -1.0, "zoom": 0.0}),
    ({"op": "relative", "zoom": -0.1}, {"op": "relative", "zoom": -0.1}),
    ({"op": "continuous", "zoom": 1, "timeout_ms": 3000}, {"op": "continuous", "zoom": 1.0, "timeout_ms": 3000}),
    ({"op": "goto_preset", "preset_token": "1"}, {"op": "goto_preset", "preset_token": "1"}),
    ({"op": "set_preset", "preset_name": "玄関"}, {"op": "set_preset", "preset_name": "玄関"}),
])
def test_post_ops(fake_iot, body, expected):
    status, res = _post("cam-002", body)
    assert status == 202, res
    msg = json.loads(fake_iot.published[0]["payload"])
    assert {k: v for k, v in msg.items() if k not in ("action", "camera_id", "request_id")} == expected


@pytest.mark.parametrize("body", [
    None, [], {}, {"op": "fly"},
    {"op": "continuous", "pan": 2},
    {"op": "continuous", "pan": "0.5"},
    {"op": "continuous", "pan": True},
    {"op": "absolute", "zoom": -0.5},
    {"op": "absolute"},
    {"op": "relative"},
    {"op": "continuous", "timeout_ms": 10},
    {"op": "goto_preset"},
    {"op": "goto_preset", "preset_token": 1},
    {"op": "set_preset", "preset_name": "x" * 65},
])
def test_post_bad_body(fake_iot, body):
    status, res = _post("cam-001", body)
    assert status == 400, res
    assert res["error"]["code"] == "BAD_REQUEST"
    assert fake_iot.published == []


def test_post_unknown_camera(fake_iot):
    status, res = _post("cam-999", {"op": "stop"})
    assert status == 404 and res["error"]["code"] == "CAMERA_NOT_FOUND"


def test_post_nan_is_rejected(fake_iot):
    ev = event("POST", "/api/cameras/cam-001/ptz")
    ev["body"] = '{"op": "continuous", "pan": NaN}'
    r = handler.lambda_handler(ev, None)
    assert r["statusCode"] == 400


@pytest.fixture
def shadow(ddb, monkeypatch):
    monkeypatch.setenv("IOT_DATA_ENDPOINT", "")
    boto3.client("iot", region_name="ap-northeast-1").create_thing(thingName="test-gw-001")
    data = boto3.client("iot-data", region_name="ap-northeast-1")
    data.update_thing_shadow(thingName="test-gw-001", payload=json.dumps({"state": {"reported": {"cameras": {
        "cam-001": {"streaming": True, "ptz": {"supported": True, "pan": 0.1, "tilt": 0, "zoom": 0.5,
                                               "presets": [{"token": "1", "name": "玄関"}],
                                               "updated_at": "2026-10-24T10:00:00Z"}},
        "cam-002": {"streaming": True},
    }}}}).encode())
    return data


def test_get_state(shadow):
    r = handler.lambda_handler(event("GET", "/api/cameras/cam-001/ptz"), None)
    assert r["statusCode"] == 200
    body = json.loads(r["body"])
    assert body["supported"] is True and body["zoom"] == 0.5
    assert body["presets"] == [{"token": "1", "name": "玄関"}]


def test_get_state_missing(shadow):
    r = handler.lambda_handler(event("GET", "/api/cameras/cam-002/ptz"), None)
    assert json.loads(r["body"]) == {"supported": False}


def test_get_state_no_shadow(ddb, monkeypatch):
    monkeypatch.setenv("IOT_DATA_ENDPOINT", "")
    boto3.client("iot", region_name="ap-northeast-1").create_thing(thingName="test-gw-001")
    r = handler.lambda_handler(event("GET", "/api/cameras/cam-001/ptz"), None)
    assert r["statusCode"] == 200 and json.loads(r["body"]) == {"supported": False}


def test_ptz_method_not_allowed(fake_iot):
    r = handler.lambda_handler(event("DELETE", "/api/cameras/cam-001/ptz"), None)
    assert r["statusCode"] == 405
