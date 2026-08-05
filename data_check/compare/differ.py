"""Сравнение и локализация.

Все метрики — Decimal. Доли никогда не приводятся к int:
суммы v6 дробные по построению, суммы v5 целые, и расхождение
меньше единицы — отдельный класс, а не совпадение.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Optional, Tuple

MATCH = "MATCH"
FRACTIONAL = "FRACTIONAL"
MISMATCH = "MISMATCH"

_CONCENTRATION_SHARE = Decimal("0.8")
_CONCENTRATION_MAX_KEYS = 3


def classify(delta: Decimal) -> str:
    if delta == 0:
        return MATCH
    if abs(delta) < Decimal("1"):
        return FRACTIONAL
    return MISMATCH


def compare_totals(left: Dict[str, Decimal], right: Dict[str, Decimal],
                   metrics: List[str]) -> Dict[str, dict]:
    out = {}
    for name in metrics:
        lv = left.get(name, Decimal("0"))
        rv = right.get(name, Decimal("0"))
        delta = rv - lv
        pct = (delta / lv * Decimal("100")) if lv != 0 else None
        out[name] = {"v5": lv, "v6": rv, "delta": delta, "pct": pct,
                     "verdict": classify(delta)}
    return out


def compare_by_dimension(left: Dict[str, Dict[str, Decimal]],
                         right: Dict[str, Dict[str, Decimal]],
                         metric: str) -> Dict[str, dict]:
    out = {}
    for key in sorted(set(left) | set(right)):
        lv = left.get(key, {}).get(metric, Decimal("0"))
        rv = right.get(key, {}).get(metric, Decimal("0"))
        only_in = None
        if key not in left:
            only_in = "v6"
        elif key not in right:
            only_in = "v5"
        delta = rv - lv
        out[key] = {"v5": lv, "v6": rv, "delta": delta,
                    "verdict": classify(delta), "only_in": only_in}
    return out


def concentration(deltas: Dict[str, Decimal]) -> Optional[Tuple[List[str], Decimal]]:
    """Топ-3 ключа держат >= 80% модуля суммарной дельты -> концентрация."""
    total = sum((abs(v) for v in deltas.values()), Decimal("0"))
    if total == 0:
        return None
    ranked = sorted(deltas.items(), key=lambda kv: abs(kv[1]), reverse=True)
    picked, acc = [], Decimal("0")
    for key, value in ranked[:_CONCENTRATION_MAX_KEYS]:
        picked.append(key)
        acc += abs(value)
        if acc / total >= _CONCENTRATION_SHARE:
            return picked, acc / total
    return None
