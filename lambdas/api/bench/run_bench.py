"""自然言語検索の評価(実 Bedrock + Athena)。

    cd lambdas/api
    mise exec -- uv run python -m bench.run_bench setup
    mise exec -- uv run python -m bench.run_bench run --tag baseline [--split dev] [--model ...] [--repeat 2]
    mise exec -- uv run python -m bench.run_bench report baseline improved ...
    mise exec -- uv run python -m bench.run_bench teardown   # 必ず実行する
    mise exec -- uv run python -m bench.run_bench check      # remaining: none を確認

一時リソース: S3 バケット cc-tmp-search-<account>、Glue データベース cc_tmp_search_bench、
Athena ワークグループ cc-tmp-search-wg(スキャン上限 10 MB)。
結果は bench/results/<tag>.jsonl(1 行 = 1 問 1 回)。

評価では「現在」を dataset.ANCHOR に固定する(app.search.now を差し替える)。
SQL に current_timestamp / now() / current_date が書かれた場合は、実時刻ではなく ANCHOR に置き換えて実行する
(評価用の差し替え。本番では実時刻なので、モデルの理解としては正しい)。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import statistics
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import timezone

import boto3
from botocore.exceptions import ClientError

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from bench import dataset, score  # noqa: E402
from bench.questions import QUESTIONS  # noqa: E402

REGION = "ap-northeast-1"
DB = "cc_tmp_search_bench"
TABLE = "detections"
WG = "cc-tmp-search-wg"
RESULTS = os.path.join(HERE, "results")

# 100 万トークンあたりの単価(USD、入力 / 出力)。Pricing API の東京(ap-northeast-1)の値(2026-10-24 取得)。Anthropic は未取得の目安
PRICES = {
    "jp.amazon.nova-2-lite-v1:0": (0.396, 3.311),
    "apac.amazon.nova-pro-v1:0": (0.96, 3.84),
    "apac.amazon.nova-lite-v1:0": (0.072, 0.288),
    "apac.amazon.nova-micro-v1:0": (0.042, 0.168),
    "amazon.nova-lite-v1:0": (0.072, 0.288),
    "qwen.qwen3-32b-v1:0": (0.18, 0.73),
    "qwen.qwen3-235b-a22b-2507-v1:0": (0.27, 1.06),
    "openai.gpt-oss-120b-1:0": (0.18, 0.73),
    "openai.gpt-oss-20b-1:0": (0.08, 0.36),
    "deepseek.v3.2": (0.74, 2.22),
    "mistral.ministral-3-14b-instruct": (0.24, 0.24),
    "jp.anthropic.claude-haiku-4-5-20251001-v1:0": (1.10, 5.50),
    "jp.anthropic.claude-sonnet-4-6": (3.30, 16.50),
}


def account() -> str:
    return boto3.client("sts").get_caller_identity()["Account"]


def bucket_name() -> str:
    return f"cc-tmp-search-{account()}"


def setup() -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    s3 = boto3.client("s3", region_name=REGION)
    glue = boto3.client("glue", region_name=REGION)
    athena = boto3.client("athena", region_name=REGION)
    bucket = bucket_name()
    try:
        s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": REGION})
    except ClientError as e:
        if e.response["Error"]["Code"] != "BucketAlreadyOwnedByYou":
            raise
    s3.put_public_access_block(Bucket=bucket, PublicAccessBlockConfiguration={
        "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True})

    rows = dataset.rows()
    schema = pa.schema([("camera_id", pa.string()), ("ts", pa.timestamp("ms")), ("label", pa.string()),
                        ("confidence", pa.float32()), ("x", pa.float32()), ("y", pa.float32()),
                        ("w", pa.float32()), ("h", pa.float32()), ("model", pa.string())])
    by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        by_key[(r["ts"].strftime("%Y-%m-%d"), r["camera_id"])].append(r)
    for (dt, cam), rs in sorted(by_key.items()):
        buf = io.BytesIO()
        pq.write_table(pa.Table.from_pylist(rs, schema=schema), buf, compression="snappy")
        s3.put_object(Bucket=bucket, Key=f"detections/dt={dt}/{cam}.parquet", Body=buf.getvalue())
    print(f"put {len(by_key)} parquet files, {len(rows)} rows")

    try:
        glue.create_database(DatabaseInput={"Name": DB, "Description": "cc-tmp-search bench (temporary)"})
    except glue.exceptions.AlreadyExistsException:
        pass
    loc = f"s3://{bucket}/detections/"
    table_input = {
        "Name": TABLE,
        "TableType": "EXTERNAL_TABLE",
        "PartitionKeys": [{"Name": "dt", "Type": "string"}],
        "Parameters": {
            "classification": "parquet",
            "projection.enabled": "true",
            "projection.dt.type": "date",
            "projection.dt.format": "yyyy-MM-dd",
            "projection.dt.range": "2026-01-01,NOW",
            "projection.dt.interval": "1",
            "projection.dt.interval.unit": "DAYS",
            "storage.location.template": loc + "dt=${dt}/",
        },
        "StorageDescriptor": {
            "Columns": [
                {"Name": "camera_id", "Type": "string"}, {"Name": "ts", "Type": "timestamp"},
                {"Name": "label", "Type": "string"}, {"Name": "confidence", "Type": "float"},
                {"Name": "x", "Type": "float"}, {"Name": "y", "Type": "float"},
                {"Name": "w", "Type": "float"}, {"Name": "h", "Type": "float"},
                {"Name": "model", "Type": "string"},
            ],
            "Location": loc,
            "InputFormat": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat",
            "OutputFormat": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat",
            "SerdeInfo": {"SerializationLibrary": "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"},
        },
    }
    try:
        glue.create_table(DatabaseName=DB, TableInput=table_input)
    except glue.exceptions.AlreadyExistsException:
        glue.update_table(DatabaseName=DB, TableInput=table_input)
    try:
        athena.create_work_group(Name=WG, Description="cc-tmp-search bench (temporary)", Configuration={
            "ResultConfiguration": {"OutputLocation": f"s3://{bucket}/athena/"},
            "EnforceWorkGroupConfiguration": True,
            "PublishCloudWatchMetricsEnabled": False,
            "BytesScannedCutoffPerQuery": 10 * 1024 * 1024,
            "EngineVersion": {"SelectedEngineVersion": "AUTO"},
        })
    except ClientError as e:
        if "already" not in e.response["Error"]["Message"].lower():
            raise
    print(f"setup done: bucket={bucket} db={DB} workgroup={WG}")


_NOW_RE = re.compile(r"\bcurrent_timestamp\b|\bnow\s*\(\s*\)|\blocaltimestamp\b", re.I)
_TODAY_RE = re.compile(r"\bcurrent_date\b", re.I)


def _patch_clock() -> list:
    """「現在」を ANCHOR に固定する。SQL 中の現在時刻の関数も ANCHOR のリテラルに置き換える。"""
    from app import athena, search

    anchor_utc = dataset.ANCHOR.astimezone(timezone.utc)
    search.now = lambda: anchor_utc
    lit = f"TIMESTAMP '{dataset.ANCHOR:%Y-%m-%d %H:%M:%S} Asia/Tokyo'"
    today = f"DATE '{anchor_utc:%Y-%m-%d}'"
    orig = athena.run
    replaced: list = []

    def run(cfg, sql, **kw):
        new = _TODAY_RE.sub(today, _NOW_RE.sub(lit, sql))
        if new != sql:
            replaced.append(sql)
        return orig(cfg, new, **kw)

    athena.run = run
    return replaced


def _one(q, cfg) -> dict:
    from app import search
    from app.http import ApiError

    t0 = time.monotonic()
    try:
        res = search.search(cfg, {"question": q.q})
    except ApiError as e:
        res = {"error": f"{e.code}: {e.message}", "usage": {}}
    except Exception as e:  # noqa: BLE001 評価は止めずに記録する
        res = {"error": f"{type(e).__name__}: {e}", "usage": {}}
    res.setdefault("usage", {})["wall_ms"] = int((time.monotonic() - t0) * 1000)
    return res


def run(tag: str, model: str | None, split: str | None, ids: list[str] | None, repeat: int, workers: int) -> None:
    os.environ.update({
        "PROJECT": "cc-tmp-search", "GLUE_DATABASE": DB, "GLUE_TABLE": TABLE, "ATHENA_WORKGROUP": WG,
        "CAMERAS": json.dumps(dataset.CAMERAS), "CAMERA_NAMES": json.dumps(dataset.CAMERA_NAMES, ensure_ascii=False),
        "STATE_TABLE": "cc-tmp-search-unused", "IOT_DATA_ENDPOINT": "",
    })
    if model:
        os.environ["BEDROCK_MODEL_ID"] = model
    from app import config

    _patch_clock()
    cfg = config.load()
    qs = [q for q in QUESTIONS if (not split or q.split == split) and (not ids or q.id in ids)]
    os.makedirs(RESULTS, exist_ok=True)
    path = os.path.join(RESULTS, f"{tag}.jsonl")
    jobs = [(q, r) for r in range(repeat) for q in qs]
    with ThreadPoolExecutor(max_workers=workers) as pool, open(path, "w") as f:
        futs = [(q, r, pool.submit(_one, q, cfg)) for q, r in jobs]
        for q, r, fut in futs:
            res = fut.result()
            sc = score.score(q, res)
            rec = {"tag": tag, "model": cfg.bedrock_model_id, "id": q.id, "split": q.split,
                   "category": q.category, "rep": r, "question": q.q, "score": sc,
                   "answer": res.get("answer"), "hits": res.get("hits"), "queries": res.get("queries"),
                   "usage": res.get("usage"), "error": res.get("error")}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            mark = "OK " if sc["pass"] else "NG "
            print(f"{mark}{q.id} {q.q[:30]:<30} {'; '.join(sc['reasons'])[:150]}")
    print(f"wrote {path}")
    report([tag])


def _load(tag: str) -> list[dict]:
    with open(os.path.join(RESULTS, f"{tag}.jsonl")) as f:
        return [json.loads(line) for line in f if line.strip()]


def _cost(model: str, it: int, ot: int) -> float:
    pi, po = PRICES.get(model, (0.0, 0.0))
    return (it * pi + ot * po) / 1e6


def summarize(recs: list[dict]) -> dict:
    out = {}
    for split in ("dev", "test", "holdout", "all"):
        rs = [r for r in recs if split == "all" or r["split"] == split]
        if not rs:
            continue
        n = len(rs)
        ok = sum(r["score"]["pass"] for r in rs)
        u = [r.get("usage") or {} for r in rs]
        it = [x.get("input_tokens", 0) for x in u]
        ot = [x.get("output_tokens", 0) for x in u]
        out[split] = {
            "n": n, "pass": ok, "rate": ok / n,
            "in_tok": statistics.mean(it), "out_tok": statistics.mean(ot),
            "cost": statistics.mean(_cost(r.get("model", ""), a, b) for r, a, b in zip(rs, it, ot)),
            "scan_kb": statistics.mean(x.get("athena_bytes_scanned", 0) for x in u) / 1024,
            "queries": statistics.mean(x.get("tool_calls", 0) for x in u),
            "sec": statistics.mean(x.get("wall_ms", 0) for x in u) / 1000,
            "p90_sec": sorted(x.get("wall_ms", 0) for x in u)[int(0.9 * (n - 1))] / 1000,
        }
    return out


def report(tags: list[str]) -> None:
    print("| tag | model | dev | test | holdout | all | 入力 tok/問 | 出力 tok/問 | $/問 | スキャン KB/問 | SQL 回/問 | 秒/問 (p90) |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for tag in tags:
        recs = _load(tag)
        s = summarize(recs)
        a = s["all"]

        def cell(k):
            return f"{s[k]['pass']}/{s[k]['n']} ({s[k]['rate']:.0%})" if k in s else "-"
        print(f"| {tag} | {recs[0]['model']} | {cell('dev')} | {cell('test')} | {cell('holdout')} | {cell('all')} | {a['in_tok']:.0f} | "
              f"{a['out_tok']:.0f} | {a['cost']:.4f} | {a['scan_kb']:.0f} | {a['queries']:.1f} | "
              f"{a['sec']:.1f} ({a['p90_sec']:.1f}) |")
    # 種類ごと
    for tag in tags:
        recs = _load(tag)
        by = defaultdict(lambda: [0, 0])
        for r in recs:
            by[r["category"]][0] += r["score"]["pass"]
            by[r["category"]][1] += 1
        print(f"{tag}: " + ", ".join(f"{k} {a}/{b}" for k, (a, b) in by.items()))


def rescore(tag: str) -> None:
    """保存した回答を、今の採点で採点し直す(採点の修正を過去の結果にも当てる)。"""
    qs = {q.id: q for q in QUESTIONS}
    recs = _load(tag)
    for r in recs:
        r["score"] = score.score(qs[r["id"]], r)
    with open(os.path.join(RESULTS, f"{tag}.jsonl"), "w") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def failures(tag: str) -> None:
    for r in _load(tag):
        if r["score"]["pass"]:
            continue
        print("=" * 80)
        print(f"{r['id']} ({r['split']}, rep {r['rep']}) {r['question']}")
        print("  理由:", "; ".join(r["score"]["reasons"]))
        print("  回答:", (r.get("answer") or "").replace("\n", " ")[:300])
        for q in r.get("queries") or []:
            print("  SQL:", (q.get("sql") or "").replace("\n", " ")[:400], "| err:", (q.get("error") or "")[:120])
        print("  hits:", json.dumps([(h["camera_id"], h["ts_jst"], h.get("label")) for h in r.get("hits") or []],
                                    ensure_ascii=False)[:300])


def teardown() -> None:
    s3 = boto3.resource("s3", region_name=REGION)
    glue = boto3.client("glue", region_name=REGION)
    athena = boto3.client("athena", region_name=REGION)
    try:
        athena.delete_work_group(WorkGroup=WG, RecursiveDeleteOption=True)
        print("deleted workgroup", WG)
    except ClientError as e:
        print("workgroup:", e.response["Error"]["Message"])
    try:
        glue.delete_table(DatabaseName=DB, Name=TABLE)
    except ClientError as e:
        print("table:", e.response["Error"]["Message"])
    try:
        glue.delete_database(Name=DB)
        print("deleted glue database", DB)
    except ClientError as e:
        print("database:", e.response["Error"]["Message"])
    b = s3.Bucket(bucket_name())
    try:
        b.object_versions.delete()
        b.objects.all().delete()
        b.delete()
        print("deleted bucket", b.name)
    except ClientError as e:
        print("bucket:", e.response["Error"]["Message"])


def check() -> None:
    left = []
    s3 = boto3.client("s3", region_name=REGION)
    left += [b["Name"] for b in s3.list_buckets()["Buckets"] if b["Name"].startswith("cc-tmp-search")]
    glue = boto3.client("glue", region_name=REGION)
    left += [d["Name"] for d in glue.get_databases()["DatabaseList"] if d["Name"].startswith("cc_tmp_search")]
    athena = boto3.client("athena", region_name=REGION)
    left += [w["Name"] for w in athena.list_work_groups()["WorkGroups"] if w["Name"].startswith("cc-tmp-search")]
    print("remaining:", left or "none")
    if left:
        sys.exit(1)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["setup", "run", "report", "rescore", "failures", "teardown", "check"])
    p.add_argument("tags", nargs="*")
    p.add_argument("--tag", default="run")
    p.add_argument("--model")
    p.add_argument("--split", choices=["dev", "test", "holdout"])
    p.add_argument("--id", action="append")
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--app-dir", help="別の版の app/ を持つディレクトリ(ベースラインの再計測用)")
    a = p.parse_args()
    if a.app_dir:
        sys.path.insert(0, os.path.abspath(a.app_dir))
    if a.cmd == "setup":
        setup()
    elif a.cmd == "run":
        run(a.tag, a.model, a.split, a.id, a.repeat, a.workers)
    elif a.cmd == "report":
        report(a.tags)
    elif a.cmd == "failures":
        for t in a.tags:
            failures(t)
    elif a.cmd == "rescore":
        for t in a.tags:
            rescore(t)
        report(a.tags)
    elif a.cmd == "teardown":
        teardown()
    else:
        check()


if __name__ == "__main__":
    main()
