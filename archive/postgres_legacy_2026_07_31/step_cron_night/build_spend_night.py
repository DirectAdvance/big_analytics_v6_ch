#!/usr/bin/env python3
"""
DEPRECATED — переименован в build_spend_daily.py (2026-06-27).

Job перенесён с ночного расписания на 14:00 Екб / 09:00 UTC.
Используй: step_cron_night/build_spend_daily.py
"""
raise RuntimeError(
    'build_spend_night.py устарел — используй build_spend_daily.py '
    '(cron: 0 9 * * *, 14:00 Екб / 09:00 UTC)'
)
