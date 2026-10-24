import pytest

from cost import desk
from cost.model import CATEGORIES, GB, compute
from cost.prices import PriceBook

BOOK = PriceBook.load()


def line(lines, label):
    return next(x for x in lines if x.label == label)


def test_ingest_1mbps():
    lines = desk.build_lines(desk.DeskParams(bitrate_mbps=1.0))
    ingest = line(lines, "KVS 取り込み")
    # 1 Mbps × 30 日 = 324 × 10^9 バイト(構想.md の「約 324 GB」)= 301.7 GiB
    assert ingest.qty * GB == pytest.approx(324e9)
    assert ingest.qty == pytest.approx(301.74, abs=0.01)


def test_us_east_example_reproduced():
    """構想.md の試算例: 1 Mbps、US East の $0.0085/GB で約 $2.75(10^9 バイト = 1 GB として)。"""
    lines = desk.build_lines(desk.DeskParams(bitrate_mbps=1.0))
    gb_decimal = line(lines, "KVS 取り込み").qty * GB / 1e9
    assert gb_decimal * 0.0085 == pytest.approx(2.75, abs=0.01)


def test_every_category_has_lines():
    lines = desk.build_lines(desk.DeskParams())
    r = compute(lines, BOOK, 1)
    for c in CATEGORIES:
        assert any(x.line.category == c for x in r.lines), c
        assert r.by_category[c] > 0, c


def test_bitrate_scales_ingest_and_storage():
    a = compute(desk.build_lines(desk.DeskParams(bitrate_mbps=1.0)), BOOK, 1)
    b = compute(desk.build_lines(desk.DeskParams(bitrate_mbps=2.0)), BOOK, 1)
    assert b.by_category["取り込み"] == pytest.approx(2 * a.by_category["取り込み"])
    assert b.by_category["一時保持"] == pytest.approx(2 * a.by_category["一時保持"])
    assert b.by_category["加工・保存"] > a.by_category["加工・保存"]


def test_detect_interval():
    lines5 = desk.build_lines(desk.DeskParams(detect_interval_s=5))
    assert line(lines5, "KVS GetImages(推論用)").qty == pytest.approx((12 + 1) * 43200)
    lines0 = desk.build_lines(desk.DeskParams(detect_interval_s=0))
    assert not any(x.category == "物体検出" for x in lines0)


def test_search_is_fixed_and_model_switch():
    p = desk.DeskParams(questions_per_month=100, bedrock_model_id="jp.anthropic.claude-haiku-4-5-20251001-v1:0")
    lines = desk.build_lines(p)
    r1, r100 = compute(lines, BOOK, 1), compute(lines, BOOK, 100)
    # Bedrock のトークンは台数によらない
    bed1 = sum(c.cost for c in r1.lines if c.line.price_key.startswith("bedrock."))
    bed100 = sum(c.cost for c in r100.lines if c.line.price_key.startswith("bedrock."))
    assert bed1 == pytest.approx(bed100)
    assert bed1 == pytest.approx(0.8 * 1.1 + 0.06 * 5.5)


def test_recording_ratio_reduces_ingest():
    full = compute(desk.build_lines(desk.DeskParams()), BOOK, 1)
    half = compute(desk.build_lines(desk.DeskParams(recording_ratio=0.5)), BOOK, 1)
    assert half.by_category["取り込み"] == pytest.approx(full.by_category["取り込み"] / 2)


def test_x86_architecture_uses_x86_price():
    lines = desk.build_lines(desk.DeskParams(architecture="x86_64"))
    assert line(lines, "Lambda detector 実行").price_key == "lambda.gb_s.x86_64"
