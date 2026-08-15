"""Build ClickHouse `fact_region_spend` from raw Yandex report rows."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_settings import DATE_FROM
from config.ch_utils import SAFE_QUERY_SETTINGS, count_rows, day_ranges, swap_shadow
from spend.build_direct_spend_staging import STAGING_TABLE, ensure_staging

logger = logging.getLogger("pipeline.region_spend")


# FACT_WEIGHT_2026-08-14 (OPTIMIZATION_PLAN.md, фаза 2.2): схема задаётся явно, а не выводится из
# CTAS-заглушки. Замер на партиции 202607 этой же таблицы: ZSTD(3) на Decimal(18,6)-метриках +
# T64+ZSTD(3) на *_id + LowCardinality на строках = −34.5% веса. T64 на самих метриках пробовали —
# хуже (−28.9%); перевод id_location в Int32 не дал ничего сверх кодека, поэтому тип не тронут.
# Порядок колонок обязан совпадать с SELECT в _insert_batch: INSERT ... SELECT позиционный.
_COLUMNS = """
    `date` Date,
    `campaign_id` Int64 CODEC(T64, ZSTD(3)),
    `ad_group_id` Int64 CODEC(T64, ZSTD(3)),
    `ad_network_type_key` LowCardinality(String),
    `id_location` Nullable(Int64) CODEC(T64, ZSTD(3)),
    `cost` Decimal(18, 6) CODEC(ZSTD(3)),
    `clicks` Decimal(18, 6) CODEC(ZSTD(3)),
    `impressions` Decimal(18, 6) CODEC(ZSTD(3)),
    `all_forms` Decimal(18, 6) CODEC(ZSTD(3)),
    `crm_order_created` Decimal(18, 6) CODEC(ZSTD(3)),
    `crm_order_paid` Decimal(18, 6) CODEC(ZSTD(3)),
    `crm_spam_order` Decimal(18, 6) CODEC(ZSTD(3)),
    `crm_order_canceled` Decimal(18, 6) CODEC(ZSTD(3)),
    `account_login` LowCardinality(Nullable(String)),
    `site_key` UInt64
"""


def _create_empty(client, target: str) -> None:
    client.command(f"DROP TABLE IF EXISTS {target} SYNC")
    client.command(
        f"""
        CREATE TABLE {target}
        ({_COLUMNS})
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(date)
        ORDER BY (date, campaign_id, ifNull(ad_group_id, 0), ad_network_type_key, ifNull(id_location, 0))
        """,
        settings=SAFE_QUERY_SETTINGS,
    )


def _insert_batch(client, target: str, lo: str, hi: str) -> None:
    client.command(
        f"""
        INSERT INTO {target}
        SELECT
            date,
            campaign_id,
            ad_group_id,
            lowerUTF8(trim(BOTH ' ' FROM ifNull(ad_network_type, ''))) AS ad_network_type_key,
            location_of_presence_id AS id_location,
            toDecimal64(sum(cost), 6) AS cost,
            toDecimal64(sum(clicks), 6) AS clicks,
            toDecimal64(sum(impressions), 6) AS impressions,
            toDecimal64(sum(all_forms), 6) AS all_forms,
            toDecimal64(sum(crm_order_created), 6) AS crm_order_created,
            toDecimal64(sum(crm_order_paid), 6) AS crm_order_paid,
            toDecimal64(sum(crm_spam_order), 6) AS crm_spam_order,
            toDecimal64(sum(crm_order_canceled), 6) AS crm_order_canceled,
            account_login,
            if(
                notEmpty(lowerUTF8(trim(BOTH ' ' FROM ifNull(anyLast(gs.domain), '')))),
                cityHash64(lowerUTF8(trim(BOTH ' ' FROM ifNull(anyLast(gs.domain), '')))),
                toUInt64(0)
            ) AS site_key
        FROM {STAGING_TABLE} y
        LEFT JOIN raw_data.gsheet_sites gs ON lower(ifNull(gs.login_key, '')) = lower(y.account_login)
        WHERE date >= toDate('{lo}') AND date < toDate('{hi}')
        GROUP BY date, campaign_id, ad_group_id, ad_network_type_key, location_of_presence_id, account_login
        """,
        settings=SAFE_QUERY_SETTINGS,
    )


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    logger.info("region_spend v6_ch: fact_region_spend")
    client = get_client()
    t0 = time.perf_counter()
    ensure_staging(client)
    shadow = "ad_analytics.fact_region_spend_new"
    _create_empty(client, shadow)
    ranges = day_ranges(DATE_FROM)
    for idx, (lo, hi) in enumerate(ranges, start=1):
        _insert_batch(client, shadow, lo, hi)
        logger.info("  region daily batch %d/%d: %s -> %s", idx, len(ranges), lo, hi)
    swap_shadow(client, "ad_analytics.fact_region_spend", shadow)
    rows = count_rows(client, "ad_analytics.fact_region_spend")
    logger.info("region_spend v6_ch завершён за %.1f сек: rows=%d", time.perf_counter() - t0, rows)
    return {"rows": rows, "details": f"fact_region_spend={rows:,}"}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())
