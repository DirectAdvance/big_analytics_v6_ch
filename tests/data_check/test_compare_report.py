from decimal import Decimal

from data_check.compare.differ import ACCEPTED, FRACTIONAL, MATCH, MISMATCH
from data_check.compare.report import format_totals, format_drilldown, format_report


def test_format_totals_prints_metric_and_delta():
    totals = {"продажи": {"v5": Decimal("3888"), "v6": Decimal("3555"),
                          "delta": Decimal("-333"), "pct": Decimal("-8.56"),
                          "verdict": MISMATCH}}
    text = format_totals(totals)
    assert "продажи" in text and "-333" in text and MISMATCH in text


def test_format_totals_marks_fractional_separately():
    totals = {"продажи": {"v5": Decimal("591"), "v6": Decimal("590.994647"),
                          "delta": Decimal("-0.005353"), "pct": Decimal("-0.0009"),
                          "verdict": FRACTIONAL}}
    assert "дробный остаток" in format_totals(totals)


def test_format_drilldown_shows_concentration():
    per_dim = {"источник": {"hotspot": (["crmf"], Decimal("0.98")), "rows": {}}}
    text = format_drilldown("квал", per_dim)
    assert "crmf" in text and "концентрация" in text


def test_format_drilldown_shows_smeared():
    per_dim = {"специалист": {"hotspot": None, "rows": {}}}
    assert "размазано" in format_drilldown("квал", per_dim)


def test_format_report_pass_when_no_open_mismatch():
    totals = {"расход": {"v5": Decimal("1"), "v6": Decimal("1"),
                         "delta": Decimal("0"), "pct": Decimal("0"), "verdict": MATCH}}
    text = format_report({"period": "2026-02-01..2026-07-31", "totals": totals,
                          "drilldown": {}, "accepted": [], "stale": []})
    assert "ГЕЙТ: PASS" in text


def test_format_report_fail_and_lists_accepted():
    totals = {"продажи": {"v5": Decimal("3888"), "v6": Decimal("3555"),
                          "delta": Decimal("-333"), "pct": Decimal("-8.56"),
                          "verdict": MISMATCH}}
    text = format_report({"period": "p", "totals": totals, "drilldown": {},
                          "accepted": [{"метрика": "продажи", "значение": "Маркар",
                                        "решение": "v6 верен", "verdict": ACCEPTED}],
                          "stale": []})
    assert "ГЕЙТ: FAIL" in text
    assert "Маркар" in text
