"""Диф двух снимков веса — единственная нетривиальная логика в tools/table_weight_report.py."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.table_weight_report import diff_tables


def test_diff_sorts_biggest_savings_first_and_keeps_new_and_dropped_tables():
    before = {"raw_yandex": 1000, "dropped": 500, "unchanged": 10}
    after = {"raw_yandex": 400, "unchanged": 10, "added": 70}

    rows = diff_tables(before, after)

    assert rows[0] == ("raw_yandex", 1000, 400, -600)
    assert ("dropped", 500, 0, -500) in rows
    assert ("added", 0, 70, 70) in rows
    assert rows[-1] == ("added", 0, 70, 70)
    assert sum(row[3] for row in rows) == -1030
