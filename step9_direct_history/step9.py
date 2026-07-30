"""Step 9 for v6_ch: ClickHouse direct history snapshot."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_utils import count_rows, swap_shadow

logger = logging.getLogger("pipeline.step9")


def prefetch_history(*args, **kwargs):  # noqa: ANN002, ANN003
    logger.info("step9 v6_ch: prefetch_history skipped (raw_data.direct_campaigns is source)")
    return None


def run(conn=None, run_id: str | None = None, history_thread=None) -> dict:  # noqa: ARG001
    logger.info("Шаг 9 v6_ch: yandex_direct_history из raw_data.direct_campaigns")
    client = get_client()
    t0 = time.perf_counter()
    client.command("DROP TABLE IF EXISTS ad_analytics.yandex_direct_history_new SYNC")
    client.command(
        """
        CREATE TABLE ad_analytics.yandex_direct_history_new
        ENGINE = ReplacingMergeTree(updated_at)
        ORDER BY (login, campaign_id, event_type)
        AS
        SELECT
            parseDateTimeBestEffortOrNull(nullIf(synced_at, '')) AS datetime,
            account_login AS login,
            campaign_id,
            campaign_name,
            'campaign_snapshot' AS event_type,
            source AS change_source,
            CAST(NULL, 'Nullable(String)') AS old_value,
            concat(ifNull(status, ''), '|', ifNull(state, ''), '|', ifNull(payment_model, '')) AS new_value,
            gs.directologist AS `директолог`,
            gs.domain AS domain,
            gs.salon AS salon,
            now() AS updated_at
        FROM raw_data.direct_campaigns dc
        LEFT JOIN raw_data.gsheet_sites gs ON lower(ifNull(gs.login_key, '')) = lower(dc.account_login)
        """
    )
    swap_shadow(client, "ad_analytics.yandex_direct_history", "ad_analytics.yandex_direct_history_new")
    rows = count_rows(client, "ad_analytics.yandex_direct_history")
    logger.info("Шаг 9 v6_ch завершён за %.1f сек: rows=%d", time.perf_counter() - t0, rows)
    return {"rows": rows, "details": f"yandex_direct_history={rows:,}"}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())
