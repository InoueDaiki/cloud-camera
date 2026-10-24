from datetime import datetime, timezone

import pytest

from app import athena, config, events
from app.prompt import calendar

UTC = timezone.utc
NOW = datetime(2026, 10, 22, 5, 35, 20, tzinfo=UTC)  # 日本時間 10/22(木)14:35:20


def test_parse_jst():
    assert events.parse_jst("2026-10-22 09:00", "s") == datetime(2026, 10, 22, 0, 0, tzinfo=UTC)
    assert events.parse_jst("2026-10-21 24:00", "s") == datetime(2026, 10, 21, 15, 0, tzinfo=UTC)
    assert events.parse_jst("2026-10-22T09:00:00+09:00", "s") == datetime(2026, 10, 22, 0, 0, tzinfo=UTC)
    with pytest.raises(events.ToolInputError):
        events.parse_jst("昨日", "start")


def test_build_sql_dt_covers_utc_dates():
    # 日本時間 10/22 の 0〜12 時 = UTC 10/21 15:00〜10/22 03:00 → dt は 2 日
    sql = events.build_sql(table="detections", start=events.parse_jst("2026-10-22 00:00", "s"),
                           end=events.parse_jst("2026-10-22 12:00", "e"), labels=["person"],
                           cameras=["cam-001"], min_confidence=0.5)
    assert "dt BETWEEN '2026-10-21' AND '2026-10-22'" in sql
    assert "ts >= TIMESTAMP '2026-10-21 15:00:00.000'" in sql
    assert "ts < TIMESTAMP '2026-10-22 03:00:00.000'" in sql
    assert "label IN ('person')" in sql and "camera_id IN ('cam-001')" in sql
    # end がちょうど UTC の 0 時なら、その日の dt は要らない
    sql = events.build_sql(table="detections", start=events.parse_jst("2026-10-22 08:00", "s"),
                           end=events.parse_jst("2026-10-22 09:00", "e"), labels=[], cameras=[], min_confidence=0.5)
    assert "dt BETWEEN '2026-10-21' AND '2026-10-21'" in sql


def test_build_sql_passes_guard():
    from app import sql_guard

    sql = events.build_sql(table="detections", start=events.parse_jst("2026-10-22 00:00", "s"),
                           end=events.parse_jst("2026-10-23 00:00", "e"), labels=["dog", "cat"], cameras=[],
                           min_confidence=0.5)
    g = sql_guard.guard(sql, table="detections", database="db")
    assert g.warnings == []


@pytest.fixture
def cfg():
    return config.Config(glue_table="detections", cameras={"cam-001": "gw", "cam-002": "gw"})


def _fake_athena(monkeypatch, rows):
    calls = []

    def run(cfg, sql, **kw):
        calls.append(sql)
        return athena.QueryResult(
            columns=["camera_id", "label", "start_ts", "end_ts", "frames", "max_objects", "confidence"],
            types=["varchar", "varchar", "timestamp", "timestamp", "bigint", "bigint", "real"],
            rows=rows, bytes_scanned=100, execution_ms=10, query_execution_id="q")

    monkeypatch.setattr(athena, "run", run)
    return calls


def test_run_search_events_and_groups(monkeypatch, cfg):
    calls = _fake_athena(monkeypatch, [
        ["cam-001", "person", "2026-10-21 15:00:00.537", "2026-10-21 15:00:25.540", "26", "1", "0.8"],
        ["cam-001", "person", "2026-10-22 01:02:05.537", "2026-10-22 01:02:24.537", "20", "2", "0.9"],
        ["cam-001", "person", "2026-10-22 01:40:15.537", "2026-10-22 01:41:14.537", "60", "3", "0.9"],
    ])
    out = events.run_search(cfg, {"start": "2026-10-22 00:00", "end": "2026-10-23 00:00", "labels": ["person"],
                                   "camera_ids": ["玄関"], "group_by": "hour"},
                            now=NOW, names={"cam-001": "玄関"}, timeout_s=5, next_event_no=3)
    p = out.payload
    assert "camera_id IN ('cam-001')" in calls[0]  # 名前から ID に直す
    assert p["event_count"] == 3 and p["total_frames"] == 106
    assert [e["event_id"] for e in p["events"]] == ["E3", "E4", "E5"]
    assert p["groups"][0]["key"] == "2026-10-22 10時台" and p["groups"][0]["events"] == 2
    assert p["events"][0]["may_continue_from_before_range"] is True  # 0 時ちょうどから: 前日から続いている
    assert p["events"][0]["camera_name"] == "玄関"
    assert any("現在" in n for n in p["notes"])  # end を現在に切り詰めた
    assert p["first_event"]["start_jst"].startswith("2026-10-22(木) 00:00:00")


def test_run_search_future_does_not_query(monkeypatch, cfg):
    calls = _fake_athena(monkeypatch, [])
    out = events.run_search(cfg, {"start": "2026-10-23 12:00", "end": "2026-10-23 18:00"},
                            now=NOW, names={}, timeout_s=5)
    assert calls == [] and out.payload["event_count"] == 0
    assert "未来" in out.payload["notes"][0]


@pytest.mark.parametrize("inp, msg", [
    ({"start": "2026-10-22 00:00", "end": "2026-10-21 00:00"}, "end"),
    ({"start": "2026-08-01 00:00", "end": "2026-10-01 00:00"}, "31"),
    ({"start": "2026-10-22 00:00", "end": "2026-10-22 12:00", "labels": ["人"]}, "COCO"),
    ({"start": "2026-10-22 00:00", "end": "2026-10-22 12:00", "camera_ids": ["cam-004"]}, "存在しない"),
    ({"start": "2026-10-22 00:00", "end": "2026-10-22 12:00", "group_by": "week"}, "group_by"),
])
def test_run_search_rejects(monkeypatch, cfg, inp, msg):
    _fake_athena(monkeypatch, [])
    with pytest.raises(events.ToolInputError, match=msg):
        events.run_search(cfg, inp, now=NOW, names={}, timeout_s=5)


def test_calendar_ranges():
    text = calendar(NOW)
    assert "2026-10-16(金): 先週の金曜" in text
    assert "2026-10-19(月): 今週の月曜" in text
    assert "2026-10-20(火): 一昨日、今週の火曜" in text
    assert "先週(前の週の月曜〜日曜): start='2026-10-12 00:00', end='2026-10-19 00:00'" in text
    assert "週末・土日(直近の土曜 0 時〜月曜 0 時): start='2026-10-17 00:00', end='2026-10-19 00:00'" in text
    assert "直近 10 分: start='2026-10-22 14:25:20', end='2026-10-22 14:35:20'" in text
    assert "今日: start='2026-10-22 00:00', end='2026-10-23 00:00'" in text
