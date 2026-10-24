"""Athena でクエリを実行して結果を返す(結果の出力先とスキャン上限はワークグループの設定に任せる)。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import aws
from .config import Config


class AthenaError(RuntimeError):
    pass


def parse_timestamp(value: str) -> datetime | None:
    """Athena の timestamp 文字列を読む。

    '2026-10-24 01:23:45.123' は UTC、'2026-10-24 10:00:00.000 Asia/Tokyo'(timestamp with time zone)はその地域の時刻。
    """
    v = value.strip()
    zone = None
    parts = v.split(" ")
    if len(parts) == 3:
        v, zone = f"{parts[0]} {parts[1]}", parts[2]
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        if zone and zone != "UTC":
            try:
                dt = dt.replace(tzinfo=ZoneInfo(zone))
            except (ZoneInfoNotFoundError, ValueError):
                return None
        else:
            dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass
class QueryResult:
    columns: list[str]
    types: list[str]
    rows: list[list[str | None]]
    bytes_scanned: int
    execution_ms: int
    query_execution_id: str


def run(cfg: Config, sql: str, *, timeout_s: float = 20.0, max_rows: int = 100) -> QueryResult:
    client = aws.client("athena")
    resp = client.start_query_execution(
        QueryString=sql,
        QueryExecutionContext={"Database": cfg.glue_database},
        WorkGroup=cfg.athena_workgroup,
    )
    qid = resp["QueryExecutionId"]
    deadline = time.monotonic() + timeout_s
    delay = 0.2
    while True:
        q = client.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
        st = q["Status"]["State"]
        if st in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        if time.monotonic() > deadline:
            client.stop_query_execution(QueryExecutionId=qid)
            raise AthenaError(f"クエリが {timeout_s:.0f} 秒以内に終わりませんでした")
        time.sleep(delay)
        delay = min(delay * 1.5, 1.0)
    stats = q.get("Statistics") or {}
    scanned = int(stats.get("DataScannedInBytes") or 0)
    if st != "SUCCEEDED":
        reason = q["Status"].get("StateChangeReason") or st
        err = AthenaError(reason)
        err.bytes_scanned = scanned  # type: ignore[attr-defined]
        raise err

    columns: list[str] = []
    types: list[str] = []
    rows: list[list[str | None]] = []
    kwargs = {"QueryExecutionId": qid, "MaxResults": min(max_rows + 1, 1000)}
    first = True
    while True:
        page = client.get_query_results(**kwargs)
        rs = page["ResultSet"]
        if first:
            info = rs["ResultSetMetadata"]["ColumnInfo"]
            columns = [c["Name"] for c in info]
            types = [c["Type"] for c in info]
        data = rs.get("Rows", [])
        if first and data:
            data = data[1:]  # 1 行目は列名
        first = False
        for r in data:
            rows.append([d.get("VarCharValue") for d in r["Data"]])
        token = page.get("NextToken")
        if not token or len(rows) >= max_rows:
            break
        kwargs["NextToken"] = token
    return QueryResult(columns, types, rows[:max_rows], scanned,
                       int(stats.get("TotalExecutionTimeInMillis") or 0), qid)
