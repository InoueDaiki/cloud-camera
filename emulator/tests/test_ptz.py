"""PTZ サービスの結合テスト(onvif-zeep と、公式 WSDL を使う strict な zeep)。

速度は既定値(pan / tilt 0.5 単位/秒、zoom 0.5 単位/秒)を前提にする。1 台目のカメラを使い、各テストの後で位置を戻す。
"""

from __future__ import annotations

import time
from datetime import timedelta

import pytest
from conftest import Target
from onvif.exceptions import ONVIFError
from onvif_helpers import (
    MEDIA2_NS,
    MEDIA2_WSDL,
    PTZ_NS,
    PTZ_WSDL,
    camera,
    media2,
    ptz_reset,
    ptz_status,
    ptz_wait_idle,
)
from test_onvif import _post
from zeep import Client
from zeep.wsse.username import UsernameToken

PROFILE = "Profile_Main"
GENERIC = "http://www.onvif.org/ver10/tptz/"
SPEED = 0.5  # 既定の pan / tilt / zoom の最大速度(単位 / 秒)


@pytest.fixture(scope="module")
def cam(target: Target):
    return camera(target.host, target.onvif_port, target.username, target.password)


@pytest.fixture
def ptz(cam):
    svc = cam.create_ptz_service()
    ptz_reset(svc)
    yield svc
    ptz_reset(svc)


def _abs(ptz, pan=None, tilt=None, zoom=None, speed=None, profile=PROFILE):
    pos = {}
    if pan is not None or tilt is not None:
        pos["PanTilt"] = {"x": pan or 0, "y": tilt or 0}
    if zoom is not None:
        pos["Zoom"] = {"x": zoom}
    req = {"ProfileToken": profile, "Position": pos}
    if speed is not None:
        req["Speed"] = {"PanTilt": {"x": speed, "y": speed}, "Zoom": {"x": speed}}
    ptz.AbsoluteMove(req)


# ---------------------------------------------------------------------------
# 広告(GetCapabilities / GetServices / プロファイル)
# ---------------------------------------------------------------------------


def test_ptz_is_advertised(target: Target, cam):
    dev = cam.devicemgmt
    caps = dev.GetCapabilities({"Category": "All"})
    assert caps.PTZ.XAddr.endswith("/onvif/ptz_service")
    assert dev.GetCapabilities({"Category": "PTZ"}).PTZ.XAddr == caps.PTZ.XAddr
    services = {s.Namespace: s for s in dev.GetServices({"IncludeCapability": False})}
    assert services[PTZ_NS].XAddr == caps.PTZ.XAddr

    # Media(ver10): main と sub に同じ PTZConfiguration
    profiles = cam.create_media_service().GetProfiles()
    assert [p.PTZConfiguration.token for p in profiles] == ["PTZConfig_1", "PTZConfig_1"]
    assert profiles[0].PTZConfiguration.NodeToken == "PTZNode_1"

    # Media2: Type=All か PTZ のときだけ Configurations.PTZ
    m2 = media2(cam, target.username, target.password)
    for types in (["All"], ["PTZ"]):
        got = m2.GetProfiles({"Type": types})
        assert [p.Configurations.PTZ.token for p in got] == ["PTZConfig_1", "PTZConfig_1"], types
    assert all(p.Configurations.PTZ is None for p in m2.GetProfiles({"Type": ["VideoEncoder"]}))


def test_nodes_configurations_and_spaces(ptz):
    nodes = ptz.GetNodes()
    assert [n.token for n in nodes] == ["PTZNode_1"]
    node = ptz.GetNode({"NodeToken": "PTZNode_1"})
    assert node.HomeSupported is True and node.MaximumNumberOfPresets >= 1
    sp = node.SupportedPTZSpaces
    assert sp.AbsolutePanTiltPositionSpace[0].URI == GENERIC + "PanTiltSpaces/PositionGenericSpace"
    assert (sp.AbsolutePanTiltPositionSpace[0].XRange.Min, sp.AbsolutePanTiltPositionSpace[0].XRange.Max) == (-1, 1)
    assert sp.AbsoluteZoomPositionSpace[0].URI == GENERIC + "ZoomSpaces/PositionGenericSpace"
    assert (sp.AbsoluteZoomPositionSpace[0].XRange.Min, sp.AbsoluteZoomPositionSpace[0].XRange.Max) == (0, 1)
    assert sp.RelativePanTiltTranslationSpace[0].URI == GENERIC + "PanTiltSpaces/TranslationGenericSpace"
    assert sp.RelativeZoomTranslationSpace[0].URI == GENERIC + "ZoomSpaces/TranslationGenericSpace"
    assert sp.ContinuousPanTiltVelocitySpace[0].URI == GENERIC + "PanTiltSpaces/VelocityGenericSpace"
    assert sp.ContinuousZoomVelocitySpace[0].URI == GENERIC + "ZoomSpaces/VelocityGenericSpace"
    assert sp.PanTiltSpeedSpace[0].URI == GENERIC + "PanTiltSpaces/GenericSpeedSpace"
    assert sp.ZoomSpeedSpace[0].URI == GENERIC + "ZoomSpaces/ZoomGenericSpeedSpace"

    confs = ptz.GetConfigurations()
    assert [c.token for c in confs] == ["PTZConfig_1"]
    conf = ptz.GetConfiguration({"PTZConfigurationToken": "PTZConfig_1"})
    assert conf.NodeToken == "PTZNode_1"
    assert conf.DefaultPTZTimeout == timedelta(seconds=10)
    assert conf.DefaultContinuousPanTiltVelocitySpace == GENERIC + "PanTiltSpaces/VelocityGenericSpace"
    opts = ptz.GetConfigurationOptions({"ConfigurationToken": "PTZConfig_1"})
    assert opts.Spaces.ContinuousZoomVelocitySpace[0].XRange.Max == 1
    assert opts.PTZTimeout.Min <= timedelta(seconds=1) and opts.PTZTimeout.Max >= timedelta(seconds=60)
    with pytest.raises(ONVIFError, match="does not exist"):
        ptz.GetNode({"NodeToken": "nope"})
    with pytest.raises(ONVIFError, match="does not exist"):
        ptz.GetConfiguration({"PTZConfigurationToken": "nope"})


def test_status_initial(ptz):
    pan, tilt, zoom, pt, z = ptz_status(ptz)
    assert (pan, tilt, zoom, pt, z) == (0, 0, 0, "IDLE", "IDLE")
    st = ptz.GetStatus({"ProfileToken": PROFILE})
    assert st.UtcTime is not None
    assert st.Position.PanTilt.space == GENERIC + "PanTiltSpaces/PositionGenericSpace"
    # sub のプロファイルも同じ窓
    assert ptz.GetStatus({"ProfileToken": "Profile_Sub"}).Position.PanTilt.x == 0


# ---------------------------------------------------------------------------
# 動き
# ---------------------------------------------------------------------------


def test_absolute_move_is_smooth_and_speed_limited(ptz):
    started = time.monotonic()
    _abs(ptz, pan=1, tilt=-0.5, zoom=1)
    samples = []
    while True:
        s = ptz_status(ptz)
        samples.append((time.monotonic() - started, *s))
        if s[3] == "IDLE" and s[4] == "IDLE":
            break
        assert time.monotonic() - started < 10
        time.sleep(0.2)
    elapsed = samples[-1][0]
    # pan 0→1 は 0.5 単位/秒で 2 秒かかる(瞬間移動しない)
    assert 1.6 < elapsed < 3.5, samples
    assert samples[-1][1:4] == (1, -0.5, 1)
    moving = [s for s in samples if s[4] == "MOVING"]
    assert len(moving) >= 4, samples
    pans = [s[1] for s in samples]
    assert pans == sorted(pans), "単調に動く"
    # どの時点でも上限速度を超えない
    for t, pan, *_ in samples:
        assert pan <= SPEED * t + 0.1, (t, pan)


def test_absolute_move_speed_fraction(ptz):
    _abs(ptz, pan=0.5, speed=0.5)  # 最大の半分 = 0.25 単位/秒
    time.sleep(1.0)
    pan = ptz_status(ptz)[0]
    assert 0.15 < pan < 0.4, pan
    ptz_wait_idle(ptz)


def test_continuous_move_stops_at_timeout(ptz):
    ptz.ContinuousMove({"ProfileToken": PROFILE, "Velocity": {"PanTilt": {"x": 1, "y": 0}}, "Timeout": timedelta(seconds=1)})
    time.sleep(0.4)
    pan, _, _, pt, z = ptz_status(ptz)
    assert pt == "MOVING" and z == "IDLE"
    assert 0.1 < pan < 0.4
    time.sleep(1.2)
    pan1, _, _, pt, _ = ptz_status(ptz)
    assert pt == "IDLE"
    assert pan1 == pytest.approx(SPEED * 1.0, abs=0.06)  # Timeout 1 秒 × 0.5 単位/秒
    time.sleep(0.5)
    assert ptz_status(ptz)[0] == pan1, "Timeout のあとは動かない"


def test_continuous_move_and_stop(ptz):
    ptz.ContinuousMove({"ProfileToken": PROFILE, "Velocity": {"PanTilt": {"x": -1, "y": 1}, "Zoom": {"x": 1}}})
    time.sleep(0.6)
    ptz.Stop({"ProfileToken": PROFILE, "PanTilt": True, "Zoom": False})
    pan, tilt, zoom, pt, z = ptz_status(ptz)
    assert pt == "IDLE" and z == "MOVING"
    assert pan < -0.2 and tilt > 0.2
    ptz.Stop({"ProfileToken": PROFILE})
    _, _, zoom, pt, z = ptz_status(ptz)
    assert (pt, z) == ("IDLE", "IDLE")
    time.sleep(0.3)
    assert ptz_status(ptz)[2] == zoom


def test_continuous_move_stops_at_the_edge(ptz):
    _abs(ptz, pan=0.9)
    ptz_wait_idle(ptz)
    ptz.ContinuousMove({"ProfileToken": PROFILE, "Velocity": {"PanTilt": {"x": 1, "y": 0}}, "Timeout": timedelta(seconds=30)})
    time.sleep(0.5)
    pan, _, _, pt, _ = ptz_status(ptz)
    assert pan == 1 and pt == "IDLE"


def test_relative_move(ptz):
    ptz.RelativeMove({"ProfileToken": PROFILE, "Translation": {"PanTilt": {"x": 0.3, "y": -0.2}, "Zoom": {"x": 0.4}}})
    # 動いている途中の差分は目標に足す
    ptz.RelativeMove({"ProfileToken": PROFILE, "Translation": {"PanTilt": {"x": 0.3, "y": 0}}})
    pan, tilt, zoom, *_ = ptz_wait_idle(ptz)
    assert (pan, tilt, zoom) == pytest.approx((0.6, -0.2, 0.4), abs=1e-4)


def test_range_is_clamped(ptz):
    _abs(ptz, pan=5, tilt=-3, zoom=2)
    assert ptz_wait_idle(ptz)[:3] == (1, -1, 1)
    ptz.RelativeMove({"ProfileToken": PROFILE, "Translation": {"PanTilt": {"x": -9, "y": 9}, "Zoom": {"x": -9}}})
    assert ptz_wait_idle(ptz)[:3] == (-1, 1, 0)
    # 速度も -1〜1 に丸める(5 を指定しても上限の速さ)
    ptz.ContinuousMove({"ProfileToken": PROFILE, "Velocity": {"PanTilt": {"x": 5, "y": 0}}, "Timeout": timedelta(seconds=0.5)})
    time.sleep(1.0)
    assert ptz_status(ptz)[0] == pytest.approx(-1 + SPEED * 0.5, abs=0.06)


def test_unknown_profile_and_space(ptz):
    with pytest.raises(ONVIFError, match="Profile token does not exist"):
        ptz.GetStatus({"ProfileToken": "nope"})
    with pytest.raises(ONVIFError, match="Space not supported"):
        ptz.AbsoluteMove({"ProfileToken": PROFILE, "Position": {"PanTilt": {"x": 0, "y": 0, "space": "http://example.com/x"}}})


# ---------------------------------------------------------------------------
# プリセットとホーム
# ---------------------------------------------------------------------------


def test_presets(ptz):
    _abs(ptz, pan=0.5, tilt=0.25, zoom=0.5)
    ptz_wait_idle(ptz)
    token = ptz.SetPreset({"ProfileToken": PROFILE, "PresetName": "玄関"})
    assert token == "1"
    presets = ptz.GetPresets({"ProfileToken": PROFILE})
    assert [(p.token, p.Name) for p in presets] == [("1", "玄関")]
    pos = presets[0].PTZPosition
    assert (pos.PanTilt.x, pos.PanTilt.y, pos.Zoom.x) == (0.5, 0.25, 0.5)

    # 同じ名前は作れない。名前なしは自動で付く
    with pytest.raises(ONVIFError, match="already exists"):
        ptz.SetPreset({"ProfileToken": PROFILE, "PresetName": "玄関"})
    _abs(ptz, pan=-0.5, tilt=0, zoom=0)
    ptz_wait_idle(ptz)
    token2 = ptz.SetPreset({"ProfileToken": PROFILE})
    assert token2 == "2"
    assert ptz.GetPresets({"ProfileToken": PROFILE})[1].Name == "Preset 2"

    # GotoPreset で戻る(滑らかに動く)
    ptz.GotoPreset({"ProfileToken": PROFILE, "PresetToken": token})
    assert ptz_status(ptz)[3] == "MOVING"
    assert ptz_wait_idle(ptz)[:3] == (0.5, 0.25, 0.5)

    # token を指定すると上書き
    _abs(ptz, pan=0)
    ptz_wait_idle(ptz)
    assert ptz.SetPreset({"ProfileToken": PROFILE, "PresetToken": token2, "PresetName": "裏口"}) == token2
    p2 = ptz.GetPresets({"ProfileToken": PROFILE})[1]
    assert (p2.Name, p2.PTZPosition.PanTilt.x) == ("裏口", 0)

    ptz.RemovePreset({"ProfileToken": PROFILE, "PresetToken": token})
    assert [p.token for p in ptz.GetPresets({"ProfileToken": PROFILE})] == [token2]
    for op, req in (
        (ptz.GotoPreset, {"PresetToken": token}),
        (ptz.RemovePreset, {"PresetToken": token}),
        (ptz.SetPreset, {"PresetToken": "99"}),
    ):
        with pytest.raises(ONVIFError, match="does not exist"):
            op({"ProfileToken": PROFILE, **req})


def test_home_position(ptz):
    # 既定のホームは (0, 0, 0)
    _abs(ptz, pan=0.4, zoom=0.2)
    ptz_wait_idle(ptz)
    ptz.GotoHomePosition({"ProfileToken": PROFILE})
    assert ptz_wait_idle(ptz)[:3] == (0, 0, 0)
    # SetHomePosition で今の位置をホームにする
    _abs(ptz, pan=-0.3, tilt=0.6, zoom=0.1)
    ptz_wait_idle(ptz)
    ptz.SetHomePosition({"ProfileToken": PROFILE})
    _abs(ptz, pan=0.3, tilt=0, zoom=0)
    ptz_wait_idle(ptz)
    ptz.GotoHomePosition({"ProfileToken": PROFILE, "Speed": {"PanTilt": {"x": 1, "y": 1}, "Zoom": {"x": 1}}})
    assert ptz_wait_idle(ptz)[:3] == pytest.approx((-0.3, 0.6, 0.1), abs=1e-4)


# ---------------------------------------------------------------------------
# 認証とスキーマ
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("digest", [True, False], ids=["PasswordDigest", "PasswordText"])
def test_ptz_requires_auth(target: Target, digest: bool):
    bad = camera(target.host, target.onvif_port, target.username, "wrong-password", digest=digest).create_ptz_service()
    with pytest.raises(ONVIFError, match="Sender not Authorized"):
        bad.GetStatus({"ProfileToken": PROFILE})
    with pytest.raises(ONVIFError, match="Sender not Authorized"):
        bad.ContinuousMove({"ProfileToken": PROFILE, "Velocity": {"PanTilt": {"x": 1, "y": 0}}})
    good = camera(target.host, target.onvif_port, target.username, target.password, digest=digest).create_ptz_service()
    assert ptz_status(good)[3] == "IDLE"  # 誤った認証の ContinuousMove で動いていない
    status, text = _post(
        f"http://{target.host}:{target.onvif_port}/onvif/ptz_service",
        '<s:Body><GetNodes xmlns="http://www.onvif.org/ver20/ptz/wsdl"/></s:Body>',
    )
    assert status == 400 and "ter:NotAuthorized" in text


def test_ptz_strict_schema(target: Target):
    """公式の PTZ WSDL(26.06)と strict な zeep で、レスポンスがスキーマどおりか確かめる。"""
    xaddr = f"http://{target.host}:{target.onvif_port}/onvif/ptz_service"
    client = Client(PTZ_WSDL, wsse=UsernameToken(target.username, target.password, use_digest=True))
    svc = client.create_service(f"{{{PTZ_NS}}}PTZBinding", xaddr)
    assert svc.GetServiceCapabilities().MoveStatus is True
    assert svc.GetNodes()[0].token == "PTZNode_1"
    assert svc.GetNode(NodeToken="PTZNode_1").SupportedPTZSpaces.ZoomSpeedSpace
    assert svc.GetConfigurations()[0].PanTiltLimits.Range.XRange.Max == 1
    assert svc.GetConfiguration(PTZConfigurationToken="PTZConfig_1").ZoomLimits.Range.XRange.Max == 1
    assert svc.GetCompatibleConfigurations(ProfileToken=PROFILE)[0].token == "PTZConfig_1"
    assert svc.GetConfigurationOptions(ConfigurationToken="PTZConfig_1").PTZTimeout.Max
    st = svc.GetStatus(ProfileToken=PROFILE)
    assert st.MoveStatus.PanTilt in ("IDLE", "MOVING") and st.UtcTime
    try:
        token = svc.SetPreset(ProfileToken=PROFILE, PresetName="strict")
        assert svc.GetPresets(ProfileToken=PROFILE)[0].PTZPosition.Zoom.x is not None
        svc.GotoPreset(ProfileToken=PROFILE, PresetToken=token)
        svc.RemovePreset(ProfileToken=PROFILE, PresetToken=token)
        svc.RelativeMove(ProfileToken=PROFILE, Translation={"Zoom": {"x": 0.1}})
        svc.AbsoluteMove(ProfileToken=PROFILE, Position={"Zoom": {"x": 0}}, Speed={"Zoom": {"x": 1}})
        svc.ContinuousMove(ProfileToken=PROFILE, Velocity={"PanTilt": {"x": 0.1, "y": 0}}, Timeout=timedelta(seconds=0.2))
        svc.Stop(ProfileToken=PROFILE)
        svc.SetHomePosition(ProfileToken=PROFILE)
        svc.GotoHomePosition(ProfileToken=PROFILE)
    finally:
        svc.Stop(ProfileToken=PROFILE)
        svc.AbsoluteMove(ProfileToken=PROFILE, Position={"PanTilt": {"x": 0, "y": 0}, "Zoom": {"x": 0}})
        deadline = time.monotonic() + 10
        while svc.GetStatus(ProfileToken=PROFILE).MoveStatus.Zoom == "MOVING" and time.monotonic() < deadline:
            time.sleep(0.1)
        svc.SetHomePosition(ProfileToken=PROFILE)

    # Media2 のプロファイルの PTZ 構成も strict に読める
    m2 = Client(MEDIA2_WSDL, wsse=UsernameToken(target.username, target.password, use_digest=True)).create_service(
        f"{{{MEDIA2_NS}}}Media2Binding", f"http://{target.host}:{target.onvif_port}/onvif/media2_service"
    )
    assert {p.Configurations.PTZ.token for p in m2.GetProfiles(Type=["All"])} == {"PTZConfig_1"}
    assert "PTZ" in m2.GetServiceCapabilities().ProfileCapabilities.ConfigurationsSupported
