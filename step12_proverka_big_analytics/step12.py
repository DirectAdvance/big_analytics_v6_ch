"""Step 12 for v6_ch: ClickHouse quality checks."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_utils import count_rows, table_exists
from step3_build_sources.step3 import SOURCE_STORE

logger = logging.getLogger("pipeline.step12")


def _scalar(client, sql: str):
    return client.query(sql).result_rows[0][0]


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    logger.info("Шаг 12 v6_ch: проверки big_analytics ClickHouse")
    client = get_client()
    t0 = time.perf_counter()
    failures: list[str] = []

    required = ["big_analytics_full", SOURCE_STORE]
    for table in required:
        if not table_exists(client, "ad_analytics", table):
            failures.append(f"missing:{table}")
            continue
        if count_rows(client, f"ad_analytics.{table}") == 0:
            failures.append(f"empty:{table}")

    if table_exists(client, "ad_analytics", "big_analytics_full"):
        checks = {
            "full_before_2026": "SELECT count() FROM ad_analytics.big_analytics_full WHERE `Date` < toDate('2026-01-01')",
            "full_null_source": "SELECT count() FROM ad_analytics.big_analytics_full WHERE `источник` IS NULL OR `источник` = ''",
            "full_funnel_korr_lt_kval": "SELECT count() FROM ad_analytics.big_analytics_full WHERE korr < kval",
            "full_funnel_kval_lt_priezd": "SELECT count() FROM ad_analytics.big_analytics_full WHERE kval < priezd",
            "full_funnel_priezd_lt_prodazhi": "SELECT count() FROM ad_analytics.big_analytics_full WHERE priezd < prodazhi",
            "full_credit_lt_approved": "SELECT count() FROM ad_analytics.big_analytics_full WHERE dohod_do_kredita < dobro",
            "direct_crop_key_overlap": """
                SELECT count()
                FROM
                (
                    SELECT key3
                    FROM ad_analytics.big_analytics_sources
                    WHERE ifNull(key3, '') != ''
                      AND _source_table IN ('direct', 'tp8', 'tp9', 'tp10')
                    INTERSECT
                    SELECT key3
                    FROM ad_analytics.big_analytics_sources
                    WHERE ifNull(key3, '') != ''
                      AND _source_table = 'crop_targeting'
                )
            """,
        }
        for name, sql in checks.items():
            value = int(_scalar(client, sql))
            logger.info("  %s=%d", name, value)
            if value:
                failures.append(f"{name}={value}")

    if failures:
        raise RuntimeError("CH quality checks failed: " + "; ".join(failures))

    rows = count_rows(client, "ad_analytics.big_analytics_full")
    logger.info("Шаг 12 v6_ch PASS за %.1f сек", time.perf_counter() - t0)
    return {"rows": rows, "details": "PASS"}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())
