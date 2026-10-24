"""物体検出 Lambda(1 分ごと)。

動画の退避が S3 に置いた 1 分の clip.mp4 からキーフレーム(1 秒に 1 枚、最大 60 枚)をデコード → メモリ上で前処理
→ ONNX Runtime で推論 → NMS → その日の Parquet の該当区間を置き換える。画像は S3 に置かない。
動画の退避が失敗した区間だけ GetImages で取る(frame_source.py)。

環境変数(Terraform と合意済み): STATE_TABLE, MEDIA_BUCKET, DETECTIONS_BUCKET, STREAM_NAME_PREFIX, LAG_SECONDS,
MAX_MINUTES, MAX_ATTEMPTS, MEDIA_RETENTION_DAYS(断片が出そろうまでの待ちは common の run_pipeline が MAX_LATE_SECONDS で判断する)
フレーム源: FRAME_SOURCE(clip / getimages、既定 clip)、UPSTREAM_MAX_WAIT_SECONDS(動画の退避を待つ上限、既定 1800)
モデル関係(省略時は既定値): DETECT_MODEL, DETECT_INPUT_WIDTH, DETECT_IMAGE_HEIGHT, DETECT_THREADS,
DETECT_BATCH, DETECT_SCORE_THRESHOLD, DETECT_NMS_THRESHOLD
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

import boto3

import frame_source
import parquet_store
from common import MinuteResult, log, max_rss_mb, run_pipeline
from detect import Detector

HERE = os.path.dirname(os.path.abspath(__file__))

# 既定のモデル(models/{DETECT_MODEL}.onnx)。選定理由は README.md のベンチマーク
DEFAULT_MODEL = "yolox_tiny.int8s8"

# 1 分区間のフレームの取り方。(camera_id, start, end) -> frame_source.FrameBatch
FRAME_SOURCE: frame_source.FrameSource = frame_source.select(os.environ.get("FRAME_SOURCE"))

_detector: Detector | None = None
_s3 = None


def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    return int(v) if v else default


def _env_float(name: str, default: float) -> float:
    v = os.environ.get(name)
    return float(v) if v else default


def get_detector() -> Detector:
    """コールドスタートで 1 回だけモデルを読み込む。"""
    global _detector
    if _detector is None:
        model = os.environ.get("DETECT_MODEL", DEFAULT_MODEL)
        _detector = Detector(
            os.path.join(HERE, "models", f"{model}.onnx"),
            input_width=_env_int("DETECT_INPUT_WIDTH", 640),
            threads=_env_int("DETECT_THREADS", 0),
            batch_size=_env_int("DETECT_BATCH", 1),
            score_threshold=_env_float("DETECT_SCORE_THRESHOLD", 0.3),
            nms_threshold=_env_float("DETECT_NMS_THRESHOLD", 0.45),
        )
        log("model_loaded", model=model, load_ms=round(_detector.load_ms, 1))
    return _detector


def _s3_client():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


def detections_to_rows(camera_id: str, frames: list[tuple[datetime, list]], model: str) -> list[dict]:
    """(フレーム時刻, 検出のリスト) の並びを契約の Parquet 行にする。"""
    rows = []
    for ts, dets in frames:
        for d in dets:
            rows.append(
                {
                    "camera_id": camera_id,
                    "ts": ts,
                    "label": d.label,
                    "confidence": d.confidence,
                    "x": d.x,
                    "y": d.y,
                    "w": d.w,
                    "h": d.h,
                    "model": model,
                }
            )
    return rows


def process_minute(camera_id: str, start: datetime, end: datetime) -> MinuteResult:
    t0 = time.perf_counter()
    detector = get_detector()
    # 1. フレームの取得(既定は S3 の clip.mp4。FRAME_SOURCE で差し替えられる)。動画の退避がまだなら NotReady
    t = time.perf_counter()
    batch = FRAME_SOURCE(camera_id, start, end)
    fetch_ms = (time.perf_counter() - t) * 1000

    # 2. 推論
    timings: dict = {}
    results = detector.detect([img for _, img in batch.frames], timings) if batch.frames else []
    frames = [(ts, dets) for (ts, _), dets in zip(batch.frames, results)]
    rows = detections_to_rows(camera_id, frames, detector.model_name)

    # 3. その日の Parquet の、この区間の行を置き換える(検出なしでも、前回の行が残っていれば消す)
    stats = parquet_store.replace_minute(
        _s3_client(), os.environ["DETECTIONS_BUCKET"], camera_id, start, end, rows
    )

    n = len(batch.frames)
    log(
        "detect_minute",
        camera_id=camera_id,
        start=start,
        frame_count=n,
        **batch.stats,
        detect_count=len(rows),
        model=detector.model_name,
        fetch_ms=round(fetch_ms, 1),
        preprocess_ms=round(timings.get("preprocess_ms", 0), 1),
        inference_ms=round(timings.get("inference_ms", 0), 1),
        postprocess_ms=round(timings.get("postprocess_ms", 0), 1),
        inference_ms_per_frame=round(timings.get("inference_ms", 0) / n, 2) if n else None,
        write_read_ms=round(stats.read_ms, 1),
        write_put_ms=round(stats.write_ms, 1),
        write_put=stats.put,
        write_attempts=stats.attempts,
        parquet_rows=stats.rows_before - stats.rows_removed + stats.rows_added,
        parquet_bytes=stats.bytes,
        total_ms=round((time.perf_counter() - t0) * 1000, 1),
        max_rss_mb=max_rss_mb(),
    )
    return MinuteResult(
        "DONE" if n else "EMPTY",
        {"detect_count": len(rows), "frame_count": n},
    )


def lambda_handler(event, context):
    # clip.mp4 を読むときは、動画の退避の処理済み位置(CURSOR#VIDEO)を越えて処理しない(common の README「上流を待つ」)
    upstream = "VIDEO" if FRAME_SOURCE is frame_source.clipfile else None
    return run_pipeline(
        "DETECT",
        event["camera_id"],
        process_minute,
        lag_seconds=_env_int("LAG_SECONDS", 60),
        max_minutes=_env_int("MAX_MINUTES", 5),
        max_attempts=_env_int("MAX_ATTEMPTS", 3),
        context=context,
        upstream_kind=upstream,
    )
