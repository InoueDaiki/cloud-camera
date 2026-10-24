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
