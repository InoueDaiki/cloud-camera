"""子プロセス(MediaMTX、FFmpeg)の監視と再起動。

- 終了したら指数バックオフ(1 秒〜30 秒)で再起動する。30 秒以上動いていたらバックオフを戻す
- FFmpeg は -progress の出力が一定時間来なければ固まったとみなして kill する(ウォッチドッグ)
- restart() で設定変更時に即座に再起動できる
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time
from collections.abc import Callable

log = logging.getLogger(__name__)


class ManagedProcess:
    def __init__(
        self,
        name: str,
        argv: Callable[[], list[str]],
        *,
        watchdog_s: float | None = None,
        progress_marker: bytes | None = None,
        before_start: Callable[[], object] | None = None,
    ):
        self.name = name
        self.argv = argv  # 起動のたびに呼ぶ(設定変更を反映するため)
        self.watchdog_s = watchdog_s
        self.progress_marker = progress_marker
        self.before_start = before_start
        self.proc: asyncio.subprocess.Process | None = None
        self.restarts = 0
        self.last_progress = 0.0
        self.progress: dict[str, str] = {}
        self._stopping = False
        self._restart_now = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"supervise-{self.name}")

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def restart(self) -> None:
        """今のプロセスを止めて、待たずに起動し直す。"""
        log.info("[%s] 再起動を要求", self.name)
        self._restart_now.set()
        await self._terminate()

    async def stop(self) -> None:
        self._stopping = True
        await self._terminate()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _terminate(self, timeout: float = 5.0) -> None:
        p = self.proc
        if p is None or p.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            p.send_signal(signal.SIGINT if "ffmpeg" in self.name else signal.SIGTERM)
        try:
            await asyncio.wait_for(p.wait(), timeout)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                p.kill()
            await p.wait()

    async def _pump(self, stream: asyncio.StreamReader, is_stdout: bool) -> None:
        while True:
            line = await stream.readline()
            if not line:
                return
            text = line.decode(errors="replace").rstrip()
            if is_stdout and self.progress_marker is not None:
                # FFmpeg の -progress(key=value)
                k, _, v = text.partition("=")
                self.progress[k] = v
                if line.startswith(self.progress_marker):
                    self.last_progress = time.monotonic()
                continue
            if text:
                log.info("[%s] %s", self.name, text)

    async def _watchdog(self) -> None:
        assert self.watchdog_s
        while self.running:
            await asyncio.sleep(1)
            if time.monotonic() - self.last_progress > self.watchdog_s:
                log.warning("[%s] %.0f 秒間進捗がないので kill します", self.name, self.watchdog_s)
                with contextlib.suppress(ProcessLookupError):
                    self.proc.kill()
                return

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stopping:
            self._restart_now.clear()
            try:
                if self.before_start:
                    r = self.before_start()
                    if asyncio.iscoroutine(r):
                        await r
                argv = self.argv()
            except Exception:
                log.exception("[%s] 起動準備に失敗", self.name)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue
            log.info("[%s] 起動: %s", self.name, " ".join(argv))
            started = time.monotonic()
            self.last_progress = started
            self.progress = {}  # 前のプロセスの frame / fps を残さない
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
            except OSError:
                log.exception("[%s] 起動に失敗", self.name)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue
            tasks = [
                asyncio.create_task(self._pump(self.proc.stdout, True)),
                asyncio.create_task(self._pump(self.proc.stderr, False)),
            ]
            if self.watchdog_s:
                tasks.append(asyncio.create_task(self._watchdog()))
            code = await self.proc.wait()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self._stopping:
                return
            uptime = time.monotonic() - started
            if self._restart_now.is_set():
                backoff = 1.0
                continue
            self.restarts += 1
            if uptime > 30:
                backoff = 1.0
            log.warning("[%s] 終了 (code=%s、%.1f 秒稼働)。%.0f 秒後に再起動", self.name, code, uptime, backoff)
            try:
                await asyncio.wait_for(self._restart_now.wait(), backoff)
            except TimeoutError:
                pass
            backoff = min(backoff * 2, 30)


async def wait_tcp(host: str, port: int, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            _, w = await asyncio.open_connection(host, port)
            w.close()
            await w.wait_closed()
            return True
        except OSError:
            await asyncio.sleep(0.2)
    return False
