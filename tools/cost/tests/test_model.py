import pytest

from cost import model as m
from cost.model import Line, compute
from cost.prices import PriceBook

BOOK = PriceBook({"prices": {
    "a": {"unit": "GB", "tiers": [{"begin": 0, "end": 100, "price": 1.0}, {"begin": 100, "end": None, "price": 0.5}]},
    "b": {"unit": "回", "tiers": [{"begin": 0, "end": None, "price": 2.0}]},
}})


def test_camera_and_fixed_scaling():
    lines = [Line("取り込み", "x", "b", 1.0, "camera"), Line("検索", "y", "b", 3.0, "fixed")]
    r1 = compute(lines, BOOK, 1)
    assert r1.total == pytest.approx(8.0)
    r10 = compute(lines, BOOK, 10)
    assert r10.by_category["取り込み"] == pytest.approx(20.0)
    assert r10.by_category["検索"] == pytest.approx(6.0)
    assert r10.per_camera == pytest.approx(2.6)
    assert r10.scale_split() == {"camera": pytest.approx(20.0), "fixed": pytest.approx(6.0)}


def test_tier_applied_on_total_and_allocated_by_quantity():
    # 同じ単価キーの 2 明細(合計 200 GB)。最初の 100 GB は $1、残りは $0.5 → $150 を量の比で配る
    lines = [Line("加工・保存", "media", "a", 150.0), Line("検出結果保存", "det", "a", 50.0)]
    r = compute(lines, BOOK, 1)
    assert r.total == pytest.approx(150.0)
    assert r.lines[0].cost == pytest.approx(112.5)
    assert r.lines[1].cost == pytest.approx(37.5)
    assert r.lines[0].unit_price == pytest.approx(0.75)


def test_unknown_category():
    with pytest.raises(ValueError):
        Line("その他", "x", "a", 1)


def test_conversions():
    assert m.to_month(10, 86400) == pytest.approx(300)  # 1 日 10 → 30 日 300
    assert m.to_month(1, 3600) == pytest.approx(720)
    with pytest.raises(ValueError):
        m.to_month(1, 0)
    # 毎日 1 GiB ずつ増えて 30 日で消える → 平均 30 GB-月
    assert m.steady_state_gb_month(m.GB, 30) == pytest.approx(30)
    assert m.lambda_gb_seconds(512, 2000) == pytest.approx(1.0)
    assert m.lambda_ephemeral_gb_seconds(512, 1000) == 0
    assert m.lambda_ephemeral_gb_seconds(1024, 1000) == pytest.approx(0.5)
    assert m.dynamodb_write_units(100) == 1
    assert m.dynamodb_write_units(1024) == 1
    assert m.dynamodb_write_units(2500) == 3
    assert m.iot_message_units(400) == 1
    assert m.iot_message_units(5 * 1024 + 1) == 2
