"""ClickHouse v6 corrections hook.

The historical v5 file applied many PostgreSQL UPDATE statements after source
tables were built. v6_ch avoids large in-place updates: normalization and basic
classification are folded into `step3_build_sources.step3`, and late enrichment
is handled by shadow-table rebuilds in later steps.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config.ch_db import get_client
from config.ch_utils import count_rows, table_exists

logger = logging.getLogger("corrections")

COMPONENT_TABLES = ["big_analytics_sources"]


def apply(conn=None, run_id: str | None = None) -> dict:  # noqa: A001, ARG001
    logger.info("corrections v6_ch: heavy UPDATE rules are folded into CH builders")
    client = get_client()
    t0 = time.perf_counter()
    total = 0
    parts: list[str] = []
    for table in COMPONENT_TABLES:
        if not table_exists(client, "ad_analytics", table):
            continue
        rows = count_rows(client, f"ad_analytics.{table}")
        total += rows
        parts.append(f"{table}={rows:,}")
    details = ", ".join(parts)
    logger.info("corrections v6_ch завершены за %.1f сек: %s", time.perf_counter() - t0, details)
    return {"rows": total, "details": details}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(apply())
