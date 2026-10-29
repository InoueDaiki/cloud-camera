"""エミュレーター本体: カメラごとの ONVIF HTTP サーバー、WS-Discovery、MediaMTX / FFmpeg の監視、操作画面をまとめて起動する。

live: ソースのカメラは、MediaMTX の live/名前 に映像が来ているか(API の ready)を 1 秒ごとに見て、
来ていれば live/名前 を、来ていなければ代替映像(cfg.live_fallback と「NO SIGNAL」の焼き込み)を流す。
切り替えは FFmpeg の再起動で行う(数秒途切れる。出力の解像度・コーデック・GOP は変わらない)。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

import aiohttp
from aiohttp import web

from .config import CameraConfig, EmulatorConfig, validate_source
from .discovery import DiscoveryServer
from .ptz import PtzController
from .services import CameraServices, RequestContext
from .soap import NS, SoapFault, content_type, envelope, fault_envelope, parse_request
from .streams import ffmpeg_command, live_path, snapshot_path, write_mediamtx_config, write_overlay_text
from .supervisor import ManagedProcess, wait_tcp

log = logging.getLogger(__name__)


def _request_host(request: web.Request, cfg: EmulatorConfig) -> str:
    if cfg.advertise_host:
        return cfg.advertise_host
    host = request.host or ""
    if host.startswith("["):  # IPv6
        return host[: host.index("]") + 1]
    return host.rsplit(":", 1)[0] if ":" in host else host or "127.0.0.1"


def _live_name(source: str) -> str | None:
    return source[len("live:") :] if source.startswith("live:") else None


class Emulator:
    def __init__(self, cfg: EmulatorConfig):
        self.cfg = cfg
        # 疑似 PTZ の状態はカメラごと(メモリ上だけ)
        self.ptz = {cam.index: PtzController(cam.ptz) for cam in cfg.cameras if cam.ptz.enabled}
        self.services = {
            cam.index: CameraServices(cfg, cam, self._on_encoder_change, self.ptz.get(cam.index)) for cam in cfg.cameras
        }
        self.mediamtx: ManagedProcess | None = None
        self.ffmpeg: dict[int, ManagedProcess] = {}
        self.ptz_links: dict = {}  # index → PtzVideoLink(映像を出すときだけ)
        self.discovery: DiscoveryServer | None = None
        self.runners: list[web.AppRunner] = []
        # live: の状態。active_source は FFmpeg を起動したときのソース(代替映像のときはそのテストパターン)
        self.active_source: dict[int, str] = {}
        self.live_ready: dict[str, bool] = {}
        self._switching: set[int] = set()
        self._http: aiohttp.ClientSession | None = None
        self._live_task: asyncio.Task | None = None

    # ---- HTTP ----

    def _make_app(self, cam: CameraConfig) -> web.Application:
        svc = self.services[cam.index]
        cfg = self.cfg

        async def soap_handler(request: web.Request) -> web.Response:
            data = await request.read()
            soap_ns = NS["s12"]
            try:
                req = parse_request(data)
                soap_ns = req.soap_ns
                sockname = request.transport.get_extra_info("sockname") if request.transport else None
                ctx = RequestContext(
                    host=_request_host(request, cfg),
                    local_ip=sockname[0] if sockname else "127.0.0.1",
                )
                body = svc.handle(req, ctx)
                log.debug("%s %s %s → OK", cam.name, request.path, req.operation)
                return web.Response(body=envelope(body, soap_ns), content_type=None,
                                    headers={"Content-Type": content_type(soap_ns)})
            except SoapFault as f:
                log.info("%s %s → Fault %s %s", cam.name, request.path, f.subcodes, f.reason)
                return web.Response(status=f.http_status, body=fault_envelope(f, soap_ns),
                                    headers={"Content-Type": content_type(soap_ns)})

        async def snapshot_handler(request: web.Request) -> web.StreamResponse:
            path = snapshot_path(cfg, cam)
            try:
                data = path.read_bytes()
            except FileNotFoundError:
                return web.Response(status=503, text="snapshot not ready\n")
            return web.Response(body=data, content_type="image/jpeg", headers={"Cache-Control": "no-store"})

        async def health_handler(request: web.Request) -> web.Response:
            return web.json_response(self.status())

        app = web.Application(client_max_size=1024 * 1024)
        app.router.add_post("/onvif/{tail:.*}", soap_handler)
        app.router.add_get("/onvif/snapshot.jpg", snapshot_handler)
        app.router.add_get("/healthz", health_handler)
        return app

    def status(self) -> dict:
        cams = {}
        for cam in self.cfg.cameras:
            p = self.ffmpeg.get(cam.index)
            ptz = self.ptz.get(cam.index)
            link = self.ptz_links.get(cam.index)
            ptz_status = None
            if ptz is not None:
                pan, tilt, zoom = ptz.position()
                ptz_status = {
                    "pan": round(pan, 4), "tilt": round(tilt, 4), "zoom": round(zoom, 4),
                    "magnification": round(ptz.magnification(), 3),
                    "moving": ptz.any_moving,
                    "presets": len(ptz.presets),
                    "commands_sent": link.commands_sent if link else 0,
                    "command_errors": link.command_errors if link else 0,
                }
            live = _live_name(cam.source)
            cams[cam.name] = {
                "index": cam.index,
                "path": cam.path_prefix,
                "onvif_port": cam.onvif_port,
                "source": cam.source,
                "active_source": self.active_source.get(cam.index),
                "live": {"path": live_path(live), "ready": self.live_ready.get(live, False)} if live else None,
                "main": vars(cam.main),
                "sub": vars(cam.sub),
                "ffmpeg_running": bool(p and p.running),
                "ffmpeg_restarts": p.restarts if p else 0,
                "frame": p.progress.get("frame") if p else None,
                "fps": p.progress.get("fps") if p else None,
                "speed": p.progress.get("speed") if p else None,
                "ptz": ptz_status,
            }
        return {
            "streams_enabled": self.cfg.streams_enabled,
            "mediamtx_running": bool(self.mediamtx and self.mediamtx.running),
            "cameras": cams,
        }

    # ---- 映像 ----

    def _on_encoder_change(self, cam: CameraConfig, which: str) -> None:
        p = self.ffmpeg.get(cam.index)
        if p is not None:
            asyncio.get_running_loop().create_task(p.restart())

    def camera(self, index: int) -> CameraConfig:
        for cam in self.cfg.cameras:
            if cam.index == index:
                return cam
        raise KeyError(index)

    async def set_source(self, index: int, source: str) -> CameraConfig:
        """映像ソースを実行中に変える(メモリ上だけ。コンテナを再起動すると設定に戻る)。FFmpeg を再起動する。"""
        cam = self.camera(index)
        source = validate_source(source)
        if source == cam.source:
            return cam
        log.info("%s: 映像ソースを %s → %s", cam.name, cam.source, source)
        cam.source = source
        p = self.ffmpeg.get(index)
        if p is not None:
            self._switching.add(index)
            await p.restart()
        return cam

    async def _probe_live(self, name: str) -> bool:
        """MediaMTX の live/名前 に映像が来ているか(API の paths/get の ready)。"""
        assert self._http is not None
        try:
            async with self._http.get(f"http://127.0.0.1:9997/v3/paths/get/{live_path(name)}") as r:
                ready = r.status == 200 and bool((await r.json()).get("ready"))
        except (aiohttp.ClientError, TimeoutError, ValueError):
            ready = False
        if self.live_ready.get(name) != ready:
            log.info("live/%s: %s", name, "映像あり" if ready else "映像なし")
        self.live_ready[name] = ready
        return ready

    async def _choose_source(self, cam: CameraConfig) -> tuple[str, str | None]:
        """FFmpeg に流させるソースと、焼き込みの注記。"""
        live = _live_name(cam.source)
        if live is None or await self._probe_live(live):
            return cam.source, None
        return self.cfg.live_fallback, f"NO SIGNAL  waiting for {live_path(live)}"

    async def _watch_live(self) -> None:
        """live: のカメラで、映像の有無と流しているソースが食い違ったら FFmpeg を再起動する。"""
        while True:
            await asyncio.sleep(1)
            for cam in self.cfg.cameras:
                live = _live_name(cam.source)
                p = self.ffmpeg.get(cam.index)
                if live is None or p is None or cam.index in self._switching:
                    continue
                ready = await self._probe_live(live)
                showing_live = self.active_source.get(cam.index) == cam.source
                if ready != showing_live:
                    self._switching.add(cam.index)
                    await p.restart()

    def _start_streams(self) -> None:
        cfg = self.cfg
        mtx_conf = write_mediamtx_config(cfg)
        self.mediamtx = ManagedProcess("mediamtx", lambda: [cfg.mediamtx_bin, str(mtx_conf)])
        self.mediamtx.start()
        for cam in cfg.cameras:
            link = None
            if cam.index in self.ptz:
                from .ptz_link import PtzVideoLink  # pyzmq は映像を出すときだけ使う

                link = PtzVideoLink(cfg, cam, self.ptz[cam.index])
                self.ptz_links[cam.index] = link

            async def prepare(cam=cam, link=link) -> None:
                if not await wait_tcp("127.0.0.1", cfg.rtsp_port, timeout=30):
                    raise RuntimeError("MediaMTX の RTSP ポートが開きません")
                source, note = await self._choose_source(cam)
                self.active_source[cam.index] = source
                self._switching.discard(cam.index)
                if link is not None:
                    link.note = note
                write_overlay_text(cfg, cam, link.overlay_line() if link else None, note)

            def argv(cam=cam) -> list[str]:
                # 再起動しても今の切り出し窓から始める
                ptz = self.ptz.get(cam.index)
                return ffmpeg_command(cfg, cam, ptz.window() if ptz else None, self.active_source.get(cam.index))

            p = ManagedProcess(
                f"ffmpeg-{cam.path_prefix}",
                argv,
                watchdog_s=20,
                progress_marker=b"progress=",
                before_start=prepare,
            )
            self.ffmpeg[cam.index] = p
            p.start()
            if link is not None:
                link.start()
        self._live_task = asyncio.create_task(self._watch_live(), name="watch-live")

    # ---- 起動・停止 ----

    async def start(self) -> None:
        self.cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
        self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3))
        for cam in self.cfg.cameras:
            runner = web.AppRunner(self._make_app(cam), access_log=None)
            await runner.setup()
            await web.TCPSite(runner, "0.0.0.0", cam.onvif_port).start()
            self.runners.append(runner)
            log.info(
                "%s: ONVIF http://0.0.0.0:%d/onvif/device_service、RTSP /%s/main (H.265 %dx%d@%d) /%s/sub (H.264 %dx%d)",
                cam.name, cam.onvif_port, cam.path_prefix, cam.main.width, cam.main.height, cam.main.fps,
                cam.path_prefix, cam.sub.width, cam.sub.height,
            )
        if self.cfg.streams_enabled:
            self._start_streams()
        if self.cfg.ui_port:
            from .ui import make_ui_app

            runner = web.AppRunner(make_ui_app(self), access_log=None)
            await runner.setup()
            await web.TCPSite(runner, "0.0.0.0", self.cfg.ui_port).start()
            self.runners.append(runner)
            log.info("操作画面 http://0.0.0.0:%d/", self.cfg.ui_port)
        if self.cfg.discovery_enabled:
            self.discovery = DiscoveryServer(self.cfg, lambda cam: self.services[cam.index].scope_uris())
            await self.discovery.start()

    async def stop(self) -> None:
        if self.discovery:
            self.discovery.stop()
        if self._live_task:
            self._live_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._live_task
        await asyncio.gather(*(link.stop() for link in self.ptz_links.values()), return_exceptions=True)
        await asyncio.gather(*(p.stop() for p in self.ffmpeg.values()), return_exceptions=True)
        if self.mediamtx:
            await self.mediamtx.stop()
        for r in self.runners:
            await r.cleanup()
        if self._http:
            await self._http.close()


async def run(cfg: EmulatorConfig) -> None:
    emu = Emulator(cfg)
    await emu.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    log.info("停止します")
    await emu.stop()
