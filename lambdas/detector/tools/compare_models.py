"""モデル・量子化・入力サイズによる検出結果の違いを比べる(手元で実行)。

基準(既定は yolox_s.dyn の幅 640)の検出を「仮の正解」とし、各設定の検出と
同じラベル・IoU ≥ 0.5 で突き合わせて、適合率・再現率・F1 を出す。COCO の mAP の代わりの目安。

  uv run python tools/compare_models.py bench_frames/640
"""

from __future__ import annotations

import argparse
import glob
import os
import sys


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from detect import Detector  # noqa: E402

MODELS = os.path.join(os.path.dirname(__file__), "..", "models")


def iou(a, b) -> float:
    ax2, ay2, bx2, by2 = a.x + a.w, a.y + a.h, b.x + b.w, b.y + b.h
    iw = max(0.0, min(ax2, bx2) - max(a.x, b.x))
    ih = max(0.0, min(ay2, by2) - max(a.y, b.y))
    inter = iw * ih
    return inter / (a.w * a.h + b.w * b.h - inter + 1e-9)


def match(ref, got, label=None):
    tp = 0
    ref = [d for d in ref if label is None or d.label == label]
    got = [d for d in got if label is None or d.label == label]
    used = set()
    for g in sorted(got, key=lambda d: -d.confidence):
        best, bi = 0.0, -1
        for i, r in enumerate(ref):
            if i in used or r.label != g.label:
                continue
            v = iou(r, g)
            if v > best:
                best, bi = v, i
        if best >= 0.5:
            used.add(bi)
            tp += 1
    return tp, len(got), len(ref)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("frames_dir")
    ap.add_argument("--ref", default="yolox_s.dyn:640")
    ap.add_argument("--configs", default="yolox_nano.dyn:416,yolox_nano.int8:416,yolox_tiny.dyn:416,"
                    "yolox_tiny.int8:416,yolox_tiny.dyn:320,yolox_tiny.int8:320,yolox_tiny.dyn:512,"
                    "yolox_tiny.int8:512,yolox_tiny.dyn:640,yolox_s.dyn:416,yolox_s.int8:416")
    a = ap.parse_args()
    images = [open(f, "rb").read() for f in sorted(glob.glob(os.path.join(a.frames_dir, "*.jpg")))]

    def run(spec):
        name, w = spec.split(":")
        return Detector(os.path.join(MODELS, name + ".onnx"), input_width=int(w)).detect(images)

    ref = run(a.ref)
    print(f"基準 {a.ref}: {sum(map(len, ref))} 件(person {sum(d.label == 'person' for r in ref for d in r)})")
    print("| 設定 | 検出数 | 適合率 | 再現率 | F1 | person 再現率 |")
    print("| --- | ---: | ---: | ---: | ---: | ---: |")
    for spec in a.configs.split(","):
        got = run(spec)
        tp = n_got = n_ref = 0
        ptp = pref = 0
        for r, g in zip(ref, got):
            t, ng, nr = match(r, g)
            tp, n_got, n_ref = tp + t, n_got + ng, n_ref + nr
            t, _, nr = match(r, g, "person")
            ptp, pref = ptp + t, pref + nr
        p = tp / max(1, n_got)
        rc = tp / max(1, n_ref)
        f1 = 2 * p * rc / max(1e-9, p + rc)
        print(f"| {spec} | {n_got} | {p:.2f} | {rc:.2f} | {f1:.2f} | {ptp / max(1, pref):.2f} |")


if __name__ == "__main__":
    main()
