"""cc-tmp-scale のカメラのスケジュール 20 本を切り替える(比較実験用。Terraform の定義には戻さない。destroy で消える)。

python -I switch_sched.py sync      # cron(* * * * ? *)、OFF: 毎分 0 秒にそろえる
python -I switch_sched.py flex      # rate(1 minute)、FLEXIBLE 1 分
python -I switch_sched.py rate      # rate(1 minute)、OFF(Terraform の定義と同じ)
"""
import sys

import boto3

sch = boto3.client("scheduler")
G = "cc-tmp-scale"
mode = sys.argv[1]
expr = {"sync": "cron(* * * * ? *)", "flex": "rate(1 minute)", "rate": "rate(1 minute)"}[mode]
ftw = {"Mode": "FLEXIBLE", "MaximumWindowInMinutes": 1} if mode == "flex" else {"Mode": "OFF"}
names = [s["Name"] for s in sch.list_schedules(GroupName=G, MaxResults=100)["Schedules"]]
for n in names:
    s = sch.get_schedule(GroupName=G, Name=n)
    t = s["Target"]
    target = {k: t[k] for k in ("Arn", "RoleArn", "Input", "RetryPolicy") if k in t}
    sch.update_schedule(GroupName=G, Name=n, ScheduleExpression=expr, FlexibleTimeWindow=ftw, Target=target, State="ENABLED")
print(mode, len(names), "schedules updated")
