"""fMP4(clip.mp4、HLS の init + 断片)から H.265 のキーフレームだけをデコードする(GetImages の代わり、案 B)。

- 非キーフレームはデコーダに渡さない(パケットのキーフレーム印で選ぶ。念のため `skip_frame="NONKEY"` も付ける)。
  デコードするのはキーフレーム(カメラの設定で 1 秒に 1 枚)だけなので、1 分 900 フレームのうち 60 枚で済む
- 各フレームの時刻はプロデューサー時刻(epoch ミリ秒)。「断片の開始時刻(ListFragments の ProducerTimestamp)+
  断片内の pts の差」で求める(`anchors`)。断片ごとに時刻を当てるので、録画の切れ目(時間軸の空白)があってもずれない。
  `anchors` がなければ「最初のフレーム = base_ms」として pts の差だけで求める(KVS の HLS の tfdt は実時刻の差を保っている)
- 区間 [start, end) を interval(既定 1 秒)ごとの枠に分け、各枠で最初のキーフレームを 1 枚だけ使う。
  枠が埋まっているキーフレームと区間の外のキーフレームはデコードしない
- 出力サイズは複数指定できる(スプライト 160×90、推論用 416×234 など)。縮小は swscale(FFmpeg)で行う

依存: PyAV(`av`)。PIL で受け取るなら Pillow、numpy で受け取るなら numpy。使い方は FRAMES.md。
"""

from __future__ import annotations

import bisect
import io
import os
import time
from dataclasses import dataclass, field
from typing import Any, BinaryIO, Iterable, Literal, Sequence

Size = tuple[int, int | None]  # (幅, 高さ)。高さ None は縦横比を保つ

# スプライト(docs/contracts.md の sprite.jpg)
SPRITE_COLS, SPRITE_ROWS = 10, 6
THUMB_W, THUMB_H = 160, 90
SPRITE_QUALITY = 70


@dataclass
class KeyFrame:
    ts_ms: int  # プロデューサー時刻(epoch ミリ秒)
    slot: int  # 区間の開始からの枠の番号(0 〜)。1 秒間隔なら「区間の開始 + slot 秒」の枠
    pts_s: float  # ファイル内の pts(秒)
    images: dict[str, Any]  # サイズ名 → 画像(PIL.Image か HWC の RGB uint8 の numpy 配列)


@dataclass
class DecodeResult:
    frames: list[KeyFrame]  # 時刻順
    stats: dict = field(default_factory=dict)  # ログ用


def anchors_from_fragments(parts: Sequence[Sequence[int]], producer_ms: Sequence[int]) -> list[tuple[int, int]]:
    """clip.mp4 の断片のオフセット(`hls.parts` の [offset, length, duration_ms])と、各断片の
    ProducerTimestamp(ListFragments)から anchors を作る。video_archiver は両方持っている。"""
    if len(parts) != len(producer_ms):
        raise ValueError(f"parts ({len(parts)}) と producer_ms ({len(producer_ms)}) の数が違う")
    return [(int(p[0]), int(ms)) for p, ms in zip(parts, producer_ms)]


def anchors_from_hls(parts: Sequence[Sequence[int]], segments: Sequence[Sequence[int]]) -> list[tuple[int, int]]:
    """DynamoDB の区間の件(`hls.parts` と `segments`)だけから anchors を作る(detector が S3 の clip.mp4 を読む場合)。

    `segments` は断片の実効長(`common.kvs.effective_durations`、`hls.parts` の duration_ms と同じ値)でつないだもの
    なので、segment の開始から parts の長さを足していくと各断片の開始時刻になる。足した値が segment の終わりに
    達したら、次の断片は次の segment の開始から始まる(録画の切れ目)。
    """
    out: list[tuple[int, int]] = []
    if not parts:
        return out
    if not segments:
        raise ValueError("segments が空")
    k = 0
    t = int(segments[0][0])
    for offset, _length, dur in parts:
        if k + 1 < len(segments) and t >= int(segments[k][1]) - 1:
            k += 1
            t = int(segments[k][0])
        out.append((int(offset), t))
        t += int(dur)
    return out


def _open_source(src: bytes | bytearray | memoryview | str | os.PathLike | BinaryIO):
    if isinstance(src, (bytes, bytearray, memoryview)):
        return io.BytesIO(bytes(src))
    return src


def _box():
    from PIL import Image as PILImage

    return PILImage.Resampling.BOX


def _resolve(size: Size, w0: int, h0: int) -> tuple[int, int]:
    w, h = size
    if h is None:
        h = max(2, round(w * h0 / w0 / 2) * 2)
    return int(w), int(h)


def decode_keyframes(
    src: bytes | bytearray | memoryview | str | os.PathLike | BinaryIO,
    *,
    start_ms: int,
    end_ms: int,
    sizes: dict[str, Size] | None = None,
    anchors: Sequence[tuple[int, int]] | None = None,
    base_ms: int | None = None,
    interval_ms: int = 1000,
    output: Literal["pil", "numpy", "frame"] = "pil",
    interpolation: str | None = "AREA",
    threads: int = 0,
) -> DecodeResult:
    """fMP4 から区間 [start_ms, end_ms) のキーフレームを interval_ms ごとに 1 枚ずつデコードする。

    src: fMP4 のバイト列、パス、またはファイルオブジェクト(init + 断片を連結した clip.mp4。HLS の init と
         断片をつないだバイト列でもよい)
    sizes: サイズ名 → (幅, 高さ)。高さ None は縦横比を保つ。None なら {"full": 元のサイズ}
    anchors: [(断片のバイトオフセット, その断片の ProducerTimestamp ms), ...]。`anchors_from_fragments` /
         `anchors_from_hls` で作る。省略時は base_ms(最初のフレームの時刻)が必要
    output: "pil"(PIL.Image)、"numpy"(HWC の RGB uint8)、"frame"(av.VideoFrame、縮小しない)
    interpolation: swscale の補間("AREA"、"BILINEAR"、"BICUBIC" など。None は PyAV の既定)
    threads: デコーダのスレッド数(0 は FFmpeg の自動)
    """
    import av  # 遅延 import(使わない関数で読み込み時間を払わない)

    if anchors is None and base_ms is None:
        raise ValueError("anchors か base_ms のどちらかが要る")
    if interval_ms <= 0 or end_ms <= start_ms:
        raise ValueError("interval_ms > 0、start_ms < end_ms にする")
    anchor_offsets = [a[0] for a in anchors] if anchors else []
    anchor_ms = [a[1] for a in anchors] if anchors else []
    n_slots = -(-(end_ms - start_ms) // interval_ms)

    t0 = time.perf_counter()
    stats: dict[str, Any] = {"packets": 0, "keyframes": 0, "decoded": 0, "skipped_outside": 0,
                             "skipped_filled": 0, "decode_ms": 0.0, "scale_ms": 0.0}
    frames: list[KeyFrame] = []
    filled: set[int] = set()
    first_pts: dict[int, float] = {}  # アンカー(断片)ごとの最初のパケットの pts
    pending: dict[int, tuple[int, int, float]] = {}  # pts → (ts_ms, slot, pts_s)。デコーダの遅延に備える

    container = av.open(_open_source(src), mode="r")
    try:
        stream = container.streams.video[0]
        ctx = stream.codec_context
        ctx.skip_frame = "NONKEY"
        if threads:
            ctx.thread_count = threads
        ctx.thread_type = "AUTO" if threads != 1 else "NONE"
        tb = float(stream.time_base)
        stats["codec"] = ctx.name
        resolved: dict[str, tuple[int, int]] | None = None

        def emit(frame) -> None:
            nonlocal resolved
            key = frame.pts
            if key not in pending:
                return
            ts, slot, pts_s = pending.pop(key)
            stats["decoded"] += 1
            if resolved is None:
                stats["width"], stats["height"] = frame.width, frame.height
                resolved = {name: _resolve(sz, frame.width, frame.height)
                            for name, sz in (sizes or {"full": (frame.width, frame.height)}).items()}
            ts0 = time.perf_counter()
            images: dict[str, Any] = {}
            if output == "frame":
                images = {name: frame for name in resolved}
            else:
                # 一番大きいサイズだけ swscale(YUV → RGB と縮小を 1 回で)で作り、小さいサイズはその RGB から
                # Pillow の BOX で縮める。swscale を 2 回呼ぶより速い(フル解像度の YUV → RGB 変換が重いため。
                # 1080p・1 コアで 60 枚 297 → 99 ms、FRAMES.md)
                order = sorted(resolved.items(), key=lambda kv: kv[1][0] * kv[1][1], reverse=True)
                big_name, (bw, bh) = order[0]
                kw = {"width": bw, "height": bh, "format": "rgb24"}
                if interpolation:
                    kw["interpolation"] = interpolation
                big = frame.reformat(**kw).to_image()
                pil = {big_name: big}
                for name, (w, h) in order[1:]:
                    pil[name] = big if (w, h) == (bw, bh) else big.resize((w, h), _box())
                if output == "pil":
                    images = pil
                else:
                    import numpy as np

                    images = {name: np.asarray(im) for name, im in pil.items()}
            stats["scale_ms"] += (time.perf_counter() - ts0) * 1000
            frames.append(KeyFrame(ts, slot, pts_s, images))

        def decode(packet) -> None:
            td = time.perf_counter()
            out = ctx.decode(packet)
            stats["decode_ms"] += (time.perf_counter() - td) * 1000
            for fr in out:
                emit(fr)

        for packet in container.demux(stream):
            if packet.pts is None:  # 終わりの空パケット
                continue
            stats["packets"] += 1
            if not packet.is_keyframe:
                continue  # 非キーフレームはデコーダに渡さない
            stats["keyframes"] += 1
            pts_s = packet.pts * tb
            if anchors:
                i = bisect.bisect_right(anchor_offsets, packet.pos) - 1 if packet.pos is not None and packet.pos >= 0 else -1
                i = max(i, 0)
                first_pts.setdefault(i, pts_s)
                ts = anchor_ms[i] + round((pts_s - first_pts[i]) * 1000)
            else:
                first_pts.setdefault(0, pts_s)
                ts = base_ms + round((pts_s - first_pts[0]) * 1000)
            if not start_ms <= ts < end_ms:
                stats["skipped_outside"] += 1
                continue
            slot = (ts - start_ms) // interval_ms
            if slot in filled:
                stats["skipped_filled"] += 1
                continue
            filled.add(slot)
            pending[packet.pts] = (ts, slot, pts_s)
            decode(packet)
        decode(None)  # 遅延して出てくるフレームを吐き出す
    finally:
        container.close()

    frames.sort(key=lambda f: f.ts_ms)
    stats["frames"] = len(frames)
    stats["slots"] = n_slots
    stats["lost"] = len(pending)  # デコードに失敗したキーフレーム
    stats["total_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    stats["decode_ms"] = round(stats["decode_ms"], 1)
    stats["scale_ms"] = round(stats["scale_ms"], 1)
    return DecodeResult(frames, stats)


def build_sprite(
    frames: Iterable[KeyFrame],
    key: str = "sprite",
    *,
    cols: int = SPRITE_COLS,
    rows: int = SPRITE_ROWS,
    thumb: tuple[int, int] = (THUMB_W, THUMB_H),
    quality: int = SPRITE_QUALITY,
) -> tuple[bytes, int]:
    """KeyFrame.slot の位置にコマを並べたシート(sprite.jpg)を作る。コマがない枠は黒。戻り値は (JPEG, 埋めたコマ数)。"""
    from PIL import Image as PILImage

    tw, th = thumb
    sheet = PILImage.new("RGB", (tw * cols, th * rows), (0, 0, 0))
    n = 0
    for f in frames:
        if not 0 <= f.slot < cols * rows:
            continue
        im = f.images[key]
        if not isinstance(im, PILImage.Image):
            im = PILImage.fromarray(im)
        if im.size != (tw, th):
            im = im.resize((tw, th))
        sheet.paste(im, ((f.slot % cols) * tw, (f.slot // cols) * th))
        n += 1
    out = io.BytesIO()
    sheet.save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue(), n


__all__ = ["DecodeResult", "KeyFrame", "anchors_from_fragments", "anchors_from_hls", "build_sprite", "decode_keyframes"]
