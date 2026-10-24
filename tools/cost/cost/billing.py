"""Cost Explorer の実請求額と、計算した額の突き合わせ。

- タグ `Project`(infra の default_tags)で絞れるかを先に試し、絞れなければアカウント全体のサービス別で出す
- Cost Explorer の値は 1 日単位で、反映まで最大 24 時間ほど遅れる。期間は UTC の日付に丸める
- タグで絞るには、支払いアカウントで `Project` をコスト配分タグとして有効にしておく必要がある(リンクアカウントからは有効化できない)
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

# Cost Explorer の SERVICE 名 → コストの項目(model.py の CATEGORIES)に寄せるための目安(表示用)
SERVICE_HINT = {
    "Amazon Kinesis Video Streams": "取り込み / 一時保持 / 動画抽出 / 物体検出 / 再生",
    "AWS Lambda": "動画抽出 / 物体検出 / 再生 / 検索",
    "Amazon Simple Storage Service": "加工・保存 / 検出結果保存 / 再生",
    "Amazon DynamoDB": "加工・保存",
    "Amazon CloudFront": "再生",
    "Amazon API Gateway": "再生",
    "Amazon Athena": "検索",
    "AWS Glue": "検索",
    "Amazon Bedrock": "検索",
    "AWS IoT": "制御",
    "Amazon EventBridge": "動画抽出 / 物体検出",
    "AmazonCloudWatch": "各 Lambda のログ",
}


def _days(start: datetime, end: datetime) -> tuple[str, str]:
    s = start.date()
    e = end.date() if end.time() == datetime.min.time() else end.date() + timedelta(days=1)
    return s.isoformat(), e.isoformat()


def fetch(session: Any, start: datetime, end: datetime, project: str | None) -> dict[str, Any]:
    ce = session.client("ce", region_name="us-east-1")
    s, e = _days(start, end)
    base = {"TimePeriod": {"Start": s, "End": e}, "Granularity": "DAILY", "Metrics": ["UnblendedCost"]}

    def run(**extra) -> dict[str, float]:
        out: dict[str, float] = defaultdict(float)
        kw = {**base, **extra}
        while True:
            r = ce.get_cost_and_usage(**kw)
            for day in r["ResultsByTime"]:
                for g in day.get("Groups", []):
                    out[" / ".join(g["Keys"])] += float(g["Metrics"]["UnblendedCost"]["Amount"])
            if not r.get("NextPageToken"):
                break
            kw["NextPageToken"] = r["NextPageToken"]
        return dict(out)

    result: dict[str, Any] = {"start": s, "end": e, "notes": []}
    result["by_service"] = run(GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}])
    result["by_usage_type"] = run(GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}])
    if project:
        try:
            tagged = run(GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
                         Filter={"Tags": {"Key": "Project", "Values": [project], "MatchOptions": ["EQUALS"]}})
        except Exception as ex:  # noqa: BLE001
            tagged = {}
            result["notes"].append(f"タグで絞れなかった: {ex}")
        result["by_service_tagged"] = tagged
        if not tagged:
            result["notes"].append(
                f"タグ Project={project} の費用が 0。コスト配分タグが有効でないか、タグの付かないリソース(IoT、Bedrock など)だけの可能性。"
                "アカウント全体の値で比べる")
    return result


def render(bill: dict[str, Any], expected_total: float | None) -> str:
    out = ["## Cost Explorer の実請求額との突き合わせ", "",
           f"期間 {bill['start']} 〜 {bill['end']}(UTC の日単位、終わりを含まない)", ""]
    tagged = bill.get("by_service_tagged") or {}
    src = tagged if tagged else bill.get("by_service", {})
    label = "タグ Project で絞った値" if tagged else "アカウント全体(他の環境の費用も含む)"
    total = sum(src.values())
    out += [f"対象: {label}", "", "| サービス | 実請求額 | 主な項目 |", "| --- | ---: | --- |"]
    for k, v in sorted(src.items(), key=lambda x: -x[1]):
        if abs(v) < 1e-6:
            continue
        out.append(f"| {k} | ${v:,.4f} | {SERVICE_HINT.get(k, '')} |")
    out.append(f"| **合計** | **${total:,.4f}** | |")
    out.append("")
    if expected_total is not None:
        diff = total - expected_total
        out.append(f"計算した額(実測台数・同じ期間): ${expected_total:,.4f}。差 ${diff:,.4f}"
                   f"({(diff / expected_total * 100) if expected_total else 0:+.1f}%)。"
                   "無料枠の適用、反映の遅れ(約 24 時間)、日単位の丸めで差が出る。")
        out.append("")
    top = sorted((bill.get("by_usage_type") or {}).items(), key=lambda x: -x[1])[:15]
    if top:
        out += ["使用タイプ別(上位 15、アカウント全体)", "", "| 使用タイプ | 実請求額 |", "| --- | ---: |"]
        out += [f"| {k} | ${v:,.4f} |" for k, v in top if v > 0]
        out.append("")
    out += [f"- {n}" for n in bill.get("notes", [])]
    return "\n".join(out)

