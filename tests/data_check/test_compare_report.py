from decimal import Decimal

from data_check.compare.differ import ACCEPTED, FRACTIONAL, MATCH, MISMATCH
from data_check.compare.report import (
    _fmt, format_contributors, format_drilldown, format_provenance, format_report,
    format_restrictions, format_totals,
)


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
    text = format_report({"period": "2026-02-01..2026-07-31", "attribution": "По дате заявки",
                          "totals": totals,
                          "drilldown": {}, "accepted": [], "stale": []})
    assert "ГЕЙТ: PASS" in text


def test_format_report_fail_and_lists_accepted():
    totals = {"продажи": {"v5": Decimal("3888"), "v6": Decimal("3555"),
                          "delta": Decimal("-333"), "pct": Decimal("-8.56"),
                          "verdict": MISMATCH}}
    text = format_report({"period": "p", "attribution": "По дате заявки",
                          "totals": totals, "drilldown": {},
                          "accepted": [{"метрика": "продажи", "значение": "Маркар",
                                        "решение": "v6 верен", "verdict": ACCEPTED}],
                          "stale": []})
    assert "ГЕЙТ: FAIL" in text
    assert "Маркар" in text


def test_format_report_lists_stale():
    totals = {"расход": {"v5": Decimal("1"), "v6": Decimal("1"),
                         "delta": Decimal("0"), "pct": Decimal("0"), "verdict": MATCH}}
    text = format_report({"period": "p", "attribution": "По дате заявки",
                          "totals": totals, "drilldown": {},
                          "accepted": [],
                          "stale": [{"метрика": "продажи", "измерение": "источник",
                                     "значение": "crmf", "решение": "v6 верен"}]})
    assert "УСТАРЕВШИЕ ЗАПИСИ РЕЕСТРА" in text
    assert "продажи" in text and "crmf" in text


def test_format_totals_pct_none_renders_na():
    totals = {"расход": {"v5": Decimal("0"), "v6": Decimal("100"),
                         "delta": Decimal("100"), "pct": None, "verdict": MISMATCH}}
    assert "n/a" in format_totals(totals)


def test_fmt_exact_zero_has_no_sign():
    assert _fmt(Decimal("0")) == "0"
    assert _fmt(Decimal("-0")) == "0"


def test_fmt_tiny_negative_below_threshold_is_not_zero():
    text = _fmt(Decimal("-1E-7"))
    assert text not in ("0", "-0")
    assert text != "0.000000"


def test_fmt_tiny_positive_below_threshold_is_not_zero():
    text = _fmt(Decimal("1E-7"))
    assert text not in ("0", "-0")


def test_fmt_ordinary_negative_fraction():
    assert _fmt(Decimal("-0.005353")) == "-0.005353"


def test_fmt_huge_decimal_does_not_raise():
    text = _fmt(Decimal("1E+29"))
    assert text not in ("0", "-0")


# --- величины в спуске (спека §5) -------------------------------------------------------

def _rows(pairs, verdict=MISMATCH):
    return {key: {"delta": Decimal(str(value)), "verdict": verdict}
            for key, value in pairs.items()}


def test_contributors_print_top_keys_with_signed_deltas():
    text = format_contributors(_rows({"март": -2104, "апрель": -1987, "май": -1850,
                                      "июнь": -500, "июль": -361}))
    assert "март -2104" in text and "апрель -1987" in text and "май -1850" in text
    assert "ост. (2) -861" in text


def test_contributors_mark_positive_direction():
    assert "+500" in format_contributors(_rows({"Кудерко": 500}))


def test_contributors_skip_accepted_and_fractional_rows():
    """Величины показывают то же расхождение, что держит гейт, — не погашенное и не шум."""
    rows = _rows({"Маркар": -548}, verdict=ACCEPTED)
    rows.update(_rows({"дробь": "-0.4"}, verdict=FRACTIONAL))
    assert format_contributors(rows) == ""


def test_drilldown_prints_magnitudes_for_concentration_and_smear():
    concentrated = {"источник": {"hotspot": (["crmf"], Decimal("0.98")),
                                 "rows": _rows({"crmf": -6640, "seo": -60})}}
    assert "crmf -6640" in format_drilldown("квал", concentrated)
    smeared = {"специалист": {"hotspot": None,
                              "rows": _rows({"Кудерко": -12, "Фаиг": -9, "Иванов": -8,
                                             "Петров": -7})}}
    text = format_drilldown("квал", smeared)
    assert "размазано" in text and "Кудерко -12" in text and "ост. (1) -7" in text


def test_contributors_count_survives_offsetting_remainder():
    """14 взаимогасящихся ключей дают сумму 0 — счётчик не даёт прочитать это как «пусто»."""
    rows = _rows({"a": 100, "b": -90, "c": 80, "d": 50, "e": -50})
    text = format_contributors(rows)
    assert "ост. (2) +0" in text


# --- сужённый срез (спека §5) -----------------------------------------------------------

RESTRICTIONS = {"источник": {"dimension": "источник", "kept": ["Контекст", "SEO"],
                             "excluded_v5": ["Посевы_Звонки"], "excluded_v6": ["Звонки"]}}


def test_restrictions_block_names_dimension_kept_and_excluded():
    text = format_restrictions(RESTRICTIONS)
    assert "источник" in text and "сравнивалось 2 значений" in text
    assert "Посевы_Звонки" in text and "Звонки" in text


def test_report_prints_narrowed_slice():
    """Отложенное сужением не пропадает из отчёта, а называется явно."""
    totals = {"обращения": {"v5": Decimal("1"), "v6": Decimal("1"), "delta": Decimal("0"),
                            "pct": Decimal("0"), "verdict": MATCH}}
    text = format_report({"period": "p", "attribution": "По дате заявки", "totals": totals,
                          "drilldown": {}, "accepted": [], "stale": [],
                          "restrictions": RESTRICTIONS})
    assert "СРЕЗ СУЖЕН" in text and "Посевы_Звонки" in text


# --- провенанс и атрибуция --------------------------------------------------------------

PROVENANCE = {
    "v5": {"table": "public.fact_big_analytics", "max_date": "2026-07-31",
           "rows": 4214553, "run_id": None, "run_at": None},
    "v6": {"table": "ad_analytics.fact_big_analytics", "max_date": "2026-07-31",
           "rows": 4190220, "run_id": "7313aec1fd42", "run_at": "2026-08-04 09:22:07"},
}


def test_provenance_names_both_sides_and_v6_run():
    text = format_provenance(PROVENANCE)
    assert "public.fact_big_analytics" in text and "4214553" in text
    assert "7313aec1fd42" in text
    assert "журнала прогонов нет" in text  # у v5 его нет, и это сказано, а не выдумано


def test_report_header_carries_provenance_and_contract_attribution():
    """Атрибуция берётся из контракта — единственное поле, ради правки которого он и есть."""
    totals = {"расход": {"v5": Decimal("1"), "v6": Decimal("1"), "delta": Decimal("0"),
                         "pct": Decimal("0"), "verdict": MATCH}}
    text = format_report({"period": "p", "attribution": "По дате визита", "totals": totals,
                          "drilldown": {}, "accepted": [], "stale": [],
                          "provenance": PROVENANCE})
    assert "атрибуция: По дате визита" in text
    assert "7313aec1fd42" in text
