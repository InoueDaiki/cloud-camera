"""時刻の変換。内部はすべて UTC の aware datetime で扱う。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .http import bad_request

UTC = timezone.utc
JST = ZoneInfo("Asia/Tokyo")


def now() -> datetime:
    return datetime.now(UTC)


def parse_ts(value: str, name: str = "time") -> datetime:
    """ISO 8601(Z または オフセット付き)か epoch ミリ秒を受け付ける。オフセットなしは UTC とみなす。"""
    if value is None or value == "":
        raise bad_request(f"{name} を指定してください")
    v = value.strip()
    if v.isdigit():
        return datetime.fromtimestamp(int(v) / 1000, UTC)
    if "T" in v and " " in v:
        # クエリ文字列で "+09:00" の "+" が空白になって届く場合を救う
        v = v.replace(" ", "+")
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError as e:
        raise bad_request(f"{name} の形式が不正です(ISO 8601 で指定): {value}") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def iso(dt: datetime, ms: bool = False) -> str:
    """2026-10-24T10:20:00Z 形式(ms=True ならミリ秒付き)。"""
    dt = dt.astimezone(UTC)
    if ms:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_auto(dt: datetime) -> str:
    """ミリ秒が 0 なら省き、あれば付ける。"""
    return iso(dt, ms=dt.microsecond != 0)


def floor_minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def ceil_minute(dt: datetime) -> datetime:
    f = floor_minute(dt)
    return f if f == dt else f + timedelta(minutes=1)


def sk_of(dt: datetime) -> str:
    """区間の件の SK(ミリ秒なし)。"""
    return iso(floor_minute(dt))


def from_ms(ms: int | float) -> datetime:
    return datetime.fromtimestamp(float(ms) / 1000, UTC)


def to_ms(dt: datetime) -> int:
    return int(round(dt.timestamp() * 1000))
