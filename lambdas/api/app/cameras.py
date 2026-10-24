"""GET /api/cameras と PUT /api/cameras/{id}/streaming(IoT のクラシックシャドウ)。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from botocore.exceptions import ClientError

from . import aws, state
from .config import Config
from .http import bad_request, not_found
from .timeutil import iso, now

# エッジは 30 秒ごとに last_seen を更新する。これより古ければ異常終了とみなす
_PRESENCE_TIMEOUT = timedelta(seconds=90)

# シャドウの desired に入っている秘密はビューワーへ返さない
_SECRET_KEYS = {"password"}


def _mask(d: dict | None) -> dict | None:
    if d is None:
        return None
    return {k: ("***" if k in _SECRET_KEYS and v is not None else v) for k, v in d.items()}


def _get_shadow(cfg: Config, gateway_id: str) -> dict | None:
    try:
        resp = aws.iot_data(cfg.iot_data_endpoint).get_thing_shadow(thingName=cfg.thing_name(gateway_id))
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            return None
        raise
    return json.loads(resp["payload"].read())


def _camera_state(shadow: dict | None, camera_id: str) -> tuple[dict | None, dict | None, str | None]:
    if not shadow:
        return None, None, None
    st = shadow.get("state") or {}
    reported = ((st.get("reported") or {}).get("cameras") or {}).get(camera_id)
    desired = ((st.get("desired") or {}).get("cameras") or {}).get(camera_id)
    updated = None
    ts = shadow.get("timestamp")
    meta_rep = (((shadow.get("metadata") or {}).get("reported") or {}).get("cameras") or {}).get(camera_id)
    if isinstance(meta_rep, dict):
        stamps = [v.get("timestamp") for v in meta_rep.values() if isinstance(v, dict) and "timestamp" in v]
        if stamps:
            ts = max(stamps)
    if ts:
        updated = iso(datetime.fromtimestamp(int(ts), timezone.utc))
    return reported, desired, updated


def _online(shadow: dict | None) -> bool | None:
    """ゲートウェイの死活。シャドウに情報がなければ None(不明)。"""
    reported = ((shadow or {}).get("state") or {}).get("reported") or {}
    online, last_seen = reported.get("online"), reported.get("last_seen")
    if not isinstance(online, bool):
        return None
    if not online:
        return False
    try:
        seen = datetime.fromisoformat(str(last_seen).replace("Z", "+00:00"))
    except ValueError:
        return None
    return now() - seen <= _PRESENCE_TIMEOUT


def list_cameras(cfg: Config) -> dict:
    camera_ids = sorted(cfg.cameras)
    cursors = state.get_cursors(cfg, camera_ids) if camera_ids else {}
    shadows: dict[str, dict | None] = {}
    out = []
    for cam in camera_ids:
        gw = cfg.cameras[cam]
        if gw and gw not in shadows:
            shadows[gw] = _get_shadow(cfg, gw)
        reported, desired, updated = _camera_state(shadows.get(gw), cam)
        c = cursors.get(cam, {})
        out.append({
            "camera_id": cam,
            "gateway_id": gw or None,
            "stream_name": cfg.stream_name(cam),
            "reported": reported,
            "desired": _mask(desired),
            "reported_at": updated,
            "online": _online(shadows.get(gw)),
            "video_processed_until": iso(c["VIDEO"]) if c.get("VIDEO") else None,
            "detect_processed_until": iso(c["DETECT"]) if c.get("DETECT") else None,
        })
    return {"cameras": out}


def set_streaming(cfg: Config, camera_id: str, body: object) -> dict:
    if not isinstance(body, dict) or not isinstance(body.get("streaming"), bool):
        raise bad_request('ボディは {"streaming": true|false} にしてください')
    gw = cfg.cameras.get(camera_id)
    if not gw:
        raise not_found(f"ゲートウェイが設定されていないカメラです: {camera_id}", "CAMERA_NOT_FOUND")
    streaming = body["streaming"]
    payload = {"state": {"desired": {"cameras": {camera_id: {"streaming": streaming}}}}}
    resp = aws.iot_data(cfg.iot_data_endpoint).update_thing_shadow(
        thingName=cfg.thing_name(gw), payload=json.dumps(payload).encode())
    result = json.loads(resp["payload"].read())
    return {"camera_id": camera_id, "streaming": streaming, "version": result.get("version")}
