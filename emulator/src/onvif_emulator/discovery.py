"""WS-Discovery(ONVIF の探索)。UDP 3702、マルチキャスト 239.255.255.250。

- 起動時に Hello、終了時に Bye をカメラごとにマルチキャストする
- Probe にはカメラごとに別の ProbeMatches(EndpointReference / XAddrs が別)をユニキャストで返す
- Resolve には EndpointReference が一致するカメラの ResolveMatches を返す
"""

from __future__ import annotations

import asyncio
import logging
import random
import socket
import struct
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable

from .config import CameraConfig, EmulatorConfig
from .soap import esc

log = logging.getLogger(__name__)

MCAST_GROUP = "239.255.255.250"
NS_S = "http://www.w3.org/2003/05/soap-envelope"
NS_A = "http://schemas.xmlsoap.org/ws/2004/08/addressing"
NS_D = "http://schemas.xmlsoap.org/ws/2005/04/discovery"
NS_DN = "http://www.onvif.org/ver10/network/wsdl"
NS_TDS = "http://www.onvif.org/ver10/device/wsdl"
ACTION = f"{NS_D}/"
TO_DISCOVERY = "urn:schemas-xmlsoap-org:ws:2005:04:discovery"
TO_ANONYMOUS = f"{NS_A}/role/anonymous"
TYPES = "dn:NetworkVideoTransmitter tds:Device"
# 応答できる Types(QName のローカル名)
SUPPORTED_TYPE_NAMES = {"NetworkVideoTransmitter", "Device"}

_ENV_OPEN = (
    f'<?xml version="1.0" encoding="UTF-8"?>\n<s:Envelope xmlns:s="{NS_S}" xmlns:a="{NS_A}" '
    f'xmlns:d="{NS_D}" xmlns:dn="{NS_DN}" xmlns:tds="{NS_TDS}">'
)


def local_ip_for(dest: str) -> str:
    """dest に届く経路で使われる自分の IP。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((dest, 9))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _scope_matches(requested: str, offered: list[str]) -> bool:
    """RFC 3986 のプレフィックス一致(既定の MatchBy)。"""
    r = requested.rstrip("/")
    for o in offered:
        o2 = o.rstrip("/")
        if o2 == r or o2.startswith(r + "/"):
            return True
    return False


class DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self, server: DiscoveryServer):
        self.server = server

    def error_received(self, exc):
        log.warning("WS-Discovery の送受信エラー: %s", exc)

    def datagram_received(self, data: bytes, addr):
        log.debug("WS-Discovery 受信 %d バイト from %s", len(data), addr)
        try:
            self.server.on_datagram(data, addr)
        except Exception:  # 不正なパケットで落ちないように
            log.exception("WS-Discovery パケットの処理に失敗 from %s", addr)


class DiscoveryServer:
    def __init__(self, cfg: EmulatorConfig, scopes: Callable[[CameraConfig], list[str]]):
        self.cfg = cfg
        self.scopes = scopes
        self.instance_id = random.randint(1, 2**31 - 1)
        self.message_number = 0
        self.transport: asyncio.DatagramTransport | None = None
        self.sock: socket.socket | None = None

    # ---- 起動・停止 ----

    async def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.bind(("", self.cfg.discovery_port))
        mreq = struct.pack("4s4s", socket.inet_aton(MCAST_GROUP), socket.inet_aton("0.0.0.0"))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        sock.setblocking(False)
        self.sock = sock
        loop = asyncio.get_running_loop()
        self.transport, _ = await loop.create_datagram_endpoint(lambda: DiscoveryProtocol(self), sock=sock)
        log.info("WS-Discovery を UDP %d で待ち受け(%s)", self.cfg.discovery_port, MCAST_GROUP)
        # Hello は MULTICAST_UDP_REPEAT に倣って少し間を空けて 2 回送る
        for _ in range(2):
            for cam in self.cfg.cameras:
                self._send(self._hello(cam, local_ip_for(MCAST_GROUP)), (MCAST_GROUP, 3702))
            await asyncio.sleep(random.uniform(0.05, 0.25))

    def stop(self) -> None:
        if self.transport is None:
            return
        for cam in self.cfg.cameras:
            try:
                self._send(self._bye(cam), (MCAST_GROUP, 3702))
            except OSError:
                pass
        self.transport.close()
        self.transport = None

    # ---- メッセージ ----

    def _host(self, peer_ip: str) -> str:
        return self.cfg.advertise_host or local_ip_for(peer_ip)

    def _xaddrs(self, cam: CameraConfig, host: str) -> str:
        return f"http://{host}:{cam.onvif_port}/onvif/device_service"

    def _header(self, action: str, to: str, relates_to: str | None = None) -> str:
        self.message_number += 1
        rel = f"<a:RelatesTo>{esc(relates_to)}</a:RelatesTo>" if relates_to else ""
        return (
            "<s:Header>"
            f'<a:Action s:mustUnderstand="1">{ACTION}{action}</a:Action>'
            f"<a:MessageID>urn:uuid:{uuid.uuid4()}</a:MessageID>{rel}"
            f'<a:To s:mustUnderstand="1">{to}</a:To>'
            f'<d:AppSequence InstanceId="{self.instance_id}" MessageNumber="{self.message_number}"/>'
            "</s:Header>"
        )

    def _endpoint(self, cam: CameraConfig, host: str) -> str:
        scopes = " ".join(self.scopes(cam))
        return (
            f"<a:EndpointReference><a:Address>urn:uuid:{cam.uuid}</a:Address></a:EndpointReference>"
            f"<d:Types>{TYPES}</d:Types><d:Scopes>{esc(scopes)}</d:Scopes>"
            f"<d:XAddrs>{esc(self._xaddrs(cam, host))}</d:XAddrs><d:MetadataVersion>1</d:MetadataVersion>"
        )

    def _hello(self, cam: CameraConfig, host: str) -> bytes:
        return (
            f"{_ENV_OPEN}{self._header('Hello', TO_DISCOVERY)}"
            f"<s:Body><d:Hello>{self._endpoint(cam, self.cfg.advertise_host or host)}</d:Hello></s:Body></s:Envelope>"
        ).encode()

    def _bye(self, cam: CameraConfig) -> bytes:
        return (
            f"{_ENV_OPEN}{self._header('Bye', TO_DISCOVERY)}"
            f"<s:Body><d:Bye><a:EndpointReference><a:Address>urn:uuid:{cam.uuid}</a:Address>"
            "</a:EndpointReference></d:Bye></s:Body></s:Envelope>"
        ).encode()

    def _probe_match(self, cam: CameraConfig, host: str, relates_to: str) -> bytes:
        return (
            f"{_ENV_OPEN}{self._header('ProbeMatches', TO_ANONYMOUS, relates_to)}"
            f"<s:Body><d:ProbeMatches><d:ProbeMatch>{self._endpoint(cam, host)}</d:ProbeMatch>"
            "</d:ProbeMatches></s:Body></s:Envelope>"
        ).encode()

    def _resolve_match(self, cam: CameraConfig, host: str, relates_to: str) -> bytes:
        return (
            f"{_ENV_OPEN}{self._header('ResolveMatches', TO_ANONYMOUS, relates_to)}"
            f"<s:Body><d:ResolveMatches><d:ResolveMatch>{self._endpoint(cam, host)}</d:ResolveMatch>"
            "</d:ResolveMatches></s:Body></s:Envelope>"
        ).encode()

    def _send(self, data: bytes, addr) -> None:
        if self.transport is not None:
            self.transport.sendto(data, addr)

    # ---- 受信 ----

    def on_datagram(self, data: bytes, addr) -> None:
        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            return
        body = root.find(f"{{{NS_S}}}Body")
        header = root.find(f"{{{NS_S}}}Header")
        if body is None or len(body) == 0:
            return
        op = body[0]
        msg_id_el = header.find(f"{{{NS_A}}}MessageID") if header is not None else None
        msg_id = (msg_id_el.text or "").strip() if msg_id_el is not None else ""
        if op.tag == f"{{{NS_D}}}Probe":
            self._on_probe(op, msg_id, addr)
        elif op.tag == f"{{{NS_D}}}Resolve":
            self._on_resolve(op, msg_id, addr)

    def _on_probe(self, probe: ET.Element, msg_id: str, addr) -> None:
        # Types は QName だが、ElementTree ではプレフィックスの宣言を引けないのでローカル名で照合する
        # (dn:NetworkVideoTransmitter、tds:Device、wsdp:Device などに応答する)
        types_el = probe.find(f"{{{NS_D}}}Types")
        req_types = (types_el.text or "").split() if types_el is not None else []
        if any(t.rpartition(":")[2] not in SUPPORTED_TYPE_NAMES for t in req_types):
            return
        scopes_el = probe.find(f"{{{NS_D}}}Scopes")
        req_scopes = (scopes_el.text or "").split() if scopes_el is not None else []
        host = self._host(addr[0])
        matched = [
            cam for cam in self.cfg.cameras if all(_scope_matches(s, self.scopes(cam)) for s in req_scopes)
        ]
        if not matched:
            return
        log.info("Probe from %s:%d → %d 台が応答", addr[0], addr[1], len(matched))
        loop = asyncio.get_running_loop()
        for cam in matched:
            # APP_MAX_DELAY(500ms)以内のランダムな遅延で応答する
            loop.call_later(random.uniform(0, 0.2), self._send, self._probe_match(cam, host, msg_id), addr)

    def _on_resolve(self, resolve: ET.Element, msg_id: str, addr) -> None:
        addr_el = resolve.find(f"{{{NS_A}}}EndpointReference/{{{NS_A}}}Address")
        if addr_el is None:
            return
        target = (addr_el.text or "").strip()
        for cam in self.cfg.cameras:
            if target == f"urn:uuid:{cam.uuid}":
                self._send(self._resolve_match(cam, self._host(addr[0]), msg_id), addr)

