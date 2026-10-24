import json
from datetime import datetime, timedelta, timezone

import pytest
from conftest import event, put_minute

import handler
from app import aws
from app.timeutil import iso, parse_ts, to_ms


def ms(s):
    return to_ms(parse_ts(s))


class FakeKvs:
    """ListFragments のスタブ。NextToken でページを分けて返す。"""

    def __init__(self, fragments, page_size=2):
        self.fragments = fragments  # [(開始 ISO, 長さ ms)]
        self.page_size = page_size
        self.calls = []

    def list_fragments(self, **kw):
        self.calls.append(kw)
        if "NextToken" in kw:
            start = int(kw["NextToken"])
            sel = self.calls[0]["FragmentSelector"]["TimestampRange"]
        else:
            start = 0
            sel = kw["FragmentSelector"]["TimestampRange"]
        frs = [(parse_ts(s), l) for s, l in self.fragments
               if sel["StartTimestamp"] <= parse_ts(s) < sel["EndTimestamp"]]
        page = frs[start:start + self.page_size]
        resp = {"Fragments": [{"ProducerTimestamp": t, "FragmentLengthInMilliseconds": l,
                               "FragmentNumber": str(i)} for i, (t, l) in enumerate(page)]}
        if start + self.page_size < len(frs):
            resp["NextToken"] = str(start + self.page_size)
        return resp


@pytest.fixture
def kvs_stub(monkeypatch):
    holder = {"stub": FakeKvs([])}
    monkeypatch.setattr(aws, "kvs_archived_media", lambda stream, api: holder["stub"])
    return holder


def cursor(ddb, cam, kind, pu):
    ddb.put_item(Item={"camera_id": cam, "sk": f"CURSOR#{kind}", "processed_until": pu,
                       "lease_until": "2000-01-01T00:00:00Z"})


def timeline(query):
    r = handler.lambda_handler(event("GET", "/api/cameras/cam-001/timeline", query), None)
    assert r["statusCode"] == 200, r["body"]
    return json.loads(r["body"])


def test_boundary_s3_then_kvs(ddb, kvs_stub):
    cursor(ddb, "cam-001", "VIDEO", "2026-10-24T10:23:00Z")
    cursor(ddb, "cam-001", "DETECT", "2026-10-24T10:22:00Z")
    put_minute(ddb, "cam-001", "2026-10-24T10:20:00Z", "DONE",
               segments=[(ms("2026-10-24T10:20:00Z"), ms("2026-10-24T10:20:41.400Z")),
                         (ms("2026-10-24T10:20:52Z"), ms("2026-10-24T10:21:00Z"))],
               files={"sprite": "cam-001/2026/10/24/10/20/sprite.jpg"})
    put_minute(ddb, "cam-001", "2026-10-24T10:21:00Z", "DONE",
               segments=[(ms("2026-10-24T10:21:00Z"), ms("2026-10-24T10:21:30Z"))])
    put_minute(ddb, "cam-001", "2026-10-24T10:22:00Z", "FAILED")
    # 処理済み位置より後の区間の件(処理中など)は無視して KVS を見る
    put_minute(ddb, "cam-001", "2026-10-24T10:23:00Z", "DONE",
               segments=[(ms("2026-10-24T10:23:00Z"), ms("2026-10-24T10:24:00Z"))])
    kvs_stub["stub"] = FakeKvs([
        ("2026-10-24T10:22:58Z", 2000),  # 処理済み位置より前: 取らない
        ("2026-10-24T10:23:00Z", 2000), ("2026-10-24T10:23:02Z", 2000),
        ("2026-10-24T10:23:04.300Z", 2000),  # 隙間 300ms はつなげる
        ("2026-10-24T10:23:10Z", 2000),      # 隙間 4s は分ける
    ])
    t = timeline({"from": "2026-10-24T10:20:00Z", "to": "2026-10-24T10:24:00Z"})
    assert t["video_processed_until"] == "2026-10-24T10:23:00Z"
    assert t["detect_processed_until"] == "2026-10-24T10:22:00Z"
    assert t["recorded"] == [
        {"start": "2026-10-24T10:20:00Z", "end": "2026-10-24T10:20:41.400Z", "source": "s3"},
        {"start": "2026-10-24T10:20:52Z", "end": "2026-10-24T10:21:30Z", "source": "s3"},  # 分またぎでつながる
        {"start": "2026-10-24T10:23:00Z", "end": "2026-10-24T10:23:06.300Z", "source": "kvs"},
        {"start": "2026-10-24T10:23:10Z", "end": "2026-10-24T10:23:12Z", "source": "kvs"},
    ]
    assert t["failed"] == [{"start": "2026-10-24T10:22:00Z", "end": "2026-10-24T10:23:00Z"}]
    assert t["minutes"] == [
        {"start": "2026-10-24T10:20:00Z", "sprite": "/media/cam-001/2026/10/24/10/20/sprite.jpg"},
        {"start": "2026-10-24T10:21:00Z", "sprite": "/media/cam-001/2026/10/24/10/21/sprite.jpg"},
    ]
    # KVS は処理済み位置から to まで、ページ送りしている
    first = kvs_stub["stub"].calls[0]["FragmentSelector"]["TimestampRange"]
    assert iso(first["StartTimestamp"]) == "2026-10-24T10:23:00Z"
    assert iso(first["EndTimestamp"]) == "2026-10-24T10:24:00Z"
    assert len(kvs_stub["stub"].calls) == 2


def test_range_clipped_to_from_to(ddb, kvs_stub):
    cursor(ddb, "cam-001", "VIDEO", "2026-10-24T11:00:00Z")
    put_minute(ddb, "cam-001", "2026-10-24T10:20:00Z", "DONE",
               segments=[(ms("2026-10-24T10:20:00Z"), ms("2026-10-24T10:21:00Z"))])
    put_minute(ddb, "cam-001", "2026-10-24T10:21:00Z", "FAILED")
    t = timeline({"from": "2026-10-24T10:20:15Z", "to": "2026-10-24T10:21:20Z"})
    assert t["recorded"] == [{"start": "2026-10-24T10:20:15Z", "end": "2026-10-24T10:21:00Z", "source": "s3"}]
    assert t["failed"] == [{"start": "2026-10-24T10:21:00Z", "end": "2026-10-24T10:21:20Z"}]
    assert [m["start"] for m in t["minutes"]] == ["2026-10-24T10:20:00Z"]
    assert kvs_stub["stub"].calls == []  # 全部処理済み位置より前なら KVS は呼ばない


def test_failed_ranges_merge(ddb, kvs_stub):
    cursor(ddb, "cam-001", "VIDEO", "2026-10-24T11:00:00Z")
    for m in ("10:20", "10:21", "10:22", "10:25"):
        put_minute(ddb, "cam-001", f"2026-10-24T{m}:00Z", "FAILED")
    put_minute(ddb, "cam-001", "2026-10-24T10:23:00Z", "EMPTY")
    t = timeline({"from": "2026-10-24T10:00:00Z", "to": "2026-10-24T10:30:00Z"})
    assert t["failed"] == [{"start": "2026-10-24T10:20:00Z", "end": "2026-10-24T10:23:00Z"},
                           {"start": "2026-10-24T10:25:00Z", "end": "2026-10-24T10:26:00Z"}]
    assert t["recorded"] == []


def test_no_cursor_uses_kvs_only(ddb, kvs_stub):
    put_minute(ddb, "cam-001", "2026-10-24T10:20:00Z", "DONE")
    kvs_stub["stub"] = FakeKvs([("2026-10-24T10:20:05Z", 1000)])
    t = timeline({"from": "2026-10-24T10:20:00Z", "to": "2026-10-24T10:21:00Z"})
    assert t["video_processed_until"] is None
    assert t["recorded"] == [{"start": "2026-10-24T10:20:05Z", "end": "2026-10-24T10:20:06Z", "source": "kvs"}]
    assert t["minutes"] == []


def test_done_without_segments_is_full_minute(ddb, kvs_stub):
    cursor(ddb, "cam-001", "VIDEO", "2026-10-24T10:21:00Z")
    put_minute(ddb, "cam-001", "2026-10-24T10:20:00Z", "DONE")
    t = timeline({"from": "2026-10-24T10:20:00Z", "to": "2026-10-24T10:21:00Z"})
    assert t["recorded"] == [{"start": "2026-10-24T10:20:00Z", "end": "2026-10-24T10:21:00Z", "source": "s3"}]


def test_pagination_full_day(ddb, kvs_stub, monkeypatch):
    """1 日分(1,440 件)を、Query のページ送りで取りこぼさないこと。"""
    start = datetime(2026, 10, 23, 15, 0, tzinfo=timezone.utc)
    with ddb.batch_writer() as bw:
        for i in range(1440):
            m = start + timedelta(minutes=i)
            s = to_ms(m)
            bw.put_item(Item={"camera_id": "cam-001", "sk": iso(m), "video_status": "DONE" if i % 2 == 0 else "EMPTY",
                              "segments": [[s, s + 60000]], "pad": "x" * 900})
    cursor(ddb, "cam-001", "VIDEO", iso(start + timedelta(days=1)))

    real_table = aws.table
    calls = []

    class Spy:
        def __init__(self, t):
            self.t = t
            self.meta = t.meta

        def query(self, **kw):
            calls.append(kw)
            kw = dict(kw, Limit=500)  # 1 ページを小さくしてページ送りを必ず起こす
            return self.t.query(**kw)

    monkeypatch.setattr(aws, "table", lambda name: Spy(real_table(name)))
    t = timeline({"from": iso(start), "to": iso(start + timedelta(days=1))})
    assert len(t["minutes"]) == 720
    assert len(t["recorded"]) == 720
    assert len(calls) == 3 and "ExclusiveStartKey" in calls[1]
    # 必要な属性だけ取る(詰め物の pad は取らない)
    proj = calls[0]["ProjectionExpression"]
    names = calls[0]["ExpressionAttributeNames"]
    used = {names.get(p.split(".")[0].strip(), p.strip()) for p in proj.split(",")}
    assert used == {"sk", "video_status", "segments", "files", "codecs"}
    assert "pad" not in json.dumps(names)


def test_range_validation(ddb, kvs_stub):
    r = handler.lambda_handler(event("GET", "/api/cameras/cam-001/timeline",
                                     {"from": "2026-10-20T00:00:00Z", "to": "2026-10-24T00:00:00Z"}), None)
    assert r["statusCode"] == 400
    r = handler.lambda_handler(event("GET", "/api/cameras/cam-001/timeline", {"from": "nope", "to": "x"}), None)
    assert r["statusCode"] == 400
    r = handler.lambda_handler(event("GET", "/api/cameras/cam-999/timeline", {}), None)
    assert r["statusCode"] == 404
    assert json.loads(r["body"])["error"]["code"] == "CAMERA_NOT_FOUND"


def test_jst_offset_in_query(ddb, kvs_stub):
    cursor(ddb, "cam-001", "VIDEO", "2026-10-24T11:00:00Z")
    # クエリ文字列の "+" が空白になって届いても読める
    t = timeline({"from": "2026-10-24T19:20:00 09:00", "to": "2026-10-24T19:21:00+09:00"})
    assert t["from"] == "2026-10-24T10:20:00Z"
