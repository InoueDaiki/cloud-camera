"""30 秒ごとに、処理済み位置の遅れ(全カメラ)とエッジの CPU・メモリ・送信量を JSON 行で記録する。

python -I sampler.py <out.jsonl>
"""
import json
import subprocess
import sys
import time
from datetime import datetime, timezone

import boto3

out = open(sys.argv[1], "a", buffering=1)
tbl = boto3.resource("dynamodb").Table("cc-tmp-scale-state")
cams = [f"cam-{i:03d}" for i in range(1, 11)]


def docker_stats(name):
    r = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{json .}}", name], capture_output=True, text=True)
    try:
        return json.loads(r.stdout)
    except Exception:
        return None


def net_tx(name):
    # コンテナの eth0 の送信バイト数(累計)
    r = subprocess.run(["docker", "exec", name, "cat", "/sys/class/net/eth0/statistics/tx_bytes"], capture_output=True, text=True)
    try:
        return int(r.stdout.strip())
    except Exception:
        return None


while True:
    now = datetime.now(timezone.utc)
    rec = {"ts": now.isoformat(), "lag": {}}
    try:
        keys = [{"camera_id": c, "sk": f"CURSOR#{k}"} for c in cams for k in ("VIDEO", "DETECT")]
        resp = boto3.resource("dynamodb").batch_get_item(RequestItems={"cc-tmp-scale-state": {"Keys": keys}})
        for it in resp["Responses"]["cc-tmp-scale-state"]:
            pu = datetime.fromisoformat(it["processed_until"].replace("Z", "+00:00"))
            rec["lag"][f'{it["camera_id"]}/{it["sk"][7:]}'] = round((now - pu).total_seconds())
    except Exception as e:  # noqa: BLE001
        rec["lag_error"] = str(e)
    st = docker_stats("cc-tmp-scale-edge-1")
    if st:
        rec["edge"] = {"cpu": st.get("CPUPerc"), "mem": st.get("MemUsage"), "pids": st.get("PIDs")}
    rec["edge_tx_bytes"] = net_tx("cc-tmp-scale-edge-1")
    st = docker_stats("cc-tmp-scale-emulator-1")
    if st:
        rec["emulator_cpu"] = st.get("CPUPerc")
    out.write(json.dumps(rec) + "\n")
    time.sleep(30)
