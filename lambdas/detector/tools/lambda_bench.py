"""Lambda 上の推論ベンチマーク(一時リソースは cc-tmp-detect- で始まる名前で作り、最後に消す)。

  uv run python tools/lambda_bench.py setup     # IAM ロール、S3 バケット、関数(arm64 / x86_64)を作る
  uv run python tools/lambda_bench.py run --out results.jsonl [--memories 1024,1769,...]
  uv run python tools/lambda_bench.py cleanup   # 全部消して、消えたことを確認する

zip は tools/build_bench.sh で build/cc-tmp-detect-bench-{arm64,x86_64}.zip に作っておく。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time

import boto3
from botocore.config import Config

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
PREFIX = "cc-tmp-detect-"
ROLE = PREFIX + "bench-role"
FUNCS = {"arm64": PREFIX + "bench", "x86_64": PREFIX + "bench-x86"}

session = boto3.Session()
cfg = Config(read_timeout=900, connect_timeout=10, retries={"max_attempts": 0})
lam = session.client("lambda", config=cfg)
iam = session.client("iam")
s3 = session.client("s3")
sts = session.client("sts")


def bucket_name() -> str:
    return f"{PREFIX}{sts.get_caller_identity()['Account']}-{session.region_name}"


def setup(args) -> None:
    trust = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"},
                       "Action": "sts:AssumeRole"}],
    }
    try:
        role_arn = iam.create_role(RoleName=ROLE, AssumeRolePolicyDocument=json.dumps(trust))["Role"]["Arn"]
        iam.attach_role_policy(RoleName=ROLE,
                               PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole")
        time.sleep(10)  # ロールの反映待ち
    except iam.exceptions.EntityAlreadyExistsException:
        role_arn = iam.get_role(RoleName=ROLE)["Role"]["Arn"]
    b = bucket_name()
    try:
        s3.create_bucket(Bucket=b, CreateBucketConfiguration={"LocationConstraint": session.region_name})
    except s3.exceptions.BucketAlreadyOwnedByYou:
        pass
    for arch, fn in FUNCS.items():
        zip_path = os.path.join(ROOT, "build", f"cc-tmp-detect-bench-{arch}.zip")
        key = os.path.basename(zip_path)
        print("upload", zip_path)
        s3.upload_file(zip_path, b, key)
        code = {"S3Bucket": b, "S3Key": key}
        try:
            lam.create_function(
                FunctionName=fn, Runtime="python3.13", Role=role_arn, Handler="bench.lambda_handler",
                Code=code, Timeout=900, MemorySize=1769, Architectures=[arch],
            )
        except lam.exceptions.ResourceConflictException:
            lam.update_function_code(FunctionName=fn, **code)
        lam.get_waiter("function_active_v2").wait(FunctionName=fn)
        lam.get_waiter("function_updated_v2").wait(FunctionName=fn)
        print("ready", fn)


def set_memory(fn: str, mem: int) -> None:
    lam.update_function_configuration(FunctionName=fn, MemorySize=mem)
    lam.get_waiter("function_updated_v2").wait(FunctionName=fn)


def invoke(fn: str, event: dict) -> tuple[dict, dict]:
    r = lam.invoke(FunctionName=fn, Payload=json.dumps(event).encode(), LogType="Tail")
    payload = json.loads(r["Payload"].read())
    tail = base64.b64decode(r.get("LogResult", "")).decode(errors="replace")
    rep = {}
    m = re.search(r"REPORT .*", tail)
    if m:
        for k, v in re.findall(r"(Duration|Billed Duration|Max Memory Used|Init Duration): ([\d.]+)", m.group(0)):
            rep[k] = float(v)
    if "FunctionError" in r:
        raise RuntimeError(f"{fn}: {payload} {tail[-1500:]}")
    return payload, rep


MAIN_CONFIGS = [
    {"model": m, "width": 416}
    for m in ("yolox_nano.dyn", "yolox_nano.int8", "yolox_tiny.dyn", "yolox_tiny.int8", "yolox_tiny.int8s8",
              "yolox_s.int8")
] + [{"model": "yolox_tiny.int8", "width": 640}, {"model": "yolox_tiny.dyn", "width": 640}]
EXTRA_CONFIGS = (
    [{"model": "yolox_tiny.int8", "width": 416, "threads": t} for t in (1, 2, 4)]
    + [{"model": "yolox_tiny.dyn", "width": 416, "threads": t} for t in (1, 2)]
    + [{"model": "yolox_tiny.int8", "width": 416, "batch": b} for b in (4, 12)]
    + [{"model": "yolox_tiny.dyn", "width": 416, "batch": 4}]
    + [{"model": "yolox_tiny.int8", "width": w} for w in (320, 512)]
    + [{"model": "yolox_tiny.dyn", "width": w} for w in (320, 512)]
    + [{"model": "yolox_tiny.int8", "width": 416, "square": True},
       {"model": "yolox_tiny.dyn", "width": 416, "square": True}]
    + [{"model": "yolox_tiny.dyn", "width": 416, "graph_opt": g} for g in ("disable", "basic", "extended")]
    + [{"model": "yolox_tiny.int8", "width": 416, "graph_opt": g} for g in ("disable", "basic")]
)


# 低メモリ(1 vCPU 未満)の確認用。候補だけを、既定スレッド数と 1 スレッドで
LOW_CONFIGS = [
    {"model": m, "width": w, **({"threads": t} if t else {})}
    for m, w in (("yolox_tiny.int8s8", 416), ("yolox_tiny.int8", 416), ("yolox_nano.dyn", 416),
                 ("yolox_tiny.int8s8", 640))
    for t in (0, 1)
]


def run(args) -> None:
    memories = [int(m) for m in args.memories.split(",")]
    archs = args.archs.split(",")
    extra_mems = {int(m) for m in args.extra_memories.split(",")} if args.extra_memories else set()
    with open(args.out, "a") as out:
        for arch in archs:
            fn = FUNCS[arch]
            for mem in memories:
                set_memory(fn, mem)
                if args.set == "low":
                    configs = LOW_CONFIGS
                else:
                    configs = MAIN_CONFIGS + (EXTRA_CONFIGS if mem in extra_mems else [])
                # 1 回の呼び出しが 15 分を超えないよう、数件ずつに分ける
                for i in range(0, len(configs), args.chunk):
                    chunk = configs[i:i + args.chunk]
                    try:
                        payload, rep = invoke(fn, {"configs": chunk})
                    except Exception as e:  # noqa: BLE001
                        print("ERROR", arch, mem, e, file=sys.stderr)
                        continue
                    for r in payload["results"]:
                        rec = {"arch": arch, "memory": mem, "cpu_count": payload["cpu_count"],
                               "cold": payload["cold"], "report": rep, **r}
                        out.write(json.dumps(rec) + "\n")
                        out.flush()
                        print(arch, mem, r.get("model"), r.get("width"), r.get("threads", 0),
                              r.get("batch", 1), r.get("graph_opt", "all"), r.get("square", False),
                              r.get("total_ms"), r.get("error", ""))


def cleanup(args) -> None:
    for fn in FUNCS.values():
        try:
            lam.delete_function(FunctionName=fn)
            print("deleted", fn)
        except lam.exceptions.ResourceNotFoundException:
            pass
    logs = session.client("logs")
    for fn in FUNCS.values():
        try:
            logs.delete_log_group(logGroupName=f"/aws/lambda/{fn}")
            print("deleted log group", fn)
        except logs.exceptions.ResourceNotFoundException:
            pass
    b = bucket_name()
    try:
        for page in s3.get_paginator("list_object_versions").paginate(Bucket=b):
            objs = [{"Key": o["Key"], "VersionId": o["VersionId"]}
                    for o in page.get("Versions", []) + page.get("DeleteMarkers", [])]
            if objs:
                s3.delete_objects(Bucket=b, Delete={"Objects": objs})
        s3.delete_bucket(Bucket=b)
        print("deleted bucket", b)
    except s3.exceptions.NoSuchBucket:
        pass
    try:
        for p in iam.list_attached_role_policies(RoleName=ROLE)["AttachedPolicies"]:
            iam.detach_role_policy(RoleName=ROLE, PolicyArn=p["PolicyArn"])
        iam.delete_role(RoleName=ROLE)
        print("deleted role", ROLE)
    except iam.exceptions.NoSuchEntityException:
        pass
    verify(args)


def verify(args) -> None:
    """cc-tmp-detect- で始まるリソースが残っていないことを確かめる。"""
    left = []
    for page in lam.get_paginator("list_functions").paginate():
        left += [f["FunctionName"] for f in page["Functions"] if f["FunctionName"].startswith(PREFIX)]
    left += [b["Name"] for b in s3.list_buckets()["Buckets"] if b["Name"].startswith(PREFIX)]
    for page in iam.get_paginator("list_roles").paginate():
        left += [r["RoleName"] for r in page["Roles"] if r["RoleName"].startswith(PREFIX)]
    logs = session.client("logs")
    left += [g["logGroupName"] for g in
             logs.describe_log_groups(logGroupNamePrefix="/aws/lambda/" + PREFIX)["logGroups"]]
    kv = session.client("kinesisvideo")
    left += [s["StreamName"] for s in kv.list_streams(
        StreamNameCondition={"ComparisonOperator": "BEGINS_WITH", "ComparisonValue": PREFIX})["StreamInfoList"]]
    ddb = session.client("dynamodb")
    left += [t for t in ddb.list_tables()["TableNames"] if t.startswith(PREFIX)]
    print("残っている一時リソース:", left or "なし")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup")
    r = sub.add_parser("run")
    r.add_argument("--out", required=True)
    r.add_argument("--memories", default="1024,1769,2048,3008,4096")
    r.add_argument("--extra-memories", default="1769,3008")
    r.add_argument("--archs", default="arm64,x86_64")
    r.add_argument("--chunk", type=int, default=6)
    r.add_argument("--set", choices=["main", "low"], default="main")
    sub.add_parser("cleanup")
    sub.add_parser("verify")
    a = ap.parse_args()
    {"setup": setup, "run": run, "cleanup": cleanup, "verify": verify}[a.cmd](a)


if __name__ == "__main__":
    main()
