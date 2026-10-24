"""カメラごとの起動時刻(分の中の秒)と間隔を見る。python -I phase.py <start> <end> [camera_id(時刻をすべて出す)]"""
import collections
import json
import sys
from datetime import datetime

import boto3

logs = boto3.client("logs")
s = int(datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00")).timestamp() * 1000)
e = int(datetime.fromisoformat(sys.argv[2].replace("Z", "+00:00")).timestamp() * 1000)
for fn in ("video-archiver", "detector"):
    per = collections.defaultdict(list)
    # 構造化ログ(print)には RequestId が付かないので、ログストリームごとに「直前の START」と pipeline_done / lease_busy を対応づける
    kw2 = dict(logGroupName=f"/aws/lambda/cc-tmp-scale-{fn}", startTime=s, endTime=e,
               filterPattern='?"START RequestId" ?pipeline_done ?lease_busy')
    evs = []
    while True:
        r = logs.filter_log_events(**kw2)
        evs += r["events"]
        if not r.get("nextToken"):
            break
        kw2["nextToken"] = r["nextToken"]
    last = {}
    for ev in sorted(evs, key=lambda x: (x["logStreamName"], x["timestamp"], x["eventId"])):
        m = ev["message"]
        if m.startswith("START RequestId"):
            last[ev["logStreamName"]] = ev["timestamp"]
            continue
        try:
            j = json.loads(m[m.index("{"):])
        except Exception:  # noqa: BLE001
            continue
        t0 = last.pop(ev["logStreamName"], None)
        if t0 is not None:
            per[j["camera_id"]].append(t0)
    print("==", fn)
    for cam in sorted(per):
        ts = sorted(set(per[cam]))
        secs = [round((t / 1000) % 60, 1) for t in ts]
        gaps = [round((b - a) / 1000, 1) for a, b in zip(ts, ts[1:])]
        print(cam, "sec:", secs[-12:], "gaps:", gaps[-12:])
        if len(sys.argv) > 3 and cam == sys.argv[3]:
            print([datetime.utcfromtimestamp(t / 1000).strftime("%H:%M:%S.%f")[:-4] for t in ts])
