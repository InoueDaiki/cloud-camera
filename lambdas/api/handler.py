"""ビューワー向け HTTP API(API Gateway HTTP API、ペイロード 2.0)。1 本の Lambda でルーティングする。

| メソッドとパス | 処理 |
| GET  /api/cameras                      | カメラ一覧と状態 |
| PUT  /api/cameras/{id}/streaming       | シャドウの desired を更新 |
| PUT  /api/cameras/{id}/codec           | 映像形式(H265 / H264)をシャドウの desired に書く |
| GET  /api/cameras/{id}/timeline        | 録画範囲(S3 と KVS の境界を隠す) |
| GET  /api/cameras/{id}/vod.m3u8        | S3 退避済み区間を連結した HLS |
| GET  /api/cameras/{id}/kvs-hls         | KVS の HLS セッション URL |
| GET  /api/cameras/{id}/webrtc          | KVS WebRTC の視聴情報(署名付き WSS URL、ICE サーバー) |
| GET  /api/cameras/{id}/ptz             | PTZ の状態(シャドウの reported) |
| POST /api/cameras/{id}/ptz             | PTZ の操作(IoT Core の cmd トピックへ publish、202) |
| POST /api/search                       | 自然言語検索 |
"""

from __future__ import annotations

import json
import logging
import re
import traceback

from app import cameras, config, kvs, playlist, ptz, search, timeline, webrtc
from app.http import ApiError, Request, error_response, json_response, not_found, text_response

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_CAM = r"(?P<id>[A-Za-z0-9_.\-]{1,64})"
ROUTES = [
    ("GET", re.compile(r"^/api/cameras/?$"), "cameras"),
    ("PUT", re.compile(rf"^/api/cameras/{_CAM}/streaming$"), "streaming"),
    ("PUT", re.compile(rf"^/api/cameras/{_CAM}/codec$"), "codec"),
    ("GET", re.compile(rf"^/api/cameras/{_CAM}/timeline$"), "timeline"),
    ("GET", re.compile(rf"^/api/cameras/{_CAM}/vod\.m3u8$"), "vod"),
    ("GET", re.compile(rf"^/api/cameras/{_CAM}/kvs-hls$"), "kvs_hls"),
    ("GET", re.compile(rf"^/api/cameras/{_CAM}/webrtc$"), "webrtc"),
    ("GET", re.compile(rf"^/api/cameras/{_CAM}/ptz$"), "ptz_get"),
    ("POST", re.compile(rf"^/api/cameras/{_CAM}/ptz$"), "ptz_post"),
    ("POST", re.compile(r"^/api/search/?$"), "search"),
]


def _require_camera(cfg: config.Config, camera_id: str) -> None:
    if cfg.cameras and camera_id not in cfg.cameras:
        raise not_found(f"カメラがありません: {camera_id}", "CAMERA_NOT_FOUND")


def dispatch(req: Request) -> dict:
    cfg = config.load()
    path_matched = False
    for method, pattern, name in ROUTES:
        m = pattern.match(req.path)
        if not m:
            continue
        path_matched = True
        if req.method != method:
            continue
        req.path_params = m.groupdict()
        cam = req.path_params.get("id")
        if cam:
            _require_camera(cfg, cam)
        if name == "cameras":
            return json_response(200, cameras.list_cameras(cfg))
        if name == "streaming":
            return json_response(200, cameras.set_streaming(cfg, cam, req.json()))
        if name == "codec":
            return json_response(200, cameras.set_codec(cfg, cam, req.json()))
        if name == "timeline":
            return json_response(200, timeline.build(cfg, cam, req.query))
        if name == "vod":
            body = playlist.build(cfg, cam, req.query)
            return text_response(200, body, "application/vnd.apple.mpegurl",
                                 {"cache-control": "max-age=30"})
        if name == "kvs_hls":
            return json_response(200, kvs.hls_session(cfg, cam, req.query))
        if name == "webrtc":
            return json_response(200, webrtc.viewer_session(cfg, cam))
        if name == "ptz_get":
            return json_response(200, ptz.get_state(cfg, cam))
        if name == "ptz_post":
            return json_response(202, ptz.command(cfg, cam, req.json()))
        if name == "search":
            result = search.search(cfg, req.json())
            logger.info("search %s", search.dumps_for_log(result))
            return json_response(200, result)
    if path_matched:
        raise ApiError(405, "METHOD_NOT_ALLOWED", f"{req.method} は使えません")
    raise not_found(f"パスがありません: {req.path}")


def lambda_handler(event, context):
    try:
        req = Request.from_event(event)
    except Exception as e:  # noqa: BLE001
        return error_response(400, "BAD_REQUEST", f"リクエストを読めません: {e}")
    try:
        return dispatch(req)
    except ApiError as e:
        return error_response(e.status, e.code, e.message)
    except Exception as e:  # noqa: BLE001
        logger.error("unhandled %s %s: %s\n%s", req.method, req.path, e, traceback.format_exc())
        return error_response(500, "INTERNAL", f"{type(e).__name__}: {e}")


if __name__ == "__main__":  # 手元での簡単な確認用
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else "/api/cameras"
    print(json.dumps(lambda_handler({"rawPath": path, "requestContext": {"http": {"method": "GET"}}}, None),
                     ensure_ascii=False, indent=2))
