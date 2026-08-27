#!/usr/bin/env python3
"""Run the same specialist fallback for the visit-axis full table after step13."""

from __future__ import annotations

import logging

import spec_fallback

TARGET = "ad_analytics.big_analytics_full_arrival"
SHADOW = "ad_analytics.big_analytics_full_arrival_spec_new"


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    return spec_fallback.run_for_table(TARGET, SHADOW, "Шаг 13.5")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())
