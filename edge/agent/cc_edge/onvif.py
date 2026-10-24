"""最小限の ONVIF クライアント(標準ライブラリだけで実装)。

- WS-Discovery(UDP マルチキャスト 239.255.255.250:3702 への Probe)
- WS-Security UsernameToken(PasswordDigest)
- Device: GetSystemDateAndTime(時刻差の補正)、GetServices(Media2 / Media の XAddr)
- Media2: GetProfiles、GetStreamUri(H.265 のプロファイルを自動選択。設定で H.264 優先にもできる)
- Media2 がないカメラは Media(ver10)にフォールバックする
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import logging
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field

log = logging.getLogger(__name__)

NS = {
    "s": "http://www.w3.org/2003/05/soap-envelope",
    "a": "http://schemas.xmlsoap.org/ws/2004/08/addressing",
    "d": "http://schemas.xmlsoap.org/ws/2005/04/discovery",
    "dn": "http://www.onvif.org/ver10/network/wsdl",
    "tds": "http://www.onvif.org/ver10/device/wsdl",
    "trt": "http://www.onvif.org/ver10/media/wsdl",
    "tr2": "http://www.onvif.org/ver20/media/wsdl",
    "tt": "http://www.onvif.org/ver10/schema",
    "wsse": "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd",
    "wsu": "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd",
}
MEDIA2_NS = NS["tr2"]
MEDIA1_NS = NS["trt"]
_PW_DIGEST = (
    "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
)
_NONCE_B64 = (
    "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary"
)


class OnvifError(Exception):
    pass


@dataclass
class Profile:
    token: str
    name: str = ""
    encoding: str = ""  # H265 / H264 / JPEG ...(大文字に正規化)
    width: int = 0
    height: int = 0
    framerate: float | None = None
    bitrate_kbps: int | None = None
    gov_length: int | None = None


@dataclass
class StreamInfo:
    rtsp_url: str
    profile_token: str
    codec: str
    width: int
    height: int
    media_version: int  # 2 = Media2, 1 = Media
    profiles: list[Profile] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["resolution"] = f"{self.width}x{self.height}"
        return d


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_local(elem: ET.Element, name: str) -> ET.Element | None:
    for e in elem.iter():
        if _local(e.tag) == name:
            return e
    return None


def _findall_local(elem: ET.Element, name: str) -> list[ET.Element]:
    return [e for e in elem.iter() if _local(e.tag) == name]


def _text(elem: ET.Element | None, name: str, default: str = "") -> str:
    if elem is None:
        return default
    e = _find_local(elem, name)
    return (e.text or "").strip() if e is not None and e.text else default


def password_digest(password: str, nonce: bytes, created: str) -> str:
    """WS-Security の PasswordDigest = Base64(SHA1(nonce + created + password))"""
    return base64.b64encode(hashlib.sha1(nonce + created.encode() + password.encode()).digest()).decode()


def security_header(username: str, password: str, now: dt.datetime | None = None,
                    nonce: bytes | None = None) -> str:
    now = now or dt.datetime.now(dt.UTC)
    created = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
    nonce = nonce if nonce is not None else os.urandom(16)
    digest = password_digest(password, nonce, created)
    return (
        f'<wsse:Security s:mustUnderstand="1" xmlns:wsse="{NS["wsse"]}" xmlns:wsu="{NS["wsu"]}">'
        "<wsse:UsernameToken>"
        f"<wsse:Username>{_xml_escape(username)}</wsse:Username>"
        f'<wsse:Password Type="{_PW_DIGEST}">{digest}</wsse:Password>'
        f'<wsse:Nonce EncodingType="{_NONCE_B64}">{base64.b64encode(nonce).decode()}</wsse:Nonce>'
        f"<wsu:Created>{created}</wsu:Created>"
        "</wsse:UsernameToken></wsse:Security>"
    )


def _xml_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def envelope(body: str, header: str = "") -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<s:Envelope xmlns:s="{NS["s"]}" xmlns:tds="{NS["tds"]}" xmlns:trt="{NS["trt"]}" '
        f'xmlns:tr2="{NS["tr2"]}" xmlns:tt="{NS["tt"]}">'
        f"<s:Header>{header}</s:Header><s:Body>{body}</s:Body></s:Envelope>"
    ).encode()


def _rewrite_host(url: str, base: str) -> str:
    """カメラが 127.0.0.1 や 0.0.0.0 など到達できないホストを返したとき、デバイス URL のホストに置き換える。"""
    u = urllib.parse.urlsplit(url)
    b = urllib.parse.urlsplit(base)
    if u.hostname in ("127.0.0.1", "localhost", "0.0.0.0", "::1", None) and b.hostname:
        netloc = b.hostname
        if u.port:
            netloc = f"{netloc}:{u.port}"
        if u.username:
            cred = u.username + (f":{u.password}" if u.password else "")
            netloc = f"{cred}@{netloc}"
        return urllib.parse.urlunsplit((u.scheme, netloc, u.path, u.query, u.fragment))
    return url


class OnvifClient:
    def __init__(self, device_url: str, username: str = "", password: str = "", timeout: float = 5.0):
        self.device_url = device_url
        self.username = username
        self.password = password
        self.timeout = timeout
        self.clock_offset = dt.timedelta(0)  # カメラの時刻 − 自分の時刻
        self.services: dict[str, str] = {}

    # --- 低レベル --------------------------------------------------------
    def call(self, url: str, body: str, auth: bool = True, action: str = "") -> ET.Element:
        header = ""
        if auth and self.username:
            header = security_header(self.username, self.password,
                                     now=dt.datetime.now(dt.UTC) + self.clock_offset)
        data = envelope(body, header)
        ctype = "application/soap+xml; charset=utf-8"
        if action:
            ctype += f'; action="{action}"'
        req = urllib.request.Request(url, data=data, headers={"Content-Type": ctype}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raw = e.read()
            reason = _fault_reason(raw) or str(e)
            raise OnvifError(f"{url}: HTTP {e.code}: {reason}") from None
        except (urllib.error.URLError, OSError) as e:
            raise OnvifError(f"{url}: {e}") from None
        root = ET.fromstring(raw)
        body_el = root.find("s:Body", NS)
        if body_el is None:
            raise OnvifError(f"{url}: SOAP Body がない")
        fault = _find_local(body_el, "Fault")
        if fault is not None:
            raise OnvifError(f"{url}: SOAP Fault: {_fault_reason(raw)}")
        return body_el

    # --- Device ----------------------------------------------------------
    def sync_clock(self) -> None:
        try:
            body = self.call(self.device_url, "<tds:GetSystemDateAndTime/>", auth=False)
        except OnvifError as e:
            log.debug("GetSystemDateAndTime に失敗(時刻補正なし): %s", e)
            return
        utc = _find_local(body, "UTCDateTime")
        if utc is None:
            return
        try:
            cam = dt.datetime(
                int(_text(utc, "Year")), int(_text(utc, "Month")), int(_text(utc, "Day")),
                int(_text(utc, "Hour")), int(_text(utc, "Minute")), int(_text(utc, "Second")),
                tzinfo=dt.UTC,
            )
        except ValueError:
            return
        self.clock_offset = cam - dt.datetime.now(dt.UTC)
        if abs(self.clock_offset.total_seconds()) > 5:
            log.warning("カメラの時計が %.1f 秒ずれている(認証の Created を補正する)",
                        self.clock_offset.total_seconds())

    def get_services(self) -> dict[str, str]:
        try:
            body = self.call(self.device_url,
                             "<tds:GetServices><tds:IncludeCapability>false</tds:IncludeCapability></tds:GetServices>")
            for svc in _findall_local(body, "Service"):
                ns = _text(svc, "Namespace")
                xaddr = _text(svc, "XAddr")
                if ns and xaddr:
                    self.services[ns] = _rewrite_host(xaddr, self.device_url)
        except OnvifError as e:
            log.info("GetServices に失敗、GetCapabilities を試す: %s", e)
            body = self.call(self.device_url,
                             "<tds:GetCapabilities><tds:Category>All</tds:Category></tds:GetCapabilities>")
            media = _find_local(body, "Media")
            if media is not None and _text(media, "XAddr"):
                self.services[MEDIA1_NS] = _rewrite_host(_text(media, "XAddr"), self.device_url)
        return self.services

    # --- Media2 / Media --------------------------------------------------
    def get_profiles(self) -> tuple[int, list[Profile]]:
        if not self.services:
            self.get_services()
        if MEDIA2_NS in self.services:
            body = self.call(self.services[MEDIA2_NS],
                             "<tr2:GetProfiles><tr2:Type>All</tr2:Type></tr2:GetProfiles>")
            return 2, parse_media2_profiles(body)
        if MEDIA1_NS in self.services:
            body = self.call(self.services[MEDIA1_NS], "<trt:GetProfiles/>")
            return 1, parse_media1_profiles(body)
        raise OnvifError("Media / Media2 サービスが見つからない")

    def get_stream_uri(self, version: int, token: str) -> str:
        if version == 2:
            body = self.call(
                self.services[MEDIA2_NS],
                f"<tr2:GetStreamUri><tr2:Protocol>RTSP</tr2:Protocol>"
                f"<tr2:ProfileToken>{_xml_escape(token)}</tr2:ProfileToken></tr2:GetStreamUri>",
            )
        else:
            body = self.call(
                self.services[MEDIA1_NS],
                "<trt:GetStreamUri><trt:StreamSetup>"
                "<tt:Stream>RTP-Unicast</tt:Stream><tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport>"
                f"</trt:StreamSetup><trt:ProfileToken>{_xml_escape(token)}</trt:ProfileToken></trt:GetStreamUri>",
            )
        uri = _text(body, "Uri")
        if not uri:
            raise OnvifError("GetStreamUri の応答に Uri がない")
        return _rewrite_host(uri, self.device_url)

    def resolve(self, profile_token: str | None = None, prefer_codec: str = "H265") -> StreamInfo:
        """プロファイルを選び(指定がなければ prefer_codec、既定 H.265 で最大解像度)、RTSP URL を返す。"""
        self.sync_clock()
        version, profiles = self.get_profiles()
        prof = select_profile(profiles, profile_token, prefer_codec)
        uri = self.get_stream_uri(version, prof.token)
        return StreamInfo(rtsp_url=uri, profile_token=prof.token, codec=prof.encoding,
                          width=prof.width, height=prof.height, media_version=version,
                          profiles=profiles)


def _fault_reason(raw: bytes) -> str:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return raw[:200].decode(errors="replace")
    reason = _find_local(root, "Reason")
    sub = _find_local(root, "Subcode")
    parts = [_text(reason, "Text")] if reason is not None else []
    if sub is not None:
        parts.append(_text(sub, "Value"))
    return " / ".join(p for p in parts if p)


def _norm_encoding(enc: str) -> str:
    e = enc.strip().upper().replace(".", "")
    return {"HEVC": "H265", "AVC": "H264"}.get(e, e)


def parse_media2_profiles(body: ET.Element) -> list[Profile]:
    out = []
    for p in _findall_local(body, "Profiles"):
        token = p.get("token", "")
        enc = _find_local(p, "VideoEncoder")
        prof = Profile(token=token, name=_text(p, "Name"))
        if enc is not None:
            prof.encoding = _norm_encoding(_text(enc, "Encoding"))
            res = _find_local(enc, "Resolution")
            prof.width = int(_text(res, "Width", "0") or 0)
            prof.height = int(_text(res, "Height", "0") or 0)
            rc = _find_local(enc, "RateControl")
            if rc is not None:
                fr = _text(rc, "FrameRateLimit")
                br = _text(rc, "BitrateLimit")
                prof.framerate = float(fr) if fr else None
                prof.bitrate_kbps = int(float(br)) if br else None
            gov = enc.get("GovLength") or _text(enc, "GovLength")
            prof.gov_length = int(gov) if gov else None
        out.append(prof)
    return out


def parse_media1_profiles(body: ET.Element) -> list[Profile]:
    out = []
    for p in _findall_local(body, "Profiles"):
        prof = Profile(token=p.get("token", ""), name=_text(p, "Name"))
        enc = _find_local(p, "VideoEncoderConfiguration")
        if enc is not None:
            prof.encoding = _norm_encoding(_text(enc, "Encoding"))
            res = _find_local(enc, "Resolution")
            prof.width = int(_text(res, "Width", "0") or 0)
            prof.height = int(_text(res, "Height", "0") or 0)
        out.append(prof)
    return out


CODECS = ("H265", "H264")


def normalize_codec(v: str | None, default: str = "H265") -> str:
    """"h265" / "H.265" / "HEVC" → "H265"、"h264" / "H.264" / "AVC" → "H264"。空なら default。"""
    if not v:
        return default
    c = _norm_encoding(v).replace("-", "")
    if c not in CODECS:
        raise ValueError(f"対応していないコーデック {v!r}(H265 か H264)")
    return c


def select_profile(profiles: list[Profile], token: str | None = None, prefer: str = "H265") -> Profile:
    """プロファイルを選ぶ。token の指定があればそれ。なければ prefer のコーデック(既定 H.265)で最大解像度、
    なければもう一方のコーデックで最大解像度。"""
    if not profiles:
        raise OnvifError("プロファイルがない")
    if token:
        for p in profiles:
            if p.token == token:
                return p
        raise OnvifError(f"プロファイル {token} が見つからない")
    prefer = normalize_codec(prefer)
    for codec in (prefer, *(c for c in CODECS if c != prefer)):
        cands = [p for p in profiles if p.encoding == codec]
        if cands:
            if codec != prefer:
                # 再エンコードしないので、そのまま送る。reported.codec で分かる
                log.warning("%s のプロファイルがない。%s を使う", prefer, codec)
            return max(cands, key=lambda p: p.width * p.height)
    raise OnvifError("H.265 / H.264 のプロファイルがない: "
                     + ", ".join(f"{p.token}={p.encoding}" for p in profiles))


# --- WS-Discovery --------------------------------------------------------
WSD_ADDR = ("239.255.255.250", 3702)


def probe_message(message_id: str | None = None) -> bytes:
    mid = message_id or f"uuid:{uuid.uuid4()}"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<s:Envelope xmlns:s="{NS["s"]}" xmlns:a="{NS["a"]}" xmlns:d="{NS["d"]}" xmlns:dn="{NS["dn"]}">'
        "<s:Header>"
        f"<a:MessageID>{mid}</a:MessageID>"
        "<a:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</a:To>"
        "<a:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</a:Action>"
        "</s:Header><s:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></s:Body>"
        "</s:Envelope>"
    ).encode()


def parse_probe_matches(raw: bytes, sender: str = "") -> list[dict]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    out = []
    for m in _findall_local(root, "ProbeMatch"):
        epr = _find_local(m, "EndpointReference")
        out.append({
            "urn": _text(epr, "Address") if epr is not None else "",
            "types": _text(m, "Types").split(),
            "scopes": _text(m, "Scopes").split(),
            "xaddrs": _text(m, "XAddrs").split(),
            "from": sender,
        })
    return out


def discover(timeout: float = 3.0, unicast_hosts: list[str] | None = None) -> list[dict]:
    """WS-Discovery の Probe を送り、timeout 秒のあいだ ProbeMatch を集める。

    unicast_hosts を渡すと、マルチキャストが届かない環境向けに各ホストの 3702 にも直接 Probe を送る。
    """
    msg_id = f"uuid:{uuid.uuid4()}"
    msg = probe_message(msg_id)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.bind(("", 0))
    found: dict[str, dict] = {}
    try:
        sock.sendto(msg, WSD_ADDR)
        for h in unicast_hosts or []:
            try:
                sock.sendto(msg, (h, WSD_ADDR[1]))
            except OSError as e:
                log.debug("unicast probe %s: %s", h, e)
        deadline = time.monotonic() + timeout
        while (remain := deadline - time.monotonic()) > 0:
            sock.settimeout(remain)
            try:
                data, addr = sock.recvfrom(65535)
            except TimeoutError:
                break
            for m in parse_probe_matches(data, addr[0]):
                key = m["urn"] or ",".join(m["xaddrs"])
                found.setdefault(key, m)
    finally:
        sock.close()
    return list(found.values())
