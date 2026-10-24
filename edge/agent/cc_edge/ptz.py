"""PTZ の制御(docs/contracts.md の「PTZ」節)。

- ONVIF PTZ クライアント(標準ライブラリだけ。onvif.py の OnvifClient の上に作る)
  - GetServices(なければ GetCapabilities)で PTZ サービスの XAddr を探す
  - Media2 / Media の GetProfiles で、プロファイルに付いた PTZConfiguration を探す
  - GetStatus、ContinuousMove(Timeout)、RelativeMove、AbsoluteMove、Stop、GetPresets、SetPreset、
    GotoPreset、GotoHomePosition
- cmd `{"action": "ptz", ...}` の処理。結果を `{p}/{gw}/ptz` に送る
- シャドウの `reported.cameras.{id}.ptz`。動いている間は 1 秒に 1 回まで、止まったら必ず 1 回

座標と速度は ONVIF の Generic 空間(pan / tilt は -1〜1、zoom は 0〜1、速度は -1〜1)をそのまま使う。

手元での確認(MQTT なしで cmd を直接流す):

    python3 -m cc_edge.ptz http://emulator:8080/onvif/device_service '{"op": "continuous", "pan": 0.5}'
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import math
import time
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from . import onvif
from .onvif import MEDIA1_NS, MEDIA2_NS, OnvifClient, OnvifError, _find_local, _findall_local, _text, _xml_escape

log = logging.getLogger(__name__)

PTZ_NS = "http://www.onvif.org/ver20/ptz/wsdl"
_X = f'xmlns:tptz="{PTZ_NS}"'

OPS = ("continuous", "stop", "absolute", "relative", "goto_preset", "set_preset", "home")
DEFAULT_TIMEOUT_MS = 10_000

# 追跡(GetStatus のポーリング)の間隔と、シャドウへの報告の最小間隔
POLL_INTERVAL_S = 0.5
REPORT_INTERVAL_S = 1.0
# 命令の直後は、まだ MOVING になっていないことがあるので、この時間は IDLE でも追跡を続ける
SETTLE_S = 0.6
# 追跡の上限(continuous は timeout_ms で止まるので、通常はここまで続かない)
TRACK_MAX_S = 300.0


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class PtzStatus:
    pan: float | None = None
    tilt: float | None = None
    zoom: float | None = None
    move_status: str = "UNKNOWN"  # MOVING / IDLE / UNKNOWN(pan/tilt と zoom のどちらかが MOVING なら MOVING)

    def position(self) -> dict:
        return {"pan": self.pan, "tilt": self.tilt, "zoom": self.zoom}


@dataclass
class Preset:
    token: str
    name: str = ""


def _float(s: str) -> float | None:
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _num(v: float) -> str:
    return f"{float(v):.6g}"


def _vector(tag: str, pan: float | None, tilt: float | None, zoom: float | None) -> str:
    """PTZVector / PTZSpeed の中身(省略した軸は送らない)。"""
    parts = []
    if pan is not None or tilt is not None:
        parts.append(f'<tt:PanTilt x="{_num(pan or 0)}" y="{_num(tilt or 0)}"/>')
    if zoom is not None:
        parts.append(f'<tt:Zoom x="{_num(zoom)}"/>')
    return f"<tptz:{tag}>{''.join(parts)}</tptz:{tag}>"


def duration(ms: int) -> str:
    """xs:duration(例: 10000 → PT10S、1500 → PT1.5S)"""
    return f"PT{ms / 1000:g}S"


def parse_status(body: ET.Element) -> PtzStatus:
    st = PtzStatus()
    pos = _find_local(body, "Position")
    if pos is not None:
        pt = _find_local(pos, "PanTilt")
        z = _find_local(pos, "Zoom")
        if pt is not None:
            st.pan, st.tilt = _float(pt.get("x", "")), _float(pt.get("y", ""))
        if z is not None:
            st.zoom = _float(z.get("x", ""))
    ms = _find_local(body, "MoveStatus")
    if ms is not None:
        vals = {_text(ms, "PanTilt").upper(), _text(ms, "Zoom").upper()} - {""}
        if not vals and ms.text and ms.text.strip():
            vals = {ms.text.strip().upper()}  # Media の古い形(MoveStatus に直接値)
        if "MOVING" in vals:
            st.move_status = "MOVING"
        elif vals and vals <= {"IDLE"}:
            st.move_status = "IDLE"
    return st


def parse_presets(body: ET.Element) -> list[Preset]:
    out = []
    for p in _findall_local(body, "Preset"):
        tok = p.get("token", "")
        if tok:
            out.append(Preset(token=tok, name=_text(p, "Name")))
    return out


def parse_ptz_profiles(body: ET.Element) -> list[tuple[str, str, str]]:
    """GetProfiles の応答から (プロファイルのトークン, PTZConfiguration のトークン, 符号化) を返す。

    Media2 は Configurations/PTZ、Media は PTZConfiguration。PTZ の付いていないプロファイルは除く。
    """
    out = []
    for p in _findall_local(body, "Profiles"):
        conf = _find_local(p, "PTZ")
        if conf is None:
            conf = _find_local(p, "PTZConfiguration")
        if conf is None:
            continue
        enc = _find_local(p, "VideoEncoder")
        if enc is None:
            enc = _find_local(p, "VideoEncoderConfiguration")
        encoding = onvif._norm_encoding(_text(enc, "Encoding")) if enc is not None else ""
        out.append((p.get("token", ""), conf.get("token", ""), encoding))
    return out


class PtzClient:
    """1 台のカメラの ONVIF PTZ。呼び出しはすべてブロッキング(asyncio.to_thread で呼ぶ)。"""

    def __init__(self, device_url: str, username: str = "", password: str = "",
                 profile_token: str | None = None, timeout: float = 3.0):
        self.onvif = OnvifClient(device_url, username, password, timeout=timeout)
        self.want_profile = profile_token
        self.xaddr: str | None = None
        self.profile_token: str | None = None
        self.config_token: str | None = None

    # --- 準備 ------------------------------------------------------------
    def connect(self) -> bool:
        """PTZ サービスとプロファイルを探す。PTZ に対応していなければ False。"""
        cli = self.onvif
        cli.sync_clock()
        services = cli.get_services()
        xaddr = services.get(PTZ_NS)
        if not xaddr:
            # GetServices に載っていない(または GetServices がない)カメラは GetCapabilities で探す
            try:
                body = cli.call(cli.device_url,
                                "<tds:GetCapabilities><tds:Category>PTZ</tds:Category></tds:GetCapabilities>")
                ptz = _find_local(body, "PTZ")
                if ptz is not None and _text(ptz, "XAddr"):
                    xaddr = onvif._rewrite_host(_text(ptz, "XAddr"), cli.device_url)
            except OnvifError as e:
                log.debug("GetCapabilities(PTZ)に失敗: %s", e)
        if not xaddr:
            return False
        profiles = self._ptz_profiles(services)
        if not profiles:
            # PTZ サービスはあるのに、プロファイルに PTZConfiguration が見えないカメラもある。
            # 配信と同じプロファイルで GetStatus が通れば対応とみなす
            try:
                _, all_profiles = cli.get_profiles()
                prof = onvif.select_profile(all_profiles, self.want_profile)
            except OnvifError:
                return False
            self.xaddr, self.profile_token, self.config_token = xaddr, prof.token, None
            try:
                self.get_status()
            except OnvifError as e:
                log.info("PTZ サービスはあるが、プロファイル %s で GetStatus できない: %s", prof.token, e)
                self.xaddr = self.profile_token = None
                return False
            log.info("プロファイルに PTZConfiguration がないが、%s で GetStatus できたので PTZ を使う", prof.token)
            return True
        chosen = None
        if self.want_profile:
            chosen = next((p for p in profiles if p[0] == self.want_profile), None)
        if chosen is None:
            # 配信と同じ選び方(H.265 優先)。main と sub は同じ PTZConfiguration なので、どれでも同じ窓が動く
            chosen = next((p for p in profiles if p[2] == "H265"), profiles[0])
        self.xaddr = xaddr
        self.profile_token, self.config_token = chosen[0], chosen[1]
        return True

    def _ptz_profiles(self, services: dict[str, str]) -> list[tuple[str, str, str]]:
        cli = self.onvif
        if MEDIA2_NS in services:
            body = cli.call(services[MEDIA2_NS], "<tr2:GetProfiles><tr2:Type>All</tr2:Type></tr2:GetProfiles>")
            found = parse_ptz_profiles(body)
            if found:
                return found
        if MEDIA1_NS in services:
            body = cli.call(services[MEDIA1_NS], "<trt:GetProfiles/>")
            return parse_ptz_profiles(body)
        return []

    # --- 操作 ------------------------------------------------------------
    def _call(self, op: str, inner: str = "") -> ET.Element:
        if not self.xaddr or self.profile_token is None:
            raise OnvifError("PTZ の準備ができていない(connect を先に呼ぶ)")
        body = (f"<tptz:{op} {_X}><tptz:ProfileToken>{_xml_escape(self.profile_token)}</tptz:ProfileToken>"
                f"{inner}</tptz:{op}>")
        return self.onvif.call(self.xaddr, body, action=f"{PTZ_NS}/{op}")

    def get_status(self) -> PtzStatus:
        return parse_status(self._call("GetStatus"))

    def continuous_move(self, pan: float, tilt: float, zoom: float, timeout_ms: int) -> None:
        self._call("ContinuousMove", _vector("Velocity", pan, tilt, zoom)
                   + f"<tptz:Timeout>{duration(timeout_ms)}</tptz:Timeout>")

    def relative_move(self, pan: float | None, tilt: float | None, zoom: float | None) -> None:
        self._call("RelativeMove", _vector("Translation", pan, tilt, zoom))

    def absolute_move(self, pan: float | None, tilt: float | None, zoom: float | None) -> None:
        self._call("AbsoluteMove", _vector("Position", pan, tilt, zoom))

    def stop(self) -> None:
        self._call("Stop", "<tptz:PanTilt>true</tptz:PanTilt><tptz:Zoom>true</tptz:Zoom>")

    def get_presets(self) -> list[Preset]:
        return parse_presets(self._call("GetPresets"))

    def set_preset(self, name: str | None = None, token: str | None = None) -> str:
        inner = ""
        if name:
            inner += f"<tptz:PresetName>{_xml_escape(name)}</tptz:PresetName>"
        if token:
            inner += f"<tptz:PresetToken>{_xml_escape(token)}</tptz:PresetToken>"
        body = self._call("SetPreset", inner)
        return _text(body, "PresetToken") or (token or "")

    def goto_preset(self, token: str) -> None:
        self._call("GotoPreset", f"<tptz:PresetToken>{_xml_escape(token)}</tptz:PresetToken>")

    def goto_home(self) -> None:
        self._call("GotoHomePosition")


# --- cmd の検証 -------------------------------------------------------------
class PtzCommandError(ValueError):
    pass


def _clamp(v: Any, lo: float, hi: float, name: str) -> float | None:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise PtzCommandError(f"{name} は数値で指定する")
    return max(lo, min(hi, float(v)))


def normalize_command(msg: dict) -> dict:
    """cmd の値を検証して範囲に収める(API でも検証するが、エッジは自分を守るためにもう一度見る)。"""
    op = msg.get("op")
    if op not in OPS:
        raise PtzCommandError(f"op は {', '.join(OPS)} のどれか: {op!r}")
    out: dict[str, Any] = {"op": op}
    if op in ("continuous", "relative"):
        # relative の pan / tilt は端から端まで(差分 2)動かせるようにする
        lim = 1 if op == "continuous" else 2
        out["pan"] = _clamp(msg.get("pan"), -lim, lim, "pan")
        out["tilt"] = _clamp(msg.get("tilt"), -lim, lim, "tilt")
        out["zoom"] = _clamp(msg.get("zoom"), -1, 1, "zoom")
    elif op == "absolute":
        out["pan"] = _clamp(msg.get("pan"), -1, 1, "pan")
        out["tilt"] = _clamp(msg.get("tilt"), -1, 1, "tilt")
        out["zoom"] = _clamp(msg.get("zoom"), 0, 1, "zoom")
    if op in ("absolute", "relative") and all(out[k] is None for k in ("pan", "tilt", "zoom")):
        raise PtzCommandError("pan / tilt / zoom のどれかが必要")
    if op == "continuous":
        t = msg.get("timeout_ms", DEFAULT_TIMEOUT_MS)
        if t is None:
            t = DEFAULT_TIMEOUT_MS
        if isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t):
            raise PtzCommandError("timeout_ms は数値で指定する")
        out["timeout_ms"] = int(max(100, min(60_000, t)))
    if op == "goto_preset":
        tok = msg.get("preset_token")
        if not isinstance(tok, str) or not tok:
            raise PtzCommandError("goto_preset には preset_token が必要")
        out["preset_token"] = tok
    if op == "set_preset":
        name, tok = msg.get("preset_name"), msg.get("preset_token")
        out["preset_name"] = str(name)[:64] if name else None
        out["preset_token"] = str(tok) if tok else None
    return out


# --- 管理(ゲートウェイ全体) ---------------------------------------------------
Publish = Callable[[str, dict], Awaitable[None]]


@dataclass
class CameraPtz:
    camera_id: str
    conf_key: str
    client: PtzClient | None = None
    supported: bool = False
    error: str | None = None
    status: PtzStatus = field(default_factory=PtzStatus)
    presets: list[Preset] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    tracker: asyncio.Task | None = None
    last_command_at: float = 0.0
    last_report_at: float = 0.0

    def reported(self) -> dict:
        # 値のないキーは null で書く(シャドウからキーが消え、前の値が残らない)
        d: dict[str, Any] = {"supported": self.supported, "pan": None, "tilt": None, "zoom": None,
                             "presets": None, "move_status": None}
        if self.supported:
            d.update(self.status.position())
            d["presets"] = [{"token": p.token, "name": p.name} for p in self.presets]
            d["move_status"] = self.status.move_status
        d["error"] = self.error
        d["updated_at"] = utc_now()
        return d


def _conf_key(conf: dict) -> str:
    keys = ("onvif_url", "username", "password", "profile_token")
    return json.dumps({k: conf.get(k) for k in keys}, sort_keys=True)


class PtzManager:
    """カメラごとの PTZ。agent.py からは sync(設定の反映)と handle(cmd)だけを呼ぶ。

    publish_result(payload) は `{p}/{gw}/ptz` へ、publish_shadow(camera_id, ptz) は
    シャドウの reported.cameras.{camera_id}.ptz へ送る関数(MQTT がなければログだけ)。
    """

    def __init__(self, gateway_id: str, publish_result: Callable[[dict], Awaitable[None]],
                 publish_shadow: Callable[[str, dict], Awaitable[None]],
                 client_factory: Callable[[dict], PtzClient] | None = None):
        self.gateway_id = gateway_id
        self.publish_result = publish_result
        self.publish_shadow = publish_shadow
        self.client_factory = client_factory or (lambda c: PtzClient(
            c["onvif_url"], c.get("username", ""), c.get("password", ""), c.get("profile_token")))
        self.cams: dict[str, CameraPtz] = {}
        self._tasks: set[asyncio.Task] = set()

    # --- 設定の反映 --------------------------------------------------------
    def sync(self, confs: dict[str, dict]) -> None:
        """desired.cameras が変わったら呼ぶ。新しいカメラと設定が変わったカメラだけ準備し直す。"""
        for cam in list(self.cams):
            if cam not in confs:
                self._drop(cam)
        for cam, conf in confs.items():
            key = _conf_key(conf or {})
            cur = self.cams.get(cam)
            if cur and cur.conf_key == key:
                continue
            if cur:
                self._drop(cam)
            st = self.cams[cam] = CameraPtz(cam, key)
            self._spawn(self._init(st, conf or {}))

    def resend(self) -> None:
        """再接続のあとなどに、いまの状態をシャドウへ送り直す。"""
        for st in self.cams.values():
            if st.ready.is_set():
                self._spawn(self._report(st, force=True))

    def _drop(self, cam: str) -> None:
        st = self.cams.pop(cam, None)
        if st and st.tracker:
            st.tracker.cancel()

    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.ensure_future(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    async def _init(self, st: CameraPtz, conf: dict) -> None:
        try:
            if not conf.get("onvif_url"):
                # rtsp_url を直接使う設定(ONVIF なし)では PTZ を使えない
                st.supported, st.error = False, None
                return
            cli = self.client_factory(conf)
            ok = await asyncio.to_thread(cli.connect)
            if not ok:
                st.supported, st.error = False, None
                log.info("[%s] PTZ: 非対応", st.camera_id)
                return
            st.client, st.supported, st.error = cli, True, None
            st.presets = await asyncio.to_thread(cli.get_presets)
            st.status = await asyncio.to_thread(cli.get_status)
            log.info("[%s] PTZ: 対応(profile=%s, presets=%d, %s)", st.camera_id, cli.profile_token,
                     len(st.presets), st.status.position())
        except Exception as e:  # noqa: BLE001
            # カメラにつながらないときは非対応として報告し、次の cmd で準備し直す
            st.client, st.supported, st.error = None, False, f"PTZ の準備に失敗: {e}"
            log.warning("[%s] %s", st.camera_id, st.error)
        finally:
            st.ready.set()
            if self.cams.get(st.camera_id) is st:
                await self._report(st, force=True)

    async def stop_all(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # --- cmd ---------------------------------------------------------------
    async def handle(self, msg: dict, confs: dict[str, dict] | None = None) -> dict:
        """cmd `{"action": "ptz", ...}` を処理し、結果を `{p}/{gw}/ptz` に送って返す。"""
        cam = msg.get("camera_id")
        result: dict[str, Any] = {"ts": utc_now(), "gateway_id": self.gateway_id, "camera_id": cam,
                                  "request_id": msg.get("request_id"), "ok": False}
        st = self.cams.get(cam) if isinstance(cam, str) else None
        if st is None:
            result["error"] = f"カメラがありません: {cam}"
            return await self._result(result)
        # 届いた順に処理する(押している間の continuous と離したときの stop の順を守る)
        async with st.lock:
            await st.ready.wait()
            try:
                cmd = normalize_command(msg)
                if st.client is None and st.error and confs and cam in confs:
                    # 準備に失敗していたら、もう一度だけ試す
                    st.ready.clear()
                    await self._init(st, confs[cam] or {})
                if not st.supported or st.client is None:
                    raise PtzCommandError(st.error or "このカメラは PTZ に対応していません")
                extra = await asyncio.to_thread(self._run, st.client, cmd)
                result.update(extra)
                if cmd["op"] == "set_preset":
                    st.presets = await asyncio.to_thread(st.client.get_presets)
                st.status = await asyncio.to_thread(st.client.get_status)
                result["ok"] = True
            except (PtzCommandError, OnvifError) as e:
                result["error"] = str(e)
            except Exception as e:  # noqa: BLE001
                log.exception("[%s] PTZ cmd に失敗", cam)
                result["error"] = f"{type(e).__name__}: {e}"
            result["position"] = st.status.position()
            result["move_status"] = st.status.move_status
            st.last_command_at = time.monotonic()
            if result["ok"]:
                self._track(st, force_report=msg.get("op") == "set_preset")
        return await self._result(result)

    @staticmethod
    def _run(cli: PtzClient, cmd: dict) -> dict:
        op = cmd["op"]
        if op == "continuous":
            cli.continuous_move(cmd["pan"] or 0.0, cmd["tilt"] or 0.0, cmd["zoom"] or 0.0, cmd["timeout_ms"])
        elif op == "stop":
            cli.stop()
        elif op == "absolute":
            cli.absolute_move(cmd["pan"], cmd["tilt"], cmd["zoom"])
        elif op == "relative":
            cli.relative_move(cmd["pan"], cmd["tilt"], cmd["zoom"])
        elif op == "goto_preset":
            cli.goto_preset(cmd["preset_token"])
        elif op == "home":
            cli.goto_home()
        elif op == "set_preset":
            return {"preset_token": cli.set_preset(cmd["preset_name"], cmd["preset_token"])}
        return {}

    async def _result(self, result: dict) -> dict:
        log.info("ptz: %s", json.dumps(result, ensure_ascii=False))
        await self.publish_result(result)
        return result

    # --- 動きの追跡とシャドウ --------------------------------------------------
    def _track(self, st: CameraPtz, force_report: bool = False) -> None:
        # 命令の直後に 1 回送る(動いている間は 1 秒に 1 回までに抑える。止まっていれば必ず送る)
        self._spawn(self._report(st, force=force_report or st.status.move_status != "MOVING"))
        if st.tracker and not st.tracker.done():
            return  # 動いている間の追跡は 1 本だけ(last_command_at の更新で延長される)
        st.tracker = self._spawn(self._track_loop(st))

    async def _track_loop(self, st: CameraPtz) -> None:
        started = time.monotonic()
        try:
            while time.monotonic() - started < TRACK_MAX_S:
                if st.status.move_status == "MOVING":
                    await self._report(st)  # 1 秒に 1 回まで
                elif time.monotonic() - st.last_command_at >= SETTLE_S:
                    break
                await asyncio.sleep(POLL_INTERVAL_S)
                try:
                    st.status = await asyncio.to_thread(st.client.get_status)
                except Exception as e:  # noqa: BLE001
                    log.warning("[%s] PTZ GetStatus に失敗: %s", st.camera_id, e)
                    break
        finally:
            # 止まったら(追跡をやめるときは)必ず 1 回送る
            if self.cams.get(st.camera_id) is st:
                await self._report(st, force=True)

    async def _report(self, st: CameraPtz, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - st.last_report_at < REPORT_INTERVAL_S:
            return
        st.last_report_at = now
        try:
            await self.publish_shadow(st.camera_id, st.reported())
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] PTZ のシャドウ報告に失敗: %s", st.camera_id, e)


# --- 手元での確認用 CLI ------------------------------------------------------
async def _cli(url: str, cmds: list[dict], username: str, password: str) -> None:
    async def show(kind: str, payload) -> None:
        print(kind, json.dumps(payload, ensure_ascii=False), flush=True)

    mgr = PtzManager("local", lambda r: show("result", r), lambda c, p: show(f"shadow[{c}]", p))
    confs = {"cam": {"onvif_url": url, "username": username, "password": password}}
    mgr.sync(confs)
    for c in cmds:
        if "sleep" in c:
            await asyncio.sleep(float(c["sleep"]))
            continue
        await mgr.handle({"action": "ptz", "camera_id": "cam", "request_id": "cli", **c}, confs)
    st = mgr.cams["cam"]
    while st.tracker and not st.tracker.done():
        await asyncio.sleep(0.1)


def main(argv: list[str] | None = None) -> None:
    import argparse

    ap = argparse.ArgumentParser(description="PTZ の cmd を ONVIF カメラに直接流す(MQTT なし)")
    ap.add_argument("onvif_url")
    ap.add_argument("cmds", nargs="*", help='JSON。例 \'{"op":"continuous","pan":0.5}\' \'{"sleep":2}\' \'{"op":"stop"}\'')
    ap.add_argument("--username", default="admin")
    ap.add_argument("--password", default="admin")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(_cli(a.onvif_url, [json.loads(c) for c in a.cmds], a.username, a.password))


if __name__ == "__main__":
    main()
