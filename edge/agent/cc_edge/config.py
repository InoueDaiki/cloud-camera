"""設定の読み込み。

優先順位: 環境変数 > 設定ファイル(JSON)> 既定値。
設定ファイルは `CC_CONFIG_FILE`(既定 `/certs/config.json`。なければ無視)。
provision-edge.sh が `edge/.certs/<gateway_id>/config.json` を書き出す。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SINKS = ("kvs", "fake", "file")


def _bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    project: str = "cloud-camera"
    gateway_id: str = "gw-001"
    thing_name: str = ""  # 空なら {project}-{gateway_id}
    region: str = "ap-northeast-1"

    # IoT Core
    iot_endpoint: str = ""  # データエンドポイント(ATS)。空なら MQTT に接続しない
    iot_credential_endpoint: str = ""  # 認証情報プロバイダーのエンドポイント
    kvs_role_alias: str = ""  # 空なら {project}-kvs-role-alias
    certs_dir: str = "/certs"
    cert_file: str = "device.pem.crt"
    key_file: str = "private.pem.key"
    ca_file: str = "AmazonRootCA1.pem"

    # 動作モード
    use_shadow: bool = True  # False ならローカル設定の cameras だけで動く
    sink: str = "kvs"  # kvs / fake(ドライラン)/ file(ドライラン、MKV に保存)
    output_dir: str = "/var/lib/cc-edge/out"  # sink=file の出力先
    status_interval_s: float = 30.0
    discovery_on_start: bool = True
    discovery_timeout_s: float = 3.0

    # パイプライン
    rtsp_latency_ms: int = 200
    rtsp_protocols: str = "tcp"
    stall_timeout_s: float = 15.0  # この秒数バッファが来なければパイプラインを再起動
    backoff_initial_s: float = 2.0
    backoff_max_s: float = 60.0
    kvs_retention_hours: int = 1  # kvssink がストリームを自動作成する場合だけ効く
    # 回線断の間 kvssink のメモリに溜める上限(1 カメラあたり)。既定は 10 分 + 復旧後の再接続の余裕 1 分。
    # 1.5Mbps で 660 秒は約 124MB なので、時間の上限が先に効くようメモリは 160MB
    kvs_storage_size_mb: int = 160
    kvs_buffer_s: int = 660
    kvs_connection_staleness_s: int = 30
    kvs_replay_s: int = 0  # 0 ならバッファと同じ長さ
    kvs_framerate: int = 0  # 0 なら 30 で見積もる(バッファの枠 = framerate × kvs_buffer_s)
    kvs_log_config: str = "/opt/kvs/kvs_log_configuration"

    # シャドウを使わないときのカメラ設定(シャドウの desired.cameras と同じ形)
    cameras: dict[str, dict[str, Any]] = field(default_factory=dict)

    log_level: str = "INFO"

    @property
    def thing(self) -> str:
        return self.thing_name or f"{self.project}-{self.gateway_id}"

    @property
    def role_alias(self) -> str:
        return self.kvs_role_alias or f"{self.project}-kvs-role-alias"

    @property
    def cert_path(self) -> str:
        return str(Path(self.certs_dir) / self.cert_file)

    @property
    def key_path(self) -> str:
        return str(Path(self.certs_dir) / self.key_file)

    @property
    def ca_path(self) -> str:
        return str(Path(self.certs_dir) / self.ca_file)

    @property
    def topic_prefix(self) -> str:
        return f"{self.project}/{self.gateway_id}"

    @property
    def mqtt_enabled(self) -> bool:
        return bool(self.iot_endpoint) and Path(self.cert_path).exists() and Path(self.key_path).exists()

    def stream_name(self, camera_id: str) -> str:
        return f"{self.project}-{camera_id}"


# 環境変数名 → フィールド名
_ENV = {
    "CC_PROJECT": "project",
    "CC_GATEWAY_ID": "gateway_id",
    "CC_THING_NAME": "thing_name",
    "CC_AWS_REGION": "region",
    "CC_IOT_ENDPOINT": "iot_endpoint",
    "CC_IOT_CREDENTIAL_ENDPOINT": "iot_credential_endpoint",
    "CC_KVS_ROLE_ALIAS": "kvs_role_alias",
    "CC_CERTS_DIR": "certs_dir",
    "CC_CERT_FILE": "cert_file",
    "CC_KEY_FILE": "key_file",
    "CC_CA_FILE": "ca_file",
    "CC_USE_SHADOW": "use_shadow",
    "CC_SINK": "sink",
    "CC_OUTPUT_DIR": "output_dir",
    "CC_STATUS_INTERVAL": "status_interval_s",
    "CC_DISCOVERY_ON_START": "discovery_on_start",
    "CC_DISCOVERY_TIMEOUT": "discovery_timeout_s",
    "CC_RTSP_LATENCY_MS": "rtsp_latency_ms",
    "CC_RTSP_PROTOCOLS": "rtsp_protocols",
    "CC_STALL_TIMEOUT": "stall_timeout_s",
    "CC_BACKOFF_INITIAL": "backoff_initial_s",
    "CC_BACKOFF_MAX": "backoff_max_s",
    "CC_KVS_RETENTION_HOURS": "kvs_retention_hours",
    "CC_KVS_STORAGE_SIZE_MB": "kvs_storage_size_mb",
    "CC_KVS_BUFFER_S": "kvs_buffer_s",
    "CC_KVS_CONNECTION_STALENESS_S": "kvs_connection_staleness_s",
    "CC_KVS_REPLAY_S": "kvs_replay_s",
    "CC_KVS_FRAMERATE": "kvs_framerate",
    "CC_KVS_LOG_CONFIG": "kvs_log_config",
    "CC_LOG_LEVEL": "log_level",
}


def _coerce(name: str, value: Any) -> Any:
    default = Config.__dataclass_fields__[name].default
    if isinstance(default, bool):
        return _bool(value)
    if isinstance(default, int):
        return int(value)
    if isinstance(default, float):
        return float(value)
    return value


def load(env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ if env is None else env)
    cfg = Config()
    certs_dir = env.get("CC_CERTS_DIR", cfg.certs_dir)
    path = Path(env.get("CC_CONFIG_FILE", str(Path(certs_dir) / "config.json")))
    if path.exists():
        data = json.loads(path.read_text())
        for k, v in data.items():
            if k == "cameras":
                cfg.cameras = dict(v or {})
            elif k in Config.__dataclass_fields__:
                setattr(cfg, k, _coerce(k, v))
    for var, name in _ENV.items():
        if var in env and env[var] != "":
            setattr(cfg, name, _coerce(name, env[var]))
    # CC_CAMERAS(JSON)で cameras を直接与えることもできる
    if env.get("CC_CAMERAS"):
        cfg.cameras = json.loads(env["CC_CAMERAS"])
    # ドライランの別名
    if _bool(env.get("CC_DRY_RUN", "0")) and "CC_SINK" not in env:
        cfg.sink = "fake"
    if cfg.sink not in SINKS:
        raise ValueError(f"CC_SINK は {SINKS} のどれか: {cfg.sink}")
    return cfg
