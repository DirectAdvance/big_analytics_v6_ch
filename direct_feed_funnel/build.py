"""Build ClickHouse `fact_direct_feed_funnel` from raw report rows."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_utils import count_rows, month_ranges_from_table, swap_shadow

logger = logging.getLogger("pipeline.direct_feed_funnel")


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    logger.info("direct_feed_funnel v6_ch: fact_direct_feed_funnel")
    client = get_client()
    t0 = time.perf_counter()
    shadow = "ad_analytics.fact_direct_feed_funnel_new"
    client.command(f"DROP TABLE IF EXISTS {shadow} SYNC")
    client.command(
        f"""
        CREATE TABLE {shadow}
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(date)
        ORDER BY (date, campaign_id, ad_group_id, ifNull(placement, ''))
        AS
        SELECT
            toDate('2026-01-01') AS date,
            toInt64(0) AS campaign_id,
            CAST(NULL, 'Nullable(String)') AS campaign_name,
            toInt64(0) AS ad_group_id,
            CAST(NULL, 'Nullable(String)') AS ad_group_name,
            CAST(NULL, 'Nullable(String)') AS ad_network_type,
            CAST(NULL, 'Nullable(String)') AS placement,
            CAST(NULL, 'Nullable(String)') AS domain,
            CAST(NULL, 'Nullable(String)') AS account_login,
            toDecimal64(0, 6) AS cost,
            toDecimal64(0, 6) AS clicks,
            toDecimal64(0, 6) AS impressions,
            toDecimal64(0, 6) AS all_forms,
            toDecimal64(0, 6) AS crm_order_created,
            toDecimal64(0, 6) AS crm_order_paid
        WHERE 0
        """
    )
    ranges = month_ranges_from_table(client, "raw_data.yandex_direct_report_rows", "toDate(day)", "campaign_id != 0")
    for idx, (lo, hi) in enumerate(ranges, start=1):
        before = count_rows(client, shadow)
        client.command(
            f"""
            INSERT INTO {shadow}
            SELECT
                toDate(day) AS date,
                campaign_id,
                anyLast(campaign_name) AS campaign_name,
                ifNull(ad_group_id, 0) AS ad_group_id,
                anyLast(ad_group_name) AS ad_group_name,
                ad_network_type,
                placement,
                anyLast(domain) AS domain,
                client_login AS account_login,
                toDecimal64(sum(ifNull(total_cost, 0)), 6) AS cost,
                toDecimal64(sum(ifNull(clicks, 0)), 6) AS clicks,
                toDecimal64(sum(ifNull(impressions, 0)), 6) AS impressions,
                toDecimal64(sum(ifNull(all_forms, 0)), 6) AS all_forms,
                toDecimal64(sum(ifNull(crm_order_created, 0)), 6) AS crm_order_created,
                toDecimal64(sum(ifNull(crm_order_paid, 0)), 6) AS crm_order_paid
            FROM raw_data.yandex_direct_report_rows
            WHERE toDate(day) >= toDate('{lo}') AND toDate(day) < toDate('{hi}')
              AND campaign_id != 0
            GROUP BY date, campaign_id, ad_group_id, ad_network_type, placement, client_login
            """
        )
        after = count_rows(client, shadow)
        logger.info("  direct_feed batch %d/%d: +%d строк", idx, len(ranges), after - before)
    swap_shadow(client, "ad_analytics.fact_direct_feed_funnel", shadow)
    rows = count_rows(client, "ad_analytics.fact_direct_feed_funnel")
    logger.info("direct_feed_funnel v6_ch завершён за %.1f сек: rows=%d", time.perf_counter() - t0, rows)
    return {"rows": rows, "details": f"fact_direct_feed_funnel={rows:,}"}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())
