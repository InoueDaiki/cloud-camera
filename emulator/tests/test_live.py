"""ライブの入力(live: / stream:)の結合テスト。docker compose の tests サービスから実行する。

ブラウザのカメラ(WHIP)からの取り込みそのものは、ブラウザが要るので tools/emulator-webcam-check で確かめる。
ここでは、映像の来ていない live: が代替映像で配信を続けることと、stream: で別の RTSP を中継できることを見る。
"""

from __future__ import annotations

import json
import time
import urllib.request
import uuid

import pytest
from conftest import Target
from onvif_helpers import wait_until
from test_media import probe_stream, wait_stream

pytestmark = [pytest.mark.media, pytest.mark.usefixtures("need_media")]

CAM = 2


def _state(target: Target) -> dict:
    with urllib.request.urlopen(f"http://{target.host}:{target.ui_port}/api/state", timeout=10) as r:
        st = json.load(r)
    return next(c for c in st["cameras"] if c["index"] == CAM)


def _set_source(target: Target, source: str) -> None:
    req = urllib.request.Request(
        f"http://{target.host}:{target.ui_port}/api/cameras/{CAM}/source", method="PUT",
        data=json.dumps({"source": source}).encode(), headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        assert r.status == 200


def _wait_running(target: Target, active: str, timeout: float = 60) -> dict:
    """FFmpeg が active のソースで起動し、フレームが進むまで待つ。"""
    def ok():
        c = _state(target)
        if c["active_source"] != active or not c["ffmpeg_running"]:
            return None
        frame = int(c["frame"] or 0)
        return c if frame > 0 else None

    c = wait_until(ok, timeout, 1)
    assert c, f"{active} で配信が始まりません: {_state(target)}"
    return c


@pytest.fixture
def restore_source(target: Target, need_media):
    before = _state(target)["source"]
    yield
    _set_source(target, before)
    _wait_running(target, before)


@pytest.mark.usefixtures("restore_source")
def test_live_without_publisher_falls_back(target: Target):
    name = f"itest-{uuid.uuid4().hex[:8]}"
    _set_source(target, f"live:{name}")
    c = _wait_running(target, "smptebars")
    assert c["source"] == f"live:{name}"
    assert c["live"] == {"path": f"live/{name}", "ready": False}
    # 代替映像でも main / sub の解像度とコーデックは変わらない
    url = f"rtsp://{target.host}:{target.rtsp_port}/cam{CAM}"
    main = wait_stream(f"{url}/main")
    assert (main["width"], main["height"]) == (c["main"]["width"], c["main"]["height"])
    assert main["codec_name"] == {"H265": "hevc", "H264": "h264"}[c["main"]["codec"]]
    assert probe_stream(f"{url}/sub")["codec_name"] == "h264"
    # 映像が来ないまま再起動を繰り返さない
    restarts = c["ffmpeg_restarts"]
    time.sleep(5)
    c = _state(target)
    assert c["ffmpeg_running"] and c["ffmpeg_restarts"] == restarts and c["active_source"] == "smptebars"


@pytest.mark.usefixtures("restore_source")
def test_stream_relays_rtsp(target: Target):
    # 1 台目の sub をエミュレーターの中から読み、2 台目として出し直す
    source = f"stream:rtsp://127.0.0.1:{target.rtsp_port}/cam1/sub"
    _set_source(target, source)
    c = _wait_running(target, source)
    main = wait_stream(f"rtsp://{target.host}:{target.rtsp_port}/cam{CAM}/main")
    assert (main["width"], main["height"]) == (c["main"]["width"], c["main"]["height"])
    # フレームが進み続ける(入力が途切れて再起動していない)
    f0, r0 = int(c["frame"]), c["ffmpeg_restarts"]
    time.sleep(6)
    c = _state(target)
    assert int(c["frame"]) > f0 and c["ffmpeg_restarts"] == r0
