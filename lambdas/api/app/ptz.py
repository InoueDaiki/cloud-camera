"""POST / GET /api/cameras/{id}/ptz(docs/contracts.md の「PTZ」節)。

POST は IoT Core の `{p}/{gw}/cmd` に `{"action": "ptz", ...}` を QoS 1 で publish して、すぐ 202 を返す
(エッジの処理結果は `{p}/{gw}/ptz` とシャドウの reported.cameras.{id}.ptz に出る)。
GET はシャドウの reported.cameras.{id}.ptz を返す。
"""

from __future__ import annotations

import json
import math
import uuid
from typing import Any

from . import aws
from .cameras import _get_shadow
from .config import Config
from .http import bad_request, not_found

OPS = ("continuous", "stop", "absolute", "relative", "goto_preset", "set_preset", "home")
# 各 op で受け付ける値の範囲(座標は ONVIF の Generic 空間)
_RANGES = {
    "continuous": {"pan": (-1, 1), "tilt": (-1, 1), "zoom": (-1, 1)},
    "relative": {"pan": (-2, 2), "tilt": (-2, 2), "zoom": (-1, 1)},
    "absolute": {"pan": (-1, 1), "tilt": (-1, 1), "zoom": (0, 1)},
}
TIMEOUT_MS = (100, 60_000)
DEFAULT_TIMEOUT_MS = 10_000
_MAX_TEXT = 64


def _number(body: dict, key: str, lo: float, hi: float) -> float | None:
    v = body.get(key)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise bad_request(f"{key} は数値にしてください")
    if not lo <= v <= hi:
        raise bad_request(f"{key} は {lo}〜{hi} にしてください")
    return float(v)


def _text(body: dict, key: str, required: bool = False) -> str | None:
    v = body.get(key)
    if v is None or v == "":
        if required:
            raise bad_request(f"{key} が必要です")
        return None
    if not isinstance(v, str) or len(v) > _MAX_TEXT:
        raise bad_request(f"{key} は {_MAX_TEXT} 文字までの文字列にしてください")
    return v


def validate(body: Any) -> dict:
    """本文を検証し、cmd に入れる項目(op 以降)だけを返す。"""
    if not isinstance(body, dict):
        raise bad_request('ボディは {"op": "...", ...} にしてください')
    op = body.get("op")
    if op not in OPS:
        raise bad_request(f"op は {' / '.join(OPS)} のどれかにしてください")
    cmd: dict[str, Any] = {"op": op}
    if op in _RANGES:
        for key, (lo, hi) in _RANGES[op].items():
            v = _number(body, key, lo, hi)
            if v is not None:
                cmd[key] = v
        if op != "continuous" and not any(k in cmd for k in ("pan", "tilt", "zoom")):
            raise bad_request("pan / tilt / zoom のどれかが必要です")
    if op == "continuous":
        t = _number(body, "timeout_ms", *TIMEOUT_MS)
        cmd["timeout_ms"] = int(t) if t is not None else DEFAULT_TIMEOUT_MS
    if op == "goto_preset":
        cmd["preset_token"] = _text(body, "preset_token", required=True)
    if op == "set_preset":
        for key in ("preset_name", "preset_token"):
            v = _text(body, key)
            if v is not None:
                cmd[key] = v
    return cmd


def _gateway(cfg: Config, camera_id: str) -> str:
    gw = cfg.cameras.get(camera_id)
    if not gw:
        raise not_found(f"ゲートウェイが設定されていないカメラです: {camera_id}", "CAMERA_NOT_FOUND")
    return gw


def command(cfg: Config, camera_id: str, body: Any) -> dict:
    cmd = validate(body)
    gw = _gateway(cfg, camera_id)
    request_id = str(uuid.uuid4())
    payload = {"action": "ptz", "camera_id": camera_id, "request_id": request_id, **cmd}
    aws.iot_data(cfg.iot_data_endpoint).publish(
        topic=f"{cfg.project}/{gw}/cmd", qos=1,
        payload=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())
    return {"request_id": request_id}


def get_state(cfg: Config, camera_id: str) -> dict:
    gw = _gateway(cfg, camera_id)
    shadow = _get_shadow(cfg, gw) or {}
    reported = ((((shadow.get("state") or {}).get("reported") or {}).get("cameras") or {})
                .get(camera_id) or {})
    state = reported.get("ptz")
    if not isinstance(state, dict) or "supported" not in state:
        return {"supported": False}
    return state
