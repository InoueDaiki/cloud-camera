"""ウォーターマーク方式(回線断のあとに遅れて届く断片を取りこぼさない)の試験。

時計は 10:30:20、lag 30 秒 → 処理対象の終わりは 10:29。カーソルは 10:24 から。
"""

from datetime import datetime, timedelta, timezone

import pytest

from common import MinuteResult, kvs, run_pipeline
from common.timeutil import epoch_ms

CAM = "cam-001"


def T(h, m, s=0, ms=0):
    return datetime(2026, 10, 24, h, m, s, ms * 1000, tzinfo=timezone.utc)


def item(table, sk):
    return table.get_item(Key={"camera_id": CAM, "sk": sk}).get("Item")


def cursor(table):
    return item(table, "CURSOR#VIDEO")


class Stream:
    """KVS のストリームのふり。届いた断片(ProducerTimestamp)を持ち、ListFragments の呼び出しを数える。"""

    def __init__(self):
        self.frags: list[kvs.Fragment] = []
        self.calls: list[tuple] = []
        self.error: Exception | None = None

    def add(self, start, end, step_s=1, length=1000):
        t = start
        while t < end:
            n = len(self.frags) + 1
            self.frags.append(kvs.Fragment(str(n), epoch_ms(t), epoch_ms(t) + 100, length, 100))
            t += timedelta(seconds=step_s)

    def list_fragments(self, stream, start, end, selector="PRODUCER_TIMESTAMP"):
        self.calls.append((stream, start, end, selector))
        if self.error:
            raise self.error
        return kvs.dedupe_fragments([f for f in self.frags if epoch_ms(start) <= f.producer_ms < epoch_ms(end)])


class Archive:
    """process_minute: 共通処理のキャッシュ経由で kvs.list_fragments を呼び、断片があれば DONE(本物と同じ流れ)。"""

    def __init__(self):
        self.calls = []

    def __call__(self, camera_id, start, end):
        frs = kvs.list_fragments("s", start, end)  # キャッシュから返る(API は呼ばない)
        self.calls.append((start, len(frs)))
        if not frs:
            return MinuteResult("EMPTY", {"fragment_count": 0})
        return MinuteResult("DONE", {"fragment_count": len(frs), "segments": kvs.build_segments(frs)})


@pytest.fixture(autouse=True)
def _no_real_kvs(monkeypatch):
    # キャッシュに当たらなければ実 KVS に行こうとするので、ここで失敗させる
    def boom(*a, **k):
        raise AssertionError("ListFragments API was called")

    monkeypatch.setattr(kvs, "archived_media_client", boom)
    kvs.clear_cache()
    yield
    kvs.clear_cache()


def run(table, clock, stream, proc, **kw):
    args = dict(lag_seconds=30, max_minutes=10, max_attempts=3, table=table, now=clock, retention_days=30,
                stream_name="s", list_fragments=stream.list_fragments, max_late_seconds=720)
    args.update(kw)
    return run_pipeline("VIDEO", CAM, proc, **args)


def test_live_stream_processes_all_with_one_list_call(table, clock):
    st = Stream()
    st.add(T(10, 24), T(10, 30, 18))
    proc = Archive()
    res = run(table, clock, st, proc)
    assert res["stopped"] == "caught_up"
    assert [c[1] for c in proc.calls] == [60] * 5
    # ListFragments はウォーターマーク用の 1 回だけ(カーソル 〜 現在 + 60 秒)。各区間の一覧はキャッシュから
    assert st.calls == [("s", T(10, 24), T(10, 31, 20), "PRODUCER_TIMESTAMP")]
    assert res["watermark"] == "2026-10-24T10:30:18.000Z"
    # 処理済み位置まで追いついていれば ListFragments を呼ばない
    st.calls.clear()
    clock.dt = T(10, 30, 25)
    run(table, clock, st, Archive())
    assert st.calls == []


def test_waits_for_watermark_without_counting_attempts(table, clock):
    st = Stream()
    st.add(T(10, 24), T(10, 26, 30))  # 送信が 10:26:30 で止まった(回線断)
    proc = Archive()
    res = run(table, clock, st, proc)
    assert [c[0] for c in proc.calls] == [T(10, 24), T(10, 25)]
    assert res["stopped"] == "waiting"
    assert cursor(table)["processed_until"] == "2026-10-24T10:26:00Z"
    assert item(table, "2026-10-24T10:26:00Z") is None  # 試行回数にも数えない(件を作らない)
    for _ in range(3):  # 何度起動しても待つ
        clock.dt += timedelta(minutes=1)
        res = run(table, clock, st, Archive())
        assert res["stopped"] == "waiting"
    assert cursor(table)["processed_until"] == "2026-10-24T10:26:00Z"


def test_backlog_after_outage_fills_in_order(table, clock):
    """回線断の間は処理済み位置が止まり、復旧後にバッファの断片が古い順に届くと、録画ありで埋まる。"""
    st = Stream()
    st.add(T(10, 24), T(10, 26, 30))
    run(table, clock, st, Archive())
    assert cursor(table)["processed_until"] == "2026-10-24T10:26:00Z"
    # 5 分後に復旧。バッファの断片が古い順に届き、10:29:10 まで届いたところ
    clock.dt = T(10, 35, 20)
    st.add(T(10, 26, 30), T(10, 29, 10))
    proc = Archive()
    res = run(table, clock, st, proc)
    assert [c[0] for c in proc.calls] == [T(10, 26), T(10, 27), T(10, 28)]
    assert [c[1] for c in proc.calls] == [60, 60, 60]
    assert res["stopped"] == "waiting" and cursor(table)["processed_until"] == "2026-10-24T10:29:00Z"
    for m in (26, 27, 28):
        it = item(table, f"2026-10-24T10:{m}:00Z")
        assert it["video_status"] == "DONE" and it["fragment_count"] == 60 and it["video_attempts"] == 1
    # 残りが届いて追いつく。一度確定した区間は二度と処理しない
    st.add(T(10, 29, 10), T(10, 35, 18))
    proc = Archive()
    res = run(table, clock, st, proc)
    assert [c[0] for c in proc.calls] == [T(10, 29), T(10, 30), T(10, 31), T(10, 32), T(10, 33)]
    assert res["stopped"] == "caught_up"
    assert item(table, "2026-10-24T10:26:00Z")["video_attempts"] == 1


def test_resent_duplicates_are_counted_once(table, clock):
    st = Stream()
    st.add(T(10, 24), T(10, 30, 18))
    # 10:25:10〜10:25:19 の 10 本が再送されて 2 つずつ入った
    for f in list(st.frags):
        if epoch_ms(T(10, 25, 10)) <= f.producer_ms < epoch_ms(T(10, 25, 20)):
            st.frags.append(kvs.Fragment(str(10_000 + int(f.number)), f.producer_ms, f.server_ms + 300_000, 1000, 100))
    proc = Archive()
    run(table, clock, st, proc)
    assert dict(proc.calls)[T(10, 25)] == 60
    it = item(table, "2026-10-24T10:25:00Z")
    assert it["segments"] == [[epoch_ms(T(10, 25)), epoch_ms(T(10, 26))]]


def test_watermark_exactly_at_end_is_ready(table, clock):
    st = Stream()
    st.add(T(10, 24), T(10, 25), length=1000)  # 最後の断片 10:24:59 + 1 秒 = 10:25:00
    proc = Archive()
    run(table, clock, st, proc)
    assert [c[0] for c in proc.calls] == [T(10, 24)]


def test_max_late_processes_with_what_arrived(table, clock):
    """MAX_LATE_SECONDS を過ぎたら届いている分で確定する。その後に届いた断片は処理しない(限界)。"""
    st = Stream()
    st.add(T(10, 24), T(10, 26, 30))
    run(table, clock, st, Archive())
    # 10:26 の区間の終わり(10:27)から 720 秒 = 10:39:00。直前はまだ待つ
    clock.dt = T(10, 38, 59)
    res = run(table, clock, st, Archive())
    assert res["stopped"] == "waiting"
    clock.dt = T(10, 39, 30)
    proc = Archive()
    res = run(table, clock, st, proc)
    # 10:26 は届いている 30 本で確定。10:27 の終わり 10:28 + 720 秒 = 10:40 なのでまだ待つ
    assert proc.calls == [(T(10, 26), 30)]
    assert res["max_late"] == ["2026-10-24T10:26:00Z"]
    assert item(table, "2026-10-24T10:26:00Z")["video_status"] == "DONE"
    assert res["stopped"] == "waiting" and cursor(table)["processed_until"] == "2026-10-24T10:27:00Z"
    clock.dt = T(10, 40, 30)
    proc = Archive()
    run(table, clock, st, proc)
    assert proc.calls == [(T(10, 27), 0)]
    assert item(table, "2026-10-24T10:27:00Z")["video_status"] == "EMPTY"
    # その後にバッファの断片が届いても、確定済みの区間は処理し直さない(後の区間は届いた分で埋まる)
    st.add(T(10, 26, 30), T(10, 40, 10))
    proc = Archive()
    run(table, clock, st, proc)
    assert proc.calls[0] == (T(10, 28), 60)
    assert item(table, "2026-10-24T10:27:00Z")["video_status"] == "EMPTY"
    assert item(table, "2026-10-24T10:26:00Z")["fragment_count"] == 30


def test_max_late_from_env(table, clock, monkeypatch):
    monkeypatch.setenv("MAX_LATE_SECONDS", "60")
    st = Stream()  # 何も届いていない
    proc = Archive()
    res = run(table, clock, st, proc, max_late_seconds=None)
    # 終わりから 60 秒過ぎた区間(〜10:29:20 に終わる)だけ確定: 10:24〜10:28
    assert [c[0] for c in proc.calls] == [T(10, 24), T(10, 25), T(10, 26), T(10, 27), T(10, 28)]
    assert res["stopped"] == "caught_up"


def test_list_error_waits(table, clock):
    st = Stream()
    st.add(T(10, 24), T(10, 30, 18))
    st.error = RuntimeError("throttled")
    proc = Archive()
    res = run(table, clock, st, proc)
    assert proc.calls == [] and res["stopped"] == "waiting" and "throttled" in res["watermark_error"]


def test_cache_cleared_after_run(table, clock):
    st = Stream()
    st.add(T(10, 24), T(10, 30, 18))
    run(table, clock, st, Archive())
    with pytest.raises(AssertionError):
        kvs.list_fragments("s", T(10, 24), T(10, 25))


def test_use_watermark_false(table, clock):
    calls = []
    res = run_pipeline("VIDEO", CAM, lambda c, s, e: calls.append(s) or MinuteResult("EMPTY"), lag_seconds=30,
                       max_minutes=10, max_attempts=3, table=table, now=clock, use_watermark=False)
    assert len(calls) == 5 and res["stopped"] == "caught_up" and "watermark" not in res
