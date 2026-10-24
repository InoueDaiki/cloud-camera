"""本番の zip(build/detector.zip)を一時関数にデプロイし、実際の KVS(GetImages)で通しを確かめる。

一時リソース(すべて cc-tmp-detect- で始まる):
  KVS ストリーム cc-tmp-detect-{camera}、DynamoDB cc-tmp-detect-state、S3 cc-tmp-detect-{acct}-{region}、
  IAM ロール cc-tmp-detect-handler-role、関数 cc-tmp-detect-handler

  uv run python tools/lambda_e2e.py setup --memory 2048
  uv run python tools/lambda_e2e.py push --camera h265 --mkv crosswalk60_h265.mkv --minutes 4   # 直近の区間に送る
  uv run python tools/lambda_e2e.py invoke --camera h265
  uv run python tools/lambda_e2e.py show --camera h265
  uv run python tools/lambda_e2e.py cleanup    # 全部消して確認(tools/lambda_bench.py verify と同じ確認)
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import boto3

sys.path.insert(0, os.path.dirname(__file__))
from kvs_put_media import put_media  # noqa: E402
from lambda_bench import PREFIX, ROOT, bucket_name, verify  # noqa: E402

session = boto3.Session()
lam = session.client("lambda")
iam = session.client("iam")
s3 = session.client("s3")
ddb = session.client("dynamodb")
kv = session.client("kinesisvideo")
logs = session.client("logs")

FN = PREFIX + "handler"
ROLE = PREFIX + "handler-role"
TABLE = PREFIX + "state"


def setup(a) -> None:
    acct = session.client("sts").get_caller_identity()["Account"]
    region = session.region_name
    b = bucket_name()
    try:
        s3.create_bucket(Bucket=b, CreateBucketConfiguration={"LocationConstraint": region})
    except s3.exceptions.BucketAlreadyOwnedByYou:
        pass
    try:
        ddb.create_table(
            TableName=TABLE, BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "camera_id", "AttributeType": "S"},
                                  {"AttributeName": "sk", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "camera_id", "KeyType": "HASH"},
                       {"AttributeName": "sk", "KeyType": "RANGE"}],
        )
        ddb.get_waiter("table_exists").wait(TableName=TABLE)
    except ddb.exceptions.ResourceInUseException:
        pass
    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {
        "Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
    policy = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["kinesisvideo:GetDataEndpoint", "kinesisvideo:ListFragments",
                                       "kinesisvideo:GetImages", "kinesisvideo:DescribeStream"],
         "Resource": f"arn:aws:kinesisvideo:{region}:{acct}:stream/{PREFIX}*/*"},
        {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"], "Resource": f"arn:aws:s3:::{b}/*"},
        {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": f"arn:aws:s3:::{b}"},
        {"Effect": "Allow", "Action": ["dynamodb:*"],
         "Resource": f"arn:aws:dynamodb:{region}:{acct}:table/{TABLE}"},
    ]}
    try:
        arn = iam.create_role(RoleName=ROLE, AssumeRolePolicyDocument=json.dumps(trust))["Role"]["Arn"]
        iam.attach_role_policy(RoleName=ROLE,
                               PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole")
        time.sleep(10)
    except iam.exceptions.EntityAlreadyExistsException:
        arn = iam.get_role(RoleName=ROLE)["Role"]["Arn"]
    iam.put_role_policy(RoleName=ROLE, PolicyName="e2e", PolicyDocument=json.dumps(policy))
    zip_path = os.path.join(ROOT, "build", "detector.zip")
    s3.upload_file(zip_path, b, "detector.zip")
    env = {"Variables": {
        "STATE_TABLE": TABLE, "DETECTIONS_BUCKET": b, "STREAM_NAME_PREFIX": PREFIX,
        "LAG_SECONDS": "45", "MAX_MINUTES": "10", "MAX_ATTEMPTS": "3", "MEDIA_RETENTION_DAYS": "1",
    }}
    code = {"S3Bucket": b, "S3Key": "detector.zip"}
    try:
        lam.create_function(FunctionName=FN, Runtime="python3.13", Role=arn, Handler="handler.lambda_handler",
                            Code=code, Timeout=300, MemorySize=a.memory, Architectures=["arm64"],
                            Environment=env)
    except lam.exceptions.ResourceConflictException:
        lam.update_function_code(FunctionName=FN, **code)
        lam.get_waiter("function_updated_v2").wait(FunctionName=FN)
        lam.update_function_configuration(FunctionName=FN, MemorySize=a.memory, Environment=env)
    lam.get_waiter("function_active_v2").wait(FunctionName=FN)
    lam.get_waiter("function_updated_v2").wait(FunctionName=FN)
    print("ready", FN)


def push(a) -> None:
    stream = PREFIX + a.camera
    try:
        kv.create_stream(StreamName=stream, DataRetentionInHours=1,
                         MediaType="video/h265" if "265" in a.mkv else "video/h264")
        time.sleep(5)
    except kv.exceptions.ResourceInUseException:
        pass
    body = open(a.mkv, "rb").read()
    now = datetime.now(timezone.utc)
    # 処理対象の終わり(現在 − lag を 1 分境界に切り捨て)から minutes 分さかのぼった区間に送る
    end = (now - timedelta(seconds=45)).replace(second=0, microsecond=0)
    for i in range(a.minutes, 0, -1):
        t = end - timedelta(minutes=i)
        acks = put_media(stream, body, t)
        kinds: dict = {}
        for x in acks:
            kinds[x.get("EventType")] = kinds.get(x.get("EventType"), 0) + 1
        print(stream, t.isoformat(), kinds)


def invoke(a) -> None:
    t = time.time()
    r = lam.invoke(FunctionName=FN, Payload=json.dumps({"camera_id": a.camera}).encode(), LogType="Tail")
    print("elapsed", round(time.time() - t, 1), "s", r.get("FunctionError", ""))
    print(r["Payload"].read().decode()[:2000])
    print(base64.b64decode(r["LogResult"]).decode(errors="replace")[-4000:])


def show(a) -> None:
    import pyarrow.parquet as pq

    items = ddb.query(TableName=TABLE, KeyConditionExpression="camera_id = :c",
                      ExpressionAttributeValues={":c": {"S": a.camera}})["Items"]
    for it in items:
        print({k: list(v.values())[0] for k, v in it.items()
               if k.startswith("detect_") or k in ("sk", "frame_count", "processed_until")})
    b = bucket_name()
    for o in s3.list_objects_v2(Bucket=b, Prefix="detections/").get("Contents", []):
        if not o["Key"].endswith(f"/{a.camera}.parquet"):
            continue
        t = pq.ParquetFile(io.BytesIO(s3.get_object(Bucket=b, Key=o["Key"])["Body"].read())).read()
        print(o["Key"], o["Size"], "bytes", t.num_rows, "rows")
        print(t.group_by("label").aggregate([("label", "count")]).sort_by([("label_count", "descending")])
              .slice(0, 8).to_pylist())
        print(t.slice(0, 3).to_pylist())


def cleanup(a) -> None:
    for fn in (FN,):
        try:
            lam.delete_function(FunctionName=fn)
            print("deleted", fn)
        except lam.exceptions.ResourceNotFoundException:
            pass
        try:
            logs.delete_log_group(logGroupName=f"/aws/lambda/{fn}")
        except logs.exceptions.ResourceNotFoundException:
            pass
    try:
        iam.delete_role_policy(RoleName=ROLE, PolicyName="e2e")
    except iam.exceptions.NoSuchEntityException:
        pass
    try:
        for p in iam.list_attached_role_policies(RoleName=ROLE)["AttachedPolicies"]:
            iam.detach_role_policy(RoleName=ROLE, PolicyArn=p["PolicyArn"])
        iam.delete_role(RoleName=ROLE)
        print("deleted role", ROLE)
    except iam.exceptions.NoSuchEntityException:
        pass
    try:
        ddb.delete_table(TableName=TABLE)
        ddb.get_waiter("table_not_exists").wait(TableName=TABLE)
        print("deleted table", TABLE)
    except ddb.exceptions.ResourceNotFoundException:
        pass
    for s in kv.list_streams(StreamNameCondition={"ComparisonOperator": "BEGINS_WITH",
                                                  "ComparisonValue": PREFIX})["StreamInfoList"]:
        kv.delete_stream(StreamARN=s["StreamARN"])
        print("deleted stream", s["StreamName"])
    # バケットとベンチマーク関数は lambda_bench.py cleanup で消す


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup")
    s.add_argument("--memory", type=int, default=2048)
    p = sub.add_parser("push")
    p.add_argument("--camera", required=True)
    p.add_argument("--mkv", required=True)
    p.add_argument("--minutes", type=int, default=3)
    for name in ("invoke", "show"):
        x = sub.add_parser(name)
        x.add_argument("--camera", required=True)
    sub.add_parser("cleanup")
    sub.add_parser("verify")
    a = ap.parse_args()
    {"setup": setup, "push": push, "invoke": invoke, "show": show, "cleanup": cleanup,
     "verify": verify}[a.cmd](a)


if __name__ == "__main__":
    main()
