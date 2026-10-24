"""テスト用の ONVIF クライアント補助(onvif-zeep と zeep)。"""

from __future__ import annotations

import socket
import time
import uuid
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

from conftest import WSDL_DIR, Target
from onvif import ONVIFCamera, ONVIFService
from wsdiscovery.discovery import ThreadedWSDiscovery

MEDIA2_NS = "http://www.onvif.org/ver20/media/wsdl"
MEDIA2_WSDL = str(WSDL_DIR / "ver20/media/wsdl/media.wsdl")


PROBE = """<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
<s:Header><a:Action s:mustUnderstand="1">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</a:Action>
<a:MessageID>urn:uuid:{mid}</a:MessageID><a:To s:mustUnderstand="1">urn:schemas-xmlsoap-org:ws:2005:04:discovery</a:To>
</s:Header><s:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></s:Body></s:Envelope>"""


def _unicast_probe(host: str, port: int, timeout: float) -> list[dict]:
    """指定ホストに直接 Probe を送る(WSDiscovery パッケージはユニキャスト応答を読めないため自前)。"""
    a = "{http://schemas.xmlsoap.org/ws/2004/08/addressing}"
    d = "{http://schemas.xmlsoap.org/ws/2005/04/discovery}"
    mid = uuid.uuid4()
    out: dict[str, dict] = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(0.2)
        s.sendto(PROBE.format(mid=mid).encode(), (host, port))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, _ = s.recvfrom(65535)
            except TimeoutError:
                continue
            root = ET.fromstring(data)
            if root.findtext(f".//{a}RelatesTo") != f"urn:uuid:{mid}":
                continue
            for m in root.iter(f"{d}ProbeMatch"):
                epr = m.findtext(f"{a}EndpointReference/{a}Address")
                out[epr] = {
                    "epr": epr,
                    "xaddrs": (m.findtext(f"{d}XAddrs") or "").split(),
                    "scopes": (m.findtext(f"{d}Scopes") or "").split(),
                    "types": (m.findtext(f"{d}Types") or "").split(),
                }
    return list(out.values())


def discover(target: Target, timeout: float = 3.0) -> list[dict]:
    """WS-Discovery で探し、[{epr, xaddrs, scopes, types}] を返す。

    multicast: WSDiscovery パッケージ(python-ws-discovery)で 239.255.255.250:3702 に Probe
    unicast  : target.host:discovery_port に直接 Probe
    """
    if target.discovery_mode == "unicast":
        return _unicast_probe(target.host, target.discovery_port, timeout)
    wsd = ThreadedWSDiscovery()
    wsd.start()
    try:
        services = wsd.searchServices(timeout=timeout)
        out = []
        for s in services:
            out.append(
                {
                    "epr": s.getEPR(),
                    "xaddrs": list(s.getXAddrs()),
                    "scopes": [str(x) for x in s.getScopes()],
                    "types": [str(x) for x in s.getTypes()],
                }
            )
        return out
    finally:
        wsd.stop()


def camera(host: str, port: int, user: str, password: str, *, digest: bool = True) -> ONVIFCamera:
    """onvif-zeep の ONVIFCamera。生成時に GetCapabilities を呼ぶ。"""
    return ONVIFCamera(host, port, user, password, encrypt=digest, no_cache=True, adjust_time=True)


def camera_from_xaddr(xaddr: str, user: str, password: str, **kw) -> ONVIFCamera:
    u = urlparse(xaddr)
    return camera(u.hostname, u.port or 80, user, password, **kw)


def media2(cam: ONVIFCamera, user: str, password: str, *, digest: bool = True) -> ONVIFService:
    """GetServices で Media2 の XAddr を引き、公式の Media2 WSDL で onvif-zeep のサービスを作る。"""
    services = cam.devicemgmt.GetServices({"IncludeCapability": False})
    xaddr = next(s.XAddr for s in services if s.Namespace == MEDIA2_NS)
    return ONVIFService(
        xaddr,
        user,
        password,
        MEDIA2_WSDL,
        encrypt=digest,
        no_cache=True,
        dt_diff=cam.dt_diff,
        binding_name=f"{{{MEDIA2_NS}}}Media2Binding",
    )


def wait_until(pred, timeout: float, interval: float = 0.5):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = pred()
        if last:
            return last
        time.sleep(interval)
    return last
