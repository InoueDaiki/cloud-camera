"""ONVIF の Device / Media(ver10)/ Media2(ver20)/ PTZ(ver20)サービス。

1 台のカメラにつき 1 つの CameraServices を作る。レスポンスは手書きの XML テンプレートで返す。
要素の順序は ONVIF のスキーマ(onvif.xsd、各 WSDL)の sequence どおりにしている(zeep は順序に厳しい)。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .config import CameraConfig, EmulatorConfig, StreamSettings
from .ptz import PtzController
from .ptz_service import PtzServiceMixin, ptz_capabilities_xml, ptz_configuration_xml
from .soap import (
    NS,
    SoapFault,
    SoapRequest,
    action_not_supported,
    child,
    child_text,
    children,
    esc,
    invalid_arg,
    not_authorized,
    verify_username_token,
)

log = logging.getLogger(__name__)

# 認証なしで呼べる操作(ONVIF Core 仕様の PRE_AUTH クラス)
PRE_AUTH = {
    (NS["tds"], "GetSystemDateAndTime"),
    (NS["tds"], "GetCapabilities"),
    (NS["tds"], "GetServices"),
    (NS["tds"], "GetServiceCapabilities"),
    (NS["tds"], "GetWsdlUrl"),
    (NS["tds"], "GetHostname"),
    (NS["tds"], "GetEndpointReference"),
}

VIDEO_SOURCE_TOKEN = "VideoSource_1"
VIDEO_SOURCE_CONFIG_TOKEN = "VideoSourceConfig_1"
STREAMS = ("main", "sub")
PROFILE_TOKEN = {"main": "Profile_Main", "sub": "Profile_Sub"}
ENCODER_TOKEN = {"main": "VideoEncoder_Main", "sub": "VideoEncoder_Sub"}
QUALITY_RANGE = (0, 100)
BITRATE_RANGE = (64, 16384)
GOV_RANGE = (1, 300)
FPS_SUPPORTED = (30, 25, 20, 15, 10, 5, 1)


@dataclass
class RequestContext:
    host: str  # クライアントが接続してきたホスト名(XAddrs、URI に使う)
    local_ip: str  # このコンテナの IP(GetNetworkInterfaces 用)


class CameraServices(PtzServiceMixin):
    def __init__(
        self,
        cfg: EmulatorConfig,
        cam: CameraConfig,
        on_encoder_change: Callable[[CameraConfig, str], Any] | None = None,
        ptz: PtzController | None = None,
    ):
        self.cfg = cfg
        self.cam = cam
        self.on_encoder_change = on_encoder_change
        self.ptz = ptz  # None なら PTZ なし(GetServices などに載せない)
        self._handlers: dict[tuple[str, str], Callable[[SoapRequest, RequestContext], str]] = {}
        for ns_key, prefix in (("tds", "dev_"), ("trt", "m1_"), ("tr2", "m2_"), ("tptz", "ptz_")):
            for name in dir(self):
                if name.startswith(prefix):
                    self._handlers[(NS[ns_key], name[len(prefix) :])] = getattr(self, name)

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------

    def handle(self, req: SoapRequest, ctx: RequestContext) -> str:
        key = (req.service_ns, req.operation)
        if key not in PRE_AUTH:
            if not verify_username_token(
                req.security, self.cam.username, self.cam.password, self.cfg.auth_max_skew_s
            ):
                raise not_authorized()
        handler = self._handlers.get(key)
        if handler is None:
            raise action_not_supported(req.operation)
        return handler(req, ctx)

    # ------------------------------------------------------------------
    # 共通部品
    # ------------------------------------------------------------------

    def _base(self, ctx: RequestContext) -> str:
        return f"http://{ctx.host}:{self.cam.onvif_port}"

    def _xaddr(self, ctx: RequestContext, service: str) -> str:
        return f"{self._base(ctx)}/onvif/{service}"

    def _stream(self, which: str) -> StreamSettings:
        return self.cam.main if which == "main" else self.cam.sub

    def _rtsp_uri(self, ctx: RequestContext, which: str) -> str:
        return f"rtsp://{ctx.host}:{self.cfg.rtsp_port}/{self.cam.path_prefix}/{which}"

    def _snapshot_uri(self, ctx: RequestContext) -> str:
        return f"{self._base(ctx)}/onvif/snapshot.jpg"

    def _profile_from_token(self, token: str | None) -> str:
        for which, t in PROFILE_TOKEN.items():
            if t == token:
                return which
        raise invalid_arg("NoProfile", f"Profile token does not exist: {token}")

    def _encoder_from_token(self, token: str | None) -> str:
        for which, t in ENCODER_TOKEN.items():
            if t == token:
                return which
        raise invalid_arg("NoConfig", f"Configuration token does not exist: {token}")

    def _scopes(self) -> list[tuple[str, str]]:
        name = self.cam.name.replace(" ", "%20")
        return [
            ("Fixed", "onvif://www.onvif.org/type/video_encoder"),
            ("Fixed", "onvif://www.onvif.org/type/Network_Video_Transmitter"),
            ("Fixed", "onvif://www.onvif.org/Profile/Streaming"),
            ("Fixed", "onvif://www.onvif.org/Profile/T"),
            ("Fixed", f"onvif://www.onvif.org/hardware/{self.cam.model}"),
            ("Configurable", f"onvif://www.onvif.org/name/{name}"),
            ("Configurable", f"onvif://www.onvif.org/location/{self.cam.location}"),
        ]

    def scope_uris(self) -> list[str]:
        return [s for _, s in self._scopes()]

    # ---- tt: 型の XML ----

    def _video_source_config_xml(self, tag: str) -> str:
        m = self.cam.main
        return (
            f'<{tag} token="{VIDEO_SOURCE_CONFIG_TOKEN}">'
            f"<tt:Name>VideoSourceConfig</tt:Name><tt:UseCount>2</tt:UseCount>"
            f"<tt:SourceToken>{VIDEO_SOURCE_TOKEN}</tt:SourceToken>"
            f'<tt:Bounds x="0" y="0" width="{m.width}" height="{m.height}"/>'
            f"</{tag}>"
        )

    def _encoder1_xml(self, tag: str, which: str) -> str:
        """Media1 の VideoEncoderConfiguration。Media1 の Encoding は JPEG/MPEG4/H264 のみなので sub(H.264)専用。"""
        s = self._stream(which)
        return (
            f'<{tag} token="{ENCODER_TOKEN[which]}">'
            f"<tt:Name>{which}</tt:Name><tt:UseCount>1</tt:UseCount>"
            f"<tt:Encoding>H264</tt:Encoding>"
            f"<tt:Resolution><tt:Width>{s.width}</tt:Width><tt:Height>{s.height}</tt:Height></tt:Resolution>"
            f"<tt:Quality>{s.quality:g}</tt:Quality>"
            f"<tt:RateControl><tt:FrameRateLimit>{s.fps}</tt:FrameRateLimit>"
            f"<tt:EncodingInterval>1</tt:EncodingInterval>"
            f"<tt:BitrateLimit>{s.bitrate_kbps}</tt:BitrateLimit></tt:RateControl>"
            f"<tt:H264><tt:GovLength>{s.gov_length}</tt:GovLength><tt:H264Profile>High</tt:H264Profile></tt:H264>"
            f"<tt:Multicast><tt:Address><tt:Type>IPv4</tt:Type><tt:IPv4Address>0.0.0.0</tt:IPv4Address></tt:Address>"
            f"<tt:Port>0</tt:Port><tt:TTL>0</tt:TTL><tt:AutoStart>false</tt:AutoStart></tt:Multicast>"
            f"<tt:SessionTimeout>PT60S</tt:SessionTimeout>"
            f"</{tag}>"
        )

    def _encoder2_xml(self, tag: str, which: str) -> str:
        """Media2 の VideoEncoder2Configuration。main は H265、sub は H264。"""
        s = self._stream(which)
        profile = "Main" if s.codec == "H265" else "High"
        return (
            f'<{tag} token="{ENCODER_TOKEN[which]}" GovLength="{s.gov_length}" Profile="{profile}">'
            f"<tt:Name>{which}</tt:Name><tt:UseCount>1</tt:UseCount>"
            f"<tt:Encoding>{s.codec}</tt:Encoding>"
            f"<tt:Resolution><tt:Width>{s.width}</tt:Width><tt:Height>{s.height}</tt:Height></tt:Resolution>"
            f'<tt:RateControl ConstantBitRate="false"><tt:FrameRateLimit>{s.fps}</tt:FrameRateLimit>'
            f"<tt:BitrateLimit>{s.bitrate_kbps}</tt:BitrateLimit></tt:RateControl>"
            f"<tt:Quality>{s.quality:g}</tt:Quality>"
            f"</{tag}>"
        )

    def _profile1_xml(self, which: str) -> str:
        enc = self._encoder1_xml("tt:VideoEncoderConfiguration", which) if which == "sub" else ""
        # main と sub は同じ切り出し窓なので、同じ PTZConfiguration を付ける
        ptz = ptz_configuration_xml("tt:PTZConfiguration") if self.ptz else ""
        return (
            f'<trt:Profiles token="{PROFILE_TOKEN[which]}" fixed="true">'
            f"<tt:Name>{which}</tt:Name>"
            f"{self._video_source_config_xml('tt:VideoSourceConfiguration')}"
            f"{enc}{ptz}</trt:Profiles>"
        )

    def _profile2_xml(self, which: str, types: set[str]) -> str:
        conf = ""
        if types:
            parts = []
            if "All" in types or "VideoSource" in types:
                parts.append(self._video_source_config_xml("tr2:VideoSource"))
            if "All" in types or "VideoEncoder" in types:
                parts.append(self._encoder2_xml("tr2:VideoEncoder", which))
            if self.ptz and ("All" in types or "PTZ" in types):
                parts.append(ptz_configuration_xml("tr2:PTZ"))
            conf = f"<tr2:Configurations>{''.join(parts)}</tr2:Configurations>"
        return (
            f'<tr2:Profiles token="{PROFILE_TOKEN[which]}" fixed="true">'
            f"<tr2:Name>{which}</tr2:Name>{conf}</tr2:Profiles>"
        )

    def _apply_encoder(self, which: str, conf_el, *, media2: bool) -> None:
        """SetVideoEncoderConfiguration の共通処理。解像度とコーデックは固定(変えようとしたら Fault)。"""
        s = self._stream(which)
        width = child_text(conf_el, "Resolution", "Width")
        height = child_text(conf_el, "Resolution", "Height")
        if width and height and (int(width), int(height)) != (s.width, s.height):
            raise invalid_arg("ConfigModify", "Resolution is fixed on this device")
        encoding = child_text(conf_el, "Encoding")
        if encoding and encoding.upper() != s.codec:
            raise invalid_arg("ConfigModify", f"Encoding is fixed to {s.codec} for this configuration")

        new = StreamSettings(**vars(s))
        bitrate = child_text(conf_el, "RateControl", "BitrateLimit")
        if bitrate:
            new.bitrate_kbps = int(float(bitrate))
        fps = child_text(conf_el, "RateControl", "FrameRateLimit")
        if fps:
            new.fps = max(1, round(float(fps)))
        gov = conf_el.get("GovLength") if media2 else child_text(conf_el, "H264", "GovLength")
        if gov:
            new.gov_length = int(gov)
        quality = child_text(conf_el, "Quality")
        if quality:
            new.quality = float(quality)

        if not (BITRATE_RANGE[0] <= new.bitrate_kbps <= BITRATE_RANGE[1]):
            raise invalid_arg("ConfigModify", f"BitrateLimit out of range {BITRATE_RANGE}")
        if not (GOV_RANGE[0] <= new.gov_length <= GOV_RANGE[1]):
            raise invalid_arg("ConfigModify", f"GovLength out of range {GOV_RANGE}")
        if not (1 <= new.fps <= 30):
            raise invalid_arg("ConfigModify", "FrameRateLimit out of range (1-30)")
        if not (QUALITY_RANGE[0] <= new.quality <= QUALITY_RANGE[1]):
            raise invalid_arg("ConfigModify", f"Quality out of range {QUALITY_RANGE}")

        if vars(new) == vars(s):
            return
        log.info("%s: %s のエンコーダー設定を変更 %s -> %s", self.cam.name, which, vars(s), vars(new))
        if which == "main":
            self.cam.main = new
        else:
            self.cam.sub = new
        if self.on_encoder_change:
            self.on_encoder_change(self.cam, which)

    # ==================================================================
    # Device(tds)
    # ==================================================================

    def dev_GetDeviceInformation(self, req, ctx):
        c = self.cam
        return (
            "<tds:GetDeviceInformationResponse>"
            f"<tds:Manufacturer>{esc(c.manufacturer)}</tds:Manufacturer>"
            f"<tds:Model>{esc(c.model)}</tds:Model>"
            f"<tds:FirmwareVersion>{esc(c.firmware)}</tds:FirmwareVersion>"
            f"<tds:SerialNumber>{esc(c.serial)}</tds:SerialNumber>"
            f"<tds:HardwareId>{esc(c.hardware_id)}</tds:HardwareId>"
            "</tds:GetDeviceInformationResponse>"
        )

    def dev_GetSystemDateAndTime(self, req, ctx):
        now = datetime.now(UTC)
        dt = (
            f"<tt:Time><tt:Hour>{now.hour}</tt:Hour><tt:Minute>{now.minute}</tt:Minute>"
            f"<tt:Second>{now.second}</tt:Second></tt:Time>"
            f"<tt:Date><tt:Year>{now.year}</tt:Year><tt:Month>{now.month}</tt:Month>"
            f"<tt:Day>{now.day}</tt:Day></tt:Date>"
        )
        return (
            "<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime>"
            "<tt:DateTimeType>NTP</tt:DateTimeType><tt:DaylightSavings>false</tt:DaylightSavings>"
            "<tt:TimeZone><tt:TZ>UTC0</tt:TZ></tt:TimeZone>"
            f"<tt:UTCDateTime>{dt}</tt:UTCDateTime><tt:LocalDateTime>{dt}</tt:LocalDateTime>"
            "</tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>"
        )

    def dev_SetSystemDateAndTime(self, req, ctx):
        # 時刻はホストに従うので受け付けるだけ
        return "<tds:SetSystemDateAndTimeResponse/>"

    def dev_GetCapabilities(self, req, ctx):
        cats = {(c.text or "").strip() for c in children(req.body, "Category")} or {"All"}
        want = lambda name: "All" in cats or name in cats  # noqa: E731
        parts = []
        if want("Device"):
            parts.append(
                "<tt:Device>"
                f"<tt:XAddr>{self._xaddr(ctx, 'device_service')}</tt:XAddr>"
                "<tt:Network><tt:IPFilter>false</tt:IPFilter><tt:ZeroConfiguration>false</tt:ZeroConfiguration>"
                "<tt:IPVersion6>false</tt:IPVersion6><tt:DynDNS>false</tt:DynDNS></tt:Network>"
                "<tt:System><tt:DiscoveryResolve>true</tt:DiscoveryResolve><tt:DiscoveryBye>true</tt:DiscoveryBye>"
                "<tt:RemoteDiscovery>false</tt:RemoteDiscovery><tt:SystemBackup>false</tt:SystemBackup>"
                "<tt:SystemLogging>false</tt:SystemLogging><tt:FirmwareUpgrade>false</tt:FirmwareUpgrade>"
                "<tt:SupportedVersions><tt:Major>2</tt:Major><tt:Minor>60</tt:Minor></tt:SupportedVersions>"
                "</tt:System>"
                "<tt:IO><tt:InputConnectors>0</tt:InputConnectors><tt:RelayOutputs>0</tt:RelayOutputs></tt:IO>"
                "<tt:Security><tt:TLS1.1>false</tt:TLS1.1><tt:TLS1.2>false</tt:TLS1.2>"
                "<tt:OnboardKeyGeneration>false</tt:OnboardKeyGeneration>"
                "<tt:AccessPolicyConfig>false</tt:AccessPolicyConfig><tt:X.509Token>false</tt:X.509Token>"
                "<tt:SAMLToken>false</tt:SAMLToken><tt:KerberosToken>false</tt:KerberosToken>"
                "<tt:RELToken>false</tt:RELToken></tt:Security>"
                "</tt:Device>"
            )
        if want("Media"):
            parts.append(
                "<tt:Media>"
                f"<tt:XAddr>{self._xaddr(ctx, 'media_service')}</tt:XAddr>"
                "<tt:StreamingCapabilities><tt:RTPMulticast>false</tt:RTPMulticast>"
                "<tt:RTP_TCP>true</tt:RTP_TCP><tt:RTP_RTSP_TCP>true</tt:RTP_RTSP_TCP>"
                "</tt:StreamingCapabilities></tt:Media>"
            )
        if want("PTZ") and self.ptz:
            parts.append(f"<tt:PTZ><tt:XAddr>{self._xaddr(ctx, 'ptz_service')}</tt:XAddr></tt:PTZ>")
        if not parts:
            raise SoapFault("Receiver", ["ter:ActionNotSupported", "ter:NoSuchService"], "No such service")
        return f"<tds:GetCapabilitiesResponse><tds:Capabilities>{''.join(parts)}</tds:Capabilities></tds:GetCapabilitiesResponse>"

    def _device_caps(self) -> str:
        return (
            "<tds:Capabilities>"
            '<tds:Network IPFilter="false" ZeroConfiguration="false" IPVersion6="false" DynDNS="false" '
            'Dot11Configuration="false" HostnameFromDHCP="false" NTP="0"/>'
            '<tds:Security TLS1.0="false" TLS1.1="false" TLS1.2="false" OnboardKeyGeneration="false" '
            'AccessPolicyConfig="false" DefaultAccessPolicy="false" Dot1X="false" RemoteUserHandling="false" '
            'X.509Token="false" SAMLToken="false" KerberosToken="false" UsernameToken="true" '
            'HttpDigest="false" RELToken="false"/>'
            '<tds:System DiscoveryResolve="true" DiscoveryBye="true" RemoteDiscovery="false" '
            'SystemBackup="false" SystemLogging="false" FirmwareUpgrade="false" HttpFirmwareUpgrade="false" '
            'HttpSystemBackup="false" HttpSystemLogging="false" HttpSupportInformation="false"/>'
            "</tds:Capabilities>"
        )

    def _media1_caps(self) -> str:
        return (
            '<trt:Capabilities SnapshotUri="true" Rotation="false" VideoSourceMode="false" OSD="false">'
            '<trt:ProfileCapabilities MaximumNumberOfProfiles="2"/>'
            '<trt:StreamingCapabilities RTPMulticast="false" RTP_TCP="true" RTP_RTSP_TCP="true" '
            'NonAggregateControl="false" NoRTSPStreaming="false"/>'
            "</trt:Capabilities>"
        )

    def _media2_caps(self) -> str:
        supported = "VideoSource VideoEncoder PTZ" if self.ptz else "VideoSource VideoEncoder"
        return (
            '<tr2:Capabilities SnapshotUri="true" Rotation="false" VideoSourceMode="false" OSD="false" '
            'TemporaryOSDText="false" Mask="false" SourceMask="false">'
            f'<tr2:ProfileCapabilities MaximumNumberOfProfiles="2" ConfigurationsSupported="{supported}"/>'
            '<tr2:StreamingCapabilities RTSPStreaming="true" RTPMulticast="false" RTP_RTSP_TCP="true" '
            'NonAggregateControl="false" AutoStartMulticast="false"/>'
            "</tr2:Capabilities>"
        )

    def dev_GetServices(self, req, ctx):
        include = (child_text(req.body, "IncludeCapability") or "false").lower() == "true"
        services = [
            (NS["tds"], "device_service", self._device_caps(), (2, 60)),
            (NS["trt"], "media_service", self._media1_caps(), (2, 60)),
            (NS["tr2"], "media2_service", self._media2_caps(), (17, 6)),
        ]
        if self.ptz:
            services.append((NS["tptz"], "ptz_service", ptz_capabilities_xml(), (2, 60)))
        out = []
        for ns, path, caps, (major, minor) in services:
            cap = f"<tds:Capabilities>{caps}</tds:Capabilities>" if include else ""
            out.append(
                f"<tds:Service><tds:Namespace>{ns}</tds:Namespace>"
                f"<tds:XAddr>{self._xaddr(ctx, path)}</tds:XAddr>{cap}"
                f"<tds:Version><tt:Major>{major}</tt:Major><tt:Minor>{minor}</tt:Minor></tds:Version>"
                "</tds:Service>"
            )
        return f"<tds:GetServicesResponse>{''.join(out)}</tds:GetServicesResponse>"

    def dev_GetServiceCapabilities(self, req, ctx):
        return f"<tds:GetServiceCapabilitiesResponse>{self._device_caps()}</tds:GetServiceCapabilitiesResponse>"

    def dev_GetWsdlUrl(self, req, ctx):
        return "<tds:GetWsdlUrlResponse><tds:WsdlUrl>https://www.onvif.org/profiles/specifications/</tds:WsdlUrl></tds:GetWsdlUrlResponse>"

    def dev_GetEndpointReference(self, req, ctx):
        return f"<tds:GetEndpointReferenceResponse><tds:GUID>urn:uuid:{self.cam.uuid}</tds:GUID></tds:GetEndpointReferenceResponse>"

    def dev_GetScopes(self, req, ctx):
        items = "".join(
            f"<tds:Scopes><tt:ScopeDef>{d}</tt:ScopeDef><tt:ScopeItem>{esc(s)}</tt:ScopeItem></tds:Scopes>"
            for d, s in self._scopes()
        )
        return f"<tds:GetScopesResponse>{items}</tds:GetScopesResponse>"

    def dev_GetHostname(self, req, ctx):
        return (
            "<tds:GetHostnameResponse><tds:HostnameInformation><tt:FromDHCP>false</tt:FromDHCP>"
            f"<tt:Name>{esc(self.cam.name)}</tt:Name></tds:HostnameInformation></tds:GetHostnameResponse>"
        )

    def dev_GetNetworkInterfaces(self, req, ctx):
        return (
            '<tds:GetNetworkInterfacesResponse><tds:NetworkInterfaces token="eth0">'
            "<tt:Enabled>true</tt:Enabled>"
            f"<tt:Info><tt:Name>eth0</tt:Name><tt:HwAddress>{self.cam.mac}</tt:HwAddress><tt:MTU>1500</tt:MTU></tt:Info>"
            "<tt:IPv4><tt:Enabled>true</tt:Enabled><tt:Config>"
            f"<tt:Manual><tt:Address>{esc(ctx.local_ip)}</tt:Address><tt:PrefixLength>24</tt:PrefixLength></tt:Manual>"
            "<tt:DHCP>false</tt:DHCP></tt:Config></tt:IPv4>"
            "</tds:NetworkInterfaces></tds:GetNetworkInterfacesResponse>"
        )

    def dev_GetNetworkProtocols(self, req, ctx):
        return (
            "<tds:GetNetworkProtocolsResponse>"
            f"<tds:NetworkProtocols><tt:Name>HTTP</tt:Name><tt:Enabled>true</tt:Enabled><tt:Port>{self.cam.onvif_port}</tt:Port></tds:NetworkProtocols>"
            f"<tds:NetworkProtocols><tt:Name>RTSP</tt:Name><tt:Enabled>true</tt:Enabled><tt:Port>{self.cfg.rtsp_port}</tt:Port></tds:NetworkProtocols>"
            "</tds:GetNetworkProtocolsResponse>"
        )

    def dev_GetNetworkDefaultGateway(self, req, ctx):
        return "<tds:GetNetworkDefaultGatewayResponse><tds:NetworkGateway/></tds:GetNetworkDefaultGatewayResponse>"

    def dev_GetDNS(self, req, ctx):
        return "<tds:GetDNSResponse><tds:DNSInformation><tt:FromDHCP>true</tt:FromDHCP></tds:DNSInformation></tds:GetDNSResponse>"

    def dev_GetNTP(self, req, ctx):
        return "<tds:GetNTPResponse><tds:NTPInformation><tt:FromDHCP>true</tt:FromDHCP></tds:NTPInformation></tds:GetNTPResponse>"

    def dev_GetDiscoveryMode(self, req, ctx):
        return "<tds:GetDiscoveryModeResponse><tds:DiscoveryMode>Discoverable</tds:DiscoveryMode></tds:GetDiscoveryModeResponse>"

    def dev_GetUsers(self, req, ctx):
        return (
            f"<tds:GetUsersResponse><tds:User><tt:Username>{esc(self.cam.username)}</tt:Username>"
            "<tt:UserLevel>Administrator</tt:UserLevel></tds:User></tds:GetUsersResponse>"
        )

    def dev_GetRelayOutputs(self, req, ctx):
        return "<tds:GetRelayOutputsResponse/>"

    # ==================================================================
    # Media(trt, Profile S)
    # ==================================================================

    def m1_GetServiceCapabilities(self, req, ctx):
        return f"<trt:GetServiceCapabilitiesResponse>{self._media1_caps()}</trt:GetServiceCapabilitiesResponse>"

    def m1_GetProfiles(self, req, ctx):
        return f"<trt:GetProfilesResponse>{''.join(self._profile1_xml(w) for w in STREAMS)}</trt:GetProfilesResponse>"

    def m1_GetProfile(self, req, ctx):
        which = self._profile_from_token(child_text(req.body, "ProfileToken"))
        xml = self._profile1_xml(which).replace("<trt:Profiles ", "<trt:Profile ").replace("</trt:Profiles>", "</trt:Profile>")
        return f"<trt:GetProfileResponse>{xml}</trt:GetProfileResponse>"

    def m1_GetStreamUri(self, req, ctx):
        which = self._profile_from_token(child_text(req.body, "ProfileToken"))
        return (
            "<trt:GetStreamUriResponse><trt:MediaUri>"
            f"<tt:Uri>{esc(self._rtsp_uri(ctx, which))}</tt:Uri>"
            "<tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>"
            "<tt:InvalidAfterReboot>false</tt:InvalidAfterReboot><tt:Timeout>PT0S</tt:Timeout>"
            "</trt:MediaUri></trt:GetStreamUriResponse>"
        )

    def m1_GetSnapshotUri(self, req, ctx):
        self._profile_from_token(child_text(req.body, "ProfileToken"))
        return (
            "<trt:GetSnapshotUriResponse><trt:MediaUri>"
            f"<tt:Uri>{esc(self._snapshot_uri(ctx))}</tt:Uri>"
            "<tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>"
            "<tt:InvalidAfterReboot>false</tt:InvalidAfterReboot><tt:Timeout>PT0S</tt:Timeout>"
            "</trt:MediaUri></trt:GetSnapshotUriResponse>"
        )

    def m1_GetVideoSources(self, req, ctx):
        m = self.cam.main
        return (
            f'<trt:GetVideoSourcesResponse><trt:VideoSources token="{VIDEO_SOURCE_TOKEN}">'
            f"<tt:Framerate>{m.fps}</tt:Framerate>"
            f"<tt:Resolution><tt:Width>{m.width}</tt:Width><tt:Height>{m.height}</tt:Height></tt:Resolution>"
            "</trt:VideoSources></trt:GetVideoSourcesResponse>"
        )

    def m1_GetVideoSourceConfigurations(self, req, ctx):
        return (
            "<trt:GetVideoSourceConfigurationsResponse>"
            f"{self._video_source_config_xml('trt:Configurations')}"
            "</trt:GetVideoSourceConfigurationsResponse>"
        )

    def m1_GetVideoSourceConfiguration(self, req, ctx):
        token = child_text(req.body, "ConfigurationToken")
        if token != VIDEO_SOURCE_CONFIG_TOKEN:
            raise invalid_arg("NoConfig", f"Configuration token does not exist: {token}")
        return (
            "<trt:GetVideoSourceConfigurationResponse>"
            f"{self._video_source_config_xml('trt:Configuration')}"
            "</trt:GetVideoSourceConfigurationResponse>"
        )

    def m1_GetVideoEncoderConfigurations(self, req, ctx):
        return (
            "<trt:GetVideoEncoderConfigurationsResponse>"
            f"{self._encoder1_xml('trt:Configurations', 'sub')}"
            "</trt:GetVideoEncoderConfigurationsResponse>"
        )

    def m1_GetVideoEncoderConfiguration(self, req, ctx):
        which = self._encoder_from_token(child_text(req.body, "ConfigurationToken"))
        if which != "sub":
            raise invalid_arg("NoConfig", "H.265 configuration is only available via Media2")
        return (
            "<trt:GetVideoEncoderConfigurationResponse>"
            f"{self._encoder1_xml('trt:Configuration', 'sub')}"
            "</trt:GetVideoEncoderConfigurationResponse>"
        )

    def m1_GetCompatibleVideoEncoderConfigurations(self, req, ctx):
        which = self._profile_from_token(child_text(req.body, "ProfileToken"))
        conf = self._encoder1_xml("trt:Configurations", "sub") if which == "sub" else ""
        return f"<trt:GetCompatibleVideoEncoderConfigurationsResponse>{conf}</trt:GetCompatibleVideoEncoderConfigurationsResponse>"

    def m1_GetVideoEncoderConfigurationOptions(self, req, ctx):
        s = self.cam.sub
        return (
            "<trt:GetVideoEncoderConfigurationOptionsResponse><trt:Options>"
            f"<tt:QualityRange><tt:Min>{QUALITY_RANGE[0]}</tt:Min><tt:Max>{QUALITY_RANGE[1]}</tt:Max></tt:QualityRange>"
            "<tt:H264>"
            f"<tt:ResolutionsAvailable><tt:Width>{s.width}</tt:Width><tt:Height>{s.height}</tt:Height></tt:ResolutionsAvailable>"
            f"<tt:GovLengthRange><tt:Min>{GOV_RANGE[0]}</tt:Min><tt:Max>{GOV_RANGE[1]}</tt:Max></tt:GovLengthRange>"
            "<tt:FrameRateRange><tt:Min>1</tt:Min><tt:Max>30</tt:Max></tt:FrameRateRange>"
            "<tt:EncodingIntervalRange><tt:Min>1</tt:Min><tt:Max>1</tt:Max></tt:EncodingIntervalRange>"
            "<tt:H264ProfilesSupported>High</tt:H264ProfilesSupported>"
            "</tt:H264></trt:Options></trt:GetVideoEncoderConfigurationOptionsResponse>"
        )

    def m1_SetVideoEncoderConfiguration(self, req, ctx):
        conf = child(req.body, "Configuration")
        which = self._encoder_from_token(conf.get("token") if conf is not None else None)
        if which != "sub":
            raise invalid_arg("ConfigModify", "H.265 configuration can only be changed via Media2")
        self._apply_encoder(which, conf, media2=False)
        return "<trt:SetVideoEncoderConfigurationResponse/>"

    def m1_SetSynchronizationPoint(self, req, ctx):
        self._profile_from_token(child_text(req.body, "ProfileToken"))
        return "<trt:SetSynchronizationPointResponse/>"

    def m1_GetAudioSources(self, req, ctx):
        return "<trt:GetAudioSourcesResponse/>"

    def m1_GetAudioSourceConfigurations(self, req, ctx):
        return "<trt:GetAudioSourceConfigurationsResponse/>"

    def m1_GetAudioEncoderConfigurations(self, req, ctx):
        return "<trt:GetAudioEncoderConfigurationsResponse/>"

    def m1_GetMetadataConfigurations(self, req, ctx):
        return "<trt:GetMetadataConfigurationsResponse/>"

    # ==================================================================
    # Media2(tr2)
    # ==================================================================

    def m2_GetServiceCapabilities(self, req, ctx):
        return f"<tr2:GetServiceCapabilitiesResponse>{self._media2_caps()}</tr2:GetServiceCapabilitiesResponse>"

    def m2_GetProfiles(self, req, ctx):
        token = child_text(req.body, "Token")
        # 仕様どおり、Type を指定しないと構成(Configurations)は返さない。全部欲しいときは Type=All
        types = {(t.text or "").strip() for t in children(req.body, "Type")}
        targets = [self._profile_from_token(token)] if token else list(STREAMS)
        return f"<tr2:GetProfilesResponse>{''.join(self._profile2_xml(w, types) for w in targets)}</tr2:GetProfilesResponse>"

    def m2_GetStreamUri(self, req, ctx):
        which = self._profile_from_token(child_text(req.body, "ProfileToken"))
        protocol = child_text(req.body, "Protocol") or "RTSP"
        if protocol not in ("RTSP", "RtspUnicast", "RtspOverHttp"):
            raise invalid_arg("InvalidStreamSetup", f"Unsupported protocol: {protocol}")
        return f"<tr2:GetStreamUriResponse><tr2:Uri>{esc(self._rtsp_uri(ctx, which))}</tr2:Uri></tr2:GetStreamUriResponse>"

    def m2_GetSnapshotUri(self, req, ctx):
        self._profile_from_token(child_text(req.body, "ProfileToken"))
        return f"<tr2:GetSnapshotUriResponse><tr2:Uri>{esc(self._snapshot_uri(ctx))}</tr2:Uri></tr2:GetSnapshotUriResponse>"

    def _m2_targets(self, req) -> list[str]:
        ctoken = child_text(req.body, "ConfigurationToken")
        ptoken = child_text(req.body, "ProfileToken")
        if ctoken:
            return [self._encoder_from_token(ctoken)]
        if ptoken:
            return [self._profile_from_token(ptoken)]
        return list(STREAMS)

    def m2_GetVideoEncoderConfigurations(self, req, ctx):
        confs = "".join(self._encoder2_xml("tr2:Configurations", w) for w in self._m2_targets(req))
        return f"<tr2:GetVideoEncoderConfigurationsResponse>{confs}</tr2:GetVideoEncoderConfigurationsResponse>"

    def m2_GetVideoSourceConfigurations(self, req, ctx):
        return (
            "<tr2:GetVideoSourceConfigurationsResponse>"
            f"{self._video_source_config_xml('tr2:Configurations')}"
            "</tr2:GetVideoSourceConfigurationsResponse>"
        )

    def m2_GetVideoEncoderConfigurationOptions(self, req, ctx):
        out = []
        for w in self._m2_targets(req):
            s = self._stream(w)
            profile = "Main" if s.codec == "H265" else "High"
            fps_list = " ".join(str(f) for f in FPS_SUPPORTED)
            out.append(
                f'<tr2:Options GovLengthRange="{GOV_RANGE[0]} {GOV_RANGE[1]}" FrameRatesSupported="{fps_list}" '
                f'ProfilesSupported="{profile}" ConstantBitRateSupported="false" GuaranteedFrameRateSupported="false">'
                f"<tt:Encoding>{s.codec}</tt:Encoding>"
                f"<tt:QualityRange><tt:Min>{QUALITY_RANGE[0]}</tt:Min><tt:Max>{QUALITY_RANGE[1]}</tt:Max></tt:QualityRange>"
                f"<tt:ResolutionsAvailable><tt:Width>{s.width}</tt:Width><tt:Height>{s.height}</tt:Height></tt:ResolutionsAvailable>"
                f"<tt:BitrateRange><tt:Min>{BITRATE_RANGE[0]}</tt:Min><tt:Max>{BITRATE_RANGE[1]}</tt:Max></tt:BitrateRange>"
                "</tr2:Options>"
            )
        return f"<tr2:GetVideoEncoderConfigurationOptionsResponse>{''.join(out)}</tr2:GetVideoEncoderConfigurationOptionsResponse>"

    def m2_SetVideoEncoderConfiguration(self, req, ctx):
        conf = child(req.body, "Configuration")
        which = self._encoder_from_token(conf.get("token") if conf is not None else None)
        self._apply_encoder(which, conf, media2=True)
        return "<tr2:SetVideoEncoderConfigurationResponse/>"

    def m2_SetSynchronizationPoint(self, req, ctx):
        self._profile_from_token(child_text(req.body, "ProfileToken"))
        return "<tr2:SetSynchronizationPointResponse/>"

    def m2_GetAudioSourceConfigurations(self, req, ctx):
        return "<tr2:GetAudioSourceConfigurationsResponse/>"

    def m2_GetAudioEncoderConfigurations(self, req, ctx):
        return "<tr2:GetAudioEncoderConfigurationsResponse/>"

    def m2_GetMetadataConfigurations(self, req, ctx):
        return "<tr2:GetMetadataConfigurationsResponse/>"

