"""Build compatibility tables expected by the existing Power BI semantic model."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_settings import DATE_FROM
from config.ch_utils import (
    SAFE_QUERY_SETTINGS,
    apply_storage_codecs,
    count_rows,
    day_ranges,
    range_batches,
    q,
    swap_shadow,
    table_exists,
)
from criterion_spend.cleaning import CRITERION_CLEAN
from spend.build_direct_spend_staging import STAGING_TABLE
from step3_build_sources.step3 import _metric_expr

log = logging.getLogger("build_pbi_compat")

def _site_key_expr(alias: str = "f") -> str:
    return f"cityHash64(lowerUTF8(trim(BOTH ' ' FROM ifNull({alias}.domain, ''))))"


def _city_tier_key_expr(city_expr: str, date_expr: str) -> str:
    city = f"if(ifNull({city_expr}, '') = '', '(пусто)', ifNull({city_expr}, '(пусто)'))"
    month = f"formatDateTime(toStartOfMonth(ifNull({date_expr}, toDate('1900-01-01'))), '%Y-%m')"
    return f"concat({city}, '|', {month})"


def _pbi_int64_key(expr: str) -> str:
    return f"reinterpretAsInt64({expr})"


PBI_SOURCE_OBJECTS = [
    "Dim_Account",
    "Dim_AdGroup",
    "Dim_AdFormat",
    "Dim_AdText",
    "Dim_AdNetworkType",
    "Dim_Adjustment",
    "Dim_Campaign",
    "Dim_City_Tier",
    "Dim_Date",
    "Dim_Device",
    "Dim_Location",
    "Dim_ManagerLogin",
    "Dim_PlacementFeed",
    "Dim_CRMStatus",
    "Dim_Salon",
    "Dim_Site",
    "Dim_Source",
    "Dim_VkAdGroup",
    "Dim_VkAdPlan",
    "Dim_VkBanner",
    "pbi_big_analytics_full",
    "check_utm_fuck_direct",
    "dim_criterion",
    "yandex_direct_history",
    "fact_adformat_spend",
    "fact_criterion_spend",
    "fact_criterion_spend_star",
    "fact_criterion_zayavki",
    "fact_direct_feed_funnel",
    "fact_direct_feed_funnel_star",
    "fact_ml_korrektirovki",
    "fact_region_spend",
    "fact_region_spend_star",
    "fact_region_zayavki",
    "fact_vk_ads",
    "pixel_score",
    "v_yandex_direct_minus_delta",
    "yandex_direct_404_errors",
    "yandex_direct_cookie_analytics_website_pages",
    "yandex_direct_korrektirovki",
    "yandex_direct_minus_snapshot",
    "yandex_direct_ads_texts",
    "yandex_direct_type_placement_report_master",
    "direct_autorules_posevy_placement_links",
    # PLACEMENT_LINKS_BI_2026-08-17. Модель PBI читала ручную копию БА5
    # `raw_new_tp_placement_links` (снимок от 16.08, сам не обновляется) — из-за этого фикс
    # двойной кодировки площадок в отчёт не доезжал. Отчёт переведён на этот bi-слой над живой
    # таблицей шага 139; правило «PBI ходит только через bi_*» — DB_AD_ANALYTICS.md §1.3.
    "yandex_direct_tp_placement_links",
    # ARP_LIVE_2026-08-23: PBI-таблицы `analytics_report_placement`(+`_links`) и
    # `yandex_direct_search_query_report_master` уходят с замороженных снимков БА5
    # `raw_new_arp_fact` / `raw_new_search_query_report_master_pbi` на живые `bi_*`.
    "analytics_report_placement",
    "yandex_direct_search_query_report_master",
]

LEGACY_BI_VIEWS = [
    "bi_big_analytics_full_arrival",
]


def _replace_view(client, name: str, select_sql: str) -> None:
    client.command(f"DROP VIEW IF EXISTS ad_analytics.{q(name)} SYNC")
    client.command(f"DROP TABLE IF EXISTS ad_analytics.{q(name)} SYNC")
    client.command(f"CREATE VIEW ad_analytics.{q(name)} AS {select_sql}")


def drop_bi_views(client) -> None:
    for table in PBI_SOURCE_OBJECTS:
        client.command(f"DROP TABLE IF EXISTS ad_analytics.{q(f'bi_{table}')} SYNC")
    for view_name in LEGACY_BI_VIEWS:
        client.command(f"DROP TABLE IF EXISTS ad_analytics.{q(view_name)} SYNC")


def _pbi_full_sql(where_sql: str = "") -> str:
    return f"""
        SELECT
            f.`Date` AS `Date`,
            f.`CampaignId` AS `CampaignId`,
            f.ad_network_type_key,
            f.device_key,
            f.source_key,
            toFloat64(f.total_cost) AS total_cost,
            toFloat64(f.kol_vo_zayavok) AS `Обращения`,
            toFloat64(f.korr) AS korr,
            toFloat64(f.kval) AS kval,
            toFloat64(f.priezd) AS priezd,
            toFloat64(f.prodazhi) AS prodazhi,
            toInt64(round(f.nekorr)) AS nekorr,
            toInt64(round(f.ne_otvechaet)) AS ne_otvechaet,
            toInt64(round(f.filtr)) AS filtr,
            toInt64(round(f.nedozvon)) AS nedozvon,
            f.`RlAdjustmentId` AS `RlAdjustmentId`,
            f.fid,
            -- PBI_CLICKS_IMPRESSIONS_FIX_2026-08-06: v5's pbi_big_analytics_full projects the real
            -- f."Clicks" column as-is (COLUMNS_big_analytics_full.md: Clicks=клики, Impressions=показы
            -- — two distinct fields, never swapped). The previous alias here substituted Impressions
            -- for Clicks, so SUM(Clicks) in Power BI actually summed показы (643 287 417 instead of
            -- the real 30 026 838). Fixed to match v5: Clicks stays Clicks, Impressions is its own column.
            toFloat64(f.`Clicks`) AS `Clicks`,
            toFloat64(f.`Impressions`) AS `Impressions`,
            f.`План заявки`,
            f.`План приезда`,
            concat(ifNull(da.account_login, ''), '|', ifNull(f.domain, '')) AS `аккаунт|сайт`,
            f.domain AS `домен`,
            toInt64(round(f.priedet)) AS priedet,
            f.dohod_do_kredita,
            f.dobro,
            f.`атрибуция`,
            f.`AdGroupId` AS `AdGroupId`,
            f.tp,
            multiIf(
                f.source_key = 'пиксель', 'Пиксель',
                f.source_key = 'pixel', 'Пиксель',
                dsl.`направление`
            ) AS `направление`,
            multiIf(
                dcs.`тип_заявки` IS NULL OR dcs.`тип_заявки` = '' OR dcs.`тип_заявки` IN ('Заявка', 'Из базы', 'Пиксель'),
                'Заявки',
                dcs.`тип_заявки`
            ) AS `тип_заявки`,
            f.`специалист` AS `специалист`,
            dsl.`салон` AS `салон`,
            dsl.`город` AS `город`,
            dsl.`регион` AS `регион`,
            dsl.`тип_сайта` AS `тип_сайта`,
            dsl.`шаблон` AS `шаблон`,
            dcs.`статус` AS `статус`,
            dsl.`проджект` AS `проджект`,
            dsl.`менеджер` AS `менеджер`,
            dsl.`id_салона` AS `id_салона`,
            if(
                ifNull(dcs.`Название crm`, '') IN ('', 'Не указана'),
                if(ifNull(dsite.`Название crm`, '') = '', 'Не указана', dsite.`Название crm`),
                dcs.`Название crm`
            ) AS `Название crm`,
            {_city_tier_key_expr("dsl.`город`", "f.`Date`")} AS city_tier_key,
            toInt64(f.manager_login_key % 9223372036854775807) AS manager_login_key
        FROM ad_analytics.fact_big_analytics f
        LEFT JOIN ad_analytics.Dim_Account da ON da.account_key = f.account_key
        LEFT JOIN ad_analytics.Dim_CRMStatus dcs ON dcs.crm_status_key = f.crm_status_key
        LEFT JOIN ad_analytics.Dim_Salon dsl ON dsl.salon_key = f.salon_key
        LEFT JOIN ad_analytics.Dim_Site dsite ON dsite.site_key = f.site_key
        {where_sql}
    """


def build_pbi_full(client) -> int:
    _replace_view(client, "pbi_import_big_analytics_full", _pbi_full_sql())
    _replace_view(
        client,
        "pbi_big_analytics_full",
        "SELECT * FROM ad_analytics.pbi_import_big_analytics_full"
    )
    return count_rows(client, "ad_analytics.pbi_big_analytics_full")


def build_dim_city_tier(client) -> int:
    shadow = "ad_analytics.Dim_City_Tier_new"
    client.command(f"DROP TABLE IF EXISTS {shadow} SYNC", settings=SAFE_QUERY_SETTINGS)
    source_exists = table_exists(client, "ad_analytics", "gsheet_city_tier")
    if source_exists:
        tier_join_sql = """
            WITH
            fact_grain AS
            (
                SELECT DISTINCT
                    city_tier_key,
                    if(ifNull(`город`, '') = '', '', `город`) AS `город`,
                    toStartOfMonth(ifNull(`Date`, toDate('1900-01-01'))) AS month
                FROM ad_analytics.pbi_big_analytics_full
            ),
            latest_tier AS
            (
                SELECT
                    gorod,
                    argMax(tier, month) AS `тир_текущий`
                FROM ad_analytics.gsheet_city_tier
                GROUP BY gorod
            )
            SELECT
                fg.city_tier_key,
                fg.`город`,
                COALESCE(nullIf(mt.tier, ''), 'Без тира') AS `тир_месяца`,
                ifNull(mt.is_backfill, false) AS `тир_месяца_backfill`,
                COALESCE(nullIf(lt.`тир_текущий`, ''), 'Без тира') AS `тир_текущий`
            FROM fact_grain fg
            LEFT JOIN ad_analytics.gsheet_city_tier mt
                ON mt.gorod = fg.`город` AND mt.month = fg.month
            LEFT JOIN latest_tier lt
                ON lt.gorod = fg.`город`
        """
    else:
        tier_join_sql = """
            SELECT DISTINCT
                city_tier_key,
                if(ifNull(`город`, '') = '', '', `город`) AS `город`,
                'Без тира' AS `тир_месяца`,
                false AS `тир_месяца_backfill`,
                'Без тира' AS `тир_текущий`
            FROM ad_analytics.pbi_big_analytics_full
        """
    client.command(
        f"""
        CREATE TABLE {shadow}
        ENGINE = MergeTree
        ORDER BY city_tier_key
        AS
        {tier_join_sql}
        """,
        settings=SAFE_QUERY_SETTINGS,
    )
    swap_shadow(client, "ad_analytics.Dim_City_Tier", shadow)
    return count_rows(client, "ad_analytics.Dim_City_Tier")


def _pixel_score_sql(where_sql: str = "") -> str:
    return f"""
        SELECT
            toStartOfMonth(`Date`) AS month,
            ps.domain AS domain,
            ds.`салон` AS `салон`,
            ps.`источник`,
            ps.`CampaignId` AS CampaignId,
            dc.CampaignName AS CampaignName,
            toFloat64(kol_vo_zayavok) AS kol_vo_zayavok,
            toFloat64(korr) AS korr,
            toFloat64(kval) AS kval,
            toFloat64(priezd) AS priezd,
            toFloat64(prodazhi) AS prodazhi,
            ds.`направление` AS `направление`,
            toFloat64(1) AS cpl_score,
            toFloat64(kol_vo_zayavok) AS `pixel_kol_vo_домена`,
            toFloat64(kol_vo_zayavok) AS `pixel_kol_vo_кампании`,
            CAST(NULL, 'Nullable(Float64)') AS `cpl_avg_квал`,
            CAST(NULL, 'Nullable(Float64)') AS `cpl_avg_визит`,
            CAST(NULL, 'Nullable(Float64)') AS `cpl_avg_продажа`,
            CAST(NULL, 'Nullable(Float64)') AS `cpl_кам_квал`,
            CAST(NULL, 'Nullable(Float64)') AS `cpl_кам_визит`,
            CAST(NULL, 'Nullable(Float64)') AS `cpl_кам_продажа`,
            toFloat64(1) AS `score_квал`,
            toFloat64(1) AS `score_визит`,
            toFloat64(1) AS `score_продажа`,
            toFloat64(1) AS `w_квал`,
            toFloat64(1) AS `w_визит`,
            toFloat64(1) AS `w_продажа`,
            'данные' AS `status_квал`,
            'данные' AS `status_визит`,
            'данные' AS `status_продажа`,
            toFloat64(total_cost) AS `расход`,
            toFloat64(100) AS weight,
            toFloat64(kval) AS `pixel_квал_домена`,
            toFloat64(kval) AS `attr_pixel_квал_кампании`,
            toFloat64(priezd) AS `pixel_приезд_домена`,
            toFloat64(priezd) AS `attr_pixel_приезд_кампании`,
            toFloat64(prodazhi) AS `pixel_продажи_домена`,
            toFloat64(prodazhi) AS `attr_pixel_продажи_кампании`
        FROM ad_analytics.big_analytics_pixel_score ps
        LEFT JOIN ad_analytics.Dim_Site ds ON ds.site_key = {_site_key_expr("ps")}
        LEFT JOIN ad_analytics.Dim_Campaign dc ON dc.CampaignId = ps.CampaignId
        {where_sql}
    """


def build_pixel_score(client) -> int:
    shadow = "ad_analytics.pixel_score_new"
    client.command(f"DROP TABLE IF EXISTS {shadow} SYNC")
    client.command(
        f"""
        CREATE TABLE {shadow}
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(month)
        ORDER BY (month, ifNull(domain, ''), ifNull(`салон`, ''))
        AS
        {_pixel_score_sql("WHERE 0")}
        """,
        settings=SAFE_QUERY_SETTINGS,
    )
    ranges = range_batches(DATE_FROM, days=7)
    for idx, (lo, hi) in enumerate(ranges, start=1):
        client.command(
            f"""
            INSERT INTO {shadow}
            {_pixel_score_sql(f"WHERE `Date` >= toDate('{lo}') AND `Date` < toDate('{hi}')")}
            """,
            settings=SAFE_QUERY_SETTINGS,
        )
        log.info("  pixel_score weekly batch %d/%d: %s -> %s", idx, len(ranges), lo, hi)
    swap_shadow(client, "ad_analytics.pixel_score", shadow)
    return count_rows(client, "ad_analytics.pixel_score")


def build_dim_placement_feed(client) -> int:
    stage = "ad_analytics.Dim_PlacementFeed_stage_new"
    shadow = "ad_analytics.Dim_PlacementFeed_new"
    use_staging = table_exists(client, "ad_analytics", "direct_spend_staging")
    source_table = STAGING_TABLE if use_staging else "raw_data.yandex_direct_report_rows"
    date_expr = "date" if use_staging else "toDate(day)"
    placement_expr = "placement" if use_staging else "trim(BOTH ' ' FROM ifNull(placement, ''))"
    client.command(f"DROP TABLE IF EXISTS {stage} SYNC")
    client.command(f"DROP TABLE IF EXISTS {shadow} SYNC")
    client.command(
        f"""
        CREATE TABLE {stage}
        ENGINE = MergeTree
        ORDER BY placement_feed_key
        AS
        SELECT
            lowerUTF8(trim(BOTH ' ' FROM ifNull({placement_expr}, ''))) AS placement_feed_key,
            trim(BOTH ' ' FROM ifNull({placement_expr}, '')) AS placement,
            trim(BOTH ' ' FROM ifNull({placement_expr}, '')) AS feed_key,
            trim(BOTH ' ' FROM ifNull({placement_expr}, '')) AS feed_url_key,
            ad_network_type,
            ad_network_type AS AdNetworkType,
            tuple(count(), lengthUTF8(placement)) AS sort_weight
            FROM {source_table}
            WHERE 0
            GROUP BY placement_feed_key, placement, ad_network_type
        """,
        settings=SAFE_QUERY_SETTINGS,
    )
    ranges = range_batches(DATE_FROM, days=7)
    for idx, (lo, hi) in enumerate(ranges, start=1):
        client.command(
            f"""
            INSERT INTO {stage}
            SELECT
                lowerUTF8(trim(BOTH ' ' FROM ifNull({placement_expr}, ''))) AS placement_feed_key,
                trim(BOTH ' ' FROM ifNull({placement_expr}, '')) AS placement,
                trim(BOTH ' ' FROM ifNull({placement_expr}, '')) AS feed_key,
                trim(BOTH ' ' FROM ifNull({placement_expr}, '')) AS feed_url_key,
                ad_network_type,
                ad_network_type AS AdNetworkType,
                tuple(count(), lengthUTF8(placement)) AS sort_weight
                FROM {source_table}
                WHERE {date_expr} >= toDate('{lo}') AND {date_expr} < toDate('{hi}')
                  AND notEmpty(lowerUTF8(trim(BOTH ' ' FROM ifNull({placement_expr}, ''))))
                GROUP BY placement_feed_key, placement, ad_network_type
            """,
            settings=SAFE_QUERY_SETTINGS,
        )
        log.info("  Dim_PlacementFeed weekly stage batch %d/%d: %s -> %s", idx, len(ranges), lo, hi)
    client.command(
        f"""
        CREATE TABLE {shadow}
        ENGINE = MergeTree
        ORDER BY placement_feed_key
        AS
        SELECT
            placement_feed_key,
            argMax(placement, sort_weight) AS placement,
            CAST(NULL, 'Nullable(String)') AS feed_name,
            CAST(NULL, 'Nullable(String)') AS feed_url,
            argMax(feed_key, sort_weight) AS feed_key,
            argMax(feed_url_key, sort_weight) AS feed_url_key,
            argMax(ad_network_type, sort_weight) AS ad_network_type,
            argMax(AdNetworkType, sort_weight) AS AdNetworkType
        FROM {stage}
        GROUP BY placement_feed_key
        """,
        settings=SAFE_QUERY_SETTINGS,
    )
    swap_shadow(client, "ad_analytics.Dim_PlacementFeed", shadow)
    client.command(f"DROP TABLE IF EXISTS {stage} SYNC")
    return count_rows(client, "ad_analytics.Dim_PlacementFeed")


def _dim_placement_feed_pbi_sql() -> str:
    return """
        SELECT
            toUInt32(row_number() OVER (ORDER BY placement_feed_key)) AS placement_feed_id,
            placement_feed_key,
            cityHash64(placement_feed_key) AS placement_feed_key_hash,
            placement,
            feed_name,
            feed_url,
            feed_key,
            feed_url_key,
            ad_network_type,
            AdNetworkType
        FROM ad_analytics.Dim_PlacementFeed
    """


def _feed_funnel_pbi_sql(where_sql: str = "") -> str:
    return f"""
        SELECT
            f.date,
            f.placement_feed_key AS placement_feed_key,
            f.campaign_id,
            f.ad_group_id AS adgroup_id,
            coalesce(nullIf(f.domain, ''), ds.domain) AS domain,
            toFloat64(f.cost) AS total_cost,
            toFloat64(f.clicks) AS clicks,
            toFloat64(f.impressions) AS impressions,
            toInt64(round(f.all_forms)) AS kol_vo_zayavok,
            toInt64(round(f.crm_order_created)) AS korr,
            toInt64(0) AS nekorr,
            toInt64(round(f.crm_order_paid)) AS kval,
            toInt64(round(f.crm_order_paid)) AS priezd,
            toInt64(round(f.crm_order_paid)) AS prodazhi,
            toInt64(round(f.crm_order_paid)) AS dobro,
            toInt64(round(f.crm_order_paid)) AS dohod_do_kredita,
            toInt64(0) AS filtr,
            toInt64(round(f.all_forms)) AS attributed_leads,
            toInt64(0) AS ne_otvechaet,
            toInt64(0) AS nedozvon,
            toInt64(0) AS priedet,
            toFloat64(f.all_forms) AS goal_all_forms,
            toFloat64(f.crm_order_created) AS goal_crm_order_created,
            toFloat64(f.crm_order_paid) AS goal_crm_order_paid,
            toFloat64(0) AS goal_crm_order_canceled,
            toFloat64(0) AS goal_crm_spam_order,
            toUInt8(0) AS is_tp67,
            'контекст' AS source_key,
            now() AS generated_at
        FROM ad_analytics.fact_direct_feed_funnel f
        LEFT JOIN ad_analytics.Dim_Site ds ON ds.site_key = f.site_key
        {where_sql}
    """


def _feed_funnel_star_sql(where_sql: str = "") -> str:
    return f"""
        WITH placement_feed_ids AS
        (
            SELECT
                cityHash64(placement_feed_key) AS placement_feed_key_hash,
                toUInt32(row_number() OVER (ORDER BY placement_feed_key)) AS placement_feed_id
            FROM ad_analytics.Dim_PlacementFeed
        )
        SELECT
            f.date,
            f.campaign_id,
            f.ad_group_id AS adgroup_id,
            p.placement_feed_id,
            {_pbi_int64_key("f.site_key")} AS site_key,
            toFloat64(f.cost) AS cost,
            toFloat64(f.clicks) AS clicks,
            toFloat64(f.impressions) AS impressions,
            toFloat64(f.all_forms) AS all_forms,
            toFloat64(f.crm_order_created) AS crm_order_created,
            toFloat64(f.crm_order_paid) AS crm_order_paid
        FROM ad_analytics.fact_direct_feed_funnel_light f
        LEFT JOIN placement_feed_ids p ON p.placement_feed_key_hash = f.placement_feed_key_hash
        {where_sql}
    """


def _feed_funnel_star_pbi_sql() -> str:
    return f"""
        SELECT
            date,
            campaign_id,
            adgroup_id,
            placement_feed_id,
            {_pbi_int64_key("site_key")} AS site_key,
            cost,
            clicks,
            impressions,
            all_forms,
            crm_order_created,
            crm_order_paid
        FROM ad_analytics.pbi_import_fact_direct_feed_funnel_star
    """


def build_pbi_import_direct_feed_funnel(client) -> int:
    shadow = "ad_analytics.pbi_import_fact_direct_feed_funnel_new"
    client.command(f"DROP TABLE IF EXISTS {shadow} SYNC")
    client.command(
        f"""
        CREATE TABLE {shadow}
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(date)
        ORDER BY (date, campaign_id, adgroup_id, placement_feed_key, ifNull(domain, ''))
        AS
        {_feed_funnel_pbi_sql("WHERE 0")}
        """,
        settings=SAFE_QUERY_SETTINGS,
    )
    # PBI_WEIGHT_2026-08-14 (OPTIMIZATION_PLAN.md, фаза 3): самая тяжёлая PBI-проекция.
    # Схема выводится из SELECT, поэтому кодеки вешаем на пустую shadow: замерено ZSTD(3) на
    # Float64-метриках −15.7%, Gorilla оказался хуже отсутствия кодека.
    apply_storage_codecs(client, shadow)
    ranges = day_ranges(DATE_FROM)
    for idx, (lo, hi) in enumerate(ranges, start=1):
        client.command(
            f"""
            INSERT INTO {shadow}
            {_feed_funnel_pbi_sql(f"WHERE f.date >= toDate('{lo}') AND f.date < toDate('{hi}')")}
            """,
            settings=SAFE_QUERY_SETTINGS,
        )
        log.info("  pbi_import_fact_direct_feed_funnel batch %d/%d: %s -> %s", idx, len(ranges), lo, hi)
    swap_shadow(client, "ad_analytics.pbi_import_fact_direct_feed_funnel", shadow)
    return count_rows(client, "ad_analytics.pbi_import_fact_direct_feed_funnel")


def build_pbi_import_direct_feed_funnel_star(client) -> int:
    shadow = "ad_analytics.pbi_import_fact_direct_feed_funnel_star_new"
    client.command(f"DROP TABLE IF EXISTS {shadow} SYNC")
    client.command(
        f"""
        CREATE TABLE {shadow}
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(date)
        ORDER BY (date, campaign_id, adgroup_id, placement_feed_id, site_key)
        AS
        {_feed_funnel_star_sql("WHERE 0")}
        """,
        settings=SAFE_QUERY_SETTINGS,
    )
    apply_storage_codecs(client, shadow)
    ranges = day_ranges(DATE_FROM)
    for idx, (lo, hi) in enumerate(ranges, start=1):
        client.command(
            f"""
            INSERT INTO {shadow}
            {_feed_funnel_star_sql(f"WHERE f.date >= toDate('{lo}') AND f.date < toDate('{hi}')")}
            """,
            settings=SAFE_QUERY_SETTINGS,
        )
        log.info("  pbi_import_fact_direct_feed_funnel_star batch %d/%d: %s -> %s", idx, len(ranges), lo, hi)
    swap_shadow(client, "ad_analytics.pbi_import_fact_direct_feed_funnel_star", shadow)
    return count_rows(client, "ad_analytics.pbi_import_fact_direct_feed_funnel_star")


def _region_spend_pbi_sql(where_sql: str = "") -> str:
    return f"""
        SELECT
            f.date,
            f.campaign_id,
            f.ad_group_id,
            f.ad_network_type_key,
            f.id_location AS id_location,
            f.distance_km,
            f.distance_km_agreg,
            toFloat64(f.cost) AS cost,
            toFloat64(f.clicks) AS clicks,
            toFloat64(f.impressions) AS impressions,
            toInt64(round(f.all_forms)) AS `Все формы`,
            toInt64(round(f.crm_order_created)) AS `CRM: Заказ создан`,
            toInt64(round(f.crm_order_paid)) AS `CRM: Заказ оплачен`,
            toInt64(round(f.crm_spam_order)) AS `CRM: Спам заказ`,
            toInt64(round(f.crm_order_canceled)) AS `CRM: Заказ отменен`,
            f.`специалист` AS `специалист`,
            ds.domain AS domain,
            now() AS updated_at
        FROM ad_analytics.fact_region_spend f
        LEFT JOIN ad_analytics.Dim_Site ds ON ds.site_key = f.site_key
        {where_sql}
    """


def _region_spend_star_pbi_sql(where_sql: str = "") -> str:
    # GEO_LOCATION_JOIN_2026-08-24: distance_km/distance_km_agreg — физические колонки факта
    # (JOIN к справочнику ad_analytics.gsheet_yandex_direct_id_location сделан один раз при сборке
    # fact_region_spend в region_spend/build_region_spend.py, а не здесь) — звёздные *_star вьюхи
    # обязаны отдавать только ключи и метрики без JOIN (см.
    # test_region_and_criterion_star_views_keep_only_keys_and_metrics).
    return f"""
        SELECT
            f.date,
            f.campaign_id,
            f.ad_group_id,
            f.ad_network_type_key,
            f.id_location,
            {_pbi_int64_key("f.site_key")} AS site_key,
            f.distance_km,
            f.distance_km_agreg,
            toFloat64(f.cost) AS cost,
            toFloat64(f.clicks) AS clicks,
            toFloat64(f.impressions) AS impressions,
            toFloat64(f.all_forms) AS all_forms,
            toFloat64(f.crm_order_created) AS crm_order_created,
            toFloat64(f.crm_order_paid) AS crm_order_paid,
            toFloat64(f.crm_spam_order) AS crm_spam_order,
            toFloat64(f.crm_order_canceled) AS crm_order_canceled,
            f.`специалист` AS `специалист`
        FROM ad_analytics.fact_region_spend f
        {where_sql}
    """


def _adformat_spend_pbi_sql(where_sql: str = "") -> str:
    return f"""
        SELECT
            f.date,
            f.campaign_id,
            f.ad_group_id,
            f.ad_network_type_key,
            lowerUTF8(trim(BOTH ' ' FROM ifNull(f.ad_format, ''))) AS ad_format_key,
            toFloat64(f.cost) AS cost,
            toFloat64(f.clicks) AS clicks,
            toFloat64(f.impressions) AS impressions,
            toInt64(round(f.all_forms)) AS `Все формы`,
            toInt64(round(f.crm_order_created)) AS `CRM: Заказ создан`,
            toInt64(0) AS `CRM: Заказ оплачен`,
            toInt64(0) AS `CRM: Спам заказ`,
            toInt64(0) AS `CRM: Заказ отменен`,
            f.`специалист` AS `специалист`,
            ds.domain AS domain,
            now() AS updated_at
        FROM ad_analytics.fact_adformat_spend f
        LEFT JOIN ad_analytics.Dim_Site ds ON ds.site_key = f.site_key
        {where_sql}
    """


def _criterion_spend_pbi_sql(where_sql: str = "") -> str:
    return f"""
        SELECT
            f.date,
            f.campaign_id,
            f.ad_group_id,
            f.ad_network_type_key,
            f.criterion_id,
            ifNull(dcr.criterion, '') AS criterion,
            toFloat64(f.cost) AS cost,
            toFloat64(f.clicks) AS clicks,
            toFloat64(f.impressions) AS impressions,
            toInt64(round(f.all_forms)) AS `Все формы`,
            toInt64(round(f.crm_order_created)) AS `CRM: Заказ создан`,
            toInt64(round(f.crm_order_paid)) AS `CRM: Заказ оплачен`,
            toInt64(round(f.crm_spam_order)) AS `CRM: Спам заказ`,
            toInt64(round(f.crm_order_canceled)) AS `CRM: Заказ отменен`,
            toInt64(0) AS kol_vo_zayavok,
            toInt64(0) AS korr,
            toInt64(0) AS nekorr,
            toInt64(0) AS kval,
            toInt64(0) AS priezd,
            toInt64(0) AS prodazhi,
            f.`специалист` AS `специалист`,
            ds.domain AS domain,
            now() AS updated_at
        FROM ad_analytics.fact_criterion_spend f
        LEFT JOIN ad_analytics.Dim_Criterion dcr ON dcr.criterion_key = f.criterion_key
        LEFT JOIN ad_analytics.Dim_Site ds ON ds.site_key = f.site_key
        {where_sql}
    """


def _criterion_spend_star_pbi_sql(where_sql: str = "") -> str:
    # CRITERION_CRM_SUMS_2026-08-24: TMDL fact_criterion_spend reads `bi_fact_criterion_spend_star`
    # (not `bi_fact_criterion_spend`) and expects these 5 columns already under their Russian
    # display names — the M code does no rename step for this table (unlike fact_region_spend,
    # which renames snake_case -> Russian in Power Query). Without them M's
    # `Table.SelectColumns(..., MissingField.UseNull)` + `Table.ReplaceValue(null, 0, ...)`
    # silently turns the whole column into zeros, same failure shape as the `toInt64(0)` literals
    # below in `_criterion_spend_pbi_sql` — just one layer further downstream.
    return f"""
        SELECT
            f.date,
            f.campaign_id,
            f.ad_group_id,
            f.ad_network_type_key,
            {_pbi_int64_key("f.criterion_key")} AS criterion_key,
            {_pbi_int64_key("f.site_key")} AS site_key,
            toFloat64(f.cost) AS cost,
            toFloat64(f.clicks) AS clicks,
            toFloat64(f.impressions) AS impressions,
            toFloat64(f.all_forms) AS `Все формы`,
            toFloat64(f.crm_order_created) AS `CRM: Заказ создан`,
            toFloat64(f.crm_order_paid) AS `CRM: Заказ оплачен`,
            toFloat64(f.crm_spam_order) AS `CRM: Спам заказ`,
            toFloat64(f.crm_order_canceled) AS `CRM: Заказ отменен`,
            f.`специалист` AS `специалист`
        FROM ad_analytics.fact_criterion_spend f
        {where_sql}
    """


def _region_zayavki_pbi_sql() -> str:
    return """
        SELECT
            f.created_date,
            f.campaign_id,
            f.id_location,
            dl.distance_km_agreg,
            f.kol_vo_zayavok,
            f.korr,
            f.kval,
            f.priezd,
            f.prodazhi,
            f.nekorr,
            f.ne_otvechaet,
            f.filtr,
            f.nedozvon,
            f.priedet,
            f.dohod_do_kredita,
            f.dobro,
            f.updated_at
        FROM ad_analytics.fact_region_zayavki f
        LEFT JOIN ad_analytics.Dim_Location dl ON dl.id_location = f.id_location
    """


def _criterion_zayavki_pbi_sql() -> str:
    return """
        SELECT
            created_date,
            campaign_id,
            criterion,
            kol_vo_zayavok,
            korr,
            kval,
            priezd,
            prodazhi,
            nekorr,
            ne_otvechaet,
            filtr,
            nedozvon,
            priedet,
            dohod_do_kredita,
            dobro,
            updated_at
        FROM ad_analytics.fact_criterion_zayavki
    """


def build_pbi_import_region_spend(client) -> int:
    _replace_view(client, "pbi_import_region_spend", _region_spend_pbi_sql())
    return count_rows(client, "ad_analytics.pbi_import_region_spend")


def _dim_criterion_sql() -> str:
    return f"""
        SELECT
            criterion_key,
            argMax(criterion, sort_weight) AS criterion,
            argMax(criterion_type, sort_weight) AS criterion_type,
            argMax(criterion_raw, sort_weight) AS criterion_raw
        FROM
        (
            SELECT
                criterion_key,
                criterion,
                criterion_type,
                criterion_raw,
                tuple(rows, source_priority, lengthUTF8(criterion)) AS sort_weight
            FROM
            (
                SELECT
                    cityHash64(lowerUTF8(trim(BOTH ' ' FROM criterion_norm))) AS criterion_key,
                    trim(BOTH ' ' FROM criterion_norm) AS criterion,
                    anyLast(multiIf(
                        positionCaseInsensitive(criterion_norm, 'autotargeting') > 0, 'autotargeting',
                        positionCaseInsensitive(criterion_norm, 'ретаргетинг') > 0, 'retargeting',
                        positionCaseInsensitive(criterion_norm, 'интерес') > 0 OR positionCaseInsensitive(criterion_norm, 'привычк') > 0, 'interests',
                        'keyword'
                    )) AS criterion_type,
                    anyLast(criterion_raw) AS criterion_raw,
                    count() AS rows,
                    2 AS source_priority
                FROM
                (
                    SELECT
                        {CRITERION_CLEAN} AS criterion_norm,
                        criterion AS criterion_raw
                    FROM raw_data.yandex_direct_report_rows
                    WHERE toDate(day) >= toDate('{DATE_FROM}')
                      AND campaign_id != 0
                )
                WHERE notEmpty(lowerUTF8(trim(BOTH ' ' FROM criterion_norm)))
                GROUP BY criterion_key, criterion
            )
            UNION ALL
            SELECT
                criterion_key,
                criterion,
                criterion_type,
                criterion_raw,
                tuple(rows, source_priority, lengthUTF8(criterion)) AS sort_weight
            FROM
            (
                SELECT
                    cityHash64(lowerUTF8(trim(BOTH ' ' FROM ifNull(criterion, '')))) AS criterion_key,
                    trim(BOTH ' ' FROM ifNull(criterion, '')) AS criterion,
                    anyLast(criterion_type) AS criterion_type,
                    anyLast(criterion_raw) AS criterion_raw,
                    count() AS rows,
                    1 AS source_priority
                FROM ad_analytics.fact_criterion_zayavki
                WHERE notEmpty(lowerUTF8(trim(BOTH ' ' FROM ifNull(criterion, ''))))
                GROUP BY criterion_key, criterion
            )
        )
        GROUP BY criterion_key
    """


def build_dim_criterion(client) -> int:
    shadow = "ad_analytics.Dim_Criterion_new"
    client.command(f"DROP TABLE IF EXISTS {shadow} SYNC")
    client.command(
        f"""
        CREATE TABLE {shadow}
        ENGINE = MergeTree
        ORDER BY criterion_key
        AS
        {_dim_criterion_sql()}
        """,
        settings=SAFE_QUERY_SETTINGS,
    )
    swap_shadow(client, "ad_analytics.Dim_Criterion", shadow)
    _replace_view(client, "dim_criterion", "SELECT * FROM ad_analytics.Dim_Criterion")
    return count_rows(client, "ad_analytics.Dim_Criterion")


# ARP_LIVE_2026-08-23. `analytics_report_placement` в Power BI читал замороженный снимок БА5
# `raw_new_arp_fact` (2026-07-01..2026-08-13, сам не обновляется). Здесь он заменён живой вьюхой
# над `fact_direct_feed_funnel` + воронка из `raw_leads` по механике БА5
# (`work/big_analytics_v5/step_cron_night/report_placement/step2_build_analytics.py`):
#   • площадка лида достаётся из `utm_source` (`s:<...>`), снимается префикс `www.`/`m.`,
#     затем lower; `none` при непустой кампании → `yandex`;
#   • ключ матчинга = created_date | campaign_id | group_id (0 для кампаний `tp6`/`tp7`) | площадка;
#   • этап D БА5 («только заявки», `логин IS NULL`) НЕ воспроизводится: в Power BI он и в БА5 не
#     доезжал — `public.arp_fact` (`work/big_analytics_v5/star_refactor/build_star.py:936`)
#     заканчивается `WHERE domain IS NOT NULL`, а такие строки несут `domain = NULL`; в снимке
#     `raw_new_arp_fact` строк с пустым `логин` = 0. Замер 2026-08-23: ветка добавляла бы
#     7 772 строки с 89 944 заявками против 45 600 по всей стороне Директа — тройной перекос.
# ⚠️ Расход здесь — `total_cost` (с НДС и комиссией), канон BA6
# (`spend/build_direct_spend_staging.py`), поэтому он ВЫШЕ снимка БА5, который нёс `cost` без НДС.
# Это разница определения метрики, а не дефект — обратно не чинить.

_ARP_WWW_PREFIX_RE = r"'^(www\\.|m\\.)'"

# БА5 на стороне Директа: `re.sub(r'^(www\.|m\.)', '', placement.lower())`.
# SEARCH_PLACEMENT_LOCALE_2026-08-23: отчёт БА5 тянулся из Директа на английской локали и нёс площадку
# поиска `Yandex`; `raw_data.yandex_direct_report_rows` в BA6 — на русской, `Яндекс`. Лид со
# стороны CRM даёт литерал `yandex` (правило `none` + кампания → `yandex`), поэтому без этого
# маппинга вся поисковая ветка воронки не матчилась: замер на 2026-07-01..2026-08-13 —
# 11 745 заявок из 35 830 (33%) терялись именно на `яндекс`.
# LOWER_EXPLICIT_2026-08-23: `Dim_PlacementFeed.placement_feed_key` сегодня уже lower, но это
# инвариант чужого билдера, а не гарантия — регистр площадки ломает матчинг воронки молча.
_ARP_PLACEMENT_LOWER = "lowerUTF8(placement_feed_key)"
_ARP_SEARCH_PLACEMENT_KEY = (
    f"if({_ARP_PLACEMENT_LOWER} = 'яндекс', 'yandex', {_ARP_PLACEMENT_LOWER})"
)
_ARP_DIRECT_PLACEMENT_KEY = f"replaceRegexpOne({_ARP_SEARCH_PLACEMENT_KEY}, {_ARP_WWW_PREFIX_RE}, '')"

# БА5 на стороне лидов: `LOWER(REGEXP_REPLACE(COALESCE(regexp_match(utm_source, ...)[1], utm_source, ''),
# '^(www\.|m\.)', ''))` — порядок именно такой: сначала снять префикс, потом lower.
_ARP_LEAD_PLACEMENT_CAPTURE = r"extract(ifNull(utm_source, ''), '(?:^|[^a-z])s:(.+)$')"

_ARP_FUNNEL_COLUMNS = (
    "kol_vo_zayavok", "korr", "kval", "priezd", "prodazhi", "nekorr",
    "ne_otvechaet", "nedozvon", "filtr", "priedet", "dohod_do_kredita", "dobro",
)


def _arp_columns(template: str) -> str:
    return ",\n            ".join(template.format(name=name) for name in _ARP_FUNNEL_COLUMNS)


def _arp_lead_metrics_sql() -> str:
    """Лиды в разрезе БА5-ключа: дата | кампания | группа | площадка + воронка `_metric_expr`."""
    return f"""
        SELECT
            created_date,
            campaign_id_raw,
            ifNull(campaign_id_raw, 0) AS campaign_id,
            ad_group_id,
            if(campaign_id_raw IS NOT NULL AND campaign_id_raw != 0 AND placement_base = 'none',
               'yandex', placement_base) AS placement,
            {_metric_expr("status", "reason", "source_type", "salon")}
        FROM
        (
            SELECT
                created_date,
                campaign_id_raw,
                ad_group_id,
                utm_source,
                lowerUTF8(replaceRegexpOne(
                    if(notEmpty(placement_capture), placement_capture, utm_source),
                    {_ARP_WWW_PREFIX_RE}, ''
                )) AS placement_base,
                status,
                reason,
                source_type,
                salon
            FROM
            (
                SELECT
                    assumeNotNull(created_date) AS created_date,
                    campaign_id AS campaign_id_raw,
                    if(match(lowerUTF8(ifNull(utm_campaign, '')), 'tp[67]'), 0, ifNull(group_id, 0)) AS ad_group_id,
                    ifNull(utm_source, '') AS utm_source,
                    {_ARP_LEAD_PLACEMENT_CAPTURE} AS placement_capture,
                    status,
                    reason,
                    source_type,
                    salon
                FROM ad_analytics.raw_leads
                WHERE created_date >= toDate('{DATE_FROM}')
                  AND ifNull(deal_type, '') != 'Звонок'
                  AND is_copy_for_removal = 0
            )
        )
    """


def _arp_direct_leads_sql() -> str:
    """Этап B БА5: воронка, приклеиваемая к строкам Директа по ключу key2."""
    return f"""
        SELECT
            created_date,
            campaign_id,
            ad_group_id,
            placement,
            toUInt8(1) AS matched,
            {_arp_columns("toInt64(sum({name})) AS {name}")}
        FROM ({_arp_lead_metrics_sql()})
        WHERE campaign_id_raw IS NOT NULL
        GROUP BY created_date, campaign_id, ad_group_id, placement
    """


def _analytics_report_placement_pbi_sql() -> str:
    return f"""
        SELECT
            f.date AS date,
            f.domain AS `домен`,
            f.account_login AS `логин`,
            pf.ad_network_type AS ad_network_type,
            pf.placement AS placement,
            f.placement_feed_key AS placement_feed_key,
            f.placement_key_norm AS placement_key,
            toFloat64(f.cost) AS cost,
            toInt64(round(f.all_forms)) AS `Все формы`,
            toInt64(round(f.crm_order_created)) AS `CRM: Заказ создан`,
            toInt64(round(f.crm_order_paid)) AS `CRM: Заказ оплачен`,
            toInt64(0) AS `CRM: Спам заказ`,
            toInt64(0) AS `CRM: Заказ отменен`,
            CAST(dc.tp, 'Nullable(String)') AS tp,
            ds.`специалист` AS `Специалист`,
            ds.`салон` AS `салон`,
            ds.`тип_сайта` AS `тип_сайта`,
            now() AS updated_at,
            {_arp_columns("toInt64(ifNull(l.{name}, 0)) AS {name}")},
            CAST(concat(toString(f.campaign_id), '|', ifNull(dc.CampaignName, '')), 'Nullable(String)')
                AS `номер кампании|название кампании`,
            CAST(f.campaign_id, 'Nullable(Int64)') AS CampaignId,
            CAST(f.ad_group_id, 'Nullable(Int64)') AS AdGroupId,
            toInt64(round(f.clicks)) AS clicks,
            f.date AS `Date`,
            f.domain AS domain,
            if(ifNull(l.matched, 0) = 1, CAST('Заявки', 'Nullable(String)'), CAST(NULL, 'Nullable(String)'))
                AS `тип_заявки`
        FROM
        (
            SELECT *, {_ARP_DIRECT_PLACEMENT_KEY} AS placement_key_norm
            FROM ad_analytics.fact_direct_feed_funnel
        ) f
        LEFT JOIN ad_analytics.Dim_PlacementFeed pf ON pf.placement_feed_key = f.placement_feed_key
        LEFT JOIN ad_analytics.Dim_Campaign dc ON dc.CampaignId = f.campaign_id
        LEFT JOIN ad_analytics.Dim_Site ds ON ds.site_key = f.site_key
        LEFT JOIN ({_arp_direct_leads_sql()}) l
               ON l.created_date = f.date
              AND l.campaign_id = f.campaign_id
              AND l.ad_group_id = f.ad_group_id
              AND l.placement = f.placement_key_norm
        WHERE ifNull(ds.domain, '') != ''
        """


def _search_query_report_master_pbi_sql() -> str:
    """Живой агрегат `yd_search_query_report_master` без `query`/`criterion` — зерно снимка БА5."""
    return """
        SELECT
            loaded_at,
            date_from,
            date_to,
            client_login,
            multiIf(
                criterion_type = 'AUTOTARGETING_CRITERION_TYPE', 'Автотаргетинг',
                criterion_type = 'KEYWORD_CRITERION_TYPE', 'Ключевые слова',
                criterion_type
            ) AS criterion_type,
            multiIf(
                targeting_category = 'EXACT', 'Точное соответствие',
                targeting_category = 'ALTERNATIVE', 'Альтернативные запросы',
                targeting_category = 'NARROW', 'Узкие запросы',
                targeting_category = 'ACCESSORY', 'Сопутствующие запросы',
                targeting_category = 'BROADER', 'Широкие запросы',
                targeting_category = 'UNDEFINED', 'Не определено',
                targeting_category
            ) AS targeting_category,
            multiIf(
                brand_options = 'NO_BRAND', 'Без бренда',
                brand_options = 'COMPETITOR_BRAND', 'Бренды конкурентов',
                brand_options = 'SELF_BRAND', 'Свой бренд',
                brand_options = 'UNKNOWN_BRAND', 'Бренд не определен',
                brand_options
            ) AS brand_options,
            campaign_id,
            ad_group_id,
            toInt64(sum(impressions)) AS impressions,
            toInt64(sum(clicks)) AS clicks,
            sum(cost) AS cost,
            toInt64(sum(goal_all_forms)) AS goal_all_forms,
            toInt64(sum(goal_crm_order_paid)) AS goal_crm_order_paid
        FROM
        (
            SELECT
                toDate(loaded_at) AS loaded_at,
                date_from,
                date_to,
                client_login,
                CAST(criterion_type, 'String') AS criterion_type,
                CAST(targeting_category, 'String') AS targeting_category,
                CAST(brand_options, 'String') AS brand_options,
                toInt64(campaign_id) AS campaign_id,
                toInt64(ad_group_id) AS ad_group_id,
                impressions,
                clicks,
                cost,
                goal_all_forms,
                goal_crm_order_paid
            FROM ad_analytics.yd_search_query_report_master
        )
        GROUP BY loaded_at, date_from, date_to, client_login, criterion_type,
                 targeting_category, brand_options, campaign_id, ad_group_id
    """


def create_light_aliases(client) -> dict[str, int]:
    statements = {
        "yandex_direct_korrektirovki": """
            WITH gs_login AS
            (
                SELECT
                    lower(ifNull(login_key, '')) AS login_key,
                    anyLast(directologist) AS directologist
                FROM reference_data.gsheet_sites
                WHERE ifNull(login_key, '') != ''
                  AND niche = 'Авто'
                GROUP BY login_key
            )
            SELECT
                modifier_id AS id,
                account_login AS ulogin,
                campaign_id,
                campaign_name,
                ad_group_id,
                level,
                modifier_id,
                enabled,
                modifier_type,
                modifier_name,
                bid_percent,
                korrektirovki_bid,
                audience_id,
                gs.directologist AS `специалист`,
                k.campaign_status,
                CAST(NULL, 'Nullable(String)') AS status,
                parseDateTimeBestEffortOrNull(synced_at) AS loaded_at
            FROM raw_data.yandex_direct_korrektirovki k
            LEFT JOIN gs_login gs ON gs.login_key = lower(ifNull(k.account_login, ''))
        """,
    }
    out: dict[str, int] = {}
    for table, sql in statements.items():
        _replace_view(client, table, sql)
        out[table] = count_rows(client, f"ad_analytics.{table}")
    empty_views = {
        "check_utm_fuck_direct": [
            ("id", "Nullable(Int64)"), ("login", "Nullable(String)"), ("CampaignId", "Nullable(Int64)"),
            ("CampaignName", "Nullable(String)"), ("group_id", "Nullable(Int64)"), ("group_name", "Nullable(String)"),
            ("tracking_params", "Nullable(String)"), ("домен", "Nullable(String)"),
            ("cost", "Nullable(Decimal(18, 6))"), ("специалист", "Nullable(String)"),
            ("date", "Nullable(Date)"), ("utm_source_type", "Nullable(String)"),
        ],
        "yandex_direct_minus_snapshot": [
            ("id", "Nullable(Int64)"), ("date", "Nullable(Date)"), ("login", "Nullable(String)"),
            ("campaign_id", "Nullable(Int64)"), ("campaign_name", "Nullable(String)"),
            ("campaign_state", "Nullable(String)"), ("block", "Nullable(String)"),
            ("minus_in_campaign", "Nullable(Int64)"), ("minus_in_groups", "Nullable(Int64)"),
            ("minus_in_sets", "Nullable(Int64)"), ("minus_total", "Nullable(Int64)"),
            ("has_minus", "Nullable(Bool)"), ("check_ok", "Nullable(Bool)"),
            ("loaded_at", "Nullable(DateTime)"), ("специалист", "Nullable(String)"),
        ],
        "v_yandex_direct_minus_delta": [
            ("date", "Nullable(Date)"), ("login", "Nullable(String)"), ("campaign_id", "Nullable(Int64)"),
            ("minus_total", "Nullable(Int64)"), ("minus_total_prev", "Nullable(Int64)"), ("delta", "Nullable(Int64)"),
            ("dynamics", "Nullable(String)"), ("campaign_name", "Nullable(String)"),
            ("campaign_state", "Nullable(String)"), ("block", "Nullable(String)"),
            ("minus_in_campaign", "Nullable(Int64)"), ("minus_in_groups", "Nullable(Int64)"),
            ("minus_in_sets", "Nullable(Int64)"), ("has_minus", "Nullable(Bool)"),
            ("check_ok", "Nullable(Bool)"), ("специалист", "Nullable(String)"),
        ],
        "yandex_direct_404_errors": [
            ("id", "Nullable(Int64)"), ("№ счетчика", "Nullable(String)"),
            ("counter_name", "Nullable(String)"), ("site", "Nullable(String)"),
            ("специалист", "Nullable(String)"), ("url", "Nullable(String)"),
            ("page_title", "Nullable(String)"), ("utm_campaign", "Nullable(String)"),
            ("№ кампании", "Nullable(Int64)"), ("utm_content", "Nullable(String)"),
            ("№ группы", "Nullable(Int64)"), ("detected_at", "Nullable(DateTime)"),
            ("visit_date", "Nullable(Date)"), ("week_start", "Nullable(Date)"),
        ],
        "yandex_direct_cookie_analytics_website_pages": [
            ("id", "Nullable(Int64)"), ("login_key", "Nullable(String)"), ("domain", "Nullable(String)"),
            ("clicks", "Nullable(Decimal(18, 6))"), ("goal_all_forms", "Nullable(Decimal(18, 6))"),
            ("goal_crm_order_created", "Nullable(Decimal(18, 6))"),
            ("goal_crm_order_paid", "Nullable(Decimal(18, 6))"), ("final_url", "Nullable(String)"),
            ("directologist", "Nullable(String)"), ("template", "Nullable(String)"),
            ("salon", "Nullable(String)"), ("city", "Nullable(String)"), ("region", "Nullable(String)"),
            ("site_type", "Nullable(String)"), ("loaded_at", "Nullable(DateTime)"),
            ("page_type", "Nullable(String)"), ("banner_href", "Nullable(String)"),
            ("date_from", "Nullable(Date)"), ("date_to", "Nullable(Date)"),
            ("sum", "Nullable(Decimal(18, 6))"), ("agoalnum", "Nullable(String)"),
            ("aconv", "Nullable(Decimal(18, 6))"), ("agoalcost", "Nullable(Decimal(18, 6))"),
        ],
    }
    for table, cols in empty_views.items():
        if table_exists(client, "ad_analytics", table):
            out[table] = count_rows(client, f"ad_analytics.{table}")
            continue
        select_sql = ", ".join(f"CAST(NULL, '{typ}') AS {q(name)}" for name, typ in cols)
        _replace_view(client, table, f"SELECT {select_sql} WHERE 0")
        out[table] = count_rows(client, f"ad_analytics.{table}")
    return out


DIRECT_SERVICE_BI_SELECTS = {
    "yandex_direct_korrektirovki": """
        SELECT
            id,
            ulogin,
            campaign_id,
            ad_group_id,
            level,
            modifier_id,
            enabled,
            modifier_type,
            modifier_name,
            bid_percent,
            korrektirovki_bid,
            audience_id,
            loaded_at,
            status
        FROM ad_analytics.yandex_direct_korrektirovki
    """,
    "yandex_direct_minus_snapshot": """
        SELECT
            toInt64(id % 9223372036854775807) AS id,
            date,
            login,
            campaign_id,
            campaign_state,
            block,
            minus_in_campaign,
            minus_in_groups,
            minus_in_sets,
            minus_total,
            has_minus,
            check_ok,
            loaded_at
        FROM ad_analytics.yandex_direct_minus_snapshot
    """,
    "v_yandex_direct_minus_delta": """
        SELECT
            date,
            login,
            campaign_id,
            minus_total,
            minus_total_prev,
            delta,
            dynamics,
            campaign_state,
            block,
            minus_in_campaign,
            minus_in_groups,
            minus_in_sets,
            has_minus,
            check_ok
        FROM ad_analytics.v_yandex_direct_minus_delta
    """,
}


def _dim_date_pbi_sql() -> str:
    return """
        SELECT
            `Date`,
            `День недели`,
            week_start,
            toInt64(year) AS year,
            toInt64(month) AS month,
            toInt64(month_key) AS month_key,
            year_month,
            toInt64(day) AS day
        FROM ad_analytics.Dim_Date
    """


def _campaign_status_ru_expr(expr: str) -> str:
    clean = f"trim(BOTH ' ' FROM ifNull({expr}, ''))"
    upper = f"upperUTF8({clean})"
    return (
        f"multiIf({clean} = '', 'Не указана', "
        f"{upper} IN ('ACCEPTED', 'ACTIVE'), 'Активна', "
        f"{upper} = 'DRAFT', 'Черновик', "
        f"{upper} = 'MODERATION', 'На модерации', "
        f"{upper} = 'REJECTED', 'Отклонена', "
        f"{upper} IN ('SUSPENDED', 'STOPPED'), 'Остановлена', "
        f"{upper} = 'ARCHIVED', 'Архив', {expr})"
    )


def _payment_model_ru_expr(expr: str) -> str:
    clean = f"trim(BOTH ' ' FROM ifNull({expr}, ''))"
    upper = f"upperUTF8({clean})"
    return (
        f"multiIf({clean} = '', 'Не указана', "
        f"{upper} = 'CPA', 'за конверсии', "
        f"{upper} = 'CPC', 'за клики', {expr})"
    )


def _ad_network_type_ru_expr(expr: str) -> str:
    clean = f"trim(BOTH ' ' FROM ifNull({expr}, ''))"
    upper = f"upperUTF8({clean})"
    return (
        f"multiIf({clean} = '', 'Не указана', "
        f"{upper} = 'SEARCH', 'Поиск', "
        f"{upper} = 'AD_NETWORK', 'РСЯ', {expr})"
    )


def _dim_ad_network_type_pbi_sql() -> str:
    ad_network_type = _ad_network_type_ru_expr("ad_network_type")
    ad_network_type_camel = _ad_network_type_ru_expr("AdNetworkType")
    return f"""
        SELECT
            ad_network_type_key,
            {ad_network_type} AS ad_network_type,
            {ad_network_type_camel} AS AdNetworkType
        FROM ad_analytics.Dim_AdNetworkType
    """


def _dim_ad_format_pbi_sql() -> str:
    return """
        SELECT
            ad_format_key,
            anyLast(multiIf(
                ad_format = 'IMAGE', 'графический',
                ad_format = 'TEXT', 'текстовый (ТГО)',
                ad_format = 'VIDEO', 'видео',
                ad_format IN ('SMART_SINGLE', 'SMART_MULTIPLE', 'SMART_TILE'), 'смарт-баннер',
                ad_format = 'ADAPTIVE_IMAGE', 'адаптивный графический',
                ad_format = 'multicard', 'комбинаторное объявление',
                ad_format
            )) AS ad_format
        FROM (
            SELECT
                lowerUTF8(trim(BOTH ' ' FROM ifNull(ad_format, ''))) AS ad_format_key,
                ifNull(ad_format, '') AS ad_format
            FROM ad_analytics.fact_adformat_spend
        )
        GROUP BY ad_format_key
    """


def _dim_ad_text_pbi_sql() -> str:
    return """
        SELECT
            banner_id,
            argMax(banner_type, loaded_at) AS ad_type,
            argMax(banner_status, loaded_at) AS status,
            argMax(banner_title, loaded_at) AS title,
            argMax(banner_body, loaded_at) AS text,
            argMax(banner_href, loaded_at) AS banner_href
        FROM raw_data.direct_cookie_ads_texts_master
        GROUP BY banner_id
    """


def _dim_adjustment_pbi_sql() -> str:
    return """
        SELECT
            RlAdjustmentId,
            RlAdjustmentId_total
        FROM ad_analytics.Dim_Adjustment
    """


def _dim_device_pbi_sql() -> str:
    return """
        SELECT
            device_key,
            Device
        FROM ad_analytics.Dim_Device
    """


def _dim_manager_login_pbi_sql() -> str:
    return """
        SELECT
            toInt64(manager_login_key % 9223372036854775807) AS manager_login_key,
            manager_login
        FROM ad_analytics.Dim_ManagerLogin
    """


def _dim_account_pbi_sql() -> str:
    return """
        SELECT
            account_key,
            account_login
        FROM ad_analytics.Dim_Account
    """


def _dim_crm_status_pbi_sql() -> str:
    return """
        SELECT
            crm_status_key,
            `Название crm`,
            `тип_заявки`,
            `статус`,
            cascade_level
        FROM ad_analytics.Dim_CRMStatus
    """


def _dim_salon_pbi_sql() -> str:
    return """
        SELECT
            salon_key,
            `салон`,
            `город`,
            `регион`,
            `тип_сайта`,
            `шаблон`,
            `специалист`,
            `проджект`,
            `менеджер`,
            `id_салона`,
            `направление`
        FROM ad_analytics.Dim_Salon
    """


def _dim_source_pbi_sql() -> str:
    return """
        SELECT
            source_key,
            `источник`,
            `поставщик`,
            _source_table
        FROM ad_analytics.Dim_Source
    """


def _dim_campaign_pbi_sql() -> str:
    campaign_status = _campaign_status_ru_expr("dc.campaign_status")
    payment_model = _payment_model_ru_expr("dc.payment_model")
    cpc_cpa = _payment_model_ru_expr("dc.cpc_cpa")
    service_campaign_status = _campaign_status_ru_expr("ss.campaign_status")
    return f"""
        WITH auto_accounts AS (
            SELECT DISTINCT lower(ifNull(login_key, '')) AS account_login
            FROM reference_data.gsheet_sites
            WHERE ifNull(login_key, '') != ''
              AND niche = 'Авто'
        ),
        service_campaigns AS (
            SELECT
                assumeNotNull(campaign_id) AS CampaignId,
                anyLastIf(campaign_name, campaign_name IS NOT NULL AND campaign_name != '') AS CampaignName,
                anyLastIf(account_login, account_login IS NOT NULL AND account_login != '') AS account_login,
                anyLastIf(campaign_status, campaign_status IS NOT NULL AND campaign_status != '') AS campaign_status,
                anyLastIf(specialist, specialist IS NOT NULL AND specialist != '') AS specialist
            FROM (
                SELECT
                    campaign_id,
                    campaign_name,
                    ulogin AS account_login,
                    campaign_status,
                    `специалист` AS specialist
                FROM ad_analytics.yandex_direct_korrektirovki
                WHERE campaign_id IS NOT NULL
                  AND campaign_id != 0
                  AND lower(ifNull(ulogin, '')) IN (SELECT account_login FROM auto_accounts)
                UNION ALL
                SELECT
                    campaign_id,
                    campaign_name,
                    login AS account_login,
                    CAST(NULL, 'Nullable(String)') AS campaign_status,
                    `специалист` AS specialist
                FROM ad_analytics.yandex_direct_minus_snapshot
                WHERE campaign_id != 0
                  AND lower(ifNull(login, '')) IN (SELECT account_login FROM auto_accounts)
                UNION ALL
                SELECT
                    campaign_id,
                    campaign_name,
                    login AS account_login,
                    CAST(NULL, 'Nullable(String)') AS campaign_status,
                    CAST(NULL, 'Nullable(String)') AS specialist
                FROM ad_analytics.yandex_direct_history
                WHERE campaign_id IS NOT NULL
                  AND campaign_id != 0
                  AND lower(ifNull(login, '')) IN (SELECT account_login FROM auto_accounts)
            )
            GROUP BY CampaignId
        )
        SELECT
            dc.CampaignId,
            dc.CampaignName,
            dc.account_login,
            dc.campaign_code,
            dc.tp,
            {cpc_cpa} AS cpc_cpa,
            dc.site_quiz,
            {campaign_status} AS `статус_кампании`,
            ss.specialist AS `специалист`,
            CAST(NULL, 'Nullable(String)') AS manager_login,
            {campaign_status} AS campaign_status,
            {payment_model} AS payment_model,
            {payment_model} AS `тип_оплаты`,
            dc.`номер кампании | название кампании`
        FROM ad_analytics.Dim_Campaign dc
        LEFT JOIN service_campaigns ss ON ss.CampaignId = dc.CampaignId
        UNION ALL
        SELECT
            ss.CampaignId,
            ss.CampaignName,
            ifNull(ss.account_login, '') AS account_login,
            CAST(NULL, 'Nullable(String)') AS campaign_code,
            '' AS tp,
            'Не указана' AS cpc_cpa,
            '' AS site_quiz,
            {service_campaign_status} AS `статус_кампании`,
            ss.specialist AS `специалист`,
            CAST(NULL, 'Nullable(String)') AS manager_login,
            {service_campaign_status} AS campaign_status,
            'Не указана' AS payment_model,
            'Не указана' AS `тип_оплаты`,
            concat(toString(ss.CampaignId), ' | ', ifNull(ss.CampaignName, '')) AS `номер кампании | название кампании`
        FROM service_campaigns ss
        LEFT JOIN ad_analytics.Dim_Campaign dc ON dc.CampaignId = ss.CampaignId
        WHERE dc.CampaignId = 0
    """


def _dim_adgroup_pbi_sql() -> str:
    return """
        SELECT
            AdGroupId,
            AdGroupName,
            if(trim(ifNull(adgroup_code, '')) = '', 'Не указано', adgroup_code) AS adgroup_code,
            `номер группы | название группы`,
            if(trim(ifNull(`марки авто`, '')) = '', 'Не указано', `марки авто`) AS `марки авто`,
            if(trim(ifNull(ag_part1, '')) = '', 'Не указано', ag_part1) AS ag_part1,
            if(trim(ifNull(ag_part2, '')) = '', 'Не указано', ag_part2) AS ag_part2,
            if(trim(ifNull(ag_part3, '')) = '', 'Не указано', ag_part3) AS ag_part3,
            if(trim(ifNull(ag_part4, '')) = '', 'Не указано', ag_part4) AS ag_part4,
            if(trim(ifNull(ag_part5, '')) = '', 'Не указано', ag_part5) AS ag_part5,
            if(trim(ifNull(ag_part6, '')) = '', 'Не указано', ag_part6) AS ag_part6,
            if(trim(ifNull(ag_part7, '')) = '', 'Не указано', ag_part7) AS ag_part7,
            if(trim(ifNull(ag_part1, '')) = '', 'Не указано', ag_part1) AS ag_part1_name,
            `неверный_кодер_new`,
            parent_CampaignId
        FROM ad_analytics.Dim_AdGroup
        UNION ALL
        SELECT
            sg.AdGroupId,
            CAST(NULL, 'Nullable(String)') AS AdGroupName,
            'Не указано' AS adgroup_code,
            '' AS `номер группы | название группы`,
            'Не указано' AS `марки авто`,
            'Не указано' AS ag_part1,
            'Не указано' AS ag_part2,
            'Не указано' AS ag_part3,
            'Не указано' AS ag_part4,
            'Не указано' AS ag_part5,
            'Не указано' AS ag_part6,
            'Не указано' AS ag_part7,
            'Не указано' AS ag_part1_name,
            CAST(NULL, 'Nullable(String)') AS `неверный_кодер_new`,
            sg.parent_CampaignId
        FROM (
            SELECT
                assumeNotNull(ad_group_id) AS AdGroupId,
                anyLast(ifNull(campaign_id, 0)) AS parent_CampaignId
            FROM ad_analytics.yandex_direct_korrektirovki
            WHERE ad_group_id IS NOT NULL
              AND ad_group_id != 0
            GROUP BY AdGroupId
        ) sg
        LEFT JOIN ad_analytics.Dim_AdGroup da ON da.AdGroupId = sg.AdGroupId
        WHERE da.AdGroupId = 0
    """


def _dim_city_tier_pbi_sql() -> str:
    return """
        SELECT
            city_tier_key,
            `город`,
            `тир_месяца`,
            `тир_месяца_backfill`,
            `тир_текущий`
        FROM ad_analytics.Dim_City_Tier
    """


def _dim_site_pbi_sql() -> str:
    return f"""
        SELECT
            {_pbi_int64_key("site_key")} AS site_key,
            domain, `салон`, `город`, `регион`, `тип_сайта`, `шаблон`,
            `направление`, `статус`, status, `специалист`, `проджект`, project_manager,
            `id_салона`, `менеджер`, `Название crm`
        FROM ad_analytics.Dim_Site
    """


def _vk_ads_pbi_sql() -> str:
    return f"""
        SELECT
            f.date AS date,
            f.account_id AS account_id,
            {_pbi_int64_key("f.site_key")} AS site_key,
            f.domain AS domain,
            f.`салон`,
            f.ad_plan_id AS ad_plan_id,
            p.ad_plan_name AS ad_plan_name,
            f.ad_group_id AS ad_group_id,
            g.ad_group_name AS ad_group_name,
            f.banner_id AS banner_id,
            b.banner_name AS banner_name,
            f.`атрибуция`,
            f.shows,
            f.clicks,
            f.spent,
            f.`заявки`,
            f.`заявки_корр`,
            f.`записи`,
            f.`квал`,
            f.`визиты`,
            f.`продажи`,
            f.`регион`,
            f.`тип_сайта`,
            f.`специалист`
        FROM ad_analytics.fact_vk_ads f
        LEFT JOIN ad_analytics.Dim_VkAdPlan p ON f.ad_plan_id = p.ad_plan_id
        LEFT JOIN ad_analytics.Dim_VkAdGroup g ON f.ad_group_id = g.ad_group_id
        LEFT JOIN ad_analytics.Dim_VkBanner b ON f.banner_id = b.banner_id
    """


def _dim_criterion_pbi_sql() -> str:
    return f"""
        SELECT {_pbi_int64_key("criterion_key")} AS criterion_key, criterion, criterion_type, criterion_raw
        FROM ad_analytics.Dim_Criterion
    """


def _check_utm_direct_pbi_sql() -> str:
    return """
        SELECT
            id,
            login,
            CampaignId,
            group_id,
            tracking_params,
            `домен`,
            toFloat64(cost) AS cost,
            date,
            utm_source_type
        FROM ad_analytics.check_utm_fuck_direct
    """


def _fact_ml_korrektirovki_pbi_sql() -> str:
    return """
        SELECT
            CampaignId,
            AdGroupId,
            RlAdjustmentId,
            priezd_arrival_date,
            prodazhi_arrival_date,
            dohod_do_kredita,
            dobro,
            toFloat64(total_cost) AS total_cost,
            toFloat64(kol_vo_zayavok) AS kol_vo_zayavok,
            toFloat64(korr) AS korr,
            toFloat64(kval) AS kval,
            toFloat64(priezd) AS priezd,
            toFloat64(prodazhi) AS prodazhi,
            toFloat64(Clicks) AS Clicks,
            toFloat64(Impressions) AS Impressions,
            nekorr,
            ne_otvechaet,
            nedozvon,
            filtr,
            priedet,
            `План заявки`,
            `План приезда`,
            Date,
            domain,
            `атрибуция`,
            lowerUTF8(trim(BOTH ' ' FROM ifNull(`источник`, ''))) AS source_key,
            lowerUTF8(trim(BOTH ' ' FROM ifNull(AdNetworkType, ''))) AS ad_network_type_key,
            `аккаунт|сайт`,
            lowerUTF8(trim(BOTH ' ' FROM ifNull(Device, ''))) AS device_key,
            fid,
            `тип_заявки`,
            ml_audience_name,
            bid_percent,
            ml_tier
        FROM ad_analytics.fact_ml_korrektirovki
    """


def _direct_history_pbi_sql() -> str:
    return """
        SELECT
            toInt64(cityHash64(toString(ifNull(datetime, toDateTime(0))), ifNull(login, ''), ifNull(toString(campaign_id), ''), ifNull(new_value, '')) % 9223372036854775807) AS id,
            login AS ulogin,
            datetime,
            CAST(NULL, 'Nullable(String)') AS user_login,
            CAST(NULL, 'Nullable(Int64)') AS user_uid,
            change_source,
            event_type,
            CAST(NULL, 'Nullable(String)') AS category,
            campaign_id,
            old_value,
            new_value,
            CAST(NULL, 'Nullable(String)') AS raw_event,
            `директолог`,
            domain,
            salon,
            updated_at AS loaded_at
        FROM ad_analytics.yandex_direct_history
    """


def _pixel_score_pbi_sql() -> str:
    return """
        SELECT
            month,
            domain,
            lowerUTF8(trim(BOTH ' ' FROM ifNull(`источник`, ''))) AS source_key,
            CampaignId,
            kol_vo_zayavok,
            korr,
            kval,
            priezd,
            prodazhi,
            cpl_score,
            `pixel_kol_vo_домена`,
            `pixel_kol_vo_кампании`,
            `cpl_avg_квал`,
            `cpl_avg_визит`,
            `cpl_avg_продажа`,
            `cpl_кам_квал`,
            `cpl_кам_визит`,
            `cpl_кам_продажа`,
            `score_квал`,
            `score_визит`,
            `score_продажа`,
            `w_квал`,
            `w_визит`,
            `w_продажа`,
            `status_квал`,
            `status_визит`,
            `status_продажа`,
            `расход`,
            weight,
            `pixel_квал_домена`,
            `attr_pixel_квал_кампании`,
            `pixel_приезд_домена`,
            `attr_pixel_приезд_кампании`,
            `pixel_продажи_домена`,
            `attr_pixel_продажи_кампании`
        FROM ad_analytics.pixel_score
    """


def _cookie_pages_pbi_sql() -> str:
    return """
        SELECT
            id,
            login_key,
            domain,
            toInt64(ifNull(round(clicks), 0)) AS clicks,
            toInt64(ifNull(round(goal_all_forms), 0)) AS goal_all_forms,
            toInt64(ifNull(round(goal_crm_order_created), 0)) AS goal_crm_order_created,
            toInt64(ifNull(round(goal_crm_order_paid), 0)) AS goal_crm_order_paid,
            final_url,
            directologist,
            template,
            salon,
            city,
            region,
            site_type,
            loaded_at,
            page_type,
            banner_href,
            date_from,
            date_to,
            toFloat64(sum) AS sum,
            toInt64OrZero(ifNull(agoalnum, '')) AS agoalnum,
            toFloat64(aconv) AS aconv,
            toFloat64(agoalcost) AS agoalcost
        FROM ad_analytics.yandex_direct_cookie_analytics_website_pages
    """


def _direct_ads_texts_pbi_sql() -> str:
    return """
        SELECT
            loaded_at,
            client_login,
            campaign_id,
            adgroup_id AS ad_group_id,
            banner_id,
            toInt64(sum(shows)) AS impressions,
            toInt64(sum(clicks)) AS clicks,
            sum(cost) AS cost,
            toInt64(round(sum(goals))) AS goal_all_forms,
            toInt64(0) AS goal_crm_order_paid
        FROM raw_data.direct_cookie_ads_texts_master
        GROUP BY loaded_at, client_login, campaign_id, ad_group_id, banner_id
    """


def _direct_type_placement_pbi_sql() -> str:
    return """
        SELECT
            toInt64(cityHash64(
                toString(toStartOfMonth(scope_from)),
                client_login,
                toString(ifNull(campaign_id, 0)),
                toString(ifNull(adgroup_id, 0)),
                position_type
            ) % 9223372036854775807) AS id,
            loaded_at,
            toStartOfMonth(scope_from) AS date,
            client_login,
            campaign_id,
            adgroup_id AS ad_group_id,
            CAST(NULL, 'Nullable(String)') AS ad_network_type,
            position_type AS type_placement,
            multiIf(
                position_type = 'ADV_GALLERY_POSITION_TYPE', 'Товарная галерея',
                position_type = 'ALONE_POSITION_TYPE', 'Эксклюзивное размещение',
                position_type = 'BLOGGER_POSITION_TYPE', 'Блогер',
                position_type = 'CPA_NETWORK_POSITION_TYPE', 'Реклама в CPA-сети',
                position_type = 'MAKS_POSITION_TYPE', 'Реклама в MAX',
                position_type = 'MAPS_GEO_PRODUCT_POSITION_TYPE', 'Геореклама в Яндекс Картах',
                position_type = 'NON_PRIME_POSITION_TYPE', 'Прочее',
                position_type = 'ORGANIC_SEARCH_POSITION_TYPE', 'Динамические места на поиске',
                position_type = 'PRIME_POSITION_TYPE', 'Спецразмещение',
                position_type = 'SERP_GEO_WIZARD_POSITION_TYPE', 'Геоблок в поисковой выдаче',
                position_type = 'SERVICE_GALLERY_POSITION_TYPE', 'Галерея услуг',
                position_type = 'SUGGEST_POSITION_TYPE', 'Реклама в саджесте',
                position_type = 'TELEGRAM_POSITION_TYPE', 'Реклама в Telegram',
                position_type
            ) AS type_placement_ru,
            toInt64(sum(shows)) AS impressions,
            toInt64(sum(clicks)) AS clicks,
            sum(cost) AS cost,
            toInt64(round(sum(goals))) AS goal_all_forms,
            toInt64(0) AS goal_crm_order_paid
        FROM raw_data.direct_cookie_type_placement_master
        GROUP BY loaded_at, date, client_login, campaign_id, ad_group_id, type_placement, type_placement_ru
    """


def _direct_autorules_posevy_placement_links_sql() -> str:
    return """
        SELECT
            toDate(r.day) AS date,
            r.client_login AS account_login,
            any(coalesce(g.city, '')) AS city,
            l.placement_link AS placement_link,
            multiIf(
                positionCaseInsensitive(l.placement_link, 't.me/') > 0
                    OR positionCaseInsensitive(l.placement_link, 'telegram.me/') > 0,
                'телеграм',
                positionCaseInsensitive(l.placement_link, 'max.ru') > 0,
                'макс',
                'другое'
            ) AS source,
            r.campaign_id AS campaign_id,
            anyLast(coalesce(r.campaign_name, '')) AS campaign_name,
            round(sum(coalesce(r.total_cost, 0)), 2) AS cost,
            toInt64(sum(coalesce(r.impressions, 0))) AS impressions,
            toInt64(sum(coalesce(r.clicks, 0))) AS clicks,
            round(sum(coalesce(r.all_forms, 0)), 2) AS all_forms,
            round(sum(coalesce(r.crm_order_paid, 0)), 2) AS crm_order_paid,
            if(impressions > 0, round(clicks / impressions * 100, 2), NULL) AS ctr,
            if(all_forms > 0, round(cost / all_forms, 2), NULL) AS cpl_all_forms,
            if(crm_order_paid > 0, round(cost / crm_order_paid, 2), NULL) AS cpl_crm_order_paid
        FROM raw_data.yandex_direct_report_rows AS r
        INNER JOIN ad_analytics.yandex_direct_tp_placement_links AS l
            ON l.placement = r.placement
        LEFT JOIN (
            SELECT login_key, any(coalesce(city, '')) AS city
            FROM reference_data.gsheet_sites
            WHERE ifNull(login_key, '') != ''
              AND niche = 'Авто'
            GROUP BY login_key
        ) AS g ON g.login_key = r.client_login
        WHERE coalesce(l.placement_link, '') != ''
          AND (
            positionCaseInsensitive(coalesce(r.campaign_name, ''), 'tp8') > 0
            OR positionCaseInsensitive(coalesce(r.campaign_name, ''), 'tp9') > 0
            OR positionCaseInsensitive(coalesce(r.campaign_name, ''), 'tp10') > 0
          )
        GROUP BY date, account_login, placement_link, source, campaign_id
    """


PBI_VIEW_SQL_BUILDERS = {
    "fact_direct_feed_funnel": lambda: "SELECT * FROM ad_analytics.pbi_import_fact_direct_feed_funnel",
    "fact_direct_feed_funnel_star": _feed_funnel_star_pbi_sql,
    "Dim_Account": _dim_account_pbi_sql,
    "Dim_Date": _dim_date_pbi_sql,
    "Dim_AdFormat": _dim_ad_format_pbi_sql,
    "Dim_AdText": _dim_ad_text_pbi_sql,
    "Dim_AdNetworkType": _dim_ad_network_type_pbi_sql,
    "Dim_Adjustment": _dim_adjustment_pbi_sql,
    "Dim_Device": _dim_device_pbi_sql,
    "Dim_ManagerLogin": _dim_manager_login_pbi_sql,
    "Dim_CRMStatus": _dim_crm_status_pbi_sql,
    "Dim_Salon": _dim_salon_pbi_sql,
    "Dim_Source": _dim_source_pbi_sql,
    "Dim_Campaign": _dim_campaign_pbi_sql,
    "Dim_City_Tier": _dim_city_tier_pbi_sql,
    "Dim_AdGroup": _dim_adgroup_pbi_sql,
    "Dim_PlacementFeed": _dim_placement_feed_pbi_sql,
    "Dim_Site": _dim_site_pbi_sql,
    "fact_region_spend": _region_spend_pbi_sql,
    "fact_region_spend_star": _region_spend_star_pbi_sql,
    "fact_adformat_spend": _adformat_spend_pbi_sql,
    "fact_criterion_spend": _criterion_spend_pbi_sql,
    "fact_criterion_spend_star": _criterion_spend_star_pbi_sql,
    "fact_region_zayavki": _region_zayavki_pbi_sql,
    "fact_vk_ads": _vk_ads_pbi_sql,
    "fact_criterion_zayavki": _criterion_zayavki_pbi_sql,
    "Dim_Criterion": _dim_criterion_pbi_sql,
    "dim_criterion": _dim_criterion_pbi_sql,
    "check_utm_fuck_direct": _check_utm_direct_pbi_sql,
    "fact_ml_korrektirovki": _fact_ml_korrektirovki_pbi_sql,
    "yandex_direct_history": _direct_history_pbi_sql,
    "pixel_score": _pixel_score_pbi_sql,
    "yandex_direct_cookie_analytics_website_pages": _cookie_pages_pbi_sql,
    "yandex_direct_ads_texts": _direct_ads_texts_pbi_sql,
    "yandex_direct_type_placement_report_master": _direct_type_placement_pbi_sql,
    "direct_autorules_posevy_placement_links": _direct_autorules_posevy_placement_links_sql,
    "analytics_report_placement": _analytics_report_placement_pbi_sql,
    "yandex_direct_search_query_report_master": _search_query_report_master_pbi_sql,
}


def _pbi_view_select_sql(table: str) -> str:
    if table in DIRECT_SERVICE_BI_SELECTS:
        return DIRECT_SERVICE_BI_SELECTS[table]
    if builder := PBI_VIEW_SQL_BUILDERS.get(table):
        return builder()
    return f"SELECT * FROM ad_analytics.{q(table)}"


def create_bi_views(client) -> list[str]:
    views: list[str] = []
    for table in PBI_SOURCE_OBJECTS:
        view_name = f"bi_{table}"
        _replace_view(client, view_name, _pbi_view_select_sql(table))
        client.query(f"DESCRIBE TABLE ad_analytics.{q(view_name)}", settings=SAFE_QUERY_SETTINGS)
        views.append(view_name)
    return views


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    log.info("build_pbi_compat v6_ch")
    client = get_client()
    t0 = time.perf_counter()
    drop_bi_views(client)
    rows = {
        "pbi_big_analytics_full": build_pbi_full(client),
        "Dim_City_Tier": build_dim_city_tier(client),
        "pixel_score": build_pixel_score(client),
        "Dim_PlacementFeed": build_dim_placement_feed(client),
        "pbi_import_fact_direct_feed_funnel": build_pbi_import_direct_feed_funnel(client),
        "pbi_import_fact_direct_feed_funnel_star": build_pbi_import_direct_feed_funnel_star(client),
        "pbi_import_region_spend": build_pbi_import_region_spend(client),
        "Dim_Criterion": build_dim_criterion(client),
    }
    rows.update(create_light_aliases(client))
    bi_views = create_bi_views(client)
    details = ", ".join(f"{k}={v:,}" for k, v in rows.items())
    details = f"{details}, bi_views_created={len(bi_views)}"
    log.info("build_pbi_compat v6_ch завершён за %.1f сек: %s", time.perf_counter() - t0, details)
    return {"rows": sum(rows.values()), "details": details}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(run())
