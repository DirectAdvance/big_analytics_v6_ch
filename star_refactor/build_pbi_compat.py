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

log = logging.getLogger("build_pbi_compat")


def _site_key_expr(alias: str = "f") -> str:
    return f"cityHash64(lowerUTF8(trim(BOTH ' ' FROM ifNull({alias}.domain, ''))))"


PBI_SOURCE_OBJECTS = [
    "Dim_Account",
    "Dim_AdGroup",
    "Dim_AdFormat",
    "Dim_AdNetworkType",
    "Dim_Adjustment",
    "Dim_Campaign",
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
    # PLACEMENT_LINKS_BI_2026-08-17. Модель PBI читала ручную копию БА5
    # `raw_new_tp_placement_links` (снимок от 16.08, сам не обновляется) — из-за этого фикс
    # двойной кодировки площадок в отчёт не доезжал. Отчёт переведён на этот bi-слой над живой
    # таблицей шага 139; правило «PBI ходит только через bi_*» — DB_AD_ANALYTICS.md §1.3.
    "yandex_direct_tp_placement_links",
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
            multiIf(
                f.source_key = 'пиксель', 'Пиксель',
                f.source_key = 'pixel', 'Пиксель',
                dsl.`направление`
            ) AS `направление`,
            dcs.`тип_заявки`,
            dsl.`специалист`,
            dsl.`салон`,
            dsl.`город`,
            dsl.`регион`,
            dsl.`тип_сайта`,
            dsl.`шаблон`,
            dcs.`статус`,
            dsl.`проджект`,
            dsl.`менеджер`,
            dsl.`id_салона`,
            dcs.`Название crm`,
            toInt64(f.manager_login_key % 9223372036854775807) AS manager_login_key
        FROM ad_analytics.fact_big_analytics f
        LEFT JOIN ad_analytics.Dim_Account da ON da.account_key = f.account_key
        LEFT JOIN ad_analytics.Dim_CRMStatus dcs ON dcs.crm_status_key = f.crm_status_key
        LEFT JOIN ad_analytics.Dim_Salon dsl ON dsl.salon_key = f.salon_key
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
            toInt64(0) AS kval,
            toInt64(0) AS priezd,
            toInt64(round(f.crm_order_paid)) AS prodazhi,
            toInt64(0) AS dobro,
            toInt64(0) AS dohod_do_kredita,
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
            f.site_key,
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
            CAST(NULL, 'Nullable(Int64)') AS distance_km,
            dl.distance_km_agreg,
            toFloat64(f.cost) AS cost,
            toFloat64(f.clicks) AS clicks,
            toFloat64(f.impressions) AS impressions,
            toInt64(round(f.all_forms)) AS `Все формы`,
            toInt64(round(f.crm_order_created)) AS `CRM: Заказ создан`,
            toInt64(round(f.crm_order_paid)) AS `CRM: Заказ оплачен`,
            toInt64(round(f.crm_spam_order)) AS `CRM: Спам заказ`,
            toInt64(round(f.crm_order_canceled)) AS `CRM: Заказ отменен`,
            ds.domain AS domain,
            now() AS updated_at
        FROM ad_analytics.fact_region_spend f
        LEFT JOIN ad_analytics.Dim_Location dl ON dl.id_location = f.id_location
        LEFT JOIN ad_analytics.Dim_Site ds ON ds.site_key = f.site_key
        {where_sql}
    """


def _region_spend_star_pbi_sql(where_sql: str = "") -> str:
    return f"""
        SELECT
            f.date,
            f.campaign_id,
            f.ad_group_id,
            f.ad_network_type_key,
            f.id_location,
            f.site_key,
            toFloat64(f.cost) AS cost,
            toFloat64(f.clicks) AS clicks,
            toFloat64(f.impressions) AS impressions,
            toFloat64(f.all_forms) AS all_forms,
            toFloat64(f.crm_order_created) AS crm_order_created,
            toFloat64(f.crm_order_paid) AS crm_order_paid,
            toFloat64(f.crm_spam_order) AS crm_spam_order,
            toFloat64(f.crm_order_canceled) AS crm_order_canceled
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
            toInt64(0) AS `Все формы`,
            toInt64(0) AS `CRM: Заказ создан`,
            toInt64(0) AS `CRM: Заказ оплачен`,
            toInt64(0) AS `CRM: Спам заказ`,
            toInt64(0) AS `CRM: Заказ отменен`,
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
            toInt64(0) AS `Все формы`,
            toInt64(0) AS `CRM: Заказ создан`,
            toInt64(0) AS `CRM: Заказ оплачен`,
            toInt64(0) AS `CRM: Спам заказ`,
            toInt64(0) AS `CRM: Заказ отменен`,
            toInt64(0) AS kol_vo_zayavok,
            toInt64(0) AS korr,
            toInt64(0) AS nekorr,
            toInt64(0) AS kval,
            toInt64(0) AS priezd,
            toInt64(0) AS prodazhi,
            ds.domain AS domain,
            now() AS updated_at
        FROM ad_analytics.fact_criterion_spend f
        LEFT JOIN ad_analytics.Dim_Criterion dcr ON dcr.criterion_key = f.criterion_key
        LEFT JOIN ad_analytics.Dim_Site ds ON ds.site_key = f.site_key
        {where_sql}
    """


def _criterion_spend_star_pbi_sql(where_sql: str = "") -> str:
    return f"""
        SELECT
            f.date,
            f.campaign_id,
            f.ad_group_id,
            f.ad_network_type_key,
            f.criterion_key,
            f.site_key,
            toFloat64(f.cost) AS cost,
            toFloat64(f.clicks) AS clicks,
            toFloat64(f.impressions) AS impressions
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


def build_arf_fact(client) -> int:
    _replace_view(client, "arf_fact", "SELECT * FROM ad_analytics.pbi_import_fact_direct_feed_funnel")
    return count_rows(client, "ad_analytics.arf_fact")


def build_arc_fact(client) -> int:
    _replace_view(client, "arc_fact", _criterion_spend_pbi_sql())
    return count_rows(client, "ad_analytics.arc_fact")


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


def _arp_fact_sql(where_sql: str = "") -> str:
    return f"""
        SELECT
            f.date, f.domain AS `домен`, f.account_login AS `логин`, pf.ad_network_type, pf.placement,
            f.placement_feed_key AS placement_feed_key,
            pf.placement AS placement_key, toFloat64(f.cost) AS cost, toInt64(round(f.all_forms)) AS `Все формы`,
            toInt64(round(f.crm_order_created)) AS `CRM: Заказ создан`, toInt64(round(f.crm_order_paid)) AS `CRM: Заказ оплачен`,
            toInt64(0) AS `CRM: Спам заказ`, toInt64(0) AS `CRM: Заказ отменен`,
            dc.tp,
            ds.`специалист` AS `Специалист`, ds.`салон`, ds.`тип_сайта`,
            now() AS updated_at,
            toInt64(0) AS kol_vo_zayavok, toInt64(0) AS korr, toInt64(0) AS kval,
            toInt64(0) AS priezd, toInt64(0) AS prodazhi, toInt64(0) AS nekorr,
            toInt64(0) AS priedet,
            concat(toString(f.campaign_id), '|', ifNull(dc.CampaignName, '')) AS `номер кампании|название кампании`,
            f.campaign_id AS CampaignId, f.ad_group_id AS AdGroupId, toInt64(round(f.clicks)) AS clicks,
            toInt64(0) AS ne_otvechaet, toInt64(0) AS nedozvon, toInt64(0) AS filtr,
            toInt64(0) AS dohod_do_kredita, toInt64(0) AS dobro,
            f.date AS Date, f.domain AS domain, CAST(NULL, 'Nullable(String)') AS `тип_заявки`
        FROM ad_analytics.fact_direct_feed_funnel f
        LEFT JOIN ad_analytics.Dim_PlacementFeed pf ON pf.placement_feed_key = f.placement_feed_key
        LEFT JOIN ad_analytics.Dim_Campaign dc ON dc.CampaignId = f.campaign_id
        LEFT JOIN ad_analytics.Dim_Site ds ON ds.site_key = f.site_key
        {where_sql}
    """


def build_arp_fact(client) -> int:
    _replace_view(client, "arp_fact", _arp_fact_sql())
    return count_rows(client, "ad_analytics.arp_fact")


def create_light_aliases(client) -> dict[str, int]:
    statements = {
        "yandex_direct_korrektirovki": """
            WITH gs_login AS
            (
                SELECT
                    lower(ifNull(login_key, '')) AS login_key,
                    anyLast(directologist) AS directologist
                FROM raw_data.gsheet_sites
                WHERE ifNull(login_key, '') != ''
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


def _dim_ad_network_type_pbi_sql() -> str:
    return """
        SELECT
            ad_network_type_key,
            ad_network_type,
            AdNetworkType
        FROM ad_analytics.Dim_AdNetworkType
    """


def _dim_ad_format_pbi_sql() -> str:
    return """
        SELECT
            ad_format_key,
            anyLast(ad_format) AS ad_format
        FROM (
            SELECT
                lowerUTF8(trim(BOTH ' ' FROM ifNull(ad_format, ''))) AS ad_format_key,
                ifNull(ad_format, '') AS ad_format
            FROM ad_analytics.fact_adformat_spend
        )
        GROUP BY ad_format_key
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
    return """
        WITH service_campaigns AS (
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
                UNION ALL
                SELECT
                    campaign_id,
                    campaign_name,
                    login AS account_login,
                    CAST(NULL, 'Nullable(String)') AS campaign_status,
                    `специалист` AS specialist
                FROM ad_analytics.yandex_direct_minus_snapshot
                WHERE campaign_id != 0
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
            )
            GROUP BY CampaignId
        )
        SELECT
            dc.CampaignId,
            dc.CampaignName,
            dc.account_login,
            dc.campaign_code,
            dc.tp,
            dc.cpc_cpa,
            dc.site_quiz,
            dc.campaign_status AS `статус_кампании`,
            ss.specialist AS `специалист`,
            CAST(NULL, 'Nullable(String)') AS manager_login,
            dc.campaign_status,
            dc.payment_model,
            dc.payment_model AS `тип_оплаты`,
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
            '' AS cpc_cpa,
            '' AS site_quiz,
            ss.campaign_status AS `статус_кампании`,
            ss.specialist AS `специалист`,
            CAST(NULL, 'Nullable(String)') AS manager_login,
            ss.campaign_status,
            CAST(NULL, 'Nullable(String)') AS payment_model,
            CAST(NULL, 'Nullable(String)') AS `тип_оплаты`,
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
            adgroup_code,
            `номер группы | название группы`,
            `марки авто`,
            ag_part1,
            ag_part2,
            ag_part3,
            ag_part4,
            ag_part5,
            ag_part6,
            ag_part7,
            ag_part1 AS ag_part1_name,
            `неверный_кодер_new`,
            parent_CampaignId
        FROM ad_analytics.Dim_AdGroup
        UNION ALL
        SELECT
            sg.AdGroupId,
            CAST(NULL, 'Nullable(String)') AS AdGroupName,
            CAST(NULL, 'Nullable(String)') AS adgroup_code,
            '' AS `номер группы | название группы`,
            '' AS `марки авто`,
            '' AS ag_part1,
            '' AS ag_part2,
            '' AS ag_part3,
            '' AS ag_part4,
            '' AS ag_part5,
            '' AS ag_part6,
            '' AS ag_part7,
            '' AS ag_part1_name,
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


def _dim_site_pbi_sql() -> str:
    return """
        SELECT
            site_key, domain, `салон`, `город`, `регион`, `тип_сайта`, `шаблон`,
            `направление`, `статус`, status, `специалист`, `проджект`, project_manager,
            `id_салона`, `менеджер`, `Название crm`
        FROM ad_analytics.Dim_Site
    """


def _vk_ads_pbi_sql() -> str:
    return """
        SELECT
            f.date AS date,
            f.account_id AS account_id,
            f.`салон`,
            f.ad_plan_id AS ad_plan_id,
            f.ad_group_id AS ad_group_id,
            f.banner_id AS banner_id,
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
    """


def _dim_criterion_pbi_sql() -> str:
    return """
        SELECT criterion_key, criterion, criterion_type, criterion_raw
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
            banner_type AS ad_type,
            banner_status AS status,
            banner_title AS title,
            banner_body AS text,
            toInt64(sum(shows)) AS impressions,
            toInt64(sum(clicks)) AS clicks,
            sum(cost) AS cost,
            toInt64(round(sum(goals))) AS goal_all_forms,
            toInt64(0) AS goal_crm_order_paid
        FROM raw_data.direct_cookie_ads_texts_master
        GROUP BY loaded_at, client_login, campaign_id, ad_group_id, ad_type, status, title, text
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


PBI_VIEW_SQL_BUILDERS = {
    "fact_direct_feed_funnel": lambda: "SELECT * FROM ad_analytics.pbi_import_fact_direct_feed_funnel",
    "fact_direct_feed_funnel_star": lambda: "SELECT * FROM ad_analytics.pbi_import_fact_direct_feed_funnel_star",
    "Dim_Account": _dim_account_pbi_sql,
    "Dim_Date": _dim_date_pbi_sql,
    "Dim_AdFormat": _dim_ad_format_pbi_sql,
    "Dim_AdNetworkType": _dim_ad_network_type_pbi_sql,
    "Dim_Adjustment": _dim_adjustment_pbi_sql,
    "Dim_Device": _dim_device_pbi_sql,
    "Dim_ManagerLogin": _dim_manager_login_pbi_sql,
    "Dim_CRMStatus": _dim_crm_status_pbi_sql,
    "Dim_Salon": _dim_salon_pbi_sql,
    "Dim_Source": _dim_source_pbi_sql,
    "Dim_Campaign": _dim_campaign_pbi_sql,
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
}


def _pbi_view_select_sql(table: str) -> str:
    if table in DIRECT_SERVICE_BI_SELECTS:
        return DIRECT_SERVICE_BI_SELECTS[table]
    if builder := PBI_VIEW_SQL_BUILDERS.get(table):
        return builder()
    return f"SELECT * FROM ad_analytics.{q(table)}"


def create_bi_views(client) -> dict[str, int]:
    out: dict[str, int] = {}
    for table in PBI_SOURCE_OBJECTS:
        view_name = f"bi_{table}"
        _replace_view(client, view_name, _pbi_view_select_sql(table))
        out[view_name] = count_rows(client, f"ad_analytics.{q(view_name)}")
    return out


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    log.info("build_pbi_compat v6_ch")
    client = get_client()
    t0 = time.perf_counter()
    drop_bi_views(client)
    rows = {
        "pbi_big_analytics_full": build_pbi_full(client),
        "pixel_score": build_pixel_score(client),
        "Dim_PlacementFeed": build_dim_placement_feed(client),
        "pbi_import_fact_direct_feed_funnel": build_pbi_import_direct_feed_funnel(client),
        "pbi_import_fact_direct_feed_funnel_star": build_pbi_import_direct_feed_funnel_star(client),
        "pbi_import_region_spend": build_pbi_import_region_spend(client),
        "Dim_Criterion": build_dim_criterion(client),
    }
    rows.update(create_light_aliases(client))
    rows.update(create_bi_views(client))
    details = ", ".join(f"{k}={v:,}" for k, v in rows.items())
    log.info("build_pbi_compat v6_ch завершён за %.1f сек: %s", time.perf_counter() - t0, details)
    return {"rows": sum(rows.values()), "details": details}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(run())
