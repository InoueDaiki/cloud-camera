"""時刻の扱い。すべて UTC の aware datetime を使う。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

MINUTE = timedelta(minutes=1)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_utc(dt: datetime) -> datetime:
    """naive は UTC とみなし、aware は UTC に直す。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def floor_minute(dt: datetime) -> datetime:
    return to_utc(dt).replace(second=0, microsecond=0)


def iso(dt: datetime) -> str:
    """契約の時刻文字列(ミリ秒なし)。例: 2026-10-24T10:20:00Z"""
    return to_utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    return to_utc(datetime.fromisoformat(s.replace("Z", "+00:00")))


def epoch_ms(dt: datetime) -> int:
    return int(round(to_utc(dt).timestamp() * 1000))


def from_epoch_ms(ms: int | float) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def media_prefix(camera_id: str, start: datetime) -> str:
    """メディアバケットの区間ごとのキー接頭辞。例: cam-001/2026/10/24/10/20/"""
    return f"{camera_id}/{to_utc(start).strftime('%Y/%m/%d/%H/%M')}/"
