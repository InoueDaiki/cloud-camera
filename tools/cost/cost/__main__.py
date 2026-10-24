"""コマンドライン。`uv run python -m cost <サブコマンド> --help` を参照。"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import typing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import billing, desk, measure, prices, report
from .model import compute

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"


def _session(profile: str | None):
    import boto3

    return boto3.Session(profile_name=profile) if profile else boto3.Session()


def _time(s: str) -> datetime:
    t = datetime.fromisoformat(s)
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    print(f"書き出し: {path}", file=sys.stderr)


def _summary(lines, book) -> str:
    out = []
    for n in report.SCALES:
        r = compute(lines, book, n)
        out.append(f"{n:>5,} 台: 合計 {report.usd(r.total)} / 月、1 台あたり {report.usd(r.per_camera)}")
    r1 = compute(lines, book, 1)
    out.append("1 台の内訳: " + "、".join(f"{c} {report.usd(v)}" for c, v in r1.by_category.items()))
    return "\n".join(out)


# ---------------------------------------------------------------------------
def cmd_prices(a) -> None:
    book = prices.fetch_price_book(a.region, _session(a.profile))
    out = Path(a.out) if a.out else (prices.DEFAULT_PATH if a.region == prices.REGION else ROOT / f"prices_{a.region}.json")
    prices.save(book, out)
    print(f"{len(book['prices'])} 件を {out} に保存", file=sys.stderr)
    for k, v in book["errors"].items():
        print(f"取得できなかった: {k}: {v}", file=sys.stderr)


def _add_desk_args(p: argparse.ArgumentParser) -> None:
    hints = typing.get_type_hints(desk.DeskParams)
    for f in dataclasses.fields(desk.DeskParams):
        t = hints[f.name]
        conv = str if t is str else (int if t is int else float)
        p.add_argument("--" + f.name.replace("_", "-"), type=conv, default=None, help=f"既定 {f.default}")


def cmd_desk(a) -> None:
    book = prices.PriceBook.load(a.prices)
    kw = {f.name: getattr(a, f.name) for f in dataclasses.fields(desk.DeskParams) if getattr(a, f.name) is not None}
    params = desk.DeskParams(**kw)
    lines = desk.build_lines(params)
    notes = [
        "机上計算。物体検出の推論時間、視聴時間、質問数とトークン数は仮定(実測で置き換える)",
        "GB は 2^30 バイト(AWS の課金の GB)。1 Mbps × 30 日 = 324 × 10^9 バイト = 301.7 GB",
        "Bedrock の jp. 推論プロファイルは、リージョン内(Regional)の単価で計算(global. は Global の単価)",
        ("動画の退避(スプライト)と物体検出の両方が GetImages を呼ぶ。KVS の GetImages は 1 枚 $0.00001 なので、"
        "毎秒 1 枚でも 1 台・月 259 万枚になる"),
    ]
    text = report.render("コスト試算(机上計算)", lines, book,
                         meta={"モード": "机上計算", "変更した引数": json.dumps(kw, ensure_ascii=False) if kw else "なし(既定値)"},
                         params=params.as_dict(), notes=notes)
    out = Path(a.out) if a.out else REPORTS / f"desk_{datetime.now():%Y%m%d_%H%M%S}.md"
    _write(out, text)
    print(_summary(lines, book))


def _period(a) -> tuple[datetime, datetime]:
    end = _time(a.end) if a.end else datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    start = _time(a.start) if a.start else end - timedelta(days=a.days)
    if start >= end:
        raise SystemExit("start は end より前にする")
    return start, end


def cmd_measure(a) -> None:
    book = prices.PriceBook.load(a.prices)
    if a.from_raw:
        raw = json.loads(Path(a.from_raw).read_text())
    else:
        start, end = _period(a)
        raw = measure.collect(_session(a.profile), a.project, start, end, a.region)
        raw_out = Path(a.raw_out) if a.raw_out else REPORTS / f"raw_{a.project}_{start:%Y%m%dT%H%M}_{end:%Y%m%dT%H%M}.json"
        _write(raw_out, json.dumps(raw, ensure_ascii=False, indent=1, default=str))
    opt = measure.MeasureOptions(media_retention_days=a.media_retention_days,
                                 detections_retention_days=a.detections_retention_days)
    lines, notes = measure.to_lines(raw, opt)
    extra = []
    if a.billing:
        start, end = _time(raw["start"]), _time(raw["end"])
        bill = billing.fetch(_session(a.profile), start, end, raw["project"])
        n = max(len(raw.get("cameras") or []), 1)
        r = compute(lines, book, n)
        expected = r.total * (end - start).total_seconds() / (30 * 86400)
        extra.append(billing.render(bill, expected))
    text = report.render(f"コスト実測({raw['project']})", lines, book,
                         meta={"モード": "実測", "project": raw["project"], "期間": f"{raw['start']} 〜 {raw['end']}",
                               "カメラ": ", ".join(c["camera_id"] for c in raw.get("cameras") or []) or "なし"},
                         notes=notes, extra_sections=extra)
    out = Path(a.out) if a.out else REPORTS / f"measure_{raw['project']}_{datetime.now():%Y%m%d_%H%M%S}.md"
    _write(out, text)
    print(_summary(lines, book))


def cmd_billing(a) -> None:
    start, end = _period(a)
    bill = billing.fetch(_session(a.profile), start, end, a.project)
    print(billing.render(bill, None))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m cost", description="カメラ 1 台・1 か月あたりのコスト計測")
    ap.add_argument("--profile", default=None, help="AWS プロファイル(既定は環境変数 AWS_PROFILE)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("prices", help="Pricing API から単価表を作る")
    p.add_argument("--region", default=prices.REGION)
    p.add_argument("--out", default=None)
    p.set_defaults(func=cmd_prices)

    p = sub.add_parser("desk", help="机上計算(引数で前提を与える)")
    p.add_argument("--prices", default=str(prices.DEFAULT_PATH))
    p.add_argument("--out", default=None)
    _add_desk_args(p)
    p.set_defaults(func=cmd_desk)

    def period_args(p):
        p.add_argument("--start", default=None, help="ISO 8601(UTC)。省略時は end − days")
        p.add_argument("--end", default=None, help="ISO 8601(UTC)。省略時は現在時刻(時の境界に切り捨て)")
        p.add_argument("--days", type=float, default=7.0)

    p = sub.add_parser("measure", help="デプロイ済みの環境から実測して計算")
    p.add_argument("--project", default="cloud-camera")
    p.add_argument("--region", default=prices.REGION)
    p.add_argument("--prices", default=str(prices.DEFAULT_PATH))
    period_args(p)
    p.add_argument("--raw-out", default=None, help="集めた生の値の保存先(JSON)")
    p.add_argument("--from-raw", default=None, help="AWS を呼ばず、保存した生の値から計算し直す")
    p.add_argument("--media-retention-days", type=float, default=None)
    p.add_argument("--detections-retention-days", type=float, default=None)
    p.add_argument("--billing", action="store_true", help="Cost Explorer の実請求額と突き合わせる")
    p.add_argument("--out", default=None)
    p.set_defaults(func=cmd_measure)

    p = sub.add_parser("billing", help="Cost Explorer の実請求額だけを出す")
    p.add_argument("--project", default="cloud-camera")
    period_args(p)
    p.set_defaults(func=cmd_billing)

    a = ap.parse_args(argv)
    a.func(a)


if __name__ == "__main__":
    main()
