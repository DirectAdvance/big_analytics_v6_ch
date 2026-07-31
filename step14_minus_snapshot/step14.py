"""Step 14 for v6_ch: minus snapshot placeholder from current ClickHouse sources."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_utils import SAFE_QUERY_SETTINGS, count_rows, swap_shadow

logger = logging.getLogger("pipeline.step14")


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    logger.info("Шаг 14 v6_ch: yandex_direct_minus_snapshot")
    client = get_client()
    t0 = time.perf_counter()
    client.command("DROP TABLE IF EXISTS ad_analytics.yandex_direct_minus_snapshot_new SYNC", settings=SAFE_QUERY_SETTINGS)
    client.command(
        """
        CREATE TABLE ad_analytics.yandex_direct_minus_snapshot_new
        ENGINE = MergeTree
        ORDER BY (`date`, login, campaign_id)
        AS SELECT
            CAST(0, 'UInt64') AS id,
            toDate('1970-01-01') AS `date`,
            CAST('', 'String') AS login,
            CAST(0, 'Int64') AS campaign_id,
            CAST(NULL, 'Nullable(String)') AS campaign_name,
            CAST(NULL, 'Nullable(String)') AS campaign_state,
            CAST(NULL, 'Nullable(String)') AS block,
            CAST(0, 'Int64') AS minus_in_campaign,
            CAST(0, 'Int64') AS minus_in_groups,
            CAST(0, 'Int64') AS minus_in_sets,
            CAST(0, 'Int64') AS minus_total,
            CAST(0, 'Bool') AS has_minus,
            CAST(1, 'Bool') AS check_ok,
            now() AS loaded_at,
            CAST(NULL, 'Nullable(String)') AS `специалист`
        WHERE 0
        """,
        settings=SAFE_QUERY_SETTINGS,
    )
    swap_shadow(client, "ad_analytics.yandex_direct_minus_snapshot", "ad_analytics.yandex_direct_minus_snapshot_new")
    rows = count_rows(client, "ad_analytics.yandex_direct_minus_snapshot")
    logger.info("Шаг 14 v6_ch завершён за %.1f сек: rows=%d", time.perf_counter() - t0, rows)
    return {"rows": rows, "details": f"yandex_direct_minus_snapshot={rows:,}"}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())
