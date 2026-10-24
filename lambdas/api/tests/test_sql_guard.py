import pytest

from app.sql_guard import MAX_ROWS, SqlRejected, guard

OK = "SELECT camera_id, ts FROM detections WHERE dt BETWEEN '2026-10-23' AND '2026-10-24' AND label = 'person'"


def g(sql):
    return guard(sql, table="detections", database="test_db")


def test_ok_adds_limit():
    r = g(OK)
    assert r.sql.endswith(f"LIMIT {MAX_ROWS}")
    assert (r.dt_from, r.dt_to) == ("2026-10-23", "2026-10-24")


def test_keeps_small_limit_and_original_text():
    sql = OK + " ORDER BY ts LIMIT 10;"
    r = g(sql)
    assert r.limit == 10
    assert r.sql == sql.rstrip(";")


def test_large_limit_is_capped():
    r = g(OK + " LIMIT 5000")
    assert r.limit == MAX_ROWS
    assert "5000" not in r.sql


@pytest.mark.parametrize("where", [
    "dt = '2026-10-24'",
    "dt IN ('2026-10-23', '2026-10-24')",
    "dt >= '2026-10-23' AND dt <= '2026-10-24'",
    "'2026-10-23' <= dt AND dt < '2026-10-25'",
    "(dt BETWEEN '2026-10-01' AND '2026-10-31') AND confidence > 0.5",
])
def test_dt_forms(where):
    g(f"SELECT count(*) FROM detections WHERE {where}")


def test_qualified_table_and_cte_and_aggregate():
    g("WITH p AS (SELECT * FROM test_db.detections WHERE dt = '2026-10-24' AND label='person') "
      "SELECT date_trunc('minute', ts) m, count(*) c FROM p GROUP BY 1 ORDER BY 1")


@pytest.mark.parametrize("sql, msg", [
    ("SELECT * FROM detections", "dt"),
    ("SELECT * FROM detections WHERE dt >= '2026-10-01'", "dt"),
    ("SELECT * FROM detections WHERE dt = '2026-10-24' OR 1=1", "dt"),
    ("SELECT * FROM detections WHERE dt BETWEEN '2026-01-01' AND '2026-12-31'", "31"),
    ("SELECT * FROM detections WHERE dt = date '2026-10-24'", "YYYY-MM-DD"),
    ("SELECT * FROM detections WHERE dt = '2026-10-24'; DROP TABLE detections", "1 文"),
    ("SELECT * FROM detections WHERE dt = '2026-10-24'; SELECT 1", "1 文"),
    ("DROP TABLE detections", "SELECT"),
    ("INSERT INTO detections SELECT * FROM detections WHERE dt='2026-10-24'", "SELECT"),
    ("SELECT * FROM other WHERE dt = '2026-10-24'", "detections"),
    ("SELECT * FROM information_schema.tables", "detections"),
    ("SELECT * FROM otherdb.detections WHERE dt = '2026-10-24'", "データベース"),
    ("SELECT 1", "detections"),
    ("SELECT * FROM detections WHERE dt='2026-10-24' UNION ALL SELECT * FROM detections", "dt"),
    ("SELECT * FROM detections d JOIN detections e ON d.ts = e.ts WHERE dt = '2026-10-24'", None),
    ("SELECT * FROM (SELECT * FROM detections) WHERE dt = '2026-10-24'", "dt"),
    ("SELECT * FROM detections WHERE dt = '2026-10-24' LIMIT ALL", "LIMIT"),
    ("", "空"),
])
def test_rejected(sql, msg):
    if msg is None:
        # JOIN の両側が detections でも、同じ SELECT の WHERE に dt があれば通る(スキャンは dt で絞られる)
        g(sql)
        return
    with pytest.raises(SqlRejected) as e:
        g(sql)
    assert msg in str(e.value)


def test_explain_not_select():
    with pytest.raises(SqlRejected):
        g("EXPLAIN SELECT * FROM detections WHERE dt = '2026-10-24'")


def test_union_limit_applies_to_whole():
    r = g("SELECT ts FROM detections WHERE dt='2026-10-24' UNION ALL "
          "SELECT ts FROM detections WHERE dt='2026-10-23'")
    assert r.sql.endswith(f"LIMIT {MAX_ROWS}")


# ---- ts と dt の整合(評価で見つかった「dt だけで日本時間の日を絞ったつもり」「dt が 1 日足りない」への対策)

def test_ts_without_dt_coverage_is_rejected():
    # 日本時間 10/22 0 時〜 = UTC 10/21 15 時〜。dt に 10/21 がない
    with pytest.raises(SqlRejected) as e:
        g("SELECT * FROM detections WHERE dt = '2026-10-22' "
          "AND ts >= TIMESTAMP '2026-10-22 00:00:00 Asia/Tokyo' AND ts < TIMESTAMP '2026-10-22 12:00:00 Asia/Tokyo'")
    assert "dt BETWEEN '2026-10-21' AND '2026-10-22'" in str(e.value)


@pytest.mark.parametrize("cond", [
    "ts >= TIMESTAMP '2026-10-22 00:00:00 Asia/Tokyo' AND ts < TIMESTAMP '2026-10-22 12:00:00 Asia/Tokyo'",
    "ts BETWEEN TIMESTAMP '2026-10-21 15:00:00' AND TIMESTAMP '2026-10-22 02:59:59'",
    "ts >= from_iso8601_timestamp('2026-10-22T00:00:00+09:00') AND ts < TIMESTAMP '2026-10-22 03:00:00 UTC'",
])
def test_ts_with_dt_coverage_ok(cond):
    r = g(f"SELECT * FROM detections WHERE dt BETWEEN '2026-10-21' AND '2026-10-22' AND {cond}")
    assert r.warnings == []


def test_ts_24_oclock_is_rejected():
    with pytest.raises(SqlRejected) as e:
        g("SELECT * FROM detections WHERE dt BETWEEN '2026-10-21' AND '2026-10-22' "
          "AND ts < TIMESTAMP '2026-10-21 24:00:00 Asia/Tokyo'")
    assert "00:00:00" in str(e.value)


def test_missing_ts_is_warned_not_rejected():
    r = g(OK)
    assert len(r.warnings) == 1 and "ts の条件がありません" in r.warnings[0]
    # 検査を切れば警告しない(段階ごとの比較用)
    assert guard(OK, table="detections", database="test_db", check_ts=False).warnings == []


def test_ts_check_inside_cte():
    with pytest.raises(SqlRejected):
        g("WITH f AS (SELECT * FROM detections WHERE dt = '2026-10-22' "
          "AND ts >= TIMESTAMP '2026-10-22 08:00:00 Asia/Tokyo') SELECT count(*) FROM f")
