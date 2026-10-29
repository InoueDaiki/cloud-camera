"""ネットワークを使わない単体テスト(設定、認証、FFmpeg コマンド)。"""

from __future__ import annotations

import base64
import hashlib
import xml.etree.ElementTree as ET

import pytest

from onvif_emulator.config import load_config, validate_source
from onvif_emulator.soap import PASSWORD_DIGEST, PASSWORD_TEXT, verify_username_token
from onvif_emulator.streams import ffmpeg_command, mediamtx_config, write_overlay_text


def test_config_env_overrides(tmp_path):
    y = tmp_path / "c.yaml"
    y.write_text(
        "defaults: {fps: 10, bitrate_kbps: 500}\n"
        "count: 3\n"
        "cameras:\n  - {name: front}\n  - {name: back, source: 'file:/media/a.mp4'}\n"
    )
    cfg = load_config(y, env={"EMULATOR_KEYFRAME_INTERVAL_S": "2", "EMULATOR_CAM3_FPS": "20", "EMULATOR_ONVIF_PORT": "9000"})
    assert [c.name for c in cfg.cameras] == ["front", "back", "cam-003"]
    assert [c.onvif_port for c in cfg.cameras] == [9000, 9001, 9002]
    assert cfg.cameras[0].main.gov_length == 20  # 10fps × 2 秒
    assert cfg.cameras[2].main.fps == 20 and cfg.cameras[2].main.gov_length == 40
    assert cfg.cameras[1].source == "file:/media/a.mp4"
    assert cfg.cameras[0].uuid != cfg.cameras[1].uuid


def test_config_rejects_unknown_source():
    with pytest.raises(ValueError):
        load_config(env={"EMULATOR_SOURCE": "nope"})


def _token(user: str, pw_value: str, pw_type: str, nonce: bytes | None, created: str) -> ET.Element:
    ns_wsse = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
    ns_wsu = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
    nonce_xml = f"<wsse:Nonce>{base64.b64encode(nonce).decode()}</wsse:Nonce>" if nonce else ""
    return ET.fromstring(
        f'<wsse:Security xmlns:wsse="{ns_wsse}" xmlns:wsu="{ns_wsu}"><wsse:UsernameToken>'
        f"<wsse:Username>{user}</wsse:Username>"
        f'<wsse:Password Type="{pw_type}">{pw_value}</wsse:Password>{nonce_xml}'
        f"<wsu:Created>{created}</wsu:Created></wsse:UsernameToken></wsse:Security>"
    )


def test_password_digest():
    nonce, created = b"0123456789abcdef", "2026-10-24T10:00:00Z"
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + b"secret").digest()).decode()
    assert verify_username_token(_token("admin", digest, PASSWORD_DIGEST, nonce, created), "admin", "secret")
    assert not verify_username_token(_token("admin", digest, PASSWORD_DIGEST, nonce, created), "admin", "other")
    assert not verify_username_token(_token("root", digest, PASSWORD_DIGEST, nonce, created), "admin", "secret")
    # Created のずれを検査する設定なら古いトークンは拒否
    assert not verify_username_token(
        _token("admin", digest, PASSWORD_DIGEST, nonce, created), "admin", "secret", max_skew_s=60
    )


def test_password_text():
    assert verify_username_token(_token("admin", "secret", PASSWORD_TEXT, None, ""), "admin", "secret")
    assert not verify_username_token(_token("admin", "nope", PASSWORD_TEXT, None, ""), "admin", "secret")
    assert not verify_username_token(None, "admin", "secret")


def test_ffmpeg_command_gop_and_codecs(tmp_path):
    cfg = load_config(env={"EMULATOR_CAMERAS": "1", "EMULATOR_FPS": "25", "EMULATOR_RUNTIME_DIR": str(tmp_path)})
    cmd = ffmpeg_command(cfg, cfg.cameras[0])
    x265 = cmd[cmd.index("-x265-params") + 1]
    assert "keyint=25" in x265 and "min-keyint=25" in x265 and "scenecut=0" in x265
    assert "vbv-maxrate=1024" in x265
    assert "libx265" in cmd and "libx264" in cmd
    assert "rtsp://127.0.0.1:8554/cam1/main" in cmd and "rtsp://127.0.0.1:8554/cam1/sub" in cmd
    file_cfg = load_config(env={"EMULATOR_CAMERAS": "1", "EMULATOR_SOURCE": "file:/media/x.mp4"})
    fcmd = ffmpeg_command(file_cfg, file_cfg.cameras[0])
    assert fcmd[fcmd.index("-stream_loop") + 1] == "-1"


def test_mediamtx_auth():
    cfg = load_config(env={"EMULATOR_RTSP_AUTH": "true"})
    users = mediamtx_config(cfg)["authInternalUsers"]
    # コンテナ内(127.0.0.1)は live: を読むために読み出しもできる。外からはカメラの認証情報が必要
    readers = [u for u in users if any(p["action"] == "read" for p in u["permissions"]) and not u["ips"]]
    assert readers and all(u["user"] == "admin" for u in readers)
    local = [u for u in users if u["ips"]]
    assert local and all(set(u["ips"]) <= {"127.0.0.1", "::1"} for u in local)


@pytest.mark.parametrize(
    "source",
    ["testsrc", "file:/media/a.mp4", "live:webcam", "live:cam_1-a", "stream:rtsp://10.0.0.5:554/stream1",
     "stream:srt://host:9000?mode=caller"],
)
def test_validate_source_accepts(source):
    assert validate_source(source) == source


@pytest.mark.parametrize("source", ["", "file:", "live:", "live:a/b", "live:日本", "stream:/dev/video0", "nope"])
def test_validate_source_rejects(source):
    with pytest.raises(ValueError):
        validate_source(source)


def test_ffmpeg_command_live_and_stream(tmp_path):
    cfg = load_config(env={"EMULATOR_CAMERAS": "1", "EMULATOR_SOURCE": "live:webcam", "EMULATOR_RUNTIME_DIR": str(tmp_path)})
    cam = cfg.cameras[0]
    cmd = ffmpeg_command(cfg, cam)
    # ライブの入力は実時間待ち(-re)もループもしない。送り手が止まったら -timeout で終わる
    assert cmd[cmd.index("-i") + 1] == "rtsp://127.0.0.1:8554/live/webcam"
    assert "-re" not in cmd and "-stream_loop" not in cmd and "-timeout" in cmd
    assert cmd[cmd.index("-filter_complex") + 1].startswith(f"[0:v]fps={cam.main.fps},")
    # 映像が来ていない間は代替映像(テストパターン)を流す
    fb = ffmpeg_command(cfg, cam, source=cfg.live_fallback)
    assert fb[fb.index("-i") + 1].startswith("smptebars=") and "-re" in fb
    st = ffmpeg_command(cfg, cam, source="stream:srt://10.0.0.5:9000")
    inp = st[: st.index("-i") + 2]
    assert inp[-1] == "srt://10.0.0.5:9000" and "-rw_timeout" in inp and "-rtsp_transport" not in inp


def test_mediamtx_webrtc(tmp_path):
    cfg = load_config(env={"EMULATOR_WEBRTC_HOSTS": "127.0.0.1, 192.168.1.50", "EMULATOR_ADVERTISE_HOST": "cam.local",
                           "EMULATOR_WEBRTC_ICE_PORT": "8200"})
    c = mediamtx_config(cfg)
    assert c["webrtc"] is True
    assert c["webrtcAddress"] == "127.0.0.1:8889"  # WHIP の HTTP は UI が中継する
    assert c["webrtcLocalUDPAddress"] == ":8200" and c["webrtcLocalTCPAddress"] == ":8200"
    assert c["webrtcAdditionalHosts"] == ["127.0.0.1", "192.168.1.50", "cam.local"]


def test_live_fallback_must_be_pattern():
    with pytest.raises(ValueError):
        load_config(env={"EMULATOR_LIVE_FALLBACK": "file:/media/a.mp4"})


def test_overlay_note(tmp_path):
    cfg = load_config(env={"EMULATOR_CAMERAS": "1", "EMULATOR_RUNTIME_DIR": str(tmp_path)})
    text = write_overlay_text(cfg, cfg.cameras[0], "PTZ pan +0.000", "NO SIGNAL 100%").read_text()
    lines = text.splitlines()
    assert lines[3] == "PTZ pan +0.000" and lines[4] == "NO SIGNAL 100%%"
