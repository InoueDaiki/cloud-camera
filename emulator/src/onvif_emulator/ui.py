"""操作画面(cfg.ui_port)。カメラごとの映像ソースの切り替えと、ブラウザのカメラの取り込み(WHIP の中継)。

- GET  /                              画面(ui/index.html)
- GET  /api/state                     カメラの一覧と状態、選べるソース(テストパターン、media_dir の動画)
- PUT  /api/cameras/{index}/source    {"source": "..."} で映像ソースを変える(FFmpeg を再起動する)
- GET  /api/cameras/{index}/snapshot.jpg
- POST /whip/{name}                   WHIP の offer を MediaMTX(127.0.0.1)の live/{name}/whip に中継する
- PATCH / DELETE /whip/{name}/{id}    WHIP のセッション(trickle ICE、終了)

WHIP の HTTP を中継するのは、MediaMTX への配信をコンテナ内(127.0.0.1)からに限ったまま、画面と同じオリジンで
送れるようにするため(CORS が要らない)。映像そのもの(ICE / SRTP)はブラウザから MediaMTX の ICE ポートに直接届く。
ブラウザのカメラ(getUserMedia)は安全なオリジンでしか使えないので、画面は http://localhost:… で開く。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

import aiohttp
from aiohttp import web

from .config import LIVE_NAME_RE, TEST_SOURCES
from .streams import live_path, snapshot_path

if TYPE_CHECKING:
    from .app import Emulator

log = logging.getLogger(__name__)

INDEX_HTML = Path(__file__).parent / "ui" / "index.html"
MEDIA_SUFFIXES = {".mp4", ".mkv", ".mov", ".webm", ".m4v", ".ts"}

# WHIP の要求・応答で中継するヘッダー
_WHIP_REQ_HEADERS = ("Content-Type", "If-Match", "Accept")
_WHIP_RESP_HEADERS = ("Content-Type", "ETag", "Link", "Accept-Patch")


def list_media(media_dir: Path) -> list[str]:
    try:
        files = [p for p in media_dir.iterdir() if p.is_file() and p.suffix.lower() in MEDIA_SUFFIXES]
    except OSError:
        return []
    return sorted(str(p) for p in files)


def make_ui_app(emu: Emulator) -> web.Application:
    cfg = emu.cfg
    whip_base = f"http://127.0.0.1:{cfg.webrtc_port}"

    def camera_or_404(request: web.Request):
        try:
            return emu.camera(int(request.match_info["index"]))
        except (KeyError, ValueError):
            raise web.HTTPNotFound(text="no such camera\n") from None

    async def index(request: web.Request) -> web.Response:
        return web.Response(body=INDEX_HTML.read_bytes(), content_type="text/html", charset="utf-8",
                            headers={"Cache-Control": "no-store"})

    async def state(request: web.Request) -> web.Response:
        st = emu.status()
        return web.json_response({
            "streams_enabled": st["streams_enabled"],
            "mediamtx_running": st["mediamtx_running"],
            "rtsp_port": cfg.rtsp_port,
            "live_fallback": cfg.live_fallback,
            "cameras": [{"name": name, **c} for name, c in st["cameras"].items()],
            "sources": {"patterns": sorted(TEST_SOURCES), "files": list_media(cfg.media_dir)},
        })

    async def set_source(request: web.Request) -> web.Response:
        cam = camera_or_404(request)
        try:
            body = await request.json()
            await emu.set_source(cam.index, str(body["source"]))
        except (ValueError, KeyError, TypeError) as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response({"name": cam.name, "source": cam.source})

    async def snapshot(request: web.Request) -> web.Response:
        cam = camera_or_404(request)
        try:
            data = snapshot_path(cfg, cam).read_bytes()
        except FileNotFoundError:
            return web.Response(status=503, text="snapshot not ready\n")
        return web.Response(body=data, content_type="image/jpeg", headers={"Cache-Control": "no-store"})

    async def whip(request: web.Request) -> web.Response:
        name = request.match_info["name"]
        if not LIVE_NAME_RE.match(name):
            return web.Response(status=400, text="bad name\n")
        session = request.match_info.get("session", "")
        if session and not re.fullmatch(r"[A-Za-z0-9_-]+", session):
            return web.Response(status=400, text="bad session\n")
        url = f"{whip_base}/{live_path(name)}/whip" + (f"/{session}" if session else "")
        headers = {h: request.headers[h] for h in _WHIP_REQ_HEADERS if h in request.headers}
        body = await request.read()
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as http:
                async with http.request(request.method, url, data=body, headers=headers) as r:
                    data = await r.read()
                    out = {h: r.headers[h] for h in _WHIP_RESP_HEADERS if h in r.headers}
                    loc = r.headers.get("Location")
                    if loc:
                        # MediaMTX の /live/{name}/whip/{id} を、この画面の /whip/{name}/{id} に読み替える
                        out["Location"] = re.sub(r"^.*/whip/", f"/whip/{name}/", loc)
                    if request.method != "PATCH":
                        log.info("WHIP: %s %s → %d", request.method, live_path(name), r.status)
                    return web.Response(status=r.status, body=data, headers=out)
        except aiohttp.ClientError as e:
            return web.Response(status=502, text=f"MediaMTX に届きません: {e}\n")

    app = web.Application(client_max_size=1024 * 1024)
    app.router.add_get("/", index)
    app.router.add_get("/api/state", state)
    app.router.add_put("/api/cameras/{index}/source", set_source)
    app.router.add_get("/api/cameras/{index}/snapshot.jpg", snapshot)
    app.router.add_post("/whip/{name}", whip)
    app.router.add_route("PATCH", "/whip/{name}/{session}", whip)
    app.router.add_route("DELETE", "/whip/{name}/{session}", whip)
    return app
