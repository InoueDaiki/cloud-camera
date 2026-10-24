"""clip 版のフレーム源(S3 の clip.mp4 のキーフレームをデコード)と、待ち合わせ、FAILED のときの予備の経路。

DynamoDB と S3 は moto。clip.mp4 は PyAV(PyPI の wheel の libx265)でその場で作る H.265 の fMP4。
"""

import io
import os
from datetime import datetime, timedelta, timezone
from fractions import Fraction

import boto3
import numpy as np
import pyarrow.parquet as pq
import pytest
from moto import mock_aws
from PIL import Image

from common import NotReady, iso, kvs
from common.tests.media import BASE, START, level, make_fmp4, moof_offsets
from conftest import FIXTURES, MODELS

av = pytest.importorskip("av")

UTC = timezone.utc
MEDIA = "test-media"
DETECTIONS = "test-detections"
TABLE = "test-state"
CAM = "cam-001"
T0 = datetime.fromtimestamp(START / 1000, UTC)  # 区間の開始(分の境界)
SK = iso(T0)  # 2026-10-24T04:00:00Z
KEY = "cam-001/2026/10/24/04/00/clip.mp4"


@pytest.fixture
def aws(monkeypatch):
    monkeypatch.setenv("STATE_TABLE", TABLE)
    monkeypatch.setenv("MEDIA_BUCKET", MEDIA)
    monkeypatch.setenv("DETECTIONS_BUCKET", DETECTIONS)
    monkeypatch.setenv("STREAM_NAME_PREFIX", "cc-test-")
    monkeypatch.delenv("DETECT_IMAGE_HEIGHT", raising=False)
    monkeypatch.delenv("UPSTREAM_MAX_WAIT_SECONDS", raising=False)
    import frame_source

    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="ap-northeast-1")
        table = ddb.create_table(
            TableName=TABLE, BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "camera_id", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "camera_id", "AttributeType": "S"},
                                  {"AttributeName": "sk", "AttributeType": "S"}],
        )
        s3 = boto3.client("s3", region_name="ap-northeast-1")
        for b in (MEDIA, DETECTIONS):
            s3.create_bucket(Bucket=b, CreateBucketConfiguration={"LocationConstraint": "ap-northeast-1"})
        monkeypatch.setattr(frame_source, "_table", None)
        monkeypatch.setattr(frame_source, "_s3", None)
        # 予備の経路(GetImages)は呼ばれたことだけ記録する
        calls = []

        def fallback(camera_id, start, end):
            calls.append((camera_id, start, end))
            return frame_source.FrameBatch([(start, b"jpeg")], {"source": "getimages"})

        monkeypatch.setattr(frame_source, "FALLBACK", fallback)
        yield frame_source, table, s3, calls


def put_clip(table, s3, data: bytes, producer: list[int], *, segments=None, status="DONE"):
    offs = moof_offsets(data)
    ends = offs[1:] + [len(data)]
    parts = [[o, e - o, 1000] for o, e in zip(offs, ends)]
    s3.put_object(Bucket=MEDIA, Key=KEY, Body=data)
    table.put_item(Item={
        "camera_id": CAM, "sk": SK, "video_status": status,
        "files": {"clip": KEY}, "hls": {"init_length": offs[0], "parts": parts},
        "segments": segments or [[producer[0], producer[-1] + 1000]],
    })


def now_after(seconds):
    return lambda: T0 + timedelta(minutes=1, seconds=seconds)


def test_clip_decodes_keyframes(aws):
    fs, table, s3, calls = aws
    data = make_fmp4(5)
    put_clip(table, s3, data, [BASE + i * 1000 for i in range(5)])
    b = fs.clipfile(CAM, T0, T0 + timedelta(minutes=1), now=now_after(60))
    # キーフレームの実際の時刻(区間の開始 + 0.979 秒の位相)。GetImages のように秒ちょうどではない
    assert [int(ts.timestamp() * 1000) - START for ts, _ in b.frames] == [979, 1979, 2979, 3979, 4979]
    img = b.frames[2][1]
    assert isinstance(img, Image.Image) and img.size == (640, 360)  # 幅 640、縦横比を保つ
    assert abs(float(np.asarray(img)[:, 400:].mean()) - level(2)) < 10
    assert b.stats["source"] == "clip" and b.stats["decoded"] == 5 and b.stats["image_size"] == [640, 360]
    assert b.stats["clip_bytes"] == len(data)
    assert calls == []  # GetImages は呼ばない


def test_clip_uses_segments_for_gaps(aws):
    """録画の切れ目: segments の 2 つ目の開始から時刻を当てる(anchors_from_hls)。"""
    fs, table, s3, _ = aws
    data = make_fmp4(4)
    producer = [BASE, BASE + 1000, BASE + 30_000, BASE + 31_000]
    put_clip(table, s3, data, producer, segments=[[BASE, BASE + 2000], [BASE + 30_000, BASE + 32_000]])
    b = fs.clipfile(CAM, T0, T0 + timedelta(minutes=1), now=now_after(60))
    assert [int(ts.timestamp() * 1000) - BASE for ts, _ in b.frames] == [0, 1000, 30_000, 31_000]


def test_clip_sections_after_codec_switch(aws):
    """区間の途中で H.265 → H.264 に変わった区間(hls.sections)は、部分ごとの clip をデコードする(docs/codec.md)。"""
    fs, table, s3, calls = aws
    a, b = make_fmp4(2), make_fmp4(2, codec="h264")
    secs = []
    for name, data in (("clip.mp4", a), ("clip.1.mp4", b)):
        offs = moof_offsets(data)
        ends = offs[1:] + [len(data)]
        key = KEY.replace("clip.mp4", name)
        s3.put_object(Bucket=MEDIA, Key=key, Body=data)
        secs.append({"clip": key, "init_length": offs[0], "parts": [[o, e - o, 1000] for o, e in zip(offs, ends)]})
    table.put_item(Item={
        "camera_id": CAM, "sk": SK, "video_status": "DONE",
        "files": {"clip": KEY, "clips": [s["clip"] for s in secs]},
        "hls": {"init_length": secs[0]["init_length"], "parts": secs[0]["parts"], "sections": secs},
        "segments": [[BASE, BASE + 2000], [BASE + 10_000, BASE + 12_000]],
    })
    r = fs.clipfile(CAM, T0, T0 + timedelta(minutes=1), now=now_after(60))
    assert [int(ts.timestamp() * 1000) - BASE for ts, _ in r.frames] == [0, 1000, 10_000, 11_000]
    assert r.stats["sections"] == 2 and r.stats["clip_bytes"] == len(a) + len(b)
    assert abs(float(np.asarray(r.frames[3][1])[:, 400:].mean()) - level(1)) < 10
    assert calls == []


def test_clip_height_from_env(aws, monkeypatch):
    fs, table, s3, _ = aws
    monkeypatch.setenv("DETECT_INPUT_WIDTH", "416")
    monkeypatch.setenv("DETECT_IMAGE_HEIGHT", "256")
    put_clip(table, s3, make_fmp4(2), [BASE, BASE + 1000])
    b = fs.clipfile(CAM, T0, T0 + timedelta(minutes=1), now=now_after(60))
    assert b.frames[0][1].size == (416, 256)


def test_video_not_processed_yet_is_not_ready(aws):
    fs, table, s3, calls = aws
    with pytest.raises(NotReady):
        fs.clipfile(CAM, T0, T0 + timedelta(minutes=1), now=now_after(120))
    assert calls == []


def test_video_missing_too_long_falls_back(aws, monkeypatch):
    fs, table, s3, calls = aws
    b = fs.clipfile(CAM, T0, T0 + timedelta(minutes=1), now=now_after(1800))
    assert calls and b.stats["fallback"] == "video_missing"
    monkeypatch.setenv("UPSTREAM_MAX_WAIT_SECONDS", "100")
    calls.clear()
    fs.clipfile(CAM, T0, T0 + timedelta(minutes=1), now=now_after(100))
    assert calls


def test_video_empty_returns_no_frames(aws):
    fs, table, s3, calls = aws
    table.put_item(Item={"camera_id": CAM, "sk": SK, "video_status": "EMPTY", "segments": []})
    b = fs.clipfile(CAM, T0, T0 + timedelta(minutes=1), now=now_after(60))
    assert b.frames == [] and b.stats["video_status"] == "EMPTY" and calls == []


def test_video_failed_uses_getimages(aws):
    fs, table, s3, calls = aws
    table.put_item(Item={"camera_id": CAM, "sk": SK, "video_status": "FAILED",
                         "video_error": "max_attempts: boom"})
    b = fs.clipfile(CAM, T0, T0 + timedelta(minutes=1), now=now_after(60))
    assert calls == [(CAM, T0, T0 + timedelta(minutes=1))]
    assert b.stats["fallback"] == "video_failed" and b.stats["source"] == "getimages" and len(b.frames) == 1


def test_broken_clip_uses_getimages(aws):
    fs, table, s3, calls = aws
    put_clip(table, s3, make_fmp4(2), [BASE, BASE + 1000])
    s3.put_object(Bucket=MEDIA, Key=KEY, Body=b"broken" * 100)
    b = fs.clipfile(CAM, T0, T0 + timedelta(minutes=1), now=now_after(60))
    assert calls and b.stats["fallback"] == "decode_error"


def test_select():
    import frame_source

    assert frame_source.select(None) is frame_source.clipfile
    assert frame_source.select("GetImages") is frame_source.getimages
    with pytest.raises(ValueError):
        frame_source.select("hls")


def _person_clip(seconds: int) -> bytes:
    """person.jpg を 1280×720 に置いた H.265 の fMP4(1 秒 1 キーフレーム、断片はキーフレームごと)。"""
    src = Image.open(os.path.join(FIXTURES, "person.jpg")).convert("RGB")
    canvas = Image.new("RGB", (1280, 720), (90, 90, 90))
    canvas.paste(src.resize((round(src.width * 720 / src.height), 720)), (0, 0))
    arr = np.asarray(canvas)
    buf = io.BytesIO()
    out = av.open(buf, mode="w", format="mp4", options={"movflags": "frag_keyframe+empty_moov+default_base_moof"})
    st = out.add_stream("libx265", rate=5)
    st.width, st.height, st.pix_fmt = 1280, 720, "yuv420p"
    st.codec_context.options = {"x265-params": "keyint=5:min-keyint=5:scenecut=0:bframes=0:log-level=none"}
    st.codec_context.time_base = Fraction(1, 5)
    for i in range(seconds * 5):
        fr = av.VideoFrame.from_ndarray(arr, format="rgb24")
        fr.pts = i
        for p in st.encode(fr):
            out.mux(p)
    for p in st.encode(None):
        out.mux(p)
    out.close()
    return buf.getvalue()


@pytest.mark.skipif(not os.path.exists(os.path.join(MODELS, "yolox_nano.dyn.onnx")), reason="モデルがない")
def test_process_minute_from_clip_detects_person(aws, monkeypatch):
    """handler の通し: clip.mp4 → キーフレーム → 推論 → Parquet。GetImages は呼ばない。ts はキーフレームの時刻。"""
    fs, table, s3, calls = aws
    monkeypatch.setenv("DETECT_MODEL", "yolox_nano.dyn")
    import handler

    monkeypatch.setattr(handler, "_detector", None)
    monkeypatch.setattr(handler, "_s3", None)
    monkeypatch.setattr(handler, "FRAME_SOURCE", fs.clipfile)

    def no_getimages(*a, **k):
        raise AssertionError("GetImages was called")

    monkeypatch.setattr(kvs, "get_images", no_getimages)
    put_clip(table, s3, _person_clip(3), [BASE, BASE + 1000, BASE + 2000])
    res = handler.process_minute(CAM, T0, T0 + timedelta(minutes=1))
    assert res.status == "DONE" and res.attributes["frame_count"] == 3
    t = pq.read_table(io.BytesIO(s3.get_object(Bucket=DETECTIONS, Key="detections/dt=2026-10-24/cam-001.parquet")["Body"].read()))
    assert "person" in t.column("label").to_pylist()
    ts = sorted({int(x.timestamp() * 1000) for x in t.column("ts").to_pylist()})
    assert ts == [BASE, BASE + 1000, BASE + 2000]
    assert calls == []


def test_lambda_handler_waits_for_video(monkeypatch):
    """clip のときは run_pipeline に upstream_kind="VIDEO" を渡す(動画の退避の処理済み位置を越えない)。"""
    import frame_source
    import handler

    got = {}
    monkeypatch.setattr(handler, "run_pipeline", lambda *a, **k: got.update(k) or {})
    monkeypatch.setattr(handler, "FRAME_SOURCE", frame_source.clipfile)
    handler.lambda_handler({"camera_id": CAM}, None)
    assert got["upstream_kind"] == "VIDEO"
    monkeypatch.setattr(handler, "FRAME_SOURCE", frame_source.getimages)
    handler.lambda_handler({"camera_id": CAM}, None)
    assert got["upstream_kind"] is None
