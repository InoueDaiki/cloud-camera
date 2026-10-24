"""パイプラインの仕様と、子プロセス(runner)の監視・再起動。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass

log = logging.getLogger(__name__)


@dataclass
class PipelineSpec:
    camera_id: str
    rtsp_url: str
    codec: str = "H265"
    username: str = ""
    password: str = ""
    sink: str = "kvs"  # kvs / fake / file
    stream_name: str = ""
    region: str = "ap-northeast-1"
    # IoT 認証情報プロバイダー(kvssink の iot-certificate)
    iot_credential_endpoint: str = ""
    cert_path: str = ""
    key_path: str = ""
    ca_path: str = ""
    role_alias: str = ""
    thing_name: str = ""
    retention_hours: int = 1
    storage_size_mb: int = 64
    log_config: str = "/opt/kvs/kvs_log_configuration"
    output_path: str = ""
    latency_ms: int = 200
    protocols: str = "tcp"
    framerate: int = 0
    stall_timeout_s: float = 15.0
    stats_interval_s: float = 5.0

    @property
    def codec_upper(self) -> str:
        return self.codec.upper().replace(".", "")

    @property
    def codec_lower(self) -> str:
        c = self.codec_upper.lower()
        if c not in ("h265", "h264"):
            raise ValueError(f"対応していないコーデック: {self.codec}")
        return c

    def protocols_flag(self) -> int:
        # GstRTSPLowerTrans: udp=1, udp-mcast=2, tcp=4
        bits = {"udp": 1, "udp-mcast": 2, "tcp": 4}
        return sum(bits[p.strip()] for p in self.protocols.split("+") if p.strip())

    def iot_certificate_structure(self) -> str:
        def q(v: str) -> str:
            return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'

        return (
            "iot-certificate"
            f",endpoint={q(self.iot_credential_endpoint)}"
            f",cert-path={q(self.cert_path)}"
            f",key-path={q(self.key_path)}"
            f",ca-path={q(self.ca_path)}"
            f",role-aliases={q(self.role_alias)}"
            f",iot-thing-name={q(self.thing_name)}"
        )

    def describe(self) -> str:
        """ログ用のパイプライン表現(gst-launch 相当。パスワードは伏せる)。"""
        c = self.codec_lower
        fmt = "hvc1" if c == "h265" else "avc"
        head = (f"rtspsrc location={self.rtsp_url} protocols={self.protocols} latency={self.latency_ms}"
                f"{' user-id=' + self.username + ' user-pw=***' if self.username else ''}"
                f" ! rtp{c}depay ! {c}parse config-interval=-1"
                f" ! video/x-{c},stream-format={fmt},alignment=au ! {c}timestamper ! queue")
        if self.sink == "kvs":
            tail = (f"kvssink stream-name={self.stream_name} aws-region={self.region}"
                    f" iot-certificate=\"{self.iot_certificate_structure()}\"")
        elif self.sink == "file":
            tail = f"matroskamux ! filesink location={self.output_path}"
        else:
            tail = "fakesink sync=false"
        return f"{head} ! {tail}"

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, s: str) -> PipelineSpec:
        return cls(**json.loads(s))


class Backoff:
    def __init__(self, initial: float, maximum: float):
        self.initial = initial
        self.maximum = maximum
        self.failures = 0

    def next_delay(self) -> float:
        d = min(self.maximum, self.initial * (2 ** self.failures))
        self.failures += 1
        return d

    def reset(self) -> None:
        self.failures = 0


class PipelineSupervisor:
    """子プロセスを 1 つ動かし続ける。落ちたらバックオフして起動し直す。

    spec_factory は起動のたびに呼ばれる(ONVIF で URL を取り直せるように)。
    """

    # この秒数以上動いたら「正常に動いた」とみなしてバックオフを戻す
    HEALTHY_AFTER_S = 30.0

    def __init__(self, camera_id: str, spec_factory: Callable[[int], "asyncio.Future | PipelineSpec"],
                 on_event: Callable[[str, dict], None], backoff: Backoff):
        self.camera_id = camera_id
        self.spec_factory = spec_factory
        self.on_event = on_event
        self.backoff = backoff
        self.proc: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        # 計測値(status と reported に使う)
        self.running = False  # 子プロセスがバッファを流している
        self.started_at: float | None = None  # 今回の配信開始(monotonic)
        self.bytes_total = 0  # 累計の送信バイト数(再起動をまたいで加算)
        self._bytes_run = 0
        self.restarts = 0
        self.last_error: str | None = None
        self.last_stats: dict = {}
        self.caps: dict = {}
        self.spec: PipelineSpec | None = None

    def start(self) -> None:
        if self.task is None or self.task.done():
            self._stop.clear()
            self.task = asyncio.create_task(self._loop(), name=f"pipeline-{self.camera_id}")

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
        self.running = False
        self.started_at = None

    @property
    def uptime_s(self) -> float:
        return 0.0 if self.started_at is None else time.monotonic() - self.started_at

    @property
    def bytes_sent(self) -> int:
        return self.bytes_total + self._bytes_run

    async def _loop(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                spec = self.spec_factory(attempt)
                if asyncio.iscoroutine(spec) or isinstance(spec, asyncio.Future):
                    spec = await spec
                self.spec = spec
            except Exception as e:  # noqa: BLE001
                self.last_error = f"resolve: {e}"
                self.on_event("error", {"message": self.last_error})
                await self._sleep(self.backoff.next_delay())
                attempt += 1
                continue
            t0 = time.monotonic()
            code = await self._run_once(spec)
            ran = time.monotonic() - t0
            self.bytes_total += self._bytes_run
            self._bytes_run = 0
            self.running = False
            self.started_at = None
            if self._stop.is_set():
                break
            if ran >= self.HEALTHY_AFTER_S:
                self.backoff.reset()
            self.restarts += 1
            attempt += 1
            delay = self.backoff.next_delay()
            log.warning("[%s] パイプラインが終了(code=%s, %.0f 秒動作)。%.0f 秒後に再起動",
                        self.camera_id, code, ran, delay)
            self.on_event("exit", {"code": code, "ran_s": round(ran, 1), "retry_in_s": delay})
            await self._sleep(delay)

    async def _sleep(self, s: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), s)
        except TimeoutError:
            pass

    async def _run_once(self, spec: PipelineSpec) -> int:
        log.info("[%s] 起動: %s", self.camera_id, spec.describe())
        env = dict(os.environ)
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "cc_edge.runner", spec.to_json(),
            stdout=asyncio.subprocess.PIPE, stderr=None, env=env,
            limit=1 << 20,
        )
        assert self.proc.stdout is not None
        async for line in self.proc.stdout:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                log.info("[%s] %s", self.camera_id, line.decode(errors="replace").rstrip())
                continue
            self._handle(ev)
        return await self.proc.wait()

    def _handle(self, ev: dict) -> None:
        t = ev.get("type")
        if t == "stats":
            self.last_stats = ev
            self._bytes_run = int(ev.get("bytes", 0))
            flowing = ev.get("last_buffer_age_s") is not None and ev["last_buffer_age_s"] < 5
            if flowing and not self.running:
                self.running = True
                self.started_at = time.monotonic() - float(ev.get("uptime_s") or 0)
                self.last_error = None
                self.on_event("streaming", {})
            log.debug("[%s] stats %s", self.camera_id, ev)
        elif t == "caps":
            self.caps = ev
            log.info("[%s] caps: %s", self.camera_id, ev)
            if ev.get("codec") == "H265" and not ev.get("has_codec_data"):
                log.warning("[%s] codec_data がない(KVS のコーデックプライベートデータが空になる)", self.camera_id)
        elif t == "error":
            self.last_error = ev.get("message")
            log.error("[%s] %s", self.camera_id, ev)
            self.on_event("error", ev)
        elif t == "ack":
            log.debug("[%s] ack %s", self.camera_id, ev)
        else:
            log.info("[%s] %s", self.camera_id, ev)
