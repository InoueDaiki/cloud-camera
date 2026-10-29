"""GET /api/cameras と PUT /api/cameras/{id}/streaming、/codec、/encoder(IoT のクラシックシャドウ)。"""

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


CODECS = ("H265", "H264")


def _update_desired(cfg: Config, camera_id: str, fields: dict) -> dict:
    gw = cfg.cameras.get(camera_id)
    if not gw:
        raise not_found(f"ゲートウェイが設定されていないカメラです: {camera_id}", "CAMERA_NOT_FOUND")
    payload = {"state": {"desired": {"cameras": {camera_id: fields}}}}
    resp = aws.iot_data(cfg.iot_data_endpoint).update_thing_shadow(
        thingName=cfg.thing_name(gw), payload=json.dumps(payload).encode())
    return json.loads(resp["payload"].read())


def set_codec(cfg: Config, camera_id: str, body: object) -> dict:
    """映像形式をシャドウの desired.cameras.{id}.codec に書く。エッジがカメラのエンコーダーを切り替え(ONVIF)、
    パイプラインを組み直して reported.codec に実際の形式を返す(docs/codec.md)。"""
    codec = body.get("codec") if isinstance(body, dict) else None
    if isinstance(codec, str):
        codec = codec.strip().upper().replace(".", "")
        codec = {"HEVC": "H265", "AVC": "H264"}.get(codec, codec)
    if codec not in CODECS:
        raise bad_request('ボディは {"codec": "H265"|"H264"} にしてください')
    result = _update_desired(cfg, camera_id, {"codec": codec})
    return {"camera_id": camera_id, "codec": codec, "version": result.get("version")}


RATE_CONTROLS = ("VBR", "CBR")
_ENCODER_USAGE = ('ボディは {"quality": 数値, "bitrate_limit_kbps": 正の整数, "rate_control": "VBR"|"CBR"} '
                  "の 1 つ以上にしてください(null でその項目を消す)")


def _number(v: object) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def set_encoder(cfg: Config, camera_id: str, body: object) -> dict:
    """エンコーダーの設定(ONVIF の Quality、ビットレート上限、VBR / CBR)をシャドウの desired.cameras.{id}.encoder に書く。
    書いた項目だけが変わる(シャドウは入れ子のオブジェクトを合わせる)。エッジがパイプラインを作り直さずに
    カメラへ設定し、reported.encoder に実際の値を返す(docs/codec.md)。"""
    keys = ("quality", "bitrate_limit_kbps", "rate_control")
    if not isinstance(body, dict) or not body or set(body) - set(keys):
        raise bad_request(_ENCODER_USAGE)
    fields: dict = {}
    for k in keys:
        if k not in body:
            continue
        v = body[k]
        if v is None:
            fields[k] = None
        elif k == "quality" and _number(v) and v >= 0:
            fields[k] = v
        elif k == "bitrate_limit_kbps" and _number(v) and v > 0 and float(v).is_integer():
            fields[k] = int(v)
        elif k == "rate_control" and isinstance(v, str) and v.strip().upper() in RATE_CONTROLS:
            fields[k] = v.strip().upper()
        else:
            raise bad_request(_ENCODER_USAGE)
    result = _update_desired(cfg, camera_id, {"encoder": fields})
    return {"camera_id": camera_id, "encoder": fields, "version": result.get("version")}


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
