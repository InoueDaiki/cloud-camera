import json

import pytest

from cost import prices
from cost.prices import PriceBook, Spec, parse_products, split_free_tier


def product(sku: str, dims: list[tuple[str, str | None, str]], usagetype: str = "APN1-X") -> str:
    pd = {}
    for i, (begin, end, usd) in enumerate(dims):
        pd[f"{sku}.T.{i}"] = {"beginRange": begin, "endRange": end or "Inf", "pricePerUnit": {"USD": usd},
                              "unit": "GB", "description": f"tier {i}"}
    return json.dumps({
        "product": {"sku": sku, "attributes": {"usagetype": usagetype}},
        "publicationDate": "2026-10-01T00:00:00Z",
        "terms": {"OnDemand": {f"{sku}.T": {"priceDimensions": pd}}},
    })


SPEC = Spec("x", "Svc", "{R}-X", "GB", "テスト")


def test_parse_single_tier():
    e = parse_products(SPEC, [product("S1", [("0", None, "0.0109650000")])])
    assert e["sku"] == "S1" and e["source"] == "pricing_api"
    assert e["tiers"] == [{"begin": 0.0, "end": None, "price": 0.010965, "description": "tier 0"}]
    assert e["free_tier"] is None


def test_parse_tiers_sorted_and_free_tier_removed():
    # DynamoDB の保存: 最初の 25 GB が無料、その後 $0.285
    e = parse_products(SPEC, [product("S2", [("25", None, "0.285"), ("0", "25", "0")])])
    assert e["free_tier"]["amount"] == 25
    assert e["tiers"] == [{"begin": 0.0, "end": None, "price": 0.285, "description": "tier 0"}]


def test_zero_price_only_is_not_free_tier():
    # S3 → CloudFront の転送は単価そのものが 0(無料枠ではない)
    tiers, free = split_free_tier([{"begin": 0.0, "end": None, "price": 0.0, "description": ""}])
    assert free is None and tiers[0]["price"] == 0


def test_unit_scale_bedrock_per_1k_to_per_1m():
    spec = Spec("b", "AmazonBedrock", "{R}-Y", "100 万トークン", "b", unit_scale=1000.0)
    e = parse_products(spec, [product("S3", [("0", None, "0.000396")])])
    assert e["tiers"][0]["price"] == pytest.approx(0.396)


def test_ambiguous_sku_raises():
    with pytest.raises(LookupError):
        parse_products(SPEC, [product("A", [("0", None, "1")]), product("B", [("0", None, "2")])])
    with pytest.raises(LookupError):
        parse_products(SPEC, [])


def test_tiered_cost():
    book = PriceBook({"prices": {"s3": {"unit": "GB", "tiers": [
        {"begin": 0, "end": 51200, "price": 0.025},
        {"begin": 51200, "end": 512000, "price": 0.024},
        {"begin": 512000, "end": None, "price": 0.023},
    ]}}})
    assert book.cost("s3", 100) == pytest.approx(2.5)
    assert book.cost("s3", 60000) == pytest.approx(51200 * 0.025 + 8800 * 0.024)
    assert book.cost("s3", 0) == 0
    with pytest.raises(KeyError):
        book.cost("nope", 1)


def test_expand_usagetype():
    assert prices._expand(Spec("k", "s", "{R}-BytesIn", "GB", "l"), "ap-northeast-1") == "APN1-BytesIn"
    fm = Spec("k", "s", "{R}-MP:{R}_InputTokenCount-Units", "t", "l")
    assert prices._expand(fm, "ap-northeast-1") == "APN1-MP:APN1_InputTokenCount-Units"
    assert prices._expand(Spec("k", "s", "{R}-Requests-Tier1", "r", "l"), "us-east-1", bare=True) == "Requests-Tier1"
    assert prices._expand(Spec("k", "s", "{CF}-DataTransfer-Out-Bytes", "GB", "l"), "ap-northeast-1") == \
        "JP-DataTransfer-Out-Bytes"


def test_bedrock_prefix():
    assert prices.bedrock_price_prefix("jp.amazon.nova-2-lite-v1:0") == "bedrock.nova-2-lite.geo"
    assert prices.bedrock_price_prefix("global.amazon.nova-2-lite-v1:0") == "bedrock.nova-2-lite.global"
    assert prices.bedrock_price_prefix("jp.anthropic.claude-haiku-4-5-20251001-v1:0") == "bedrock.claude-haiku-4-5.geo"
    with pytest.raises(KeyError):
        prices.bedrock_price_prefix("jp.unknown-model")


def test_committed_price_file_is_complete():
    """リポジトリの単価表に、SPECS のキーがすべて入っていて、値が妥当な範囲にある。"""
    book = PriceBook.load()
    assert book.data["region"] == "ap-northeast-1"
    for s in prices.SPECS:
        assert s.key in book, s.key
        e = book.entry(s.key)
        assert e["source"] == "pricing_api" and e["sku"]
    for m in prices.MANUAL_PRICES:
        assert book.entry(m["key"])["source"] == "manual"
    # 2026-10-24 に取得した東京の単価(公式ページの US East の値とは別)
    assert 0.005 < book.unit_price("kvs.ingest_gb") < 0.05
    assert book.unit_price("s3.to_cloudfront_gb") == 0
    assert book.entry("scheduler.invocations")["free_tier"]  # 1,400 万回の無料枠は外してある
    assert book.unit_price("scheduler.invocations") > 0
