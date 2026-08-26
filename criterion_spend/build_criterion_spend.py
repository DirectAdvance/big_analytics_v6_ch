"""Build ClickHouse `fact_criterion_spend` from raw Yandex report rows."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_settings import DATE_FROM
from config.ch_utils import SAFE_QUERY_SETTINGS, count_rows, day_ranges, swap_shadow
from corrections import calls_specialist_correction_expr
from criterion_spend.cleaning import CRITERION_CLEAN
from spend.build_direct_spend_staging import STAGING_TABLE, ensure_staging
from spend.dated_site_join import gs_best_cte

logger = logging.getLogger("pipeline.criterion_spend")

# FACT_WEIGHT_2026-08-14 (OPTIMIZATION_PLAN.md, фаза 2.2): явная схема с кодеками вместо вывода
# типов из CTAS-заглушки. Замер на однотипной fact_region_spend: −34.5% веса.
# Порядок колонок обязан совпадать с SELECT ниже: INSERT ... SELECT позиционный.
_COLUMNS = """
    `date` Date,
    `campaign_id` Int64 CODEC(T64, ZSTD(3)),
    `ad_group_id` Int64 CODEC(T64, ZSTD(3)),
    `ad_network_type_key` LowCardinality(String),
    `criterion_id` Nullable(Int64) CODEC(T64, ZSTD(3)),
    `criterion_key` UInt64,
    `cost` Decimal(18, 6) CODEC(ZSTD(3)),
    `clicks` Decimal(18, 6) CODEC(ZSTD(3)),
    `impressions` Decimal(18, 6) CODEC(ZSTD(3)),
    `all_forms` Decimal(18, 6) CODEC(ZSTD(3)),
    `crm_order_created` Decimal(18, 6) CODEC(ZSTD(3)),
    `crm_order_paid` Decimal(18, 6) CODEC(ZSTD(3)),
    `crm_spam_order` Decimal(18, 6) CODEC(ZSTD(3)),
    `crm_order_canceled` Decimal(18, 6) CODEC(ZSTD(3)),
    `account_login` LowCardinality(Nullable(String)),
    `site_key` UInt64,
    `специалист` LowCardinality(Nullable(String))
"""


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    logger.info("criterion_spend v6_ch: fact_criterion_spend")
    client = get_client()
    t0 = time.perf_counter()
    ensure_staging(client)
    shadow = "ad_analytics.fact_criterion_spend_new"
    client.command(f"DROP TABLE IF EXISTS {shadow} SYNC")
    client.command(
        f"""
        CREATE TABLE {shadow}
        ({_COLUMNS})
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(date)
        ORDER BY (date, campaign_id, ad_group_id, ad_network_type_key, ifNull(criterion_id, 0), criterion_key)
        """,
        settings=SAFE_QUERY_SETTINGS,
    )
    ranges = day_ranges(DATE_FROM)
    for idx, (lo, hi) in enumerate(ranges, start=1):
        pairs_sql = (
            f"SELECT DISTINCT lower(trim(ifNull(account_login, ''))) AS login_key, date AS date_val "
            f"FROM {STAGING_TABLE} WHERE date >= toDate('{lo}') AND date < toDate('{hi}')"
        )
        # SPECIALIST_DATE_OVERRIDE_2026-08-25: same shared rule as region_spend, see there.
        specialist_expr = calls_specialist_correction_expr(
            "date",
            "account_login",
            "any(gb.directologist)",
            "CAST(NULL, 'Nullable(String)')",
        )
        client.command(
            f"""
            WITH
            {gs_best_cte(pairs_sql)}
            INSERT INTO {shadow}
            SELECT
                date,
                campaign_id,
                ad_group_id,
                lowerUTF8(trim(BOTH ' ' FROM ifNull(ad_network_type, ''))) AS ad_network_type_key,
                criterion_id,
                criterion_key,
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
                    notEmpty(lowerUTF8(trim(BOTH ' ' FROM ifNull(any(gb.domain), '')))),
                    cityHash64(lowerUTF8(trim(BOTH ' ' FROM ifNull(any(gb.domain), '')))),
                    toUInt64(0)
                ) AS site_key,
                {specialist_expr} AS `специалист`
            FROM {STAGING_TABLE} y
            LEFT JOIN gs_best gb
              ON gb.match_login_key = lower(trim(ifNull(y.account_login, '')))
             AND gb.match_date = y.date
            WHERE date >= toDate('{lo}') AND date < toDate('{hi}')
            GROUP BY date, campaign_id, ad_group_id, ad_network_type_key, criterion_id, criterion_norm, criterion_key, account_login
            """,
            settings=SAFE_QUERY_SETTINGS,
        )
        logger.info("  criterion daily batch %d/%d: %s -> %s", idx, len(ranges), lo, hi)
    swap_shadow(client, "ad_analytics.fact_criterion_spend", shadow)
    rows = count_rows(client, "ad_analytics.fact_criterion_spend")
    logger.info("criterion_spend v6_ch завершён за %.1f сек: rows=%d", time.perf_counter() - t0, rows)
    return {"rows": rows, "details": f"fact_criterion_spend={rows:,}"}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())
