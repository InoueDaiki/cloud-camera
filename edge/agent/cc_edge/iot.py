"""AWS IoT Core への接続(AWS IoT Device SDK v2、MQTT5)。

コールバックは CRT のスレッドで呼ばれるので、asyncio のループに渡して処理する。
接続(再接続)が成功するたびに購読し直し、シャドウを取り直す(クリーンセッションのため)。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

from awscrt import mqtt5
from awsiot import mqtt5_client_builder

from .config import Config

log = logging.getLogger(__name__)

Handler = Callable[[str, dict], Awaitable[None] | None]


class IotConnection:
    def __init__(self, cfg: Config, loop: asyncio.AbstractEventLoop):
        self.cfg = cfg
        self.loop = loop
        self.handlers: dict[str, Handler] = {}
        self.on_connected: list[Callable[[], Awaitable[None] | None]] = []
        self.connected = asyncio.Event()
        p = cfg.topic_prefix
        self.t_status = f"{p}/status"
        self.t_discovery = f"{p}/discovery"
        self.t_cmd = f"{p}/cmd"
        will = mqtt5.PublishPacket(
            topic=self.t_status,
            payload=json.dumps({"online": False, "gateway_id": cfg.gateway_id}),
            qos=mqtt5.QoS.AT_LEAST_ONCE,
        )
        self.client = mqtt5_client_builder.mtls_from_path(
            endpoint=cfg.iot_endpoint,
            cert_filepath=cfg.cert_path,
            pri_key_filepath=cfg.key_path,
            ca_filepath=cfg.ca_path,
            client_id=cfg.thing,  # ポリシーで iot:ClientId = Thing 名に絞れるよう、Thing 名を使う
            will=will,
            will_delay_interval_sec=0,
            keep_alive_interval_sec=60,
            min_reconnect_delay_ms=1000,
            max_reconnect_delay_ms=30000,  # 回線断から戻ったら 30 秒以内に status と reported を戻す
            on_publish_received=self._on_publish,
            on_lifecycle_connection_success=self._on_success,
            on_lifecycle_connection_failure=self._on_failure,
            on_lifecycle_disconnection=self._on_disconnect,
        )

    # --- CRT スレッドからのコールバック ------------------------------------
    def _on_publish(self, data: mqtt5.PublishReceivedData) -> None:
        pkt = data.publish_packet
        try:
            payload = json.loads(pkt.payload) if pkt.payload else {}
        except (ValueError, UnicodeDecodeError):
            log.warning("JSON でないメッセージを無視: %s", pkt.topic)
            return
        self.loop.call_soon_threadsafe(self._dispatch, pkt.topic, payload)

    def _on_success(self, _data) -> None:
        log.info("IoT Core に接続した: %s(client_id=%s)", self.cfg.iot_endpoint, self.cfg.thing)
        self.loop.call_soon_threadsafe(self._connected)

    def _on_failure(self, data) -> None:
        log.warning("IoT Core への接続に失敗: %s", getattr(data, "exception", data))

    def _on_disconnect(self, data) -> None:
        log.warning("IoT Core から切断: %s", getattr(data, "exception", data))
        self.loop.call_soon_threadsafe(self.connected.clear)

    # --- ループ上の処理 ----------------------------------------------------
    def _dispatch(self, topic: str, payload: dict) -> None:
        h = self.handlers.get(topic)
        if h is None:
            return
        res = h(topic, payload)
        if asyncio.iscoroutine(res):
            asyncio.ensure_future(res)

    def _connected(self) -> None:
        self.connected.set()

        async def after():
            if self.handlers:
                await self.subscribe(list(self.handlers))
            for cb in self.on_connected:
                r = cb()
                if asyncio.iscoroutine(r):
                    await r

        asyncio.ensure_future(after())

    def handle(self, topic: str, handler: Handler) -> None:
        self.handlers[topic] = handler

    async def subscribe(self, topics: list[str]) -> None:
        fut = self.client.subscribe(mqtt5.SubscribePacket(
            subscriptions=[mqtt5.Subscription(topic_filter=t, qos=mqtt5.QoS.AT_LEAST_ONCE) for t in topics]))
        res = await asyncio.wrap_future(fut)
        log.info("購読: %s → %s", topics, [c.name for c in res.reason_codes])

    async def publish(self, topic: str, payload: dict, qos: int = 1) -> None:
        if not self.connected.is_set():
            log.debug("未接続のため送信を省略: %s", topic)
            return
        fut = self.client.publish(mqtt5.PublishPacket(
            topic=topic, payload=json.dumps(payload, separators=(",", ":")),
            qos=mqtt5.QoS(qos)))
        try:
            # shield: タイムアウトで CRT 側の Future まで取り消すと、後で CRT が結果を入れようとして
            # InvalidStateError がログに出る(回線断のときに起きる)
            await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(fut)), 10)
        except Exception as e:  # noqa: BLE001
            log.warning("publish %s に失敗: %s", topic, e)

    def start(self) -> None:
        self.client.start()

    def stop(self) -> None:
        # 正常終了時も LWT を送ってもらうため、DISCONNECT_WITH_WILL_MESSAGE で切る
        self.client.stop(mqtt5.DisconnectPacket(
            reason_code=mqtt5.DisconnectReasonCode.DISCONNECT_WITH_WILL_MESSAGE))
