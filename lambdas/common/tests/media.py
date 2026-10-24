"""テスト用の H.265(または H.264)fMP4 を作る(common / video_archiver / detector のテストで共用)。

PyAV の PyPI wheel(libx265 と libx264 を同梱)でその場でエンコードする。zip には入らない(tests/ は同梱しない)。
    from common.tests.media import make_fmp4, moof_offsets, level
"""

from __future__ import annotations

import io
import struct
from fractions import Fraction

BASE = 1_792_814_400_979  # 断片の開始(KVS の実測と同じく .979 の位相)
START = BASE - 979  # 区間の開始(分の境界)
W, H, FPS = 192, 108, 10


def level(sec: int) -> int:
    """秒ごとに違う明るさ(デコードした絵がどの秒のものかを見分ける)。"""
    return 20 + (sec * 23) % 200


def make_fmp4(seconds: int, *, fps: int = FPS, keyint: int = FPS, gap_after: int | None = None,
              gap_s: int = 0, codec: str = "h265") -> bytes:
    """1 秒 1 キーフレームの H.265(codec="h264" なら H.264)fMP4(KVS の HLS の init + 断片と同じく moof + mdat、キーフレームごとに断片)。

    フレームの明るさは「その秒の番号」で決まる。gap_after 秒の後に gap_s 秒の空白(録画の切れ目)を入れる。
    """
    import av
    import numpy as np

    buf = io.BytesIO()
    out = av.open(buf, mode="w", format="mp4",
                  options={"movflags": "frag_keyframe+empty_moov+default_base_moof"})
    if codec == "h264":
        st = out.add_stream("libx264", rate=fps)
        st.codec_context.options = {"x264-params": f"keyint={keyint}:min-keyint={keyint}:scenecut=0:bframes=0"}
    else:
        st = out.add_stream("libx265", rate=fps)
        st.codec_context.options = {"x265-params": f"keyint={keyint}:min-keyint={keyint}:scenecut=0:bframes=0:log-level=none"}
    st.width, st.height, st.pix_fmt = W, H, "yuv420p"
    st.codec_context.time_base = Fraction(1, fps)
    pts = 0
    for i in range(seconds * fps):
        sec = i // fps
        if gap_after is not None and i == gap_after * fps:
            pts += gap_s * fps
        img = np.full((H, W, 3), level(sec), dtype=np.uint8)
        img[:, : (i % fps) * 4 + 4] = 255  # 秒の中のフレーム番号(キーフレームは左端 4 画素だけ白)
        fr = av.VideoFrame.from_ndarray(img, format="rgb24")
        fr.pts = pts
        pts += 1
        for p in st.encode(fr):
            out.mux(p)
    for p in st.encode(None):
        out.mux(p)
    out.close()
    return buf.getvalue()


def moof_offsets(data: bytes) -> list[int]:
    """トップレベルの moof の位置(= 断片の先頭のオフセット)。"""
    out, pos = [], 0
    while pos + 8 <= len(data):
        size, typ = struct.unpack(">I4s", data[pos:pos + 8])
        if size == 1:
            size = struct.unpack(">Q", data[pos + 8:pos + 16])[0]
        if typ == b"moof":
            out.append(pos)
        if size < 8:
            break
        pos += size
    return out


