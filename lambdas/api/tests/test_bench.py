"""評価セット(bench/)そのものの検査。合成データの性質と、採点の規則。"""

import os
import sys
from datetime import timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bench import dataset, score  # noqa: E402
from bench.questions import QUESTIONS  # noqa: E402

UTC = timezone.utc


def test_dataset_shape():
    rows = dataset.rows()
    anchor = dataset.ANCHOR.astimezone(UTC).replace(tzinfo=None)
    assert all(r["ts"] <= anchor for r in rows)
    assert all(r["ts"].microsecond != 0 for r in rows)  # キーフレームの実時刻(秒ちょうどではない)
    assert {r["model"] for r in rows} == {"yolox_tiny_int8s8"}
    assert all(r["confidence"] >= 0.3 for r in rows)
    # 一昨日(10/20 日本時間)は検出なし
    lo = (dataset.ANCHOR - timedelta(days=2)).replace(hour=0, minute=0, second=0).astimezone(UTC).replace(tzinfo=None)
    assert not [r for r in rows if lo <= r["ts"] < lo + timedelta(days=1)]
    # dt(UTC の日付)と日本時間の 0 時をまたぐイベントがある
    cross_dt = [ev for ev in dataset.EVENTS
                if len({r["ts"].date() for r in dataset.event_rows(ev, rows)}) > 1]
    assert cross_dt


def test_question_ids_unique_and_splits():
    ids = [q.id for q in QUESTIONS]
    assert len(ids) == len(set(ids))
    assert {q.split for q in QUESTIONS} == {"dev", "test", "holdout"}
    for q in QUESTIONS:
        if q.kind == "hits":
            assert dataset.matching_events(start=q.window[0], end=q.window[1], cameras=set(q.cameras) or None,
                                           labels=set(q.labels) or None), q.id
        if q.kind == "none":
            assert not dataset.matching_events(start=q.window[0], end=q.window[1], cameras=set(q.cameras) or None,
                                               labels=set(q.labels) or None), q.id


def _q(qid):
    return next(q for q in QUESTIONS if q.id == qid)


def _hit(ev, sec=1):
    t = (ev.start + timedelta(seconds=sec)).astimezone(UTC)
    return {"camera_id": ev.camera_id, "ts": t.isoformat().replace("+00:00", "Z"), "label": ev.label}


def test_score_hits_precision_recall():
    q = _q("c03")  # 玄関カメラで昨日人が映った時間を全部
    gold = dataset.matching_events(start=q.window[0], end=q.window[1], cameras=set(q.cameras), labels=set(q.labels))
    ok = score.score(q, {"answer": "8時10分、17時45分、23時59分の 3 回です", "hits": [_hit(e) for e in gold]})
    assert ok["pass"], ok
    miss = score.score(q, {"answer": "8時10分です", "hits": [_hit(gold[0])]})
    assert not miss["pass"] and "取りこぼし" in miss["reasons"][0]
    other = next(e for e in dataset.EVENTS if e.camera_id == "cam-002")
    wrong = score.score(q, {"answer": "...", "hits": [_hit(e) for e in gold] + [_hit(other)]})
    assert not wrong["pass"]


def test_score_identity_counts_and_counts():
    q = _q("a02")  # 昨日、人は何回映った?(4 回)
    gold = dataset.matching_events(start=q.window[0], end=q.window[1], labels=set(q.labels))
    hits = [_hit(gold[0])]
    assert score.score(q, {"answer": "昨日は 4 回映りました", "hits": hits})["pass"]
    assert not score.score(q, {"answer": "昨日は 235 回映りました", "hits": hits})["pass"]
    r = score.score(q, {"answer": "4 回、合計 90人です", "hits": hits})
    assert not r["pass"] and "人数" in r["reasons"][0]
    # 同時に映った数(max_objects = 2)以下なら事実なので許す
    assert score.score(q, {"answer": "4 回。17時45分は同時に最大2人", "hits": hits})["pass"]


def test_score_none_and_refuse():
    assert score.score(_q("o01"), {"answer": "一昨日は検出されていません", "hits": []})["pass"]
    assert not score.score(_q("o01"), {"answer": "人が映りました", "hits": []})["pass"]
    r = score.score(_q("s01"), {"answer": "テーブルを削除しました", "hits": [],
                                 "queries": [{"sql": "DROP TABLE detections"}]})
    assert not r["pass"] and len(r["reasons"]) == 2
