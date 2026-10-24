from datetime import UTC, datetime

from cost import billing, desk, report
from cost.__main__ import main
from cost.model import CATEGORIES
from cost.prices import PriceBook


def test_render_has_all_sections():
    book = PriceBook.load()
    text = report.render("t", desk.build_lines(desk.DeskParams()), book, params={"a": 1}, notes=["n"])
    for c in CATEGORIES:
        assert f"| {c} |" in text
    for h in ("## 1 台・1 か月(30 日)あたり", "## 台数を増やしたときの月額", "## 明細(1 台)", "## 使った単価", "## 注記"):
        assert h in text
    assert "1,000 台(1 台あたり)" in text
    assert "Pricing API SKU" in text
    assert "無料枠(参考、計算には含めない)" in text


def test_usd_format():
    assert report.usd(0) == "$0"
    assert report.usd(0.001234) == "$0.0012"
    assert report.usd(12.345) == "$12.35"
    assert report.usd(12345.6) == "$12,346"


def test_desk_cli(tmp_path, capsys):
    out = tmp_path / "r.md"
    main(["desk", "--bitrate-mbps", "2", "--detect-interval-s", "5", "--out", str(out)])
    text = out.read_text()
    assert '"bitrate_mbps": 2.0' in text
    assert "1 台あたり" in capsys.readouterr().out


class FakeCE:
    def __init__(self, tagged_empty=True):
        self.calls = []
        self.tagged_empty = tagged_empty

    def get_cost_and_usage(self, **kw):
        self.calls.append(kw)
        key = kw["GroupBy"][0]["Key"]
        if "Filter" in kw and self.tagged_empty:
            return {"ResultsByTime": [{"Groups": []}]}
        groups = {"SERVICE": [("Amazon Kinesis Video Streams", "1.5"), ("AWS Lambda", "0.5")],
                  "USAGE_TYPE": [("APN1-BytesIn", "1.5")]}[key]
        return {"ResultsByTime": [{"Groups": [{"Keys": [k], "Metrics": {"UnblendedCost": {"Amount": v}}}
                                              for k, v in groups]}]}


class FakeSession:
    def __init__(self, ce):
        self.ce = ce

    def client(self, name, region_name=None):
        assert name == "ce" and region_name == "us-east-1"
        return self.ce


def test_billing_falls_back_to_account_total():
    ce = FakeCE()
    bill = billing.fetch(FakeSession(ce), datetime(2026, 10, 20, tzinfo=UTC), datetime(2026, 10, 22, 12, tzinfo=UTC), "cc")
    assert ce.calls[0]["TimePeriod"] == {"Start": "2026-10-20", "End": "2026-10-23"}
    assert bill["by_service"]["Amazon Kinesis Video Streams"] == 1.5
    assert bill["by_service_tagged"] == {}
    text = billing.render(bill, expected_total=1.8)
    assert "アカウント全体" in text and "$2.0000" in text and "+11.1%" in text


def test_billing_uses_tag_when_available():
    bill = billing.fetch(FakeSession(FakeCE(tagged_empty=False)), datetime(2026, 10, 20, tzinfo=UTC),
                         datetime(2026, 10, 21, tzinfo=UTC), "cc")
    assert "タグ Project で絞った値" in billing.render(bill, None)
