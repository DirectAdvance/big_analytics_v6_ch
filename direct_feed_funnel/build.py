"""Build ClickHouse Direct feed aggregate for the existing PBI compatibility contract."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_settings import DATE_FROM
from config.ch_utils import SAFE_QUERY_SETTINGS, count_rows, day_ranges, replace_view, swap_shadow
from star_refactor.build_pbi_compat import build_dim_placement_feed

logger = logging.getLogger("pipeline.direct_feed_funnel")
LIGHT_TABLE = "ad_analytics.fact_direct_feed_funnel_light"
COMPAT_VIEW = "ad_analytics.fact_direct_feed_funnel"
RAW_FEED_REPORT = "raw_data.direct_feed_report_rows"
RAW_FEED_URLS = "raw_data.direct_cookie_feed_urls"


def _site_key_sql(expr: str = "domain") -> str:
    return (
        f"if(notEmpty(lowerUTF8(trim(BOTH ' ' FROM ifNull({expr}, '')))), "
        f"cityHash64(lowerUTF8(trim(BOTH ' ' FROM ifNull({expr}, '')))), toUInt64(0))"
    )


# FACT_WEIGHT_2026-08-14 (OPTIMIZATION_PLAN.md, фаза 2.2): явная схема с кодеками вместо вывода
# типов из CTAS-заглушки. Замер на однотипной fact_region_spend: −34.5% веса.
# Порядок колонок обязан совпадать с fact_direct_feed_funnel_insert_sql: INSERT позиционный.
_LIGHT_COLUMNS = """
    `date` Date,
    `campaign_id` Int64 CODEC(T64, ZSTD(3)),
    `ad_group_id` Int64 CODEC(T64, ZSTD(3)),
    `placement_feed_key_hash` UInt64,
    `domain` LowCardinality(Nullable(String)),
    `account_login` LowCardinality(Nullable(String)),
    `site_key` UInt64,
    `cost` Decimal(18, 6) CODEC(ZSTD(3)),
    `clicks` Decimal(18, 6) CODEC(ZSTD(3)),
    `impressions` Decimal(18, 6) CODEC(ZSTD(3)),
    `all_forms` Decimal(18, 6) CODEC(ZSTD(3)),
    `crm_order_created` Decimal(18, 6) CODEC(ZSTD(3)),
    `crm_order_paid` Decimal(18, 6) CODEC(ZSTD(3))
"""


def fact_direct_feed_funnel_create_sql(target: str) -> str:
    return f"""
        CREATE TABLE {target}
        ({_LIGHT_COLUMNS})
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(date)
        ORDER BY (date, campaign_id, ad_group_id, placement_feed_key_hash, site_key)
    """


def fact_direct_feed_funnel_insert_sql(target: str, lo: str, hi: str) -> str:
    return f"""
        INSERT INTO {target}
        WITH
            urls AS
            (
                SELECT
                    lowerUTF8(trim(BOTH ' ' FROM client_login)) AS client_login_key,
                    feed_id,
                    argMax(feed_name, loaded_at) AS feed_name,
                    argMax(feed_url, loaded_at) AS feed_url,
                    argMax(feed_url_key, loaded_at) AS feed_url_key
                FROM {RAW_FEED_URLS}
                GROUP BY client_login_key, feed_id
            )
        SELECT
            date,
            campaign_id,
            ad_group_id,
            cityHash64(placement_feed_key) AS placement_feed_key_hash,
            anyLast(domain) AS domain,
            account_login,
            site_key,
            toDecimal64(sum(cost), 6) AS cost,
            toDecimal64(sum(clicks), 6) AS clicks,
            toDecimal64(sum(impressions), 6) AS impressions,
            toDecimal64(sum(all_forms), 6) AS all_forms,
            toDecimal64(sum(crm_order_created), 6) AS crm_order_created,
            toDecimal64(sum(crm_order_paid), 6) AS crm_order_paid
        FROM
        (
            SELECT
                r.date,
                r.campaign_id,
                ifNull(r.ad_group_id, 0) AS ad_group_id,
                ifNull(
                    nullIf(lowerUTF8(trim(BOTH ' ' FROM ifNull(u.feed_url_key, ''))), ''),
                    ifNull(
                        nullIf(lowerUTF8(trim(BOTH ' ' FROM ifNull(r.feed_name, ''))), ''),
                        concat('feed:', toString(r.feed_id))
                    )
                ) AS placement_feed_key,
                domain(ifNull(u.feed_url, '')) AS domain,
                r.client_login AS account_login,
                {_site_key_sql("domain(ifNull(u.feed_url, ''))")} AS site_key,
                ifNull(r.cost, toDecimal128(0, 9)) AS cost,
                ifNull(r.clicks, 0) AS clicks,
                ifNull(r.impressions, 0) AS impressions,
                ifNull(r.all_forms, toDecimal128(0, 9)) AS all_forms,
                ifNull(r.crm_order_created, toDecimal128(0, 9)) AS crm_order_created,
                ifNull(r.crm_order_paid, toDecimal128(0, 9)) AS crm_order_paid
            FROM {RAW_FEED_REPORT} r
            LEFT JOIN urls u
              ON u.client_login_key = lowerUTF8(trim(BOTH ' ' FROM r.client_login))
             AND u.feed_id = r.feed_id
            WHERE r.date >= toDate('{lo}') AND r.date < toDate('{hi}')
        )
        WHERE date >= toDate('{lo}') AND date < toDate('{hi}')
        GROUP BY date, campaign_id, ad_group_id, placement_feed_key_hash, account_login, site_key
    """


def fact_direct_feed_funnel_view_sql(source: str = LIGHT_TABLE) -> str:
    return f"""
        WITH placement_feed AS
        (
            SELECT
                cityHash64(placement_feed_key) AS placement_feed_key_hash,
                anyLast(placement_feed_key) AS placement_feed_key_value
            FROM ad_analytics.Dim_PlacementFeed
            GROUP BY placement_feed_key_hash
        )
        SELECT
            f.date,
            f.campaign_id,
            f.ad_group_id,
            ifNull(pf.placement_feed_key_value, '') AS placement_feed_key,
            f.domain,
            f.account_login,
            f.site_key,
            f.cost,
            f.clicks,
            f.impressions,
            f.all_forms,
            f.crm_order_created,
            f.crm_order_paid
        FROM {source} f
        LEFT JOIN placement_feed pf ON pf.placement_feed_key_hash = f.placement_feed_key_hash
    """


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    logger.info("direct_feed_funnel v6_ch: raw Direct feed report + compatibility view")
    client = get_client()
    t0 = time.perf_counter()
    shadow = "ad_analytics.fact_direct_feed_funnel_light_new"
    client.command(f"DROP TABLE IF EXISTS {shadow} SYNC")
    client.command(
        fact_direct_feed_funnel_create_sql(shadow),
        settings=SAFE_QUERY_SETTINGS,
    )
    ranges = day_ranges(DATE_FROM)
    for idx, (lo, hi) in enumerate(ranges, start=1):
        client.command(
            fact_direct_feed_funnel_insert_sql(shadow, lo, hi),
            settings=SAFE_QUERY_SETTINGS,
        )
        logger.info("  direct_feed daily batch %d/%d: %s -> %s", idx, len(ranges), lo, hi)
    swap_shadow(client, LIGHT_TABLE, shadow)
    dim_rows = build_dim_placement_feed(client)
    replace_view(client, COMPAT_VIEW, fact_direct_feed_funnel_view_sql())
    rows = count_rows(client, COMPAT_VIEW)
    light_rows = count_rows(client, LIGHT_TABLE)
    logger.info(
        "direct_feed_funnel v6_ch завершён за %.1f сек: light_rows=%d view_rows=%d dim_rows=%d",
        time.perf_counter() - t0,
        light_rows,
        rows,
        dim_rows,
    )
    return {
        "rows": rows,
        "details": f"fact_direct_feed_funnel_light={light_rows:,}, view={rows:,}, Dim_PlacementFeed={dim_rows:,}",
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())
