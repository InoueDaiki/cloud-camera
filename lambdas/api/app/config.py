"""環境変数から設定を読む。

Terraform 担当と合意した環境変数:
PROJECT, STATE_TABLE, MEDIA_BUCKET, STREAM_NAME_PREFIX, GLUE_DATABASE, GLUE_TABLE,
ATHENA_WORKGROUP, BEDROCK_MODEL_ID, IOT_DATA_ENDPOINT, IOT_THING_PREFIX, CAMERAS
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

# 費用と品質のバランスで選んだ既定値(README の「モデルの選び方」を参照)。
# Anthropic のモデルはアカウントでユースケースの申請が済んだら
# BEDROCK_MODEL_ID=jp.anthropic.claude-haiku-4-5-20251001-v1:0 に切り替える候補。
DEFAULT_MODEL_ID = "jp.amazon.nova-2-lite-v1:0"


@dataclass(frozen=True)
class Config:
    project: str = "cloud-camera"
    state_table: str = ""
    media_bucket: str = ""
    stream_name_prefix: str = ""
    glue_database: str = ""
    glue_table: str = "detections"
    athena_workgroup: str = ""
    bedrock_model_id: str = DEFAULT_MODEL_ID
    iot_data_endpoint: str = ""
    iot_thing_prefix: str = ""
    cameras: dict[str, str] = field(default_factory=dict)
    # 以下は合意外の任意設定(未設定なら既定値)
    media_url_prefix: str = "/media/"

    def stream_name(self, camera_id: str) -> str:
        return f"{self.stream_name_prefix}{camera_id}"

    def thing_name(self, gateway_id: str) -> str:
        return f"{self.iot_thing_prefix}{gateway_id}"

    def media_url(self, key: str) -> str:
        return self.media_url_prefix + key.lstrip("/")


def load() -> Config:
    """呼び出しのたびに環境変数を読む(テストやローカルサーバーで差し替えやすくするため)。"""
    env = os.environ
    project = env.get("PROJECT", "cloud-camera")
    cameras_raw = env.get("CAMERAS", "{}") or "{}"
    try:
        cameras = json.loads(cameras_raw)
    except json.JSONDecodeError:
        cameras = {}
    if isinstance(cameras, list):  # ["cam-001", ...] だけ渡された場合も受け付ける
        cameras = {c: "" for c in cameras}
    return Config(
        project=project,
        state_table=env.get("STATE_TABLE", f"{project}-state"),
        media_bucket=env.get("MEDIA_BUCKET", ""),
        stream_name_prefix=env.get("STREAM_NAME_PREFIX", f"{project}-"),
        glue_database=env.get("GLUE_DATABASE", project.replace("-", "_")),
        glue_table=env.get("GLUE_TABLE", "detections"),
        athena_workgroup=env.get("ATHENA_WORKGROUP", "primary"),
        bedrock_model_id=env.get("BEDROCK_MODEL_ID") or DEFAULT_MODEL_ID,
        iot_data_endpoint=env.get("IOT_DATA_ENDPOINT", ""),
        iot_thing_prefix=env.get("IOT_THING_PREFIX", f"{project}-"),
        cameras={str(k): str(v) for k, v in cameras.items()},
        media_url_prefix=env.get("MEDIA_URL_PREFIX", "/media/"),
    )
