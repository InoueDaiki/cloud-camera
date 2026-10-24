"""動画の退避 Lambda(EventBridge Scheduler から 1 分ごと、入力 {"camera_id": "cam-001"})。"""

from __future__ import annotations

import os

from archiver import Archiver, env_int
from common import log, run_pipeline

_archiver: Archiver | None = None


def _get_archiver() -> Archiver:
    global _archiver
    if _archiver is None:
        _archiver = Archiver(bucket=os.environ["MEDIA_BUCKET"])
    return _archiver


def lambda_handler(event, context):
    camera_id = event["camera_id"]
    summary = run_pipeline(
        "VIDEO",
        camera_id,
        _get_archiver().process_minute,
        lag_seconds=env_int("LAG_SECONDS", 30),
        max_minutes=env_int("MAX_MINUTES", 5),
        max_attempts=env_int("MAX_ATTEMPTS", 3),
        context=context,
    )
    log("handler_done", camera_id=camera_id, stopped=summary.get("stopped"),
        processed_until=summary.get("processed_until"), minutes=len(summary.get("minutes", [])))
    return summary
