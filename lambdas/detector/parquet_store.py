"""日ごとの Parquet(detections/dt=YYYY-MM-DD/{camera_id}.parquet)への追記。

1. その日のファイルを読む(なければ空)。ETag を覚えておく
2. 書く 1 分区間(start ≤ ts < end)の行を取り除き、新しい行を足す(再実行しても重複しない)
3. 条件付き PUT(既存なら If-Match: ETag、新規なら If-None-Match: *)で書き直す
4. 競合(412 / 409)したら読み直して 1 からやり直す

ファイル名は区間の開始時刻(UTC)の日付で決めるので、0 時をまたぐ区間も開始日のファイルに入る。
"""

from __future__ import annotations

import io
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

log = logging.getLogger(__name__)

SCHEMA = pa.schema(
    [
        ("camera_id", pa.string()),
        ("ts", pa.timestamp("ms", tz="UTC")),
        ("label", pa.string()),
        ("confidence", pa.float32()),
        ("x", pa.float32()),
        ("y", pa.float32()),
        ("w", pa.float32()),
        ("h", pa.float32()),
        ("model", pa.string()),
    ]
)

CONFLICT_CODES = {"PreconditionFailed", "ConditionalRequestConflict", "412", "409"}


class ConflictError(Exception):
    """条件付き PUT の再試行回数を使い切った。"""


@dataclass
class WriteStats:
    key: str
    rows_before: int = 0
    rows_removed: int = 0
    rows_added: int = 0
    bytes: int = 0
    attempts: int = 0
    put: bool = False
    read_ms: float = 0.0
    write_ms: float = 0.0
    extra: dict = field(default_factory=dict)


def object_key(camera_id: str, start: datetime) -> str:
    return f"detections/dt={start.astimezone(timezone.utc):%Y-%m-%d}/{camera_id}.parquet"


def rows_to_table(rows: list[dict]) -> pa.Table:
    if not rows:
        return SCHEMA.empty_table()
    return pa.Table.from_pylist(rows, schema=SCHEMA)


def _read(s3, bucket: str, key: str) -> tuple[pa.Table | None, str | None]:
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
    except s3.exceptions.NoSuchKey:
        return None, None
    body = obj["Body"].read()
    # read_table は pyarrow.dataset を経由するので、軽い ParquetFile で読む
    table = pq.ParquetFile(io.BytesIO(body)).read()
    return table, obj["ETag"]


def _merge(existing: pa.Table | None, new: pa.Table, start: datetime, end: datetime) -> tuple[pa.Table, int]:
    if existing is None or existing.num_rows == 0:
        return new, 0
    existing = existing.select(SCHEMA.names).cast(SCHEMA)
    ts = existing.column("ts")
    lo = pa.scalar(start, type=SCHEMA.field("ts").type)
    hi = pa.scalar(end, type=SCHEMA.field("ts").type)
    in_minute = pc.and_(pc.greater_equal(ts, lo), pc.less(ts, hi))
    kept = existing.filter(pc.invert(in_minute))
    merged = pa.concat_tables([kept, new]) if new.num_rows else kept
    # 時刻順に並べておくと、Athena の行グループの統計が効きやすい
    merged = merged.sort_by([("ts", "ascending")])
    return merged, existing.num_rows - kept.num_rows


def _serialize(table: pa.Table) -> bytes:
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="zstd", use_dictionary=["camera_id", "label", "model"])
    return buf.getvalue()


def _is_conflict(err: Exception) -> bool:
    resp = getattr(err, "response", None) or {}
    code = str(resp.get("Error", {}).get("Code", ""))
    status = str(resp.get("ResponseMetadata", {}).get("HTTPStatusCode", ""))
    return code in CONFLICT_CODES or status in ("409", "412")


def replace_minute(
    s3,
    bucket: str,
    camera_id: str,
    start: datetime,
    end: datetime,
    rows: list[dict],
    *,
    max_attempts: int = 5,
) -> WriteStats:
    """その日の Parquet の [start, end) の行を rows で置き換える。冪等。"""
    key = object_key(camera_id, start)
    new = rows_to_table(rows)
    stats = WriteStats(key=key, rows_added=new.num_rows)
    for attempt in range(1, max_attempts + 1):
        stats.attempts = attempt
        t = time.perf_counter()
        existing, etag = _read(s3, bucket, key)
        stats.read_ms += (time.perf_counter() - t) * 1000
        merged, removed = _merge(existing, new, start, end)
        stats.rows_before = existing.num_rows if existing is not None else 0
        stats.rows_removed = removed
        if removed == 0 and new.num_rows == 0:
            # 書き換えるものがない(検出なしの区間)。PUT を省く
            stats.put = False
            return stats
        body = _serialize(merged)
        cond = {"IfMatch": etag} if etag else {"IfNoneMatch": "*"}
        t = time.perf_counter()
        try:
            s3.put_object(
                Bucket=bucket, Key=key, Body=body,
                ContentType="application/vnd.apache.parquet", **cond,
            )
        except Exception as e:  # botocore.exceptions.ClientError
            stats.write_ms += (time.perf_counter() - t) * 1000
            if not _is_conflict(e):
                raise
            log.warning("条件付き PUT が競合したので読み直す", extra={"key": key, "attempt": attempt})
            time.sleep(min(2.0, 0.1 * 2**attempt) * random.random())
            continue
        stats.write_ms += (time.perf_counter() - t) * 1000
        stats.bytes = len(body)
        stats.put = True
        return stats
    raise ConflictError(f"{key}: 条件付き PUT が {max_attempts} 回競合した")
