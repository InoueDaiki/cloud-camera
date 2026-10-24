"""推論ベンチマーク。Lambda(一時関数 cc-tmp-detect-bench)でも手元でも動く。

イベント(手元では --event に JSON):
  {"frames_dir": "bench_frames", "configs": [
     {"model": "yolox_nano.int8", "width": 416, "threads": 0, "batch": 1, "graph_opt": "all"}]}

各設定で、セッション作成 → 1 枚でウォームアップ → 60 枚を detect(デコード + 前処理 + 推論 + 後処理)し、
所要時間の内訳と検出数を返す。
"""

from __future__ import annotations

import glob
import json
import os
import platform
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
for p in (HERE, os.path.join(HERE, "..")):
    if os.path.exists(os.path.join(p, "detect.py")) and p not in sys.path:
        sys.path.insert(0, p)

from detect import Detector  # noqa: E402

_COLD = True


def _models_dir() -> str:
    for p in (os.path.join(HERE, "models"), os.path.join(HERE, "..", "models")):
        if os.path.isdir(p):
            return p
    raise FileNotFoundError("models")


def run_config(cfg: dict, frames_dir: str, n_frames: int) -> dict:
    width = cfg.get("width", 416)
    files = sorted(glob.glob(os.path.join(frames_dir, str(width), "*.jpg")))[:n_frames]
    images = [open(f, "rb").read() for f in files]
    det = Detector(
        os.path.join(_models_dir(), cfg["model"] + ".onnx"),
        input_width=width,
        threads=cfg.get("threads", 0),
        batch_size=cfg.get("batch", 1),
        graph_opt=cfg.get("graph_opt", "all"),
    )
    det.detect(images[:1])  # ウォームアップ
    timings: dict = {}
    t = time.perf_counter()
    results = det.detect(images, timings)
    total = (time.perf_counter() - t) * 1000
    return {
        **cfg,
        "frames": len(images),
        "load_ms": round(det.load_ms, 1),
        "total_ms": round(total, 1),
        "per_frame_ms": round(total / len(images), 2),
        **{k: round(v, 1) for k, v in timings.items()},
        "detections": sum(len(r) for r in results),
        "persons": sum(1 for r in results for d in r if d.label == "person"),
    }


def lambda_handler(event, context=None):
    global _COLD
    cold, _COLD = _COLD, False
    frames_dir = event.get("frames_dir", os.path.join(HERE, "bench_frames"))
    n = event.get("n_frames", 60)
    out = []
    for cfg in event["configs"]:
        try:
            out.append(run_config(cfg, frames_dir, n))
        except Exception as e:  # 1 設定の失敗で全体を止めない
            out.append({**cfg, "error": repr(e)})
    return {
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "memory_mb": os.environ.get("AWS_LAMBDA_FUNCTION_MEMORY_SIZE"),
        "cold": cold,
        "results": out,
    }


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--event", required=True)
    a = ap.parse_args()
    res = lambda_handler(json.loads(a.event))
    for r in res.pop("results"):
        print(json.dumps(r, ensure_ascii=False))
    print(json.dumps(res))
