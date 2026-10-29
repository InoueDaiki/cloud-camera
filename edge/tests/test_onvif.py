"""ONVIF クライアントの単体テスト。SOAP を返す小さな HTTP サーバーを立てて検証する。"""

import base64
import datetime as dt
import hashlib
import threading
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from cc_edge import onvif

S = "http://www.w3.org/2003/05/soap-envelope"


def _env(body: str) -> bytes:
    return (f'<s:Envelope xmlns:s="{S}" xmlns:tds="http://www.onvif.org/ver10/device/wsdl" '
            'xmlns:tr2="http://www.onvif.org/ver20/media/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema">'
            f"<s:Body>{body}</s:Body></s:Envelope>").encode()


PROFILES = """
<tr2:GetProfilesResponse>
 <tr2:Profiles token="sub" fixed="true"><tr2:Name>sub</tr2:Name><tr2:Configurations>
  <tr2:VideoEncoder token="ve2" GovLength="25"><tt:Encoding>H264</tt:Encoding>
   <tt:Resolution><tt:Width>640</tt:Width><tt:Height>360</tt:Height></tt:Resolution></tr2:VideoEncoder>
 </tr2:Configurations></tr2:Profiles>
 <tr2:Profiles token="main" fixed="true"><tr2:Name>main</tr2:Name><tr2:Configurations>
  <tr2:VideoEncoder token="ve1" GovLength="25"><tt:Encoding>{main}</tt:Encoding>
   <tt:Resolution><tt:Width>1920</tt:Width><tt:Height>1080</tt:Height></tt:Resolution>
   <tt:RateControl ConstantBitRate="{cbr}"><tt:FrameRateLimit>25</tt:FrameRateLimit><tt:BitrateLimit>{bitrate}</tt:BitrateLimit></tt:RateControl>
   <tt:Quality>{quality}</tt:Quality>
  </tr2:VideoEncoder></tr2:Configurations></tr2:Profiles>
</tr2:GetProfilesResponse>"""


class FakeCamera(BaseHTTPRequestHandler):
    user, password = "admin", "admin"
    calls: list[str] = []
    main_encoding = "H265"  # SetVideoEncoderConfiguration で変わる
    main_quality, main_bitrate, main_cbr = "50", "2048", "false"
    reject_set = False
    last_set: ET.Element | None = None

    def log_message(self, *a):
        pass

    def _auth_ok(self, root) -> bool:
        tok = None
        for e in root.iter():
            if e.tag.endswith("UsernameToken"):
                tok = e
        if tok is None:
            return False
        vals = {c.tag.rsplit("}", 1)[-1]: c.text for c in tok}
        nonce = base64.b64decode(vals["Nonce"])
        expect = base64.b64encode(hashlib.sha1(nonce + vals["Created"].encode() + self.password.encode()).digest()).decode()
        return vals["Username"] == self.user and vals["Password"] == expect

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        root = ET.fromstring(raw)
        body = root.find(f"{{{S}}}Body")
        op = list(body)[0].tag.rsplit("}", 1)[-1]
        FakeCamera.calls.append(op)
        port = self.server.server_address[1]
        if op == "GetSystemDateAndTime":
            n = dt.datetime.now(dt.UTC)
            resp = _env(f"<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime><tt:UTCDateTime>"
                        f"<tt:Time><tt:Hour>{n.hour}</tt:Hour><tt:Minute>{n.minute}</tt:Minute><tt:Second>{n.second}</tt:Second></tt:Time>"
                        f"<tt:Date><tt:Year>{n.year}</tt:Year><tt:Month>{n.month}</tt:Month><tt:Day>{n.day}</tt:Day></tt:Date>"
                        "</tt:UTCDateTime></tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>")
            return self._send(200, resp)
        if not self._auth_ok(root):
            return self._send(400, _env("<s:Fault><s:Code><s:Value>s:Sender</s:Value><s:Subcode>"
                                        "<s:Value>ter:NotAuthorized</s:Value></s:Subcode></s:Code>"
                                        "<s:Reason><s:Text>Sender not Authorized</s:Text></s:Reason></s:Fault>"))
        if op == "GetServices":
            resp = _env("<tds:GetServicesResponse>"
                        "<tds:Service><tds:Namespace>http://www.onvif.org/ver20/media/wsdl</tds:Namespace>"
                        "<tds:XAddr>http://127.0.0.1:%d/onvif/media2</tds:XAddr></tds:Service>"
                        "</tds:GetServicesResponse>" % port)
        elif op == "GetProfiles":
            resp = _env(PROFILES.format(main=FakeCamera.main_encoding, quality=FakeCamera.main_quality,
                                        bitrate=FakeCamera.main_bitrate, cbr=FakeCamera.main_cbr))
        elif op == "GetVideoEncoderConfigurations":
            resp = _env(f'<tr2:GetVideoEncoderConfigurationsResponse><tr2:Configurations token="ve1" GovLength="25" '
                        f'Profile="Main"><tt:Name>main</tt:Name><tt:Encoding>{FakeCamera.main_encoding}</tt:Encoding>'
                        "<tt:Resolution><tt:Width>1920</tt:Width><tt:Height>1080</tt:Height></tt:Resolution>"
                        f'<tt:RateControl ConstantBitRate="{FakeCamera.main_cbr}"><tt:FrameRateLimit>25</tt:FrameRateLimit>'
                        f"<tt:BitrateLimit>{FakeCamera.main_bitrate}</tt:BitrateLimit></tt:RateControl>"
                        f"<tt:Quality>{FakeCamera.main_quality}</tt:Quality>"
                        "</tr2:Configurations></tr2:GetVideoEncoderConfigurationsResponse>")
        elif op == "SetVideoEncoderConfiguration":
            if FakeCamera.reject_set:
                return self._send(400, _env("<s:Fault><s:Reason><s:Text>ConfigModify</s:Text></s:Reason></s:Fault>"))
            conf = list(list(body)[0])[0]
            FakeCamera.last_set = conf
            text = {e.tag.rsplit("}", 1)[-1]: e for e in conf.iter()}
            FakeCamera.main_encoding = text["Encoding"].text
            FakeCamera.main_quality = text["Quality"].text
            FakeCamera.main_bitrate = text["BitrateLimit"].text
            FakeCamera.main_cbr = text["RateControl"].get("ConstantBitRate")
            resp = _env("<tr2:SetVideoEncoderConfigurationResponse/>")
        elif op == "GetStreamUri":
            tok = [e.text for e in root.iter() if e.tag.endswith("ProfileToken")][0]
            resp = _env(f"<tr2:GetStreamUriResponse><tr2:Uri>rtsp://0.0.0.0:8554/cam1/{tok}</tr2:Uri></tr2:GetStreamUriResponse>")
        else:
            return self._send(400, _env("<s:Fault><s:Reason><s:Text>unknown</s:Text></s:Reason></s:Fault>"))
        self._send(200, resp)

    def _send(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/soap+xml")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def camera():
    srv = HTTPServer(("127.0.0.1", 0), FakeCamera)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    FakeCamera.calls = []
    FakeCamera.main_encoding = "H265"
    FakeCamera.main_quality, FakeCamera.main_bitrate, FakeCamera.main_cbr = "50", "2048", "false"
    FakeCamera.reject_set = False
    yield f"http://127.0.0.1:{srv.server_address[1]}/onvif/device_service"
    srv.shutdown()


def test_password_digest_known_vector():
    # ONVIF Core Spec の例と同じ計算方法: Base64(SHA1(nonce + created + password))
    nonce = b"\x01" * 16
    d = onvif.password_digest("admin", nonce, "2026-10-24T00:00:00.000Z")
    assert d == base64.b64encode(hashlib.sha1(nonce + b"2026-10-24T00:00:00.000Zadmin").digest()).decode()


def test_resolve_selects_h265_and_rewrites_host(camera):
    info = onvif.OnvifClient(camera, "admin", "admin").resolve()
    assert info.codec == "H265"
    assert info.profile_token == "main"
    assert (info.width, info.height) == (1920, 1080)
    assert info.media_version == 2
    # 0.0.0.0 はデバイス URL のホストに置き換わる
    assert info.rtsp_url == "rtsp://127.0.0.1:8554/cam1/main"
    assert FakeCamera.calls == ["GetSystemDateAndTime", "GetServices", "GetProfiles", "GetStreamUri"]


def test_resolve_explicit_token(camera):
    info = onvif.OnvifClient(camera, "admin", "admin").resolve("sub")
    assert info.codec == "H264" and info.rtsp_url.endswith("/sub")


def test_bad_password(camera):
    with pytest.raises(onvif.OnvifError, match="Not ?Authorized"):
        onvif.OnvifClient(camera, "admin", "wrong").resolve()


def test_select_profile_fallback_h264():
    ps = [onvif.Profile("a", encoding="JPEG"), onvif.Profile("b", encoding="H264", width=640, height=360)]
    assert onvif.select_profile(ps).token == "b"


def test_select_profile_prefer_h264():
    ps = [onvif.Profile("main", encoding="H265", width=1920, height=1080),
          onvif.Profile("sub", encoding="H264", width=640, height=360),
          onvif.Profile("main264", encoding="H264", width=1280, height=720)]
    assert onvif.select_profile(ps).token == "main"
    assert onvif.select_profile(ps, prefer="H.264").token == "main264"
    # H.264 優先でも H.264 がなければ H.265
    assert onvif.select_profile(ps[:1], prefer="h264").token == "main"
    with pytest.raises(ValueError):
        onvif.select_profile(ps, prefer="VP9")


def test_resolve_prefer_h264(camera):
    info = onvif.OnvifClient(camera, "admin", "admin").resolve(prefer_codec="H264")
    assert info.codec == "H264" and info.rtsp_url.endswith("/sub")


def _cli(url: str) -> onvif.OnvifClient:
    c = onvif.OnvifClient(url, "admin", "admin")
    c.codec_switch_settle_s = 0
    return c


def test_resolve_switches_camera_codec(camera):
    info = _cli(camera).resolve(want_codec="H264")
    # sub(H.264、低解像度)に逃げずに、main のエンコーダーを H.264 に変える
    assert info.codec == "H264" and info.profile_token == "main" and info.codec_error is None
    conf = FakeCamera.last_set
    assert conf.tag.endswith("}Configuration") and conf.get("Profile") == "High" and conf.get("token") == "ve1"
    assert "SetVideoEncoderConfiguration" in FakeCamera.calls
    # 戻す。同じなら Set しない
    info = _cli(camera).resolve(want_codec="H265")
    assert info.codec == "H265" and FakeCamera.last_set.get("Profile") == "Main"
    FakeCamera.calls = []
    _cli(camera).resolve(want_codec="H265")
    assert "SetVideoEncoderConfiguration" not in FakeCamera.calls


def test_resolve_falls_back_to_other_profile_when_camera_rejects(camera):
    FakeCamera.reject_set = True
    info = _cli(camera).resolve(want_codec="H264")
    assert info.codec == "H264" and info.profile_token == "sub"
    assert "変えられない" in info.codec_error


def test_prefer_codec_config():
    from cc_edge import config

    assert config.load({"CC_CONFIG_FILE": "/nonexistent"}).prefer_codec == "H265"
    assert config.load({"CC_CONFIG_FILE": "/nonexistent", "CC_PREFER_CODEC": "avc"}).prefer_codec == "H264"


def test_parse_probe_matches():
    raw = f"""<s:Envelope xmlns:s="{S}" xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing"
      xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"><s:Body><d:ProbeMatches><d:ProbeMatch>
      <a:EndpointReference><a:Address>urn:uuid:1234</a:Address></a:EndpointReference>
      <d:Types>dn:NetworkVideoTransmitter tds:Device</d:Types>
      <d:Scopes>onvif://www.onvif.org/name/cam1 onvif://www.onvif.org/Profile/Streaming</d:Scopes>
      <d:XAddrs>http://10.0.0.5:8080/onvif/device_service</d:XAddrs>
      </d:ProbeMatch></d:ProbeMatches></s:Body></s:Envelope>""".encode()
    m = onvif.parse_probe_matches(raw, "10.0.0.5")
    assert m == [{"urn": "urn:uuid:1234", "types": ["dn:NetworkVideoTransmitter", "tds:Device"],
                  "scopes": ["onvif://www.onvif.org/name/cam1", "onvif://www.onvif.org/Profile/Streaming"],
                  "xaddrs": ["http://10.0.0.5:8080/onvif/device_service"], "from": "10.0.0.5"}]


def test_profile_encoder_settings(camera):
    info = _cli(camera).resolve()
    assert info.selected().encoder() == {"quality": 50.0, "bitrate_limit_kbps": 2048, "rate_control": "VBR",
                                         "framerate": 25.0, "gov_length": 25}


def test_resolve_sets_encoder(camera):
    info = _cli(camera).resolve(want_encoder={"quality": 70, "bitrate_limit_kbps": 4000, "rate_control": "VBR"})
    assert info.encoder_error is None
    conf = FakeCamera.last_set
    assert conf.get("token") == "ve1" and conf.get("Profile") == "Main"  # コーデックはそのまま
    assert (FakeCamera.main_quality, FakeCamera.main_bitrate, FakeCamera.main_cbr) == ("70", "4000", "false")
    # 設定し直したプロファイルの値を返す
    assert info.selected().encoder()["quality"] == 70.0 and info.selected().encoder()["bitrate_limit_kbps"] == 4000
    # 同じなら Set しない。省略した項目は変えない
    FakeCamera.calls = []
    _cli(camera).resolve(want_encoder={"quality": 70})
    assert "SetVideoEncoderConfiguration" not in FakeCamera.calls
    assert _cli(camera).apply_encoder("main", {"rate_control": "cbr"})
    assert (FakeCamera.main_quality, FakeCamera.main_bitrate, FakeCamera.main_cbr) == ("70", "4000", "true")


def test_resolve_reports_encoder_error(camera):
    FakeCamera.reject_set = True
    info = _cli(camera).resolve(want_encoder={"bitrate_limit_kbps": 4000})
    # 配信は今の設定のまま続ける
    assert info.rtsp_url.endswith("/main") and "変えられない" in info.encoder_error
    info = _cli(camera).resolve(want_encoder={"quality": "high"})
    assert "quality" in info.encoder_error


@pytest.mark.parametrize("want", [{"quality": True}, {"bitrate_limit_kbps": 0}, {"rate_control": "ABR"}, []])
def test_encoder_args_rejects_bad_values(want):
    with pytest.raises(ValueError):
        onvif.encoder_args(want)
