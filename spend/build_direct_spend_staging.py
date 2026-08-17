"""Build transient ClickHouse staging for Direct spend/feed full rebuilds."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_settings import DATE_FROM
from config.ch_utils import SAFE_QUERY_SETTINGS, count_rows, day_ranges, swap_shadow, table_exists
from criterion_spend.cleaning import CRITERION_CLEAN

logger = logging.getLogger("pipeline.direct_spend_staging")

STAGING_TABLE = "ad_analytics.direct_spend_staging"


def _create_empty(client, target: str) -> None:
    client.command(f"DROP TABLE IF EXISTS {target} SYNC")
    client.command(
        f"""
        CREATE TABLE {target}
        (
            date Date,
            campaign_id Int64,
            ad_group_id Int64,
            ad_network_type LowCardinality(Nullable(String)),
            location_of_presence_id Nullable(Int64),
            ad_format LowCardinality(Nullable(String)),
            criterion_id Nullable(Int64),
            criterion_norm String,
            criterion_key UInt64,
            placement_feed_key String,
            placement String,
            domain Nullable(String),
            account_login LowCardinality(String),
            cost Decimal(38, 9),
            clicks Int64,
            impressions Int64,
            all_forms Decimal(38, 9),
            crm_order_created Decimal(38, 9),
            crm_order_paid Decimal(38, 9),
            crm_spam_order Decimal(38, 9),
            crm_order_canceled Decimal(38, 9)
        )
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(date)
        ORDER BY (date, campaign_id, ad_group_id, account_login)
        """,
        settings=SAFE_QUERY_SETTINGS,
    )


def _insert_batch(client, target: str, lo: str, hi: str) -> None:
    client.command(
        f"""
        INSERT INTO {target}
        WITH cleaned AS
        (
            SELECT
                *,
                {CRITERION_CLEAN} AS criterion_norm
            FROM raw_data.yandex_direct_report_rows
            WHERE toDate(day) >= toDate('{lo}') AND toDate(day) < toDate('{hi}')
              AND campaign_id != 0
        )
        SELECT
            toDate(day) AS date,
            campaign_id,
            ifNull(ad_group_id, 0) AS ad_group_id,
            ad_network_type,
            location_of_presence_id,
            ad_format,
            criterion_id,
            criterion_norm,
            cityHash64(lowerUTF8(trim(BOTH ' ' FROM criterion_norm))) AS criterion_key,
            lowerUTF8(trim(BOTH ' ' FROM ifNull(placement, ''))) AS placement_feed_key,
            trim(BOTH ' ' FROM ifNull(placement, '')) AS placement,
            domain,
            client_login AS account_login,
            ifNull(total_cost, toDecimal128(0, 9)) AS cost,
            ifNull(clicks, 0) AS clicks,
            ifNull(impressions, 0) AS impressions,
            ifNull(all_forms, toDecimal128(0, 9)) AS all_forms,
            ifNull(crm_order_created, toDecimal128(0, 9)) AS crm_order_created,
            ifNull(crm_order_paid, toDecimal128(0, 9)) AS crm_order_paid,
            ifNull(crm_spam_order, toDecimal128(0, 9)) AS crm_spam_order,
            ifNull(crm_order_canceled, toDecimal128(0, 9)) AS crm_order_canceled
        FROM cleaned
        """,
        settings=SAFE_QUERY_SETTINGS,
    )


def build(client) -> int:
    shadow = "ad_analytics.direct_spend_staging_new"
    _create_empty(client, shadow)
    ranges = day_ranges(DATE_FROM)
    for idx, (lo, hi) in enumerate(ranges, start=1):
        _insert_batch(client, shadow, lo, hi)
        logger.info("  direct staging batch %d/%d: %s -> %s", idx, len(ranges), lo, hi)
    swap_shadow(client, STAGING_TABLE, shadow)
    return count_rows(client, STAGING_TABLE)


def ensure_staging(client) -> int:
    if table_exists(client, "ad_analytics", "direct_spend_staging"):
        return count_rows(client, STAGING_TABLE)
    logger.info("direct_spend_staging отсутствует: строю full staging")
    return build(client)


def cleanup(client) -> None:
    client.command(f"DROP TABLE IF EXISTS {STAGING_TABLE} SYNC", settings=SAFE_QUERY_SETTINGS)


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    logger.info("direct_spend_staging v6_ch: transient full staging")
    client = get_client()
    t0 = time.perf_counter()
    rows = build(client)
    logger.info("direct_spend_staging v6_ch завершён за %.1f сек: rows=%d", time.perf_counter() - t0, rows)
    return {"rows": rows, "details": f"direct_spend_staging={rows:,}"}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())
