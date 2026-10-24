"""ベンチマーク用の 60 フレーム(16:9 の JPEG)を作る。

GetImages に WidthPixels を指定したときと同じく、幅 w・アスペクト比 16:9 の JPEG を
bench_frames/{w}/000.jpg〜059.jpg に書く。元画像を少しずつずらして切り出し、動きを模擬する。

使い方: uv run python tools/make_bench_frames.py models/calib bench_frames --widths 320,416,512,640
"""

from __future__ import annotations

import argparse
import glob
import os

from PIL import Image


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("src_dir")
    ap.add_argument("out_dir")
    ap.add_argument("--widths", default="320,416,512,640")
    ap.add_argument("--count", type=int, default=60)
    a = ap.parse_args()
    # 人が写っている画像を優先(person, kite, horses)
    srcs = [p for name in ("person", "kite", "horses", "dog")
            for p in glob.glob(os.path.join(a.src_dir, f"{name}.jpg"))]
    per = -(-a.count // len(srcs))
    widths = [int(w) for w in a.widths.split(",")]
    n = 0
    for p in srcs:
        img = Image.open(p).convert("RGB")
        w, h = img.size
        # 16:9 の窓を 90% の大きさで取り、左上から右下へずらす
        cw = int(min(w, h * 16 / 9) * 0.9)
        ch = int(cw * 9 / 16)
        for k in range(per):
            if n >= a.count:
                break
            t = k / max(1, per - 1)
            x = int((w - cw) * t)
            y = int((h - ch) * t)
            crop = img.crop((x, y, x + cw, y + ch))
            for tw in widths:
                d = os.path.join(a.out_dir, str(tw))
                os.makedirs(d, exist_ok=True)
                crop.resize((tw, round(tw * 9 / 16)), Image.BILINEAR).save(
                    os.path.join(d, f"{n:03d}.jpg"), quality=85
                )
            n += 1
    print(f"{n} フレーム x {len(widths)} サイズ → {a.out_dir}")


if __name__ == "__main__":
    main()
