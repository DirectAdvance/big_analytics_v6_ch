"""Текстовый отчёт гейта."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Dict

from data_check.compare.differ import ACCEPTED, FRACTIONAL, MISMATCH

_FRACTIONAL_NOTE = "дробный остаток"


def _fmt(value: Decimal) -> str:
    """Форматирует Decimal для вывода, не мутируя исходное значение.

    Ноль всегда печатается как "0" (без знака). Ненулевое значение никогда не
    печатается как "0"/"-0" — если 6-знаковое округление коллапсирует его в ноль
    (например, Decimal('-1E-7')), отдаём str(value), чтобы читатель видел, что
    это реальное ненулевое число, а не совпадение.
    """
    if value == 0:
        return "0"
    try:
        has_fraction = bool(value % 1)
    except InvalidOperation:
        # value % 1 не помещается в текущую точность контекста (сверхбольшая
        # экспонента) — не роняем форматирование, отдаём как есть.
        return str(value)
    if not has_fraction:
        return "%d" % value
    text = ("%.6f" % value).rstrip("0").rstrip(".")
    if text in ("0", "-0", ""):
        return str(value)
    return text


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


_TOP_CONTRIBUTORS = 3


def _signed(value: Decimal) -> str:
    """Дельта всегда со знаком: читателю нужно направление, а не только модуль."""
    text = _fmt(value)
    return text if text.startswith("-") else "+" + text


def format_contributors(rows: Dict[str, dict]) -> str:
    """Верхние вкладчики среза с величинами: «март -2104 · апрель -1987 · ост. (14) -861».

    Берутся только ОТКРЫТЫЕ MISMATCH — те же строки, на которых считается концентрация.
    Погашенные решением и дробный шум сюда не попадают, иначе величины в спуске
    рассказывали бы не про то расхождение, которое держит гейт.
    """
    open_rows = [(key, row["delta"]) for key, row in rows.items()
                 if row.get("verdict") == MISMATCH]
    if not open_rows:
        return ""
    ranked = sorted(open_rows, key=lambda kv: abs(kv[1]), reverse=True)
    parts = ["%s %s" % (key, _signed(delta)) for key, delta in ranked[:_TOP_CONTRIBUTORS]]
    rest = ranked[_TOP_CONTRIBUTORS:]
    if rest:
        # Количество остатка печатается рядом с суммой: 14 взаимогасящихся ключей
        # дают «ост. +0», и без счётчика это читалось бы как «там ничего нет».
        parts.append("ост. (%d) %s" % (len(rest), _signed(sum((d for _, d in rest),
                                                             Decimal("0")))))
    return " · ".join(parts)


def format_drilldown(metric: str, per_dimension: Dict[str, dict]) -> str:
    lines = ["УРОВЕНЬ 1 — СПУСК по метрике «%s»" % metric]
    for dimension, payload in per_dimension.items():
        hotspot = payload.get("hotspot")
        if hotspot:
            keys, share = hotspot
            tail = "%s (%.0f%%)  🎯 концентрация" % (" · ".join(keys), share * 100)
        else:
            tail = "размазано → не здесь"
        lines.append("  по %-16s %s" % (dimension.upper(), tail))
        contributors = format_contributors(payload.get("rows") or {})
        if contributors:
            lines.append("     %-16s %s" % ("", contributors))
    return "\n".join(lines)


def format_provenance(provenance: Dict[str, dict]) -> str:
    """Шапка «откуда взята каждая сторона» (спека §5)."""
    lines = []
    for side in ("v5", "v6"):
        info = provenance[side]
        run = ("прогон %s от %s" % (info["run_id"], info["run_at"])
               if info.get("run_id") else "журнала прогонов нет")
        lines.append("  %s: %s   строк %d   max(Date) %s   %s"
                     % (side, info["table"], info["rows"], info["max_date"], run))
    return "\n".join(lines)


def format_restrictions(restrictions: Dict[str, dict]) -> str:
    """Сужённые срезы печатаются, а не пропадают (спека §5)."""
    lines = ["СРЕЗ СУЖЕН (сравнивались НЕ все значения измерения)"]
    for dimension, info in restrictions.items():
        lines.append("  измерение «%s»: сравнивалось %d значений (%s)"
                     % (dimension, len(info["kept"]), ", ".join(info["kept"])))
        for side in ("v5", "v6"):
            excluded = info["excluded_%s" % side]
            lines.append("    отложено %s (%d): %s"
                         % (side, len(excluded), ", ".join(excluded) or "—"))
    return "\n".join(lines)


def format_report(result: dict) -> str:
    open_mismatch = [n for n, r in result["totals"].items() if r["verdict"] == MISMATCH]
    parts = ["ГЕЙТ v5 → v6   период %s   атрибуция: %s"
             % (result["period"], result["attribution"])]
    if result.get("provenance"):
        parts.append(format_provenance(result["provenance"]))
    parts += ["", format_totals(result["totals"])]
    for metric, per_dim in result["drilldown"].items():
        parts += ["", format_drilldown(metric, per_dim)]
    if result.get("restrictions"):
        parts += ["", format_restrictions(result["restrictions"])]
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
