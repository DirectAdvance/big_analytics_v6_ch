"""Текстовый отчёт гейта."""
from __future__ import annotations

from decimal import Decimal
from typing import Dict

from data_check.compare.differ import ACCEPTED, FRACTIONAL, MISMATCH

_FRACTIONAL_NOTE = "дробный остаток"


def _fmt(value: Decimal) -> str:
    return ("%.6f" % value).rstrip("0").rstrip(".") if value % 1 else "%d" % value


def format_totals(totals: Dict[str, dict]) -> str:
    lines = ["УРОВЕНЬ 0 — ИТОГ",
             "  %-12s %16s %16s %14s %9s  %s" % ("метрика", "v5", "v6", "Δ", "Δ%", "вердикт")]
    for name, row in totals.items():
        pct = "n/a" if row["pct"] is None else "%.2f%%" % row["pct"]
        note = "  (%s)" % _FRACTIONAL_NOTE if row["verdict"] == FRACTIONAL else ""
        lines.append("  %-12s %16s %16s %14s %9s  %s%s" % (
            name, _fmt(row["v5"]), _fmt(row["v6"]), _fmt(row["delta"]),
            pct, row["verdict"], note))
    return "\n".join(lines)


def format_drilldown(metric: str, per_dimension: Dict[str, dict]) -> str:
    lines = ["УРОВЕНЬ 1 — СПУСК по метрике «%s»" % metric]
    for dimension, payload in per_dimension.items():
        hotspot = payload.get("hotspot")
        if hotspot:
            keys, share = hotspot
            lines.append("  по %-16s %s (%.0f%%)  🎯 концентрация"
                         % (dimension.upper(), " · ".join(keys), share * 100))
        else:
            lines.append("  по %-16s размазано → не здесь" % dimension.upper())
    return "\n".join(lines)


def format_report(result: dict) -> str:
    open_mismatch = [n for n, r in result["totals"].items() if r["verdict"] == MISMATCH]
    parts = ["ГЕЙТ v5 → v6   период %s   атрибуция: по дате заявки" % result["period"], "",
             format_totals(result["totals"])]
    for metric, per_dim in result["drilldown"].items():
        parts += ["", format_drilldown(metric, per_dim)]
    if result["accepted"]:
        parts += ["", "ОСОЗНАННЫЕ ОТЛИЧИЯ (погашены решением)"]
        for entry in result["accepted"]:
            parts.append("  %s / %s — %s" % (entry.get("метрика"), entry.get("значение"),
                                             entry.get("решение")))
    if result["stale"]:
        parts += ["", "УСТАРЕВШИЕ ЗАПИСИ РЕЕСТРА (расхождения больше нет)"]
        for entry in result["stale"]:
            parts.append("  %s / %s" % (entry.get("метрика"), entry.get("значение")))
    parts += ["", "ГЕЙТ: %s" % ("FAIL" if open_mismatch else "PASS")]
    if open_mismatch:
        parts.append("  блокеры: %s" % ", ".join(open_mismatch))
    return "\n".join(parts)
