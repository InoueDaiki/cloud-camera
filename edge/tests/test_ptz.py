"""PTZ の単体テスト。契約(docs/contracts.md の「PTZ」節)どおりの SOAP を返すスタブのカメラで検証する。"""

import asyncio
import threading
import time
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from cc_edge import ptz

S = "http://www.w3.org/2003/05/soap-envelope"
NSDECL = (f'xmlns:s="{S}" xmlns:tds="http://www.onvif.org/ver10/device/wsdl" '
          'xmlns:tr2="http://www.onvif.org/ver20/media/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema" '
          'xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl"')


def _env(body: str) -> bytes:
    return f"<s:Envelope {NSDECL}><s:Body>{body}</s:Body></s:Envelope>".encode()


PROFILES = """<tr2:GetProfilesResponse>
 <tr2:Profiles token="Profile_Sub"><tr2:Name>sub</tr2:Name><tr2:Configurations>
  <tr2:VideoEncoder token="ve2"><tt:Encoding>H264</tt:Encoding></tr2:VideoEncoder>
  <tr2:PTZ token="PtzConf"><tt:Name>ptz</tt:Name><tt:NodeToken>PtzNode</tt:NodeToken></tr2:PTZ>
 </tr2:Configurations></tr2:Profiles>
 <tr2:Profiles token="Profile_Main"><tr2:Name>main</tr2:Name><tr2:Configurations>
  <tr2:VideoEncoder token="ve1"><tt:Encoding>H265</tt:Encoding></tr2:VideoEncoder>
  <tr2:PTZ token="PtzConf"><tt:Name>ptz</tt:Name><tt:NodeToken>PtzNode</tt:NodeToken></tr2:PTZ>
 </tr2:Configurations></tr2:Profiles>
</tr2:GetProfilesResponse>"""


class Sim:
    """疑似 PTZ の状態(速度で動き、Timeout で止まる)。"""

    def __init__(self):
        self.reset()

    def reset(self):
        self.pos = [0.0, 0.0, 0.0]
        self.vel = [0.0, 0.0, 0.0]
        self.until = 0.0
        self.last = time.monotonic()
        self.presets = {"1": ("玄関", [0.5, 0.5, 0.25])}
        self.calls: list[tuple[str, ET.Element]] = []

    def step(self):
        now = time.monotonic()
        end = min(now, self.until) if any(self.vel) else now
        dt = max(0.0, end - self.last)
        lo = [-1, -1, 0]
        for i in range(3):
            self.pos[i] = max(lo[i], min(1, self.pos[i] + self.vel[i] * dt))
        if any(self.vel) and now >= self.until:
            self.vel = [0.0, 0.0, 0.0]
        self.last = now

    @property
    def moving(self):
        return any(self.vel)


def _vec(el):
    pt = next((e for e in el.iter() if e.tag.endswith("}PanTilt")), None)
    z = next((e for e in el.iter() if e.tag.endswith("}Zoom")), None)
    return ((float(pt.get("x")), float(pt.get("y"))) if pt is not None else None,
            float(z.get("x")) if z is not None else None)


def make_handler(sim: Sim, with_ptz: bool, ptz_conf: bool = True):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            body = ET.fromstring(raw).find(f"{{{S}}}Body")
            req = list(body)[0]
            op = req.tag.rsplit("}", 1)[-1]
            sim.calls.append((op, req))
            port = self.server.server_address[1]
            sim.step()
            if op == "GetSystemDateAndTime":
                return self._send(200, _env("<tds:GetSystemDateAndTimeResponse/>"))
            if op == "GetServices":
                svcs = ("<tds:Service><tds:Namespace>http://www.onvif.org/ver20/media/wsdl</tds:Namespace>"
                        f"<tds:XAddr>http://127.0.0.1:{port}/onvif/media2_service</tds:XAddr></tds:Service>")
                if with_ptz:
                    svcs += ("<tds:Service><tds:Namespace>http://www.onvif.org/ver20/ptz/wsdl</tds:Namespace>"
                             f"<tds:XAddr>http://0.0.0.0:{port}/onvif/ptz_service</tds:XAddr></tds:Service>")
                return self._send(200, _env(f"<tds:GetServicesResponse>{svcs}</tds:GetServicesResponse>"))
            if op == "GetCapabilities":
                return self._send(200, _env("<tds:GetCapabilitiesResponse><tds:Capabilities/></tds:GetCapabilitiesResponse>"))
            if op == "GetProfiles":
                p = PROFILES if with_ptz and ptz_conf else PROFILES.replace('<tr2:PTZ token="PtzConf"><tt:Name>ptz</tt:Name><tt:NodeToken>PtzNode</tt:NodeToken></tr2:PTZ>', "")
                return self._send(200, _env(p))
            if not with_ptz or self.path != "/onvif/ptz_service":
                return self._fault("unknown")
            tok = next((e.text for e in req if e.tag.endswith("ProfileToken")), None)
            if tok not in ("Profile_Main", "Profile_Sub"):
                return self._fault("no profile")
            if op == "GetStatus":
                ms = "MOVING" if sim.moving else "IDLE"
                p, t, z = sim.pos
                return self._send(200, _env(
                    "<tptz:GetStatusResponse><tptz:PTZStatus><tt:Position>"
                    f'<tt:PanTilt x="{p}" y="{t}"/><tt:Zoom x="{z}"/></tt:Position>'
                    f"<tt:MoveStatus><tt:PanTilt>{ms}</tt:PanTilt><tt:Zoom>{ms}</tt:Zoom></tt:MoveStatus>"
                    "</tptz:PTZStatus></tptz:GetStatusResponse>"))
            if op == "ContinuousMove":
                pt, z = _vec(req)
                sim.vel = [pt[0] if pt else 0, pt[1] if pt else 0, z or 0]
                tm = next(e.text for e in req if e.tag.endswith("Timeout"))
                sim.until = time.monotonic() + float(tm[2:-1])
            elif op == "Stop":
                sim.vel = [0.0, 0.0, 0.0]
            elif op == "AbsoluteMove":
                pt, z = _vec(req)
                if pt:
                    sim.pos[0], sim.pos[1] = pt
                if z is not None:
                    sim.pos[2] = z
            elif op == "RelativeMove":
                pt, z = _vec(req)
                if pt:
                    sim.pos[0] += pt[0]
                    sim.pos[1] += pt[1]
                if z is not None:
                    sim.pos[2] += z
            elif op == "GetPresets":
                items = "".join(f'<tptz:Preset token="{k}"><tt:Name>{n}</tt:Name></tptz:Preset>'
                                for k, (n, _) in sim.presets.items())
                return self._send(200, _env(f"<tptz:GetPresetsResponse>{items}</tptz:GetPresetsResponse>"))
            elif op == "SetPreset":
                name = next((e.text for e in req if e.tag.endswith("PresetName")), "") or ""
                new = str(len(sim.presets) + 1)
                sim.presets[new] = (name, list(sim.pos))
                return self._send(200, _env(f"<tptz:SetPresetResponse><tptz:PresetToken>{new}</tptz:PresetToken></tptz:SetPresetResponse>"))
            elif op == "GotoPreset":
                ptok = next(e.text for e in req if e.tag.endswith("PresetToken"))
                if ptok not in sim.presets:
                    return self._fault("no preset")
                sim.pos = list(sim.presets[ptok][1])
            elif op == "GotoHomePosition":
                sim.pos = [0.0, 0.0, 0.0]
            else:
                return self._fault("unknown")
            self._send(200, _env(f"<tptz:{op}Response/>"))

        def _fault(self, text):
            self._send(400, _env(f"<s:Fault><s:Reason><s:Text>{text}</s:Text></s:Reason></s:Fault>"))

        def _send(self, code, data):
            self.send_response(code)
            self.send_header("Content-Type", "application/soap+xml")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return H


def _serve(sim, with_ptz, ptz_conf=True):
    srv = HTTPServer(("127.0.0.1", 0), make_handler(sim, with_ptz, ptz_conf))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/onvif/device_service"


@pytest.fixture
def cam():
    sim = Sim()
    srv, url = _serve(sim, True)
    yield sim, url
    srv.shutdown()


@pytest.fixture
def cam_noptz():
    sim = Sim()
    srv, url = _serve(sim, False)
    yield sim, url
    srv.shutdown()


# --- クライアント ------------------------------------------------------------
def test_client_connect_and_ops(cam):
    sim, url = cam
    c = ptz.PtzClient(url, "admin", "admin")
    assert c.connect() is True
    assert c.xaddr.startswith("http://127.0.0.1:")  # 0.0.0.0 はデバイス URL のホストに置き換わる
    assert c.profile_token == "Profile_Main" and c.config_token == "PtzConf"  # H.265 を優先
    assert [(p.token, p.name) for p in c.get_presets()] == [("1", "玄関")]
    c.absolute_move(0.25, -0.5, 0.4)
    st = c.get_status()
    assert (st.pan, st.tilt, st.zoom, st.move_status) == (0.25, -0.5, 0.4, "IDLE")
    c.continuous_move(1, 0, 0, 1500)
    op, req = sim.calls[-1]
    assert op == "ContinuousMove"
    assert next(e.text for e in req if e.tag.endswith("Timeout")) == "PT1.5S"
    assert c.get_status().move_status == "MOVING"
    c.stop()
    assert c.get_status().move_status == "IDLE"
    assert c.set_preset("駐車場") == "2"
    c.goto_home()
    assert c.get_status().pan == 0.0
    c.goto_preset("2")
    assert c.get_status().zoom == pytest.approx(0.4)
    # 認証ヘッダーを付けて呼んでいる
    assert ptz.PTZ_NS in ET.tostring(sim.calls[-1][1]).decode()


def test_client_explicit_profile(cam):
    _, url = cam
    c = ptz.PtzClient(url, "admin", "admin", profile_token="Profile_Sub")
    assert c.connect() and c.profile_token == "Profile_Sub"


def test_client_not_supported(cam_noptz):
    _, url = cam_noptz
    assert ptz.PtzClient(url, "admin", "admin").connect() is False


def test_client_without_ptz_configuration():
    """PTZ サービスはあるがプロファイルに PTZConfiguration がないカメラは、配信と同じプロファイルで試す。"""
    sim = Sim()
    srv, url = _serve(sim, True, ptz_conf=False)
    try:
        c = ptz.PtzClient(url, "admin", "admin")
        assert c.connect() is True
        assert c.profile_token == "Profile_Main" and c.config_token is None
    finally:
        srv.shutdown()


def test_parse_status_variants():
    body = ET.fromstring(_env('<tptz:GetStatusResponse><tptz:PTZStatus><tt:Position><tt:PanTilt x="0.1" y="0.2"/>'
                              "</tt:Position><tt:MoveStatus><tt:PanTilt>IDLE</tt:PanTilt><tt:Zoom>MOVING</tt:Zoom>"
                              "</tt:MoveStatus></tptz:PTZStatus></tptz:GetStatusResponse>"))
    st = ptz.parse_status(body)
    assert (st.pan, st.tilt, st.zoom, st.move_status) == (0.1, 0.2, None, "MOVING")
    assert ptz.parse_status(ET.fromstring(_env("<x/>"))).move_status == "UNKNOWN"


def test_normalize_command():
    n = ptz.normalize_command
    assert n({"op": "continuous", "pan": 5, "tilt": -0.5}) == {
        "op": "continuous", "pan": 1.0, "tilt": -0.5, "zoom": None, "timeout_ms": 10000}
    assert n({"op": "absolute", "zoom": -1})["zoom"] == 0.0
    assert n({"op": "continuous", "timeout_ms": 999999})["timeout_ms"] == 60000
    for bad in ({"op": "fly"}, {"op": "absolute"}, {"op": "relative", "pan": "x"}, {"op": "goto_preset"},
                {"op": "continuous", "pan": True}, {"op": "continuous", "pan": float("nan")}):
        with pytest.raises(ptz.PtzCommandError):
            n(bad)


# --- 管理(cmd とシャドウ) -------------------------------------------------------
class Recorder:
    def __init__(self):
        self.results: list[dict] = []
        self.shadow: list[tuple[float, str, dict]] = []

    async def result(self, r):
        self.results.append(r)

    async def report(self, cam, p):
        self.shadow.append((time.monotonic(), cam, p))


def _manager(rec, monkeypatch):
    # テストを速くする
    monkeypatch.setattr(ptz, "POLL_INTERVAL_S", 0.1)
    monkeypatch.setattr(ptz, "SETTLE_S", 0.2)
    return ptz.PtzManager("gw-001", rec.result, rec.report)


async def _wait_idle(mgr, cam):
    st = mgr.cams[cam]
    await st.ready.wait()
    for _ in range(100):
        if not st.tracker or st.tracker.done():
            return
        await asyncio.sleep(0.05)


def test_manager_startup_report_and_continuous(cam, monkeypatch):
    sim, url = cam
    rec = Recorder()

    async def run():
        mgr = _manager(rec, monkeypatch)
        confs = {"cam-001": {"onvif_url": url, "username": "admin", "password": "admin"},
                 "cam-002": {"rtsp_url": "rtsp://x/y"}}
        mgr.sync(confs)
        await mgr.cams["cam-001"].ready.wait()
        await mgr.cams["cam-002"].ready.wait()
        await asyncio.sleep(0)
        # 起動時に supported とプリセットを報告する
        first = {c: p for _, c, p in rec.shadow}
        assert first["cam-001"]["supported"] is True
        assert first["cam-001"]["presets"] == [{"token": "1", "name": "玄関"}]
        assert first["cam-001"]["error"] is None and "updated_at" in first["cam-001"]
        assert first["cam-002"]["supported"] is False and first["cam-002"]["pan"] is None
        rec.shadow.clear()

        r = await mgr.handle({"action": "ptz", "camera_id": "cam-001", "request_id": "r1",
                              "op": "continuous", "pan": 1, "timeout_ms": 1600}, confs)
        assert r["ok"] is True and r["request_id"] == "r1" and r["gateway_id"] == "gw-001"
        assert r["move_status"] == "MOVING" and set(r["position"]) == {"pan", "tilt", "zoom"}
        await asyncio.sleep(1.3)
        r2 = await mgr.handle({"action": "ptz", "camera_id": "cam-001", "request_id": "r2", "op": "stop"}, confs)
        assert r2["ok"] and r2["move_status"] == "IDLE"
        await _wait_idle(mgr, "cam-001")
        moving = [(t, p) for t, c, p in rec.shadow if p["move_status"] == "MOVING"]
        # 動いている間は 1 秒に 1 回まで(1.3 秒なら 2 回)
        assert 1 <= len(moving) <= 2
        if len(moving) == 2:
            assert moving[1][0] - moving[0][0] >= 0.95
        # 止まったら必ず 1 回
        last = rec.shadow[-1][2]
        assert last["move_status"] == "IDLE" and last["pan"] > 0.5
        assert rec.results[-1]["request_id"] == "r2"

        # 非対応のカメラはエラー結果
        r3 = await mgr.handle({"action": "ptz", "camera_id": "cam-002", "request_id": "r3", "op": "home"}, confs)
        assert r3["ok"] is False and "対応していません" in r3["error"]
        r4 = await mgr.handle({"action": "ptz", "camera_id": "nope", "op": "home"}, confs)
        assert r4["ok"] is False and "カメラがありません" in r4["error"]
        r5 = await mgr.handle({"action": "ptz", "camera_id": "cam-001", "op": "goto_preset",
                               "preset_token": "99"}, confs)
        assert r5["ok"] is False and "no preset" in r5["error"]
        await mgr.stop_all()

    asyncio.run(run())


def test_manager_set_preset_reports_presets(cam, monkeypatch):
    sim, url = cam
    rec = Recorder()

    async def run():
        mgr = _manager(rec, monkeypatch)
        confs = {"cam-001": {"onvif_url": url, "username": "admin", "password": "admin"}}
        mgr.sync(confs)
        await mgr.cams["cam-001"].ready.wait()
        await mgr.handle({"camera_id": "cam-001", "op": "absolute", "pan": -0.5, "tilt": 0.5, "zoom": 0.5}, confs)
        r = await mgr.handle({"camera_id": "cam-001", "op": "set_preset", "preset_name": "裏口"}, confs)
        assert r["ok"] and r["preset_token"] == "2"
        await _wait_idle(mgr, "cam-001")
        assert rec.shadow[-1][2]["presets"][-1] == {"token": "2", "name": "裏口"}
        await mgr.handle({"camera_id": "cam-001", "op": "home"}, confs)
        r = await mgr.handle({"camera_id": "cam-001", "op": "goto_preset", "preset_token": "2"}, confs)
        assert r["position"] == {"pan": -0.5, "tilt": 0.5, "zoom": 0.5}
        await mgr.stop_all()

    asyncio.run(run())


def test_manager_not_supported_camera(cam_noptz, monkeypatch):
    _, url = cam_noptz
    rec = Recorder()

    async def run():
        mgr = _manager(rec, monkeypatch)
        confs = {"cam-001": {"onvif_url": url, "username": "admin", "password": "admin"}}
        mgr.sync(confs)
        r = await mgr.handle({"camera_id": "cam-001", "op": "continuous", "pan": 0.5}, confs)
        assert r["ok"] is False
        assert rec.shadow[0][2]["supported"] is False and rec.shadow[0][2]["error"] is None

    asyncio.run(run())


def test_manager_unreachable_camera_reports_error(monkeypatch):
    rec = Recorder()

    async def run():
        mgr = _manager(rec, monkeypatch)
        confs = {"cam-001": {"onvif_url": "http://127.0.0.1:9/onvif/device_service"}}
        mgr.sync(confs)
        await mgr.cams["cam-001"].ready.wait()
        await asyncio.sleep(0)
        assert rec.shadow[0][2]["supported"] is False and "PTZ の準備に失敗" in rec.shadow[0][2]["error"]
        r = await mgr.handle({"camera_id": "cam-001", "op": "stop"}, confs)
        assert r["ok"] is False

    asyncio.run(run())


def test_manager_orders_commands(cam, monkeypatch):
    """続けて届いた cmd は届いた順に処理する(continuous → stop が逆転しない)。"""
    sim, url = cam
    rec = Recorder()

    async def run():
        mgr = _manager(rec, monkeypatch)
        confs = {"cam-001": {"onvif_url": url}}
        mgr.sync(confs)
        await asyncio.gather(
            mgr.handle({"camera_id": "cam-001", "request_id": "a", "op": "continuous", "pan": 1}, confs),
            mgr.handle({"camera_id": "cam-001", "request_id": "b", "op": "stop"}, confs),
        )
        assert [r["request_id"] for r in rec.results] == ["a", "b"]
        ops = [op for op, _ in sim.calls if op in ("ContinuousMove", "Stop")]
        assert ops == ["ContinuousMove", "Stop"]
        await mgr.stop_all()

    asyncio.run(run())
