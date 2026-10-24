"""コストの明細と計算。

明細(`Line`)は「1 か月(30 日)あたりの使用量」と単価キーの組。机上計算(desk.py)も実測(measure.py)も
同じ明細の並びを作り、`compute()` が台数ごとの金額にする。

- `scale="camera"`: 台数に比例する(qty は 1 台あたり)
- `scale="fixed"`: 台数によらない(qty は全体の量。例: 質問の数で決まる Bedrock のトークン)
- 段階料金(S3 の 50 TB 超など)は、同じ単価キーの明細を合計した量に掛け、量の比で明細に配り戻す
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Literal

from .prices import PriceBook

DAYS_PER_MONTH = 30

# 構想.md「コスト検証の進め方」の表の項目(表示順)
CATEGORIES: list[str] = [
    "取り込み",
    "一時保持",
    "動画抽出",
    "加工・保存",
    "物体検出",
    "検出結果保存",
    "再生",
    "検索",
    "制御",
]

Source = Literal["実測", "推定", "机上"]


@dataclass
class Line:
    category: str
    label: str
    price_key: str
    qty: float  # 30 日あたり。scale=camera なら 1 台あたり
    scale: Literal["camera", "fixed"] = "camera"
    source: Source = "机上"
    basis: str = ""  # 量の根拠

    def __post_init__(self) -> None:
        if self.category not in CATEGORIES:
            raise ValueError(f"未知の項目: {self.category}")


@dataclass
class CostedLine:
    line: Line
    total_qty: float
    cost: float  # N 台ぶんの月額(USD)
    unit_price: float  # 段階を考慮した実効単価


@dataclass
class Result:
    cameras: int
    lines: list[CostedLine]
    by_category: dict[str, float] = field(default_factory=dict)
    total: float = 0.0

    @property
    def per_camera(self) -> float:
        return self.total / self.cameras if self.cameras else 0.0

    def scale_split(self) -> dict[str, float]:
        out = {"camera": 0.0, "fixed": 0.0}
        for c in self.lines:
            out[c.line.scale] += c.cost
        return out


def compute(lines: list[Line], prices: PriceBook, cameras: int = 1) -> Result:
    if cameras < 1:
        raise ValueError("cameras は 1 以上")
    totals = [ln.qty * cameras if ln.scale == "camera" else ln.qty for ln in lines]
    # 単価キーごとに量を合計して段階料金を掛ける
    by_key: dict[str, float] = defaultdict(float)
    for ln, q in zip(lines, totals, strict=True):
        by_key[ln.price_key] += max(q, 0.0)
    key_cost = {k: prices.cost(k, q) for k, q in by_key.items()}
    costed: list[CostedLine] = []
    for ln, q in zip(lines, totals, strict=True):
        kq = by_key[ln.price_key]
        if kq > 0:
            cost = key_cost[ln.price_key] * max(q, 0.0) / kq
            unit = key_cost[ln.price_key] / kq
        else:
            cost, unit = 0.0, prices.unit_price(ln.price_key)
        costed.append(CostedLine(ln, q, cost, unit))
    by_cat = {c: 0.0 for c in CATEGORIES}
    for c in costed:
        by_cat[c.line.category] += c.cost
    return Result(cameras, costed, by_cat, sum(by_cat.values()))


# ---------------------------------------------------------------------------
# 換算の小道具
# ---------------------------------------------------------------------------
GB = 1024**3  # AWS の課金の GB は GiB(2^30 バイト)
MINUTES_PER_MONTH = DAYS_PER_MONTH * 24 * 60
SECONDS_PER_MONTH = MINUTES_PER_MONTH * 60


def to_month(value: float, period_seconds: float) -> float:
    """期間 period_seconds の合計値を 30 日あたりに換算する。"""
    if period_seconds <= 0:
        raise ValueError("期間が 0 以下")
    return value * SECONDS_PER_MONTH / period_seconds


def steady_state_gb_month(bytes_per_day: float, retention_days: float) -> float:
    """毎日 bytes_per_day ずつ増え、retention_days で消える保存量の定常状態(平均保存量 = GB-月)。"""
    return bytes_per_day * retention_days / GB


def lambda_gb_seconds(memory_mb: float, duration_ms_total: float) -> float:
    return memory_mb / 1024 * duration_ms_total / 1000


def lambda_ephemeral_gb_seconds(ephemeral_mb: float, duration_ms_total: float) -> float:
    """512 MB を超えた分だけ課金される。"""
    return max(ephemeral_mb - 512, 0) / 1024 * duration_ms_total / 1000


def dynamodb_write_units(item_bytes: float) -> int:
    """書き込み 1 回の WRU(1 KB 単位で切り上げ。UpdateItem は更新前後の大きい方の項目サイズ)。"""
    return max(1, -(-int(item_bytes) // 1024))


def iot_message_units(message_bytes: float) -> int:
    """IoT Core のメッセージは 5 KB 単位で数える。"""
    return max(1, -(-int(message_bytes) // (5 * 1024)))
