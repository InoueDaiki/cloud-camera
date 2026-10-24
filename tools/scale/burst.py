"""全カメラ × 2 関数を同じ瞬間に非同期起動する(「毎分の境界で一斉起動」を再現する)。

python -I burst.py disable   # スケジュールを止める(仕事を溜める)
python -I burst.py fire      # 20 本を並列で Invoke(InvocationType=Event)
"""
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import boto3

G = "cc-tmp-scale"
if sys.argv[1] == "disable":
    sch = boto3.client("scheduler")
    for s in sch.list_schedules(GroupName=G, MaxResults=100)["Schedules"]:
        d = sch.get_schedule(GroupName=G, Name=s["Name"])
        t = d["Target"]
        sch.update_schedule(GroupName=G, Name=s["Name"], ScheduleExpression=d["ScheduleExpression"],
                            FlexibleTimeWindow=d["FlexibleTimeWindow"],
                            Target={k: t[k] for k in ("Arn", "RoleArn", "Input", "RetryPolicy") if k in t}, State="DISABLED")
    print("disabled", datetime.now(timezone.utc).isoformat())
else:
    lam = boto3.client("lambda")
    jobs = [(f"{G}-{fn}", f"cam-{i:03d}") for fn in ("video-archiver", "detector") for i in range(1, 11)]

    def inv(j):
        r = lam.invoke(FunctionName=j[0], InvocationType="Event", Payload=json.dumps({"camera_id": j[1]}).encode())
        return r["StatusCode"]

    t0 = time.time()
    with ThreadPoolExecutor(20) as ex:
        codes = list(ex.map(inv, jobs))
    print("fired", len(codes), set(codes), "in", round(time.time() - t0, 2), "s at", datetime.now(timezone.utc).isoformat())
