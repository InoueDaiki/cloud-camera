"""DynamoDB `{p}-state` の読み取り(処理済み位置と区間の件)。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterator

from . import aws
from .config import Config
from .timeutil import iso, parse_ts

CURSOR_KINDS = ("VIDEO", "DETECT")


def get_cursors(cfg: Config, camera_ids: list[str]) -> dict[str, dict[str, datetime | None]]:
    """カメラごとの {"VIDEO": datetime|None, "DETECT": datetime|None}。BatchGetItem でまとめて読む。"""
    result: dict[str, dict[str, datetime | None]] = {c: {k: None for k in CURSOR_KINDS} for c in camera_ids}
    keys = [{"camera_id": c, "sk": f"CURSOR#{k}"} for c in camera_ids for k in CURSOR_KINDS]
    ddb = aws.table(cfg.state_table).meta.client
    for i in range(0, len(keys), 100):
        req = {cfg.state_table: {
            "Keys": keys[i:i + 100],
            "ProjectionExpression": "camera_id, sk, processed_until",
        }}
        while req:
            resp = ddb.batch_get_item(RequestItems=req)
            for item in resp.get("Responses", {}).get(cfg.state_table, []):
                cam = item["camera_id"]["S"] if isinstance(item["camera_id"], dict) else item["camera_id"]
                sk = item["sk"]["S"] if isinstance(item["sk"], dict) else item["sk"]
                pu = item.get("processed_until")
                if isinstance(pu, dict):
                    pu = pu.get("S")
                kind = sk.split("#", 1)[1]
                if cam in result and kind in CURSOR_KINDS and pu:
                    result[cam][kind] = parse_ts(pu, "processed_until")
            req = resp.get("UnprocessedKeys") or None
    return result


def query_minutes(cfg: Config, camera_id: str, start: datetime, end: datetime,
                  attributes: list[str]) -> Iterator[dict]:
    """SK が [start, end) の区間の件を古い順に返す。ページ送り(LastEvaluatedKey)に対応。

    start / end は 1 分境界を想定。SK は ISO 文字列なので、文字列の範囲で絞れる
    (`CURSOR#...` は数字より後ろに並ぶので含まれない)。
    """
    if end <= start:
        return
    last_sk = iso(end - timedelta(minutes=1))
    names = {f"#a{i}": a.split(".")[0] for i, a in enumerate(attributes)}
    proj_parts = []
    for i, a in enumerate(attributes):
        rest = a.split(".")[1:]
        proj_parts.append(".".join([f"#a{i}", *rest]))
    names["#pk"] = "camera_id"
    names["#sk"] = "sk"
    kwargs = {
        "KeyConditionExpression": "#pk = :c AND #sk BETWEEN :a AND :b",
        "ExpressionAttributeNames": names,
        "ExpressionAttributeValues": {":c": camera_id, ":a": iso(start), ":b": last_sk},
        "ProjectionExpression": ", ".join(["#sk", *proj_parts]),
    }
    tbl = aws.table(cfg.state_table)
    while True:
        resp = tbl.query(**kwargs)
        yield from resp.get("Items", [])
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek
