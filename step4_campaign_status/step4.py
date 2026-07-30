"""Step 4 for v6_ch: campaign status from ClickHouse raw_data."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_utils import count_rows

logger = logging.getLogger("pipeline.step4")


def prefetch_statuses(*args, **kwargs):  # noqa: ANN002, ANN003
    """Compatibility hook for v5 orchestrators; v6 reads raw_data directly."""
    logger.info("step4 v6_ch: prefetch_statuses skipped (raw_data.direct_campaigns is source)")
    return None


def run(conn=None, run_id: str | None = None, prefetch_thread=None) -> dict:  # noqa: ARG001
    logger.info("Шаг 4 v6_ch: campaign_status из raw_data.direct_campaigns")
    client = get_client()
    t0 = time.perf_counter()

    client.command("DROP TABLE IF EXISTS ad_analytics.campaign_status_new SYNC")
    client.command(
        """
        CREATE TABLE ad_analytics.campaign_status_new
        ENGINE = ReplacingMergeTree(_version)
        ORDER BY (`CampaignId`)
        AS
        SELECT
            campaign_id AS `CampaignId`,
            account_login,
            multiIf(
                upper(ifNull(status, '')) IN ('ACCEPTED', 'ACTIVE') OR upper(ifNull(state, '')) = 'ON', 'Активна',
                upper(ifNull(status, '')) IN ('SUSPENDED', 'STOPPED') OR upper(ifNull(state, '')) = 'OFF', 'Остановлена',
                upper(ifNull(status, '')) IN ('ARCHIVED'), 'Архив',
                ifNull(status, '')
            ) AS `статус`,
            CAST(NULL, 'Nullable(String)') AS `специалист`,
            campaign_name AS `CampaignName`,
            CAST(NULL, 'Nullable(String)') AS manager_login,
            multiIf(
                upper(ifNull(status, '')) IN ('ACCEPTED', 'ACTIVE') OR upper(ifNull(state, '')) = 'ON', 'Активна',
                upper(ifNull(status, '')) IN ('SUSPENDED', 'STOPPED') OR upper(ifNull(state, '')) = 'OFF', 'Остановлена',
                upper(ifNull(status, '')) IN ('ARCHIVED'), 'Архив',
                ifNull(status, '')
            ) AS campaign_status,
            payment_model,
            now() AS _version
        FROM raw_data.direct_campaigns
        """
    )
    client.command("EXCHANGE TABLES ad_analytics.campaign_status AND ad_analytics.campaign_status_new")
    client.command("DROP TABLE IF EXISTS ad_analytics.campaign_status_new SYNC")

    rows = count_rows(client, "ad_analytics.campaign_status")
    logger.info("Шаг 4 v6_ch завершён за %.1f сек: campaign_status=%d", time.perf_counter() - t0, rows)
    return {"rows": rows, "details": f"campaign_status={rows:,}"}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())
