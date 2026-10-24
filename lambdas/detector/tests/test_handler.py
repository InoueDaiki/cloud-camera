"""process_minute の通し(KVS は差し替え、S3 は moto)。"""

import io
import os
from datetime import datetime, timedelta, timezone

import boto3
import pyarrow.parquet as pq
import pytest
from moto import mock_aws

from common import kvs
from conftest import FIXTURES, MODELS

UTC = timezone.utc
BUCKET = "test-detections"

pytestmark = pytest.mark.skipif(
    not os.path.exists(os.path.join(MODELS, "yolox_nano.dyn.onnx")), reason="モデルがない"
)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("DETECTIONS_BUCKET", BUCKET)
    monkeypatch.setenv("STREAM_NAME_PREFIX", "cc-test-")
    monkeypatch.setenv("DETECT_MODEL", "yolox_nano.dyn")
    import handler

    handler._detector = None
    handler._s3 = None
    with mock_aws():
        s3 = boto3.client("s3", region_name="ap-northeast-1")
        s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "ap-northeast-1"})
        yield handler, s3


def _fake_kvs(monkeypatch, start, n, gaps=()):
    with open(os.path.join(FIXTURES, "person.jpg"), "rb") as f:
        jpeg = f.read()
    calls = {}

    def list_fragments(stream, s, e):
        calls["stream"] = stream
        return [object()] if n else []

    def get_images(stream, s, e, interval_ms, width, height):
        calls["size"] = (width, height)
        base = int(start.timestamp() * 1000)
        out = []
        for i in range(n + 1):  # 終わりの時刻ちょうどの 1 枚は捨てられること
            if i in gaps:
                out.append(kvs.Image(ts_ms=base + i * 1000, content=None, error="NO_MEDIA"))
            else:
                out.append(kvs.Image(ts_ms=base + i * 1000, content=jpeg, error=None))
        return out

    monkeypatch.setattr(kvs, "list_fragments", list_fragments)
    monkeypatch.setattr(kvs, "get_images", get_images)
    monkeypatch.setattr(kvs, "minute_ready", lambda *a, **k: (True, "quiet"))
    return calls


def test_process_minute_writes_rows(env, monkeypatch, capsys):
    handler, s3 = env
    start = datetime(2026, 10, 24, 10, 20, tzinfo=UTC)
    calls = _fake_kvs(monkeypatch, start, n=60, gaps={5, 6})
    res = handler.process_minute("cam-001", start, start + timedelta(minutes=1))
    assert calls["stream"] == "cc-test-cam-001" and calls["size"] == (416, 234)
    assert res.status == "DONE"
    assert res.attributes["frame_count"] == 58
    t = pq.read_table(io.BytesIO(
        s3.get_object(Bucket=BUCKET, Key="detections/dt=2026-10-24/cam-001.parquet")["Body"].read()))
    assert t.num_rows == res.attributes["detect_count"] > 0
    ts = t.column("ts").to_pylist()
    assert min(ts) == start and max(ts) == start + timedelta(seconds=59)
    assert "person" in t.column("label").to_pylist()
    assert set(t.column("model").to_pylist()) == {"yolox_nano"}
    assert '"event": "detect_minute"' in capsys.readouterr().out
    # 再実行しても重複しない
    res2 = handler.process_minute("cam-001", start, start + timedelta(minutes=1))
    t2 = pq.read_table(io.BytesIO(
        s3.get_object(Bucket=BUCKET, Key="detections/dt=2026-10-24/cam-001.parquet")["Body"].read()))
    assert t2.num_rows == t.num_rows == res2.attributes["detect_count"]


def test_process_minute_empty(env, monkeypatch):
    handler, s3 = env
    start = datetime(2026, 10, 24, 10, 20, tzinfo=UTC)
    _fake_kvs(monkeypatch, start, n=0)
    res = handler.process_minute("cam-001", start, start + timedelta(minutes=1))
    assert res.status == "EMPTY"
    assert res.attributes == {"detect_count": 0, "frame_count": 0}
    assert s3.list_objects_v2(Bucket=BUCKET).get("KeyCount", 0) == 0


def test_frame_source_can_be_swapped(env, monkeypatch):
    """FRAME_SOURCE を差し替えれば GetImages を使わずに推論できる(PIL / numpy の画像も受け付ける)。"""
    import numpy as np
    from PIL import Image

    import frame_source

    handler, s3 = env
    start = datetime(2026, 10, 24, 10, 20, tzinfo=UTC)
    monkeypatch.setattr(kvs, "minute_ready", lambda *a, **k: (True, "quiet"))
    img = Image.open(os.path.join(FIXTURES, "person.jpg")).convert("RGB")
    frames = [(start, img), (start + timedelta(seconds=1), np.asarray(img))]
    monkeypatch.setattr(handler, "FRAME_SOURCE",
                        lambda c, s, e: frame_source.FrameBatch(frames, {"source": "test"}))
    res = handler.process_minute("cam-001", start, start + timedelta(minutes=1))
    assert res.status == "DONE" and res.attributes["frame_count"] == 2
    assert res.attributes["detect_count"] > 0


def test_not_ready_raises(env, monkeypatch):
    handler, s3 = env
    from common import NotReady

    start = datetime(2026, 10, 24, 10, 20, tzinfo=UTC)
    _fake_kvs(monkeypatch, start, n=60)
    monkeypatch.setattr(kvs, "minute_ready", lambda *a, **k: (False, "producer_behind"))
    with pytest.raises(NotReady):
        handler.process_minute("cam-001", start, start + timedelta(minutes=1))


def test_resource_not_found_falls_back_to_segments(env, monkeypatch):
    """録画が区間の途中(10:20:30.4〜)から始まると GetImages が ResourceNotFound になる → 録画範囲で取り直す。"""
    handler, s3 = env
    start = datetime(2026, 10, 24, 10, 20, tzinfo=UTC)
    base = int(start.timestamp() * 1000)
    with open(os.path.join(FIXTURES, "person.jpg"), "rb") as f:
        jpeg = f.read()
    frags = [kvs.Fragment(number=str(i), producer_ms=base + 30_400 + i * 1000, server_ms=0, length_ms=1000, size=1)
             for i in range(29)]
    calls = []

    class ResourceNotFoundException(Exception):
        pass

    def get_images(stream, s, e, interval_ms, width, height):
        calls.append((int(s.timestamp() * 1000) - base, int(e.timestamp() * 1000) - base))
        if len(calls) == 1:
            raise ResourceNotFoundException("no fragments near start")
        a, b = calls[-1]
        return [kvs.Image(ts_ms=base + t, content=jpeg, error=None) for t in range(a, b + 1, 1000)]

    monkeypatch.setattr(kvs, "minute_ready", lambda *a, **k: (True, "quiet"))
    monkeypatch.setattr(kvs, "list_fragments", lambda *a: frags)
    monkeypatch.setattr(kvs, "get_images", get_images)
    res = handler.process_minute("cam-001", start, start + timedelta(minutes=1))
    # 2 回目は録画の始まりを秒に切り上げた 10:20:31 から
    assert calls == [(0, 60_000), (31_000, 59_400)]
    assert res.status == "DONE" and res.attributes["frame_count"] == 29
