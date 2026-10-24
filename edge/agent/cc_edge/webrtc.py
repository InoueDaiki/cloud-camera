"""KVS WebRTC のライブ配信: master(C の子プロセス `ccWebrtcMaster`)の起動・監視。

1 カメラ = 1 シグナリングチャネル(`{p}-{camera_id}`)= 1 プロセス。master は視聴者の offer を見て、
H.265 を受けられる視聴者には main(H.265)、それ以外には sub(H.264)の RTSP を再エンコードせずに流す
(edge/webrtc/src/cc_webrtc_master.c)。RTSP は視聴者がいるストリームだけ読むので、kvssink の録画(main)とは別の接続になる。

録画(kvssink の子プロセス)とは別のプロセスなので、master が落ちても・回線断でつなぎ直しても録画とバッファには触らない。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass

from . import onvif
from .pipeline import Backoff

log = logging.getLogger(__name__)

DEFAULT_BIN = "/opt/kvs/webrtc/bin/ccWebrtcMaster"
DEFAULT_LIB_DIR = "/opt/kvs/webrtc/lib"
DEFAULT_CACERT = "/opt/kvs/webrtc/cert.pem"

# reported.cameras.{id}.webrtc.state
STATE_DISABLED = "DISABLED"  # desired で無効、または配信(streaming)を止めている
STATE_UNAVAILABLE = "UNAVAILABLE"  # master のバイナリや IoT の認証情報がない(ドライランなど)
STATE_STARTING = "STARTING"  # 起動中(シグナリングにつながる前)
STATE_CONNECTED = "CONNECTED"  # シグナリングにつながり、視聴者を待っている
STATE_RECONNECTING = "RECONNECTING"  # シグナリングが切れ、SDK がつなぎ直している(回線断など)
STATE_ERROR = "ERROR"  # プロセスが終了し、バックオフ中


def webrtc_enabled(conf: dict) -> bool:
    """desired.cameras.{id}.webrtc.enabled(省略時は有効)。"""
    w = conf.get("webrtc")
    if isinstance(w, dict) and "enabled" in w:
        return bool(w["enabled"])
    if isinstance(w, bool):
        return w
    return True


def select_sub_profile(profiles: list[onvif.Profile], main_token: str) -> onvif.Profile | None:
    """WebRTC の H.264 用のプロファイル(main 以外の H.264 で最大解像度)。なければ None。"""
    cands = [p for p in profiles if p.encoding == "H264" and p.token != main_token]
    return max(cands, key=lambda p: p.width * p.height, default=None)


@dataclass
class WebRtcSpec:
    camera_id: str
    channel: str
    main_rtsp_url: str | None
    main_codec: str = "H265"
    sub_rtsp_url: str | None = None
    sub_codec: str = "H264"
    username: str = ""
    password: str = ""
    region: str = "ap-northeast-1"
    iot_credential_endpoint: str = ""
    cert_path: str = ""
    key_path: str = ""
    role_alias: str = ""
    thing_name: str = ""
    idle_stop_s: int = 10
    ice_transport_policy: str = "all"  # relay で TURN を強制
    log_level: int = 3  # AWS_KVS_LOG_LEVEL(1 verbose 〜 4 warn)
    binary: str = DEFAULT_BIN
    lib_dir: str = DEFAULT_LIB_DIR
    cacert: str = DEFAULT_CACERT

    def codecs(self) -> list[str]:
        """視聴者に出せるコーデック(H.265 を先に)。"""
        have = {c for u, c in ((self.main_rtsp_url, self.main_codec), (self.sub_rtsp_url, self.sub_codec)) if u}
        return [c for c in ("H265", "H264") if c in have]

    def env(self, base: dict[str, str] | None = None) -> dict[str, str]:
        env = dict(os.environ if base is None else base)
        lib = self.lib_dir + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
        env.update({
            "CC_WEBRTC_CHANNEL": self.channel,
            "CC_MAIN_CODEC": self.main_codec.lower(),
            "CC_SUB_CODEC": self.sub_codec.lower(),
            "CC_IDLE_STOP_S": str(self.idle_stop_s),
            "AWS_DEFAULT_REGION": self.region,
            "AWS_IOT_CORE_CREDENTIAL_ENDPOINT": self.iot_credential_endpoint,
            "AWS_IOT_CORE_ROLE_ALIAS": self.role_alias,
            "AWS_IOT_CORE_THING_NAME": self.thing_name,
            "AWS_IOT_CORE_CERT": self.cert_path,
            "AWS_IOT_CORE_PRIVATE_KEY": self.key_path,
            "AWS_KVS_CACERT_PATH": self.cacert,
            "AWS_KVS_LOG_LEVEL": str(self.log_level),
            "KVS_ICE_TRANSPORT_POLICY": self.ice_transport_policy,
            # kvssink の libkvsCommonLws.so(別の版)より先に WebRTC SDK のものを読む
            "LD_LIBRARY_PATH": lib,
        })
        for k, v in (("CC_MAIN_RTSP_URL", self.main_rtsp_url), ("CC_SUB_RTSP_URL", self.sub_rtsp_url),
                     ("CC_RTSP_USER", self.username), ("CC_RTSP_PASS", self.password)):
            if v:
                env[k] = v
            else:
                env.pop(k, None)
        # サンプル共通処理は AWS_IOT_CORE_CERTIFICATE_ID があるとチャネル名を上書きする
        env.pop("AWS_IOT_CORE_CERTIFICATE_ID", None)
        return env

    def same_media(self, other: WebRtcSpec | None) -> bool:
        """RTSP とコーデックが同じか(違えば master を起動し直す)。"""
        if other is None:
            return False
        keys = ("channel", "main_rtsp_url", "main_codec", "sub_rtsp_url", "sub_codec", "username", "password")
        return all(getattr(self, k) == getattr(other, k) for k in keys)

    def describe(self) -> str:
        d = asdict(self)
        d["password"] = "***" if self.password else ""
        return json.dumps({k: d[k] for k in ("channel", "main_rtsp_url", "main_codec", "sub_rtsp_url", "sub_codec")},
                          ensure_ascii=False)


class MasterSupervisor:
    """master の子プロセスを 1 つ動かし続ける。落ちたらバックオフして起動し直す(runner と同じ流儀)。

    spec_factory は起動のたびに呼ばれ、RTSP の URL などを返す(None なら少し待って呼び直す)。
    """

    HEALTHY_AFTER_S = 30.0
    STATS_STALE_S = 35.0  # 統計(10 秒ごと)がこれより古ければ視聴者数を 0 とみなす

    def __init__(self, camera_id: str, spec_factory: Callable[[int], Awaitable[WebRtcSpec | None]],
                 on_change: Callable[[], None], backoff: Backoff):
        self.camera_id = camera_id
        self.spec_factory = spec_factory
        self.on_change = on_change
        self.backoff = backoff
        self.proc: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.spec: WebRtcSpec | None = None
        self.state = STATE_STARTING
        self.restarts = 0
        self.last_error: str | None = None
        self.last_exit: dict | None = None
        self.stats: dict = {}
        self._stats_at = 0.0
        self.sent_bytes_prev = 0  # 前の子プロセスまでの送信バイト数の累計

    # --- 制御 -------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def start(self) -> None:
        if not self.running:
            self._stop.clear()
            self.state = STATE_STARTING
            self.task = asyncio.create_task(self._loop(), name=f"webrtc-{self.camera_id}")

    async def stop(self) -> None:
        self._stop.set()
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 10)
            except TimeoutError:
                self.proc.kill()
        if self.task:
            try:
                await asyncio.wait_for(self.task, 15)
            except (TimeoutError, asyncio.CancelledError):
                self.task.cancel()
        self.task = None
        self._clear_stats()

    # --- 状態 -------------------------------------------------------------
    def _clear_stats(self) -> None:
        if self.stats:
            self.sent_bytes_prev += int(self.stats.get("sent_bytes") or 0)
        self.stats = {}
        self._stats_at = 0.0

    def _fresh_stats(self) -> dict:
        if self.stats and time.monotonic() - self._stats_at <= self.STATS_STALE_S:
            return self.stats
        return {}

    @property
    def viewers(self) -> int:
        return int(self._fresh_stats().get("viewers") or 0)

    def viewers_by_codec(self) -> dict[str, int]:
        out = {"H265": 0, "H264": 0}
        for st in (self._fresh_stats().get("streams") or {}).values():
            c = st.get("codec")
            if c in out:
                out[c] += int(st.get("viewers") or 0)
        return out

    def status(self) -> dict:
        st = self._fresh_stats()
        streams = {name: {k: s.get(k) for k in ("codec", "running", "viewers", "in_kbps", "sent_kbps", "restarts")}
                   for name, s in (st.get("streams") or {}).items() if s.get("available")}
        return {
            "state": self.state,
            "viewers": self.viewers,
            "viewers_by_codec": self.viewers_by_codec(),
            "sent_kbps": st.get("sent_kbps", 0.0),
            "sent_bytes": self.sent_bytes_prev + int(self.stats.get("sent_bytes") or 0),
            "streams": streams,
            "restarts": self.restarts,
            "error": self.last_error,
            "last_exit": self.last_exit,
        }

    def _set_state(self, state: str) -> None:
        if state != self.state:
            log.info("[%s] WebRTC: %s → %s", self.camera_id, self.state, state)
            self.state = state
            self.on_change()

    # --- 実行 -------------------------------------------------------------
    async def _loop(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                spec = await self.spec_factory(attempt)
            except Exception as e:  # noqa: BLE001
                spec = None
                self.last_error = f"resolve: {e}"
            if spec is None:
                # RTSP の URL がまだ分からない(録画側の ONVIF の解決待ちなど)
                await self._sleep(min(self.backoff.next_delay(), 10))
                attempt += 1
                continue
            self.spec = spec
            t0 = time.monotonic()
            code = await self._run_once(spec)
            ran = time.monotonic() - t0
            self._clear_stats()
            if self._stop.is_set():
                break
            if ran >= self.HEALTHY_AFTER_S:
                self.backoff.reset()
            self.restarts += 1
            attempt += 1
            delay = self.backoff.next_delay()
            self.last_exit = {"code": code, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                              "reason": self.last_error}
            log.warning("[%s] WebRTC の master が終了(code=%s, %.0f 秒動作)。%.0f 秒後に再起動",
                        self.camera_id, code, ran, delay)
            self._set_state(STATE_ERROR)
            await self._sleep(delay)
            if not self._stop.is_set():
                self._set_state(STATE_STARTING)

    async def _sleep(self, s: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), s)
        except TimeoutError:
            pass

    async def _run_once(self, spec: WebRtcSpec) -> int:
        log.info("[%s] WebRTC の master を起動: %s", self.camera_id, spec.describe())
        try:
            self.proc = await asyncio.create_subprocess_exec(
                spec.binary, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                env=spec.env(), limit=1 << 20)
        except OSError as e:
            self.last_error = f"起動できない: {e}"
            return -1
        assert self.proc.stdout is not None
        async for line in self.proc.stdout:
            text = line.decode(errors="replace").rstrip()
            if text.startswith("{"):
                try:
                    self._handle(json.loads(text))
                    continue
                except json.JSONDecodeError:
                    pass
            if text:
                # SDK のログ(AWS_KVS_LOG_LEVEL)。多いので DEBUG。警告とエラーだけ INFO で出す
                lvl = logging.INFO if (" WARN " in text or " ERROR " in text or "[cc]" in text) else logging.DEBUG
                log.log(lvl, "[%s] webrtc: %s", self.camera_id, text)
        return await self.proc.wait()

    def _handle(self, ev: dict) -> None:
        if ev.get("cc_stats"):
            before = (self.viewers, self.viewers_by_codec())
            self.stats = ev
            self._stats_at = time.monotonic()
            if (self.viewers, self.viewers_by_codec()) != before:
                self.on_change()
            log.debug("[%s] webrtc stats %s", self.camera_id, ev)
            return
        kind = ev.get("cc_event")
        if kind == "ready":
            self.last_error = None
            self._set_state(STATE_CONNECTED)
        elif kind == "signaling":
            st = str(ev.get("state", ""))
            if st == "Connected":
                self.last_error = None
                self._set_state(STATE_CONNECTED)
            elif st in ("Disconnected", "Connecting", "Get Security Credentials", "Get Channel Endpoint",
                        "Get ICE Server Configuration", "Describe Channel") and self.state == STATE_CONNECTED:
                self._set_state(STATE_RECONNECTING)
        elif kind == "viewer":
            log.info("[%s] WebRTC の視聴者: %s %s(%s)", self.camera_id, ev.get("peer"), ev.get("codec"), ev.get("stream"))
        elif kind == "reject":
            log.warning("[%s] WebRTC: 受けられる映像がない offer を断った(%s)", self.camera_id, ev.get("peer"))
        elif kind == "rtsp_error":
            log.warning("[%s] WebRTC の RTSP(%s): %s", self.camera_id, ev.get("stream"), ev.get("message"))
        elif kind == "exit":
            self.last_error = f"master が終了した(status {ev.get('status')})"
        else:
            log.info("[%s] webrtc %s", self.camera_id, ev)
