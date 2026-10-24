"""自然言語検索の end-to-end 検証(実 AWS)。

一時リソース(S3 バケット、Glue データベース/テーブル、Athena ワークグループ)を `cc-tmp-api-` で作り、
ダミーの検出結果 Parquet を置いて、ハンドラーをそのまま呼んで日本語の質問に答えさせる。

    cd lambdas/api
    mise exec -- uv run python scripts/e2e_search.py setup
    mise exec -- uv run python scripts/e2e_search.py run [--model jp.anthropic.claude-haiku-4-5-20251001-v1:0]
    mise exec -- uv run python scripts/e2e_search.py teardown   # 必ず実行する
    mise exec -- uv run python scripts/e2e_search.py check      # 残っていないことの確認

Glue/Athena のデータベース名はハイフンを使えないので `cc_tmp_api_e2e` にしている。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import random
import sys
from datetime import datetime, timedelta, timezone

import boto3
import pyarrow as pa
import pyarrow.parquet as pq
from botocore.exceptions import ClientError

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

REGION = "ap-northeast-1"
DB = "cc_tmp_api_e2e"
TABLE = "detections"
WG = "cc-tmp-api-wg"
JST = timezone(timedelta(hours=9))
CAMERAS = {"cam-001": "gw-001", "cam-002": "gw-001"}

QUESTIONS = [
    "今日の午前中に人が映ったのはいつ?",
    "昨日の夕方に車は来た?",
    "昨日、犬が映ったカメラと時間を教えて",
    "今日いちばん人が多かった時間帯は?",
]


def account() -> str:
    return boto3.client("sts").get_caller_identity()["Account"]


def bucket_name() -> str:
    return f"cc-tmp-api-{account()}"


def dummy_rows(now: datetime) -> list[dict]:
    """今日と昨日(日本時間)のダミー検出。答えが分かるように、時刻を決め打ちで入れる。"""
    rnd = random.Random(42)
    today = now.astimezone(JST).replace(hour=0, minute=0, second=0, microsecond=0)
    yday = today - timedelta(days=1)
    rows = []

    def burst(cam, start_jst: datetime, seconds: int, label: str, conf=0.85, objs=1):
        for s in range(seconds):
            t = start_jst + timedelta(seconds=s)
            for _ in range(objs):
                rows.append({
                    "camera_id": cam, "ts": t.astimezone(timezone.utc).replace(tzinfo=None),
                    "label": label, "confidence": round(min(0.99, conf + rnd.uniform(-0.1, 0.1)), 3),
                    "x": rnd.random() * 0.8, "y": rnd.random() * 0.8, "w": 0.1, "h": 0.3, "model": "yolox_nano",
                })

    # 今日の午前中: 人 3 回(8:05、9:12、11:40)、cam-002 で 10:30
    burst("cam-001", today.replace(hour=8, minute=5, second=10), 25, "person")
    burst("cam-001", today.replace(hour=9, minute=12, second=34), 40, "person", objs=2)
    burst("cam-001", today.replace(hour=11, minute=40), 15, "person")
    burst("cam-002", today.replace(hour=10, minute=30, second=5), 30, "person", objs=3)
    # 今日の未明: 低信頼度のノイズ
    burst("cam-001", today.replace(hour=3, minute=0), 5, "person", conf=0.3)
    # 昨日の夕方: 車 2 回(17:20 cam-001、18:45 cam-002)、トラック 16:05
    burst("cam-001", yday.replace(hour=17, minute=20, second=3), 20, "car")
    burst("cam-002", yday.replace(hour=18, minute=45, second=30), 12, "car")
    burst("cam-001", yday.replace(hour=16, minute=5), 8, "truck")
    # 昨日: 犬(7:30 cam-002)、人(12:00〜)
    burst("cam-002", yday.replace(hour=7, minute=30, second=20), 18, "dog")
    burst("cam-001", yday.replace(hour=12, minute=0), 60, "person")
    # 現在時刻より後のデータは入れない
    nowu = now.astimezone(timezone.utc).replace(tzinfo=None)
    return [r for r in rows if r["ts"] <= nowu]


def setup() -> None:
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

    rows = dummy_rows(datetime.now(timezone.utc))
    schema = pa.schema([("camera_id", pa.string()), ("ts", pa.timestamp("ms")), ("label", pa.string()),
                        ("confidence", pa.float32()), ("x", pa.float32()), ("y", pa.float32()),
                        ("w", pa.float32()), ("h", pa.float32()), ("model", pa.string())])
    by_key: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        by_key.setdefault((r["ts"].strftime("%Y-%m-%d"), r["camera_id"]), []).append(r)
    for (dt, cam), rs in sorted(by_key.items()):
        rs.sort(key=lambda r: r["ts"])
        tbl = pa.Table.from_pylist(rs, schema=schema)
        buf = io.BytesIO()
        pq.write_table(tbl, buf, compression="snappy")
        key = f"detections/dt={dt}/{cam}.parquet"
        s3.put_object(Bucket=bucket, Key=key, Body=buf.getvalue())
        print(f"put s3://{bucket}/{key} rows={len(rs)} bytes={buf.tell()}")

    try:
        glue.create_database(DatabaseInput={"Name": DB, "Description": "cc-tmp-api e2e (temporary)"})
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
        athena.create_work_group(Name=WG, Description="cc-tmp-api e2e (temporary)", Configuration={
            "ResultConfiguration": {"OutputLocation": f"s3://{bucket}/athena/"},
            "EnforceWorkGroupConfiguration": True,
            "PublishCloudWatchMetricsEnabled": False,
            "BytesScannedCutoffPerQuery": 10 * 1024 * 1024,  # 最小値 10 MB。暴走したクエリはここで止まる
            "EngineVersion": {"SelectedEngineVersion": "AUTO"},
        })
    except ClientError as e:
        if "already" not in e.response["Error"]["Message"].lower():
            raise
    print(f"setup done: bucket={bucket} db={DB} table={TABLE} workgroup={WG} rows={len(rows)}")


def run(model: str | None, questions: list[str]) -> None:
    os.environ.update({
        "PROJECT": "cc-tmp-api", "GLUE_DATABASE": DB, "GLUE_TABLE": TABLE, "ATHENA_WORKGROUP": WG,
        "CAMERAS": json.dumps(CAMERAS), "STATE_TABLE": "cc-tmp-api-unused",
    })
    if model:
        os.environ["BEDROCK_MODEL_ID"] = model
    import handler

    totals = {"input_tokens": 0, "output_tokens": 0, "athena_bytes_scanned": 0}
    for q in questions:
        ev = {"rawPath": "/api/search", "requestContext": {"http": {"method": "POST"}},
              "body": json.dumps({"question": q})}
        r = handler.lambda_handler(ev, None)
        body = json.loads(r["body"])
        print("=" * 80)
        print(f"Q: {q}  (status {r['statusCode']})")
        if r["statusCode"] != 200:
            print(json.dumps(body, ensure_ascii=False, indent=2))
            continue
        for i, qq in enumerate(body["queries"], 1):
            print(f"--- SQL {i} (scanned={qq.get('bytes_scanned')} rows={qq.get('row_count')} err={qq.get('error')})")
            print(qq["sql"])
        print(f"A: {body['answer']}")
        print("hits:", json.dumps(body["hits"], ensure_ascii=False))
        print("usage:", json.dumps(body["usage"], ensure_ascii=False))
        for k in totals:
            totals[k] += body["usage"][k]
    print("=" * 80)
    print("totals:", json.dumps(totals))


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
    """一時リソースが残っていないことを確認する。"""
    left = []
    s3 = boto3.client("s3", region_name=REGION)
    left += [b["Name"] for b in s3.list_buckets()["Buckets"] if b["Name"].startswith("cc-tmp-api")]
    glue = boto3.client("glue", region_name=REGION)
    left += [d["Name"] for d in glue.get_databases()["DatabaseList"] if d["Name"].startswith("cc_tmp_api")
             or d["Name"].startswith("cc-tmp-api")]
    athena = boto3.client("athena", region_name=REGION)
    left += [w["Name"] for w in athena.list_work_groups()["WorkGroups"] if w["Name"].startswith("cc-tmp-api")]
    print("remaining:", left or "none")
    if left:
        sys.exit(1)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["setup", "run", "teardown", "check"])
    p.add_argument("--model")
    p.add_argument("--question", action="append")
    a = p.parse_args()
    if a.cmd == "setup":
        setup()
    elif a.cmd == "run":
        run(a.model, a.question or QUESTIONS)
    elif a.cmd == "teardown":
        teardown()
    else:
        check()


if __name__ == "__main__":
    main()
