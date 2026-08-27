"""Step 4 for v6_ch: campaign status from ClickHouse raw_data."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_utils import SAFE_QUERY_SETTINGS, count_rows

logger = logging.getLogger("pipeline.step4")


def prefetch_statuses(*args, **kwargs):  # noqa: ANN002, ANN003
    """Compatibility hook for v5 orchestrators; v6 reads raw_data directly."""
    logger.info("step4 v6_ch: prefetch_statuses skipped (raw_data.direct_cookie_campaign_status is source)")
    return None


def run(conn=None, run_id: str | None = None, prefetch_thread=None) -> dict:  # noqa: ARG001
    logger.info("Шаг 4 v6_ch: campaign_status VIEW из raw_data.direct_cookie_campaign_status")
    client = get_client()
    t0 = time.perf_counter()

    client.command("DROP TABLE IF EXISTS ad_analytics.campaign_status_v SYNC")
    client.command("DROP TABLE IF EXISTS ad_analytics.campaign_status SYNC")
    client.command(
        """
        CREATE VIEW ad_analytics.campaign_status AS
        WITH latest_cookie AS
        (
            SELECT *
            FROM
            (
                SELECT
                    campaign_id,
                    lowerUTF8(trim(BOTH ' ' FROM ifNull(client_login, ''))) AS account_login,
                    manager_login,
                    campaign_name,
                    primary_status,
                    strategy_type,
                    pay_for_conversion,
                    row_number() OVER (
                        PARTITION BY campaign_id
                        ORDER BY extracted_at DESC, loaded_at DESC
                    ) AS rn
                FROM raw_data.direct_cookie_campaign_status
            )
            WHERE rn = 1
        )
        SELECT
            coalesce(lc.campaign_id, dc.campaign_id) AS `CampaignId`,
            coalesce(nullIf(lc.account_login, ''), dc.account_login) AS account_login,
            multiIf(
                upperUTF8(ifNull(lc.primary_status, '')) IN ('ACCEPTED', 'ACTIVE'), 'Активна',
                upperUTF8(ifNull(lc.primary_status, '')) = 'DRAFT', 'Черновик',
                upperUTF8(ifNull(lc.primary_status, '')) = 'MODERATION', 'На модерации',
                upperUTF8(ifNull(lc.primary_status, '')) = 'REJECTED', 'Отклонена',
                upperUTF8(ifNull(lc.primary_status, '')) IN ('SUSPENDED', 'STOPPED'), 'Остановлена',
                upperUTF8(ifNull(lc.primary_status, '')) = 'ARCHIVED', 'Архив',
                ifNull(lc.primary_status, '')
            ) AS `статус`,
            CAST(NULL, 'Nullable(String)') AS `специалист`,
            coalesce(nullIf(lc.campaign_name, ''), dc.campaign_name) AS `CampaignName`,
            lc.manager_login AS manager_login,
            lc.primary_status AS campaign_status,
            multiIf(
                ifNull(lc.pay_for_conversion, false), 'CPA',
                lc.pay_for_conversion IS NOT NULL, 'CPC',
                ifNull(lc.strategy_type, '')
            ) AS payment_model,
            now() AS _version
        FROM latest_cookie lc
        FULL OUTER JOIN reference_data.direct_campaigns dc
            ON dc.campaign_id = lc.campaign_id
        """,
        settings=SAFE_QUERY_SETTINGS,
    )
    client.command(
        """
        CREATE VIEW ad_analytics.campaign_status_v AS
        SELECT
            `CampaignId`,
            CAST(account_login, 'Nullable(String)') AS account_login,
            CAST(`статус`, 'LowCardinality(Nullable(String))') AS `статус`,
            CAST(`специалист`, 'LowCardinality(Nullable(String))') AS `специалист`,
            `CampaignName`,
            manager_login,
            CAST(campaign_status, 'LowCardinality(Nullable(String))') AS campaign_status,
            CAST(payment_model, 'LowCardinality(Nullable(String))') AS payment_model
        FROM ad_analytics.campaign_status
        """,
        settings=SAFE_QUERY_SETTINGS,
    )

    rows = count_rows(client, "ad_analytics.campaign_status_v")
    logger.info("Шаг 4 v6_ch завершён за %.1f сек: campaign_status_v=%d", time.perf_counter() - t0, rows)
    return {"rows": rows, "details": f"campaign_status_v={rows:,}"}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())
