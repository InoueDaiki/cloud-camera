"""疑似 PTZ の映像の検証(RTSP を ffmpeg / ffprobe で読む)。docker compose の tests サービスから実行する。

- PTZ の操作で映像が変わる(main / sub / スナップショットが同じ窓)
- 操作中も配信が途切れない(pts が単調増加、キーフレーム間隔が保たれる、解像度・コーデックが変わらない、FFmpeg が再起動しない)
- 操作から映像に反映されるまでの遅延

PTZ_SHOTS_DIR を指定すると、操作前後のフレームと計測結果(ptz_latency.json)をそこに書き出す。
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import threading
import time
import urllib.request
from datetime import timedelta
from pathlib import Path

import pytest
from conftest import Target
from onvif_helpers import camera, ptz_reset, ptz_wait_idle
from test_media import keyframe_intervals, probe_packets, probe_stream, wait_stream

pytestmark = [pytest.mark.media, pytest.mark.usefixtures("need_media")]

PROFILE = "Profile_Main"
SMALL = (160, 90)  # 比較用に縮小したグレースケール
SHOTS = Path(os.environ["PTZ_SHOTS_DIR"]) if os.environ.get("PTZ_SHOTS_DIR") else None


def _url(target: Target, which: str, n: int = 1) -> str:
    return f"rtsp://{target.host}:{target.rtsp_port}/cam{n}/{which}"


@pytest.fixture(scope="module")
def ptz(target: Target):
    cam = camera(target.host, target.onvif_port, target.username, target.password)
    svc = cam.create_ptz_service()
    ptz_reset(svc)
    yield svc
    ptz_reset(svc)


def grab(url_or_file: str, save: str | None = None) -> bytes:
    """1 フレームを縮小グレースケール(160×90)で取る。save を指定すると元の解像度の PNG も書く。

    RTSP はキーフレームから読む(途中の P フレームから読むと参照がなく灰色の絵になる)。
    """
    rtsp = ["-rtsp_transport", "tcp", "-skip_frame", "nokey"] if url_or_file.startswith("rtsp://") else []
    args = ["ffmpeg", "-v", "error", "-y", *rtsp, "-i", url_or_file, "-frames:v", "1",
            "-vf", f"scale={SMALL[0]}:{SMALL[1]},format=gray", "-f", "rawvideo", "pipe:1"]
    if save and SHOTS:
        SHOTS.mkdir(parents=True, exist_ok=True)
        args += ["-frames:v", "1", "-update", "1", str(SHOTS / save)]
    out = subprocess.run(args, capture_output=True, timeout=30, check=True)
    assert len(out.stdout) == SMALL[0] * SMALL[1], len(out.stdout)
    return out.stdout


def diff(a: bytes, b: bytes) -> float:
    """平均絶対差。上 30% は焼き込み(時刻・フレーム番号・PTZ の値)があるので除く。"""
    start = SMALL[0] * int(SMALL[1] * 0.3)
    return sum(abs(x - y) for x, y in zip(a[start:], b[start:], strict=True)) / (len(a) - start)


def health(target: Target) -> dict:
    with urllib.request.urlopen(f"http://{target.host}:{target.onvif_port}/healthz", timeout=5) as r:
        return json.load(r)["cameras"]["cam-001"]


def test_ptz_changes_video(target: Target, ptz):
    main, sub = _url(target, "main"), _url(target, "sub")
    wait_stream(main)
    ptz_reset(ptz)
    time.sleep(1.5)
    before = grab(main, "ptz_0_before.png")
    time.sleep(1.0)
    baseline = diff(before, grab(main))  # 動かさないときの変化(テストパターンの動く部分)

    ptz.AbsoluteMove({"ProfileToken": PROFILE, "Position": {"PanTilt": {"x": 0.6, "y": -0.4}, "Zoom": {"x": 1}}})
    ptz_wait_idle(ptz)
    time.sleep(1.5)  # スナップショット(1 秒ごと)が更新されるまで
    after = grab(main, "ptz_1_zoom4x_pan0.6_tilt-0.4.png")
    moved = diff(before, after)
    assert moved > baseline * 3 + 10, (baseline, moved)

    # sub とスナップショットも同じ窓(main の「後」に近く、「前」とは遠い)
    sub_after = grab(sub, "ptz_2_sub_after.png")
    snap = f"http://{target.host}:{target.onvif_port}/onvif/snapshot.jpg"
    snap_file = (SHOTS or Path("/tmp")) / "ptz_3_snapshot_after.jpg"
    snap_file.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(snap, timeout=5) as r:
        snap_file.write_bytes(r.read())
    snap_after = grab(str(snap_file))
    for name, img in (("sub", sub_after), ("snapshot", snap_after)):
        assert diff(after, img) < moved / 3, (name, diff(after, img), moved)

    # ホームに戻すと元の見た目に戻る
    ptz.GotoHomePosition({"ProfileToken": PROFILE})
    ptz_wait_idle(ptz)
    time.sleep(1.0)
    back = grab(main, "ptz_4_back_home.png")
    assert diff(before, back) < moved / 3, (diff(before, back), moved)


def test_ptz_does_not_interrupt_stream(target: Target, ptz):
    """PTZ を動かし続けている間も、main / sub の配信が途切れず、解像度・コーデック・GOP が変わらない。"""
    seconds = 12.0
    urls = {w: _url(target, w) for w in ("main", "sub")}
    before = {w: probe_stream(u) for w, u in urls.items()}
    restarts = health(target)["ffmpeg_restarts"]
    results: dict[str, list] = {}

    def probe(which: str) -> None:
        results[which] = probe_packets(urls[which], seconds)

    threads = [threading.Thread(target=probe, args=(w,)) for w in urls]
    for t in threads:
        t.start()
    time.sleep(1.5)
    # ズームしながらパン、プリセット、ホーム(切り出し窓の大きさと位置が毎フレーム変わる)
    ptz.ContinuousMove({"ProfileToken": PROFILE, "Velocity": {"PanTilt": {"x": 1, "y": 0.5}, "Zoom": {"x": 1}},
                        "Timeout": timedelta(seconds=2)})
    time.sleep(2.5)
    ptz.ContinuousMove({"ProfileToken": PROFILE, "Velocity": {"PanTilt": {"x": -1, "y": -1}, "Zoom": {"x": -0.5}},
                        "Timeout": timedelta(seconds=2)})
    time.sleep(1.0)
    ptz.Stop({"ProfileToken": PROFILE})
    ptz.AbsoluteMove({"ProfileToken": PROFILE, "Position": {"PanTilt": {"x": -1, "y": 1}, "Zoom": {"x": 0.8}}})
    time.sleep(2.0)
    ptz.GotoHomePosition({"ProfileToken": PROFILE})
    for t in threads:
        t.join()

    for which, pkts in results.items():
        pts = [t for t, _ in pkts]
        assert pts[-1] - pts[0] >= seconds - 1.5, (which, pts[0], pts[-1])
        assert all(b > a for a, b in zip(pts, pts[1:], strict=False)), f"{which}: pts が単調増加していない"
        gaps = [b - a for a, b in zip(pts, pts[1:], strict=False)]
        assert max(gaps) < 0.2, f"{which}: 配信が途切れた(最大の間隔 {max(gaps):.3f} 秒)"
        iv = keyframe_intervals(pkts)
        assert len(iv) >= seconds - 3 and all(abs(x - 1.0) < 0.08 for x in iv), (which, iv)
    after = {w: probe_stream(u) for w, u in urls.items()}
    assert after == before, (before, after)
    h = health(target)
    assert h["ffmpeg_restarts"] == restarts and h["ffmpeg_running"]
    assert h["ptz"]["command_errors"] == 0, h["ptz"]
    ptz_wait_idle(ptz)


class FrameReader:
    """RTSP を読み続け、届いた時刻と縮小グレースケールのフレームを記録する。"""

    def __init__(self, url: str):
        self.frames: list[tuple[float, bytes]] = []
        self.proc = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-fflags", "nobuffer", "-flags", "low_delay", "-rtsp_transport", "tcp",
             "-i", url, "-vf", f"scale={SMALL[0]}:{SMALL[1]},format=gray", "-f", "rawvideo", "pipe:1"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self) -> None:
        size = SMALL[0] * SMALL[1]
        while True:
            buf = self.proc.stdout.read(size)
            if len(buf) < size:
                return
            self.frames.append((time.monotonic(), buf))

    def since(self, t: float) -> list[tuple[float, bytes]]:
        return [f for f in self.frames if f[0] >= t]

    def close(self) -> None:
        self.proc.kill()
        self.proc.wait()


def test_ptz_latency(target: Target, ptz):
    """操作(SOAP の送信)から、変化したフレームが RTSP で届くまでの時間。

    受信側の遅延(RTSP、デコード)を含む。sub(H.264)を読む(main と同じ窓、デコードが軽い)。
    4 倍にズームした状態でパンする(1 フレーム目から大きく動くので、変化したフレームを取りこぼしにくい)。
    判定は「操作直前のフレームとの差」が、止まっている間の差(テストパターン自体の動き)を超えた最初のフレーム。
    """
    ptz_reset(ptz)
    ptz.AbsoluteMove({"ProfileToken": PROFILE, "Position": {"PanTilt": {"x": 0, "y": 0}, "Zoom": {"x": 1}}})
    ptz_wait_idle(ptz)
    reader = FrameReader(_url(target, os.environ.get("PTZ_LATENCY_STREAM", "sub")))
    try:
        assert reader.thread.is_alive()
        time.sleep(3.0)
        t = time.monotonic()
        time.sleep(2.0)
        idle = reader.since(t)
        assert len(idle) > 20, len(idle)
        noise = [diff(idle[0][1], f[1]) for f in idle[1:]]
        threshold = max(noise) * 2 + float(os.environ.get("PTZ_LATENCY_MARGIN", "1"))

        latencies = []
        runs = int(os.environ.get("PTZ_LATENCY_RUNS", "6"))
        for i in range(runs):
            pan = 0.5 if i % 2 == 0 else -0.5
            started = time.monotonic()
            ptz.AbsoluteMove({"ProfileToken": PROFILE, "Position": {"PanTilt": {"x": pan, "y": 0}}})
            sent = time.monotonic()
            time.sleep(1.5)
            frames = list(reader.frames[-60:])
            ref = [f for f in frames if f[0] < started][-1]
            changed = next((f for f in frames if f[0] >= started and diff(ref[1], f[1]) > threshold), None)
            if os.environ.get("PTZ_LATENCY_DEBUG"):
                print("DBG", pan, [(round(f[0] - started, 3), round(diff(ref[1], f[1]), 2)) for f in frames if f[0] >= started][:25])
            assert changed, f"pan {pan}: 映像が変わらない"
            latencies.append({"pan": pan, "latency_s": round(changed[0] - started, 3),
                              "soap_s": round(sent - started, 3)})
            ptz_wait_idle(ptz)
            time.sleep(1.0)
    finally:
        reader.close()
    med = statistics.median(x["latency_s"] for x in latencies)
    report = {"median_latency_s": med, "noise_max": round(max(noise), 2), "threshold": round(threshold, 2),
              "fps": round(len(idle) / 2.0, 1), "runs": latencies}
    print("PTZ latency:", json.dumps(report, ensure_ascii=False))
    if SHOTS:
        (SHOTS / "ptz_latency.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    assert med < 1.5, report
