"""tools/lambda_bench.py run の結果(JSONL)を Markdown の表にする。

費用は東京リージョンの単価(Pricing API で確認、2026-10-24):
  x86_64 $0.0000166667 / GB 秒、arm64 $0.0000133334 / GB 秒(いずれも最初の段階)、リクエスト $0.20 / 100 万件
1 分ぶんの所要時間 = 60 枚の推論(デコード〜NMS)+ OVERHEAD_MS(GetImages と Parquet の読み書き。実 KVS の通しで約 2.1 秒)
1 か月 = 30 日 = 43,200 区間。無料枠は考えない。
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict

PRICE = {"arm64": 0.0000133334, "x86_64": 0.0000166667}
REQUEST = 0.20 / 1_000_000
OVERHEAD_MS = 2100
MINUTES_PER_MONTH = 43_200


def cost_per_minute(arch: str, memory: int, total_ms: float) -> float:
    billed_s = (total_ms + OVERHEAD_MS) / 1000
    return memory / 1024 * billed_s * PRICE[arch] + REQUEST


def key(r) -> str:
    k = f"{r['model']} @{r['width']}"
    if r.get("square"):
        k += " 正方形"
    if r.get("threads"):
        k += f" threads={r['threads']}"
    if r.get("batch", 1) != 1:
        k += f" batch={r['batch']}"
    if r.get("graph_opt", "all") != "all":
        k += f" opt={r['graph_opt']}"
    return k


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("results", nargs="+", help="JSONL(複数可。同じ設定は後のファイルが優先)")
    a = ap.parse_args()
    rows = [json.loads(line) for f in a.results for line in open(f)]
    rows = [r for r in rows if "total_ms" in r]
    by = defaultdict(dict)
    mems = sorted({r["memory"] for r in rows})
    for r in rows:
        by[(r["arch"], key(r))][r["memory"]] = r

    print("### 60 枚の処理時間(秒、デコード + 前処理 + 推論 + NMS)\n")
    print("| arch | 設定 | " + " | ".join(f"{m} MB" for m in mems) + " | 検出数 | person |")
    print("| --- | --- | " + " | ".join("---:" for _ in mems) + " | ---: | ---: |")
    for (arch, k), d in sorted(by.items()):
        any_r = next(iter(d.values()))
        cells = [f"{d[m]['total_ms'] / 1000:.1f}" if m in d else "" for m in mems]
        print(f"| {arch} | {k} | " + " | ".join(cells) + f" | {any_r['detections']} | {any_r['persons']} |")

    print("\n### 1 分あたり・1 か月あたりの Lambda 費用(USD、1 台)\n")
    print("| arch | 設定 | メモリ | 1 分の所要(秒) | 1 分あたり | 1 か月あたり |")
    print("| --- | --- | ---: | ---: | ---: | ---: |")
    best = []
    for (arch, k), d in sorted(by.items()):
        for m, r in sorted(d.items()):
            t = r["total_ms"] + OVERHEAD_MS
            c = cost_per_minute(arch, m, r["total_ms"])
            best.append((c, arch, k, m, t))
    for c, arch, k, m, t in sorted(best):
        if t > 60_000:
            continue
        print(f"| {arch} | {k} | {m} | {t / 1000:.1f} | ${c:.6f} | ${c * MINUTES_PER_MONTH:.2f} |")


if __name__ == "__main__":
    main()
