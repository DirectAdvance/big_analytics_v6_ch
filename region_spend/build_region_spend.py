"""Build ClickHouse `fact_region_spend` from raw Yandex report rows."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_utils import count_rows, month_ranges_from_table, swap_shadow

logger = logging.getLogger("pipeline.region_spend")


def _create_empty(client, target: str) -> None:
    client.command(f"DROP TABLE IF EXISTS {target} SYNC")
    client.command(
        f"""
        CREATE TABLE {target}
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(date)
        ORDER BY (date, campaign_id, ifNull(ad_group_id, 0), ifNull(id_location, 0))
        AS
        SELECT
            toDate('2026-01-01') AS date,
            toString(cityHash64('')) AS row_hash,
            toInt64(0) AS campaign_id,
            CAST(NULL, 'Nullable(String)') AS campaign_name,
            toInt64(0) AS ad_group_id,
            CAST(NULL, 'Nullable(String)') AS ad_group_name,
            CAST(NULL, 'Nullable(String)') AS ad_network_type,
            CAST(NULL, 'Nullable(Int64)') AS id_location,
            CAST(NULL, 'Nullable(String)') AS location,
            CAST(NULL, 'Nullable(String)') AS `Область`,
            CAST(NULL, 'Nullable(String)') AS GeoRegionType,
            CAST(NULL, 'Nullable(Float64)') AS distance_km,
            CAST(NULL, 'Nullable(Int32)') AS distance_km_agreg,
            toDecimal64(0, 6) AS cost,
            toDecimal64(0, 6) AS clicks,
            toDecimal64(0, 6) AS impressions,
            toDecimal64(0, 6) AS all_forms,
            toDecimal64(0, 6) AS crm_order_created,
            toDecimal64(0, 6) AS crm_order_paid,
            toDecimal64(0, 6) AS crm_spam_order,
            toDecimal64(0, 6) AS crm_order_canceled,
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


def _insert_batch(client, target: str, lo: str, hi: str) -> None:
    client.command(
        f"""
        INSERT INTO {target}
        SELECT
            toDate(day) AS date,
            toString(cityHash64(toString(toDate(day)), campaign_id, ifNull(ad_group_id, 0), ifNull(ad_network_type, ''), ifNull(location_of_presence_id, 0))) AS row_hash,
            campaign_id,
            anyLast(campaign_name) AS campaign_name,
            ifNull(ad_group_id, 0) AS ad_group_id,
            anyLast(ad_group_name) AS ad_group_name,
            ad_network_type,
            location_of_presence_id AS id_location,
            CAST(NULL, 'Nullable(String)') AS location,
            CAST(NULL, 'Nullable(String)') AS `Область`,
            CAST(NULL, 'Nullable(String)') AS GeoRegionType,
            CAST(NULL, 'Nullable(Float64)') AS distance_km,
            CAST(NULL, 'Nullable(Int32)') AS distance_km_agreg,
            toDecimal64(sum(ifNull(total_cost, 0)), 6) AS cost,
            toDecimal64(sum(ifNull(clicks, 0)), 6) AS clicks,
            toDecimal64(sum(ifNull(impressions, 0)), 6) AS impressions,
            toDecimal64(sum(ifNull(all_forms, 0)), 6) AS all_forms,
            toDecimal64(sum(ifNull(crm_order_created, 0)), 6) AS crm_order_created,
            toDecimal64(sum(ifNull(crm_order_paid, 0)), 6) AS crm_order_paid,
            toDecimal64(sum(ifNull(crm_spam_order, 0)), 6) AS crm_spam_order,
            toDecimal64(sum(ifNull(crm_order_canceled, 0)), 6) AS crm_order_canceled,
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
        GROUP BY date, campaign_id, ad_group_id, ad_network_type, location_of_presence_id, client_login
        """
    )


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    logger.info("region_spend v6_ch: fact_region_spend")
    client = get_client()
    t0 = time.perf_counter()
    shadow = "ad_analytics.fact_region_spend_new"
    _create_empty(client, shadow)
    ranges = month_ranges_from_table(client, "raw_data.yandex_direct_report_rows", "toDate(day)", "campaign_id != 0")
    for idx, (lo, hi) in enumerate(ranges, start=1):
        before = count_rows(client, shadow)
        _insert_batch(client, shadow, lo, hi)
        after = count_rows(client, shadow)
        logger.info("  region batch %d/%d: +%d строк", idx, len(ranges), after - before)
    swap_shadow(client, "ad_analytics.fact_region_spend", shadow)
    rows = count_rows(client, "ad_analytics.fact_region_spend")
    logger.info("region_spend v6_ch завершён за %.1f сек: rows=%d", time.perf_counter() - t0, rows)
    return {"rows": rows, "details": f"fact_region_spend={rows:,}"}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())
