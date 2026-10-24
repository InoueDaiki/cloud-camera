"""Parquet 追記の冪等性、0 時またぎ、条件付き PUT の競合。"""

import io
from datetime import datetime, timedelta, timezone

import boto3
import pyarrow.parquet as pq
import pytest
from moto import mock_aws

import parquet_store
from parquet_store import ConflictError, object_key, replace_minute

BUCKET = "test-detections"
UTC = timezone.utc


@pytest.fixture
def s3():
    with mock_aws():
        c = boto3.client("s3", region_name="ap-northeast-1")
        c.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "ap-northeast-1"})
        yield c


def rows_for(start, n_frames=3, per_frame=2, label="person", cam="cam-001"):
    return [
        {"camera_id": cam, "ts": start + timedelta(seconds=i), "label": label,
         "confidence": 0.9, "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4, "model": "yolox_tiny"}
        for i in range(n_frames) for _ in range(per_frame)
    ]


def read(s3, key):
    return pq.read_table(io.BytesIO(s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()))


def test_key_uses_interval_start_date():
    assert object_key("cam-001", datetime(2026, 10, 24, 23, 59, tzinfo=UTC)) == \
        "detections/dt=2026-10-24/cam-001.parquet"


def test_schema_matches_contract(s3):
    start = datetime(2026, 10, 24, 10, 20, tzinfo=UTC)
    replace_minute(s3, BUCKET, "cam-001", start, start + timedelta(minutes=1), rows_for(start))
    t = read(s3, object_key("cam-001", start))
    assert t.schema.names == ["camera_id", "ts", "label", "confidence", "x", "y", "w", "h", "model"]
    assert str(t.schema.field("ts").type) == "timestamp[ms, tz=UTC]"
    assert str(t.schema.field("confidence").type) == "float"


def test_rerun_same_minute_is_idempotent(s3):
    start = datetime(2026, 10, 24, 10, 20, tzinfo=UTC)
    end = start + timedelta(minutes=1)
    key = object_key("cam-001", start)
    s1 = replace_minute(s3, BUCKET, "cam-001", start, end, rows_for(start))
    assert s1.put and s1.rows_added == 6
    s2 = replace_minute(s3, BUCKET, "cam-001", start, end, rows_for(start))
    assert s2.rows_removed == 6 and s2.rows_added == 6
    assert read(s3, key).num_rows == 6
    # 次の区間を足しても前の区間はそのまま
    nxt = end
    replace_minute(s3, BUCKET, "cam-001", nxt, nxt + timedelta(minutes=1), rows_for(nxt, n_frames=2))
    t = read(s3, key)
    assert t.num_rows == 10
    # 前の区間をやり直して検出数が変わったら、置き換わる
    replace_minute(s3, BUCKET, "cam-001", start, end, rows_for(start, n_frames=1, per_frame=1))
    t = read(s3, key)
    assert t.num_rows == 5
    ts = t.column("ts").to_pylist()
    assert ts == sorted(ts)


def test_rerun_with_no_detections_removes_old_rows(s3):
    start = datetime(2026, 10, 24, 10, 20, tzinfo=UTC)
    end = start + timedelta(minutes=1)
    replace_minute(s3, BUCKET, "cam-001", start, end, rows_for(start))
    st = replace_minute(s3, BUCKET, "cam-001", start, end, [])
    assert st.put and st.rows_removed == 6
    assert read(s3, object_key("cam-001", start)).num_rows == 0


def test_empty_minute_without_file_skips_put(s3):
    start = datetime(2026, 10, 24, 10, 20, tzinfo=UTC)
    st = replace_minute(s3, BUCKET, "cam-001", start, start + timedelta(minutes=1), [])
    assert not st.put
    assert s3.list_objects_v2(Bucket=BUCKET).get("KeyCount", 0) == 0


def test_midnight_crossing_goes_to_each_day(s3):
    last = datetime(2026, 10, 24, 23, 59, tzinfo=UTC)
    first = datetime(2026, 10, 25, 0, 0, tzinfo=UTC)
    replace_minute(s3, BUCKET, "cam-001", last, first, rows_for(last + timedelta(seconds=57)))
    replace_minute(s3, BUCKET, "cam-001", first, first + timedelta(minutes=1), rows_for(first, n_frames=1))
    d1 = read(s3, "detections/dt=2026-10-24/cam-001.parquet")
    d2 = read(s3, "detections/dt=2026-10-25/cam-001.parquet")
    # 23:59:57〜23:59:59 の 3 フレームは 24 日、00:00:00 は 25 日
    assert d1.num_rows == 6 and d2.num_rows == 2
    assert max(d1.column("ts").to_pylist()) < first
    # 23:59 をやり直しても 25 日のファイルは変わらない
    replace_minute(s3, BUCKET, "cam-001", last, first, rows_for(last, n_frames=1))
    assert read(s3, "detections/dt=2026-10-24/cam-001.parquet").num_rows == 2
    assert read(s3, "detections/dt=2026-10-25/cam-001.parquet").num_rows == 2


def test_cameras_do_not_interfere(s3):
    start = datetime(2026, 10, 24, 10, 20, tzinfo=UTC)
    end = start + timedelta(minutes=1)
    replace_minute(s3, BUCKET, "cam-001", start, end, rows_for(start))
    replace_minute(s3, BUCKET, "cam-002", start, end, rows_for(start, cam="cam-002", n_frames=1))
    assert read(s3, object_key("cam-001", start)).num_rows == 6
    assert read(s3, object_key("cam-002", start)).num_rows == 2


class RacingS3:
    """get_object の直後に、別の書き手が同じキーを書き換える(競合を起こす)ラッパー。"""

    def __init__(self, s3, races: int, other_rows):
        self._s3 = s3
        self.races = races
        self.other_rows = other_rows
        self.exceptions = s3.exceptions
        self.puts = []

    def get_object(self, **kw):
        resp = self._s3.get_object(**kw)
        if self.races > 0:
            self.races -= 1
            # 割り込み: 別区間の行を足して書き換える(ETag が変わる)
            start, rows = self.other_rows.pop(0)
            replace_minute(self._s3, kw["Bucket"], "cam-001", start, start + timedelta(minutes=1), rows)
        return resp

    def put_object(self, **kw):
        self.puts.append({k: v for k, v in kw.items() if k.startswith("If")})
        return self._s3.put_object(**kw)


def test_conditional_put_conflict_rereads_and_keeps_both(s3, monkeypatch):
    monkeypatch.setattr(parquet_store.time, "sleep", lambda s: None)
    base = datetime(2026, 10, 24, 10, 0, tzinfo=UTC)
    replace_minute(s3, BUCKET, "cam-001", base, base + timedelta(minutes=1), rows_for(base, n_frames=1))
    other = base + timedelta(minutes=5)
    racing = RacingS3(s3, races=1, other_rows=[(other, rows_for(other, n_frames=2))])
    mine = base + timedelta(minutes=1)
    st = replace_minute(racing, BUCKET, "cam-001", mine, mine + timedelta(minutes=1), rows_for(mine))
    assert st.attempts == 2
    # 1 回目は古い ETag の If-Match で弾かれ、2 回目は新しい ETag で成功
    assert len(racing.puts) == 2 and racing.puts[0]["IfMatch"] != racing.puts[1]["IfMatch"]
    t = read(s3, object_key("cam-001", base))
    assert t.num_rows == 2 + 4 + 6  # 誰の書き込みも失われない


def test_conditional_create_conflict(s3, monkeypatch):
    """新規作成(If-None-Match: *)の間に別の書き手がファイルを作った場合。"""
    monkeypatch.setattr(parquet_store.time, "sleep", lambda s: None)
    start = datetime(2026, 10, 24, 10, 0, tzinfo=UTC)
    key = object_key("cam-001", start)

    class CreateRace(RacingS3):
        def get_object(self, **kw):
            if self.races > 0:
                self.races -= 1
                other = start + timedelta(minutes=3)
                replace_minute(self._s3, BUCKET, "cam-001", other, other + timedelta(minutes=1),
                               rows_for(other, n_frames=1))
                raise self._s3.exceptions.NoSuchKey({"Error": {"Code": "NoSuchKey"}}, "GetObject")
            return self._s3.get_object(**kw)

    racing = CreateRace(s3, races=1, other_rows=[])
    st = replace_minute(racing, BUCKET, "cam-001", start, start + timedelta(minutes=1), rows_for(start))
    assert racing.puts[0] == {"IfNoneMatch": "*"}
    assert st.attempts == 2
    assert read(s3, key).num_rows == 8


def test_conflict_gives_up_after_max_attempts(s3, monkeypatch):
    monkeypatch.setattr(parquet_store.time, "sleep", lambda s: None)
    base = datetime(2026, 10, 24, 10, 0, tzinfo=UTC)
    replace_minute(s3, BUCKET, "cam-001", base, base + timedelta(minutes=1), rows_for(base, n_frames=1))
    others = [(base + timedelta(minutes=10 + i), rows_for(base + timedelta(minutes=10 + i), n_frames=1))
              for i in range(3)]
    racing = RacingS3(s3, races=3, other_rows=others)
    with pytest.raises(ConflictError):
        replace_minute(racing, BUCKET, "cam-001", base, base + timedelta(minutes=1), [], max_attempts=3)
