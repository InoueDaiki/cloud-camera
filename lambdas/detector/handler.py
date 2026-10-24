"""物体検出 Lambda(1 分ごと)。

GetImages で 1 分区間を 1 秒間隔の JPEG(最大 60 枚)として取得 → メモリ上で前処理 → ONNX Runtime で推論
→ NMS → その日の Parquet の該当区間を置き換える。画像は S3 に置かない。

環境変数(Terraform と合意済み): STATE_TABLE, DETECTIONS_BUCKET, STREAM_NAME_PREFIX, LAG_SECONDS,
MAX_MINUTES, MAX_ATTEMPTS, MEDIA_RETENTION_DAYS(MAX_WAIT_SECONDS は video_archiver と共通の省略可能な値)
モデル関係(省略時は既定値): DETECT_MODEL, DETECT_INPUT_WIDTH, DETECT_IMAGE_HEIGHT, DETECT_THREADS,
DETECT_BATCH, DETECT_SCORE_THRESHOLD, DETECT_NMS_THRESHOLD
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

import boto3

import parquet_store
from common import MinuteResult, NotReady, kvs, log, max_rss_mb, run_pipeline
from common.timeutil import epoch_ms, from_epoch_ms, utcnow
from detect import Detector

HERE = os.path.dirname(os.path.abspath(__file__))

# 既定のモデル(models/{DETECT_MODEL}.onnx)。選定理由は README.md のベンチマーク
DEFAULT_MODEL = "yolox_tiny.int8"

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
            input_width=_env_int("DETECT_INPUT_WIDTH", 416),
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


def fetch_images(stream: str, start: datetime, end: datetime, fragments, width: int, height: int) -> list:
    """1 秒間隔の JPEG を取る。

    GetImages は範囲の始まりの近くに断片がないと ResourceNotFoundException になる(実測。録画が区間の途中から
    始まるときなど)。そのときは録画がある範囲ごとに「開始を秒に切り上げた時刻 〜 終わり」で取り直す
    (断片のちょうど開始時刻を指定すると MEDIA_ERROR が返ることがあるため切り上げる。video_archiver と同じ)。
    """
    try:
        return kvs.get_images(stream, start, end, interval_ms=1000, width=width, height=height)
    except Exception as e:  # noqa: BLE001
        if "ResourceNotFound" not in type(e).__name__ and "ResourceNotFound" not in str(e):
            raise
    out = []
    s_ms, e_ms = epoch_ms(start), epoch_ms(end)
    for seg_s, seg_e in kvs.build_segments(fragments):
        a = max(s_ms, -(-seg_s // 1000) * 1000)
        b = min(e_ms, seg_e)
        if a >= b:
            continue
        try:
            out += kvs.get_images(stream, from_epoch_ms(a), from_epoch_ms(b), interval_ms=1000,
                                  width=width, height=height)
        except Exception as e:  # noqa: BLE001
            if "ResourceNotFound" not in type(e).__name__ and "ResourceNotFound" not in str(e):
                raise
            log("detect_segment_no_images", stream=stream, seg_start_ms=a, seg_end_ms=b)
    return sorted(out, key=lambda im: im.ts_ms)


def process_minute(camera_id: str, start: datetime, end: datetime) -> MinuteResult:
    t0 = time.perf_counter()
    stream = kvs.stream_name_for(camera_id)
    detector = get_detector()
    width = detector.fixed_hw[1] if detector.fixed_hw else detector.input_width
    # GetImages は幅と高さを両方指定する必要がある(片方だけだと InvalidArgumentException)。
    # 既定は 16:9。カメラのアスペクト比が違うときは DETECT_IMAGE_HEIGHT で合わせる
    height = _env_int("DETECT_IMAGE_HEIGHT", round(width * 9 / 16))

    # 0. 断片が出そろうまで待つ(エッジの送信が遅れているとき。common.kvs.minute_ready)
    ready, why = kvs.minute_ready(stream, end, utcnow(), quiet_seconds=_env_int("LAG_SECONDS", 60),
                                  max_wait_seconds=_env_int("MAX_WAIT_SECONDS", 600))
    if not ready:
        raise NotReady(why)

    # 1. 取得(録画がなければ GetImages を呼ばない)
    t = time.perf_counter()
    fragments = kvs.list_fragments(stream, start, end)
    images = fetch_images(stream, start, end, fragments, width, height) if fragments else []
    # GetImages は終わりの時刻も含む(1 秒間隔で 61 枚)ので、start ≤ ts < end に絞る
    s_ms, e_ms = epoch_ms(start), epoch_ms(end)
    seen: set[int] = set()
    ok = []
    for im in images:
        if im.content and not im.error and s_ms <= im.ts_ms < e_ms and im.ts_ms not in seen:
            seen.add(im.ts_ms)
            ok.append(im)
    errors: dict[str, int] = {}
    for im in images:
        if im.error:
            errors[im.error] = errors.get(im.error, 0) + 1
    fetch_ms = (time.perf_counter() - t) * 1000

    # 2. 推論
    timings: dict = {}
    results = detector.detect([im.content for im in ok], timings) if ok else []
    frames = [
        (datetime.fromtimestamp(im.ts_ms / 1000, tz=timezone.utc), dets)
        for im, dets in zip(ok, results)
    ]
    rows = detections_to_rows(camera_id, frames, detector.model_name)

    # 3. その日の Parquet の、この区間の行を置き換える(検出なしでも、前回の行が残っていれば消す)
    stats = parquet_store.replace_minute(
        _s3_client(), os.environ["DETECTIONS_BUCKET"], camera_id, start, end, rows
    )

    n = len(ok)
    log(
        "detect_minute",
        camera_id=camera_id,
        start=start,
        fragments=len(fragments),
        frame_count=n,
        image_errors=errors,
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
    return run_pipeline(
        "DETECT",
        event["camera_id"],
        process_minute,
        lag_seconds=_env_int("LAG_SECONDS", 60),
        max_minutes=_env_int("MAX_MINUTES", 5),
        max_attempts=_env_int("MAX_ATTEMPTS", 3),
        context=context,
    )
