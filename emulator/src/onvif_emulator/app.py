"""エミュレーター本体: カメラごとの ONVIF HTTP サーバー、WS-Discovery、MediaMTX / FFmpeg の監視をまとめて起動する。"""

from __future__ import annotations

import asyncio
import logging
import signal

from aiohttp import web

from .config import CameraConfig, EmulatorConfig
from .discovery import DiscoveryServer
from .ptz import PtzController
from .services import CameraServices, RequestContext
from .soap import NS, SoapFault, content_type, envelope, fault_envelope, parse_request
from .streams import ffmpeg_command, snapshot_path, write_mediamtx_config, write_overlay_text
from .supervisor import ManagedProcess, wait_tcp

log = logging.getLogger(__name__)


def _request_host(request: web.Request, cfg: EmulatorConfig) -> str:
    if cfg.advertise_host:
        return cfg.advertise_host
    host = request.host or ""
    if host.startswith("["):  # IPv6
        return host[: host.index("]") + 1]
    return host.rsplit(":", 1)[0] if ":" in host else host or "127.0.0.1"


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
            cams[cam.name] = {
                "path": cam.path_prefix,
                "onvif_port": cam.onvif_port,
                "source": cam.source,
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
                write_overlay_text(cfg, cam, link.overlay_line() if link else None)
                if not await wait_tcp("127.0.0.1", cfg.rtsp_port, timeout=30):
                    raise RuntimeError("MediaMTX の RTSP ポートが開きません")

            def argv(cam=cam) -> list[str]:
                # 再起動しても今の切り出し窓から始める
                ptz = self.ptz.get(cam.index)
                return ffmpeg_command(cfg, cam, ptz.window() if ptz else None)

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

    # ---- 起動・停止 ----

    async def start(self) -> None:
        self.cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
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
        if self.cfg.discovery_enabled:
            self.discovery = DiscoveryServer(self.cfg, lambda cam: self.services[cam.index].scope_uris())
            await self.discovery.start()

    async def stop(self) -> None:
        if self.discovery:
            self.discovery.stop()
        await asyncio.gather(*(link.stop() for link in self.ptz_links.values()), return_exceptions=True)
        await asyncio.gather(*(p.stop() for p in self.ffmpeg.values()), return_exceptions=True)
        if self.mediamtx:
            await self.mediamtx.stop()
        for r in self.runners:
            await r.cleanup()


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
