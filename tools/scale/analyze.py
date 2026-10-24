"""cc-tmp-scale の計測を集計する。

python -I analyze.py <start ISO> <end ISO> [label]

- Lambda の REPORT 行から: 起動時刻(秒の位置)、実行時間の分布、Init、メモリ、1 秒ごとの同時実行数(START〜END の重なり)
- pipeline_done から: stopped の内訳、1 起動あたりの区間数
- CloudWatch: Lambda の Throttles / ConcurrentExecutions / AsyncEventAge / AsyncEventsDropped、
  Scheduler、KVS の API ごとの Requests / Success、DynamoDB の Throttle
"""
import collections
import json
import re
import sys
from datetime import datetime, timedelta, timezone

import boto3

P = "cc-tmp-scale"
start = datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
end = datetime.fromisoformat(sys.argv[2].replace("Z", "+00:00"))
label = sys.argv[3] if len(sys.argv) > 3 else ""
logs = boto3.client("logs")
cw = boto3.client("cloudwatch")
ms = lambda d: int(d.timestamp() * 1000)  # noqa: E731

REPORT = re.compile(r"REPORT RequestId: (\S+)\s+Duration: ([\d.]+) ms\s+Billed Duration: \d+ ms\s+Memory Size: \d+ MB\s+Max Memory Used: (\d+) MB(?:\s+Init Duration: ([\d.]+) ms)?")


def events(group, pattern):
    kw = dict(logGroupName=group, startTime=ms(start), endTime=ms(end), filterPattern=pattern)
    while True:
        r = logs.filter_log_events(**kw)
        yield from r["events"]
        if not r.get("nextToken"):
            break
        kw["nextToken"] = r["nextToken"]


def pct(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


result = {"label": label, "start": start.isoformat(), "end": end.isoformat(), "functions": {}}
intervals = []  # (start_ms, end_ms, fn)
for fn in ("video-archiver", "detector"):
    g = f"/aws/lambda/{P}-{fn}"
    reps = []
    for e in events(g, '"REPORT RequestId"'):
        m = REPORT.search(e["message"])
        if not m:
            continue
        dur = float(m.group(2))
        init = float(m.group(4)) if m.group(4) else None
        # REPORT の timestamp は終了時刻付近。開始 = 終了 − (実行 + Init)
        t_end = e["timestamp"]
        t_start = t_end - dur - (init or 0)
        reps.append({"dur": dur, "init": init, "mem": int(m.group(3)), "t_start": t_start})
        intervals.append((t_start, t_end, fn))
    stopped = collections.Counter()
    minutes = []
    for e in events(g, '"pipeline_done"'):
        try:
            j = json.loads(e["message"][e["message"].index("{"):])
        except Exception:  # noqa: BLE001
            continue
        stopped[j.get("stopped")] += 1
        minutes.append(j.get("minute_count", 0))
    durs = [r["dur"] for r in reps]
    secs = collections.Counter(int((r["t_start"] / 1000) % 60) // 5 * 5 for r in reps)
    result["functions"][fn] = {
        "invocations": len(reps),
        "cold_starts": sum(1 for r in reps if r["init"] is not None),
        "init_ms_p50": pct([r["init"] for r in reps if r["init"]], 50),
        "dur_ms": {"p50": pct(durs, 50), "p90": pct(durs, 90), "p99": pct(durs, 99), "max": max(durs) if durs else None,
                   "mean": sum(durs) / len(durs) if durs else None},
        "max_mem_mb": max((r["mem"] for r in reps), default=None),
        "start_second_hist_5s": dict(sorted(secs.items())),
        "stopped": dict(stopped),
        "minutes_per_invocation": dict(collections.Counter(minutes)),
    }

# 1 秒ごとの同時実行数(両関数の合計と関数別)
conc = collections.Counter()
conc_fn = {"video-archiver": collections.Counter(), "detector": collections.Counter()}
for s, e, fn in intervals:
    for t in range(int(s // 1000), int(e // 1000) + 1):
        conc[t] += 1
        conc_fn[fn][t] += 1
if conc:
    vals = [conc.get(t, 0) for t in range(int(ms(start) // 1000), int(ms(end) // 1000))]
    result["concurrency_per_second"] = {"max": max(vals), "p99": pct(vals, 99), "p90": pct(vals, 90), "mean": sum(vals) / len(vals),
                                        "max_video": max(conc_fn["video-archiver"].values(), default=0),
                                        "max_detect": max(conc_fn["detector"].values(), default=0)}
    # 分の中の位置(秒)ごとの平均同時実行数
    pos = collections.defaultdict(list)
    for t in range(int(ms(start) // 1000), int(ms(end) // 1000)):
        pos[t % 60].append(conc.get(t, 0))
    result["mean_concurrency_by_second_of_minute"] = {k: round(sum(v) / len(v), 2) for k, v in sorted(pos.items())}


def metric(ns, name, dims, stat="Sum", period=60):
    r = cw.get_metric_statistics(Namespace=ns, MetricName=name, Dimensions=[{"Name": k, "Value": v} for k, v in dims.items()],
                                 StartTime=start, EndTime=end, Period=period, Statistics=[stat])
    pts = sorted(r["Datapoints"], key=lambda x: x["Timestamp"])
    return [p[stat] for p in pts]


cwres = {}
for fn in ("video-archiver", "detector", "api"):
    d = {"FunctionName": f"{P}-{fn}"}
    cwres[fn] = {
        "Invocations": sum(metric("AWS/Lambda", "Invocations", d)),
        "Errors": sum(metric("AWS/Lambda", "Errors", d)),
        "Throttles": sum(metric("AWS/Lambda", "Throttles", d)),
        "ConcurrentExecutions_max": max(metric("AWS/Lambda", "ConcurrentExecutions", d, "Maximum"), default=None),
        "AsyncEventAge_max_ms": max(metric("AWS/Lambda", "AsyncEventAge", d, "Maximum"), default=None),
        "AsyncEventsDropped": sum(metric("AWS/Lambda", "AsyncEventsDropped", d)),
    }
cwres["account_ConcurrentExecutions_max"] = max(metric("AWS/Lambda", "ConcurrentExecutions", {}, "Maximum"), default=None)
cwres["account_Throttles"] = sum(metric("AWS/Lambda", "Throttles", {}))
# Scheduler(スケジュールグループ単位)
for name in ("InvocationAttemptCount", "TargetErrorCount", "TargetErrorThrottledCount", "InvocationThrottleCount",
             "InvocationDroppedCount", "InvocationsFailedToBeSentToDeadLetterCount"):
    cwres[f"scheduler_{name}"] = sum(metric("AWS/Scheduler", name, {"ScheduleGroup": P}))
# KVS(全ストリームの合計)
kvs = collections.defaultdict(float)
apis = ["ListFragments", "GetHLSStreamingSessionURL", "GetHLSMediaPlaylist", "GetHLSMasterPlaylist", "GetMP4InitFragment",
        "GetMP4MediaFragment", "GetImages", "PutMedia"]
for i in range(1, 11):
    s = f"{P}-cam-{i:03d}"
    for a in apis:
        for suffix in ("Requests", "Success"):
            kvs[f"{a}.{suffix}"] += sum(metric("AWS/KinesisVideo", f"{a}.{suffix}", {"StreamName": s}))
    kvs["PutMedia.IncomingBytes"] += sum(metric("AWS/KinesisVideo", "PutMedia.IncomingBytes", {"StreamName": s}))
    kvs["PutMedia.ConnectionErrors"] += sum(metric("AWS/KinesisVideo", "PutMedia.ConnectionErrors", {"StreamName": s}))
cwres["kvs_total"] = dict(kvs)
d = {"TableName": f"{P}-state"}
cwres["dynamodb"] = {
    "ConsumedWCU": sum(metric("AWS/DynamoDB", "ConsumedWriteCapacityUnits", d)),
    "ConsumedRCU": sum(metric("AWS/DynamoDB", "ConsumedReadCapacityUnits", d)),
    "WriteThrottleEvents": sum(metric("AWS/DynamoDB", "WriteThrottleEvents", d)),
    "ReadThrottleEvents": sum(metric("AWS/DynamoDB", "ReadThrottleEvents", d)),
}
result["cloudwatch"] = cwres
result["minutes"] = (end - start).total_seconds() / 60
print(json.dumps(result, indent=1, ensure_ascii=False, default=str))
