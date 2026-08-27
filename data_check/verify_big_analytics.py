#!/usr/bin/env python3
"""Master verification for big_analytics_v6_ch ClickHouse tables."""

from __future__ import annotations

import argparse
import logging
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_settings import GSHEET_SITES_EFFECTIVE
from config.ch_utils import SAFE_QUERY_SETTINGS, count_rows, table_exists
from corrections import _KUDERKO_DATE, _KUDERKO_LOGINS  # единственный источник — не дублировать
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("verify_big_analytics")


REQUIRED_TABLES = [
    "raw_yandex",
    "raw_leads",
    "raw_calls",
    "big_analytics_full",
    "big_analytics_pixel_score",
    "big_analytics_full_arrival",
    "big_analytics_unified",
    "fact_big_analytics",
    "pbi_big_analytics_full",
    "pixel_score",
    "Dim_Criterion",
    "dim_criterion",
]

PBI_SOURCE_OBJECTS = [
    "Dim_AdGroup",
    "Dim_AdNetworkType",
    "Dim_Campaign",
    "Dim_Date",
    "Dim_Device",
    "Dim_Location",
    "Dim_ManagerLogin",
    "Dim_PlacementFeed",
    "Dim_Site",
    "Dim_Source",
    "Dim_VkAdGroup",
    "Dim_VkAdPlan",
    "Dim_VkBanner",
    "pbi_big_analytics_full",
    # `big_analytics_full_arrival` здесь БЫТЬ НЕ ДОЛЖНО: с ca7174e (2026-08-04)
    # `bi_big_analytics_full_arrival` переведена в `LEGACY_BI_VIEWS`
    # (`star_refactor/build_pbi_compat.py`) и штатно дропается в `drop_bi_views()`.
    # Сама витрина `big_analytics_full_arrival` проверяется выше — в
    # `REQUIRED_TABLES` и `WIDE_COMPAT_VIEWS`.
    "check_utm_fuck_direct",
    "dim_criterion",
    "yandex_direct_history",
    "fact_adformat_spend",
    "fact_criterion_spend",
    "fact_criterion_zayavki",
    "fact_direct_feed_funnel",
    "fact_ml_korrektirovki",
    "fact_region_spend",
    "fact_region_zayavki",
    "fact_vk_ads",
    "pixel_score",
    "v_yandex_direct_minus_delta",
    "yandex_direct_404_errors",
    "yandex_direct_cookie_analytics_website_pages",
    "yandex_direct_korrektirovki",
    "yandex_direct_minus_snapshot",
    # ARP_LIVE_2026-08-23: живая замена замороженным снимкам БА5 `raw_new_arp_fact` и
    # `raw_new_search_query_report_master_pbi`. Пустая `bi_*` = FAIL гейта.
    "analytics_report_placement",
    "yandex_direct_search_query_report_master",
]

# Пустых `bi_*` больше нет by-design: любой ноль в активном PBI-контракте — FAIL.
PBI_EMPTY_ALLOWED = set()

# Заглушки без источника в ClickHouse больше не разрешены.
PBI_EMPTY_BY_DESIGN = set()

PBI_COMPAT_OBJECTS = [
    "pbi_import_big_analytics_full",
    "pbi_import_fact_direct_feed_funnel",
    "pbi_import_region_spend",
]

FULL_PHYSICAL_TABLES = [
    "pixel_score",
    "Dim_Criterion",
    "Dim_AdFormat",
    "Dim_AdNetworkType",
    "Dim_Device",
    "Dim_Source",
]

COMPAT_VIEWS = [
    "dim_criterion",
]

# PIXEL_DEDUP_2026-08-15 (распоряжение Семёна): `big_analytics_pixel_score` убран отсюда.
# Этот список требует от объекта движок `View`, а таблица теперь физическая по замыслу: с оси
# заявок убрали дубль `пиксель_атрибуц`, поэтому вьюхе над `fact_big_analytics` больше нечего
# фильтровать. Проверка не потеряна — существование и непустота этой же таблицы остаются в
# `REQUIRED_TABLES` (единственное, что ушло, — требование быть вьюхой).
WIDE_COMPAT_VIEWS = [
    "big_analytics_full",
    "big_analytics_full_arrival",
    "big_analytics_unified",
]

# GOLDEN_TOL_WIDENED_2026-08-07 (v6 ONLY — распоряжение Семёна, НЕ трогать v5's эталон/допуск).
# Эталон 25 422 774.00 ±100 был перенесён в v6 из контура v5 при миграции (v5 golden стабилен,
# дрейф +24 ₽ прогон за прогоном, PASS). В v6 допуск ±100 нестабилен: из 4 последних прогонов
# golden прошёл только ОДИН (9c33cc1e5196, delta +30.03), три упали (187c0ac48b29 delta +531.85,
# 78804a3bcf30, a460eeed0b83).
#
# Root-cause установлен ФАКТОМ (2026-08-07, KNOWN_ISSUES.md #37) — не дефект ETL/атрибуции v6, а
# незавершённый бэкфил ВНЕШНЕГО загрузчика (вне репозитория, вне мониторинга raw_data.etl_runs),
# который на 2026-08-07 всё ещё доливает историю по 38 из 67 логинов `_KUDERKO_LOGINS` в
# raw_data.yandex_direct_report_rows. Более ранняя гипотеза про «~50 логинов с индивидуальными
# коэффициентами и 63 аккаунта-призрака» (#33/#35) и про spec_fallback проверена и ОТВЕРГНУТА как
# причина этого конкретного дрейфа — смежное наблюдение, не источник.
# ⚠️ Расширение ±100→±1000 — ОБХОД, НЕ ФИКС (решение Семёна: допуск сам по себе не лечит неполноту
# сырья, откат не нужен). См. `_kuderko_raw_coverage()` ниже и KNOWN_ISSUES.md #37 — ±1000 при этом
# ОСТАЁТСЯ, но golden понижается до информационного статуса ТОЛЬКО пока сырьё неполно; как только
# присутствуют все 67 — golden снова блокирует прогон как жёсткий гейт.
# ARP_FUNNEL_GATE_2026-08-23. Счётчик строк `bi_analytics_report_placement` задаётся стороной
# Директа (`fact_direct_feed_funnel`) и остаётся ненулевым, даже если воронка не приклеилась ни к
# одной строке — проверка `rows > 0` такую регрессию НЕ ловит. Реальный случай: площадка поиска
# в БА5 звалась `Yandex`, в BA6 — `Яндекс`, из-за чего 33% воронки молча не матчилось.
# Порог — доля заявок вьюхи от лидов `raw_leads`, годных к матчингу (тот же фильтр, что в
# `_arp_lead_metrics_sql` + `campaign_id IS NOT NULL`). Замер 2026-08-23: здоровое значение 0.517
# (помесячно 0.401..0.632), с воспроизведённым багом локали — 0.373. Пол 0.40 ловит баг и лежит
# ниже худшего наблюдённого месяца.
ARP_FUNNEL_VIEW = "bi_analytics_report_placement"
ARP_FUNNEL_RATIO_FLOOR = 0.40

GOLDEN_COST = Decimal("25422774.00")
GOLDEN_COST_TOL = Decimal("1000.00")
GOLDEN_SALES_FLOOR = 54
GOLDEN_SPECIALIST = "Кудерко Семен"
GOLDEN_SOURCES = ("direct", "tp8", "tp9", "tp10", "seo", "calls", "direct_unmatched", "direct_zero")
GOLDEN_FACT_COLUMNS = {"salon_key", "атрибуция", "_source_table", "total_cost", "prodazhi"}
GOLDEN_SALON_COLUMNS = {"salon_key", "специалист"}
PBI_RESTORED_TEXT_COLUMNS = {
    "специалист",
    "Название crm",
    "тип_заявки",
    "статус",
    "салон",
    "город",
    "регион",
}
DIRECT_SPEND_LOSS_TOLERANCE = Decimal("1.00")
BI_CONTRACT_TOLERANCE = Decimal("1.00")
CLOSED_MONTH_DRIFT_RATIO = Decimal("0.04")
BAD_SPECIALISTS = ("", "Без специалиста", "Звонки", "Посевы", "Тоборев Владимир")


def _golden_kuderko_sql(source_sql: str) -> str:
    return f"""
        SELECT
            round(sum(f.total_cost), 2) AS rashod,
            round(sum(f.prodazhi)) AS prodazhi
        FROM ad_analytics.fact_big_analytics f
        LEFT JOIN ad_analytics.Dim_Salon dsl ON dsl.salon_key = f.salon_key
        WHERE dsl.`специалист` = {{specialist:String}}
          AND f.`атрибуция` = 'По дате заявки'
          AND f.`_source_table` IN ({source_sql})
    """


def _arp_funnel_liveness(client) -> tuple[int, int]:
    """Заявки, доехавшие до `bi_analytics_report_placement`, и лиды, годные к матчингу."""
    row = client.query(
        f"""
        SELECT
            (SELECT toInt64(ifNull(sum(kol_vo_zayavok), 0))
             FROM ad_analytics.`{ARP_FUNNEL_VIEW}`) AS view_leads,
            (SELECT toInt64(count())
             FROM ad_analytics.raw_leads
             WHERE ifNull(deal_type, '') != 'Звонок'
               AND is_copy_for_removal = 0
               AND campaign_id IS NOT NULL) AS eligible_leads
        """,
        settings=SAFE_QUERY_SETTINGS,
    ).result_rows[0]
    return int(row[0]), int(row[1])


def _scalar(client, sql: str):
    return client.query(sql).result_rows[0][0]


def _engine(client, table: str) -> str | None:
    rows = client.query(
        """
        SELECT engine
        FROM system.tables
        WHERE database='ad_analytics' AND name={table:String}
        """,
        parameters={"table": table},
    ).result_rows
    return rows[0][0] if rows else None


def _has_columns(client, table: str, columns: set[str]) -> bool:
    rows = client.query(
        """
        SELECT name
        FROM system.columns
        WHERE database='ad_analytics' AND table={table:String}
        """,
        parameters={"table": table},
    ).result_rows
    existing = {row[0] for row in rows}
    return columns.issubset(existing)


def _direct_spend_loss_rows(client) -> list[tuple]:
    return client.query(
        f"""
        WITH auto_domains AS
        (
            SELECT DISTINCT lowerUTF8(trim(ifNull(domain, ''))) AS domain
            FROM {GSHEET_SITES_EFFECTIVE}
            WHERE niche = 'Авто'
              AND ifNull(domain, '') != ''
        ),
        raw AS
        (
            SELECT
                toStartOfMonth(`Date`) AS month,
                lowerUTF8(trim(account_login)) AS account_login,
                lowerUTF8(trim(ifNull(domain, ''))) AS domain,
                round(sum(total_cost), 2) AS raw_cost
            FROM ad_analytics.raw_yandex
            WHERE lowerUTF8(trim(ifNull(domain, ''))) IN (SELECT domain FROM auto_domains)
            GROUP BY month, account_login, domain
        ),
        direct AS
        (
            SELECT
                toStartOfMonth(`Date`) AS month,
                lowerUTF8(trim(account_login)) AS account_login,
                lowerUTF8(trim(ifNull(domain, ''))) AS domain,
                round(sum(total_cost), 2) AS direct_cost
            FROM ad_analytics.big_analytics_direct
            WHERE ifNull(domain, '') != ''
            GROUP BY month, account_login, domain
        )
        SELECT
            raw.month,
            raw.account_login,
            raw.domain,
            raw.raw_cost,
            ifNull(direct.direct_cost, toDecimal64(0, 2)) AS direct_cost,
            raw.raw_cost - ifNull(direct.direct_cost, toDecimal64(0, 2)) AS missing_cost
        FROM raw
        LEFT JOIN direct USING (month, account_login, domain)
        WHERE raw.raw_cost > ifNull(direct.direct_cost, toDecimal64(0, 2)) + {{tolerance:Decimal(18, 2)}}
        ORDER BY missing_cost DESC
        LIMIT 20
        """,
        parameters={"tolerance": DIRECT_SPEND_LOSS_TOLERANCE},
        settings=SAFE_QUERY_SETTINGS,
    ).result_rows


def _auto_spend_pbi_loss_rows(client) -> list[tuple]:
    return client.query(
        f"""
        WITH auto_domains AS
        (
            SELECT DISTINCT lowerUTF8(trim(ifNull(domain, ''))) AS domain
            FROM {GSHEET_SITES_EFFECTIVE}
            WHERE niche = 'Авто'
              AND ifNull(domain, '') != ''
        ),
        raw AS
        (
            SELECT
                toStartOfMonth(`Date`) AS month,
                lowerUTF8(trim(account_login)) AS account_login,
                lowerUTF8(trim(ifNull(domain, ''))) AS domain,
                round(sum(total_cost), 2) AS raw_cost
            FROM ad_analytics.raw_yandex
            WHERE lowerUTF8(trim(ifNull(domain, ''))) IN (SELECT domain FROM auto_domains)
            GROUP BY month, account_login, domain
        ),
        pbi AS
        (
            SELECT
                toStartOfMonth(`Date`) AS month,
                lowerUTF8(trim(splitByChar('|', `аккаунт|сайт`)[1])) AS account_login,
                lowerUTF8(trim(ifNull(`домен`, ''))) AS domain,
                round(sum(total_cost), 2) AS pbi_cost
            FROM ad_analytics.pbi_big_analytics_full
            WHERE lowerUTF8(trim(ifNull(`домен`, ''))) IN (SELECT domain FROM auto_domains)
            GROUP BY month, account_login, domain
        )
        SELECT
            raw.month,
            raw.account_login,
            raw.domain,
            raw.raw_cost,
            ifNull(pbi.pbi_cost, toDecimal64(0, 2)) AS pbi_cost,
            raw.raw_cost - ifNull(pbi.pbi_cost, toDecimal64(0, 2)) AS missing_cost
        FROM raw
        LEFT JOIN pbi USING (month, account_login, domain)
        WHERE raw.raw_cost > ifNull(pbi.pbi_cost, toDecimal64(0, 2)) + {{tolerance:Decimal(18, 2)}}
        ORDER BY missing_cost DESC
        LIMIT 20
        """,
        parameters={"tolerance": BI_CONTRACT_TOLERANCE},
        settings=SAFE_QUERY_SETTINGS,
    ).result_rows


def _direct_feed_spend_loss_rows(client) -> list[tuple]:
    if not (
        table_exists(client, "raw_data", "direct_feed_report_rows")
        and table_exists(client, "raw_data", "direct_cookie_feed_urls")
    ):
        return []
    return client.query(
        """
        WITH urls AS
        (
            SELECT
                lowerUTF8(trim(BOTH ' ' FROM client_login)) AS client_login_key,
                feed_id,
                argMax(feed_url, loaded_at) AS feed_url
            FROM raw_data.direct_cookie_feed_urls
            GROUP BY client_login_key, feed_id
        ),
        raw AS
        (
            SELECT
                toStartOfMonth(r.date) AS month,
                lowerUTF8(trim(r.client_login)) AS account_login,
                lowerUTF8(trim(domain(ifNull(u.feed_url, '')))) AS domain,
                round(sum(ifNull(r.cost, toDecimal128(0, 9))), 2) AS raw_cost
            FROM raw_data.direct_feed_report_rows r
            LEFT JOIN urls u
              ON u.client_login_key = lowerUTF8(trim(BOTH ' ' FROM r.client_login))
             AND u.feed_id = r.feed_id
            WHERE positionCaseInsensitive(ifNull(r.campaign_name, ''), 'tp8') = 0
              AND positionCaseInsensitive(ifNull(r.campaign_name, ''), 'tp9') = 0
              AND positionCaseInsensitive(ifNull(r.campaign_name, ''), 'tp10') = 0
            GROUP BY month, account_login, domain
        ),
        final AS
        (
            SELECT
                toStartOfMonth(date) AS month,
                lowerUTF8(trim(ifNull(account_login, ''))) AS account_login,
                lowerUTF8(trim(ifNull(domain, ''))) AS domain,
                round(sum(cost), 2) AS final_cost
            FROM ad_analytics.fact_direct_feed_funnel
            GROUP BY month, account_login, domain
        )
        SELECT
            raw.month,
            raw.account_login,
            raw.domain,
            raw.raw_cost,
            ifNull(final.final_cost, toDecimal64(0, 2)) AS final_cost,
            raw.raw_cost - ifNull(final.final_cost, toDecimal64(0, 2)) AS missing_cost
        FROM raw
        LEFT JOIN final USING (month, account_login, domain)
        WHERE raw.raw_cost > ifNull(final.final_cost, toDecimal64(0, 2)) + {tolerance:Decimal(18, 2)}
        ORDER BY missing_cost DESC
        LIMIT 20
        """,
        parameters={"tolerance": BI_CONTRACT_TOLERANCE},
        settings=SAFE_QUERY_SETTINGS,
    ).result_rows


def _pbi_funnel_loss_rows(client) -> list[tuple]:
    return client.query(
        """
        WITH source AS
        (
            SELECT
                toStartOfMonth(Date) AS month,
                'По дате заявки' AS attribution,
                round(sum(total_cost), 2) AS cost,
                round(sum(kol_vo_zayavok), 4) AS obrashenia,
                round(sum(korr), 4) AS zayavki,
                round(sum(kval), 4) AS kval,
                round(sum(priezd), 4) AS priezd,
                round(sum(prodazhi), 4) AS prodazhi
            FROM ad_analytics.big_analytics_full
            GROUP BY month

            UNION ALL

            SELECT
                toStartOfMonth(Date) AS month,
                'По дате визита' AS attribution,
                round(sum(total_cost), 2) AS cost,
                round(sum(kol_vo_zayavok), 4) AS obrashenia,
                round(sum(korr), 4) AS zayavki,
                round(sum(kval), 4) AS kval,
                round(sum(priezd), 4) AS priezd,
                round(sum(prodazhi), 4) AS prodazhi
            FROM ad_analytics.big_analytics_full_arrival
            GROUP BY month
        ),
        pbi AS
        (
            SELECT
                toStartOfMonth(Date) AS month,
                `атрибуция` AS attribution,
                round(sum(total_cost), 2) AS cost,
                round(sum(`Обращения`), 4) AS obrashenia,
                round(sum(korr), 4) AS zayavki,
                round(sum(kval), 4) AS kval,
                round(sum(priezd), 4) AS priezd,
                round(sum(prodazhi), 4) AS prodazhi
            FROM ad_analytics.pbi_big_analytics_full
            GROUP BY month, attribution
        )
        SELECT
            source.month,
            source.attribution,
            source.cost,
            ifNull(pbi.cost, 0) AS pbi_cost,
            source.obrashenia,
            ifNull(pbi.obrashenia, 0) AS pbi_obrashenia,
            source.zayavki,
            ifNull(pbi.zayavki, 0) AS pbi_zayavki,
            source.kval,
            ifNull(pbi.kval, 0) AS pbi_kval,
            source.priezd,
            ifNull(pbi.priezd, 0) AS pbi_priezd,
            source.prodazhi,
            ifNull(pbi.prodazhi, 0) AS pbi_prodazhi
        FROM source
        LEFT JOIN pbi USING (month, attribution)
        WHERE abs(source.cost - ifNull(pbi.cost, 0)) > {tolerance:Decimal(18, 2)}
           OR abs(source.obrashenia - ifNull(pbi.obrashenia, 0)) > 0.001
           OR abs(source.zayavki - ifNull(pbi.zayavki, 0)) > 0.001
           OR abs(source.kval - ifNull(pbi.kval, 0)) > 0.001
           OR abs(source.priezd - ifNull(pbi.priezd, 0)) > 0.001
           OR abs(source.prodazhi - ifNull(pbi.prodazhi, 0)) > 0.001
        ORDER BY month DESC, attribution
        LIMIT 20
        """,
        parameters={"tolerance": BI_CONTRACT_TOLERANCE},
        settings=SAFE_QUERY_SETTINGS,
    ).result_rows


def _closed_month_drift_rows(client) -> list[tuple]:
    if not table_exists(client, "ad_analytics", "pipeline_run_snapshot_v"):
        return []
    return client.query(
        """
        WITH previous_run AS
        (
            SELECT run_id
            FROM ad_analytics.pipeline_run_snapshot_v
            ORDER BY recorded_at DESC
            LIMIT 1
        ),
        prev_base AS
        (
            SELECT
                month,
                round(sum(cost), 2) AS cost,
                round(sum(prodazhi), 4) AS sales
            FROM ad_analytics.pipeline_run_snapshot_v
            WHERE run_id IN (SELECT run_id FROM previous_run)
              AND month < toStartOfMonth(today())
            GROUP BY month
        ),
        cur_base AS
        (
            SELECT
                toStartOfMonth(Date) AS month,
                round(sum(total_cost), 2) AS cost,
                round(sum(prodazhi), 4) AS sales
            FROM ad_analytics.fact_big_analytics
            WHERE `атрибуция` = 'По дате заявки'
              AND toStartOfMonth(Date) < toStartOfMonth(today())
            GROUP BY month
        ),
        prev AS
        (
            SELECT
                month,
                cost,
                sales,
                if(sales = 0, toFloat64(0), toFloat64(cost / sales)) AS cps
            FROM prev_base
        ),
        cur AS
        (
            SELECT
                month,
                cost,
                sales,
                if(sales = 0, toFloat64(0), toFloat64(cost / sales)) AS cps
            FROM cur_base
        ),
        metrics AS
        (
            SELECT month, 'sales' AS metric, toFloat64(prev.sales) AS prev_value, toFloat64(cur.sales) AS cur_value
            FROM prev INNER JOIN cur USING (month)
            UNION ALL
            SELECT month, 'cpl_sale' AS metric, prev.cps AS prev_value, cur.cps AS cur_value
            FROM prev INNER JOIN cur USING (month)
            WHERE prev.cps > 0 AND cur.cps > 0
        )
        SELECT
            month,
            metric,
            round(prev_value, 4) AS prev_value,
            round(cur_value, 4) AS cur_value,
            round((cur_value - prev_value) / prev_value, 6) AS drift_ratio
        FROM metrics
        WHERE prev_value > 0
          AND abs((cur_value - prev_value) / prev_value) > {ratio:Float64}
        ORDER BY abs(drift_ratio) DESC
        LIMIT 20
        """,
        parameters={"ratio": float(CLOSED_MONTH_DRIFT_RATIO)},
        settings=SAFE_QUERY_SETTINGS,
    ).result_rows


def _auto_empty_dims_rows(client) -> list[tuple]:
    return client.query(
        f"""
        WITH auto_domains AS
        (
            SELECT DISTINCT lowerUTF8(trim(ifNull(domain, ''))) AS domain
            FROM {GSHEET_SITES_EFFECTIVE}
            WHERE niche = 'Авто'
              AND ifNull(domain, '') != ''
        )
        SELECT
            toStartOfMonth(Date) AS month,
            ifNull(`домен`, '') AS domain,
            ifNull(`специалист`, '') AS specialist,
            ifNull(`город`, '') AS city,
            ifNull(`салон`, '') AS salon,
            round(sum(total_cost), 2) AS cost,
            round(sum(`Обращения`), 4) AS obrashenia,
            round(sum(prodazhi), 4) AS sales
        FROM ad_analytics.pbi_big_analytics_full
        WHERE lowerUTF8(trim(ifNull(`домен`, ''))) IN (SELECT domain FROM auto_domains)
          AND (
              ifNull(trim(`город`), '') = ''
              OR ifNull(trim(`салон`), '') = ''
          )
        GROUP BY month, domain, specialist, city, salon
        HAVING cost > {{tolerance:Decimal(18, 2)}}
            OR obrashenia > 0.001
            OR sales > 0.001
        ORDER BY cost DESC, obrashenia DESC, sales DESC
        LIMIT 20
        """,
        parameters={"tolerance": BI_CONTRACT_TOLERANCE},
        settings=SAFE_QUERY_SETTINGS,
    ).result_rows


def _sales_without_real_specialist_rows(client) -> list[tuple]:
    return client.query(
        """
        WITH rows AS
        (
            SELECT
                'request' AS axis,
                `источник` AS source,
                `специалист` AS specialist,
                domain,
                account_login,
                total_cost,
                prodazhi
            FROM ad_analytics.big_analytics_full
            WHERE ifNull(prodazhi, 0) > 0

            UNION ALL

            SELECT
                'arrival' AS axis,
                `источник` AS source,
                `специалист` AS specialist,
                domain,
                account_login,
                total_cost,
                prodazhi
            FROM ad_analytics.big_analytics_full_arrival
            WHERE ifNull(prodazhi, 0) > 0
        )
        SELECT
            axis,
            source,
            ifNull(specialist, '') AS specialist,
            ifNull(domain, '') AS domain,
            ifNull(account_login, '') AS account_login,
            round(sum(total_cost), 2) AS cost,
            round(sum(prodazhi), 4) AS sales
        FROM rows
        WHERE ifNull(trim(specialist), '') IN {bad_specialists:Array(String)}
        GROUP BY axis, source, specialist, domain, account_login
        ORDER BY sales DESC, cost DESC
        LIMIT 20
        """,
        parameters={"bad_specialists": list(BAD_SPECIALISTS)},
        settings=SAFE_QUERY_SETTINGS,
    ).result_rows


def _pbi_blank_specialist_rows(client) -> list[tuple]:
    return client.query(
        """
        SELECT
            toStartOfMonth(Date) AS month,
            ifNull(`специалист`, '') AS specialist,
            ifNull(`домен`, '') AS domain,
            ifNull(`аккаунт|сайт`, '') AS account_site,
            round(sum(total_cost), 2) AS cost,
            round(sum(`Обращения`), 4) AS obrashenia,
            round(sum(prodazhi), 4) AS sales
        FROM ad_analytics.pbi_big_analytics_full
        WHERE ifNull(trim(`специалист`), '') = ''
        GROUP BY month, specialist, domain, account_site
        HAVING cost > {tolerance:Decimal(18, 2)}
            OR obrashenia > 0.001
            OR sales > 0.001
        ORDER BY cost DESC, obrashenia DESC, sales DESC
        LIMIT 20
        """,
        parameters={"tolerance": BI_CONTRACT_TOLERANCE},
        settings=SAFE_QUERY_SETTINGS,
    ).result_rows


def _kuderko_raw_coverage(client) -> tuple[int, int, list[str]]:
    """Сколько логинов `corrections._KUDERKO_LOGINS` реально есть в сыром источнике.

    Golden Кудерко (`GOLDEN_COST`/`GOLDEN_SALES_FLOOR`) посчитан по ВСЕМ 67 логинам разом.
    Внешний загрузчик (вне этого репозитория, вне `raw_data.etl_runs`) на 2026-08-07 всё ещё
    доливает историю по части из них батчами — см. KNOWN_ISSUES.md #37. Пока хотя бы один
    логин пуст, сравнение факта с эталоном методологически некорректно: это не дефект ETL/
    атрибуции, а неполнота исходных данных. Возвращает (present, total, missing_logins) —
    `missing_logins` НЕ обрезан, обрезку делает вызывающий код для компактности лога.

    Нормализация логина ЗЕРКАЛИТ правило-1 (`corrections.specialist_correction_expr`,
    `corrections.py:174`: `lowerUTF8(trim(ifNull(account_expr, '')))`) — сравнение раньше было
    точным (`client_login IN (...)`), а `_KUDERKO_LOGINS` уже все в нижнем регистре, так что
    сегодня оба варианта дают одинаковые 29/67; но без нормализации здесь логин с иным регистром
    в новом батче пройдёт правило-1, а этот guard ложно посчитает его «отсутствует» (director
    review, ЗАМЕЧАНИЕ 3).

    ⚠️ «Присутствует» — это ПРОКСИ, не «полностью загружен»: логин может иметь ровно одну строку
    ВНЕ golden-окна правила-1 (`day < 2026-04-10`, пример-факт: `porg-riga5gvo` — 2 строки только
    за 2026-06-18) и всё равно засчитается «есть». Так специально: `present_pre_cutoff` в
    `_kuderko_pre_cutoff_presence()` — отдельный диагностический счётчик именно под этот случай
    (ЗАМЕЧАНИЕ 4), основной счётчик здесь НЕ фильтруется по дате намеренно (иначе логин без
    до-апрельских данных в принципе никогда не засчитается «есть», и guard не снимется НИКОГДА —
    это уже настоящее маскирование, см. KNOWN_ISSUES.md #37).
    """
    logins = _KUDERKO_LOGINS
    total = len(logins)
    if not table_exists(client, "raw_data", "yandex_direct_report_rows"):
        return 0, total, list(logins)
    rows = client.query(
        """
        SELECT DISTINCT lowerUTF8(trim(client_login)) AS login
        FROM raw_data.yandex_direct_report_rows
        WHERE lowerUTF8(trim(client_login)) IN {logins:Array(String)}
        """,
        parameters={"logins": list(logins)},
        settings=SAFE_QUERY_SETTINGS,
    ).result_rows
    present_logins = {row[0] for row in rows}
    missing = [login for login in logins if login not in present_logins]
    return len(present_logins), total, missing


def _kuderko_pre_cutoff_presence(client) -> int:
    """Сколько логинов `_KUDERKO_LOGINS` имеют хотя бы одну строку СТРОГО ДО отсечки правила-1.

    Диагностический ДОПОЛНИТЕЛЬНЫЙ счётчик (director review, ЗАМЕЧАНИЕ 4) — НЕ гейт, НЕ влияет на
    `golden_raw_incomplete`/`failures`, только логируется. `_kuderko_raw_coverage()` выше считает
    логин «есть» по ЛЮБОЙ строке, включая строки вне golden-окна (`day < _KUDERKO_DATE`) — значит
    основной счётчик может дойти до 67/67 и снять guard, хотя под реальным golden-окном сырьё
    всё ещё неполное. Этот счётчик делает такое расхождение видимым в логе на момент снятия guard'а.
    """
    logins = _KUDERKO_LOGINS
    if not table_exists(client, "raw_data", "yandex_direct_report_rows"):
        return 0
    rows = client.query(
        """
        SELECT count(DISTINCT lowerUTF8(trim(client_login)))
        FROM raw_data.yandex_direct_report_rows
        WHERE lowerUTF8(trim(client_login)) IN {logins:Array(String)}
          AND day < {cutoff:String}
        """,
        parameters={"logins": list(logins), "cutoff": _KUDERKO_DATE},
        settings=SAFE_QUERY_SETTINGS,
    ).result_rows
    return int(rows[0][0]) if rows else 0


def run(full: bool = False, no_star: bool = False, tg: bool = False) -> int:  # noqa: ARG001
    client = get_client()
    failures: list[str] = []

    for table in REQUIRED_TABLES:
        if no_star and table == "fact_big_analytics":
            continue
        if not table_exists(client, "ad_analytics", table):
            failures.append(f"missing:{table}")
            continue
        rows = count_rows(client, f"ad_analytics.{table}")
        log.info("%s=%d", table, rows)
        if table not in {"big_analytics_crop_targeting"} and rows == 0:
            failures.append(f"empty:{table}")

    for source in PBI_SOURCE_OBJECTS:
        view = f"bi_{source}"
        engine = _engine(client, view)
        if engine is None:
            failures.append(f"missing_pbi_view:{view}")
            continue
        if engine != "View":
            failures.append(f"pbi_not_view:{view}:{engine}")
        rows = count_rows(client, f"ad_analytics.`{view}`")
        log.info("%s=%d engine=%s", view, rows, engine)
        if rows == 0 and view not in PBI_EMPTY_ALLOWED:
            failures.append(f"empty_pbi_view:{view}")
        elif rows == 0 and view not in PBI_EMPTY_BY_DESIGN:
            log.warning(
                "PBI_VIEW_EMPTY_WHITELISTED: %s пуст, но покрыт PBI_EMPTY_ALLOWED — "
                "гейт не падает. Проверить, регрессия это или норма.", view
            )

    view_leads, eligible_leads = _arp_funnel_liveness(client)
    ratio = view_leads / eligible_leads if eligible_leads else 0.0
    log.info(
        "arp_funnel_liveness: %s=%d заявок, годных лидов=%d, доля=%.3f (пол %.2f)",
        ARP_FUNNEL_VIEW, view_leads, eligible_leads, ratio, ARP_FUNNEL_RATIO_FLOOR,
    )
    if view_leads == 0:
        failures.append(f"empty_pbi_funnel:{ARP_FUNNEL_VIEW}")
    elif ratio < ARP_FUNNEL_RATIO_FLOOR:
        failures.append(f"pbi_funnel_ratio:{ARP_FUNNEL_VIEW}:{ratio:.3f}<{ARP_FUNNEL_RATIO_FLOOR}")

    if not _has_columns(client, "raw_yandex", {"domain"}):
        failures.append("direct_spend_guard_missing_raw_yandex_domain")
    else:
        spend_loss = _direct_spend_loss_rows(client)
        log.info("direct_spend_loss_slices=%d", len(spend_loss))
        for row in spend_loss[:5]:
            log.error(
                "direct_spend_loss month=%s login=%s domain=%s raw=%s direct=%s missing=%s",
                *row,
            )
        if spend_loss:
            failures.append(f"direct_spend_loss_slices={len(spend_loss)}")

        auto_pbi_loss = _auto_spend_pbi_loss_rows(client)
        log.info("bi_contract_auto_spend_raw_to_pbi_slices=%d", len(auto_pbi_loss))
        for row in auto_pbi_loss[:5]:
            log.error(
                "bi_contract_auto_spend_raw_to_pbi month=%s login=%s domain=%s raw=%s pbi=%s missing=%s",
                *row,
            )
        if auto_pbi_loss:
            failures.append(f"bi_contract_auto_spend_raw_to_pbi_slices={len(auto_pbi_loss)}")

    feed_spend_loss = _direct_feed_spend_loss_rows(client)
    log.info("bi_contract_direct_feed_spend_slices=%d", len(feed_spend_loss))
    for row in feed_spend_loss[:5]:
        log.error(
            "bi_contract_direct_feed_spend month=%s login=%s domain=%s raw=%s final=%s missing=%s",
            *row,
        )
    if feed_spend_loss:
        failures.append(f"bi_contract_direct_feed_spend_slices={len(feed_spend_loss)}")

    pbi_funnel_loss = _pbi_funnel_loss_rows(client)
    log.info("bi_contract_pbi_funnel_slices=%d", len(pbi_funnel_loss))
    for row in pbi_funnel_loss[:5]:
        log.error(
            "bi_contract_pbi_funnel month=%s attribution=%s cost=%s/%s obr=%s/%s z=%s/%s kval=%s/%s priezd=%s/%s sales=%s/%s",
            *row,
        )
    if pbi_funnel_loss:
        failures.append(f"bi_contract_pbi_funnel_slices={len(pbi_funnel_loss)}")

    closed_month_drift = _closed_month_drift_rows(client)
    log.info("bi_contract_closed_month_drift_slices=%d", len(closed_month_drift))
    for row in closed_month_drift[:5]:
        log.error(
            "bi_contract_closed_month_drift month=%s metric=%s previous=%s current=%s drift=%s",
            *row,
        )
    if closed_month_drift:
        failures.append(f"bi_contract_closed_month_drift_slices={len(closed_month_drift)}")

    auto_empty_dims = _auto_empty_dims_rows(client)
    log.info("bi_contract_auto_empty_dims_slices=%d", len(auto_empty_dims))
    for row in auto_empty_dims[:5]:
        log.error(
            "bi_contract_auto_empty_dims month=%s domain=%s specialist=%s city=%s salon=%s cost=%s obr=%s sales=%s",
            *row,
        )
    if auto_empty_dims:
        failures.append(f"bi_contract_auto_empty_dims_slices={len(auto_empty_dims)}")

    no_spec_sales = _sales_without_real_specialist_rows(client)
    log.info("sales_without_real_specialist_slices=%d", len(no_spec_sales))
    for row in no_spec_sales[:5]:
        log.error(
            "sales_without_real_specialist axis=%s source=%s specialist=%s domain=%s login=%s cost=%s sales=%s",
            *row,
        )
    if no_spec_sales:
        failures.append(f"sales_without_real_specialist_slices={len(no_spec_sales)}")

    pbi_blank_specialists = _pbi_blank_specialist_rows(client)
    log.info("bi_contract_pbi_blank_specialist_slices=%d", len(pbi_blank_specialists))
    for row in pbi_blank_specialists[:5]:
        log.error(
            "bi_contract_pbi_blank_specialist month=%s specialist=%s domain=%s account_site=%s cost=%s obr=%s sales=%s",
            *row,
        )
    if pbi_blank_specialists:
        failures.append(f"bi_contract_pbi_blank_specialist_slices={len(pbi_blank_specialists)}")

    if not no_star:
        for table in PBI_COMPAT_OBJECTS:
            engine = _engine(client, table)
            if engine is None:
                failures.append(f"missing_compat:{table}")
                continue
            rows = count_rows(client, f"ad_analytics.`{table}`")
            log.info("%s=%d engine=%s", table, rows, engine)
            if rows == 0:
                failures.append(f"empty_compat:{table}")

        for table in FULL_PHYSICAL_TABLES:
            engine = _engine(client, table)
            if engine is None:
                failures.append(f"missing_physical:{table}")
                continue
            if engine == "View":
                failures.append(f"physical_is_view:{table}")
            rows = count_rows(client, f"ad_analytics.`{table}`")
            log.info("%s=%d engine=%s", table, rows, engine)
            if rows == 0:
                failures.append(f"empty_physical:{table}")

        for table in COMPAT_VIEWS:
            engine = _engine(client, table)
            if engine is None:
                failures.append(f"missing_compat_view:{table}")
                continue
            rows = count_rows(client, f"ad_analytics.`{table}`")
            log.info("%s=%d engine=%s", table, rows, engine)
            if engine != "View":
                failures.append(f"compat_not_view:{table}:{engine}")
            if rows == 0:
                failures.append(f"empty_compat_view:{table}")

        for table in WIDE_COMPAT_VIEWS:
            engine = _engine(client, table)
            if engine is None:
                failures.append(f"missing_wide_compat:{table}")
                continue
            rows = count_rows(client, f"ad_analytics.`{table}`")
            log.info("%s=%d engine=%s", table, rows, engine)
            if engine != "View":
                failures.append(f"wide_compat_not_view:{table}:{engine}")
            if rows == 0:
                failures.append(f"empty_wide_compat:{table}")

        raw_present, raw_total, raw_missing = _kuderko_raw_coverage(client)
        raw_present_pre_cutoff = _kuderko_pre_cutoff_presence(client)
        golden_raw_incomplete = raw_present < raw_total
        # ЗАМЕЧАНИЕ 4 (director review): диагностический лог, отдельно от гейта выше — показывает,
        # сколько логинов реально видны ДО отсечки правила-1 (_KUDERKO_DATE), а не просто "есть
        # хоть какая-то строка". Если раскроется раскол present_any_day > present_pre_cutoff — это
        # знак, что часть "присутствующих" логинов покрыта строками ВНЕ golden-окна (пример-факт —
        # porg-riga5gvo, см. docstring _kuderko_pre_cutoff_presence и KNOWN_ISSUES.md #37).
        log.info(
            "kuderko_raw_coverage: present_any_day=%d present_pre_cutoff=%d total=%d (cutoff=%s)",
            raw_present, raw_present_pre_cutoff, raw_total, _KUDERKO_DATE,
        )
        if golden_raw_incomplete:
            log.warning(
                "KUDERKO_RAW_INCOMPLETE: raw_data.yandex_direct_report_rows содержит %d/%d "
                "логинов _KUDERKO_LOGINS (пусто %d). Сырьё под golden НЕПОЛНО: внешний загрузчик "
                "(вне этого репозитория, вне мониторинга raw_data.etl_runs) всё ещё доливает "
                "историю по недостающим аккаунтам батчами прямо по датам, которые golden считает "
                "замороженными (факт 2026-08-06: e-20078432 получил январские данные в 16:49 UTC, "
                "porg-kkhtgf2u — в 17:15 UTC, 44 261 строк / 1 986 682.13 ₽, без дублей по "
                "бизнес-ключу). Эталон Кудерко посчитан по ВСЕМ 67 логинам разом (средний расход "
                "на уже загруженный аккаунт ≈907 тыс ₽) — при неполном сырье расхождение по "
                "расходу/продажам ОЖИДАЕМО и НЕ является регрессией ETL или атрибуции, чинить "
                "corrections/spec_fallback НЕ НУЖНО (root-cause и цифры — KNOWN_ISSUES.md #37). "
                "golden ниже понижен до информационного статуса ТОЛЬКО на этот прогон; "
                "отсутствующие логины (первые 10 из %d): %s",
                raw_present,
                raw_total,
                len(raw_missing),
                len(raw_missing),
                ", ".join(raw_missing[:10]),
            )
        else:
            log.info("kuderko_raw_coverage=%d/%d — сырьё полное, golden в обычном режиме", raw_present, raw_total)

        if not _has_columns(client, "fact_big_analytics", GOLDEN_FACT_COLUMNS):
            failures.append("golden_missing_fact_columns")
        elif not _has_columns(client, "Dim_Salon", GOLDEN_SALON_COLUMNS):
            failures.append("golden_missing_salon_columns")
        elif not _has_columns(client, "pbi_big_analytics_full", PBI_RESTORED_TEXT_COLUMNS):
            failures.append("missing_pbi_restored_text_columns")
        else:
            source_sql = ", ".join(f"'{source}'" for source in GOLDEN_SOURCES)
            rashod, prodazhi = client.query(
                _golden_kuderko_sql(source_sql),
                parameters={"specialist": GOLDEN_SPECIALIST},
            ).result_rows[0]
            rashod = Decimal(str(rashod or "0"))
            prodazhi = int(prodazhi or 0)
            cost_delta = rashod - GOLDEN_COST
            log.info(
                "golden_kuderko cost=%s delta=%+s sales=%d floor=%d",
                rashod,
                cost_delta,
                prodazhi,
                GOLDEN_SALES_FLOOR,
            )
            if abs(cost_delta) > GOLDEN_COST_TOL:
                if golden_raw_incomplete:
                    log.warning(
                        "golden_cost_delta=%s ИГНОРИРУЕТСЯ вердиктом: сырьё под golden неполно "
                        "(%d/%d логинов, см. KUDERKO_RAW_INCOMPLETE выше) — не регрессия ETL",
                        cost_delta, raw_present, raw_total,
                    )
                else:
                    failures.append(f"golden_cost_delta={cost_delta}")
            if prodazhi < GOLDEN_SALES_FLOOR:
                if golden_raw_incomplete:
                    log.warning(
                        "golden_sales=%d ИГНОРИРУЕТСЯ вердиктом: сырьё под golden неполно "
                        "(%d/%d логинов, см. KUDERKO_RAW_INCOMPLETE выше) — не регрессия ETL",
                        prodazhi, raw_present, raw_total,
                    )
                else:
                    failures.append(f"golden_sales={prodazhi}")

    checks = {
        "raw_yandex_cost_zero": "SELECT if(sum(total_cost) = 0, 1, 0) FROM ad_analytics.raw_yandex",
        "full_before_2026": "SELECT count() FROM ad_analytics.big_analytics_full WHERE `Date` < toDate('2026-01-01')",
        "full_null_source": "SELECT count() FROM ad_analytics.big_analytics_full WHERE `источник` IS NULL OR `источник` = ''",
        # LAST_DAY_COMPLETENESS_2026-08-24: свежий день в витрине не должен быть заметно
        # меньше соседних. Ловит прогон, стартовавший внутрь окна загрузки
        # `raw_data.yandex_direct_report_rows` (внешний загрузчик пишет её 04:30–06:42 МСК):
        # крон стоял в 05:00 МСК и брал ~треть вчерашнего расхода — 2 399 534 руб. за
        # 2026-08-23 против медианы 8 011 567 по семи предыдущим дням, ratio 0.30.
        # Порог 0.6 откалиброван бэктестом по 39 дням (16.07–23.08): здоровые ratio
        # 0.784–1.236 (минимум — суббота 01.08), единственный < 0.6 — сломанный 23.08.
        # Пустое окно сравнения (свежая БД) даёт median 0 → проверка молчит, не падает.
        "full_last_day_incomplete": """
            SELECT if(
                (SELECT toFloat64(sum(total_cost))
                 FROM ad_analytics.big_analytics_full
                 WHERE `Date` = today() - 1)
                <
                (SELECT median(c) * 0.6 FROM (
                    SELECT toFloat64(sum(total_cost)) AS c
                    FROM ad_analytics.big_analytics_full
                    WHERE `Date` >= today() - 8 AND `Date` <= today() - 2
                    GROUP BY `Date`
                )),
                1, 0
            )
        """,
        "full_funnel_korr_lt_kval": "SELECT count() FROM ad_analytics.big_analytics_full WHERE korr < kval",
        "full_funnel_kval_lt_priezd": "SELECT count() FROM ad_analytics.big_analytics_full WHERE kval < priezd",
        "full_funnel_priezd_lt_prodazhi": "SELECT count() FROM ad_analytics.big_analytics_full WHERE priezd < prodazhi",
        # REASON_FUNNEL_NESTING_2026-08-25 (director review): dohod_do_kredita/dobro
        # (claim-axis, big_analytics_full) are category-supersets of korr/priezd by
        # construction in step3_build_sources/step3.py::_metric_expr — credit_side's
        # categories (credit, approved, sale) are a subset of priezd's (visit, sale,
        # credit, approved), and approved_side's (approved, sale) a subset of
        # credit_side's, so `dobro <= dohod_do_kredita <= priezd <= korr` per row is a
        # structural guarantee, not a reference number — no golden fixture needed, any
        # violation is a code regression in the category sets.
        "full_funnel_dobro_gt_dohod_do_kredita": (
            "SELECT count() FROM ad_analytics.big_analytics_full WHERE dobro > dohod_do_kredita"
        ),
        "full_funnel_dohod_do_kredita_gt_priezd": (
            "SELECT count() FROM ad_analytics.big_analytics_full WHERE dohod_do_kredita > priezd"
        ),
        "full_funnel_prodazhi_gt_dobro": (
            "SELECT count() FROM ad_analytics.big_analytics_full WHERE prodazhi > dobro"
        ),
        "region_funnel_dohod_do_kredita_gt_priezd": (
            "SELECT count() FROM ad_analytics.fact_region_zayavki WHERE dohod_do_kredita > priezd"
        ),
        "region_funnel_dobro_gt_dohod_do_kredita": (
            "SELECT count() FROM ad_analytics.fact_region_zayavki WHERE dobro > dohod_do_kredita"
        ),
        "region_funnel_prodazhi_gt_dobro": (
            "SELECT count() FROM ad_analytics.fact_region_zayavki WHERE prodazhi > dobro"
        ),
        "criterion_funnel_dohod_do_kredita_gt_priezd": (
            "SELECT count() FROM ad_analytics.fact_criterion_zayavki WHERE dohod_do_kredita > priezd"
        ),
        "criterion_funnel_dobro_gt_dohod_do_kredita": (
            "SELECT count() FROM ad_analytics.fact_criterion_zayavki WHERE dobro > dohod_do_kredita"
        ),
        "criterion_funnel_prodazhi_gt_dobro": (
            "SELECT count() FROM ad_analytics.fact_criterion_zayavki WHERE prodazhi > dobro"
        ),
        "feed_funnel_dohod_do_kredita_gt_priezd": (
            "SELECT count() FROM ad_analytics.bi_fact_direct_feed_funnel WHERE dohod_do_kredita > priezd"
        ),
        "feed_funnel_dobro_gt_dohod_do_kredita": (
            "SELECT count() FROM ad_analytics.bi_fact_direct_feed_funnel WHERE dobro > dohod_do_kredita"
        ),
        "feed_funnel_prodazhi_gt_dobro": (
            "SELECT count() FROM ad_analytics.bi_fact_direct_feed_funnel WHERE prodazhi > dobro"
        ),
        "ml_korrektirovki_funnel_dohod_do_kredita_gt_priezd": (
            "SELECT count() FROM ad_analytics.fact_ml_korrektirovki WHERE dohod_do_kredita > priezd"
        ),
        "ml_korrektirovki_funnel_dobro_gt_dohod_do_kredita": (
            "SELECT count() FROM ad_analytics.fact_ml_korrektirovki WHERE dobro > dohod_do_kredita"
        ),
        "ml_korrektirovki_funnel_prodazhi_gt_dobro": (
            "SELECT count() FROM ad_analytics.fact_ml_korrektirovki WHERE prodazhi > dobro"
        ),
        "unified_count_mismatch": """
            SELECT if(
                (SELECT count() FROM ad_analytics.big_analytics_unified)
                !=
                (SELECT count() FROM ad_analytics.big_analytics_full)
                + (SELECT count() FROM ad_analytics.big_analytics_full_arrival),
                1, 0
            )
        """,
    }
    if not no_star:
        checks["fact_unified_count_mismatch"] = """
            SELECT if(
                (SELECT count() FROM ad_analytics.fact_big_analytics)
                !=
                (SELECT count() FROM ad_analytics.big_analytics_unified),
                1, 0
            )
        """

    for name, sql in checks.items():
        value = int(_scalar(client, sql))
        log.info("%s=%d", name, value)
        if value:
            failures.append(f"{name}={value}")

    if failures:
        log.error("FAIL: %s", "; ".join(failures))
        return 1
    log.info("PASS")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--no-star", action="store_true")
    parser.add_argument("--tg", action="store_true")
    args = parser.parse_args()
    try:
        return run(full=args.full, no_star=args.no_star, tg=args.tg)
    except Exception:
        log.exception("CRASH")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
