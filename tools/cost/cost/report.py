"""Markdown のレポート。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .model import CATEGORIES, Line, compute
from .prices import PriceBook

SCALES = (1, 100, 1000)


def usd(v: float) -> str:
    if v == 0:
        return "$0"
    if abs(v) < 0.01:
        return f"${v:.4f}"
    if abs(v) < 1000:
        return f"${v:,.2f}"
    return f"${v:,.0f}"


def qty(v: float) -> str:
    if v == 0:
        return "0"
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    if abs(v) >= 1:
        return f"{v:,.2f}"
    return f"{v:.4g}"


def price(v: float) -> str:
    return f"${v:.6g}"


def render(
    title: str,
    lines: list[Line],
    prices: PriceBook,
    *,
    meta: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    notes: list[str] | None = None,
    scales: tuple[int, ...] = SCALES,
    extra_sections: list[str] | None = None,
) -> str:
    results = {n: compute(lines, prices, n) for n in scales}
    r1 = results[scales[0]]
    out: list[str] = [f"# {title}", ""]
    out.append(f"生成: {datetime.now(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')}  ")
    out.append(f"単価表: {prices.data.get('region')}、取得 {prices.data.get('fetched_at')}"
               "(AWS Pricing API。無料枠は含めない)  ")
    for k, v in (meta or {}).items():
        out.append(f"{k}: {v}  ")
    out.append("")

    # --- まとめ ---
    out += ["## 1 台・1 か月(30 日)あたり", ""]
    head = "| 項目 | " + " | ".join(f"{n:,} 台(1 台あたり)" for n in scales) + " |"
    out += [head, "| --- |" + " ---: |" * len(scales)]
    for c in CATEGORIES:
        out.append(f"| {c} | " + " | ".join(usd(results[n].by_category[c] / n) for n in scales) + " |")
    out.append("| **合計** | " + " | ".join(f"**{usd(results[n].per_camera)}**" for n in scales) + " |")
    out.append("")

    # --- 台数を増やしたとき ---
    out += ["## 台数を増やしたときの月額", "",
            ("台数に比例する明細(取り込み、保存、Lambda など)と、台数によらない明細(質問数で決まる検索など)を分けた。"
            "段階料金(S3 の 50 TB 超、CloudFront の 10 TB 超など)はアカウント全体の量で掛けている。"), "",
            "| 台数 | 台数に比例 | 台数によらない | 合計 / 月 | 1 台あたり |", "| ---: | ---: | ---: | ---: | ---: |"]
    for n in scales:
        sp = results[n].scale_split()
        out.append(f"| {n:,} | {usd(sp['camera'])} | {usd(sp['fixed'])} | {usd(results[n].total)} | "
                   f"{usd(results[n].per_camera)} |")
    out.append("")

    # --- 明細 ---
    out += ["## 明細(1 台)", "",
            "数量は 30 日あたり。「台数によらない」の明細は全体の量。単価は正規化後の単位(段階料金は 1 台時点の実効単価)。", "",
            "| 項目 | 明細 | 数量 | 単位 | 単価 | 月額 | 区分 | 出所 | 根拠 |",
            "| --- | --- | ---: | --- | ---: | ---: | --- | --- | --- |"]
    for c in CATEGORIES:
        for cl in [x for x in r1.lines if x.line.category == c]:
            ln = cl.line
            e = prices.entry(ln.price_key)
            scope = "台数比例" if ln.scale == "camera" else "台数によらない"
            out.append(f"| {c} | {ln.label} | {qty(ln.qty)} | {e['unit']} | {price(cl.unit_price)} | {usd(cl.cost)} | "
                       f"{scope} | {ln.source} | {ln.basis} |")
    out.append("")

    # --- 大きい順 ---
    top = sorted(r1.lines, key=lambda x: -x.cost)[:8]
    out += ["## 1 台の月額が大きい明細", "", "| 明細 | 月額 | 割合 |", "| --- | ---: | ---: |"]
    for cl in top:
        share = cl.cost / r1.total * 100 if r1.total else 0
        out.append(f"| {cl.line.category} / {cl.line.label} | {usd(cl.cost)} | {share:.1f}% |")
    out.append("")

    for sec in extra_sections or []:
        out += [sec, ""]

    if params:
        out += ["## 前提(引数)", "", "| 名前 | 値 |", "| --- | --- |"]
        out += [f"| `{k}` | {v} |" for k, v in params.items()]
        out.append("")

    # --- 使った単価 ---
    used = sorted({ln.price_key for ln in lines})
    out += ["## 使った単価", "", "| キー | 内容 | 単価(最初の段階) | 単位 | 出典 | 無料枠(計算に含めない) |",
            "| --- | --- | ---: | --- | --- | --- |"]
    for k in used:
        e = prices.entry(k)
        src = f"Pricing API SKU {e.get('sku')}" if e.get("source") == "pricing_api" else f"手入力 {e.get('url', '')}"
        ft = e.get("free_tier")
        free = f"{ft['amount']:,.0f}({ft['description'][:40]})" if ft else ""
        tiers = "(段階あり)" if len(e["tiers"]) > 1 else ""
        out.append(f"| `{k}` | {e['label']} | {price(e['tiers'][0]['price'])}{tiers} | {e['unit']} | {src} | {free} |")
    out.append("")
    ft_manual = prices.data.get("free_tiers_manual") or {}
    if ft_manual:
        out += ["無料枠(参考、計算には含めない):", ""]
        out += [f"- {v['text']}([出典]({v['url']}))" for v in ft_manual.values()]
        out.append("")

    if notes:
        out += ["## 注記", ""] + [f"- {n}" for n in notes] + [""]
    return "\n".join(out)
