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
    # コーデックの優先順位を変えたらパイプラインを作り直す
    prefer = {**CAM, "prefer_codec": "H264"}
    assert diff_cameras({"cam-001": CAM}, {"cam-001": prefer})["restart"] == ["cam-001"]
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


def test_buffer_settings_defaults_and_env():
    cfg = config.load({"CC_CONFIG_FILE": "/nonexistent"})
    assert (cfg.kvs_buffer_s, cfg.kvs_storage_size_mb) == (660, 160)  # 10 分 + 余裕 1 分(1.5Mbps で約 124MB)
    cfg = config.load({"CC_CONFIG_FILE": "/nonexistent", "CC_KVS_BUFFER_S": "300", "CC_KVS_STORAGE_SIZE_MB": "64",
                       "CC_KVS_REPLAY_S": "90", "CC_KVS_CONNECTION_STALENESS_S": "20", "CC_KVS_FRAMERATE": "60"})
    assert (cfg.kvs_buffer_s, cfg.kvs_storage_size_mb, cfg.kvs_replay_s) == (300, 64, 90)
    assert (cfg.kvs_connection_staleness_s, cfg.kvs_framerate) == (20, 60)


def test_spec_buffer_properties():
    s = PipelineSpec(camera_id="cam-001", rtsp_url="rtsp://h/cam1/main", stream_name="x")
    # 枠(フレーム数)= framerate × buffer-duration。fps が分からなければ 30 で見積もる
    assert s.kvs_framerate() == 30
    assert PipelineSpec(camera_id="c", rtsp_url="r", framerate=60).kvs_framerate() == 60
    # max-latency はバッファより長くして使わない(追いつき中の張り直しを避ける)
    assert s.max_latency_s() > s.buffer_duration_s
    d = s.describe()
    assert "buffer-duration=660" in d and "storage-size=160" in d and "replay-duration=660" in d
    # 巻き戻しの既定はバッファと同じ長さ(短いと回線断の始めのぶんが送り直されない)
    assert s.replay_s() == 660 and PipelineSpec(camera_id="c", rtsp_url="r", replay_duration_s=90).replay_s() == 90


def _sup():
    from cc_edge.pipeline import PipelineSupervisor
    events = []
    sup = PipelineSupervisor("cam-001", lambda a: None, lambda k, d: events.append(k), Backoff(1, 2))
    return sup, events


def test_supervisor_kvs_error_does_not_stop_streaming():
    sup, _ = _sup()
    sup._handle({"type": "stats", "bytes": 10, "uptime_s": 5, "last_buffer_age_s": 0.1,
                 "buffer": {"unsent_s": 0.0, "persisted_lag_s": 1.0, "dropped_frames": 0}})
    assert sup.running
    # 回線断(KVS への送信エラー)は数えるだけで、配信中のまま・エラー扱いにしない
    sup._handle({"type": "kvs_error", "code": "0x52000016"})
    sup._handle({"type": "stats", "bytes": 20, "uptime_s": 10, "last_buffer_age_s": 0.1,
                 "buffer": {"unsent_s": 120.0, "persisted_lag_s": 121.0, "dropped_frames": 0}})
    assert sup.running and sup.last_error is None
    b = sup.buffer_status()
    assert b["unsent_s"] == 120.0 and b["kvs_errors"] == 1 and b["dropped_frames"] == 0


def test_supervisor_buffer_overflow_counts_and_final_stats_kept():
    sup, _ = _sup()
    sup._handle({"type": "stats", "bytes": 1, "uptime_s": 1, "last_buffer_age_s": 0.1,
                 "buffer": {"unsent_s": 600.0, "dropped_frames": 0}})
    sup._handle({"type": "buffer_overflow", "dropped_frames": 15, "new": 15, "unsent_s": 600.0})
    assert sup.dropped_frames == 15
    # 子プロセスの最後の stats(final)で計測値が消えない
    sup._handle({"type": "stats", "final": True, "bytes": 2})
    assert sup.buffer_status()["unsent_s"] == 600.0 and sup.bytes_sent == 2
    # 上限を超えて失った秒数(lost_s)は子プロセスの再起動をまたいで足す
    sup._handle({"type": "stats", "bytes": 3, "last_buffer_age_s": 0.1, "buffer": {"lost_s": 30.0}})
    assert sup.buffer_status()["lost_s"] == 30.0
    # fake / file のときは buffer なし
    sup2, _ = _sup()
    sup2._handle({"type": "stats", "bytes": 1, "last_buffer_age_s": 0.1, "buffer": None})
    assert sup2.buffer_status() is None
