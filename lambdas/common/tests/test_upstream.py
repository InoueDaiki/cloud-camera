"""上流(動画の退避)の処理済み位置を越えない(物体検出が clip.mp4 を読むための待ち合わせ)。"""

from datetime import datetime, timezone

import pytest

from common import MinuteResult, NotReady, kvs, run_pipeline
from common.timeutil import epoch_ms

CAM = "cam-001"


def T(h, m, s=0):
    return datetime(2026, 10, 24, h, m, s, tzinfo=timezone.utc)


class Stream:
    """1 秒ごとの断片が until まで届いているストリーム(until が None なら範囲の終わりまで)。"""

    def __init__(self, until=None):
        self.until = until
        self.calls = 0

    def list_fragments(self, stream, start, end, selector="PRODUCER_TIMESTAMP"):
        self.calls += 1
        out, t = [], epoch_ms(start)
        stop = min(epoch_ms(end), epoch_ms(self.until)) if self.until else epoch_ms(end)
        while t < stop:
            out.append(kvs.Fragment(str(t), t, t, 1000, 10))
            t += 1000
        return out


class Proc:
    def __init__(self, not_ready=()):
        self.calls = []
        self.not_ready = set(not_ready)

    def __call__(self, camera_id, start, end):
        self.calls.append(start)
        if start in self.not_ready:
            raise NotReady("video_status がまだない")
        return MinuteResult("DONE", {"detect_count": 0})


@pytest.fixture(autouse=True)
def _clear():
    kvs.clear_cache()
    yield
    kvs.clear_cache()


def set_video_cursor(table, until):
    table.put_item(Item={"camera_id": CAM, "sk": "CURSOR#VIDEO", "processed_until": until})


def run(table, clock, proc, stream, **kw):
    args = dict(lag_seconds=30, max_minutes=10, max_attempts=3, table=table, now=clock, retention_days=30,
                stream_name="s", list_fragments=stream.list_fragments, max_late_seconds=720,
                upstream_kind="VIDEO", upstream_max_wait=1800)
    args.update(kw)
    return run_pipeline("DETECT", CAM, proc, **args)


def cursor(table, kind="DETECT"):
    return table.get_item(Key={"camera_id": CAM, "sk": f"CURSOR#{kind}"}).get("Item")


def test_does_not_pass_upstream_cursor(table, clock):
    # 現在 10:30:20、lag 30 秒 → 対象は 10:24〜10:28 の 5 区間。動画の退避は 10:27 まで確定
    set_video_cursor(table, "2026-10-24T10:27:00Z")
    proc = Proc()
    res = run(table, clock, proc, Stream())
    assert proc.calls == [T(10, 24), T(10, 25), T(10, 26)]
    assert res["stopped"] == "waiting_upstream"
    assert res["upstream_processed_until"] == "2026-10-24T10:27:00Z"
    assert cursor(table)["processed_until"] == "2026-10-24T10:27:00Z"
    # 待っている区間の件は作らない(試行回数に数えない)
    assert table.get_item(Key={"camera_id": CAM, "sk": "2026-10-24T10:27:00Z"}).get("Item") is None
    # 上流が進めば続きを処理する
    set_video_cursor(table, "2026-10-24T10:30:00Z")
    proc = Proc()
    res = run(table, clock, proc, Stream())
    assert proc.calls == [T(10, 27), T(10, 28)] and res["stopped"] == "caught_up"


def test_upstream_done_skips_own_watermark(table, clock):
    """上流が確定させた区間は、自分のウォーターマーク(送信が止まって届いていない)を見ずに処理する。"""
    set_video_cursor(table, "2026-10-24T10:30:00Z")
    proc = Proc()
    res = run(table, clock, proc, Stream(until=T(10, 25, 30)))  # 10:25:30 で送信が止まった
    assert len(proc.calls) == 5 and res["stopped"] == "caught_up"


def test_upstream_done_does_not_call_list_fragments(table, clock):
    """上流がすべて確定させていれば、ListFragments(ウォーターマーク)を呼ばない(1 起動あたりの KVS 呼び出しを減らす)。"""
    set_video_cursor(table, "2026-10-24T10:30:00Z")
    stream = Stream()
    res = run(table, clock, Proc(), stream)
    assert res["stopped"] == "caught_up" and stream.calls == 0
    assert "watermark" not in res
    # 上流を待つだけの起動でも呼ばない(対象は 10:30 の区間、動画の退避は 10:30 まで)
    clock.dt = T(10, 31, 35)
    stream = Stream()
    res = run(table, clock, Proc(), stream)
    assert res["stopped"] == "waiting_upstream" and stream.calls == 0


def test_no_upstream_cursor_waits(table, clock):
    proc = Proc()
    res = run(table, clock, proc, Stream())
    assert proc.calls == [] and res["stopped"] == "waiting_upstream"
    assert res["upstream_processed_until"] is None


def test_upstream_stalled_past_max_wait_uses_own_watermark(table, clock):
    """上流が上限を過ぎても進まない区間は、自分のウォーターマークで判定して処理する(予備の経路)。"""
    set_video_cursor(table, "2026-10-24T10:24:00Z")
    proc = Proc()
    # 上限 320 秒: 現在 10:30:20 − 320 秒 = 10:25:00 までに終わる 10:24 の区間だけ上限を過ぎた
    res = run(table, clock, proc, Stream(), upstream_max_wait=320)
    assert proc.calls == [T(10, 24)]
    assert res["upstream_late"] == ["2026-10-24T10:24:00Z"] and res["stopped"] == "waiting_upstream"


def test_upstream_stalled_still_needs_own_watermark(table, clock):
    set_video_cursor(table, "2026-10-24T10:24:00Z")
    proc = Proc()
    res = run(table, clock, proc, Stream(until=T(10, 24, 30)), upstream_max_wait=320)
    assert proc.calls == [] and res["stopped"] == "waiting"


def test_not_ready_inside_upstream(table, clock):
    """上流の処理済み位置を越えていなくても、process_minute が NotReady なら止まる(試行回数に数えない)。"""
    set_video_cursor(table, "2026-10-24T10:30:00Z")
    proc = Proc(not_ready={T(10, 25)})
    res = run(table, clock, proc, Stream())
    assert proc.calls == [T(10, 24), T(10, 25)] and res["stopped"] == "not_ready"
    it = table.get_item(Key={"camera_id": CAM, "sk": "2026-10-24T10:25:00Z"})["Item"]
    assert it["detect_attempts"] == 0


def test_upstream_wait_from_env(table, clock, monkeypatch):
    monkeypatch.setenv("UPSTREAM_MAX_WAIT_SECONDS", "320")
    set_video_cursor(table, "2026-10-24T10:24:00Z")
    proc = Proc()
    run(table, clock, proc, Stream(), upstream_max_wait=None)
    assert proc.calls == [T(10, 24)]


def test_invalid_upstream(table, clock):
    with pytest.raises(ValueError):
        run(table, clock, Proc(), Stream(), upstream_kind="DETECT")


def test_caught_up_does_not_read_upstream(table, clock):
    set_video_cursor(table, "2026-10-24T10:30:00Z")
    run(table, clock, Proc(), Stream())
    clock.dt = T(10, 30, 25)
    res = run(table, clock, Proc(), Stream())
    assert res["stopped"] == "caught_up" and "upstream_processed_until" not in res
