"""Build ClickHouse `fact_adformat_spend` from raw Yandex report rows."""

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

logger = logging.getLogger("pipeline.adformat_spend")


# FACT_WEIGHT_2026-08-14 (OPTIMIZATION_PLAN.md, фаза 2.2): явная схема с кодеками вместо вывода
# типов из CTAS-заглушки. Замер на однотипной fact_region_spend: −34.5% веса.
# Порядок колонок обязан совпадать с SELECT ниже: INSERT ... SELECT позиционный.
_COLUMNS = """
    `date` Date,
    `campaign_id` Int64 CODEC(T64, ZSTD(3)),
    `ad_group_id` Int64 CODEC(T64, ZSTD(3)),
    `ad_network_type_key` LowCardinality(String),
    `ad_format` LowCardinality(Nullable(String)),
    `cost` Decimal(18, 6) CODEC(ZSTD(3)),
    `clicks` Decimal(18, 6) CODEC(ZSTD(3)),
    `impressions` Decimal(18, 6) CODEC(ZSTD(3)),
    `account_login` LowCardinality(Nullable(String)),
    `site_key` UInt64
"""


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    logger.info("adformat_spend v6_ch: fact_adformat_spend")
    client = get_client()
    t0 = time.perf_counter()
    ensure_staging(client)
    shadow = "ad_analytics.fact_adformat_spend_new"
    client.command(f"DROP TABLE IF EXISTS {shadow} SYNC")
    client.command(
        f"""
        CREATE TABLE {shadow}
        ({_COLUMNS})
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(date)
        ORDER BY (date, campaign_id, ad_group_id, ad_network_type_key, ifNull(ad_format, ''))
        """,
        settings=SAFE_QUERY_SETTINGS,
    )
    ranges = day_ranges(DATE_FROM)
    for idx, (lo, hi) in enumerate(ranges, start=1):
        client.command(
            f"""
            INSERT INTO {shadow}
            SELECT
                date,
                campaign_id,
                ad_group_id,
                lowerUTF8(trim(BOTH ' ' FROM ifNull(ad_network_type, ''))) AS ad_network_type_key,
                ad_format,
                toDecimal64(sum(cost), 6) AS cost,
                toDecimal64(sum(clicks), 6) AS clicks,
                toDecimal64(sum(impressions), 6) AS impressions,
                account_login,
                if(
                    notEmpty(lowerUTF8(trim(BOTH ' ' FROM ifNull(anyLast(gs.domain), '')))),
                    cityHash64(lowerUTF8(trim(BOTH ' ' FROM ifNull(anyLast(gs.domain), '')))),
                    toUInt64(0)
                ) AS site_key
            FROM {STAGING_TABLE} y
            LEFT JOIN reference_data.gsheet_sites gs ON lower(ifNull(gs.login_key, '')) = lower(y.account_login)
            WHERE date >= toDate('{lo}') AND date < toDate('{hi}')
            GROUP BY date, campaign_id, ad_group_id, ad_network_type_key, ad_format, account_login
            """,
            settings=SAFE_QUERY_SETTINGS,
        )
        logger.info("  adformat daily batch %d/%d: %s -> %s", idx, len(ranges), lo, hi)
    swap_shadow(client, "ad_analytics.fact_adformat_spend", shadow)
    rows = count_rows(client, "ad_analytics.fact_adformat_spend")
    logger.info("adformat_spend v6_ch завершён за %.1f сек: rows=%d", time.perf_counter() - t0, rows)
    return {"rows": rows, "details": f"fact_adformat_spend={rows:,}"}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())
