"""RTSP 配信とスナップショットの検証(ffprobe)。docker compose の tests サービスから実行する。"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import time
import urllib.request

import pytest
from conftest import Target
from onvif_helpers import camera, media2, wait_until

pytestmark = [pytest.mark.media, pytest.mark.usefixtures("need_media")]


def _url(target: Target, n: int, which: str) -> str:
    return f"rtsp://{target.host}:{target.rtsp_port}/cam{n}/{which}"


def probe_stream(url: str) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name,profile,width,height,avg_frame_rate,r_frame_rate", "-of", "json", url],
        capture_output=True, text=True, timeout=30, check=True,
    )
    return json.loads(out.stdout)["streams"][0]


def probe_packets(url: str, seconds: float) -> list[tuple[float, bool]]:
    """(pts 秒, キーフレームか) の一覧。"""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-select_streams", "v:0",
         "-read_intervals", f"%+{seconds}", "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", url],
        capture_output=True, text=True, timeout=seconds + 30, check=True,
    )
    pkts = []
    for line in out.stdout.splitlines():
        parts = line.strip().split(",")
        if len(parts) >= 2 and parts[0] not in ("", "N/A"):
            pkts.append((float(parts[0]), "K" in parts[1]))
    return pkts


def keyframe_intervals(pkts: list[tuple[float, bool]]) -> list[float]:
    keys = [t for t, k in pkts if k]
    return [b - a for a, b in zip(keys, keys[1:], strict=False)]


def wait_stream(url: str, timeout: float = 60) -> dict:
    def ok():
        try:
            return probe_stream(url)
        except subprocess.SubprocessError:
            return None

    s = wait_until(ok, timeout, 1)
    assert s, f"{url} に接続できません"
    return s


def test_main_is_hevc_with_fixed_resolution(target: Target):
    for n in range(1, target.cameras + 1):
        s = wait_stream(_url(target, n, "main"))
        assert s["codec_name"] == "hevc", s
        cam = camera(target.host, target.device_port(n), target.username, target.password)
        conf = next(c for c in media2(cam, target.username, target.password).GetVideoEncoderConfigurations({})
                    if c.Encoding == "H265")
        assert (s["width"], s["height"]) == (conf.Resolution.Width, conf.Resolution.Height)


def test_sub_is_h264_low_resolution(target: Target):
    main = wait_stream(_url(target, 1, "main"))
    sub = wait_stream(_url(target, 1, "sub"))
    assert sub["codec_name"] == "h264"
    assert sub["width"] < main["width"] and sub["height"] < main["height"]


def test_main_keyframe_interval_and_monotonic_pts(target: Target):
    cam = camera(target.host, target.onvif_port, target.username, target.password)
    conf = next(c for c in media2(cam, target.username, target.password).GetVideoEncoderConfigurations({})
                if c.Encoding == "H265")
    expected = conf.GovLength / conf.RateControl.FrameRateLimit
    pkts = probe_packets(_url(target, 1, "main"), 6)
    iv = keyframe_intervals(pkts)
    assert len(iv) >= 3, pkts[:20]
    assert all(abs(x - expected) < 0.08 for x in iv), (expected, iv)
    pts = [t for t, _ in pkts]
    assert all(b > a for a, b in zip(pts, pts[1:], strict=False)), "pts が単調増加していない"


def test_snapshot_jpeg(target: Target):
    cam = camera(target.host, target.onvif_port, target.username, target.password)
    uri = media2(cam, target.username, target.password).GetSnapshotUri({"ProfileToken": "Profile_Main"})

    def fetch():
        try:
            with urllib.request.urlopen(uri, timeout=5) as r:
                return r.headers.get("Content-Type"), r.read()
        except OSError:
            return None

    got = wait_until(fetch, 30, 1)
    assert got, uri
    ctype, data = got
    assert ctype == "image/jpeg" and data[:3] == b"\xff\xd8\xff" and len(data) > 1000


def test_set_video_encoder_configuration_restarts_stream(target: Target):
    """Media2 で GOP とビットレートを変えると配信が再起動して反映される(最後のカメラで試し、元に戻す)。"""
    n = target.cameras
    cam = camera(target.host, target.device_port(n), target.username, target.password)
    m2 = media2(cam, target.username, target.password)
    orig = next(c for c in m2.GetVideoEncoderConfigurations({}) if c.Encoding == "H265")
    fps = orig.RateControl.FrameRateLimit
    orig_gov, orig_bitrate = orig.GovLength, orig.RateControl.BitrateLimit
    url = _url(target, n, "main")
    try:
        conf = next(c for c in m2.GetVideoEncoderConfigurations({}) if c.Encoding == "H265")
        conf.GovLength = int(fps * 2)
        conf.RateControl.BitrateLimit = 768
        m2.SetVideoEncoderConfiguration({"Configuration": conf})
        after = next(c for c in m2.GetVideoEncoderConfigurations({}) if c.Encoding == "H265")
        assert after.GovLength == int(fps * 2) and after.RateControl.BitrateLimit == 768
        time.sleep(2)
        wait_stream(url)

        def two_seconds():
            try:
                iv = keyframe_intervals(probe_packets(url, 7))
            except subprocess.SubprocessError:  # 再起動中
                return None
            return iv if iv and all(abs(x - 2.0) < 0.1 for x in iv) else None

        assert wait_until(two_seconds, 40, 1), "キーフレーム間隔 2 秒が反映されない"
    finally:
        conf = next(c for c in m2.GetVideoEncoderConfigurations({}) if c.Encoding == "H265")
        conf.GovLength = orig_gov
        conf.RateControl.BitrateLimit = orig_bitrate
        m2.SetVideoEncoderConfiguration({"Configuration": conf})
    time.sleep(2)
    wait_stream(url)


@pytest.mark.skipif(not os.environ.get("EMULATOR_LOOP_CAMERA"), reason="EMULATOR_LOOP_CAMERA(動画ファイルのカメラ番号)が未指定")
def test_file_loop_is_continuous(target: Target):
    """動画ファイルのループをまたいでも、pts が単調増加し、途切れず、キーフレーム間隔が保たれる。

    EMULATOR_LOOP_SECONDS にファイルの長さ + 余裕を指定する(既定 70 秒。サンプルは 57 秒と 24 秒)。
    """
    n = int(os.environ["EMULATOR_LOOP_CAMERA"])
    seconds = float(os.environ.get("EMULATOR_LOOP_SECONDS", "70"))
    url = _url(target, n, "main")
    wait_stream(url)
    started = time.monotonic()
    pkts = probe_packets(url, seconds)
    wall = time.monotonic() - started
    pts = [t for t, _ in pkts]
    span = pts[-1] - pts[0]
    assert span >= seconds - 2, (span, seconds)
    assert all(b > a for a, b in zip(pts, pts[1:], strict=False)), "pts が単調増加していない"
    gaps = [b - a for a, b in zip(pts, pts[1:], strict=False)]
    assert max(gaps) < 0.5, f"配信が途切れた(最大の間隔 {max(gaps):.3f} 秒)"
    # 実時間で流れている(バースト送信や停止がない)
    assert abs(span - wall) < 5, (span, wall)
    iv = keyframe_intervals(pkts)
    assert statistics.median(iv) == pytest.approx(1.0, abs=0.08)
    assert all(abs(x - 1.0) < 0.15 for x in iv), iv


def test_processes_healthy(target: Target):
    """全カメラの FFmpeg が動いていて、異常終了による再起動が起きていない。"""
    with urllib.request.urlopen(f"http://{target.host}:{target.onvif_port}/healthz", timeout=5) as r:
        health = json.load(r)
    assert health["mediamtx_running"]
    for name, c in health["cameras"].items():
        assert c["ffmpeg_running"], name
        assert c["ffmpeg_restarts"] == 0, (name, c)
        assert float(c["speed"].rstrip("x")) > 0.9, (name, c)
