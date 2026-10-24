"""POST /api/search: Bedrock Converse のツール使用で、質問 → SQL → Athena → 回答。

流れ:
1. システムプロンプト(スキーマ、COCO ラベル、現在時刻、日本時間の扱い)と質問を Converse に渡す
2. モデルが query_detections を呼んだら、SQL をガードしてから Athena で実行し、結果を返す
3. モデルが submit_answer を呼んだら終わり。hits は検索結果に実在した時刻だけ残す
ツールの呼び出し回数と全体の時間に上限を設け、超えたら submit_answer を強制する。
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from botocore.exceptions import ClientError

from . import athena, aws, prompt, sql_guard
from .config import Config
from .http import ApiError, bad_request
from .timeutil import JST, UTC, iso_auto, now, parse_ts

MAX_QUESTION_CHARS = 500
MAX_QUERIES = 4          # query_detections の呼び出し回数の上限
MAX_TURNS = MAX_QUERIES + 2
MAX_HITS = 20
MAX_TOKENS = 1500


def _budget_seconds() -> float:
    # API Gateway の統合タイムアウト(30 秒)より手前で切り上げる
    return float(os.environ.get("SEARCH_TIMEOUT_SECONDS", "25"))


def _athena_ts(value: str) -> datetime | None:
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
            dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _format_result(res: athena.QueryResult, seen_ts: dict[str, dict]) -> dict:
    """モデルへ返す形。timestamp 列は ISO(UTC)に直し、日本時間の列を足す。"""
    ts_cols = {i for i, t in enumerate(res.types) if t.lower().startswith("timestamp")}
    out_rows = []
    for row in res.rows:
        d: dict[str, object] = {}
        for i, (col, val) in enumerate(zip(res.columns, row)):
            if i in ts_cols and val:
                dt = _athena_ts(val)
                if dt is not None:
                    d[col] = iso_auto(dt)
                    d[f"{col}_jst"] = dt.astimezone(JST).strftime("%Y-%m-%d %H:%M:%S")
                    continue
            d[col] = val
        # hits の検証用に、結果に出てきた時刻を覚えておく
        for i in ts_cols:
            if row[i]:
                dt = _athena_ts(row[i])
                if dt is not None:
                    info = seen_ts.setdefault(iso_auto(dt), {})
                    if "camera_id" in d and d["camera_id"]:
                        info.setdefault("camera_id", d["camera_id"])
                    if "label" in d and d["label"]:
                        info.setdefault("label", d["label"])
                    if "confidence" in d and d["confidence"] is not None:
                        info.setdefault("confidence", d["confidence"])
        out_rows.append(d)
    return {
        "row_count": len(out_rows),
        "rows": out_rows,
        "bytes_scanned": res.bytes_scanned,
        "note": "行数が上限に達しています。必要なら集計して絞り込んでください。" if len(out_rows) >= sql_guard.MAX_ROWS else None,
    }


def _clean_hits(raw: object, seen_ts: dict[str, dict], cameras: set[str]) -> list[dict]:
    hits = []
    seen_keys = set()
    if not isinstance(raw, list):
        return hits
    for h in raw:
        if not isinstance(h, dict):
            continue
        try:
            dt = parse_ts(str(h.get("ts", "")))
        except ApiError:
            continue
        key = iso_auto(dt)
        info = seen_ts.get(key)
        if info is None:
            continue  # 検索結果にない時刻(モデルが作った値)は捨てる
        cam = str(h.get("camera_id") or info.get("camera_id") or "")
        if cameras and cam not in cameras:
            continue
        label = h.get("label") or info.get("label")
        conf = h.get("confidence", info.get("confidence"))
        try:
            conf = round(float(conf), 3) if conf is not None else None
        except (TypeError, ValueError):
            conf = None
        if (cam, key) in seen_keys:
            continue
        seen_keys.add((cam, key))
        hits.append({
            "camera_id": cam,
            "ts": key,
            "ts_jst": dt.astimezone(JST).isoformat(timespec="seconds"),
            "label": label,
            "confidence": conf,
        })
    hits.sort(key=lambda x: (x["ts"], x["camera_id"]))
    return hits[:MAX_HITS]


def search(cfg: Config, body: object) -> dict:
    if not isinstance(body, dict) or not isinstance(body.get("question"), str) or not body["question"].strip():
        raise bad_request('ボディは {"question": "..."} にしてください')
    question = body["question"].strip()
    if len(question) > MAX_QUESTION_CHARS:
        raise bad_request(f"質問は {MAX_QUESTION_CHARS} 文字以内にしてください")

    started = time.monotonic()
    deadline = started + _budget_seconds()
    t_now = now()
    system = prompt.system_prompt(
        now=t_now, database=cfg.glue_database, table=cfg.glue_table, cameras=sorted(cfg.cameras),
        max_rows=sql_guard.MAX_ROWS, max_dt_days=sql_guard.MAX_DT_DAYS, max_queries=MAX_QUERIES)
    tool_cfg = prompt.tool_config(sql_guard.MAX_ROWS)
    messages: list[dict] = [{"role": "user", "content": [{"text": question}]}]
    bedrock = aws.client("bedrock-runtime")

    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
             "cache_read_input_tokens": 0, "cache_write_input_tokens": 0}
    queries: list[dict] = []
    seen_ts: dict[str, dict] = {}
    last_sql: str | None = None
    answer: str | None = None
    raw_hits: object = []
    bedrock_calls = 0
    query_calls = 0

    for turn in range(MAX_TURNS):
        remaining = deadline - time.monotonic()
        force_answer = query_calls >= MAX_QUERIES or remaining < 8 or turn == MAX_TURNS - 1
        cfg_turn = dict(tool_cfg)
        cfg_turn["toolChoice"] = {"tool": {"name": "submit_answer"}} if force_answer else {"any": {}}
        try:
            resp = bedrock.converse(
                modelId=cfg.bedrock_model_id,
                system=[{"text": system}],
                messages=messages,
                toolConfig=cfg_turn,
                inferenceConfig={"maxTokens": MAX_TOKENS, "temperature": 0},
            )
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "BedrockError")
            msg = e.response.get("Error", {}).get("Message", "")
            status = 429 if code == "ThrottlingException" else 502
            raise ApiError(status, f"BEDROCK_{code}", f"Bedrock の呼び出しに失敗しました: {msg}") from e
        bedrock_calls += 1
        u = resp.get("usage") or {}
        usage["input_tokens"] += int(u.get("inputTokens", 0))
        usage["output_tokens"] += int(u.get("outputTokens", 0))
        usage["total_tokens"] += int(u.get("totalTokens", 0))
        usage["cache_read_input_tokens"] += int(u.get("cacheReadInputTokens", 0))
        usage["cache_write_input_tokens"] += int(u.get("cacheWriteInputTokens", 0))

        msg = resp["output"]["message"]
        messages.append(msg)
        tool_uses = [c["toolUse"] for c in msg.get("content", []) if "toolUse" in c]
        texts = [c["text"] for c in msg.get("content", []) if "text" in c]
        if not tool_uses:
            answer = "\n".join(texts).strip() or None
            break

        results = []
        done = False
        for tu in tool_uses:
            name, tid, inp = tu.get("name"), tu.get("toolUseId"), tu.get("input") or {}
            if name == "submit_answer":
                answer = str(inp.get("answer") or "").strip() or None
                raw_hits = inp.get("hits") or []
                done = True
                results.append({"toolResult": {"toolUseId": tid, "content": [{"text": "OK"}]}})
                continue
            if name != "query_detections":
                results.append({"toolResult": {"toolUseId": tid, "status": "error",
                                               "content": [{"text": f"未知のツールです: {name}"}]}})
                continue
            query_calls += 1
            sql = str(inp.get("sql") or "")
            record: dict = {"sql": sql}
            queries.append(record)
            if query_calls > MAX_QUERIES:
                record["error"] = "呼び出し回数の上限を超えました"
                results.append({"toolResult": {"toolUseId": tid, "status": "error",
                                               "content": [{"text": "検索の回数上限に達しました。submit_answer で答えてください。"}]}})
                continue
            try:
                g = sql_guard.guard(sql, table=cfg.glue_table, database=cfg.glue_database)
            except sql_guard.SqlRejected as e:
                record["error"] = f"rejected: {e}"
                results.append({"toolResult": {"toolUseId": tid, "status": "error",
                                               "content": [{"text": f"SQL を実行しませんでした: {e}"}]}})
                continue
            record["sql"] = g.sql
            try:
                res = athena.run(cfg, g.sql, timeout_s=max(3.0, min(15.0, deadline - time.monotonic() - 5)))
            except athena.AthenaError as e:
                record["error"] = str(e)
                record["bytes_scanned"] = getattr(e, "bytes_scanned", 0)
                results.append({"toolResult": {"toolUseId": tid, "status": "error",
                                               "content": [{"text": f"Athena のエラー: {e}"}]}})
                continue
            last_sql = g.sql
            record["bytes_scanned"] = res.bytes_scanned
            record["row_count"] = len(res.rows)
            record["execution_ms"] = res.execution_ms
            payload = _format_result(res, seen_ts)
            results.append({"toolResult": {"toolUseId": tid,
                                           "content": [{"json": payload}]}})
        if done:
            break
        messages.append({"role": "user", "content": results})

    if answer is None:
        answer = "回答を作れませんでした。質問を言い換えてもう一度お試しください。"
    hits = _clean_hits(raw_hits, seen_ts, set(cfg.cameras))
    usage.update({
        "model_id": cfg.bedrock_model_id,
        "bedrock_calls": bedrock_calls,
        "tool_calls": query_calls,
        "athena_bytes_scanned": sum(int(q.get("bytes_scanned") or 0) for q in queries),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    })
    return {
        "question": question,
        "answer": answer,
        "hits": hits,
        "sql": last_sql,
        "queries": queries,
        "usage": usage,
        "now": t_now.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }


def dumps_for_log(result: dict) -> str:
    return json.dumps({k: result[k] for k in ("question", "sql", "usage")}, ensure_ascii=False)
