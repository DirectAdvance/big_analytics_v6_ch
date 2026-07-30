"""Step 7 for v6_ch: finalize ClickHouse tables."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_utils import count_rows, table_exists

logger = logging.getLogger("pipeline.step7")

TABLES = [
    "big_analytics_sources",
    "big_analytics_calls",
    "big_analytics_full",
]


def _engine(client, table: str) -> str | None:
    rows = client.query(
        """
        SELECT engine
        FROM system.tables
        WHERE database='ad_analytics' AND name={table:String}
        """,
        parameters={"table": table},
    ).result_rows
    return rows[0][0] if rows else None


def run(conn=None, run_id: str | None = None, skip_vacuum: bool = False, set_logged_tables=None) -> dict:  # noqa: ARG001
    logger.info("Шаг 7 v6_ch: OPTIMIZE ClickHouse таблиц")
    client = get_client()
    t0 = time.perf_counter()
    parts: list[str] = []
    total = 0
    optimized = 0
    for table in TABLES:
        if not table_exists(client, "ad_analytics", table):
            logger.warning("  ad_analytics.%s отсутствует — пропуск", table)
            continue
        if _engine(client, table) == "View":
            logger.info("  ad_analytics.%s is VIEW — OPTIMIZE skip", table)
            continue
        rows = count_rows(client, f"ad_analytics.{table}")
        total += rows
        parts.append(f"{table}={rows:,}")
        client.command(f"OPTIMIZE TABLE ad_analytics.{table} FINAL")
        optimized += 1
        logger.info("  optimized ad_analytics.%s (%d строк)", table, rows)
    details = ", ".join(parts)
    logger.info("Шаг 7 v6_ch завершён за %.1f сек: optimized=%d", time.perf_counter() - t0, optimized)
    return {"rows": total, "details": details}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())
