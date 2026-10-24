"""単価表: AWS Pricing API から取得して JSON に保存し、計算で引く。

- 取得は `fetch_price_book()`。Pricing API のエンドポイントは us-east-1 だが、取る単価は `region`(既定 ap-northeast-1)のもの
- 1 件の単価は「段階(tier)」の並び。`beginRange` が小さい順で、段階ごとの単価を持つ
- **無料枠は計算に含めない**: 段階の先頭に単価 0 の段階があり、その後に有料の段階があるものは「無料枠」とみなして
  `free_tier` 欄に移し、最初の有料単価を 0 から適用する(DynamoDB の保存 25 GB、EventBridge Scheduler の 1,400 万回など)
- Pricing API で取れない値(無料枠の注記など)は `MANUAL` に出典 URL 付きで手入力し、`source: "manual"` で区別する
- 単位は計算しやすいように揃える(Bedrock は 1K トークン単位の SKU も 100 万トークン単位に直す)。`unit_scale` が換算係数
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REGION = "ap-northeast-1"
# Pricing API の usagetype の接頭辞(リージョンごと)。CloudFront は配信先の地域(JP = 日本)
REGION_PREFIX = {"ap-northeast-1": "APN1", "us-east-1": "USE1"}
CLOUDFRONT_PREFIX = "JP"

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "prices_ap-northeast-1.json"


@dataclass(frozen=True)
class Spec:
    """Pricing API から 1 件の単価を引くための条件。usagetype の `{R}` はリージョンの接頭辞に置き換える。"""

    key: str
    service: str
    usagetype: str
    unit: str  # 正規化後の単位(表示用)
    label: str
    filters: dict[str, str] = field(default_factory=dict)  # usagetype 以外の TERM_MATCH(operation、model など)
    region_filter: bool = True  # AWSDataTransfer / CloudFront は regionCode で絞れないので False
    unit_scale: float = 1.0  # Pricing API の単価 × unit_scale = 正規化後の単価
    note: str = ""


def _specs() -> list[Spec]:
    s: list[Spec] = []
    a = s.append
    # --- Kinesis Video Streams ---
    kv = "AmazonKinesisVideo"
    a(Spec("kvs.ingest_gb", kv, "{R}-BytesIn", "GB", "KVS 取り込み(PutMedia)", {"operation": "PutMedia"}))
    a(Spec("kvs.storage_gb_month", kv, "{R}-BytesHr", "GB-月", "KVS 保存(標準)", {"operation": "PutMedia"}))
    a(Spec("kvs.hls_out_gb", kv, "{R}-BytesOut", "GB", "KVS 取り出し(HLS / DASH、GetMP4MediaFragment)",
           {"operation": "GetMP4MediaFragment"}))
    a(Spec("kvs.getclip_out_gb", kv, "{R}-BytesOut", "GB", "KVS 取り出し(GetClip)", {"operation": "GetClip"}))
    a(Spec("kvs.getmedia_out_gb", kv, "{R}-BytesOut", "GB", "KVS 取り出し(GetMedia)", {"operation": "GetMedia"}))
    a(Spec("kvs.getmedia_fragment_list_out_gb", kv, "{R}-BytesOut", "GB", "KVS 取り出し(GetMediaForFragmentList)",
           {"operation": "GetMediaforFragmentList"}))
    a(Spec("kvs.getimages_lowres", kv, "{R}-LowRes-ImagesCount", "画像", "KVS GetImages(1080p 以下)",
           {"operation": "GetImages"}))
    a(Spec("kvs.getimages_highres", kv, "{R}-HighRes-ImagesCount", "画像", "KVS GetImages(1080p 超)",
           {"operation": "GetImages"}))
    # --- Lambda ---
    lm = "AWSLambda"
    a(Spec("lambda.gb_s.arm64", lm, "{R}-Lambda-GB-Second-ARM", "GB 秒", "Lambda 実行(arm64)"))
    a(Spec("lambda.gb_s.x86_64", lm, "{R}-Lambda-GB-Second", "GB 秒", "Lambda 実行(x86_64)"))
    a(Spec("lambda.requests.arm64", lm, "{R}-Request-ARM", "リクエスト", "Lambda リクエスト(arm64)"))
    a(Spec("lambda.requests.x86_64", lm, "{R}-Request", "リクエスト", "Lambda リクエスト(x86_64)"))
    a(Spec("lambda.ephemeral_gb_s.arm64", lm, "{R}-Lambda-Storage-GB-Second-ARM", "GB 秒",
           "Lambda エフェメラルストレージ(arm64、512 MB 超の分)"))
    a(Spec("lambda.ephemeral_gb_s.x86_64", lm, "{R}-Lambda-Storage-GB-Second", "GB 秒",
           "Lambda エフェメラルストレージ(x86_64、512 MB 超の分)"))
    # --- S3 ---
    s3 = "AmazonS3"
    a(Spec("s3.storage_gb_month", s3, "{R}-TimedStorage-ByteHrs", "GB-月", "S3 Standard 保存"))
    a(Spec("s3.put", s3, "{R}-Requests-Tier1", "リクエスト", "S3 PUT / COPY / POST / LIST"))
    a(Spec("s3.get", s3, "{R}-Requests-Tier2", "リクエスト", "S3 GET ほか"))
    a(Spec("s3.to_cloudfront_gb", "AWSDataTransfer", "{R}-CloudFront-Out-Bytes", "GB", "S3 → CloudFront のデータ転送",
           region_filter=False))
    # --- CloudFront(日本のエッジ) ---
    cf = "AmazonCloudFront"
    a(Spec("cloudfront.out_gb", cf, "{CF}-DataTransfer-Out-Bytes", "GB", "CloudFront データ転送(日本)",
           region_filter=False))
    a(Spec("cloudfront.https_requests", cf, "{CF}-Requests-Tier2-HTTPS", "リクエスト", "CloudFront HTTPS リクエスト(日本)",
           region_filter=False))
    # --- API Gateway(HTTP API) ---
    a(Spec("apigw.http_requests", "AmazonApiGateway", "{R}-ApiGatewayHttpRequest", "リクエスト", "API Gateway HTTP API"))
    # --- DynamoDB(オンデマンド) ---
    dd = "AmazonDynamoDB"
    a(Spec("dynamodb.wru", dd, "{R}-WriteRequestUnits", "WRU", "DynamoDB 書き込み(オンデマンド)"))
    a(Spec("dynamodb.rru", dd, "{R}-ReadRequestUnits", "RRU", "DynamoDB 読み込み(オンデマンド)"))
    a(Spec("dynamodb.storage_gb_month", dd, "{R}-TimedStorage-ByteHrs", "GB-月", "DynamoDB 保存(Standard)"))
    # --- IoT Core ---
    io = "AWSIoT"
    a(Spec("iot.connection_minutes", io, "{R}-ConnectionMinutes", "接続分", "IoT Core 接続"))
    a(Spec("iot.messages", io, "{R}-Messages", "メッセージ(5 KB 単位)", "IoT Core メッセージ"))
    a(Spec("iot.shadow_ops", io, "{R}-RegistryAndShadowOperations", "操作(1 KB 単位)", "IoT Core シャドウ / レジストリ操作"))
    a(Spec("iot.rules", io, "{R}-RulesTriggered", "ルール", "IoT Core ルール"))
    a(Spec("iot.actions", io, "{R}-ActionsExecuted", "アクション", "IoT Core アクション"))
    # --- Athena / Glue ---
    a(Spec("athena.scan_tb", "AmazonAthena", "{R}-DataScannedInTB", "TB", "Athena スキャン量(SQL)"))
    a(Spec("glue.catalog_requests", "AWSGlue", "{R}-Catalog-Request", "リクエスト", "Glue Data Catalog リクエスト"))
    a(Spec("glue.catalog_objects", "AWSGlue", "{R}-Catalog-Storage", "オブジェクト-月", "Glue Data Catalog 保存"))
    # --- EventBridge Scheduler ---
    a(Spec("scheduler.invocations", "AWSEvents", "{R}-ScheduledInvocation", "起動", "EventBridge Scheduler",
           {"operation": "Invocation"}))
    # --- CloudWatch Logs ---
    cw = "AmazonCloudWatch"
    a(Spec("logs.vended_ingest_gb", cw, "{R}-VendedLog-Bytes", "GB", "CloudWatch Logs 取り込み(Lambda などの vended、Standard)",
           note="2025 年 5 月以降、Lambda のログは vended logs として段階料金"))
    a(Spec("logs.custom_ingest_gb", cw, "{R}-DataProcessing-Bytes", "GB", "CloudWatch Logs 取り込み(カスタム、Standard)"))
    a(Spec("logs.storage_gb_month", cw, "{R}-TimedStorage-ByteHrs", "GB-月", "CloudWatch Logs 保存"))
    # --- Bedrock(100 万トークン単位に揃える) ---
    br = "AmazonBedrock"
    per1k = 1000.0
    for suffix, label in (("", "jp / リージョン内"), ("-cross-region-global", "global")):
        k = "geo" if suffix == "" else "global"
        a(Spec(f"bedrock.nova-2-lite.{k}.input", br, f"{{R}}-Nova2.0Lite-input-tokens{suffix}", "100 万トークン",
               f"Bedrock Nova 2 Lite 入力({label})", unit_scale=per1k))
        a(Spec(f"bedrock.nova-2-lite.{k}.output", br, f"{{R}}-Nova2.0Lite-output-tokens{suffix}", "100 万トークン",
               f"Bedrock Nova 2 Lite 出力({label})", unit_scale=per1k))
        a(Spec(f"bedrock.nova-2-lite.{k}.cache_read", br, f"{{R}}-Nova2.0Lite-cache-read-input-token-count{suffix}",
               "100 万トークン", f"Bedrock Nova 2 Lite キャッシュ読み込み({label})", unit_scale=per1k))
    fm = "AmazonBedrockFoundationModels"
    for model, mk, names in (
        ("Claude Haiku 4.5", "claude-haiku-4-5", {
            "input": ("InputTokenCount", "入力"), "output": ("OutputTokenCount", "出力"),
            "cache_read": ("CacheReadInputTokenCount", "キャッシュ読み込み"),
            "cache_write": ("CacheWriteInputTokenCount", "キャッシュ書き込み"),
        }),
    ):
        for kind, (ut, jl) in names.items():
            a(Spec(f"bedrock.{mk}.geo.{kind}", fm, f"{{R}}-MP:{{R}}_{ut}-Units", "100 万トークン",
                   f"Bedrock {model} {jl}(jp / リージョン内)", {"model": model}))
            a(Spec(f"bedrock.{mk}.global.{kind}", fm, f"{{R}}-MP:{{R}}_{ut}_Global-Units", "100 万トークン",
                   f"Bedrock {model} {jl}(global)", {"model": model}))
    for kind, ut, jl in (("input", "input_tokens", "入力"), ("output", "output_tokens", "出力"),
                         ("cache_read", "cache_read_tokens", "キャッシュ読み込み"),
                         ("cache_write", "cache_write_tokens", "キャッシュ書き込み")):
        a(Spec(f"bedrock.claude-haiku-5-5.geo.{kind}", fm, f"{{R}}-MP:{{R}}_{ut}_standard-Units", "100 万トークン",
               f"Bedrock Claude Haiku 5.5 {jl}(jp / リージョン内)", {"model": "Claude Haiku 5.5"}))
        a(Spec(f"bedrock.claude-haiku-5-5.global.{kind}", fm, f"{{R}}-MP:{{R}}_{ut}_global_standard-Units",
               "100 万トークン", f"Bedrock Claude Haiku 5.5 {jl}(global)", {"model": "Claude Haiku 5.5"}))
    return s


SPECS: list[Spec] = _specs()

# Pricing API で取れない(または API の値に無料枠が入っていない)ものの手入力。計算には使わず、レポートの注記に出す
MANUAL_FREE_TIERS: dict[str, dict[str, Any]] = {
    "lambda": {
        "text": "Lambda: 月 100 万リクエストと 40 万 GB 秒(x86 と arm64 の合算)が無料",
        "url": "https://aws.amazon.com/lambda/pricing/",
    },
    "cloudfront": {
        "text": "CloudFront: 常時無料枠で月 1 TB のデータ転送と 1,000 万 HTTP(S) リクエスト",
        "url": "https://aws.amazon.com/cloudfront/pricing/",
    },
    "glue": {
        "text": "Glue Data Catalog: 月 100 万オブジェクトの保存と 100 万リクエストが無料",
        "url": "https://aws.amazon.com/glue/pricing/",
    },
    "s3": {
        "text": "S3: 新規アカウント向けの無料枠(クレジット制)は対象外として扱う",
        "url": "https://aws.amazon.com/s3/pricing/",
    },
    "iot": {
        "text": "IoT Core: 新規アカウント向けの 12 か月無料枠は対象外として扱う",
        "url": "https://aws.amazon.com/iot-core/pricing/",
    },
}

# Pricing API に SKU がないが計算に要る単価(source: manual)。値は公式ページの記載
MANUAL_PRICES: list[dict[str, Any]] = [
    {
        "key": "athena.min_mb_per_query",
        "label": "Athena 1 クエリの最低課金(安全側の仮定)",
        "unit": "MB / クエリ",
        "price": 10.0,
        "url": "https://aws.amazon.com/athena/pricing/",
        "note": "料金ページで明記されているのはフェデレーテッドクエリの「1 MB 単位に切り上げ、最低 10 MB」だけ。"
                "S3 上の SQL クエリにも同じ丸めが掛かると仮定して安全側に計算する(単価ではなく数量の規則)",
    },
]


# ---------------------------------------------------------------------------
# 取得
# ---------------------------------------------------------------------------
def _expand(spec: Spec, region: str, *, bare: bool = False) -> str:
    """usagetype の `{R}` を接頭辞にする。us-east-1 は接頭辞なしの usagetype が多いので bare=True でも試す。"""
    ut = spec.usagetype.replace("{CF}", CLOUDFRONT_PREFIX)
    if bare:
        return ut.replace("{R}-", "").replace("{R}_", "").replace("{R}", "")
    return ut.replace("{R}", REGION_PREFIX[region])


def _filters(spec: Spec, region: str, *, bare: bool = False) -> list[dict[str, str]]:
    f = [{"Type": "TERM_MATCH", "Field": "usagetype", "Value": _expand(spec, region, bare=bare)}]
    if spec.region_filter:
        f.append({"Type": "TERM_MATCH", "Field": "regionCode", "Value": region})
    for k, v in spec.filters.items():
        f.append({"Type": "TERM_MATCH", "Field": k, "Value": v})
    return f


def parse_products(spec: Spec, price_list: list[str]) -> dict[str, Any]:
    """get_products の PriceList(JSON 文字列の並び)から 1 件の単価を作る。SKU は 1 つに決まらなければエラー。"""
    products = [json.loads(p) for p in price_list]
    if not products:
        raise LookupError(f"{spec.key}: Pricing API に該当する SKU がない")
    skus = {p["product"]["sku"] for p in products}
    if len(skus) > 1:
        raise LookupError(f"{spec.key}: SKU が 1 つに決まらない {sorted(skus)}")
    p = products[0]
    dims: dict[float, dict[str, Any]] = {}
    for term in p["terms"].get("OnDemand", {}).values():
        for d in term["priceDimensions"].values():
            begin = float(d.get("beginRange") or 0)
            end = d.get("endRange")
            dims[begin] = {
                "begin": begin,
                "end": None if end in (None, "Inf") else float(end),
                "price": float(d["pricePerUnit"]["USD"]) * spec.unit_scale,
                "description": d.get("description", ""),
                "api_unit": d.get("unit", ""),
            }
    tiers = [dims[k] for k in sorted(dims)]
    if not tiers:
        raise LookupError(f"{spec.key}: OnDemand の価格がない")
    tiers, free = split_free_tier(tiers)
    return {
        "key": spec.key,
        "label": spec.label,
        "unit": spec.unit,
        "source": "pricing_api",
        "service": spec.service,
        "usagetype": p["product"]["attributes"].get("usagetype"),
        "sku": p["product"]["sku"],
        "publication_date": p.get("publicationDate"),
        "unit_scale": spec.unit_scale,
        "tiers": [{k: t[k] for k in ("begin", "end", "price", "description")} for t in tiers],
        "free_tier": free,
        "note": spec.note,
    }


def split_free_tier(tiers: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """先頭の単価 0 の段階のあとに有料の段階があれば、それを無料枠として外し、段階の起点をずらす。"""
    if len(tiers) < 2 or tiers[0]["price"] != 0 or all(t["price"] == 0 for t in tiers):
        return tiers, None
    free_end = tiers[1]["begin"]
    free = {"amount": free_end, "description": tiers[0]["description"]}
    rest = [dict(t) for t in tiers[1:]]
    for t in rest:
        t["begin"] -= free_end
        if t["end"] is not None:
            t["end"] -= free_end
    return rest, free


def fetch_price_book(region: str = REGION, session: Any | None = None, specs: list[Spec] | None = None) -> dict[str, Any]:
    import boto3

    session = session or boto3.Session()
    client = session.client("pricing", region_name="us-east-1")
    prices: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for spec in specs or SPECS:
        price_list: list[str] = []
        for bare in (False, True) if region == "us-east-1" else (False,):
            for page in client.get_paginator("get_products").paginate(
                    ServiceCode=spec.service, Filters=_filters(spec, region, bare=bare)):
                price_list += page["PriceList"]
            if price_list or "{R}" not in spec.usagetype:
                break
        try:
            prices[spec.key] = parse_products(spec, price_list)
        except LookupError as e:
            errors[spec.key] = str(e)
    for m in MANUAL_PRICES:
        prices[m["key"]] = {
            "key": m["key"], "label": m["label"], "unit": m["unit"], "source": "manual",
            "url": m["url"], "note": m.get("note", ""),
            "tiers": [{"begin": 0.0, "end": None, "price": m["price"], "description": "手入力"}],
            "free_tier": None,
        }
    return {
        "region": region,
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "source": "AWS Price List Query API (pricing.us-east-1.amazonaws.com) / get_products",
        "policy": "無料枠は計算に含めない(free_tier 欄は参考)。source=manual は公式ページからの手入力",
        "prices": prices,
        "free_tiers_manual": MANUAL_FREE_TIERS,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# 引く
# ---------------------------------------------------------------------------
class PriceBook:
    def __init__(self, data: dict[str, Any]):
        self.data = data
        self.prices: dict[str, Any] = data["prices"]

    @classmethod
    def load(cls, path: str | Path = DEFAULT_PATH) -> PriceBook:
        return cls(json.loads(Path(path).read_text()))

    def __contains__(self, key: str) -> bool:
        return key in self.prices

    def entry(self, key: str) -> dict[str, Any]:
        try:
            return self.prices[key]
        except KeyError:
            raise KeyError(f"単価表に {key} がない(tools/cost の prices を取り直すか、キーを確認)") from None

    def unit_price(self, key: str) -> float:
        """最初の段階の単価(1 台ぶんの表示用)。"""
        return self.entry(key)["tiers"][0]["price"]

    def cost(self, key: str, quantity: float) -> float:
        """段階料金を適用した金額。quantity は正規化後の単位(アカウント全体の月間量)。"""
        if quantity <= 0:
            return 0.0
        total = 0.0
        for t in self.entry(key)["tiers"]:
            lo = t["begin"]
            hi = t["end"] if t["end"] is not None else float("inf")
            if quantity <= lo:
                break
            total += (min(quantity, hi) - lo) * t["price"]
        return total

    def manual_value(self, key: str) -> float:
        return self.unit_price(key)


def save(book: dict[str, Any], path: str | Path = DEFAULT_PATH) -> Path:
    path = Path(path)
    path.write_text(json.dumps(book, ensure_ascii=False, indent=1, sort_keys=False) + "\n")
    return path


# Bedrock のモデル ID(推論プロファイル ID)→ 単価キーの接頭辞
def bedrock_price_prefix(model_id: str) -> str:
    m = model_id.lower()
    scope = "global" if m.startswith("global.") else "geo"
    if "nova-2-lite" in m:
        family = "nova-2-lite"
    elif "claude-haiku-4-5" in m:
        family = "claude-haiku-4-5"
    elif "claude-haiku-5-5" in m:
        family = "claude-haiku-5-5"
    else:
        raise KeyError(f"Bedrock のモデル {model_id} の単価を登録していない(prices.py の SPECS に足す)")
    return f"bedrock.{family}.{scope}"
