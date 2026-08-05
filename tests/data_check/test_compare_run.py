"""Регрессия на логику спуска гейта v5→v6.

Ни одна БД не трогается: вместо `_Collector` подставляется фейк с готовыми срезами.
Главное, что здесь защищается, — закрытие ложного PASS: тотал есть СУММА по значениям
измерения, поэтому +500 в одном ключе и -500 в другом схлопываются в ноль, тотал читается
как MATCH, спуск бы не запустился и гейт напечатал бы PASS поверх реального расхождения.
"""
from decimal import Decimal

import pytest

from data_check.compare import differ
from data_check.compare.report import format_report
from data_check.compare.run import (
    _EXTRA_DIMENSIONS, _TOTALS_DIMENSION, _drilldown, _restrict_to_comparable, _totals,
)

METRIC = "расход"
METRICS = [METRIC]
ALL_DIMENSIONS = [_TOTALS_DIMENSION] + _EXTRA_DIMENSIONS


class FakeCollector:
    """Отдаёт заранее заданные срезы вместо запросов в PostgreSQL/ClickHouse."""

    def __init__(self, slices):
        self._slices = slices
        self.asked = []
        self.restrictions = {}

    def get(self, dimension):
        if dimension not in self.asked:  # как и настоящий _Collector, кэширует срез
            self.asked.append(dimension)
        return self._slices[dimension]


def _side(values):
    """{'2026-02': 1000} -> {'2026-02': {'расход': Decimal('1000')}}"""
    return {key: {METRIC: Decimal(str(value))} for key, value in values.items()}


def _slices(left, right, dimensions=ALL_DIMENSIONS):
    """Один и тот же срез на всех измерениях — форма ключей для логики спуска не важна."""
    pair = (_side(left), _side(right))
    return {dimension: pair for dimension in dimensions}


# --- закрытие ложного PASS -------------------------------------------------------------

def test_offsetting_deltas_escalate_matched_total_to_mismatch():
    """+500 и -500 гасят друг друга в тотале — но расхождение реально и гейт обязан упасть."""
    collector = FakeCollector(_slices({"2026-02": 1000, "2026-03": 1000},
                                      {"2026-02": 1500, "2026-03": 500}))
    totals = _totals(collector, METRICS)
    assert totals[METRIC]["delta"] == 0
    assert totals[METRIC]["verdict"] == differ.MATCH  # тотал сам по себе сошёлся

    _drilldown(collector, [], totals)

    assert totals[METRIC]["verdict"] == differ.MISMATCH
    assert totals[METRIC]["escalated"] is True
    assert totals[METRIC]["original_verdict"] == differ.MATCH
    assert totals[METRIC]["delta"] == 0  # дельта не подделывается: тотал правда сошёлся


def test_escalated_total_makes_report_fail():
    """Сквозь отчёт: поднятый вердикт печатается как FAIL, а не как PASS."""
    collector = FakeCollector(_slices({"2026-02": 1000, "2026-03": 1000},
                                      {"2026-02": 1500, "2026-03": 500}))
    totals = _totals(collector, METRICS)
    drilldown, hits, stale = _drilldown(collector, [], totals)
    text = format_report({"period": "2026-02-01..2026-07-31", "totals": totals,
                          "drilldown": drilldown, "accepted": hits, "stale": stale})
    assert "ГЕЙТ: FAIL" in text
    assert "блокеры: расход" in text


def test_fractional_noise_does_not_escalate_and_makes_no_hotspot():
    """Дробный шум <1 — не расхождение: ни поднятого вердикта, ни высосанного очага."""
    collector = FakeCollector(_slices({"2026-02": 1000, "2026-03": 1000},
                                      {"2026-02": "1000.4", "2026-03": "999.6"}))
    totals = _totals(collector, METRICS)
    drilldown, _, _ = _drilldown(collector, [], totals)

    assert totals[METRIC]["verdict"] == differ.MATCH
    assert "escalated" not in totals[METRIC]
    assert drilldown[METRIC][_TOTALS_DIMENSION]["hotspot"] is None
    # спуск в остальные измерения не запускался — нечего локализовывать
    assert collector.asked == [_TOTALS_DIMENSION]


def test_mismatching_total_drills_every_dimension():
    collector = FakeCollector(_slices({"2026-02": 1000, "2026-03": 1000},
                                      {"2026-02": 1000, "2026-03": 1700}))
    totals = _totals(collector, METRICS)
    assert totals[METRIC]["verdict"] == differ.MISMATCH

    drilldown, _, _ = _drilldown(collector, [], totals)

    assert list(drilldown[METRIC]) == ALL_DIMENSIONS
    assert collector.asked == ALL_DIMENSIONS


def test_accepted_registry_suppresses_escalation():
    """Расхождение, погашенное решением Семёна, не поднимает вердикт тотала."""
    accepted = [{"метрика": METRIC, "измерение": _TOTALS_DIMENSION, "значение": key,
                 "решение": "осознанное отличие"} for key in ("2026-02", "2026-03")]
    collector = FakeCollector(_slices({"2026-02": 1000, "2026-03": 1000},
                                      {"2026-02": 1500, "2026-03": 500}))
    totals = _totals(collector, METRICS)
    drilldown, hits, _ = _drilldown(collector, accepted, totals)

    assert totals[METRIC]["verdict"] == differ.MATCH
    assert "escalated" not in totals[METRIC]
    assert drilldown[METRIC][_TOTALS_DIMENSION]["hotspot"] is None
    assert {h["значение"] for h in hits} == {"2026-02", "2026-03"}
    assert collector.asked == [_TOTALS_DIMENSION]


# --- ограничение среза сопоставимыми значениями ----------------------------------------

CONTRACT = {"dimensions": {
    "источник": {"values_comparable": ["Контекст", "SEO"]},
    "месяц": {},
}}


def test_restrict_keeps_only_comparable_values_and_counts_the_rest():
    left = _side({"Контекст": 100, "SEO": 10, "Посевы_Звонки": 7})
    right = _side({"Контекст": 100, "SEO": 10, "Звонки": 9})

    kept_left, kept_right, info = _restrict_to_comparable(CONTRACT, "источник", left, right)

    assert set(kept_left) == {"Контекст", "SEO"}
    assert set(kept_right) == {"Контекст", "SEO"}
    assert info["excluded_v5"] == ["Посевы_Звонки"]
    assert info["excluded_v6"] == ["Звонки"]
    assert info["kept"] == sorted(["Контекст", "SEO"])


def test_restrict_leaves_dimension_without_list_untouched():
    left = _side({"2026-02": 1})
    right = _side({"2026-02": 2})
    kept_left, kept_right, info = _restrict_to_comparable(CONTRACT, "месяц", left, right)
    assert (kept_left, kept_right, info) == (left, right, None)


def test_restrict_raises_when_nothing_comparable_left():
    """Пустой срез — ошибка исполнения, а не совпадение: молчаливый ноль запрещён."""
    left = _side({"Посевы_Звонки": 7})
    right = _side({"Звонки": 9})
    with pytest.raises(Exception) as exc:
        _restrict_to_comparable(CONTRACT, "источник", left, right)
    assert "values_comparable" in str(exc.value)
