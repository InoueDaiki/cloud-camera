"""ネットワークを使わない単体テスト(設定、認証、FFmpeg コマンド)。"""

from __future__ import annotations

import base64
import hashlib
import xml.etree.ElementTree as ET

import pytest

from onvif_emulator.config import load_config
from onvif_emulator.soap import PASSWORD_DIGEST, PASSWORD_TEXT, verify_username_token
from onvif_emulator.streams import ffmpeg_command, mediamtx_config


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
    readers = [u for u in users if any(p["action"] == "read" for p in u["permissions"])]
    assert all(u["user"] == "admin" for u in readers)
