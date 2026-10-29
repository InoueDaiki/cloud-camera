"""テストの共通設定。

2 つの動かし方がある:
  1. 外部のエミュレーターに対して(EMULATOR_HOST を指定)。docker compose の tests サービスはこちら
     - EMULATOR_HOST       ONVIF / RTSP のホスト(例: emulator)
     - EMULATOR_ONVIF_PORT 1 台目の ONVIF ポート(既定 8080。2 台目以降は +1)
     - EMULATOR_RTSP_PORT  既定 8554
     - EMULATOR_CAMERAS    台数(既定 2)
     - EMULATOR_DISCOVERY  multicast(既定) / unicast(EMULATOR_HOST:3702 に直接 Probe)
     - EMULATOR_UI_PORT    操作画面のポート(既定 8090)
  2. EMULATOR_HOST なし: テストのプロセス内でエミュレーターを起動する(映像なし、SOAP と Discovery のみ)
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

WSDL_DIR = Path(__file__).parent / "wsdl"


@dataclass
class Target:
    host: str
    onvif_port: int
    rtsp_port: int
    cameras: int
    discovery_mode: str  # multicast / unicast
    discovery_port: int
    username: str
    password: str
    media: bool  # RTSP 配信があるか
    ui_port: int = 8090

    def device_port(self, n: int) -> int:
        return self.onvif_port + n - 1


def _free_port_block(n: int) -> int:
    """連続した n 個の空き TCP ポートの先頭。"""
    for base in range(18080, 19000, 10):
        ok = True
        for p in range(base, base + n):
            with socket.socket() as s:
                try:
                    s.bind(("127.0.0.1", p))
                except OSError:
                    ok = False
                    break
        if ok:
            return base
    raise RuntimeError("空きポートがありません")


@pytest.fixture(scope="session")
def target() -> Target:
    if os.environ.get("EMULATOR_HOST"):
        yield Target(
            host=os.environ["EMULATOR_HOST"],
            onvif_port=int(os.environ.get("EMULATOR_ONVIF_PORT", "8080")),
            rtsp_port=int(os.environ.get("EMULATOR_RTSP_PORT", "8554")),
            cameras=int(os.environ.get("EMULATOR_CAMERAS", "2")),
            discovery_mode=os.environ.get("EMULATOR_DISCOVERY", "multicast"),
            discovery_port=int(os.environ.get("EMULATOR_DISCOVERY_PORT", "3702")),
            username=os.environ.get("EMULATOR_USERNAME", "admin"),
            password=os.environ.get("EMULATOR_PASSWORD", "admin"),
            media=os.environ.get("EMULATOR_MEDIA", "1") not in ("0", "false"),
            ui_port=int(os.environ.get("EMULATOR_UI_PORT", "8090")),
        )
        return

    # プロセス内で起動
    from onvif_emulator.app import Emulator
    from onvif_emulator.config import load_config

    base = _free_port_block(3)  # ONVIF × 2 + 操作画面
    runtime = tempfile.mkdtemp(prefix="onvif-emu-test-")
    cfg = load_config(
        env={
            "EMULATOR_CAMERAS": "2",
            "EMULATOR_ONVIF_PORT": str(base),
            "EMULATOR_STREAMS": "false",
            "EMULATOR_DISCOVERY_PORT": "13702",
            "EMULATOR_ADVERTISE_HOST": "127.0.0.1",
            "EMULATOR_RUNTIME_DIR": runtime,
            "EMULATOR_UI_PORT": str(base + 2),
        }
    )
    loop = asyncio.new_event_loop()
    emu = Emulator(cfg)
    started = threading.Event()

    def run() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(emu.start())
        started.set()
        loop.run_forever()

    th = threading.Thread(target=run, daemon=True)
    th.start()
    assert started.wait(10)
    yield Target(
        host="127.0.0.1",
        onvif_port=base,
        rtsp_port=cfg.rtsp_port,
        cameras=2,
        discovery_mode="unicast",
        discovery_port=13702,
        username="admin",
        password="admin",
        media=False,
        ui_port=base + 2,
    )
    asyncio.run_coroutine_threadsafe(emu.stop(), loop).result(10)
    loop.call_soon_threadsafe(loop.stop)
    th.join(5)
    shutil.rmtree(runtime, ignore_errors=True)


@pytest.fixture
def need_media(target: Target) -> None:
    if not target.media:
        pytest.skip("RTSP 配信なし(プロセス内起動)")
    if shutil.which("ffprobe") is None:
        pytest.skip("ffprobe がありません")
