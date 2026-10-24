"""回線断試験の結果を ListFragments とエッジのログから集計する(outage-test.sh から呼ぶ)。

    python3 report.py --stream S --block T0 --unblock T1 --end T2 --log edge.log --out result.json

T0/T1/T2 は epoch 秒。ListFragments は PRODUCER_TIMESTAMP と SERVER_TIMESTAMP の両方で引き、断片番号で合わせる。
AWS は aws CLI を使う(boto3 を入れなくてよいように)。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys


def aws(*args: str) -> dict:
    out = subprocess.run(["aws", *args, "--output", "json"], check=True, capture_output=True, text=True).stdout
    return json.loads(out) if out.strip() else {}


def iso(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def ts(v: str | float) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    return dt.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()


def list_fragments(stream: str, kind: str, start: float, end: float) -> list[dict]:
    ep = aws("kinesisvideo", "get-data-endpoint", "--stream-name", stream, "--api-name", "LIST_FRAGMENTS")["DataEndpoint"]
    sel = json.dumps({"FragmentSelectorType": kind,
                      "TimestampRange": {"StartTimestamp": start, "EndTimestamp": end}})
    frags, token = [], None
    while True:
        args = ["kinesis-video-archived-media", "list-fragments", "--endpoint-url", ep, "--stream-name", stream,
                "--max-results", "1000", "--fragment-selector", sel]
        if token:
            args += ["--next-token", token]
        r = aws(*args)
        frags += r.get("Fragments", [])
        token = r.get("NextToken")
        if not token:
            return frags


def analyze(frags: list[dict], t_block: float, t_unblock: float) -> dict:
    rows = [{"n": f["FragmentNumber"], "p": ts(f["ProducerTimestamp"]), "s": ts(f["ServerTimestamp"]),
             "len": f["FragmentLengthInMilliseconds"] / 1000, "size": f["FragmentSizeInBytes"]} for f in frags]
    by_p = sorted(rows, key=lambda r: (r["p"], r["s"]))
    # (2) 同じプロデューサー時刻の断片の重複
    seen: dict[int, int] = {}
    for r in rows:
        k = round(r["p"] * 1000)
        seen[k] = seen.get(k, 0) + 1
    dups = {k: c for k, c in seen.items() if c > 1}
    # (1) 届いた順(サーバー時刻)に並べたとき、プロデューサー時刻が単調増加か
    by_s = sorted(rows, key=lambda r: (r["s"], r["n"]))
    inv, inv_max, hi = 0, 0.0, -1.0
    for r in by_s:
        if r["p"] < hi:
            inv += 1
            inv_max = max(inv_max, hi - r["p"])
        hi = max(hi, r["p"])
    # 欠け: プロデューサー時刻で並べて、前の断片の終わり(+ 1 フレームの余裕)から次の始まりまでが空いている所
    gaps = []
    win = [r for r in by_p if r["p"] >= t_block - 30]  # 試験前(前回の起動など)の欠けは数えない
    for a, b in zip(win, win[1:], strict=False):
        g = b["p"] - (a["p"] + a["len"])
        if g > 0.5:
            gaps.append({"from": iso(a["p"] + a["len"]), "to": iso(b["p"]), "s": round(g, 1)})
    # 遮断中のプロデューサー時刻の断片(遅れて届いたはずのもの)
    during = [r for r in rows if t_block <= r["p"] < t_unblock]
    backlog = [r for r in rows if r["p"] < t_unblock and r["s"] >= t_block]
    delays = [r["s"] - r["p"] for r in rows]
    covered = sum(r["len"] for r in during)
    res = {
        "fragments": len(rows),
        "during_outage": {
            "fragments": len(during),
            "covered_s": round(covered, 1),
            "outage_s": round(t_unblock - t_block, 1),
            "coverage": round(covered / (t_unblock - t_block), 3) if t_unblock > t_block else None,
            "first_producer": iso(min(r["p"] for r in during)) if during else None,
            "last_producer": iso(max(r["p"] for r in during)) if during else None,
        },
        "gaps": gaps,
        "gap_total_s": round(sum(g["s"] for g in gaps), 1),
        # 復旧から、遮断前・遮断中のプロデューサー時刻の断片がすべて永続化されるまで
        "drain_s": round(max(r["s"] for r in backlog) - t_unblock, 1) if backlog else None,
        # 遮断開始から、遮断中の断片が届き終わるまで(クラウド側の確定待ち 720 秒の妥当性)
        "block_to_drained_s": round(max(r["s"] for r in backlog) - t_block, 1) if backlog else None,
        "max_delay_s": round(max(delays), 1) if delays else None,  # サーバー時刻 − プロデューサー時刻の最大
        "order": {"inversions": inv, "max_inversion_s": round(inv_max, 3)},
        "duplicates": {"producer_ts": len(dups), "extra_fragments": sum(c - 1 for c in dups.values()),
                       "examples": [iso(k / 1000) for k in sorted(dups)[:5]]},
        "bytes_during_outage": sum(r["size"] for r in during),
    }
    return res


STATUS_RE = re.compile(r"^(\S+ \S+) INFO cc_edge\.agent: status: (\{.*\})$")


def parse_log(path: str, t_block: float) -> dict:
    starts, statuses, mqtt_down, mqtt_up, kvs_errors, overflow, handles = 0, [], 0, 0, 0, [], 0
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.rstrip("\n")
        if "起動: rtspsrc" in line:
            starts += 1
        if "IoT Core から切断" in line:
            mqtt_down += 1
        if "IoT Core に接続した" in line:
            mqtt_up += 1
        if "KVS への送信エラー" in line:
            kvs_errors += 1
        m = re.search(r"New upload handle (\d+)", line)
        if m:
            handles = max(handles, int(m.group(1)))
        if "バッファがあふれ" in line:
            overflow.append(line[:300])
        m = STATUS_RE.match(line)
        if m:
            t = dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=dt.UTC).timestamp()
            if t >= t_block:  # 遮断開始以降だけを見る
                statuses.append(json.loads(m.group(2)))
    cams = [s["cameras"].get("cam-001") or {} for s in statuses]
    bufs = [c.get("buffer") or {} for c in cams]
    return {
        "pipeline_starts_in_log": starts,
        "restarts_last": cams[-1].get("restarts") if cams else None,
        "restarts_first": cams[0].get("restarts") if cams else None,
        "streaming_always": all(c.get("streaming") for c in cams) if cams else None,
        "mqtt_disconnects": mqtt_down, "mqtt_connects": mqtt_up, "kvs_error_lines": kvs_errors,
        "max_unsent_s": max((b.get("unsent_s") or 0) for b in bufs) if bufs else None,
        "max_store_used_mb": round(max((b.get("store_used_bytes") or 0) for b in bufs) / 2**20, 1) if bufs else None,
        "max_persisted_lag_s": max((b.get("persisted_lag_s") or 0) for b in bufs) if bufs else None,
        "dropped_frames_last": bufs[-1].get("dropped_frames") if bufs else None,
        "overflow_logs": overflow[:3],
        "max_upload_handle": handles,  # PutMedia を張った回数(kvssink のログ。INFO のときだけ)
        "stale_events_last": bufs[-1].get("stale_events") if bufs else None,
        "sessions_last": bufs[-1].get("sessions") if bufs else None,
        "last_status": cams[-1] if cams else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stream", required=True)
    ap.add_argument("--block", type=float, required=True)
    ap.add_argument("--unblock", type=float, required=True)
    ap.add_argument("--end", type=float, required=True)
    ap.add_argument("--margin", type=float, default=120)
    ap.add_argument("--log")
    ap.add_argument("--out")
    a = ap.parse_args()
    lo, hi = a.block - a.margin, a.end
    byp = list_fragments(a.stream, "PRODUCER_TIMESTAMP", lo, hi)
    bys = list_fragments(a.stream, "SERVER_TIMESTAMP", lo, hi)
    allf = {f["FragmentNumber"]: f for f in byp + bys}
    # 試験窓(プロデューサー時刻で lo〜hi)に入るものだけを集計する
    frags = [f for f in allf.values() if lo <= ts(f["ProducerTimestamp"]) < hi]
    res = {"stream": a.stream, "block": iso(a.block), "unblock": iso(a.unblock), "end": iso(a.end),
           "list_fragments": {"by_producer": len(byp), "by_server": len(bys)},
           **analyze(frags, a.block, a.unblock)}
    if a.log:
        res["edge"] = parse_log(a.log, a.block)
    js = json.dumps(res, ensure_ascii=False, indent=2)
    if a.out:
        open(a.out, "w").write(js)
    print(js)
    return 0


if __name__ == "__main__":
    sys.exit(main())
