"""Step 2 for v6_ch: ClickHouse RAW maintenance.

Postgres indexes/ANALYZE do not apply to ClickHouse. The raw tables are created
with MergeTree ORDER BY keys in step1; this step only forces a best-effort
OPTIMIZE so following heavy reads see compact parts when possible.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_settings import RAW_TARGET_TABLES
from config.ch_utils import SAFE_QUERY_SETTINGS

logger = logging.getLogger("pipeline.step2")


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    logger.info("Шаг 2 v6_ch: OPTIMIZE RAW таблиц ClickHouse")
    client = get_client()
    t0 = time.perf_counter()
    optimized = 0
    view_skipped = 0
    skipped: list[str] = []

    for logical_name, table in RAW_TARGET_TABLES.items():
        exists = bool(
            client.query(
                """
                SELECT count()
                FROM system.tables
                WHERE database = 'ad_analytics'
                  AND name = {name:String}
                """,
                parameters={"name": table.split(".", 1)[1]},
            ).result_rows[0][0]
        )
        if not exists:
            skipped.append(logical_name)
            logger.warning("  %s отсутствует — пропускаем", table)
            continue
        engine_rows = client.query(
            """
            SELECT engine
            FROM system.tables
            WHERE database = 'ad_analytics'
              AND name = {name:String}
            """,
            parameters={"name": table.split(".", 1)[1]},
            settings=SAFE_QUERY_SETTINGS,
        ).result_rows
        if engine_rows and engine_rows[0][0] == "View":
            view_skipped += 1
            logger.info("  %s — VIEW, OPTIMIZE не нужен", table)
            continue
        client.command(f"OPTIMIZE TABLE {table} FINAL")
        optimized += 1
        logger.info("  OPTIMIZE %s — OK", table)

    details = f"optimized={optimized}, view_skipped={view_skipped}, skipped={','.join(skipped) if skipped else '-'}"
    logger.info("Шаг 2 v6_ch завершён за %.1f сек: %s", time.perf_counter() - t0, details)
    return {"rows": optimized, "details": details}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())
