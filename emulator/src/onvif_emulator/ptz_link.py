"""疑似 PTZ の状態を FFmpeg に反映するループ(カメラごとに 1 つの asyncio タスク)。

- 動いている間はフレーム間隔ごとに切り出し窓を計算し、変わった値だけを zmq で crop に送る
- PTZ の操作があればすぐに起きる(待ち時間なしで次のフレームに反映する)
- 止まっている間も 1 秒ごとに全部の値を送り直す(FFmpeg が再起動したときに状態を合わせ直すため。値が同じなら映像は変わらない)
- 焼き込みの 4 行目(pan / tilt / zoom)のテキストファイルも同じタイミングで書き換える(note があれば最後の行に残す)

zmq フィルターは REP ソケットなので、こちらは DEALER で [空フレーム, コマンド] を送る(応答を待たずに複数送れる)。
IMMEDIATE を付け、FFmpeg が起動していない間のコマンドは溜めずに捨てる(起動時の値はコマンド行で渡す)。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

import zmq

from .config import CameraConfig, EmulatorConfig
from .ptz import PtzController
from .streams import PTZ_CROP_FILTER, ptz_crop_commands, ptz_overlay_line, ptz_zmq_path, write_overlay_text

log = logging.getLogger(__name__)

RESYNC_INTERVAL_S = 1.0


class PtzVideoLink:
    def __init__(self, cfg: EmulatorConfig, cam: CameraConfig, ptz: PtzController):
        self.cfg = cfg
        self.cam = cam
        self.ptz = ptz
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._ctx: zmq.Context | None = None
        self._sock: zmq.Socket | None = None
        self._sent: dict[str, str] = {}
        self._overlay: tuple[str, str | None] | None = None
        self.note: str | None = None  # 焼き込みの最後の行(app.py が live: の状態に合わせて変える)
        self._last_resync = 0.0
        self.commands_sent = 0
        self.command_errors = 0
        self.last_error: str | None = None
        ptz.listeners.append(self.kick)

    def kick(self) -> None:
        """PTZ の操作があったときに呼ぶ(すぐに反映する)。"""
        self._wake.set()

    def overlay_line(self) -> str:
        pan, tilt, zoom = self.ptz.position()
        return ptz_overlay_line(self.ptz.window(), pan, tilt, zoom, self.ptz.any_moving)

    def start(self) -> None:
        self._ctx = zmq.Context.instance()
        self._sock = self._ctx.socket(zmq.DEALER)
        self._sock.setsockopt(zmq.IMMEDIATE, 1)
        self._sock.setsockopt(zmq.LINGER, 0)
        self._sock.setsockopt(zmq.SNDHWM, 64)
        self._sock.connect(f"ipc://{ptz_zmq_path(self.cfg, self.cam)}")
        self._task = asyncio.create_task(self._run(), name=f"ptz-{self.cam.path_prefix}")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self._sock is not None:
            self._sock.close(0)

    def _send(self, cmd: str, arg: str) -> bool:
        assert self._sock is not None
        try:
            self._sock.send_multipart([b"", f"{PTZ_CROP_FILTER} {cmd} {arg}".encode()], flags=zmq.NOBLOCK)
        except zmq.Again:
            return False  # FFmpeg がまだ bind していない(起動中・再起動中)
        self.commands_sent += 1
        return True

    def _drain_replies(self) -> None:
        assert self._sock is not None
        while True:
            try:
                frames = self._sock.recv_multipart(flags=zmq.NOBLOCK)
            except zmq.Again:
                return
            reply = frames[-1].decode(errors="replace")
            if not reply.startswith("0 "):
                self.command_errors += 1
                self.last_error = reply
                log.warning("[%s] PTZ のコマンドが失敗: %s", self.cam.name, reply)

    def sync(self, force: bool = False) -> None:
        """今の窓と焼き込みを FFmpeg に送る(変わった値だけ。force なら全部)。"""
        cmds = ptz_crop_commands(self.ptz.window())
        # w(倍率)を先に送る。x / y は crop がフレームごとに評価するので順番の影響は小さい
        for key in ("w", "x", "y"):
            if force or self._sent.get(key) != cmds[key]:
                if self._send(key, cmds[key]):
                    self._sent[key] = cmds[key]
                else:
                    self._sent.pop(key, None)
        overlay = (self.overlay_line(), self.note)
        if force or overlay != self._overlay:
            try:
                write_overlay_text(self.cfg, self.cam, *overlay)
                self._overlay = overlay
            except OSError as e:
                log.warning("[%s] 焼き込みのテキストを書けません: %s", self.cam.name, e)
        self._drain_replies()

    async def _run(self) -> None:
        frame_s = 1.0 / max(1, self.cam.main.fps)
        while True:
            now = time.monotonic()
            force = now - self._last_resync >= RESYNC_INTERVAL_S
            if force:
                self._last_resync = now
            try:
                self.sync(force)
            except Exception:  # ループは止めない
                log.exception("[%s] PTZ の反映に失敗", self.cam.name)
            # 動いている間はフレームごと、止まっていれば次の再送か操作まで待つ
            timeout = frame_s if self.ptz.any_moving else RESYNC_INTERVAL_S
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout)
