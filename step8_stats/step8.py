"""Step 8 for v6_ch: read-only ClickHouse statistics."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_utils import count_rows, table_exists

logger = logging.getLogger("pipeline.step8")

TABLES = [
    "raw_yandex",
    "raw_leads",
    "raw_calls",
    "big_analytics_sources",
    "big_analytics_calls",
    "big_analytics_full",
    "big_analytics_pixel_score",
    "big_analytics_full_arrival",
    "big_analytics_unified",
    "fact_big_analytics",
]


def run(conn=None, run_id: str | None = None, send_tg: bool = False, **kwargs) -> dict:  # noqa: ARG001
    logger.info("Шаг 8 v6_ch: статистика ClickHouse")
    client = get_client()
    t0 = time.perf_counter()
    parts: list[str] = []
    total = 0
    for table in TABLES:
        if not table_exists(client, "ad_analytics", table):
            continue
        rows = count_rows(client, f"ad_analytics.{table}")
        total += rows
        parts.append(f"{table}={rows:,}")
        logger.info("  %s: %d строк", table, rows)

    if table_exists(client, "ad_analytics", "big_analytics_full"):
        row = client.query(
            """
            SELECT
                sum(total_cost),
                sum(kol_vo_zayavok),
                sum(korr),
                sum(kval),
                sum(priezd),
                sum(prodazhi)
            FROM ad_analytics.big_analytics_full
            """
        ).result_rows[0]
        logger.info(
            "  full metrics: cost=%s z=%s korr=%s kval=%s priezd=%s prodazhi=%s",
            *row,
        )

    details = ", ".join(parts)
    logger.info("Шаг 8 v6_ch завершён за %.1f сек", time.perf_counter() - t0)
    return {"rows": total, "details": details}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())
