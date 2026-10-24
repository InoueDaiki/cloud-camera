"""疑似 PTZ の単体テスト(動きのシミュレーション、FFmpeg のコマンド、設定、PTZ 無効のカメラ)。時計は差し替える。"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from onvif_emulator import streams
from onvif_emulator.config import PtzSettings, load_config
from onvif_emulator.ptz import MAX_PRESETS, PtzController, PtzError, parse_duration
from onvif_emulator.services import CameraServices, RequestContext
from onvif_emulator.soap import SoapFault, parse_request
from onvif_emulator.streams import ffmpeg_command, ptz_crop_commands, ptz_source_size


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clk() -> Clock:
    return Clock()


def ctl(clk: Clock, **kw) -> PtzController:
    return PtzController(PtzSettings(**kw), clock=clk)


def test_absolute_move_speed_limited(clk):
    p = ctl(clk, pan_tilt_speed=0.5, zoom_speed=0.25)
    p.absolute_move(1.0, -1.0, 1.0)
    clk.t += 1.0
    assert p.position() == pytest.approx((0.5, -0.5, 0.25))
    assert p.moving() == (True, True)
    clk.t += 1.0
    assert p.position() == pytest.approx((1.0, -1.0, 0.5))
    assert p.moving() == (False, True)
    clk.t += 10
    assert p.position() == (1.0, -1.0, 1.0) and p.moving() == (False, False)


def test_speed_fraction_and_zero_speed(clk):
    p = ctl(clk, pan_tilt_speed=1.0)
    p.absolute_move(1.0, None, None, pan_tilt_speed=(0.25, 1.0))
    clk.t += 1.0
    assert p.position()[0] == pytest.approx(0.25)
    p.absolute_move(-1.0, None, None, pan_tilt_speed=(0.0, 0.0))  # 0 は既定(最大)扱い
    clk.t += 0.25
    assert p.position()[0] == pytest.approx(0.0)


def test_continuous_move_timeout_is_exact(clk):
    p = ctl(clk, pan_tilt_speed=0.5, zoom_speed=0.5)
    p.continuous_move(1.0, -0.5, None, timeout_s=1.0)
    clk.t += 5.0  # 途中で update されなくても Timeout の時刻で止まる
    assert p.position() == pytest.approx((0.5, -0.25, 0.0))
    assert p.moving() == (False, False) and p.deadline is None


def test_continuous_move_default_timeout_and_clamp(clk):
    p = ctl(clk, pan_tilt_speed=0.1)
    p.continuous_move(9.0, None, None)  # 速度は 1 に丸める、Timeout 省略は 10 秒
    clk.t += 11
    assert p.position()[0] == pytest.approx(1.0)
    p.continuous_move(-1.0, None, None, timeout_s=0.0001)  # 下限 0.1 秒に丸める
    clk.t += 1
    assert p.position()[0] == pytest.approx(0.99)


def test_continuous_move_stops_at_limit(clk):
    p = ctl(clk, pan_tilt_speed=1.0)
    p.continuous_move(1.0, None, None, timeout_s=60)
    clk.t += 2
    assert p.position()[0] == 1.0 and p.moving() == (False, False)


def test_stop_axes_separately(clk):
    p = ctl(clk, pan_tilt_speed=0.5, zoom_speed=0.5)
    p.continuous_move(1.0, 0.0, 1.0, timeout_s=30)
    clk.t += 0.5
    p.stop(pan_tilt=True, zoom=False)
    clk.t += 0.5
    assert p.position() == pytest.approx((0.25, 0.0, 0.5))
    p.stop()
    assert p.moving() == (False, False) and p.deadline is None


def test_relative_move_accumulates_on_target(clk):
    p = ctl(clk, pan_tilt_speed=0.5)
    p.relative_move(0.4, None, None)
    clk.t += 0.2
    p.relative_move(0.4, None, None)  # 動いている途中なら目標に足す
    clk.t += 10
    assert p.position()[0] == pytest.approx(0.8)
    p.relative_move(5.0, -5.0, -5.0)  # 範囲外は丸める
    clk.t += 10
    assert p.position() == (1.0, -1.0, 0.0)


def test_window_mapping(clk):
    p = ctl(clk, max_zoom=4.0, pan_tilt_speed=100, zoom_speed=100)
    w = p.window()
    assert (w.magnification, w.fx, w.fy) == (1.0, 0.5, 0.5)
    p.absolute_move(-1.0, 1.0, 1.0)
    clk.t += 1
    w = p.window()
    assert (w.magnification, w.fx, w.fy) == (4.0, 0.0, 0.0)  # 左上、4 倍
    p.absolute_move(1.0, -1.0, 0.5)
    clk.t += 1
    w = p.window()
    assert (w.magnification, w.fx, w.fy) == (2.5, 1.0, 1.0)  # 右下、1 + 0.5 × 3 倍
    assert ptz_crop_commands(w) == {"w": "iw/2.5", "x": "(in_w-out_w)*1", "y": "(in_h-out_h)*1"}


def test_presets_and_home(clk):
    p = ctl(clk, pan_tilt_speed=100, zoom_speed=100)
    p.absolute_move(0.5, 0.5, 0.5)
    clk.t += 1
    assert p.set_preset("A") == "1"
    assert p.set_preset() == "2" and p.presets["2"].name == "Preset 2"
    p.remove_preset("1")
    assert p.set_preset("B") == "1"  # 空いた番号を使う
    with pytest.raises(PtzError) as e:
        p.set_preset("B")
    assert e.value.kind == "PresetExist"
    with pytest.raises(PtzError) as e:
        p.set_preset("x" * 65)
    assert e.value.kind == "InvalidPresetName"
    with pytest.raises(PtzError) as e:
        p.goto_preset("99")
    assert e.value.kind == "NoToken"
    for _ in range(MAX_PRESETS - 2):
        p.set_preset()
    with pytest.raises(PtzError) as e:
        p.set_preset()
    assert e.value.kind == "TooManyPresets"

    p.set_home()
    p.absolute_move(-1, -1, 0)
    clk.t += 1
    p.goto_home()
    clk.t += 1
    assert p.position() == (0.5, 0.5, 0.5)


def test_listeners_are_called(clk):
    p = ctl(clk)
    calls = []
    p.listeners.append(lambda: calls.append(1))
    p.continuous_move(1, 0, 0, 1)
    p.absolute_move(0, 0, 0)
    p.stop()
    assert len(calls) == 3


@pytest.mark.parametrize(
    ("text", "sec"),
    [("PT10S", 10), ("PT0.5S", 0.5), ("PT1M30S", 90), ("P0DT0H0M5S", 5), ("PT1H", 3600), ("P1D", 86400)],
)
def test_parse_duration(text, sec):
    assert parse_duration(text) == sec


@pytest.mark.parametrize("text", ["", "P", "PT", "10S", "PTxS"])
def test_parse_duration_invalid(text):
    with pytest.raises(ValueError):
        parse_duration(text)


# ---------------------------------------------------------------------------
# FFmpeg のコマンドと設定
# ---------------------------------------------------------------------------


def test_ffmpeg_command_with_ptz(tmp_path):
    cfg = load_config(env={"EMULATOR_CAMERAS": "1", "EMULATOR_RUNTIME_DIR": str(tmp_path)})
    cam = cfg.cameras[0]
    cmd = ffmpeg_command(cfg, cam)
    graph = cmd[cmd.index("-filter_complex") + 1]
    # 元映像は main の 2 倍、切り出してから main の解像度へ
    assert cmd[cmd.index("-i") + 1] == "testsrc=size=2560x1440:rate=15"
    assert f"zmq=bind_address=ipc\\\\://{tmp_path}/cam1.ptz.sock," in graph
    assert "crop@ptz=w=iw/1:h=ow*720/1280:x=(in_w-out_w)*0.5:y=(in_h-out_h)*0.5,null,scale=1280:720" in graph
    # 焼き込みは切り出しのあと(常に見える)、毎フレーム読み直す
    assert graph.index("crop@ptz") < graph.index("drawtext")
    assert ":reload=1:" in graph
    # 出力の解像度とコーデックは PTZ なしと同じ
    assert "[s]scale=640:360[sub]" in graph and "libx265" in cmd and "libx264" in cmd


def test_ffmpeg_command_restarts_from_current_window(tmp_path, clk):
    cfg = load_config(env={"EMULATOR_CAMERAS": "1", "EMULATOR_RUNTIME_DIR": str(tmp_path)})
    p = ctl(clk, pan_tilt_speed=100, zoom_speed=100)
    p.absolute_move(1, 1, 1)
    clk.t += 1
    cmd = ffmpeg_command(cfg, cfg.cameras[0], p.window())
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "crop@ptz=w=iw/4:h=ow*720/1280:x=(in_w-out_w)*1:y=(in_h-out_h)*0," in graph


def test_ffmpeg_command_without_ptz(tmp_path):
    cfg = load_config(env={"EMULATOR_CAMERAS": "1", "EMULATOR_PTZ": "false", "EMULATOR_RUNTIME_DIR": str(tmp_path)})
    cmd = ffmpeg_command(cfg, cfg.cameras[0])
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert cmd[cmd.index("-i") + 1] == "testsrc=size=1280x720:rate=15"
    assert "zmq" not in graph and "crop" not in graph and "reload" not in graph


def test_source_size(tmp_path, monkeypatch):
    cfg = load_config(env={"EMULATOR_CAMERAS": "1", "EMULATOR_PTZ_SOURCE_SCALE": "3", "EMULATOR_RUNTIME_DIR": str(tmp_path)})
    assert ptz_source_size(cfg, cfg.cameras[0]) == (3840, 2160)
    fcfg = load_config(env={"EMULATOR_CAMERAS": "1", "EMULATOR_SOURCE": "file:/media/x.mp4"})
    cam = fcfg.cameras[0]
    for res, expected in (
        ((1920, 1080), (1920, 1080)),  # 動画ファイルは元の解像度
        ((1440, 1080), (1920, 1080)),  # 4:3 は 16:9 に広げる(黒帯)
        ((640, 360), (1280, 720)),  # main より小さければ main の大きさ
        (None, (2560, 1440)),  # 取れなければ main × source_scale
    ):
        monkeypatch.setattr(streams, "probe_resolution", lambda *_a, res=res: res)
        assert ptz_source_size(fcfg, cam) == expected, res


def test_overlay_text_has_ptz_line(tmp_path):
    cfg = load_config(env={"EMULATOR_CAMERAS": "1", "EMULATOR_RUNTIME_DIR": str(tmp_path)})
    cam = cfg.cameras[0]
    path = streams.write_overlay_text(cfg, cam, "PTZ pan +0.500 tilt -0.250 zoom 1.000 (4.00x)")
    lines = path.read_text().splitlines()
    assert lines[0].startswith("cam-001") and lines[3].startswith("PTZ pan +0.500")
    assert not path.with_suffix(".tmp").exists()


def test_config_ptz_settings(tmp_path):
    y = tmp_path / "c.yaml"
    y.write_text("defaults: {ptz_max_zoom: 8}\ncameras:\n  - {name: a}\n  - {name: b, ptz: false}\n")
    cfg = load_config(y, env={"EMULATOR_PTZ_PAN_TILT_SPEED": "1.5", "EMULATOR_CAM1_PTZ_ZOOM_SPEED": "0.25"})
    a, b = cfg.cameras
    assert a.ptz == PtzSettings(enabled=True, max_zoom=8, pan_tilt_speed=1.5, zoom_speed=0.25, source_scale=2.0)
    assert b.ptz.enabled is False
    for env in ({"EMULATOR_PTZ_MAX_ZOOM": "0.5"}, {"EMULATOR_PTZ_ZOOM_SPEED": "0"}, {"EMULATOR_PTZ_SOURCE_SCALE": "8"}):
        with pytest.raises(ValueError):
            load_config(env=env)


# ---------------------------------------------------------------------------
# PTZ を無効にしたカメラ
# ---------------------------------------------------------------------------


def _call(svc: CameraServices, ns: str, body: str) -> str:
    env = (
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Header>'
        '<wsse:Security xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">'
        "<wsse:UsernameToken><wsse:Username>admin</wsse:Username><wsse:Password>admin</wsse:Password>"
        f'</wsse:UsernameToken></wsse:Security></s:Header><s:Body><x:{body} xmlns:x="{ns}"/></s:Body></s:Envelope>'
    )
    return svc.handle(parse_request(env.encode()), RequestContext(host="127.0.0.1", local_ip="127.0.0.1"))


def test_ptz_disabled_camera():
    cfg = load_config(env={"EMULATOR_CAMERAS": "1", "EMULATOR_PTZ": "false"})
    svc = CameraServices(cfg, cfg.cameras[0], ptz=None)
    dev = "http://www.onvif.org/ver10/device/wsdl"
    assert "ptz_service" not in _call(svc, dev, "GetServices")
    assert "<tt:PTZ>" not in _call(svc, dev, "GetCapabilities")
    assert "PTZConfiguration" not in _call(svc, "http://www.onvif.org/ver10/media/wsdl", "GetProfiles")
    with pytest.raises(SoapFault) as e:
        _call(svc, "http://www.onvif.org/ver20/ptz/wsdl", "GetNodes")
    assert e.value.subcodes == ["ter:ActionNotSupported", "ter:PTZNotSupported"]


def test_ptz_enabled_capabilities_xml_is_well_formed():
    cfg = load_config(env={"EMULATOR_CAMERAS": "1"})
    svc = CameraServices(cfg, cfg.cameras[0], ptz=PtzController(cfg.cameras[0].ptz))
    for ns, op in (
        ("http://www.onvif.org/ver10/device/wsdl", "GetCapabilities"),
        ("http://www.onvif.org/ver20/ptz/wsdl", "GetNodes"),
        ("http://www.onvif.org/ver20/ptz/wsdl", "GetConfigurations"),
    ):
        xml = _call(svc, ns, op)
        decl = 'xmlns:tt="http://www.onvif.org/ver10/schema" xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl" ' \
               'xmlns:tds="http://www.onvif.org/ver10/device/wsdl"'
        ET.fromstring(f"<r {decl}>{xml}</r>")
