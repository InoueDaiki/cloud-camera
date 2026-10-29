"""KVS WebRTC の master の起動・監視とシャドウへの載せ方の単体テスト(本物の master と AWS は使わない)。"""

import asyncio
import json
import sys
import textwrap

from cc_edge import config, webrtc
from cc_edge.agent import Agent, diff_cameras
from cc_edge.onvif import Profile, StreamInfo
from cc_edge.pipeline import Backoff

CAM = {"onvif_url": "http://emulator:8080/onvif/device_service", "username": "admin",
       "password": "admin", "profile_token": None, "streaming": True}


def test_enabled_default_and_values():
    assert webrtc.webrtc_enabled({}) is True
    assert webrtc.webrtc_enabled({"webrtc": {}}) is True
    assert webrtc.webrtc_enabled({"webrtc": {"enabled": False}}) is False
    assert webrtc.webrtc_enabled({"webrtc": False}) is False


def test_webrtc_toggle_does_not_restart_recording():
    # webrtc.enabled だけの変更は録画のパイプラインの作り直し(restart)にならない
    off = {**CAM, "webrtc": {"enabled": False}}
    assert diff_cameras({"cam-001": CAM}, {"cam-001": off}) == {"start": [], "stop": [], "restart": [], "remove": [], "encoder": []}


def test_select_sub_profile():
    profs = [Profile("Profile_Main", encoding="H265", width=1280, height=720),
             Profile("Profile_Sub", encoding="H264", width=640, height=360),
             Profile("Profile_Jpeg", encoding="JPEG", width=640, height=360)]
    assert webrtc.select_sub_profile(profs, "Profile_Main").token == "Profile_Sub"
    # main を H.264 に切り替えたカメラでも、H.264 の視聴者には main 以外(sub)を選ぶ
    profs[0].encoding = "H264"
    assert webrtc.select_sub_profile(profs, "Profile_Main").token == "Profile_Sub"
    assert webrtc.select_sub_profile(profs[:1], "Profile_Main") is None


def _spec(**kw):
    base = dict(camera_id="cam-001", channel="cloud-camera-cam-001", main_rtsp_url="rtsp://h/cam1/main",
                sub_rtsp_url="rtsp://h/cam1/sub", iot_credential_endpoint="x.credentials.iot",
                cert_path="/certs/device.pem.crt", key_path="/certs/private.pem.key",
                role_alias="cloud-camera-kvs-role-alias", thing_name="cloud-camera-gw-001")
    base.update(kw)
    return webrtc.WebRtcSpec(**base)


def test_spec_codecs_and_env():
    s = _spec(username="admin", password="p\"w")
    assert s.codecs() == ["H265", "H264"]
    assert _spec(sub_rtsp_url=None).codecs() == ["H265"]
    assert _spec(main_codec="H264").codecs() == ["H264"]
    env = s.env({"LD_LIBRARY_PATH": "/opt/kvs/lib", "AWS_IOT_CORE_CERTIFICATE_ID": "x", "CC_SUB_RTSP_URL": "old"})
    # kvssink の libkvsCommonLws.so より先に WebRTC SDK のものを読む
    assert env["LD_LIBRARY_PATH"] == "/opt/kvs/webrtc/lib:/opt/kvs/lib"
    assert env["CC_WEBRTC_CHANNEL"] == "cloud-camera-cam-001"
    assert (env["CC_MAIN_CODEC"], env["CC_SUB_CODEC"]) == ("h265", "h264")
    assert env["CC_RTSP_PASS"] == "p\"w" and env["AWS_IOT_CORE_ROLE_ALIAS"] == "cloud-camera-kvs-role-alias"
    assert "AWS_IOT_CORE_CERTIFICATE_ID" not in env
    env = _spec(sub_rtsp_url=None).env({"CC_SUB_RTSP_URL": "old"})
    assert "CC_SUB_RTSP_URL" not in env
    assert s.same_media(_spec(username="admin", password="p\"w"))
    assert not s.same_media(_spec(main_codec="H264", username="admin", password="p\"w"))
    assert "p\"w" not in s.describe()


def test_config_channel_name_and_available(tmp_path):
    cfg = config.load({"CC_CONFIG_FILE": "/nonexistent", "CC_PROJECT": "cc-tmp-rtc"})
    assert cfg.channel_name("cam-001") == "cc-tmp-rtc-cam-001"
    assert not cfg.webrtc_available  # バイナリも証明書もない
    binary = tmp_path / "master"
    binary.write_text("")
    (tmp_path / "device.pem.crt").write_text("")
    (tmp_path / "private.pem.key").write_text("")
    cfg = config.load({"CC_CONFIG_FILE": "/nonexistent", "CC_WEBRTC_BIN": str(binary), "CC_CERTS_DIR": str(tmp_path),
                       "CC_IOT_CREDENTIAL_ENDPOINT": "x.credentials.iot", "CC_WEBRTC_CHANNEL_PREFIX": "other-"})
    assert cfg.webrtc_available and cfg.channel_name("cam-002") == "other-cam-002"
    cfg = config.load({"CC_CONFIG_FILE": "/nonexistent", "CC_WEBRTC_BIN": str(binary), "CC_CERTS_DIR": str(tmp_path),
                       "CC_IOT_CREDENTIAL_ENDPOINT": "x.credentials.iot", "CC_WEBRTC": "false"})
    assert not cfg.webrtc_available


STATS = {"cc_stats": True, "viewers": 3, "sent_kbps": 1500.0, "sent_bytes": 1000,
         "streams": {"main": {"codec": "H265", "available": True, "running": True, "viewers": 2, "in_kbps": 600.0,
                              "sent_kbps": 1200.0, "restarts": 0},
                     "sub": {"codec": "H264", "available": True, "running": True, "viewers": 1, "in_kbps": 300.0,
                             "sent_kbps": 300.0, "restarts": 1}}}


def test_supervisor_events_and_stats():
    changes = []
    sup = webrtc.MasterSupervisor("cam-001", None, lambda: changes.append(1), Backoff(1, 2))
    sup._handle({"cc_event": "ready", "channel": "c"})
    assert sup.state == webrtc.STATE_CONNECTED
    sup._handle(STATS)
    assert sup.viewers == 3 and sup.viewers_by_codec() == {"H265": 2, "H264": 1}
    st = sup.status()
    assert st["sent_kbps"] == 1500.0 and st["streams"]["sub"]["restarts"] == 1 and st["sent_bytes"] == 1000
    # 回線断でシグナリングが切れたら RECONNECTING、つながり直したら CONNECTED
    sup._handle({"cc_event": "signaling", "state": "Disconnected"})
    assert sup.state == webrtc.STATE_RECONNECTING
    sup._handle({"cc_event": "signaling", "state": "Connected"})
    assert sup.state == webrtc.STATE_CONNECTED
    assert len(changes) >= 4
    # 統計が古くなったら視聴者 0 とみなす
    sup._stats_at -= 100
    assert sup.viewers == 0
    # 子プロセスが入れ替わっても送信量は累計する
    sup._clear_stats()
    sup._handle({**STATS, "sent_bytes": 5})
    assert sup.status()["sent_bytes"] == 1005


FAKE_MASTER = textwrap.dedent("""\
    import json, os, sys, time
    print("2026-10-24 SDK log line", flush=True)
    print(json.dumps({"cc_event": "ready", "channel": os.environ["CC_WEBRTC_CHANNEL"]}), flush=True)
    print(json.dumps({"cc_stats": True, "viewers": 1, "sent_kbps": 10.0, "sent_bytes": 100,
                      "streams": {"sub": {"codec": "H264", "available": True, "viewers": 1}}}), flush=True)
    time.sleep(float(os.environ.get("FAKE_SLEEP", "0.3")))
    sys.exit(3)
""")


def test_supervisor_runs_and_restarts_with_backoff(tmp_path, monkeypatch):
    script = tmp_path / "fake_master.py"
    script.write_text(FAKE_MASTER)
    binary = tmp_path / "master"
    binary.write_text(f"#!/bin/sh\nexec {sys.executable} {script}\n")
    binary.chmod(0o755)

    async def run():
        async def factory(attempt):
            return _spec(binary=str(binary))

        sup = webrtc.MasterSupervisor("cam-001", factory, lambda: None, Backoff(0.1, 0.2))
        sup.start()
        for _ in range(100):
            await asyncio.sleep(0.05)
            if sup.restarts >= 2:
                break
        await sup.stop()
        return sup

    sup = asyncio.run(run())
    assert sup.restarts >= 2
    assert sup.last_exit["code"] == 3
    assert sup.status()["sent_bytes"] >= 200  # 2 回ぶんの送信量を累計
    assert not sup.running


def test_supervisor_waits_for_spec():
    async def run():
        calls = []

        async def factory(attempt):
            calls.append(attempt)
            return None  # 録画側の ONVIF の解決待ち

        sup = webrtc.MasterSupervisor("cam-001", factory, lambda: None, Backoff(0.01, 0.02))
        sup.start()
        await asyncio.sleep(0.2)
        await sup.stop()
        return calls, sup

    calls, sup = asyncio.run(run())
    assert len(calls) >= 2 and sup.restarts == 0


def test_reported_and_status_shape(tmp_path):
    cfg = config.load({"CC_CONFIG_FILE": "/nonexistent", "CC_USE_SHADOW": "false", "CC_SINK": "fake"})

    async def run():
        agent = Agent(cfg)
        await agent.apply({"cam-001": {"rtsp_url": "rtsp://h/cam1/main", "codec": "H265",
                                       "sub_rtsp_url": "rtsp://h/cam1/sub", "streaming": False}})
        rep = agent.reported_state()["cameras"]["cam-001"]["webrtc"]
        st = agent.status_payload()["cameras"]["cam-001"]["webrtc"]
        w = agent.workers["cam-001"]
        spec = await w._webrtc_spec(0)
        await agent.apply({"cam-001": {"rtsp_url": "rtsp://h/cam1/main", "streaming": True}})
        rep2 = agent.reported_state()["cameras"]["cam-001"]["webrtc"]
        await asyncio.gather(*(w.stop() for w in agent.workers.values()))
        return rep, st, spec, rep2

    rep, st, spec, rep2 = asyncio.run(run())
    assert rep == {"enabled": True, "channel": "cloud-camera-cam-001", "state": "DISABLED", "codecs": [],
                   "viewers": 0, "error": None}
    assert st["state"] == "DISABLED" and st["viewers"] == 0 and st["viewers_by_codec"] == {"H265": 0, "H264": 0}
    assert (spec.main_rtsp_url, spec.sub_rtsp_url, spec.codecs()) == ("rtsp://h/cam1/main", "rtsp://h/cam1/sub",
                                                                      ["H265", "H264"])
    # 配信中でも master のバイナリや認証情報がなければ UNAVAILABLE(ドライラン)
    assert rep2["state"] == "UNAVAILABLE"
    json.dumps(rep2)


def test_sub_url_from_onvif_profiles(monkeypatch):
    cfg = config.load({"CC_CONFIG_FILE": "/nonexistent"})
    agent = Agent(cfg)
    from cc_edge.agent import CameraWorker
    w = CameraWorker(agent, "cam-001", CAM)
    w.info = StreamInfo(rtsp_url="rtsp://emulator:8554/cam1/main", profile_token="Profile_Main", codec="H265",
                        width=1280, height=720, media_version=2,
                        profiles=[Profile("Profile_Main", encoding="H265"), Profile("Profile_Sub", encoding="H264")])
    calls = []

    def fake_services(self):
        self.services = {"x": "y"}
        return self.services

    def fake_uri(self, version, token):
        calls.append((version, token))
        return "rtsp://emulator:8554/cam1/sub"

    monkeypatch.setattr("cc_edge.onvif.OnvifClient.get_services", fake_services)
    monkeypatch.setattr("cc_edge.onvif.OnvifClient.get_stream_uri", fake_uri)
    spec = asyncio.run(w._webrtc_spec(0))
    spec2 = asyncio.run(w._webrtc_spec(1))
    assert (spec.main_rtsp_url, spec.sub_rtsp_url, spec.sub_codec) == (
        "rtsp://emulator:8554/cam1/main", "rtsp://emulator:8554/cam1/sub", "H264")
    assert spec2.sub_rtsp_url == spec.sub_rtsp_url and calls == [(2, "Profile_Sub")]  # 2 回目はキャッシュ
    assert spec.username == "admin" and spec.channel == "cloud-camera-cam-001"
