"""Step 14 for v6_ch: minus snapshot placeholder from current ClickHouse sources."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_utils import count_rows, swap_shadow

logger = logging.getLogger("pipeline.step14")


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    logger.info("Шаг 14 v6_ch: yandex_direct_minus_snapshot")
    client = get_client()
    t0 = time.perf_counter()
    client.command("DROP TABLE IF EXISTS ad_analytics.yandex_direct_minus_snapshot_new SYNC")
    client.command(
        """
        CREATE TABLE ad_analytics.yandex_direct_minus_snapshot_new
        ENGINE = MergeTree
        ORDER BY (snapshot_at, account_login, campaign_id)
        AS
        SELECT
            now() AS snapshot_at,
            account_login,
            campaign_id,
            campaign_name,
            CAST(NULL, 'Nullable(String)') AS minus_phrase,
            CAST(NULL, 'Nullable(String)') AS level
        FROM raw_data.direct_campaigns
        WHERE 0
        """
    )
    swap_shadow(client, "ad_analytics.yandex_direct_minus_snapshot", "ad_analytics.yandex_direct_minus_snapshot_new")
    rows = count_rows(client, "ad_analytics.yandex_direct_minus_snapshot")
    logger.info("Шаг 14 v6_ch завершён за %.1f сек: rows=%d", time.perf_counter() - t0, rows)
    return {"rows": rows, "details": f"yandex_direct_minus_snapshot={rows:,}"}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())
