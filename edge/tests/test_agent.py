"""シャドウの差分適用、設定、パイプライン仕様の単体テスト(GStreamer と AWS は使わない)。"""

import json

from cc_edge import config
from cc_edge.agent import diff_cameras
from cc_edge.pipeline import Backoff, PipelineSpec

CAM = {"onvif_url": "http://emulator:8080/onvif/device_service", "username": "admin",
       "password": "admin", "profile_token": None, "streaming": True}


def test_diff_new_and_removed():
    plan = diff_cameras({"cam-002": CAM}, {"cam-001": CAM})
    assert plan == {"start": ["cam-001"], "stop": [], "restart": [], "remove": ["cam-002"]}


def test_diff_streaming_toggle():
    off = {**CAM, "streaming": False}
    assert diff_cameras({"cam-001": CAM}, {"cam-001": off})["stop"] == ["cam-001"]
    assert diff_cameras({"cam-001": off}, {"cam-001": CAM})["start"] == ["cam-001"]
    # 新規で streaming=false なら起動しない
    assert diff_cameras({}, {"cam-001": off}) == {"start": [], "stop": [], "restart": [], "remove": []}


def test_diff_restart_on_config_change():
    changed = {**CAM, "profile_token": "sub"}
    assert diff_cameras({"cam-001": CAM}, {"cam-001": changed})["restart"] == ["cam-001"]
    assert diff_cameras({"cam-001": CAM}, {"cam-001": dict(CAM)}) == {
        "start": [], "stop": [], "restart": [], "remove": []}


def test_config_env_overrides_file(tmp_path):
    f = tmp_path / "config.json"
    f.write_text(json.dumps({"gateway_id": "gw-009", "iot_endpoint": "x-ats.iot", "cameras": {"cam-001": CAM}}))
    cfg = config.load({"CC_CONFIG_FILE": str(f), "CC_GATEWAY_ID": "gw-002", "CC_DRY_RUN": "1",
                       "CC_STATUS_INTERVAL": "5"})
    assert cfg.gateway_id == "gw-002"
    assert cfg.iot_endpoint == "x-ats.iot"
    assert cfg.thing == "cloud-camera-gw-002"
    assert cfg.topic_prefix == "cloud-camera/gw-002"
    assert cfg.sink == "fake"
    assert cfg.status_interval_s == 5.0
    assert cfg.cameras == {"cam-001": CAM}
    assert cfg.stream_name("cam-001") == "cloud-camera-cam-001"
    assert cfg.role_alias == "cloud-camera-kvs-role-alias"


def test_spec_iot_structure_and_describe():
    s = PipelineSpec(camera_id="cam-001", rtsp_url="rtsp://h/cam1/main", username="admin", password="s3cret",
                     stream_name="cloud-camera-cam-001", iot_credential_endpoint="abc.credentials.iot",
                     cert_path="/certs/device.pem.crt", key_path="/certs/private.pem.key",
                     ca_path="/certs/AmazonRootCA1.pem", role_alias="cloud-camera-kvs-role-alias",
                     thing_name="cloud-camera-gw-001")
    st = s.iot_certificate_structure()
    assert st.startswith("iot-certificate,endpoint=\"abc.credentials.iot\"")
    assert 'role-aliases="cloud-camera-kvs-role-alias"' in st
    assert 'iot-thing-name="cloud-camera-gw-001"' in st
    d = s.describe()
    assert "rtph265depay ! h265parse" in d and "stream-format=hvc1" in d and "s3cret" not in d
    assert PipelineSpec.from_json(s.to_json()) == s
    assert s.protocols_flag() == 4


def test_backoff():
    b = Backoff(2, 10)
    assert [b.next_delay() for _ in range(5)] == [2, 4, 8, 10, 10]
    b.reset()
    assert b.next_delay() == 2
