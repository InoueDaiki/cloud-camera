"""実 KVS で各 API の挙動を確かめる調査用スクリプト(ローカルから実行)。

    cd lambdas/video_archiver
    mise exec -- uv run python testing/probe.py <stream-name> <out-dir> [minutes-ago]

- ListFragments: 断片の長さ、プロデューサー時刻とサーバー時刻の差(到着の遅れ)
- GetImages: H.265 で画像が返るか
- HLS(fMP4): init と断片を取得して連結
- GetClip: 出力の MP4 の構造(トップレベルのボックス)
"""

from __future__ import annotations

import json
import struct
import sys
import time
from datetime import timedelta
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[2])]

from archiver import build_clip, build_playlist, build_sprite  # noqa: E402
from common import kvs  # noqa: E402
from common.timeutil import floor_minute, utcnow  # noqa: E402


def boxes(data: bytes, limit: int = 40) -> list[str]:
    out, i = [], 0
    while i + 8 <= len(data) and len(out) < limit:
        size, typ = struct.unpack(">I4s", data[i : i + 8])
        if size == 1:
            size = struct.unpack(">Q", data[i + 8 : i + 16])[0]
        out.append(f"{typ.decode(errors='replace')}:{size}")
        if size < 8:
            break
        i += size
    return out


def main():
    stream, out = sys.argv[1], Path(sys.argv[2])
    ago = int(sys.argv[3]) if len(sys.argv) > 3 else 2
    out.mkdir(parents=True, exist_ok=True)
    start = floor_minute(utcnow()) - timedelta(minutes=ago)
    end = start + timedelta(minutes=1)
    print("range", start, end)

    t = time.monotonic()
    frags = kvs.list_fragments(stream, start, end)
    print(f"ListFragments: {len(frags)} fragments in {int((time.monotonic()-t)*1000)} ms")
    if frags:
        lens = [f.length_ms for f in frags]
        delays = [f.server_ms - f.producer_ms for f in frags]
        print(" length_ms min/max", min(lens), max(lens), " server-producer ms min/max", min(delays), max(delays))
        print(" first", frags[0], "\n last", frags[-1])
        print(" segments", kvs.build_segments(frags))

    # GetImages
    t = time.monotonic()
    try:
        imgs = kvs.get_images(stream, start, end, interval_ms=1000, width=160, height=90)
        ok = [i for i in imgs if i.content]
        errs = sorted({i.error for i in imgs if i.error})
        print(f"GetImages: {len(imgs)} entries, {len(ok)} with content, errors={errs} in {int((time.monotonic()-t)*1000)} ms")
        if ok:
            (out / "image0.jpg").write_bytes(ok[0].content)
        sprite, n = build_sprite(imgs, start)
        (out / "sprite.jpg").write_bytes(sprite)
        print(f" sprite {len(sprite)} bytes, {n} frames")
    except Exception as e:  # noqa: BLE001
        print("GetImages error:", type(e).__name__, e)

    # HLS
    t = time.monotonic()
    clip = build_clip(stream, start, end, frags)
    print(f"HLS clip: {len(clip.data)} bytes, {len(clip.parts)} parts, init {clip.init_length} in {int((time.monotonic()-t)*1000)} ms")
    print(" init boxes", boxes(clip.data[: clip.init_length]))
    print(" frag0 boxes", boxes(clip.data[clip.parts[0][0] : clip.parts[0][0] + clip.parts[0][1]]))
    (out / "clip.mp4").write_bytes(clip.data)
    (out / "index.m3u8").write_text(build_playlist(clip))
    json.dump({"parts": clip.parts, "init_length": clip.init_length}, open(out / "hls.json", "w"))

    # GetClip
    t = time.monotonic()
    try:
        c = kvs.archived_media_client(stream, kvs.GET_CLIP)
        r = c.get_clip(StreamName=stream, ClipFragmentSelector={"FragmentSelectorType": "PRODUCER_TIMESTAMP",
                                                                "TimestampRange": {"StartTimestamp": start, "EndTimestamp": end}})
        data = r["Payload"].read()
        print(f"GetClip: {len(data)} bytes, {r.get('ContentType')} in {int((time.monotonic()-t)*1000)} ms")
        bx = boxes(data, 400)
        print(" boxes", bx[:8], "... total", len(bx), "moof", sum(1 for b in bx if b.startswith("moof")))
        (out / "getclip.mp4").write_bytes(data)
    except Exception as e:  # noqa: BLE001
        print("GetClip error:", type(e).__name__, e)


if __name__ == "__main__":
    main()
