"""Build ClickHouse Direct placement aggregate with a feed-funnel compatibility name.

⚠️ FEED_FUNNEL_NOT_PORTED_2026-08-05 — это НЕ воронка по фидам, это агрегат по площадкам РСЯ.

`placement_feed_key` приходит из `spend/build_direct_spend_staging.py:81` — это
`yandex_direct_report_rows.placement`, т.е. площадка РСЯ (`dzen.ru`, `com.vkontakte.android`,
`m.pogoda.yandex.ru`), а не товарный фид. Замер 2026-08-05: 12 573 546 строк, 33 879 «ключей
фида», расход 1 251 794 248 ₽ (= весь расход Директа). Эталон v5 (`public.fact_direct_feed_funnel`,
собирается `work/big_analytics_v5/direct_feed_funnel/build_keyed.py`): 81 786 строк, 60 фидов.

Порт v5 НЕВОЗМОЖЕН без источников — в ClickHouse их нет (проверено по `system.tables`):
  • `yandex_direct_feeds_report` (v5: 1 029 502 строки, 2026-01-01..2026-07-31, 21 колонка) —
    сам отчёт по фидам из API Директа; вся расходная сторона воронки;
  • `yandex_direct_feed_urls` (v5: 9 712 строк) — реестр URL фидов (наливается
    `direct_feed_funnel/fetch_feed_urls_cookie.py` по кукам);
  • `direct_global_feed_rules` (v5: 14 строк) — правила стабилизации ключа фида;
  • CRM-база shadow orders (`load_db('shadow_orders')` → `public.orders`) — лидовая сторона,
    из неё вытаскивается `fid` из `entry_point`.

Пока эти источники не появятся в `raw_data`, compatibility view нельзя считать витриной фидов —
и её нельзя чинить правкой этого файла. Замену источников не выдумывать.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_settings import DATE_FROM
from config.ch_utils import SAFE_QUERY_SETTINGS, count_rows, day_ranges, replace_view, swap_shadow
from spend.build_direct_spend_staging import STAGING_TABLE, ensure_staging
from star_refactor.build_pbi_compat import build_dim_placement_feed

logger = logging.getLogger("pipeline.direct_feed_funnel")
LIGHT_TABLE = "ad_analytics.fact_direct_feed_funnel_light"
COMPAT_VIEW = "ad_analytics.fact_direct_feed_funnel"


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
                date,
                campaign_id,
                ad_group_id,
                placement_feed_key,
                domain,
                account_login,
                {_site_key_sql()} AS site_key,
                cost,
                clicks,
                impressions,
                all_forms,
                crm_order_created,
                crm_order_paid
            FROM {STAGING_TABLE}
            WHERE date >= toDate('{lo}') AND date < toDate('{hi}')
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
    logger.info("direct_feed_funnel v6_ch: fact_direct_feed_funnel_light + compatibility view")
    client = get_client()
    t0 = time.perf_counter()
    ensure_staging(client)
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
