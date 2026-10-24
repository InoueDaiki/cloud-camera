#!/usr/bin/env python3
"""物体検出の検証用サンプル動画(人と車が映るもの)を取得して emulator/media/ に mp4 で置く。

ライセンスは商用利用可のもの(CC0 / パブリックドメイン)だけを選んでいる。出典と条件は README を参照。
Wikimedia Commons から原本(WebM)を取得し、FFmpeg で 1280x720 の H.264 mp4(音声なし)に変換する。

使い方:
  python3 emulator/scripts/fetch_sample_media.py               # ホストに ffmpeg がある場合
  docker run --rm -v ./emulator/media:/media cloud-camera/onvif-emulator \
      python /app/scripts/fetch_sample_media.py --out /media     # コンテナの ffmpeg を使う場合
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

UA = "cloud-camera-onvif-emulator/0.1 (https://github.com/; sample media fetcher)"

# name: 出力ファイル名(拡張子なし)
SAMPLES = [
    {
        "name": "crosswalk_toronto",
        "url": "https://upload.wikimedia.org/wikipedia/commons/d/d2/DiagonalCrosswalkYongeDundas.webm",
        "page": "https://commons.wikimedia.org/wiki/File:DiagonalCrosswalkYongeDundas.webm",
        "license": "CC0 1.0",
        "author": "Raysonho @ Open Grid Scheduler / Grid Engine",
        "content": "トロント Yonge & Dundas のスクランブル交差点(歩行者、車)",
    },
    {
        "name": "traffic_bangkok",
        "url": "https://upload.wikimedia.org/wikipedia/commons/a/a9/Bangkok_Traffic_During_Enduring_Partners_26_%281009429%29.webm",
        "page": "https://commons.wikimedia.org/wiki/File:Bangkok_Traffic_During_Enduring_Partners_26_(1009429).webm",
        "license": "Public domain(米国連邦政府職員の職務著作物、DVIDS)",
        "author": "U.S. Army National Guard video by Staff Sgt. James Bunn",
        "content": "バンコクの道路(車、バイク、歩行者)",
    },
]


def download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r, dest.open("wb") as f:
        shutil.copyfileobj(r, f, 1024 * 1024)


def transcode(ffmpeg: str, src: Path, dst: Path, width: int, height: int) -> None:
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p"
    )
    tmp = dst.with_suffix(".tmp.mp4")
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-an", "-vf", vf,
         "-c:v", "libx264", "-preset", "medium", "-crf", "23", "-movflags", "+faststart", str(tmp)],
        check=True,
    )
    tmp.replace(dst)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "media"))
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--force", action="store_true", help="既存のファイルも作り直す")
    args = ap.parse_args()

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        print("ffmpeg が見つかりません。コンテナ内で実行してください(スクリプト冒頭の使い方を参照)", file=sys.stderr)
        return 1
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        for s in SAMPLES:
            dst = out / f"{s['name']}.mp4"
            if dst.exists() and not args.force:
                print(f"skip  {dst}(既にあります)")
                continue
            src = Path(td) / f"{s['name']}.webm"
            print(f"get   {s['page']}")
            download(s["url"], src)
            print(f"conv  {dst}")
            transcode(ffmpeg, src, dst, args.width, args.height)
            digest = hashlib.sha256(dst.read_bytes()).hexdigest()[:16]
            print(f"done  {dst} ({dst.stat().st_size // 1024} KiB, sha256 {digest}…)  {s['license']} / {s['author']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
