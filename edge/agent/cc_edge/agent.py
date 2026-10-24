"""エージェント本体: シャドウ(またはローカル設定)に従ってカメラごとのパイプラインを動かす。"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import signal
import urllib.parse
from pathlib import Path
from typing import Any

from . import onvif, ptz
from .config import Config
from .pipeline import Backoff, PipelineSpec, PipelineSupervisor

log = logging.getLogger(__name__)


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _conf_key(conf: dict) -> str:
    """パイプラインの作り直しが要る設定だけを比較する(streaming は別に扱う)。"""
    keys = ("onvif_url", "username", "password", "profile_token", "prefer_codec", "rtsp_url", "codec")
    return json.dumps({k: conf.get(k) for k in keys}, sort_keys=True)


def diff_cameras(current: dict[str, dict], desired: dict[str, dict]) -> dict[str, list[str]]:
    """差分適用の計画を作る。current / desired は camera_id → 設定。

    戻り値: {"start": [...], "stop": [...], "restart": [...], "remove": [...]}
    """
    plan: dict[str, list[str]] = {"start": [], "stop": [], "restart": [], "remove": []}
    for cam in sorted(set(current) - set(desired)):
        plan["remove"].append(cam)
    for cam in sorted(desired):
        want = desired[cam] or {}
        streaming = bool(want.get("streaming", True))
        have = current.get(cam)
        if have is None:
            if streaming:
                plan["start"].append(cam)
            continue
        was = bool(have.get("streaming", True))
        if not streaming:
            if was:
                plan["stop"].append(cam)
        elif not was:
            plan["start"].append(cam)
        elif _conf_key(have) != _conf_key(want):
            plan["restart"].append(cam)
    return plan


class CameraWorker:
    def __init__(self, agent: Agent, camera_id: str, conf: dict):
        self.agent = agent
        self.cfg = agent.cfg
        self.camera_id = camera_id
        self.conf = dict(conf)
        self.info: onvif.StreamInfo | None = None
        self.resolve_error: str | None = None
        self.sup = PipelineSupervisor(
            camera_id, self._spec, self._on_event,
            Backoff(self.cfg.backoff_initial_s, self.cfg.backoff_max_s))

    async def _spec(self, attempt: int) -> PipelineSpec:
        c = self.conf
        if c.get("rtsp_url"):
            rtsp, codec = c["rtsp_url"], c.get("codec", "H265")
            resolution = c.get("resolution")
            self.info = onvif.StreamInfo(rtsp_url=rtsp, profile_token=c.get("profile_token") or "",
                                         codec=codec, width=0, height=0, media_version=0)
            if resolution:
                w, _, h = str(resolution).partition("x")
                self.info.width, self.info.height = int(w or 0), int(h or 0)
        else:
            # 初回と、3 回失敗するごとに ONVIF で取り直す(カメラの再起動で URL が変わることがある)
            if self.info is None or attempt % 3 == 0:
                cli = onvif.OnvifClient(c["onvif_url"], c.get("username", ""), c.get("password", ""))
                try:
                    self.info = await asyncio.to_thread(cli.resolve, c.get("profile_token"),
                                                        c.get("prefer_codec") or self.cfg.prefer_codec)
                    self.resolve_error = None
                    log.info("[%s] ONVIF: profile=%s codec=%s %dx%d %s", self.camera_id,
                             self.info.profile_token, self.info.codec, self.info.width,
                             self.info.height, self.info.rtsp_url)
                except Exception as e:
                    self.resolve_error = str(e)
                    self.agent.report_soon()
                    raise
            rtsp, codec = self.info.rtsp_url, self.info.codec
        cfg = self.cfg
        out = ""
        if cfg.sink == "file":
            Path(cfg.output_dir).mkdir(parents=True, exist_ok=True)
            stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
            out = str(Path(cfg.output_dir) / f"{self.camera_id}-{stamp}.mkv")
        return PipelineSpec(
            camera_id=self.camera_id, rtsp_url=rtsp, codec=codec,
            username=c.get("username", ""), password=c.get("password", ""),
            sink=cfg.sink, stream_name=cfg.stream_name(self.camera_id), region=cfg.region,
            iot_credential_endpoint=cfg.iot_credential_endpoint, cert_path=cfg.cert_path,
            key_path=cfg.key_path, ca_path=cfg.ca_path, role_alias=cfg.role_alias,
            thing_name=cfg.thing, retention_hours=cfg.kvs_retention_hours,
            storage_size_mb=cfg.kvs_storage_size_mb, buffer_duration_s=cfg.kvs_buffer_s,
            connection_staleness_s=cfg.kvs_connection_staleness_s, replay_duration_s=cfg.kvs_replay_s,
            framerate=cfg.kvs_framerate, log_config=cfg.kvs_log_config,
            output_path=out, latency_ms=cfg.rtsp_latency_ms, protocols=cfg.rtsp_protocols,
            stall_timeout_s=cfg.stall_timeout_s,
        )

    def _on_event(self, kind: str, data: dict) -> None:
        self.agent.report_soon()

    def start(self) -> None:
        self.sup.start()

    async def stop(self) -> None:
        await self.sup.stop()

    @property
    def streaming_desired(self) -> bool:
        return bool(self.conf.get("streaming", True))

    def reported(self) -> dict[str, Any]:
        info = self.info
        caps = self.sup.caps
        w = caps.get("width") or (info.width if info else 0)
        h = caps.get("height") or (info.height if info else 0)
        err = None
        if self.streaming_desired and not self.sup.running:
            err = self.resolve_error or self.sup.last_error
        return {
            "streaming": self.sup.running,
            "rtsp_url": info.rtsp_url if info else None,
            "codec": (caps.get("codec") or (info.codec if info else None)),
            "resolution": f"{w}x{h}" if w and h else None,
            "profile_token": info.profile_token if info else None,
            "error": err,
        }

    def status(self) -> dict[str, Any]:
        st = self.sup.last_stats
        return {
            "streaming": self.sup.running,
            "uptime_s": round(self.sup.uptime_s),
            "bytes_sent": self.sup.bytes_sent,
            "restarts": self.sup.restarts,
            "gop_s": st.get("gop_s"),
            "persist_latency_ms": st.get("persist_latency_ms"),
            "error": self.reported()["error"],
            # 回線断のバッファ(kvssink のときだけ。それ以外は null)。項目は docs/contracts.md
            "buffer": self.sup.buffer_status(),
            "last_exit": self.sup.last_exit,
        }


class Agent:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.workers: dict[str, CameraWorker] = {}
        self.confs: dict[str, dict] = {}  # 現在適用中の desired.cameras
        self.iot = None
        self._apply_lock = asyncio.Lock()
        self._report_event = asyncio.Event()
        self._last_reported: dict | None = None
        self._stop = asyncio.Event()
        self._shadow_version = -1
        self.ptz = ptz.PtzManager(cfg.gateway_id, self._publish_ptz_result, self._publish_ptz_shadow)

    # --- シャドウ -------------------------------------------------------
    @property
    def shadow_prefix(self) -> str:
        return f"$aws/things/{self.cfg.thing}/shadow"

    async def request_shadow(self) -> None:
        await self.iot.publish(f"{self.shadow_prefix}/get", {})

    async def _on_shadow_get(self, _topic: str, msg: dict) -> None:
        desired = ((msg.get("state") or {}).get("desired") or {}).get("cameras") or {}
        self._shadow_version = max(self._shadow_version, int(msg.get("version", -1)))
        log.info("シャドウ(get, version=%s): desired.cameras=%s", msg.get("version"), list(desired))
        self._last_reported = None  # 取り直したときは reported を必ず送る
        await self.apply(desired)

    async def _on_shadow_get_rejected(self, _topic: str, msg: dict) -> None:
        # シャドウがまだない(404)ときは、カメラなしとして動く
        log.warning("シャドウの取得が拒否された: %s", msg)
        await self.apply({})

    async def _on_shadow_documents(self, _topic: str, msg: dict) -> None:
        cur = msg.get("current") or {}
        ver = int(cur.get("version", -1))
        if ver <= self._shadow_version:
            return
        self._shadow_version = ver
        desired = ((cur.get("state") or {}).get("desired") or {}).get("cameras") or {}
        await self.apply(desired)

    # --- 差分適用 -------------------------------------------------------
    async def apply(self, desired: dict[str, dict]) -> None:
        desired = {k: (v or {}) for k, v in desired.items()}
        async with self._apply_lock:
            plan = diff_cameras(self.confs, desired)
            if any(plan.values()):
                log.info("差分適用: %s", {k: v for k, v in plan.items() if v})
            for cam in plan["remove"]:
                w = self.workers.pop(cam, None)
                if w:
                    await w.stop()
            # 止めたカメラもワーカーは残す(reported に載せ、送信バイト数の累計を保つ)
            for cam in plan["stop"] + plan["restart"]:
                w = self.workers.get(cam)
                if w:
                    await w.stop()
            for cam, conf in desired.items():
                w = self.workers.get(cam)
                if w is None:
                    w = self.workers[cam] = CameraWorker(self, cam, conf)
                if cam in plan["restart"]:
                    w.info = None  # ONVIF で取り直す
                w.conf = dict(conf)
                if cam in plan["start"] or cam in plan["restart"]:
                    w.start()
            self.confs = desired
            self.ptz.sync(desired)
        self.report_soon()

    # --- reported / status ---------------------------------------------
    def report_soon(self) -> None:
        self._report_event.set()

    def reported_state(self) -> dict:
        cams: dict[str, Any] = {cam: w.reported() for cam, w in sorted(self.workers.items())}
        return {"cameras": cams, "gateway": {"gateway_id": self.cfg.gateway_id, "sink": self.cfg.sink}}

    async def _report_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._report_event.wait(), 5)
            except TimeoutError:
                pass
            self._report_event.clear()
            state = self.reported_state()
            if state == self._last_reported:
                continue
            payload_state = json.loads(json.dumps(state))
            # desired から消えたカメラは reported からも消す(null で削除)
            if self._last_reported:
                for cam in self._last_reported.get("cameras", {}):
                    if cam not in state["cameras"]:
                        payload_state["cameras"][cam] = None
            if self.iot and self.cfg.use_shadow:
                if not self.iot.connected.is_set():
                    continue
                await self.iot.publish(f"{self.shadow_prefix}/update", {"state": {"reported": payload_state}})
            log.info("reported: %s", json.dumps(payload_state, ensure_ascii=False))
            self._last_reported = state
            await asyncio.sleep(1)  # 連続した変化をまとめる

    def status_payload(self) -> dict:
        return {
            "ts": utc_now(),
            "online": True,
            "gateway_id": self.cfg.gateway_id,
            "cameras": {cam: w.status() for cam, w in sorted(self.workers.items())},
        }

    async def _status_loop(self) -> None:
        while not self._stop.is_set():
            payload = self.status_payload()
            # 回線断で publish が待たされても計測値がログに残るよう、先にログに出す
            log.info("status: %s", json.dumps(payload, ensure_ascii=False))
            if self.iot:
                await self.iot.publish(self.iot.t_status, payload, qos=0)
                # ビューワーのオンライン表示用。異常終了は API 側で now - last_seen > 90 秒をオフラインとみなす
                await self.publish_presence(True, payload["ts"])
            try:
                await asyncio.wait_for(self._stop.wait(), self.cfg.status_interval_s)
            except TimeoutError:
                pass

    async def publish_presence(self, online: bool, ts: str | None = None) -> None:
        """シャドウの reported.online / reported.last_seen を更新する。"""
        if not (self.iot and self.cfg.use_shadow and self.iot.connected.is_set()):
            return
        await self.iot.publish(f"{self.shadow_prefix}/update",
                               {"state": {"reported": {"online": online, "last_seen": ts or utc_now()}}})

    # --- cmd / discovery ------------------------------------------------
    def _discovery_hosts(self) -> list[str]:
        hosts = set()
        for conf in self.confs.values():
            if conf.get("onvif_url"):
                h = urllib.parse.urlsplit(conf["onvif_url"]).hostname
                if h:
                    hosts.add(h)
        return sorted(hosts)

    async def run_discovery(self, request_id: str | None = None) -> dict:
        devices = await asyncio.to_thread(onvif.discover, self.cfg.discovery_timeout_s, self._discovery_hosts())
        payload = {"ts": utc_now(), "gateway_id": self.cfg.gateway_id, "devices": devices}
        if request_id:
            payload["request_id"] = request_id
        log.info("discovery: %d 台 %s", len(devices), [d.get("xaddrs") for d in devices])
        if self.iot:
            await self.iot.publish(self.iot.t_discovery, payload)
        return payload

    async def _on_cmd(self, _topic: str, msg: dict) -> None:
        action = msg.get("action")
        rid = msg.get("request_id")
        log.info("cmd: %s", msg)
        if action == "discover":
            await self.run_discovery(rid)
        elif action == "probe":
            # 指定したデバイスのプロファイル一覧を discovery トピックに返す
            cli = onvif.OnvifClient(msg["onvif_url"], msg.get("username", ""), msg.get("password", ""))
            try:
                info = await asyncio.to_thread(cli.resolve, msg.get("profile_token"),
                                               msg.get("prefer_codec") or self.cfg.prefer_codec)
                result = {"onvif_url": msg["onvif_url"], "selected": info.to_dict()}
            except Exception as e:  # noqa: BLE001
                result = {"onvif_url": msg["onvif_url"], "error": str(e)}
            if self.iot:
                await self.iot.publish(self.iot.t_discovery,
                                       {"ts": utc_now(), "gateway_id": self.cfg.gateway_id,
                                        "request_id": rid, "probe": result})
        elif action == "restart":
            cam = msg.get("camera_id")
            targets = [cam] if cam else list(self.workers)
            async with self._apply_lock:
                for c in targets:
                    w = self.workers.get(c)
                    if w and w.streaming_desired:
                        await w.stop()
                        nw = CameraWorker(self, c, w.conf)
                        self.workers[c] = nw
                        nw.start()
        elif action == "status":
            if self.iot:
                await self.iot.publish(self.iot.t_status, self.status_payload())
        elif action == "ptz":
            await self.ptz.handle(msg, self.confs)
        elif action == "reload":
            if self.cfg.use_shadow and self.iot:
                await self.request_shadow()
        else:
            log.warning("知らない cmd: %s", action)

    # --- PTZ(処理は ptz.py) ---------------------------------------------
    async def _publish_ptz_result(self, payload: dict) -> None:
        if self.iot:
            await self.iot.publish(f"{self.cfg.topic_prefix}/ptz", payload)

    async def _publish_ptz_shadow(self, camera_id: str, state: dict) -> None:
        if self.iot and self.cfg.use_shadow and self.iot.connected.is_set():
            await self.iot.publish(f"{self.shadow_prefix}/update",
                                   {"state": {"reported": {"cameras": {camera_id: {"ptz": state}}}}})

    # --- 実行 -----------------------------------------------------------
    async def _on_connected(self) -> None:
        await self.iot.publish(self.iot.t_status, self.status_payload())
        if self.cfg.use_shadow:
            await self.publish_presence(True)
            await self.request_shadow()
            self.ptz.resend()

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._stop.set)
        cfg = self.cfg
        log.info("起動: gateway=%s thing=%s sink=%s shadow=%s mqtt=%s", cfg.gateway_id, cfg.thing,
                 cfg.sink, cfg.use_shadow, cfg.mqtt_enabled)
        if cfg.mqtt_enabled:
            from .iot import IotConnection  # awscrt はドライランでも入っているが、遅延 import にしておく

            self.iot = IotConnection(cfg, loop)
            sp = self.shadow_prefix
            self.iot.handle(self.iot.t_cmd, self._on_cmd)
            if cfg.use_shadow:
                self.iot.handle(f"{sp}/get/accepted", self._on_shadow_get)
                self.iot.handle(f"{sp}/get/rejected", self._on_shadow_get_rejected)
                self.iot.handle(f"{sp}/update/documents", self._on_shadow_documents)
                self.iot.handle(f"{sp}/update/rejected", lambda t, m: log.warning("shadow update rejected: %s", m))
            self.iot.on_connected.append(self._on_connected)
            self.iot.start()
        elif cfg.use_shadow:
            log.warning("MQTT の設定(CC_IOT_ENDPOINT と証明書)がないため、ローカル設定の cameras で動く")
            cfg.use_shadow = False
        if not cfg.use_shadow:
            await self.apply(cfg.cameras)
        tasks = [asyncio.create_task(self._report_loop()), asyncio.create_task(self._status_loop())]
        if cfg.discovery_on_start:
            tasks.append(asyncio.create_task(self._discovery_after_start()))
        await self._stop.wait()
        log.info("停止中…")
        for t in tasks:
            t.cancel()
        await self.ptz.stop_all()
        await asyncio.gather(*(w.stop() for w in self.workers.values()), return_exceptions=True)
        self._last_reported = None
        if self.iot:
            # 止めたカメラの状態と online=false を送ってから切断する(切断時に LWT も送られる)
            state = self.reported_state()
            if self.cfg.use_shadow and self.iot.connected.is_set():
                await self.iot.publish(f"{self.shadow_prefix}/update", {"state": {"reported": state}})
            await self.publish_presence(False)
        if self.iot:
            self.iot.stop()
            await asyncio.sleep(0.5)

    async def _discovery_after_start(self) -> None:
        await asyncio.sleep(2)
        if self.iot:
            try:
                await asyncio.wait_for(self.iot.connected.wait(), 30)
            except TimeoutError:
                pass
        try:
            await self.run_discovery()
        except Exception as e:  # noqa: BLE001
            log.warning("discovery に失敗: %s", e)
