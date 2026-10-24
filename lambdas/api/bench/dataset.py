"""評価用の合成検出結果。

基準時刻(評価で「現在」とみなす時刻)は固定する(ANCHOR)。答えが毎回同じになるように、検出のまとまり(イベント)を
決め打ちで並べ、そこから 1 秒ごとのフレームの検出行を作る。

実データに合わせたこと:
- 1 行 = 1 フレームでの 1 検出。同じ物が 30 秒映れば約 30 行(人物の同定はしない)。1 フレームに 2 人なら 2 行
- ts はキーフレームの実時刻で、秒ちょうどではない(カメラごとの位相 0.5〜0.9 秒 + 揺らぎ)
- キーフレームが 2 秒おきの区間もある(step=2)
- model は yolox_tiny_int8s8、confidence は検出器のしきい値 0.3 以上
- 日本時間の 0 時(UTC の前日 15 時)と 9 時(UTC の 0 時 = dt の境目)をまたぐイベントがある
- 一昨日(10/20)は全カメラで検出なし
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
UTC = timezone.utc

# 評価の「現在」: 2026-10-22(木)14:35:20 日本時間
ANCHOR = datetime(2026, 10, 22, 14, 35, 20, tzinfo=JST)

MODEL = "yolox_tiny_int8s8"
CAMERAS = {"cam-001": "gw-001", "cam-002": "gw-001", "cam-003": "gw-002"}
CAMERA_NAMES = {"cam-001": "玄関", "cam-002": "駐車場", "cam-003": "庭"}
# キーフレームの位相(秒の中の位置)。カメラごとに固定
_PHASE = {"cam-001": 0.537, "cam-002": 0.712, "cam-003": 0.861}


@dataclass(frozen=True)
class Event:
    """1 つの検出のまとまり。start は日本時間。"""
    camera_id: str
    label: str
    start: datetime
    seconds: int
    objs: int = 1          # 1 フレームに映る数(行数が増える。人数として数えてはいけない)
    step: int = 1          # フレームの間隔(秒)
    conf: float = 0.8
    noise: bool = False    # 低信頼度の誤検出らしきもの(正解にも誤りにも数えない)

    @property
    def end(self) -> datetime:
        return self.start + timedelta(seconds=self.seconds)


def _j(m: int, d: int, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(2026, m, d, hh, mm, ss, tzinfo=JST)


EVENTS: list[Event] = [
    # --- 10/11(日)先々週。「先週」に入れてはいけない
    Event("cam-003", "dog", _j(10, 11, 11, 0, 5), 20),
    # --- 先週 10/12(月)〜10/18(日)
    Event("cam-003", "dog", _j(10, 14, 15, 0, 10), 40, step=2),
    Event("cam-001", "person", _j(10, 14, 19, 20, 0), 30),
    Event("cam-002", "car", _j(10, 16, 12, 10, 0), 30),
    Event("cam-002", "motorcycle", _j(10, 16, 12, 40, 3), 10),
    Event("cam-001", "person", _j(10, 17, 10, 0, 0), 60),
    Event("cam-003", "cat", _j(10, 17, 22, 5, 0), 20),
    Event("cam-002", "bicycle", _j(10, 18, 9, 45, 0), 15),
    Event("cam-002", "person", _j(10, 18, 9, 45, 0), 15),
    # --- 10/19(月)
    Event("cam-001", "person", _j(10, 19, 9, 15, 0), 20),
    Event("cam-002", "car", _j(10, 19, 8, 0, 0), 30),
    # --- 10/20(火)検出なし
    # --- 10/21(水)昨日
    Event("cam-002", "car", _j(10, 21, 2, 0, 0), 4, conf=0.35, noise=True),
    Event("cam-003", "dog", _j(10, 21, 7, 30, 20), 18),
    Event("cam-001", "person", _j(10, 21, 8, 10, 20), 30),
    Event("cam-002", "person", _j(10, 21, 12, 0, 0), 60),
    Event("cam-002", "car", _j(10, 21, 16, 20, 5), 30),
    Event("cam-003", "dog", _j(10, 21, 16, 50, 0), 30, step=2),
    Event("cam-002", "truck", _j(10, 21, 17, 10, 0), 20),
    Event("cam-001", "person", _j(10, 21, 17, 45, 10), 40, objs=2),
    Event("cam-002", "car", _j(10, 21, 18, 5, 40), 25),
    Event("cam-001", "person", _j(10, 21, 23, 59, 40), 45),   # 日本時間の 0 時をまたぐ
    # --- 10/22(木)今日(現在 14:35:20)
    Event("cam-001", "person", _j(10, 22, 3, 10, 0), 5, conf=0.33, noise=True),
    Event("cam-001", "person", _j(10, 22, 7, 15, 12), 30),
    Event("cam-002", "car", _j(10, 22, 7, 40, 0), 60),
    Event("cam-001", "person", _j(10, 22, 8, 59, 45), 40),     # 9 時(UTC の日付の境目)をまたぐ
    Event("cam-003", "dog", _j(10, 22, 9, 30, 0), 50, step=2),
    Event("cam-001", "person", _j(10, 22, 10, 2, 5), 20, objs=2),
    Event("cam-001", "person", _j(10, 22, 10, 40, 15), 60, objs=3),
    Event("cam-002", "car", _j(10, 22, 11, 5, 30), 45),
    Event("cam-002", "person", _j(10, 22, 11, 5, 50), 10),
    Event("cam-001", "person", _j(10, 22, 12, 30, 40), 15),
    Event("cam-003", "bird", _j(10, 22, 13, 15, 0), 8, conf=0.6),
    Event("cam-002", "bicycle", _j(10, 22, 13, 50, 10), 12),
    Event("cam-002", "person", _j(10, 22, 13, 50, 10), 12),
    Event("cam-001", "person", _j(10, 22, 14, 27, 3), 25),
    Event("cam-002", "car", _j(10, 22, 14, 31, 0), 20),
]


def rows(seed: int = 7) -> list[dict]:
    """EVENTS から検出行を作る。ts は UTC(naive、ミリ秒)。"""
    rnd = random.Random(seed)
    out = []
    for ev in EVENTS:
        phase = _PHASE[ev.camera_id]
        for s in range(0, ev.seconds, ev.step):
            jitter = rnd.uniform(-0.03, 0.03)
            t = ev.start + timedelta(seconds=s + phase + jitter)
            if t > ANCHOR:
                continue
            t = t.astimezone(UTC).replace(tzinfo=None)
            t = t.replace(microsecond=t.microsecond // 1000 * 1000)
            for k in range(ev.objs):
                x = min(0.85, 0.1 + 0.25 * k + 0.01 * s)
                out.append({
                    "camera_id": ev.camera_id, "ts": t, "label": ev.label,
                    "confidence": round(max(0.3, min(0.98, ev.conf + rnd.uniform(-0.08, 0.08))), 3),
                    "x": x, "y": 0.3 + rnd.uniform(-0.02, 0.02), "w": 0.12, "h": 0.35, "model": MODEL,
                })
    out.sort(key=lambda r: (r["ts"], r["camera_id"]))
    return out


def event_rows(ev: Event, all_rows: list[dict] | None = None) -> list[dict]:
    rs = all_rows if all_rows is not None else rows()
    lo = ev.start.astimezone(UTC).replace(tzinfo=None)
    hi = ev.end.astimezone(UTC).replace(tzinfo=None) + timedelta(seconds=1)
    return [r for r in rs if r["camera_id"] == ev.camera_id and r["label"] == ev.label and lo <= r["ts"] < hi]


def matching_events(*, start: datetime, end: datetime, cameras: set[str] | None = None,
                    labels: set[str] | None = None, include_noise: bool = False) -> list[Event]:
    """[start, end)(日本時間)と重なり、現在より前に始まったイベント。"""
    out = []
    for ev in EVENTS:
        if ev.noise and not include_noise:
            continue
        if cameras and ev.camera_id not in cameras:
            continue
        if labels and ev.label not in labels:
            continue
        if ev.start >= ANCHOR:
            continue
        if ev.start < end and ev.end > start:
            out.append(ev)
    return sorted(out, key=lambda e: (e.start, e.camera_id))
