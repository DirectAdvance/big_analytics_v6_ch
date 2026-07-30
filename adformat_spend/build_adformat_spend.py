"""Build ClickHouse `fact_adformat_spend` from raw Yandex report rows."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_utils import count_rows, month_ranges_from_table, swap_shadow

logger = logging.getLogger("pipeline.adformat_spend")


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    logger.info("adformat_spend v6_ch: fact_adformat_spend")
    client = get_client()
    t0 = time.perf_counter()
    shadow = "ad_analytics.fact_adformat_spend_new"
    client.command(f"DROP TABLE IF EXISTS {shadow} SYNC")
    client.command(
        f"""
        CREATE TABLE {shadow}
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(date)
        ORDER BY (date, campaign_id, ad_group_id, ifNull(ad_format, ''))
        AS
        SELECT
            toDate('2026-01-01') AS date,
            toString(cityHash64('')) AS row_hash,
            toInt64(0) AS campaign_id,
            CAST(NULL, 'Nullable(String)') AS campaign_name,
            toInt64(0) AS ad_group_id,
            CAST(NULL, 'Nullable(String)') AS ad_group_name,
            CAST(NULL, 'Nullable(String)') AS ad_network_type,
            CAST(NULL, 'Nullable(String)') AS ad_format,
            toDecimal64(0, 6) AS cost,
            toDecimal64(0, 6) AS clicks,
            toDecimal64(0, 6) AS impressions,
            CAST(NULL, 'Nullable(String)') AS account_login,
            CAST(NULL, 'Nullable(String)') AS domain,
            CAST(NULL, 'Nullable(String)') AS `специалист`,
            CAST(NULL, 'Nullable(String)') AS `салон`,
            CAST(NULL, 'Nullable(String)') AS `город`,
            CAST(NULL, 'Nullable(String)') AS `регион`,
            CAST(NULL, 'Nullable(String)') AS `тип_сайта`,
            CAST(NULL, 'Nullable(String)') AS `шаблон`,
            CAST(NULL, 'Nullable(String)') AS `статус`,
            CAST(NULL, 'Nullable(String)') AS direction,
            CAST(NULL, 'Nullable(String)') AS `проджект`,
            CAST(NULL, 'Nullable(String)') AS `id_салона`
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
                toString(cityHash64(toString(toDate(day)), campaign_id, ifNull(ad_group_id, 0), ifNull(ad_network_type, ''), ifNull(ad_format, ''))) AS row_hash,
                campaign_id,
                anyLast(campaign_name) AS campaign_name,
                ifNull(ad_group_id, 0) AS ad_group_id,
                anyLast(ad_group_name) AS ad_group_name,
                ad_network_type,
                ad_format,
                toDecimal64(sum(ifNull(total_cost, 0)), 6) AS cost,
                toDecimal64(sum(ifNull(clicks, 0)), 6) AS clicks,
                toDecimal64(sum(ifNull(impressions, 0)), 6) AS impressions,
                client_login AS account_login,
                anyLast(gs.domain) AS domain,
                anyLast(gs.directologist) AS `специалист`,
                anyLast(gs.salon) AS `салон`,
                anyLast(gs.city) AS `город`,
                anyLast(gs.region) AS `регион`,
                anyLast(gs.site_type) AS `тип_сайта`,
                anyLast(gs.template) AS `шаблон`,
                anyLast(gs.status) AS `статус`,
                anyLast(gs.direction) AS direction,
                anyLast(gs.project_manager) AS `проджект`,
                anyLast(gs.client_id) AS `id_салона`
            FROM raw_data.yandex_direct_report_rows y
            LEFT JOIN raw_data.gsheet_sites gs ON lower(ifNull(gs.login_key, '')) = lower(y.client_login)
            WHERE toDate(day) >= toDate('{lo}') AND toDate(day) < toDate('{hi}')
              AND campaign_id != 0
            GROUP BY date, campaign_id, ad_group_id, ad_network_type, ad_format, client_login
            """
        )
        after = count_rows(client, shadow)
        logger.info("  adformat batch %d/%d: +%d строк", idx, len(ranges), after - before)
    swap_shadow(client, "ad_analytics.fact_adformat_spend", shadow)
    rows = count_rows(client, "ad_analytics.fact_adformat_spend")
    logger.info("adformat_spend v6_ch завершён за %.1f сек: rows=%d", time.perf_counter() - t0, rows)
    return {"rows": rows, "details": f"fact_adformat_spend={rows:,}"}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())
