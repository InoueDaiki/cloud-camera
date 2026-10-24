"""SOAP の共通処理: 名前空間、エンベロープの組み立て、Fault、WS-Security UsernameToken の検証。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from xml.sax.saxutils import escape as _escape
from xml.sax.saxutils import quoteattr

NS = {
    "s12": "http://www.w3.org/2003/05/soap-envelope",
    "s11": "http://schemas.xmlsoap.org/soap/envelope/",
    "tt": "http://www.onvif.org/ver10/schema",
    "tds": "http://www.onvif.org/ver10/device/wsdl",
    "trt": "http://www.onvif.org/ver10/media/wsdl",
    "tr2": "http://www.onvif.org/ver20/media/wsdl",
    "tptz": "http://www.onvif.org/ver20/ptz/wsdl",
    "ter": "http://www.onvif.org/ver10/error",
    "wsse": "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd",
    "wsu": "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd",
}

PASSWORD_DIGEST = (
    "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
)
PASSWORD_TEXT = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordText"


def esc(v: object) -> str:
    """XML テキストのエスケープ。"""
    return _escape(str(v))


def attr(v: object) -> str:
    """属性値(引用符込み)。"""
    return quoteattr(str(v))


def b(v: bool) -> str:
    return "true" if v else "false"


class SoapFault(Exception):
    """ONVIF の SOAP Fault。code は Sender / Receiver、subcodes は ter:... の並び。"""

    def __init__(self, code: str, subcodes: list[str], reason: str, http_status: int | None = None):
        super().__init__(reason)
        self.code = code
        self.subcodes = subcodes
        self.reason = reason
        self.http_status = http_status or (400 if code == "Sender" else 500)


def not_authorized() -> SoapFault:
    return SoapFault("Sender", ["ter:NotAuthorized"], "Sender not Authorized")


def action_not_supported(op: str) -> SoapFault:
    return SoapFault("Receiver", ["ter:ActionNotSupported"], f"Optional Action Not Implemented: {op}")


def invalid_arg(sub: str, reason: str) -> SoapFault:
    return SoapFault("Sender", ["ter:InvalidArgVal", f"ter:{sub}"], reason)


@dataclass
class SoapRequest:
    soap_ns: str  # SOAP 1.2 か 1.1 の名前空間
    service_ns: str
    operation: str
    body: ET.Element  # Body 直下の要素(操作)
    security: ET.Element | None


def parse_request(data: bytes) -> SoapRequest:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise SoapFault("Sender", ["ter:WellFormed"], f"Malformed XML: {e}") from e
    soap_ns = root.tag[1:].split("}")[0] if root.tag.startswith("{") else ""
    if soap_ns not in (NS["s12"], NS["s11"]) or not root.tag.endswith("}Envelope"):
        raise SoapFault("Sender", ["ter:WellFormed"], "Not a SOAP envelope")
    header = root.find(f"{{{soap_ns}}}Header")
    body = root.find(f"{{{soap_ns}}}Body")
    if body is None or len(body) == 0:
        raise SoapFault("Sender", ["ter:WellFormed"], "Empty SOAP body")
    op = body[0]
    ns, _, local = op.tag[1:].partition("}")
    security = header.find(f"{{{NS['wsse']}}}Security") if header is not None else None
    return SoapRequest(soap_ns=soap_ns, service_ns=ns, operation=local, body=op, security=security)


# ---------------------------------------------------------------------------
# WS-Security UsernameToken
# ---------------------------------------------------------------------------


def _parse_created(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def verify_username_token(security: ET.Element | None, username: str, password: str, max_skew_s: int = 0) -> bool:
    """UsernameToken を検証する。PasswordDigest と PasswordText の両方に対応。"""
    if security is None:
        return False
    token = security.find(f"{{{NS['wsse']}}}UsernameToken")
    if token is None:
        return False
    user_el = token.find(f"{{{NS['wsse']}}}Username")
    pw_el = token.find(f"{{{NS['wsse']}}}Password")
    if user_el is None or pw_el is None or (user_el.text or "") != username:
        return False
    pw_type = pw_el.get("Type", PASSWORD_TEXT)
    given = (pw_el.text or "").strip()
    nonce_el = token.find(f"{{{NS['wsse']}}}Nonce")
    created_el = token.find(f"{{{NS['wsu']}}}Created")
    if created_el is None:  # 古いクライアントは wsse:Created を使うことがある
        created_el = token.find(f"{{{NS['wsse']}}}Created")
    created = (created_el.text or "").strip() if created_el is not None else ""

    if max_skew_s > 0 and created:
        t = _parse_created(created)
        if t is None or abs((datetime.now(UTC) - t).total_seconds()) > max_skew_s:
            return False

    if pw_type.endswith("#PasswordDigest"):
        if nonce_el is None:
            return False
        try:
            nonce = base64.b64decode((nonce_el.text or "").strip())
        except ValueError:
            return False
        expected = base64.b64encode(
            hashlib.sha1(nonce + created.encode() + password.encode()).digest()
        ).decode()
        return hmac.compare_digest(expected, given)
    if pw_type.endswith("#PasswordText"):
        return hmac.compare_digest(given.encode(), password.encode())
    return False


# ---------------------------------------------------------------------------
# レスポンスの組み立て
# ---------------------------------------------------------------------------

_NS_DECL = " ".join(
    f'xmlns:{p}="{u}"' for p, u in NS.items() if p not in ("s11", "s12", "wsse", "wsu")
)


def envelope(body_xml: str, soap_ns: str = NS["s12"]) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<env:Envelope xmlns:env="{soap_ns}" {_NS_DECL}>'
        f"<env:Body>{body_xml}</env:Body></env:Envelope>"
    ).encode()


def fault_envelope(fault: SoapFault, soap_ns: str = NS["s12"]) -> bytes:
    if soap_ns == NS["s11"]:
        code = "env:Client" if fault.code == "Sender" else "env:Server"
        body = (
            f"<env:Fault><faultcode>{code}</faultcode>"
            f"<faultstring>{esc(fault.reason)}</faultstring></env:Fault>"
        )
        return envelope(body, soap_ns)
    sub = ""
    for s in reversed(fault.subcodes):
        sub = f"<env:Subcode><env:Value>{s}</env:Value>{sub}</env:Subcode>"
    body = (
        f"<env:Fault><env:Code><env:Value>env:{fault.code}</env:Value>{sub}</env:Code>"
        f'<env:Reason><env:Text xml:lang="en">{esc(fault.reason)}</env:Text></env:Reason></env:Fault>'
    )
    return envelope(body, soap_ns)


def content_type(soap_ns: str) -> str:
    if soap_ns == NS["s11"]:
        return "text/xml; charset=utf-8"
    return "application/soap+xml; charset=utf-8"


# ---------------------------------------------------------------------------
# リクエスト要素の読み取り補助
# ---------------------------------------------------------------------------


def child_text(el: ET.Element | None, *path: str) -> str | None:
    """名前空間を無視して、ローカル名の並びで子孫要素のテキストを取る。"""
    cur = el
    for name in path:
        if cur is None:
            return None
        cur = next((c for c in cur if c.tag.rsplit("}", 1)[-1] == name), None)
    if cur is None:
        return None
    return (cur.text or "").strip()


def child(el: ET.Element | None, *path: str) -> ET.Element | None:
    cur = el
    for name in path:
        if cur is None:
            return None
        cur = next((c for c in cur if c.tag.rsplit("}", 1)[-1] == name), None)
    return cur


def children(el: ET.Element | None, name: str) -> list[ET.Element]:
    if el is None:
        return []
    return [c for c in el if c.tag.rsplit("}", 1)[-1] == name]
