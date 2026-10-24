"""common.frames(fMP4 の H.265 キーフレームのデコード)のテスト。

PyAV と Pillow、numpy が要る(common の既定の依存には入れていない)。実行方法は FRAMES.md:
  cd lambdas/common && mise exec -- uv run --with av --with pillow --with numpy pytest tests/test_frames.py
テスト用の H.265 fMP4 は PyAV(wheel に同梱の libx265)でその場で作る。
"""

from __future__ import annotations

import io
import struct
from fractions import Fraction

import pytest

av = pytest.importorskip("av")
np = pytest.importorskip("numpy")
PIL = pytest.importorskip("PIL.Image")

from common import frames  # noqa: E402

BASE = 1_792_814_400_979  # 断片の開始(KVS の実測と同じく .979 の位相)
START = BASE - 979  # 区間の開始(分の境界)
W, H, FPS = 192, 108, 10


def _level(sec: int) -> int:
    """秒ごとに違う明るさ(デコードした絵がどの秒のものかを見分ける)。"""
    return 20 + (sec * 23) % 200


def make_fmp4(seconds: int, *, fps: int = FPS, keyint: int = FPS, gap_after: int | None = None,
              gap_s: int = 0) -> bytes:
    """1 秒 1 キーフレームの H.265 fMP4(KVS の HLS の init + 断片と同じく moof + mdat、キーフレームごとに断片)。

    フレームの明るさは「その秒の番号」で決まる。gap_after 秒の後に gap_s 秒の空白(録画の切れ目)を入れる。
    """
    buf = io.BytesIO()
    out = av.open(buf, mode="w", format="mp4",
                  options={"movflags": "frag_keyframe+empty_moov+default_base_moof"})
    st = out.add_stream("libx265", rate=fps)
    st.width, st.height, st.pix_fmt = W, H, "yuv420p"
    st.codec_context.options = {"x265-params": f"keyint={keyint}:min-keyint={keyint}:scenecut=0:bframes=0:log-level=none"}
    st.codec_context.time_base = Fraction(1, fps)
    pts = 0
    for i in range(seconds * fps):
        sec = i // fps
        if gap_after is not None and i == gap_after * fps:
            pts += gap_s * fps
        img = np.full((H, W, 3), _level(sec), dtype=np.uint8)
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


@pytest.fixture(scope="module")
def clip5():
    return make_fmp4(5)


def _mean(img) -> float:
    a = np.asarray(img)
    return float(a[:, a.shape[1] // 2:].mean())  # 右半分(白い印を避ける)


def test_decodes_one_keyframe_per_second(clip5):
    r = frames.decode_keyframes(clip5, start_ms=START, end_ms=START + 60_000, base_ms=BASE,
                                sizes={"sprite": (160, 90), "detect": (416, None)})
    assert [f.ts_ms - BASE for f in r.frames] == [0, 1000, 2000, 3000, 4000]
    assert [f.slot for f in r.frames] == [0, 1, 2, 3, 4]
    assert r.stats["packets"] == 5 * FPS
    assert r.stats["keyframes"] == 5
    assert r.stats["decoded"] == 5  # 非キーフレームはデコーダに渡していない
    assert r.stats["codec"] == "hevc"
    assert (r.stats["width"], r.stats["height"]) == (W, H)
    for f in r.frames:
        assert f.images["sprite"].size == (160, 90)
        assert f.images["detect"].size == (416, 234)  # 高さ None は縦横比を保つ(偶数)
        sec = f.slot
        assert abs(_mean(f.images["detect"]) - _level(sec)) < 8
        # キーフレーム(秒の頭)の絵であること: 左端の白い印は 4 画素ぶん(416 / 192 倍で約 9 画素)だけ
        row = np.asarray(f.images["detect"])[117]
        assert row[2].mean() > 200 and abs(row[60].mean() - _level(sec)) < 12


def test_range_is_half_open_and_outside_frames_are_not_decoded(clip5):
    # キーフレームは START + 0.979, 1.979, 2.979, … 秒。終わりちょうど(2.979 秒)は含まない
    r = frames.decode_keyframes(clip5, start_ms=START + 1000, end_ms=START + 2979, base_ms=BASE,
                                sizes={"s": (64, 36)})
    assert [f.ts_ms - START for f in r.frames] == [1979]
    assert [f.slot for f in r.frames] == [0]
    assert r.stats["skipped_outside"] == 4
    assert r.stats["decoded"] == 1


def test_interval_thins_out_and_skips_decoding(clip5):
    r = frames.decode_keyframes(clip5, start_ms=START, end_ms=START + 6000, base_ms=BASE, interval_ms=2000,
                                sizes={"s": (32, 18)})
    # 枠 [0,2s) には BASE+0(0.979s)と BASE+1000(1.979s)が入る → 最初の 1 枚だけ
    assert [f.ts_ms - START for f in r.frames] == [979, 2979, 4979]
    assert r.stats["skipped_filled"] == 2
    assert r.stats["decoded"] == 3


def test_two_keyframes_per_second_gives_one_per_slot():
    data = make_fmp4(3, keyint=FPS // 2)
    r = frames.decode_keyframes(data, start_ms=BASE, end_ms=BASE + 3000, base_ms=BASE, sizes={"s": (32, 18)})
    assert r.stats["keyframes"] == 6
    assert [f.ts_ms - BASE for f in r.frames] == [0, 1000, 2000]
    assert r.stats["skipped_filled"] == 3


def test_gap_in_timeline_with_base_ms():
    """録画の切れ目: fMP4 の時間軸の空白(tfdt)がそのままプロデューサー時刻の空白になる。"""
    data = make_fmp4(4, gap_after=2, gap_s=3)
    r = frames.decode_keyframes(data, start_ms=START, end_ms=START + 60_000, base_ms=BASE, sizes={"s": (32, 18)})
    assert [f.ts_ms - BASE for f in r.frames] == [0, 1000, 5000, 6000]
    assert [f.slot for f in r.frames] == [0, 1, 5, 6]
    assert abs(_mean(r.frames[2].images["s"]) - _level(2)) < 8


def test_anchors_override_file_timeline(clip5):
    """断片ごとのプロデューサー時刻(ListFragments)で時刻を決める。fMP4 の時間軸と食い違っても anchors が正。"""
    offs = moof_offsets(clip5)
    assert len(offs) == 5
    producer = [BASE, BASE + 1000, BASE + 10_000, BASE + 11_000, BASE + 70_000]  # 2 本目の後に切れ目、最後は区間外
    anchors = frames.anchors_from_fragments([[o, 0, 1000] for o in offs], producer)
    r = frames.decode_keyframes(clip5, start_ms=START, end_ms=START + 60_000, anchors=anchors, sizes={"s": (32, 18)})
    assert [f.ts_ms for f in r.frames] == producer[:4]
    assert [f.slot for f in r.frames] == [0, 1, 10, 11]
    assert r.stats["skipped_outside"] == 1


def test_file_path_and_numpy_output(tmp_path, clip5):
    p = tmp_path / "clip.mp4"
    p.write_bytes(clip5)
    r = frames.decode_keyframes(str(p), start_ms=START, end_ms=START + 60_000, base_ms=BASE,
                                sizes={"detect": (416, 234)}, output="numpy")
    assert len(r.frames) == 5
    a = r.frames[0].images["detect"]
    assert a.shape == (234, 416, 3) and a.dtype == np.uint8


def test_full_size_and_threads(clip5):
    for threads in (0, 1, 2):
        r = frames.decode_keyframes(clip5, start_ms=START, end_ms=START + 60_000, base_ms=BASE, threads=threads)
        assert len(r.frames) == 5
        assert r.frames[0].images["full"].size == (W, H)


def test_requires_time_reference(clip5):
    with pytest.raises(ValueError):
        frames.decode_keyframes(clip5, start_ms=START, end_ms=START + 60_000)


def test_anchors_from_hls_handles_gaps():
    # 3 本続けて(実効長 1000ms)、切れ目の後に 2 本
    parts = [[800, 10, 1000], [810, 10, 1000], [820, 10, 1000], [830, 10, 1000], [840, 10, 1050]]
    segments = [[BASE, BASE + 3000], [BASE + 20_000, BASE + 22_050]]
    assert frames.anchors_from_hls(parts, segments) == [
        (800, BASE), (810, BASE + 1000), (820, BASE + 2000), (830, BASE + 20_000), (840, BASE + 21_000)]
    assert frames.anchors_from_hls([], []) == []
    with pytest.raises(ValueError):
        frames.anchors_from_fragments([[0, 1, 1]], [])


def test_build_sprite(clip5):
    r = frames.decode_keyframes(clip5, start_ms=START, end_ms=START + 60_000, base_ms=BASE,
                                sizes={"sprite": (160, 90)})
    jpeg, n = frames.build_sprite(r.frames)
    assert n == 5
    sheet = PIL.open(io.BytesIO(jpeg))
    assert sheet.size == (1600, 540)
    a = np.asarray(sheet.convert("RGB")).astype(int)
    assert abs(a[45, 100:150].mean() - _level(0)) < 10  # 左上(コマ 0)
    assert abs(a[45, 160 * 3 + 100: 160 * 3 + 150].mean() - _level(3)) < 10
    assert a[45 + 90 * 5, 160 * 9 + 80].max() < 10  # コマ 59 は黒
