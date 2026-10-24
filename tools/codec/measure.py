#!/usr/bin/env python3
"""H.264 と H.265 の同画質でのビットレート比を測る(docs/codec.md)。

tools/codec/Dockerfile のイメージ(FFmpeg + libx264 + libx265 + libvmaf)の中で動かす。標準ライブラリだけを使う。

  prepare   : 場面ごとの参照映像(1280x720、15 fps、FFV1 の可逆圧縮)を作る
  sweep     : 参照映像を H.264 / H.265 で CRF を変えてエンコードし、ビットレート、VMAF、PSNR、SSIM、デコード時間を測る
  summarize : 同じ VMAF になるビットレートを補間し、H.264 / H.265 の比(と BD-rate)を出す

エンコードの引数はエミュレーター(emulator/src/onvif_emulator/streams.py)の main / sub と同じ
(GOP 固定 = キーフレーム 1 秒、シーンカットなし、B フレームなし、zerolatency、CRF の VBR)。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

W, H, FPS = 1280, 720, 15
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"

# 焼き込み(エミュレーターと同じ 3 行、位置・大きさ。時刻は pts から)
OVERLAY_TEXT = (
    f"cam-001  {W}x{H}@{FPS}\n"
    r"%{pts:gmtime:1761300000:%Y-%m-%dT%H\:%M\:%S}.%{eif:mod(floor(t*1000)\,1000):d:3}Z" "\n"
    r"frame %{frame_num}"
)
OVERLAY_FILE = "/tmp/overlay.txt"
OVERLAY = (
    f"drawtext=fontfile={FONT}:textfile={OVERLAY_FILE}:expansion=normal:fontsize={H // 24}:fontcolor=white:"
    "box=1:boxcolor=black@0.6:boxborderw=8:line_spacing=6:x=16:y=16"
)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        sys.stderr.write(r.stderr[-3000:])
        raise subprocess.CalledProcessError(r.returncode, cmd[:3])
    return r


# ---------------------------------------------------------------- prepare


def scene_input(scene: str, media: Path, seconds: int, start: float) -> tuple[list[str], str]:
    """場面 → (FFmpeg の入力引数, フィルター)。"""
    fit = (f"fps={FPS},scale={W}:{H}:force_original_aspect_ratio=decrease,"
           f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p")
    if scene == "testsrc":
        # エミュレーターの既定(疑似 PTZ の元映像は main の 2 倍で作って縮める)
        return ["-f", "lavfi", "-i", f"testsrc=size={W * 2}x{H * 2}:rate={FPS}"], f"{fit},{OVERLAY}"
    if scene in ("crosswalk", "traffic"):
        file = media / ("crosswalk_toronto.mp4" if scene == "crosswalk" else "traffic_bangkok.mp4")
        return ["-ss", str(start), "-stream_loop", "-1", "-i", str(file)], f"{fit},{OVERLAY}"
    if scene == "still":
        # 静かな場面: 交差点の 1 コマを止めて、時刻の焼き込みと軽いセンサーノイズ(時間方向にも変わる)を足す
        file = media / "crosswalk_toronto.mp4"
        return (["-ss", str(start), "-i", str(file)],
                f"{fit},trim=end_frame=1,loop=loop=-1:size=1,setpts=N/{FPS}/TB,noise=alls=3:allf=t,{OVERLAY}")
    raise SystemExit(f"不明な場面: {scene}")


def cmd_prepare(a) -> None:
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    Path(OVERLAY_FILE).write_text(OVERLAY_TEXT)
    for scene in a.scenes.split(","):
        dst = out / f"{scene}.mkv"
        if dst.exists() and not a.force:
            print(f"skip {dst}")
            continue
        inp, vf = scene_input(scene, Path(a.media), a.seconds, a.start)
        run(["ffmpeg", "-hide_banner", "-y", *inp, "-an", "-vf", vf, "-frames:v", str(a.seconds * FPS),
             "-c:v", "ffv1", "-level", "3", "-g", "1", str(dst)])
        print(f"done {dst}")


# ---------------------------------------------------------------- sweep


def encode_args(codec: str, preset: str, crf: int, maxrate_kbps: int | None) -> list[str]:
    gop = FPS  # キーフレーム 1 秒
    if codec == "h265":
        params = [f"keyint={gop}", f"min-keyint={gop}", "scenecut=0", "open-gop=0", "repeat-headers=1", "aud=1",
                  f"crf={crf}", "log-level=error"]
        if maxrate_kbps:
            params += [f"vbv-maxrate={maxrate_kbps}", f"vbv-bufsize={maxrate_kbps * 2}"]
        return ["-c:v", "libx265", "-preset", preset, "-tune", "zerolatency", "-profile:v", "main",
                "-pix_fmt", "yuv420p", "-x265-params", ":".join(params), "-f", "hevc"]
    if codec == "h264":
        args = ["-c:v", "libx264", "-preset", preset, "-tune", "zerolatency", "-profile:v", "high",
                "-pix_fmt", "yuv420p", "-crf", str(crf), "-g", str(gop), "-keyint_min", str(gop),
                "-sc_threshold", "0", "-bf", "0"]
        if maxrate_kbps:
            args += ["-maxrate", f"{maxrate_kbps}k", "-bufsize", f"{maxrate_kbps * 2}k"]
        return [*args, "-f", "h264"]
    raise SystemExit(f"不明なコーデック: {codec}")


def _utime(stderr: str) -> float:
    m = re.search(r"bench: utime=([\d.]+)s", stderr)
    return float(m.group(1)) if m else float("nan")


def gop_sizes(path: Path, codec: str) -> list[int]:
    """GOP(1 秒)ごとのバイト数。"""
    out = run(["ffprobe", "-v", "error", "-f", "hevc" if codec == "h265" else "h264", "-select_streams", "v:0",
               "-show_entries", "packet=size,flags", "-of", "csv=p=0", str(path)]).stdout
    sizes: list[int] = []
    for line in out.splitlines():
        parts = line.split(",")
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        size, flags = int(parts[0]), parts[1]
        if "K" in flags or not sizes:
            sizes.append(0)
        sizes[-1] += size
    return sizes


def measure_one(ref: Path, codec: str, preset: str, crf: int, maxrate: int | None, threads: int) -> dict:
    scene = ref.stem
    with tempfile.TemporaryDirectory() as td:
        dist = Path(td) / f"out.{codec}"
        r = subprocess.run(["ffmpeg", "-hide_banner", "-y", "-benchmark", "-i", str(ref), "-an",
                            *encode_args(codec, preset, crf, maxrate), str(dist)],
                           check=True, capture_output=True, text=True)
        enc_s = _utime(r.stderr)
        frames = int(run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
                          "stream=nb_read_frames", "-of", "csv=p=0", str(ref)]).stdout.strip())
        size = dist.stat().st_size
        seconds = frames / FPS
        gops = gop_sizes(dist, codec)
        # VMAF + PSNR + SSIM(フレームごとの値から平均と 5 パーセンタイルを出す)。
        # 両方の pts をフレーム番号で振り直す(参照の MKV は 1 ms 単位なので 66.67 ms と 67 ms がずれ、
        # framesync が 1 つ前の参照フレームと比べてしまう)
        log = Path(td) / "vmaf.json"
        fmt = "hevc" if codec == "h265" else "h264"
        run(["ffmpeg", "-hide_banner", "-r", str(FPS), "-f", fmt, "-i", str(dist), "-i", str(ref), "-lavfi",
             f"[0:v]setpts=N/({FPS}*TB)[d];[1:v]setpts=N/({FPS}*TB)[r];"
             f"[d][r]libvmaf=n_threads={threads}:log_fmt=json:log_path={log}:"
             "feature=name=psnr|name=float_ssim", "-f", "null", "-"])
        doc = json.loads(log.read_text())
        per = [f["metrics"] for f in doc["frames"]]
        vm = sorted(p["vmaf"] for p in per)
        pooled = doc["pooled_metrics"]
        # デコード時間(1 スレッド)。キーフレームだけ(Lambda のスプライト・検出と同じ)と全フレーム
        key = subprocess.run(["ffmpeg", "-hide_banner", "-benchmark", "-threads", "1", "-skip_frame", "nokey",
                              "-f", fmt, "-i", str(dist), "-f", "null", "-"], check=True, capture_output=True, text=True)
        full = subprocess.run(["ffmpeg", "-hide_banner", "-benchmark", "-threads", "1", "-f", fmt, "-i", str(dist),
                               "-f", "null", "-"], check=True, capture_output=True, text=True)
        nkey = len(gops)
    return {
        "scene": scene, "codec": codec, "preset": preset, "crf": crf, "maxrate_kbps": maxrate,
        "frames": frames, "bytes": size, "kbps": round(size * 8 / seconds / 1000, 1),
        "gop_kbps_max": round(max(gops) * 8 / 1000, 1) if gops else None,
        "gop_kbps_min": round(min(gops) * 8 / 1000, 1) if gops else None,
        "vmaf": round(pooled["vmaf"]["mean"], 3), "vmaf_p5": round(vm[max(0, int(len(vm) * 0.05) - 1)], 3),
        "psnr_y": round(pooled["psnr_y"]["mean"], 3), "ssim": round(pooled["float_ssim"]["mean"], 5),
        "encode_cpu_s": enc_s,
        "decode_key_ms_per_frame": round(_utime(key.stderr) * 1000 / max(1, nkey), 2),
        "decode_all_ms_per_frame": round(_utime(full.stderr) * 1000 / frames, 2),
        "keyframes": nkey
    }


def cmd_sweep(a) -> None:
    refs = [Path(p) for p in a.refs]
    jobs = []
    for ref in refs:
        for preset in a.presets.split(","):
            for codec, crfs in (("h264", a.crf_h264), ("h265", a.crf_h265)):
                for crf in [int(c) for c in crfs.split(",")]:
                    jobs.append((ref, codec, preset, crf, a.maxrate or None))
    done = set()
    out = Path(a.out)
    if out.exists():
        for line in out.read_text().splitlines():
            d = json.loads(line)
            done.add((d["scene"], d["codec"], d["preset"], d["crf"], d["maxrate_kbps"]))
    jobs = [j for j in jobs if (j[0].stem, j[1], j[2], j[3], j[4]) not in done]
    print(f"{len(jobs)} 件を測る(済み {len(done)} 件)", flush=True)
    with ThreadPoolExecutor(a.jobs) as ex, out.open("a") as f:
        futs = [ex.submit(measure_one, *j, max(1, (os.cpu_count() or 4) // a.jobs)) for j in jobs]
        for fut in futs:
            d = fut.result()
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
            f.flush()
            print(f"{d['scene']:10s} {d['codec']} {d['preset']:9s} crf={d['crf']:2d} {d['kbps']:8.1f} kbps "
                  f"vmaf={d['vmaf']:.2f} psnr={d['psnr_y']:.2f} ssim={d['ssim']:.4f}", flush=True)


# ---------------------------------------------------------------- summarize


def interp_rate(points: list[dict], target: float, metric: str = "vmaf") -> float | None:
    """metric が target になるビットレート(log ビットレートを metric で線形補間)。範囲外は None。"""
    pts = sorted((p[metric], math.log(p["kbps"])) for p in points)
    for (q0, r0), (q1, r1) in zip(pts, pts[1:], strict=False):
        if q0 <= target <= q1 and q1 > q0:
            return math.exp(r0 + (r1 - r0) * (target - q0) / (q1 - q0))
    return None


def interp_crf(points: list[dict], target: float) -> float | None:
    pts = sorted((p["vmaf"], p["crf"]) for p in points)
    for (q0, c0), (q1, c1) in zip(pts, pts[1:], strict=False):
        if q0 <= target <= q1 and q1 > q0:
            return c0 + (c1 - c0) * (target - q0) / (q1 - q0)
    return None


def bd_rate(a_pts: list[dict], b_pts: list[dict], lo: float, hi: float, metric: str = "vmaf") -> float | None:
    """b の a に対するビットレートの比(同じ品質での幾何平均)。区間 [lo, hi] と両者の範囲の重なりで平均する。"""
    lo = max(lo, min(p[metric] for p in a_pts), min(p[metric] for p in b_pts))
    hi = min(hi, max(p[metric] for p in a_pts), max(p[metric] for p in b_pts))
    if hi - lo < 0.5:
        return None
    n = 50
    acc = []
    for i in range(n + 1):
        q = lo + (hi - lo) * i / n
        ra, rb = interp_rate(a_pts, q, metric), interp_rate(b_pts, q, metric)
        if ra and rb:
            acc.append(math.log(rb / ra))
    return math.exp(sum(acc) / len(acc)) if acc else None


def cmd_summarize(a) -> None:
    rows = [json.loads(l) for l in Path(a.input).read_text().splitlines() if l.strip()]
    rows = [r for r in rows if not r.get("maxrate_kbps")]
    targets = [float(t) for t in a.targets.split(",")]
    groups: dict[tuple[str, str], dict[str, list[dict]]] = {}
    for r in rows:
        groups.setdefault((r["scene"], r["preset"]), {}).setdefault(r["codec"], []).append(r)
    summary = []
    for (scene, preset), by in sorted(groups.items()):
        if "h264" not in by or "h265" not in by:
            continue
        h264, h265 = by["h264"], by["h265"]
        item = {"scene": scene, "preset": preset, "targets": {}}
        for t in targets:
            r4, r5 = interp_rate(h264, t), interp_rate(h265, t)
            item["targets"][str(t)] = {
                "h264_kbps": round(r4, 1) if r4 else None, "h265_kbps": round(r5, 1) if r5 else None,
                "ratio_h264_over_h265": round(r4 / r5, 3) if r4 and r5 else None,
                "h264_crf": round(interp_crf(h264, t), 1) if interp_crf(h264, t) else None,
                "h265_crf": round(interp_crf(h265, t), 1) if interp_crf(h265, t) else None,
            }
        bd = bd_rate(h265, h264, a.bd_lo, a.bd_hi)
        item["bd_ratio_vmaf"] = round(bd, 3) if bd else None
        bdp = bd_rate(h265, h264, 30, 50, metric="psnr_y")
        item["bd_ratio_psnr"] = round(bdp, 3) if bdp else None
        # エミュレーターの既定(H.265 CRF 28 = Quality 60)と同じ VMAF になる H.264 の CRF
        base = next((p for p in h265 if p["crf"] == a.base_crf), None)
        if base:
            c4 = interp_crf(h264, base["vmaf"])
            r4 = interp_rate(h264, base["vmaf"])
            item["base"] = {"h265_crf": a.base_crf, "h265_kbps": base["kbps"], "vmaf": base["vmaf"],
                            "h264_crf": round(c4, 1) if c4 else None, "h264_kbps": round(r4, 1) if r4 else None,
                            "ratio": round(r4 / base["kbps"], 3) if r4 else None}
        # デコード時間(全 CRF の平均)
        for c, pts in (("h264", h264), ("h265", h265)):
            item[f"{c}_decode_key_ms"] = round(sum(p["decode_key_ms_per_frame"] for p in pts) / len(pts), 2)
            item[f"{c}_decode_all_ms"] = round(sum(p["decode_all_ms_per_frame"] for p in pts) / len(pts), 2)
        summary.append(item)
    print(json.dumps(summary, ensure_ascii=False, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--scenes", default="testsrc,crosswalk,traffic,still")
    p.add_argument("--media", default="/media")
    p.add_argument("--out", default="/work/ref")
    p.add_argument("--seconds", type=int, default=30)
    p.add_argument("--start", type=float, default=5.0)
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_prepare)
    s = sub.add_parser("sweep")
    s.add_argument("refs", nargs="+")
    s.add_argument("--presets", default="ultrafast")
    s.add_argument("--crf-h264", default="18,20,22,24,26,28,30,32,34,36")
    s.add_argument("--crf-h265", default="20,22,24,26,28,30,32,34,36,38")
    s.add_argument("--maxrate", type=int, default=0, help="VBR の上限(kbps)。0 で上限なし(曲線を取るとき)")
    s.add_argument("--jobs", type=int, default=2)
    s.add_argument("--out", default="/work/results.jsonl")
    s.set_defaults(fn=cmd_sweep)
    m = sub.add_parser("summarize")
    m.add_argument("--input", default="/work/results.jsonl")
    m.add_argument("--targets", default="85,90,93,95")
    m.add_argument("--bd-lo", type=float, default=80)
    m.add_argument("--bd-hi", type=float, default=97)
    m.add_argument("--base-crf", type=int, default=28)
    m.set_defaults(fn=cmd_summarize)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
