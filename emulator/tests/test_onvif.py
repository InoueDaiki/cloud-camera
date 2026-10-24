"""ONVIF の結合テスト。実際のクライアント(WSDiscovery、onvif-zeep、zeep)でエミュレーターに接続する。"""

from __future__ import annotations

import base64
import hashlib
import os
import urllib.error
import urllib.request
from datetime import UTC, datetime
from urllib.parse import urlparse

import pytest
from conftest import Target
from onvif.exceptions import ONVIFError
from onvif_helpers import MEDIA2_NS, MEDIA2_WSDL, camera, camera_from_xaddr, discover, media2
from zeep import Client
from zeep.wsse.username import UsernameToken


@pytest.fixture(scope="module")
def discovered(target: Target) -> list[dict]:
    found = discover(target)
    # 他のカメラが同じネットワークにいても、エミュレーターのものだけを見る
    return [d for d in found if any("hardware/ONVIF-Emulator" in s for s in d["scopes"])]


def _device_xaddr(target: Target, n: int) -> str:
    return f"http://{target.host}:{target.device_port(n)}/onvif/device_service"


# ---------------------------------------------------------------------------
# Discovery → 認証 → Device 情報 → Media2 で H.265 → GetStreamUri
# ---------------------------------------------------------------------------


def test_discovery_finds_every_camera(target: Target, discovered: list[dict]):
    assert len(discovered) >= target.cameras, discovered
    eprs = {d["epr"] for d in discovered}
    assert len(eprs) == len(discovered), "EndpointReference はカメラごとに別"
    ports = {urlparse(d["xaddrs"][0]).port for d in discovered}
    assert {target.device_port(n) for n in range(1, target.cameras + 1)} <= ports
    for d in discovered:
        assert any(t.endswith("NetworkVideoTransmitter") for t in d["types"]), d["types"]
        assert d["xaddrs"][0].endswith("/onvif/device_service")


def test_full_flow_discovery_to_h265_stream_uri(target: Target, discovered: list[dict]):
    d = next(d for d in discovered if urlparse(d["xaddrs"][0]).port == target.device_port(1))
    cam = camera_from_xaddr(d["xaddrs"][0], target.username, target.password)

    info = cam.devicemgmt.GetDeviceInformation()
    assert info.Manufacturer and info.Model and info.SerialNumber

    m2 = media2(cam, target.username, target.password)
    profiles = m2.GetProfiles({"Type": ["All"]})
    h265 = [p for p in profiles if p.Configurations.VideoEncoder.Encoding == "H265"]
    assert len(h265) == 1
    enc = h265[0].Configurations.VideoEncoder
    assert enc.RateControl.ConstantBitRate is False  # VBR
    assert enc.GovLength == round(enc.RateControl.FrameRateLimit)  # キーフレーム間隔 1 秒(既定)

    uri = m2.GetStreamUri({"Protocol": "RTSP", "ProfileToken": h265[0].token})
    u = urlparse(uri)
    assert u.scheme == "rtsp" and u.port == target.rtsp_port and u.path == "/cam1/main"


def test_each_camera_has_own_identity(target: Target):
    seen = set()
    for n in range(1, target.cameras + 1):
        cam = camera(target.host, target.device_port(n), target.username, target.password)
        info = cam.devicemgmt.GetDeviceInformation()
        seen.add(info.SerialNumber)
        m2 = media2(cam, target.username, target.password)
        p = next(p for p in m2.GetProfiles({"Type": ["VideoEncoder"]}) if p.Configurations.VideoEncoder.Encoding == "H265")
        assert urlparse(m2.GetStreamUri({"Protocol": "RTSP", "ProfileToken": p.token})).path == f"/cam{n}/main"
    assert len(seen) == target.cameras


# ---------------------------------------------------------------------------
# 認証
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("digest", [True, False], ids=["PasswordDigest", "PasswordText"])
def test_valid_credentials(target: Target, digest: bool):
    cam = camera(target.host, target.onvif_port, target.username, target.password, digest=digest)
    assert cam.devicemgmt.GetHostname().Name


@pytest.mark.parametrize("digest", [True, False], ids=["PasswordDigest", "PasswordText"])
def test_wrong_password_is_rejected(target: Target, digest: bool):
    # GetCapabilities / GetSystemDateAndTime は認証なしで通るので、ONVIFCamera の生成は成功する
    cam = camera(target.host, target.onvif_port, target.username, "wrong-password", digest=digest)
    with pytest.raises(ONVIFError, match="Sender not Authorized"):
        cam.devicemgmt.GetDeviceInformation()
    with pytest.raises(ONVIFError, match="Sender not Authorized"):
        media2(cam, target.username, "wrong-password", digest=digest).GetProfiles({"Type": ["All"]})


def test_wrong_user_is_rejected(target: Target):
    cam = camera(target.host, target.onvif_port, "nobody", target.password)
    with pytest.raises(ONVIFError, match="Sender not Authorized"):
        cam.devicemgmt.GetScopes()


def _post(url: str, body: str) -> tuple[int, str]:
    env = (
        '<?xml version="1.0"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:tds="http://www.onvif.org/ver10/device/wsdl">'
        f"{body}</s:Envelope>"
    )
    req = urllib.request.Request(url, data=env.encode(), headers={"Content-Type": "application/soap+xml"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_fault_format_without_credentials(target: Target):
    status, text = _post(_device_xaddr(target, 1), "<s:Body><tds:GetDeviceInformation/></s:Body>")
    assert status == 400
    assert "<env:Value>env:Sender</env:Value>" in text
    assert "<env:Value>ter:NotAuthorized</env:Value>" in text


def test_preauth_get_system_date_and_time(target: Target):
    status, text = _post(_device_xaddr(target, 1), "<s:Body><tds:GetSystemDateAndTime/></s:Body>")
    assert status == 200
    assert f"<tt:Year>{datetime.now(UTC).year}</tt:Year>" in text


def test_raw_digest_token(target: Target):
    """PasswordDigest を自前で計算して送る(クライアント実装に依存しない確認)。"""
    nonce = os.urandom(16)
    created = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + target.password.encode()).digest()).decode()
    header = (
        '<s:Header><wsse:Security xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd" '
        'xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">'
        f"<wsse:UsernameToken><wsse:Username>{target.username}</wsse:Username>"
        '<wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">'
        f"{digest}</wsse:Password>"
        f'<wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-soap-message-security-1.0#Base64Binary">{base64.b64encode(nonce).decode()}</wsse:Nonce>'
        f"<wsu:Created>{created}</wsu:Created></wsse:UsernameToken></wsse:Security></s:Header>"
    )
    status, text = _post(_device_xaddr(target, 1), header + "<s:Body><tds:GetDeviceInformation/></s:Body>")
    assert status == 200, text
    assert "<tds:Model>" in text


def test_unknown_operation_is_action_not_supported(target: Target):
    cam = camera(target.host, target.onvif_port, target.username, target.password)
    with pytest.raises(ONVIFError, match="Not Implemented"):
        cam.devicemgmt.GetSystemLog({"LogType": "System"})


# ---------------------------------------------------------------------------
# Device / Media(Profile S)
# ---------------------------------------------------------------------------


def test_device_service_calls(target: Target):
    cam = camera(target.host, target.onvif_port, target.username, target.password)
    dev = cam.devicemgmt
    caps = dev.GetCapabilities({"Category": "All"})
    assert caps.Media.XAddr.endswith("/onvif/media_service")
    services = dev.GetServices({"IncludeCapability": True})
    assert {s.Namespace for s in services} >= {
        "http://www.onvif.org/ver10/device/wsdl",
        "http://www.onvif.org/ver10/media/wsdl",
        MEDIA2_NS,
    }
    scopes = [s.ScopeItem for s in dev.GetScopes()]
    assert any(s.startswith("onvif://www.onvif.org/name/") for s in scopes)
    nics = dev.GetNetworkInterfaces()
    assert nics[0].Info.HwAddress.startswith("02:")
    assert dev.GetNetworkProtocols()
    assert dev.GetDNS() is not None
    assert dev.GetNTP() is not None
    assert dev.GetSystemDateAndTime().UTCDateTime.Date.Year == datetime.now(UTC).year


def test_media1_profile_s(target: Target):
    cam = camera(target.host, target.onvif_port, target.username, target.password)
    media = cam.create_media_service()
    profiles = media.GetProfiles()
    tokens = [p.token for p in profiles]
    assert len(tokens) == 2
    # Media1 の Encoding に H265 はないので、H.264 の sub だけ VideoEncoderConfiguration を持つ
    sub = next(p for p in profiles if p.VideoEncoderConfiguration is not None)
    assert sub.VideoEncoderConfiguration.Encoding == "H264"
    assert media.GetProfile({"ProfileToken": sub.token}).token == sub.token
    uri = media.GetStreamUri(
        {"StreamSetup": {"Stream": "RTP-Unicast", "Transport": {"Protocol": "RTSP"}}, "ProfileToken": sub.token}
    )
    assert urlparse(uri.Uri).path == "/cam1/sub"
    snap = media.GetSnapshotUri({"ProfileToken": sub.token})
    assert snap.Uri.endswith("/onvif/snapshot.jpg")
    sources = media.GetVideoSources()
    assert sources[0].Resolution.Width > 0
    encs = media.GetVideoEncoderConfigurations()
    assert [e.Encoding for e in encs] == ["H264"]
    assert media.GetVideoSourceConfigurations()
    assert media.GetVideoEncoderConfigurationOptions({}).H264 is not None


def test_media2_strict_schema(target: Target):
    """onvif-zeep は strict=False で読むので、strict な zeep でもレスポンスがスキーマどおりか確かめる。"""
    xaddr = f"http://{target.host}:{target.onvif_port}/onvif/media2_service"
    client = Client(MEDIA2_WSDL, wsse=UsernameToken(target.username, target.password, use_digest=True))
    svc = client.create_service(f"{{{MEDIA2_NS}}}Media2Binding", xaddr)
    profiles = svc.GetProfiles(Type=["All"])
    encodings = sorted(p.Configurations.VideoEncoder.Encoding for p in profiles)
    assert encodings == ["H264", "H265"]
    # Type を省略すると構成は返らない(仕様どおり)
    assert all(p.Configurations is None for p in svc.GetProfiles())
    confs = svc.GetVideoEncoderConfigurations()
    assert {c.Encoding for c in confs} == {"H264", "H265"}
    opts = svc.GetVideoEncoderConfigurationOptions()
    assert {o.Encoding for o in opts} == {"H264", "H265"}
    assert svc.GetSnapshotUri(ProfileToken=profiles[0].token).endswith("/onvif/snapshot.jpg")
    assert svc.GetVideoSourceConfigurations()


def test_media2_set_rejects_resolution_change(target: Target):
    cam = camera(target.host, target.onvif_port, target.username, target.password)
    m2 = media2(cam, target.username, target.password)
    conf = next(c for c in m2.GetVideoEncoderConfigurations({}) if c.Encoding == "H265")
    conf.Resolution.Width = conf.Resolution.Width + 16
    with pytest.raises(ONVIFError, match="Resolution is fixed"):
        m2.SetVideoEncoderConfiguration({"Configuration": conf})
