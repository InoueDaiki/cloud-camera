"""ONVIF PTZ サービス(ver20、tptz)。CameraServices に混ぜて使う(メソッド名の接頭辞 ptz_)。

ノード 1 つ(PTZNode_1)、構成 1 つ(PTZConfig_1)を main と sub の両方のプロファイルに付ける
(main と sub は同じ切り出し窓)。座標は Generic 空間だけを扱う。範囲外の位置・速度・差分は Fault にせず丸める。
"""

from __future__ import annotations

from datetime import UTC, datetime
from xml.etree.ElementTree import Element

from .ptz import DEFAULT_TIMEOUT_S, MAX_PRESETS, TIMEOUT_RANGE_S, PtzController, PtzError, parse_duration
from .soap import SoapFault, attr, child, child_text, esc, invalid_arg

PTZ_NODE_TOKEN = "PTZNode_1"
PTZ_CONFIG_TOKEN = "PTZConfig_1"

_PT = "http://www.onvif.org/ver10/tptz/PanTiltSpaces/"
_Z = "http://www.onvif.org/ver10/tptz/ZoomSpaces/"
SPACE = {
    "pt_position": _PT + "PositionGenericSpace",
    "z_position": _Z + "PositionGenericSpace",
    "pt_translation": _PT + "TranslationGenericSpace",
    "z_translation": _Z + "TranslationGenericSpace",
    "pt_velocity": _PT + "VelocityGenericSpace",
    "z_velocity": _Z + "VelocityGenericSpace",
    "pt_speed": _PT + "GenericSpeedSpace",
    "z_speed": _Z + "ZoomGenericSpeedSpace",
}
# 差分(RelativeMove)は pan / tilt を 1 回で端から端まで動かせるよう ±2 にする
PT_TRANSLATION_RANGE = (-2.0, 2.0)


def _num(v: float) -> str:
    v = round(v, 6)
    return "0" if v == 0 else f"{v:g}"


def _duration(seconds: float) -> str:
    return f"PT{_num(seconds)}S"


def _range(lo: float, hi: float) -> str:
    return f"<tt:Min>{_num(lo)}</tt:Min><tt:Max>{_num(hi)}</tt:Max>"


def _space2d(tag: str, uri: str, lo: float, hi: float) -> str:
    return f"<{tag}><tt:URI>{uri}</tt:URI><tt:XRange>{_range(lo, hi)}</tt:XRange><tt:YRange>{_range(lo, hi)}</tt:YRange></{tag}>"


def _space1d(tag: str, uri: str, lo: float, hi: float) -> str:
    return f"<{tag}><tt:URI>{uri}</tt:URI><tt:XRange>{_range(lo, hi)}</tt:XRange></{tag}>"


def spaces_xml() -> str:
    """tt:PTZSpaces の中身(要素の順序はスキーマの sequence どおり)。"""
    return (
        _space2d("tt:AbsolutePanTiltPositionSpace", SPACE["pt_position"], -1, 1)
        + _space1d("tt:AbsoluteZoomPositionSpace", SPACE["z_position"], 0, 1)
        + _space2d("tt:RelativePanTiltTranslationSpace", SPACE["pt_translation"], *PT_TRANSLATION_RANGE)
        + _space1d("tt:RelativeZoomTranslationSpace", SPACE["z_translation"], -1, 1)
        + _space2d("tt:ContinuousPanTiltVelocitySpace", SPACE["pt_velocity"], -1, 1)
        + _space1d("tt:ContinuousZoomVelocitySpace", SPACE["z_velocity"], -1, 1)
        + _space1d("tt:PanTiltSpeedSpace", SPACE["pt_speed"], 0, 1)
        + _space1d("tt:ZoomSpeedSpace", SPACE["z_speed"], 0, 1)
    )


def ptz_configuration_xml(tag: str) -> str:
    """tt:PTZConfiguration。Media / Media2 のプロファイルと PTZ サービスで共通。"""
    return (
        f'<{tag} token="{PTZ_CONFIG_TOKEN}">'
        "<tt:Name>DigitalPTZ</tt:Name><tt:UseCount>2</tt:UseCount>"
        f"<tt:NodeToken>{PTZ_NODE_TOKEN}</tt:NodeToken>"
        f"<tt:DefaultAbsolutePantTiltPositionSpace>{SPACE['pt_position']}</tt:DefaultAbsolutePantTiltPositionSpace>"
        f"<tt:DefaultAbsoluteZoomPositionSpace>{SPACE['z_position']}</tt:DefaultAbsoluteZoomPositionSpace>"
        f"<tt:DefaultRelativePanTiltTranslationSpace>{SPACE['pt_translation']}</tt:DefaultRelativePanTiltTranslationSpace>"
        f"<tt:DefaultRelativeZoomTranslationSpace>{SPACE['z_translation']}</tt:DefaultRelativeZoomTranslationSpace>"
        f"<tt:DefaultContinuousPanTiltVelocitySpace>{SPACE['pt_velocity']}</tt:DefaultContinuousPanTiltVelocitySpace>"
        f"<tt:DefaultContinuousZoomVelocitySpace>{SPACE['z_velocity']}</tt:DefaultContinuousZoomVelocitySpace>"
        "<tt:DefaultPTZSpeed>"
        f'<tt:PanTilt x="1" y="1" space="{SPACE["pt_speed"]}"/><tt:Zoom x="1" space="{SPACE["z_speed"]}"/>'
        "</tt:DefaultPTZSpeed>"
        f"<tt:DefaultPTZTimeout>{_duration(DEFAULT_TIMEOUT_S)}</tt:DefaultPTZTimeout>"
        f"<tt:PanTiltLimits>{_space2d('tt:Range', SPACE['pt_position'], -1, 1)}</tt:PanTiltLimits>"
        f"<tt:ZoomLimits>{_space1d('tt:Range', SPACE['z_position'], 0, 1)}</tt:ZoomLimits>"
        f"</{tag}>"
    )


def ptz_capabilities_xml() -> str:
    return (
        '<tptz:Capabilities EFlip="false" Reverse="false" GetCompatibleConfigurations="true" '
        'MoveStatus="true" StatusPosition="true"/>'
    )


def _fault(e: PtzError) -> SoapFault:
    if e.kind == "TooManyPresets":
        return SoapFault("Receiver", ["ter:Action", "ter:TooManyPresets"], str(e))
    return invalid_arg(e.kind, str(e))


def _float_attr(el: Element | None, name: str) -> float | None:
    if el is None or el.get(name) is None:
        return None
    try:
        return float(el.get(name))
    except ValueError as e:
        raise invalid_arg("InvalidArgs", f"{name} is not a number: {el.get(name)!r}") from e


def _check_space(el: Element | None, *allowed: str) -> None:
    space = el.get("space") if el is not None else None
    if space and space not in allowed:
        raise invalid_arg("SpaceNotSupported", f"Space not supported: {space}")


def _speed(req_el: Element | None) -> tuple[tuple[float, float] | None, float | None]:
    """tt:PTZSpeed → ((pan, tilt), zoom)。速度は 0〜1(Generic の速さ空間)。"""
    pt = child(req_el, "PanTilt")
    z = child(req_el, "Zoom")
    _check_space(pt, SPACE["pt_speed"])
    _check_space(z, SPACE["z_speed"])
    pt_speed = None
    if pt is not None:
        x, y = _float_attr(pt, "x"), _float_attr(pt, "y")
        pt_speed = (x if x is not None else 1.0, y if y is not None else 1.0)
    return pt_speed, _float_attr(z, "x")


class PtzServiceMixin:
    """CameraServices に混ぜる PTZ の操作。self.ptz(PtzController か None)と self._profile_from_token を使う。"""

    ptz: PtzController | None

    def _ptz(self) -> PtzController:
        if self.ptz is None:
            raise SoapFault("Receiver", ["ter:ActionNotSupported", "ter:PTZNotSupported"], "PTZ is not supported")
        return self.ptz

    def _ptz_profile(self, req) -> PtzController:
        ptz = self._ptz()
        self._profile_from_token(child_text(req.body, "ProfileToken"))
        return ptz

    @staticmethod
    def _ptz_run(fn, *args):
        try:
            return fn(*args)
        except PtzError as e:
            raise _fault(e) from e

    # ---- 情報 ----

    def ptz_GetServiceCapabilities(self, req, ctx):
        return f"<tptz:GetServiceCapabilitiesResponse>{ptz_capabilities_xml()}</tptz:GetServiceCapabilitiesResponse>"

    def _node_xml(self, tag: str) -> str:
        return (
            f'<{tag} token="{PTZ_NODE_TOKEN}" FixedHomePosition="false" GeoMove="false">'
            "<tt:Name>DigitalPTZ</tt:Name>"
            f"<tt:SupportedPTZSpaces>{spaces_xml()}</tt:SupportedPTZSpaces>"
            f"<tt:MaximumNumberOfPresets>{MAX_PRESETS}</tt:MaximumNumberOfPresets>"
            "<tt:HomeSupported>true</tt:HomeSupported>"
            f"</{tag}>"
        )

    def ptz_GetNodes(self, req, ctx):
        self._ptz()
        return f"<tptz:GetNodesResponse>{self._node_xml('tptz:PTZNode')}</tptz:GetNodesResponse>"

    def ptz_GetNode(self, req, ctx):
        self._ptz()
        token = child_text(req.body, "NodeToken")
        if token != PTZ_NODE_TOKEN:
            raise invalid_arg("NoEntity", f"PTZ node does not exist: {token}")
        return f"<tptz:GetNodeResponse>{self._node_xml('tptz:PTZNode')}</tptz:GetNodeResponse>"

    def ptz_GetConfigurations(self, req, ctx):
        self._ptz()
        return f"<tptz:GetConfigurationsResponse>{ptz_configuration_xml('tptz:PTZConfiguration')}</tptz:GetConfigurationsResponse>"

    def ptz_GetConfiguration(self, req, ctx):
        self._ptz()
        token = child_text(req.body, "PTZConfigurationToken")
        if token != PTZ_CONFIG_TOKEN:
            raise invalid_arg("NoConfig", f"PTZ configuration does not exist: {token}")
        return f"<tptz:GetConfigurationResponse>{ptz_configuration_xml('tptz:PTZConfiguration')}</tptz:GetConfigurationResponse>"

    def ptz_GetCompatibleConfigurations(self, req, ctx):
        self._ptz_profile(req)
        return (
            "<tptz:GetCompatibleConfigurationsResponse>"
            f"{ptz_configuration_xml('tptz:PTZConfiguration')}"
            "</tptz:GetCompatibleConfigurationsResponse>"
        )

    def ptz_GetConfigurationOptions(self, req, ctx):
        self._ptz()
        token = child_text(req.body, "ConfigurationToken")
        if token != PTZ_CONFIG_TOKEN:
            raise invalid_arg("NoConfig", f"PTZ configuration does not exist: {token}")
        return (
            "<tptz:GetConfigurationOptionsResponse><tptz:PTZConfigurationOptions>"
            f"<tt:Spaces>{spaces_xml()}</tt:Spaces>"
            f"<tt:PTZTimeout><tt:Min>{_duration(TIMEOUT_RANGE_S[0])}</tt:Min><tt:Max>{_duration(TIMEOUT_RANGE_S[1])}</tt:Max></tt:PTZTimeout>"
            "</tptz:PTZConfigurationOptions></tptz:GetConfigurationOptionsResponse>"
        )

    def ptz_GetStatus(self, req, ctx):
        ptz = self._ptz_profile(req)
        pan, tilt, zoom = ptz.position()
        pt_moving, z_moving = ptz.moving()
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        return (
            "<tptz:GetStatusResponse><tptz:PTZStatus>"
            f"<tt:Position>{self._vector_xml(pan, tilt, zoom)}</tt:Position>"
            "<tt:MoveStatus>"
            f"<tt:PanTilt>{'MOVING' if pt_moving else 'IDLE'}</tt:PanTilt>"
            f"<tt:Zoom>{'MOVING' if z_moving else 'IDLE'}</tt:Zoom>"
            "</tt:MoveStatus>"
            f"<tt:UtcTime>{now}</tt:UtcTime>"
            "</tptz:PTZStatus></tptz:GetStatusResponse>"
        )

    @staticmethod
    def _vector_xml(pan: float, tilt: float, zoom: float) -> str:
        return (
            f'<tt:PanTilt x="{_num(pan)}" y="{_num(tilt)}" space="{SPACE["pt_position"]}"/>'
            f'<tt:Zoom x="{_num(zoom)}" space="{SPACE["z_position"]}"/>'
        )

    # ---- 移動 ----

    def ptz_ContinuousMove(self, req, ctx):
        ptz = self._ptz_profile(req)
        vel = child(req.body, "Velocity")
        pt, z = child(vel, "PanTilt"), child(vel, "Zoom")
        _check_space(pt, SPACE["pt_velocity"])
        _check_space(z, SPACE["z_velocity"])
        timeout_text = child_text(req.body, "Timeout")
        timeout = None
        if timeout_text:
            try:
                timeout = parse_duration(timeout_text)
            except ValueError as e:
                raise invalid_arg("InvalidArgs", f"Invalid Timeout: {timeout_text}") from e
        ptz.continuous_move(_float_attr(pt, "x"), _float_attr(pt, "y"), _float_attr(z, "x"), timeout)
        return "<tptz:ContinuousMoveResponse/>"

    def ptz_AbsoluteMove(self, req, ctx):
        ptz = self._ptz_profile(req)
        pos = child(req.body, "Position")
        pt, z = child(pos, "PanTilt"), child(pos, "Zoom")
        _check_space(pt, SPACE["pt_position"])
        _check_space(z, SPACE["z_position"])
        ptz.absolute_move(_float_attr(pt, "x"), _float_attr(pt, "y"), _float_attr(z, "x"), *_speed(child(req.body, "Speed")))
        return "<tptz:AbsoluteMoveResponse/>"

    def ptz_RelativeMove(self, req, ctx):
        ptz = self._ptz_profile(req)
        tr = child(req.body, "Translation")
        pt, z = child(tr, "PanTilt"), child(tr, "Zoom")
        _check_space(pt, SPACE["pt_translation"])
        _check_space(z, SPACE["z_translation"])

        def clip(v, lo, hi):
            return None if v is None else max(lo, min(hi, v))

        ptz.relative_move(
            clip(_float_attr(pt, "x"), *PT_TRANSLATION_RANGE),
            clip(_float_attr(pt, "y"), *PT_TRANSLATION_RANGE),
            clip(_float_attr(z, "x"), -1.0, 1.0),
            *_speed(child(req.body, "Speed")),
        )
        return "<tptz:RelativeMoveResponse/>"

    def ptz_Stop(self, req, ctx):
        ptz = self._ptz_profile(req)
        pan_tilt = (child_text(req.body, "PanTilt") or "true").lower() == "true"
        zoom = (child_text(req.body, "Zoom") or "true").lower() == "true"
        ptz.stop(pan_tilt, zoom)
        return "<tptz:StopResponse/>"

    # ---- プリセットとホーム ----

    def ptz_GetPresets(self, req, ctx):
        ptz = self._ptz_profile(req)
        items = "".join(
            f"<tptz:Preset token={attr(p.token)}><tt:Name>{esc(p.name)}</tt:Name>"
            f"<tt:PTZPosition>{self._vector_xml(p.pan, p.tilt, p.zoom)}</tt:PTZPosition></tptz:Preset>"
            for p in ptz.presets.values()
        )
        return f"<tptz:GetPresetsResponse>{items}</tptz:GetPresetsResponse>"

    def ptz_SetPreset(self, req, ctx):
        ptz = self._ptz_profile(req)
        name = child_text(req.body, "PresetName")
        token = child_text(req.body, "PresetToken") or None
        token = self._ptz_run(ptz.set_preset, name, token)
        return f"<tptz:SetPresetResponse><tptz:PresetToken>{esc(token)}</tptz:PresetToken></tptz:SetPresetResponse>"

    def ptz_RemovePreset(self, req, ctx):
        ptz = self._ptz_profile(req)
        self._ptz_run(ptz.remove_preset, child_text(req.body, "PresetToken") or "")
        return "<tptz:RemovePresetResponse/>"

    def ptz_GotoPreset(self, req, ctx):
        ptz = self._ptz_profile(req)
        self._ptz_run(ptz.goto_preset, child_text(req.body, "PresetToken") or "", *_speed(child(req.body, "Speed")))
        return "<tptz:GotoPresetResponse/>"

    def ptz_GotoHomePosition(self, req, ctx):
        ptz = self._ptz_profile(req)
        ptz.goto_home(*_speed(child(req.body, "Speed")))
        return "<tptz:GotoHomePositionResponse/>"

    def ptz_SetHomePosition(self, req, ctx):
        ptz = self._ptz_profile(req)
        ptz.set_home()
        return "<tptz:SetHomePositionResponse/>"
