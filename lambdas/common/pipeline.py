"""処理済み位置・リース・1 分区間ループの共通処理(構想.md「処理状態の管理」)。

1 回の起動の流れ:
1. 処理済み位置(CURSOR#{kind})の件を条件付き更新してリースを取る。取れなければ終了。
   カーソルがなければ「現在 − lag を 1 分境界に切り捨て − 5 分」で初期化する。
2. 処理済み位置から「現在 − lag を 1 分境界に切り捨て」までを、最大 max_minutes 区間処理対象にする。
   対象があれば、カーソル以降の断片を ListFragments で 1 回取り、ウォーターマーク(届いている最新の断片の終わり)を求める。
3. 古い区間から順に処理する。区間 [s, e) を処理してよいのは、
   (a) ウォーターマークが e 以上(kvssink は時刻順に送るので、e より前に始まる断片はすべて届いている)、または
   (b) 現在 − e が max_late_seconds 以上(エッジのバッファ上限を過ぎた。それより後に届く分は捨てる)
   のどちらかを満たすとき。満たさなければそこで止めて次の起動を待つ(試行回数に数えない)。
   process_minute を呼ぶ。区間の件には UpdateItem で自分の属性だけ書く。
   試行回数は処理の **前** に 1 足す(タイムアウトやメモリ不足で落ちても回数が残る)。
4. 確定(DONE / EMPTY / FAILED)した区間が続く限り、処理済み位置を 1 区間ずつ進める。
   確定しなかった区間があればそこで止め、次の起動で再挑戦する。
   試行回数が上限に達した区間、KVS の保持期限が近い(過ぎた)区間は FAILED で確定させる。
5. Lambda の残り時間が足りなくなったら打ち切る。最後にリースを解放する。

上流(upstream_kind)を指定すると、3. の前に「上流の処理済み位置 ≥ e」も条件にする(物体検出は動画の退避の
clip.mp4 を読むので、動画の退避が確定させた区間だけを処理する)。上流の処理済み位置は起動ごとに 1 回読む。
上流が確定させた区間は、上流がウォーターマークで判定済みなので自分のウォーターマークは見ない。
上流が upstream_max_wait_seconds(区間の終わりから)を過ぎても進まないときだけ、自分のウォーターマークで判定して処理する
(process_minute は上流の結果がないことを見て、予備の経路で取る)。

書く順序は「S3(process_minute の中)→ 区間の件 → 処理済み位置」。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Literal

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

from . import kvs
from .log import log, max_rss_mb
from .timeutil import MINUTE, epoch_ms, floor_minute, from_epoch_ms, iso, parse_iso, to_utc, utcnow

Kind = Literal["VIDEO", "DETECT"]
Status = Literal["DONE", "EMPTY", "FAILED"]

_PREFIX = {"VIDEO": "video", "DETECT": "detect"}

# カーソルがないときに、さかのぼって始める分数
INITIAL_BACKFILL_MINUTES = 5
# KVS の保持期間(秒)。これを過ぎた区間はもう読めない
KVS_RETENTION_SECONDS = 3600
# 保持期限までこれ以下しか残っていない区間は、失敗したら再試行せず FAILED にする
EXPIRY_MARGIN_SECONDS = 300
# 1 区間の処理に最低限残しておく時間(ミリ秒)
MIN_REMAINING_MS = 15_000
# Lambda コンテキストがないとき(ローカル実行)のリース時間
DEFAULT_LEASE_SECONDS = 900
# 区間の終わりからこれだけ過ぎたら、ウォーターマークが届いていなくても処理する(秒)。
# エッジ(kvssink)のバッファ上限 10 分 + 余裕。それより後に届いた断片は処理しない(エッジ側でも捨てられる量)
MAX_LATE_SECONDS = 720
# ウォーターマークを求めるときの ListFragments の範囲の終わり(現在 + これ)。時計のずれの余裕
WATERMARK_FUTURE_SECONDS = 60
# 上流(動画の退避)の処理済み位置を待つ上限(区間の終わりからの秒)。過ぎたら上流なしで処理する(予備の経路)。
# 上流のウォーターマーク待ち(MAX_LATE_SECONDS 720 秒)+ lag より長く、KVS の保持期限(1 時間)より十分前
UPSTREAM_MAX_WAIT_SECONDS = 1800


def upstream_max_wait_seconds(v: int | None = None) -> int:
    """上流を待つ上限(秒)。省略時は環境変数 UPSTREAM_MAX_WAIT_SECONDS、既定 1800。"""
    if v is not None:
        return v
    return int(os.environ.get("UPSTREAM_MAX_WAIT_SECONDS") or UPSTREAM_MAX_WAIT_SECONDS)


@dataclass
class MinuteResult:
    """process_minute の戻り値。

    status: DONE(データあり)/ EMPTY(録画なし)/ FAILED(再試行しても無駄な失敗。即確定)
    attributes: 区間の件に UpdateItem で SET する属性(自分の担当の属性だけ)
    """

    status: Status
    attributes: dict[str, Any] = field(default_factory=dict)


ProcessMinute = Callable[[str, datetime, datetime], MinuteResult]


class NotReady(Exception):
    """process_minute が「まだ処理できない」(断片が出そろっていない等)ときに投げる。

    失敗として数えない(試行回数を戻す)。カーソルはその区間の手前で止まり、次の起動で再挑戦する。
    """


def _table(table: Any | None):
    if table is not None:
        return table
    return boto3.resource("dynamodb").Table(os.environ["STATE_TABLE"])


def _max_late_seconds(v: int | None) -> int:
    if v is not None:
        return v
    return int(os.environ.get("MAX_LATE_SECONDS") or MAX_LATE_SECONDS)


def _retention_days(retention_days: int | None) -> int:
    if retention_days is not None:
        return retention_days
    return int(os.environ.get("MEDIA_RETENTION_DAYS", "30"))


def to_dynamo(v: Any) -> Any:
    """float を Decimal に直す(boto3 の DynamoDB リソースは float を受け付けない)。"""
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, float):
        return Decimal(str(v))
    if isinstance(v, dict):
        return {k: to_dynamo(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [to_dynamo(x) for x in v]
    return v


def _err_str(e: BaseException) -> str:
    s = f"{type(e).__name__}: {e}"
    return s[:1000]


def run_pipeline(
    kind: Kind,
    camera_id: str,
    process_minute: ProcessMinute,
    *,
    lag_seconds: int,
    max_minutes: int,
    max_attempts: int,
    context: Any = None,
    table: Any = None,
    now: Callable[[], datetime] = utcnow,
    retention_days: int | None = None,
    kvs_retention_seconds: int = KVS_RETENTION_SECONDS,
    expiry_margin_seconds: int = EXPIRY_MARGIN_SECONDS,
    min_remaining_ms: int = MIN_REMAINING_MS,
    max_late_seconds: int | None = None,
    stream_name: str | None = None,
    list_fragments: Callable[..., list] | None = None,
    use_watermark: bool = True,
    upstream_kind: Kind | None = None,
    upstream_max_wait: int | None = None,
) -> dict:
    """1 回の起動ぶんの区間ループを回し、結果の要約を返す。

    context: Lambda のコンテキスト。残り時間の判定とリースの長さ、lease_owner に使う。
    table: DynamoDB の Table リソース(省略時は環境変数 STATE_TABLE)。
    now: 現在時刻を返す関数(テスト用)。
    retention_days: expires_at の日数(省略時は環境変数 MEDIA_RETENTION_DAYS、既定 30)。
    max_late_seconds: ウォーターマークを待つ上限(省略時は環境変数 MAX_LATE_SECONDS、既定 720)。
    stream_name: KVS のストリーム名(省略時は kvs.stream_name_for(camera_id))。
    list_fragments: kvs.list_fragments の差し替え(テスト用)。
    use_watermark: False ならウォーターマークを見ずに lag だけで処理する(KVS を使わないテスト用)。
    upstream_kind: 上流の種類(例: 物体検出なら "VIDEO")。上流の処理済み位置を越えて処理しない(README の「上流を待つ」)。
    upstream_max_wait: 上流を待つ上限(秒、省略時は環境変数 UPSTREAM_MAX_WAIT_SECONDS、既定 1800)。
    """
    if kind not in _PREFIX:
        raise ValueError(f"unknown kind: {kind}")
    if upstream_kind is not None and (upstream_kind not in _PREFIX or upstream_kind == kind):
        raise ValueError(f"invalid upstream_kind: {upstream_kind}")
    p = _PREFIX[kind]
    tbl = _table(table)
    cursor_sk = f"CURSOR#{kind}"
    owner = getattr(context, "aws_request_id", None) or f"local-{os.getpid()}-{time.time_ns()}"
    days = _retention_days(retention_days)

    def remaining_ms() -> int | None:
        if context is None or not hasattr(context, "get_remaining_time_in_millis"):
            return None
        return int(context.get_remaining_time_in_millis())

    started = now()
    summary: dict[str, Any] = {
        "kind": kind,
        "camera_id": camera_id,
        "lease": False,
        "minutes": [],
        "stopped": None,
    }

    # 1. リースを取る(カーソルがなければ初期化も同時に)
    rem = remaining_ms()
    lease_seconds = (rem / 1000 + 10) if rem is not None else DEFAULT_LEASE_SECONDS
    lease_until = started + timedelta(seconds=lease_seconds)
    initial = floor_minute(started - timedelta(seconds=lag_seconds)) - timedelta(minutes=INITIAL_BACKFILL_MINUTES)
    try:
        resp = tbl.update_item(
            Key={"camera_id": camera_id, "sk": cursor_sk},
            UpdateExpression="SET lease_until = :lu, lease_owner = :o, processed_until = if_not_exists(processed_until, :init)",
            ConditionExpression=Attr("lease_until").not_exists() | Attr("lease_until").lt(iso(started)),
            ExpressionAttributeValues={":lu": iso(lease_until), ":o": owner, ":init": iso(initial)},
            ReturnValues="ALL_NEW",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            summary["stopped"] = "lease_busy"
            log("lease_busy", kind=kind, camera_id=camera_id)
            return summary
        raise
    summary["lease"] = True
    cursor = parse_iso(resp["Attributes"]["processed_until"])
    summary["processed_until_before"] = iso(cursor)

    stream = stream_name or kvs.stream_name_for(camera_id)
    try:
        _loop(
            summary=summary,
            tbl=tbl,
            p=p,
            camera_id=camera_id,
            cursor_sk=cursor_sk,
            owner=owner,
            cursor=cursor,
            process_minute=process_minute,
            lag_seconds=lag_seconds,
            max_minutes=max_minutes,
            max_attempts=max_attempts,
            days=days,
            now=now,
            remaining_ms=remaining_ms,
            kvs_retention_seconds=kvs_retention_seconds,
            expiry_margin_seconds=expiry_margin_seconds,
            min_remaining_ms=min_remaining_ms,
            watermark_fn=(lambda s, e: _watermark(stream, s, e, list_fragments or kvs.list_fragments, summary))
            if use_watermark else None,
            max_late_seconds=_max_late_seconds(max_late_seconds),
            upstream_fn=(lambda: _upstream_cursor(tbl, camera_id, upstream_kind, summary)) if upstream_kind else None,
            upstream_max_wait=upstream_max_wait_seconds(upstream_max_wait),
        )
    finally:
        kvs.clear_fragment_cache(stream)
        # 5. リースを解放する(自分が持っているときだけ)
        try:
            tbl.update_item(
                Key={"camera_id": camera_id, "sk": cursor_sk},
                UpdateExpression="REMOVE lease_until",
                ConditionExpression=Attr("lease_owner").eq(owner),
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            log("lease_release_skipped", kind=kind, camera_id=camera_id)

    summary["max_rss_mb"] = max_rss_mb()
    log("pipeline_done", **{k: v for k, v in summary.items() if k != "minutes"}, minute_count=len(summary["minutes"]))
    return summary


def _loop(
    *,
    summary: dict,
    tbl: Any,
    p: str,
    camera_id: str,
    cursor_sk: str,
    owner: str,
    cursor: datetime,
    process_minute: ProcessMinute,
    lag_seconds: int,
    max_minutes: int,
    max_attempts: int,
    days: int,
    now: Callable[[], datetime],
    remaining_ms: Callable[[], int | None],
    kvs_retention_seconds: int,
    expiry_margin_seconds: int,
    min_remaining_ms: int,
    watermark_fn: Callable[[datetime, datetime], int | None] | None,
    max_late_seconds: int,
    upstream_fn: Callable[[], datetime | None] | None = None,
    upstream_max_wait: int = UPSTREAM_MAX_WAIT_SECONDS,
) -> None:
    # 2. 処理対象: [cursor, target_end) を 1 分ずつ、最大 max_minutes 区間
    t_now = now()
    target_end = floor_minute(t_now - timedelta(seconds=lag_seconds))
    summary["target_end"] = iso(target_end)
    summary["processed_until"] = iso(cursor)
    wm: int | None = None
    if watermark_fn is not None and cursor + MINUTE <= target_end:
        # カーソル以降の断片を 1 回で取る(process_minute の list_fragments はこの結果から返る)
        wm = watermark_fn(cursor, t_now + timedelta(seconds=WATERMARK_FUTURE_SECONDS))
    upstream_until: datetime | None = None
    if upstream_fn is not None and cursor + MINUTE <= target_end:
        upstream_until = upstream_fn()
    budget_ms = 0  # これまでで最も長かった 1 区間の処理時間
    count = 0
    start = cursor
    while start + MINUTE <= target_end and count < max_minutes:
        end = start + MINUTE
        upstream_done = False
        if upstream_fn is not None:
            # 上流(動画の退避)がこの区間を確定させたか。確定させていれば、上流がウォーターマークで判定済み
            late_s = (to_utc(now()) - to_utc(end)).total_seconds()
            if upstream_until is not None and upstream_until >= end:
                upstream_done = True
            elif late_s >= upstream_max_wait:
                summary.setdefault("upstream_late", []).append(iso(start))
                log("minute_upstream_late", camera_id=camera_id, kind=p, start=iso(start), late_seconds=int(late_s),
                    upstream_until=iso(upstream_until) if upstream_until else None)
            else:
                summary["stopped"] = "waiting_upstream"
                log("minute_waiting_upstream", camera_id=camera_id, kind=p, start=iso(start),
                    upstream_until=iso(upstream_until) if upstream_until else None, late_seconds=int(late_s),
                    upstream_max_wait_seconds=upstream_max_wait)
                return
        if watermark_fn is not None and not upstream_done:
            # 3. 区間の断片が出そろったか(ウォーターマーク)、待つ上限を過ぎたか
            late_s = (to_utc(now()) - to_utc(end)).total_seconds()
            if wm is not None and wm >= epoch_ms(end):
                pass
            elif late_s >= max_late_seconds:
                summary.setdefault("max_late", []).append(iso(start))
                log("minute_max_late", camera_id=camera_id, kind=p, start=iso(start), late_seconds=int(late_s),
                    watermark=_iso_ms(wm))
            else:
                summary["stopped"] = "waiting"
                log("minute_waiting", camera_id=camera_id, kind=p, start=iso(start), watermark=_iso_ms(wm),
                    late_seconds=int(late_s), max_late_seconds=max_late_seconds)
                return
        rem = remaining_ms()
        need = max(min_remaining_ms, int(budget_ms * 1.5))
        if rem is not None and rem < need:
            summary["stopped"] = "time"
            log("stop_time", camera_id=camera_id, remaining_ms=rem, need_ms=need)
            return
        t0 = time.monotonic()
        rec = _process_one(
            tbl=tbl,
            p=p,
            camera_id=camera_id,
            start=start,
            end=end,
            process_minute=process_minute,
            max_attempts=max_attempts,
            days=days,
            now=now,
            kvs_retention_seconds=kvs_retention_seconds,
            expiry_margin_seconds=expiry_margin_seconds,
        )
        took = int((time.monotonic() - t0) * 1000)
        summary["minutes"].append(rec)
        if not rec.get("expired"):
            # 保持期間を過ぎた区間は FAILED を書くだけなので、件数の上限と所要時間の見積もりに数えない
            count += 1
            budget_ms = max(budget_ms, took)
        if rec["status"] is None:
            # 確定しなかった: カーソルはここで止め、次の起動で再挑戦
            summary["stopped"] = "not_ready" if rec.get("not_ready") else "retry"
            return
        # 4. 確定したのでカーソルを進める(自分がリースを持っているときだけ)
        try:
            tbl.update_item(
                Key={"camera_id": camera_id, "sk": cursor_sk},
                UpdateExpression="SET processed_until = :pu",
                ConditionExpression=Attr("lease_owner").eq(owner),
                ExpressionAttributeValues={":pu": iso(end)},
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                summary["stopped"] = "lease_lost"
                log("lease_lost", camera_id=camera_id, start=iso(start))
                return
            raise
        summary["processed_until"] = iso(end)
        start = end
    summary["stopped"] = "max_minutes" if count >= max_minutes and start + MINUTE <= target_end else "caught_up"


def _process_one(
    *,
    tbl: Any,
    p: str,
    camera_id: str,
    start: datetime,
    end: datetime,
    process_minute: ProcessMinute,
    max_attempts: int,
    days: int,
    now: Callable[[], datetime],
    kvs_retention_seconds: int,
    expiry_margin_seconds: int,
) -> dict:
    """1 区間を処理して区間の件を書く。戻り値の status が None なら未確定(再試行待ち)。"""
    sk = iso(start)
    key = {"camera_id": camera_id, "sk": sk}
    expires_at = int((to_utc(start) + timedelta(days=days)).timestamp())
    status_attr, attempts_attr, dur_attr, err_attr = f"{p}_status", f"{p}_attempts", f"{p}_duration_ms", f"{p}_error"

    # KVS の保持期限: 区間の終わりが (現在 − 保持期間) 以前なら「過ぎた」、
    # 区間の始まりが (現在 − 保持期間 + 余裕) 以前なら「近い」
    t_now = now()
    expired = to_utc(end) <= t_now - timedelta(seconds=kvs_retention_seconds)
    near_expiry = to_utc(start) <= t_now - timedelta(seconds=kvs_retention_seconds - expiry_margin_seconds)

    rec: dict[str, Any] = {"start": sk, "attempt": 0}
    attempt = 0

    def finalize_failed(reason: str, duration_ms: int) -> None:
        tbl.update_item(
            Key=key,
            UpdateExpression=f"SET {status_attr} = :s, {err_attr} = :err, {dur_attr} = :d, expires_at = :e",
            ExpressionAttributeValues={":s": "FAILED", ":err": reason[:1000], ":d": duration_ms, ":e": expires_at},
        )
        rec.update(status="FAILED", error=reason, duration_ms=duration_ms)
        log("minute_failed", camera_id=camera_id, kind=p, start=sk, attempt=attempt, error=reason)

    if expired:
        rec["expired"] = True
        finalize_failed("expired: KVS の保持期間を過ぎた", 0)
        return rec

    # 試行回数を処理の前に足す(タイムアウトやメモリ不足で落ちても回数が残る)
    resp = tbl.update_item(
        Key=key,
        UpdateExpression=f"SET expires_at = :e ADD {attempts_attr} :one",
        ExpressionAttributeValues={":e": expires_at, ":one": 1},
        ReturnValues="UPDATED_NEW",
    )
    attempt = int(resp["Attributes"][attempts_attr])
    rec["attempt"] = attempt
    if attempt > max_attempts:
        # 前回までの試行がタイムアウト等で記録されずに落ちたケース
        finalize_failed(f"max_attempts: {attempt - 1} 回試行済み", 0)
        return rec

    t0 = time.monotonic()
    try:
        result = process_minute(camera_id, start, end)
        if result.status not in ("DONE", "EMPTY", "FAILED"):
            raise ValueError(f"invalid status from process_minute: {result.status!r}")
    except NotReady as e:
        tbl.update_item(
            Key=key,
            UpdateExpression=f"ADD {attempts_attr} :m",
            ExpressionAttributeValues={":m": -1},
        )
        rec.update(status=None, not_ready=str(e), attempt=attempt - 1)
        log("minute_not_ready", camera_id=camera_id, kind=p, start=sk, reason=str(e))
        return rec
    except Exception as e:  # noqa: BLE001 - 区間ごとの失敗はすべて捕まえて記録する
        duration_ms = int((time.monotonic() - t0) * 1000)
        reason = _err_str(e)
        if attempt >= max_attempts or near_expiry:
            why = "max_attempts" if attempt >= max_attempts else "near_expiry"
            finalize_failed(f"{why}: {reason}", duration_ms)
            return rec
        tbl.update_item(
            Key=key,
            UpdateExpression=f"SET {err_attr} = :err, {dur_attr} = :d",
            ExpressionAttributeValues={":err": reason, ":d": duration_ms},
        )
        rec.update(status=None, error=reason, duration_ms=duration_ms)
        log("minute_retry", camera_id=camera_id, kind=p, start=sk, attempt=attempt, error=reason, duration_ms=duration_ms)
        return rec

    duration_ms = int((time.monotonic() - t0) * 1000)
    attrs = to_dynamo(dict(result.attributes))
    for reserved in ("camera_id", "sk", status_attr, attempts_attr, dur_attr, "expires_at"):
        attrs.pop(reserved, None)
    names: dict[str, str] = {}
    values: dict[str, Any] = {":s": result.status, ":d": duration_ms, ":e": expires_at}
    sets = [f"{status_attr} = :s", f"{dur_attr} = :d", "expires_at = :e"]
    for i, (k, v) in enumerate(attrs.items()):
        names[f"#a{i}"] = k
        values[f":a{i}"] = v
        sets.append(f"#a{i} = :a{i}")
    kwargs: dict[str, Any] = {
        "Key": key,
        # 前回の失敗の記録を消す(process_minute が自分で書く場合は消さない)
        "UpdateExpression": "SET " + ", ".join(sets) + ("" if err_attr in attrs else f" REMOVE {err_attr}"),
        "ExpressionAttributeValues": values,
    }
    if names:
        kwargs["ExpressionAttributeNames"] = names
    tbl.update_item(**kwargs)
    rec.update(status=result.status, duration_ms=duration_ms)
    log("minute_done", camera_id=camera_id, kind=p, start=sk, status=result.status, attempt=attempt, duration_ms=duration_ms, max_rss_mb=max_rss_mb())
    return rec


def _upstream_cursor(tbl: Any, camera_id: str, upstream_kind: str, summary: dict) -> datetime | None:
    """上流の処理済み位置(なければ None)。強い整合性で読む。"""
    it = tbl.get_item(Key={"camera_id": camera_id, "sk": f"CURSOR#{upstream_kind}"}, ConsistentRead=True).get("Item")
    v = parse_iso(it["processed_until"]) if it and it.get("processed_until") else None
    summary["upstream_processed_until"] = iso(v) if v else None
    return v


def _iso_ms(ms: int | None) -> str | None:
    if ms is None:
        return None
    return from_epoch_ms(ms).strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


def _watermark(stream: str, start: datetime, end: datetime, list_fn: Callable[..., list], summary: dict) -> int | None:
    """カーソル以降の断片を取り、キャッシュに置いてウォーターマーク(epoch ミリ秒)を返す。

    失敗したら None(区間は max_late_seconds を過ぎるまで待つ)。
    """
    try:
        frags = list_fn(stream, start, end)
    except Exception as e:  # noqa: BLE001 - 取れなければ待つだけ
        summary["watermark_error"] = _err_str(e)
        log("watermark_error", stream=stream, error=_err_str(e))
        return None
    kvs.set_fragment_cache(stream, start, end, frags)
    wm = kvs.watermark_ms(frags)
    summary["watermark"] = _iso_ms(wm)
    summary["watermark_fragments"] = len(frags)
    return wm
