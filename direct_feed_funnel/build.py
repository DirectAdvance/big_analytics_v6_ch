"""Build ClickHouse `fact_direct_feed_funnel` from raw report rows.

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

Пока эти источники не появятся в `raw_data`, таблицу нельзя считать витриной фидов —
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
from config.ch_utils import SAFE_QUERY_SETTINGS, count_rows, day_ranges, swap_shadow
from spend.build_direct_spend_staging import STAGING_TABLE, ensure_staging

logger = logging.getLogger("pipeline.direct_feed_funnel")


def _site_key_sql(expr: str = "domain") -> str:
    return (
        f"if(notEmpty(lowerUTF8(trim(BOTH ' ' FROM ifNull({expr}, '')))), "
        f"cityHash64(lowerUTF8(trim(BOTH ' ' FROM ifNull({expr}, '')))), toUInt64(0))"
    )


def fact_direct_feed_funnel_create_sql(target: str) -> str:
    return f"""
        CREATE TABLE {target}
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(date)
        ORDER BY (date, campaign_id, ad_group_id, placement_feed_key, site_key)
        AS
        SELECT
            toDate('2026-01-01') AS date,
            toInt64(0) AS campaign_id,
            toInt64(0) AS ad_group_id,
            '' AS placement_feed_key,
            CAST(NULL, 'Nullable(String)') AS domain,
            CAST(NULL, 'Nullable(String)') AS account_login,
            toUInt64(0) AS site_key,
            toDecimal64(0, 6) AS cost,
            toDecimal64(0, 6) AS clicks,
            toDecimal64(0, 6) AS impressions,
            toDecimal64(0, 6) AS all_forms,
            toDecimal64(0, 6) AS crm_order_created,
            toDecimal64(0, 6) AS crm_order_paid
        WHERE 0
    """


def fact_direct_feed_funnel_insert_sql(target: str, lo: str, hi: str) -> str:
    return f"""
        INSERT INTO {target}
        SELECT
            date,
            campaign_id,
            ad_group_id,
            placement_feed_key,
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
        GROUP BY date, campaign_id, ad_group_id, placement_feed_key, account_login, site_key
    """


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    logger.info("direct_feed_funnel v6_ch: fact_direct_feed_funnel")
    client = get_client()
    t0 = time.perf_counter()
    ensure_staging(client)
    shadow = "ad_analytics.fact_direct_feed_funnel_new"
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
    swap_shadow(client, "ad_analytics.fact_direct_feed_funnel", shadow)
    rows = count_rows(client, "ad_analytics.fact_direct_feed_funnel")
    logger.info("direct_feed_funnel v6_ch завершён за %.1f сек: rows=%d", time.perf_counter() - t0, rows)
    return {"rows": rows, "details": f"fact_direct_feed_funnel={rows:,}"}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())
