"""コンテナの CPU、メモリ、送信量を一定時間サンプリングして平均を出す(エッジの負荷と上り帯域の計測用)。

    python3 docker_stats.py --seconds 30 cc-webrtc-webrtc-1 cc-webrtc-edge-1

送信量はコンテナの NetIO の送信側の差分(SRTP、RTP、UDP、IP のヘッダーと RTSP の受信に伴う ACK を含む)。
"""

import argparse
import json
import re
import subprocess
import time

UNITS = {"B": 1, "kB": 1e3, "KB": 1e3, "MB": 1e6, "GB": 1e9, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}


def size(s: str) -> float:
    m = re.match(r"([\d.]+)\s*([A-Za-z]+)", s.strip())
    return float(m.group(1)) * UNITS[m.group(2)] if m else 0.0


def sample(names: list[str]) -> dict:
    out = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{json .}}", *names],
                         capture_output=True, text=True, check=True).stdout
    res = {}
    for line in out.splitlines():
        d = json.loads(line)
        rx, tx = d["NetIO"].split("/")
        res[d["Name"]] = {"cpu": float(d["CPUPerc"].rstrip("%")), "mem": size(d["MemUsage"].split("/")[0]),
                          "rx": size(rx), "tx": size(tx), "t": time.time()}
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="+")
    ap.add_argument("--seconds", type=float, default=30)
    a = ap.parse_args()
    first = sample(a.names)
    samples = [first]
    end = time.time() + a.seconds
    while time.time() < end:
        samples.append(sample(a.names))
    last = samples[-1]
    for n in a.names:
        cpus = [s[n]["cpu"] for s in samples[1:]]
        dt = last[n]["t"] - first[n]["t"]
        print(json.dumps({
            "container": n,
            "cpu_avg_pct": round(sum(cpus) / len(cpus), 1),
            "cpu_max_pct": round(max(cpus), 1),
            "mem_max_mib": round(max(s[n]["mem"] for s in samples) / 1024**2, 1),
            "tx_kbps": round((last[n]["tx"] - first[n]["tx"]) * 8 / 1000 / dt, 1),
            "rx_kbps": round((last[n]["rx"] - first[n]["rx"]) * 8 / 1000 / dt, 1),
            "seconds": round(dt, 1),
            "samples": len(cpus),
        }, ensure_ascii=False))


if __name__ == "__main__":
    main()
