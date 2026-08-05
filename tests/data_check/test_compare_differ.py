from decimal import Decimal

from data_check.compare.differ import (
    MATCH, FRACTIONAL, MISMATCH, classify, compare_totals, concentration, compare_by_dimension,
)


def test_classify_exact():
    assert classify(Decimal("0")) == MATCH


def test_classify_fractional_remainder():
    assert classify(Decimal("-0.005353")) == FRACTIONAL
    assert classify(Decimal("0.999999")) == FRACTIONAL


def test_classify_real_mismatch():
    assert classify(Decimal("-333")) == MISMATCH
    assert classify(Decimal("1")) == MISMATCH


def test_compare_totals_computes_delta_and_pct():
    left = {"продажи": Decimal("3888")}
    right = {"продажи": Decimal("3555")}
    out = compare_totals(left, right, ["продажи"])
    row = out["продажи"]
    assert row["delta"] == Decimal("-333")
    assert row["verdict"] == MISMATCH
    assert round(row["pct"], 2) == round(Decimal("-333") / Decimal("3888") * 100, 2)


def test_compare_totals_pct_is_none_when_left_zero():
    out = compare_totals({"x": Decimal("0")}, {"x": Decimal("5")}, ["x"])
    assert out["x"]["pct"] is None


def test_compare_by_dimension_keeps_one_sided_keys():
    left = {"Фаиг": {"квал": Decimal("100")}}
    right = {"Фаиг": {"квал": Decimal("90")}, "rivendell_excel": {"квал": Decimal("7")}}
    out = compare_by_dimension(left, right, "квал")
    assert out["Фаиг"]["delta"] == Decimal("-10")
    assert out["rivendell_excel"]["delta"] == Decimal("7")
    assert out["rivendell_excel"]["only_in"] == "v6"


def test_concentration_detects_hotspot():
    deltas = {"crmf": Decimal("-6640"), "plex": Decimal("-100"), "mega": Decimal("-62")}
    keys, share = concentration(deltas)
    assert keys == ["crmf"]
    assert share > Decimal("0.9")


def test_concentration_returns_none_when_smeared():
    deltas = {str(i): Decimal("-10") for i in range(20)}
    assert concentration(deltas) is None


def test_concentration_ignores_zero_total():
    assert concentration({"a": Decimal("0"), "b": Decimal("0")}) is None
