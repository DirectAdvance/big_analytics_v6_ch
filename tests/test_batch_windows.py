"""Окна пакетной вставки: ширина берётся из PIPELINE_BATCH_DAYS, покрытие без дыр и нахлёстов.

Ширина влияет только на память/время (см. OPTIMIZATION_PLAN.md), поэтому важно ровно одно:
объединение окон = исходный диапазон, ни одна дата не обработана дважды.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_utils import PIPELINE_BATCH_DAYS, day_ranges


def test_windows_cover_range_exactly_once():
    ranges = day_ranges("2026-01-01", "2026-08-15")

    assert ranges[0][0] == "2026-01-01"
    assert ranges[-1][1] == "2026-08-15"
    for (_, prev_hi), (next_lo, _) in zip(ranges, ranges[1:]):
        assert prev_hi == next_lo, "окна обязаны стыковаться встык: ни дыр, ни двойного счёта"


def test_no_window_crosses_a_month_boundary():
    """MONTH_SNAP_2026-08-14: недельные окна ломали веса пикселя в step11 на 24 днях.

    Причина — месячный агрегат внутри батча (`toStartOfMonth(lo)`) при окне, залезающем
    в следующий месяц. Окно в пределах месяца — гарантия, на которой держатся все такие шаги.
    """
    from datetime import date, timedelta

    for lo, hi in day_ranges("2026-01-01", "2026-08-15"):
        lo_day = date.fromisoformat(lo)
        last_day = date.fromisoformat(hi) - timedelta(days=1)
        assert (lo_day.year, lo_day.month) == (last_day.year, last_day.month), f"окно {lo}..{hi} пересекает месяц"


def test_window_width_follows_the_knob():
    from datetime import date

    ranges = day_ranges("2026-01-01", "2026-03-01")
    first_width = (date.fromisoformat(ranges[0][1]) - date.fromisoformat(ranges[0][0])).days

    assert first_width == PIPELINE_BATCH_DAYS
    # хвост может быть короче ширины, но не длиннее
    last_width = (date.fromisoformat(ranges[-1][1]) - date.fromisoformat(ranges[-1][0])).days
    assert 1 <= last_width <= PIPELINE_BATCH_DAYS
