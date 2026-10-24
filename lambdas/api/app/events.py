"""引数付きの検索ツール search_detections。

モデルには日本時間の範囲・ラベル・カメラだけを選ばせ、SQL(dt と ts の条件、まとまりへの集約)はここで組む。
自由 SQL で起きていた誤り(ts の条件の書き忘れ、dt の計算違い、検出行を回数として数える)を構造的に防ぐ。

「イベント」= 同じカメラ・同じラベルの検出が GAP_SECONDS 以内の間隔で続いたまとまり。人物の同定はしないので、
イベントの数は「映った回数」の目安であって人数・台数ではない。1 フレームに同時に映った数の最大(max_objects)は返す。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import athena
from .config import Config
from .prompt import COCO_LABELS
from .timeutil import JST, UTC, iso_auto

GAP_SECONDS = 60          # これより長く途切れたら別のイベント
MAX_RANGE_DAYS = 31
MAX_EVENTS_FETCH = 1000   # Athena から取るイベント数の上限
MAX_EVENTS_RETURN = 40    # モデルに返すイベント数の上限(多いときは先頭と末尾)
GROUPS = ("event", "hour", "day", "camera", "label")
_WEEKDAYS = "月火水木金土日"


class ToolInputError(ValueError):
    """引数の誤り。メッセージはモデルに返して直させる。"""


@dataclass
class Event:
    id: str
    camera_id: str
    label: str
    start: datetime
    end: datetime
    frames: int
    max_objects: int
    confidence: float


@dataclass
class SearchOutcome:
    payload: dict
    sql: str | None = None
    bytes_scanned: int = 0
    execution_ms: int = 0
    events: list[Event] = field(default_factory=list)


def parse_jst(value: object, name: str) -> datetime:
    """'YYYY-MM-DD HH:MM[:SS]'(日本時間)。'24:00' は翌日の 0 時として受け付ける。"""
    if not isinstance(value, str) or not value.strip():
        raise ToolInputError(f"{name} を 'YYYY-MM-DD HH:MM'(日本時間)で指定してください")
    v = value.strip().replace("T", " ")
    for suffix in ("+09:00", "Asia/Tokyo", "JST"):
        if v.endswith(suffix):
            v = v[: -len(suffix)].strip()
    extra = timedelta(0)
    if " 24:00" in v:
        v = v.replace(" 24:00", " 00:00")
        extra = timedelta(days=1)
    try:
        dt = datetime.fromisoformat(v)
    except ValueError as e:
        raise ToolInputError(f"{name} の形式が不正です: {value!r}('YYYY-MM-DD HH:MM'、日本時間)") from e
    if dt.tzinfo is not None:
        return dt.astimezone(UTC)
    return (dt.replace(tzinfo=JST) + extra).astimezone(UTC)


def _sql_str(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _utc_lit(dt: datetime) -> str:
    return "TIMESTAMP '" + dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d}'"


def build_sql(*, table: str, start: datetime, end: datetime, labels: list[str], cameras: list[str],
              min_confidence: float, gap_seconds: int = GAP_SECONDS, limit: int = MAX_EVENTS_FETCH) -> str:
    """[start, end)(UTC)の検出を、カメラ・ラベルごとのイベントにまとめる SQL。"""
    dt_lo = start.astimezone(UTC).date()
    dt_hi = (end.astimezone(UTC) - timedelta(milliseconds=1)).date()
    cond = [
        f"dt BETWEEN '{dt_lo.isoformat()}' AND '{dt_hi.isoformat()}'",
        f"ts >= {_utc_lit(start)}",
        f"ts < {_utc_lit(end)}",
        f"confidence >= {min_confidence:.2f}",
    ]
    if labels:
        cond.append("label IN (" + ", ".join(_sql_str(x) for x in labels) + ")")
    if cameras:
        cond.append("camera_id IN (" + ", ".join(_sql_str(x) for x in cameras) + ")")
    where = "\n    AND ".join(cond)
    return f"""WITH f AS (
  SELECT camera_id, label, ts, count(*) AS objs, max(confidence) AS conf
  FROM {table}
  WHERE {where}
  GROUP BY camera_id, label, ts
), g AS (
  SELECT camera_id, label, ts, objs, conf,
    CASE WHEN lag(ts) OVER (PARTITION BY camera_id, label ORDER BY ts) IS NULL
          OR ts > lag(ts) OVER (PARTITION BY camera_id, label ORDER BY ts) + INTERVAL '{gap_seconds}' SECOND
         THEN 1 ELSE 0 END AS new_ev
  FROM f
), e AS (
  SELECT camera_id, label, ts, objs, conf,
    sum(new_ev) OVER (PARTITION BY camera_id, label ORDER BY ts ROWS UNBOUNDED PRECEDING) AS ev
  FROM g
)
SELECT camera_id, label, min(ts) AS start_ts, max(ts) AS end_ts, count(*) AS frames,
  max(objs) AS max_objects, max(conf) AS confidence
FROM e
GROUP BY camera_id, label, ev
ORDER BY start_ts, camera_id, label
LIMIT {limit}"""


def _jst(dt: datetime, seconds: bool = True) -> str:
    j = dt.astimezone(JST)
    return f"{j:%Y-%m-%d}({_WEEKDAYS[j.weekday()]}) {j:%H:%M:%S}" if seconds else f"{j:%Y-%m-%d %H:%M}"


def _event_dict(ev: Event, names: dict[str, str]) -> dict:
    d = {
        "event_id": ev.id,
        "camera_id": ev.camera_id,
        "label": ev.label,
        "start_jst": _jst(ev.start),
        "end_jst": _jst(ev.end),
        "seconds": int(round((ev.end - ev.start).total_seconds())) + 1,
        "frames": ev.frames,
        "max_objects_in_frame": ev.max_objects,
        "confidence": round(ev.confidence, 3),
        "start_ts": iso_auto(ev.start),
    }
    if names.get(ev.camera_id):
        d["camera_name"] = names[ev.camera_id]
    return d


def _group_key(ev: Event, group_by: str, names: dict[str, str]) -> str:
    j = ev.start.astimezone(JST)
    if group_by == "hour":
        return f"{j:%Y-%m-%d} {j:%H}時台"
    if group_by == "day":
        return f"{j:%Y-%m-%d}({_WEEKDAYS[j.weekday()]})"
    if group_by == "camera":
        return f"{ev.camera_id}" + (f"({names[ev.camera_id]})" if names.get(ev.camera_id) else "")
    return ev.label


def summarize(events: list[Event], *, group_by: str, names: dict[str, str], start: datetime,
              end: datetime, now: datetime, notes: list[str]) -> dict:
    by_label: dict[str, int] = defaultdict(int)
    by_camera: dict[str, int] = defaultdict(int)
    for ev in events:
        by_label[ev.label] += 1
        by_camera[ev.camera_id] += 1
    payload: dict = {
        "range_jst": f"{_jst(start)} 〜 {_jst(end)}(終わりは含まない)",
        "event_count": len(events),
        "events_by_label": dict(by_label),
        "events_by_camera": dict(by_camera),
        "total_frames": sum(ev.frames for ev in events),
    }
    if events:
        payload["first_event"] = _event_dict(events[0], names)
        last = max(events, key=lambda e: e.start)
        payload["last_event"] = _event_dict(last, names)
        payload["last_frame_jst"] = _jst(max(e.end for e in events))
    if group_by != "event":
        groups: dict[str, dict] = {}
        for ev in events:
            g = groups.setdefault(_group_key(ev, group_by, names), {"events": 0, "frames": 0, "event_ids": []})
            g["events"] += 1
            g["frames"] += ev.frames
            if len(g["event_ids"]) < 5:
                g["event_ids"].append(ev.id)
        ranked = sorted(groups.items(), key=lambda kv: (-kv[1]["events"], -kv[1]["frames"], kv[0]))
        payload["groups"] = [{"key": k, **v} for k, v in ranked]
    shown = events
    if len(events) > MAX_EVENTS_RETURN:
        half = MAX_EVENTS_RETURN // 2
        shown = events[:half] + events[-half:]
        notes.append(f"イベントが {len(events)} 件あるので、先頭と末尾の {half} 件ずつだけ載せています。")
    payload["events"] = []
    edge = timedelta(seconds=GAP_SECONDS)
    for ev in shown:
        d = _event_dict(ev, names)
        # 範囲の端で切れている(範囲の前から続いていた/後も続いている)かもしれないイベント。
        # 範囲内に映っていたことに変わりはないので、回答から落とさない
        if ev.start - start < edge:
            d["may_continue_from_before_range"] = True
        if end - ev.end < edge and end < now:
            d["may_continue_after_range"] = True
        payload["events"].append(d)
    if not events:
        notes.append("この条件の検出はありません。")
    payload["notes"] = notes
    return payload


def run_search(cfg: Config, inp: dict, *, now: datetime, names: dict[str, str],
               timeout_s: float, next_event_no: int = 1) -> SearchOutcome:
    """search_detections の本体。引数を検査して SQL を組み、Athena で実行し、イベントにまとめる。"""
    start = parse_jst(inp.get("start"), "start")
    end = parse_jst(inp.get("end"), "end")
    if end <= start:
        raise ToolInputError("end は start より後にしてください(end は含まない)")
    if end - start > timedelta(days=MAX_RANGE_DAYS):
        raise ToolInputError(f"範囲は {MAX_RANGE_DAYS} 日以内にしてください")

    labels_raw = inp.get("labels") or []
    if isinstance(labels_raw, str):
        labels_raw = [labels_raw]
    labels = []
    for x in labels_raw:
        lab = str(x).strip().lower()
        if lab not in COCO_LABELS:
            raise ToolInputError(f"ラベル {x!r} は COCO の 80 クラスにありません(英語の小文字で、例: person, car, dog)")
        labels.append(lab)

    cams_raw = inp.get("camera_ids") or []
    if isinstance(cams_raw, str):
        cams_raw = [cams_raw]
    by_name = {v: k for k, v in names.items() if v}
    cameras, unknown = [], []
    for c in cams_raw:
        c = str(c).strip()
        cid = c if c in cfg.cameras else by_name.get(c)
        (cameras if cid else unknown).append(cid or c)
    if unknown:
        known = ", ".join(f"{k}({names[k]})" if names.get(k) else k for k in sorted(cfg.cameras))
        raise ToolInputError(f"存在しないカメラです: {', '.join(unknown)}。存在するカメラ: {known}。"
                             "該当するカメラがなければ、そのカメラはないと答えてください。")

    try:
        min_conf = float(inp.get("min_confidence", 0.5))
    except (TypeError, ValueError):
        min_conf = 0.5
    min_conf = min(max(min_conf, 0.0), 1.0)
    group_by = str(inp.get("group_by") or "event")
    if group_by not in GROUPS:
        raise ToolInputError(f"group_by は {', '.join(GROUPS)} のどれかです")

    notes: list[str] = []
    if start >= now:
        notes.append("指定の範囲はまだ来ていない未来の時刻です。データはありません(予測もできません)。")
        payload = summarize([], group_by=group_by, names=names, start=start, end=end, now=now, notes=notes)
        return SearchOutcome(payload=payload)
    if end > now:
        notes.append(f"範囲の終わりが現在({_jst(now)})より後なので、現在までを検索しました。")
        end = now

    sql = build_sql(table=cfg.glue_table, start=start, end=end, labels=labels, cameras=cameras,
                    min_confidence=min_conf)
    res = athena.run(cfg, sql, timeout_s=timeout_s, max_rows=MAX_EVENTS_FETCH)
    col = {c: i for i, c in enumerate(res.columns)}
    events: list[Event] = []
    for row in res.rows:
        s = athena.parse_timestamp(row[col["start_ts"]] or "")
        e = athena.parse_timestamp(row[col["end_ts"]] or "")
        if s is None or e is None:
            continue
        events.append(Event(
            id=f"E{next_event_no + len(events)}", camera_id=row[col["camera_id"]] or "",
            label=row[col["label"]] or "", start=s, end=e, frames=int(row[col["frames"]] or 0),
            max_objects=int(row[col["max_objects"]] or 0), confidence=float(row[col["confidence"]] or 0)))
    if len(res.rows) >= MAX_EVENTS_FETCH:
        notes.append(f"イベントが多すぎるため最初の {MAX_EVENTS_FETCH} 件までです。範囲を狭めてください。")
    payload = summarize(events, group_by=group_by, names=names, start=start, end=end, now=now, notes=notes)
    return SearchOutcome(payload=payload, sql=sql, bytes_scanned=res.bytes_scanned,
                         execution_ms=res.execution_ms, events=events)
