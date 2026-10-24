"""POST /api/search: Bedrock Converse のツール使用で、質問 → 検索 → 回答。

流れ:
1. システムプロンプト(日付の早見表、カメラ名、ラベルの言い換え、データの意味、回答の型)と質問を Converse に渡す
2. モデルが search_detections(引数付き。SQL はサーバーが組み、検出をイベントにまとめる)か
   query_detections(自由 SQL。ガードしてから実行)を呼んだら、Athena で実行して結果を返す
3. モデルが submit_answer を呼んだら、回答を検査する(「3人」のような数え方、イベント数より多い「n 回」)。
   問題があれば 1 回だけ書き直させる。hits は検索結果に実在したイベント・時刻だけ残す
ツールの呼び出し回数と全体の時間に上限を設け、超えたら submit_answer を強制する。

評価(bench/)で段階ごとに比べるため、機能を環境変数 SEARCH_FEATURES(カンマ区切り)で切れる。
既定はすべて有効: structured(search_detections)、guard_ts(ts と dt の整合の検査)、lint(回答の検査)。
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime

from botocore.exceptions import ClientError

from . import athena, aws, events, prompt, sql_guard
from .config import Config
from .http import ApiError, bad_request
from .timeutil import JST, UTC, iso_auto, now, parse_ts

MAX_QUESTION_CHARS = 500
MAX_QUERIES = 4          # 検索ツールの呼び出し回数の上限
MAX_TURNS = MAX_QUERIES + 3
MAX_HITS = 20
MAX_TOKENS = 1500
MAX_LINT_RETRIES = 1
ALL_FEATURES = ("structured", "guard_ts", "lint")

# 人物・車の同定はしていないので、「30人」「60台」のような数え方は誤り。
# 1 フレームに同時に映った数(max_objects_in_frame)以下なら事実なので許す(「同時に最大 3 人」)
_IDENTITY_COUNT = re.compile(r"(?<![0-9０-９])([0-9０-９]+)\s*(人|名|台|匹|羽|頭)(?!目)")
_TIMES = re.compile(r"(?<![0-9])([0-9]+)\s*回")


def _features() -> set[str]:
    raw = os.environ.get("SEARCH_FEATURES")
    if raw is None:
        return set(ALL_FEATURES)
    return {x.strip() for x in raw.split(",") if x.strip()}


def _budget_seconds() -> float:
    # API Gateway の統合タイムアウト(30 秒)より手前で切り上げる
    return float(os.environ.get("SEARCH_TIMEOUT_SECONDS", "25"))


def _athena_ts(value: str) -> datetime | None:
    return athena.parse_timestamp(value)


def camera_names(cfg: Config) -> dict[str, str]:
    """カメラ ID → 表示名。環境変数 CAMERA_NAMES、なければシャドウの name(reported か desired)。"""
    if cfg.camera_names:
        return dict(cfg.camera_names)
    if not cfg.iot_data_endpoint:
        return {}
    from . import cameras as cam_mod  # 循環 import を避ける

    names: dict[str, str] = {}
    shadows: dict[str, dict | None] = {}
    for cam, gw in cfg.cameras.items():
        if not gw:
            continue
        try:
            if gw not in shadows:
                shadows[gw] = cam_mod._get_shadow(cfg, gw)
            reported, desired, _ = cam_mod._camera_state(shadows[gw], cam)
        except Exception:  # noqa: BLE001 名前は補助情報なので、取れなくても検索は続ける
            continue
        name = (reported or {}).get("name") or (desired or {}).get("name")
        if name:
            names[cam] = str(name)
    return names


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


def _clean_hits(raw: object, seen_ts: dict[str, dict], cameras: set[str],
                event_index: dict[str, events.Event] | None = None) -> list[dict]:
    hits = []
    seen_keys = set()
    if not isinstance(raw, list):
        return hits
    event_index = event_index or {}
    for h in raw:
        if not isinstance(h, dict):
            continue
        ev = event_index.get(str(h.get("event_id") or "").strip().upper())
        if ev is not None:
            dt, key = ev.start, iso_auto(ev.start)
            cam, label, conf = ev.camera_id, ev.label, ev.confidence
        else:
            try:
                dt = parse_ts(str(h.get("ts", "")))
            except ApiError:
                continue
            key = iso_auto(dt)
            info = seen_ts.get(key)
            if info is None:
                continue  # 検索結果にない時刻(モデルが作った値)は捨てる
            cam = str(h.get("camera_id") or info.get("camera_id") or "")
            label = h.get("label") or info.get("label")
            conf = h.get("confidence", info.get("confidence"))
        if cameras and cam not in cameras:
            continue
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


def lint_answer(answer: str, *, max_events: int | None, max_objects: int = 1) -> list[str]:
    """回答の文の検査。問題があればモデルに返す指摘の文を返す。

    max_events: search_detections で見たイベント数の合計(分からなければ None)。これより大きい「n 回」は、
    検出の行数やフレーム数を回数として書いた疑いが強い。
    max_objects: 検索結果で 1 フレームに同時に映った数の最大。これより大きい「n 人」「n 台」は誤り。
    """
    issues = []
    nums = [m for m in _IDENTITY_COUNT.finditer(answer)
            if int(m.group(1).translate(str.maketrans("０１２３４５６７８９", "0123456789"))) > max_objects]
    if nums:
        issues.append(f"「{nums[0].group(0)}」のような人数・台数の表現は使えません(人物や車の同定はしておらず、"
                      "検出の数は人数・台数ではありません)。数は「n 回」(イベントの数)で書いてください。")
    if max_events is not None:
        big = sorted({int(x) for x in _TIMES.findall(answer) if int(x) > max_events})
        if big:
            issues.append(f"「{big[0]} 回」は検索結果のイベント数(合計 {max_events})より多いです。"
                          "フレーム数や検出の行数を回数として書かないでください。")
    return issues


def search(cfg: Config, body: object) -> dict:
    if not isinstance(body, dict) or not isinstance(body.get("question"), str) or not body["question"].strip():
        raise bad_request('ボディは {"question": "..."} にしてください')
    question = body["question"].strip()
    if len(question) > MAX_QUESTION_CHARS:
        raise bad_request(f"質問は {MAX_QUESTION_CHARS} 文字以内にしてください")

    feats = _features()
    structured = "structured" in feats
    started = time.monotonic()
    deadline = started + _budget_seconds()
    t_now = now()
    names = camera_names(cfg)
    system = prompt.system_prompt(
        now=t_now, database=cfg.glue_database, table=cfg.glue_table, cameras=sorted(cfg.cameras),
        max_rows=sql_guard.MAX_ROWS, max_dt_days=sql_guard.MAX_DT_DAYS, max_queries=MAX_QUERIES,
        camera_names=names, structured=structured)
    tool_cfg = prompt.tool_config(sql_guard.MAX_ROWS, structured=structured)
    messages: list[dict] = [{"role": "user", "content": [{"text": question}]}]
    bedrock = aws.client("bedrock-runtime")

    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
             "cache_read_input_tokens": 0, "cache_write_input_tokens": 0}
    queries: list[dict] = []
    seen_ts: dict[str, dict] = {}
    event_index: dict[str, events.Event] = {}
    events_seen: int | None = None
    last_sql: str | None = None
    answer: str | None = None
    raw_hits: object = []
    bedrock_calls = 0
    query_calls = 0
    lint_retries = 0

    for turn in range(MAX_TURNS):
        remaining = deadline - time.monotonic()
        force_answer = query_calls >= MAX_QUERIES or remaining < 8 or turn >= MAX_TURNS - 2
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
                cand = str(inp.get("answer") or "").strip() or None
                max_objs = max((ev.max_objects for ev in event_index.values()), default=1)
                issues = (lint_answer(cand or "", max_events=events_seen, max_objects=max_objs)
                          if "lint" in feats else [])
                if issues and lint_retries < MAX_LINT_RETRIES and time.monotonic() < deadline - 6:
                    lint_retries += 1
                    results.append({"toolResult": {"toolUseId": tid, "status": "error", "content": [{"text": (
                        "回答を直して、もう一度 submit_answer を呼んでください: " + " ".join(issues))}]}})
                    continue
                answer = cand
                raw_hits = inp.get("hits") or []
                done = True
                results.append({"toolResult": {"toolUseId": tid, "content": [{"text": "OK"}]}})
                continue
            if name not in ("query_detections", "search_detections") or (
                    name == "search_detections" and not structured):
                results.append({"toolResult": {"toolUseId": tid, "status": "error",
                                               "content": [{"text": f"未知のツールです: {name}"}]}})
                continue
            query_calls += 1
            record: dict = {"tool": name}
            queries.append(record)
            if query_calls > MAX_QUERIES:
                record["error"] = "呼び出し回数の上限を超えました"
                results.append({"toolResult": {"toolUseId": tid, "status": "error",
                                               "content": [{"text": "検索の回数上限に達しました。submit_answer で答えてください。"}]}})
                continue
            timeout_s = max(3.0, min(15.0, deadline - time.monotonic() - 5))
            if name == "search_detections":
                record["input"] = inp
                record["sql"] = None
                try:
                    out = events.run_search(cfg, inp, now=t_now, names=names, timeout_s=timeout_s,
                                            next_event_no=len(event_index) + 1)
                except events.ToolInputError as e:
                    record["error"] = f"rejected: {e}"
                    results.append({"toolResult": {"toolUseId": tid, "status": "error",
                                                   "content": [{"text": f"検索しませんでした: {e}"}]}})
                    continue
                except athena.AthenaError as e:
                    record["error"] = str(e)
                    record["bytes_scanned"] = getattr(e, "bytes_scanned", 0)
                    results.append({"toolResult": {"toolUseId": tid, "status": "error",
                                                   "content": [{"text": f"Athena のエラー: {e}"}]}})
                    continue
                record["sql"] = out.sql
                record["bytes_scanned"] = out.bytes_scanned
                record["row_count"] = len(out.events)
                record["execution_ms"] = out.execution_ms
                if out.sql:
                    last_sql = out.sql
                for ev in out.events:
                    event_index[ev.id] = ev
                    seen_ts.setdefault(iso_auto(ev.start), {"camera_id": ev.camera_id, "label": ev.label,
                                                            "confidence": ev.confidence})
                events_seen = (events_seen or 0) + len(out.events)
                results.append({"toolResult": {"toolUseId": tid, "content": [{"json": out.payload}]}})
                continue

            sql = str(inp.get("sql") or "")
            record["sql"] = sql
            try:
                g = sql_guard.guard(sql, table=cfg.glue_table, database=cfg.glue_database,
                                    check_ts="guard_ts" in feats)
            except sql_guard.SqlRejected as e:
                record["error"] = f"rejected: {e}"
                results.append({"toolResult": {"toolUseId": tid, "status": "error",
                                               "content": [{"text": f"SQL を実行しませんでした: {e}"}]}})
                continue
            record["sql"] = g.sql
            try:
                res = athena.run(cfg, g.sql, timeout_s=timeout_s)
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
            if g.warnings:
                payload["warnings"] = g.warnings
                record["warnings"] = g.warnings
            results.append({"toolResult": {"toolUseId": tid, "content": [{"json": payload}]}})
        if done:
            break
        messages.append({"role": "user", "content": results})

    if answer is None:
        answer = "回答を作れませんでした。質問を言い換えてもう一度お試しください。"
    hits = _clean_hits(raw_hits, seen_ts, set(cfg.cameras), event_index)
    usage.update({
        "model_id": cfg.bedrock_model_id,
        "bedrock_calls": bedrock_calls,
        "tool_calls": query_calls,
        "lint_retries": lint_retries,
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
