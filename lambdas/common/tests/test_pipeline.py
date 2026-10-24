from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from common import MinuteResult, run_pipeline

CAM = "cam-001"


def T(h, m, s=0):
    return datetime(2026, 10, 24, h, m, s, tzinfo=timezone.utc)


def item(table, sk):
    return table.get_item(Key={"camera_id": CAM, "sk": sk}).get("Item")


def cursor(table, kind="VIDEO"):
    return item(table, f"CURSOR#{kind}")


class Recorder:
    """呼ばれた区間を記録し、指定の区間で例外を投げる process_minute。"""

    def __init__(self, fail=(), status="DONE", attrs=None):
        self.calls = []
        self.fail = set(fail)
        self.status = status
        self.attrs = attrs or {}

    def __call__(self, camera_id, start, end):
        assert end - start == timedelta(minutes=1)
        self.calls.append(start)
        if start in self.fail:
            raise RuntimeError(f"boom {start:%H:%M}")
        return MinuteResult(self.status, dict(self.attrs, video_marker=start.isoformat()))


def run(table, clock, proc, kind="VIDEO", **kw):
    args = dict(lag_seconds=30, max_minutes=10, max_attempts=3, table=table, now=clock, retention_days=30)
    args.update(kw)
    return run_pipeline(kind, CAM, proc, **args)


def test_cursor_init_and_process_in_order(table, clock):
    proc = Recorder()
    res = run(table, clock, proc)
    # now=10:30:20, lag=30s → target_end=10:29、カーソル初期値は 10:29 − 5 分 = 10:24
    assert proc.calls == [T(10, 24), T(10, 25), T(10, 26), T(10, 27), T(10, 28)]
    assert res["processed_until_before"] == "2026-10-24T10:24:00Z"
    assert res["processed_until"] == "2026-10-24T10:29:00Z"
    assert res["stopped"] == "caught_up"
    c = cursor(table)
    assert c["processed_until"] == "2026-10-24T10:29:00Z"
    assert "lease_until" not in c  # 解放済み
    it = item(table, "2026-10-24T10:24:00Z")
    assert it["video_status"] == "DONE"
    assert it["video_attempts"] == 1
    assert "video_duration_ms" in it
    assert it["expires_at"] == int((T(10, 24) + timedelta(days=30)).timestamp())
    assert it["video_marker"] == T(10, 24).isoformat()


def test_second_run_continues_from_cursor(table, clock):
    run(table, clock, Recorder())
    clock.dt = T(10, 31, 45)
    proc = Recorder()
    run(table, clock, proc)
    assert proc.calls == [T(10, 29), T(10, 30)]
    assert cursor(table)["processed_until"] == "2026-10-24T10:31:00Z"


def test_nothing_to_do_before_lag(table, clock):
    run(table, clock, Recorder())
    clock.dt = T(10, 30, 25)  # 10:29:55 → target_end 10:29 のまま
    proc = Recorder()
    res = run(table, clock, proc)
    assert proc.calls == []
    assert res["stopped"] == "caught_up"


def test_lease_busy(table, clock):
    table.put_item(Item={"camera_id": CAM, "sk": "CURSOR#VIDEO", "processed_until": "2026-10-24T10:20:00Z",
                         "lease_until": "2026-10-24T10:35:00Z", "lease_owner": "other"})
    proc = Recorder()
    res = run(table, clock, proc)
    assert res["lease"] is False and res["stopped"] == "lease_busy"
    assert proc.calls == []
    c = cursor(table)
    assert c["lease_owner"] == "other" and c["processed_until"] == "2026-10-24T10:20:00Z"


def test_expired_lease_is_taken(table, clock):
    table.put_item(Item={"camera_id": CAM, "sk": "CURSOR#VIDEO", "processed_until": "2026-10-24T10:27:00Z",
                         "lease_until": "2026-10-24T10:30:00Z", "lease_owner": "dead"})
    proc = Recorder()
    res = run(table, clock, proc)
    assert res["lease"] is True
    assert proc.calls == [T(10, 27), T(10, 28)]


def test_kinds_are_independent(table, clock):
    run(table, clock, Recorder(), kind="VIDEO")
    det = Recorder(attrs={"detect_count": 3})
    run(table, clock, det, kind="DETECT", lag_seconds=90)
    assert cursor(table, "VIDEO")["processed_until"] == "2026-10-24T10:29:00Z"
    assert cursor(table, "DETECT")["processed_until"] == "2026-10-24T10:28:00Z"
    it = item(table, "2026-10-24T10:24:00Z")
    # 両方の属性が残っている(UpdateItem で自分の属性だけ書く)
    assert it["video_status"] == "DONE" and it["detect_status"] == "DONE"
    assert it["video_attempts"] == 1 and it["detect_attempts"] == 1
    assert it["detect_count"] == 3


def test_failure_stops_cursor_then_retries_then_failed(table, clock):
    bad = T(10, 26)
    proc = Recorder(fail={bad})
    res = run(table, clock, proc)
    assert proc.calls == [T(10, 24), T(10, 25), T(10, 26)]
    assert res["stopped"] == "retry"
    assert cursor(table)["processed_until"] == "2026-10-24T10:26:00Z"
    it = item(table, "2026-10-24T10:26:00Z")
    assert "video_status" not in it
    assert it["video_attempts"] == 1
    assert "boom" in it["video_error"]

    # 2 回目も失敗
    proc = Recorder(fail={bad})
    run(table, clock, proc)
    assert proc.calls == [bad]
    assert item(table, "2026-10-24T10:26:00Z")["video_attempts"] == 2

    # 3 回目(上限)で FAILED 確定して先へ進む
    proc = Recorder(fail={bad})
    res = run(table, clock, proc)
    assert proc.calls == [bad, T(10, 27), T(10, 28)]
    it = item(table, "2026-10-24T10:26:00Z")
    assert it["video_status"] == "FAILED" and it["video_attempts"] == 3
    assert it["video_error"].startswith("max_attempts:")
    assert cursor(table)["processed_until"] == "2026-10-24T10:29:00Z"


def test_success_after_retry_clears_error(table, clock):
    bad = T(10, 24)
    run(table, clock, Recorder(fail={bad}))
    assert "video_error" in item(table, "2026-10-24T10:24:00Z")
    run(table, clock, Recorder())
    it = item(table, "2026-10-24T10:24:00Z")
    assert it["video_status"] == "DONE" and it["video_attempts"] == 2
    assert "video_error" not in it


def test_attempts_over_limit_from_crash(table, clock):
    # 前回までにタイムアウト等で 3 回落ちた(試行回数だけ残っている)区間は、処理せずに FAILED
    table.put_item(Item={"camera_id": CAM, "sk": "CURSOR#VIDEO", "processed_until": "2026-10-24T10:27:00Z"})
    table.put_item(Item={"camera_id": CAM, "sk": "2026-10-24T10:27:00Z", "video_attempts": 3, "detect_status": "DONE"})
    proc = Recorder()
    run(table, clock, proc)
    assert proc.calls == [T(10, 28)]
    it = item(table, "2026-10-24T10:27:00Z")
    assert it["video_status"] == "FAILED" and it["video_attempts"] == 4
    assert it["detect_status"] == "DONE"


def test_near_expiry_failure_is_final(table, clock):
    # 保持期間 1 時間、余裕 5 分 → 10:30:20 時点で 9:35:20 以前に始まる区間は「期限が近い」
    table.put_item(Item={"camera_id": CAM, "sk": "CURSOR#VIDEO", "processed_until": "2026-10-24T09:35:00Z"})
    proc = Recorder(fail={T(9, 35)})
    res = run(table, clock, proc, max_minutes=2)
    assert proc.calls == [T(9, 35), T(9, 36)]
    it = item(table, "2026-10-24T09:35:00Z")
    assert it["video_status"] == "FAILED" and it["video_attempts"] == 1
    assert it["video_error"].startswith("near_expiry:")
    assert res["stopped"] == "max_minutes"
    assert cursor(table)["processed_until"] == "2026-10-24T09:37:00Z"


def test_expired_minutes_are_failed_without_processing(table, clock):
    # 9:28 の区間は 9:29 に終わる → 10:30:20 − 1 時間 = 9:30:20 より前なので読めない
    table.put_item(Item={"camera_id": CAM, "sk": "CURSOR#VIDEO", "processed_until": "2026-10-24T09:28:00Z"})
    proc = Recorder()
    res = run(table, clock, proc, max_minutes=2)
    # 9:28 と 9:29 は期限切れ(上限の件数に数えない)、9:30 と 9:31 を処理
    assert proc.calls == [T(9, 30), T(9, 31)]
    for sk in ("2026-10-24T09:28:00Z", "2026-10-24T09:29:00Z"):
        it = item(table, sk)
        assert it["video_status"] == "FAILED" and it["video_error"].startswith("expired:")
        assert "video_attempts" not in it
    assert cursor(table)["processed_until"] == "2026-10-24T09:32:00Z"
    assert res["stopped"] == "max_minutes"


def test_max_minutes(table, clock):
    table.put_item(Item={"camera_id": CAM, "sk": "CURSOR#VIDEO", "processed_until": "2026-10-24T10:00:00Z"})
    proc = Recorder()
    res = run(table, clock, proc, max_minutes=3)
    assert proc.calls == [T(10, 0), T(10, 1), T(10, 2)]
    assert res["stopped"] == "max_minutes"
    assert cursor(table)["processed_until"] == "2026-10-24T10:03:00Z"


def test_stop_when_remaining_time_is_short(table, clock, fake_context):
    ctx = fake_context(remaining_ms=60_000)

    def proc(camera_id, start, end):
        ctx.remaining -= 20_000  # 1 区間ごとに 20 秒使う想定
        return MinuteResult("EMPTY")

    res = run(table, clock, proc, context=ctx, min_remaining_ms=15_000)
    # 60s → 40s → 20s → 0s(15s 未満で打ち切り)
    assert res["stopped"] == "time"
    assert res["processed_until"] == "2026-10-24T10:27:00Z"
    assert cursor(table)["lease_owner"] == "req-1"
    assert "lease_until" not in cursor(table)


def test_lease_uses_context_remaining_time(table, clock, fake_context):
    seen = {}

    def proc(camera_id, start, end):
        seen.setdefault("lease", cursor(table)["lease_until"])
        return MinuteResult("EMPTY")

    run(table, clock, proc, context=fake_context(remaining_ms=120_000))
    # 10:30:20 + 120s + 10s
    assert seen["lease"] == "2026-10-24T10:32:30Z"


def test_lease_lost_stops_without_advancing(table, clock):
    def proc(camera_id, start, end):
        # 別の実行にリースを取られた
        table.update_item(Key={"camera_id": CAM, "sk": "CURSOR#VIDEO"},
                          UpdateExpression="SET lease_owner = :o", ExpressionAttributeValues={":o": "thief"})
        return MinuteResult("DONE")

    res = run(table, clock, proc)
    assert res["stopped"] == "lease_lost"
    c = cursor(table)
    assert c["processed_until"] == "2026-10-24T10:24:00Z"
    assert c["lease_owner"] == "thief" and "lease_until" in c  # 他人のリースは消さない


def test_status_failed_from_process_is_final(table, clock):
    calls = []

    def proc(camera_id, start, end):
        calls.append(start)
        if start == T(10, 24):
            return MinuteResult("FAILED", {"video_error": "codec changed"})
        return MinuteResult("EMPTY")

    run(table, clock, proc)
    assert len(calls) == 5
    it = item(table, "2026-10-24T10:24:00Z")
    assert it["video_status"] == "FAILED" and it["video_error"] == "codec changed"


def test_invalid_status_is_failure(table, clock):
    res = run(table, clock, lambda c, s, e: MinuteResult("WAT"))
    assert res["stopped"] == "retry"
    assert "invalid status" in item(table, "2026-10-24T10:24:00Z")["video_error"]


def test_attributes_float_and_nested(table, clock):
    attrs = {"segments": [[1, 2]], "hls": {"init_length": 10, "parts": [[10, 20, 1000.5]]}, "ratio": 0.25,
             "video_status": "IGNORED", "camera_id": "IGNORED"}
    run(table, clock, lambda c, s, e: MinuteResult("DONE", attrs))
    it = item(table, "2026-10-24T10:24:00Z")
    assert it["ratio"] == Decimal("0.25")
    assert it["hls"]["parts"][0][2] == Decimal("1000.5")
    assert it["video_status"] == "DONE" and it["camera_id"] == CAM


def test_exception_in_loop_releases_lease(table, clock):
    class Boom(BaseException):
        pass

    def proc(camera_id, start, end):
        raise Boom()

    with pytest.raises(Boom):
        run(table, clock, proc)
    assert "lease_until" not in cursor(table)


def test_unknown_kind(table, clock):
    with pytest.raises(ValueError):
        run(table, clock, Recorder(), kind="AUDIO")


def test_table_from_env(table, clock):
    res = run_pipeline("VIDEO", CAM, Recorder(), lag_seconds=30, max_minutes=1, max_attempts=3, now=clock)
    assert res["processed_until"] == "2026-10-24T10:25:00Z"
