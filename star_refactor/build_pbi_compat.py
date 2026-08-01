"""Build compatibility tables expected by the existing Power BI semantic model."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_settings import DATE_FROM
from config.ch_utils import SAFE_QUERY_SETTINGS, count_rows, range_batches, q, swap_shadow, table_exists
from criterion_spend.cleaning import CRITERION_CLEAN
from spend.build_direct_spend_staging import STAGING_TABLE

log = logging.getLogger("build_pbi_compat")


def _site_key_expr(alias: str = "f") -> str:
    return f"cityHash64(lowerUTF8(trim(BOTH ' ' FROM ifNull({alias}.domain, ''))))"


PBI_SOURCE_OBJECTS = [
    "Dim_AdGroup",
    "Dim_Campaign",
    "Dim_Date",
    "Dim_Location",
    "Dim_PlacementFeed",
    "Dim_Site",
    "arc_fact",
    "arf_fact",
    "arp_fact",
    "pbi_big_analytics_full",
    "big_analytics_full_arrival",
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
    "yandex_direct_return_commission_report",
]


def _replace_view(client, name: str, select_sql: str) -> None:
    client.command(f"DROP TABLE IF EXISTS ad_analytics.{q(name)} SYNC")
    client.command(f"CREATE VIEW ad_analytics.{q(name)} AS {select_sql}")


def drop_bi_views(client) -> None:
    for table in PBI_SOURCE_OBJECTS:
        client.command(f"DROP TABLE IF EXISTS ad_analytics.{q(f'bi_{table}')} SYNC")


def _pbi_full_sql(where_sql: str = "") -> str:
    return f"""
        SELECT
            f.`Date` AS `Date`,
            f.`CampaignId` AS `CampaignId`,
            f.`AdNetworkType`,
            f.`Device`,
            toFloat64(f.total_cost) AS total_cost,
            dc.campaign_code,
            dc.tp,
            dc.cpc_cpa,
            if(lowerUTF8(ifNull(f.`источник`, '')) = 'пиксель', 0.0, toFloat64(f.kol_vo_zayavok)) AS `Обращения`,
            if(lowerUTF8(ifNull(f.`источник`, '')) = 'пиксель', 0.0, toFloat64(f.korr)) AS korr,
            if(lowerUTF8(ifNull(f.`источник`, '')) = 'пиксель', 0.0, toFloat64(f.kval)) AS kval,
            if(lowerUTF8(ifNull(f.`источник`, '')) = 'пиксель', 0.0, toFloat64(f.priezd)) AS priezd,
            if(lowerUTF8(ifNull(f.`источник`, '')) = 'пиксель', 0.0, toFloat64(f.prodazhi)) AS prodazhi,
            if(lowerUTF8(ifNull(f.`источник`, '')) = 'пиксель', 0, toInt64(round(f.nekorr))) AS nekorr,
            if(lowerUTF8(ifNull(f.`источник`, '')) = 'пиксель', 0, toInt64(round(f.ne_otvechaet))) AS ne_otvechaet,
            if(lowerUTF8(ifNull(f.`источник`, '')) = 'пиксель', 0, toInt64(round(f.filtr))) AS filtr,
            if(lowerUTF8(ifNull(f.`источник`, '')) = 'пиксель', 0, toInt64(round(f.nedozvon))) AS nedozvon,
            f.`RlAdjustmentId`,
            f.RlAdjustmentId_total,
            f.fid,
            -- PBI v00 historically treats the Clicks field as the v5 "Clicks" column,
            -- which contains Yandex impressions. Keep this compatibility alias here
            -- while the CH fact keeps physical Clicks/Impressions semantics.
            toFloat64(f.`Impressions`) AS `Clicks`,
            f.`источник`,
            f.`План заявки`,
            f.`План приезда`,
            concat(ifNull(f.account_login, ''), '|', ifNull(f.domain, '')) AS `аккаунт|сайт`,
            f.`поставщик`,
            f.domain AS `домен`,
            if(lowerUTF8(ifNull(f.`источник`, '')) = 'пиксель', 0, toInt64(round(f.priedet))) AS priedet,
            if(lowerUTF8(ifNull(f.`источник`, '')) = 'пиксель', 0, f.dohod_do_kredita) AS dohod_do_kredita,
            if(lowerUTF8(ifNull(f.`источник`, '')) = 'пиксель', 0, f.dobro) AS dobro,
            f.`атрибуция`,
            f.`AdGroupId` AS `AdGroupId`,
            multiIf(
                f.`источник` = 'Пиксель_атрибуц', 'пиксель_атрибуц',
                lowerUTF8(ifNull(f.`источник`, '')) = 'пиксель', 'пиксель',
                ds.`направление`
            ) AS `направление`,
            dc.site_quiz,
            dag.`марки авто`,
            ds.`специалист`,
            ds.`тип_сайта`,
            ds.`статус`,
            ds.status,
            ds.`салон`,
            ds.`шаблон`,
            ds.`id_салона`,
            ds.`город`,
            ds.`регион`,
            ds.`проджект`,
            ds.project_manager,
            ds.`менеджер`,
            ds.`Название crm`,
            f.`тип_заявки`,
            f.manager_login,
            dc.`CampaignName`,
            dag.`AdGroupName`,
            f.account_login AS account_login,
            dag.adgroup_code,
            dag.ag_part1,
            dag.ag_part2,
            dag.ag_part3,
            dag.ag_part4,
            dag.ag_part5,
            dag.ag_part6,
            dag.ag_part7,
            dc.campaign_status,
            dc.payment_model,
            f.`неверный_кодер_new`,
            dd.week_start,
            dd.`День недели`,
            dag.`номер группы | название группы`,
            dc.`номер кампании | название кампании`
        FROM ad_analytics.fact_big_analytics f
        LEFT JOIN ad_analytics.Dim_Date dd ON dd.`Date` = f.`Date`
        LEFT JOIN ad_analytics.Dim_Campaign dc ON dc.`CampaignId` = f.`CampaignId`
        LEFT JOIN ad_analytics.Dim_AdGroup dag ON dag.`AdGroupId` = f.`AdGroupId`
        LEFT JOIN ad_analytics.Dim_Site ds ON ds.site_key = {_site_key_expr("f")}
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
            `салон`,
            domain,
            `источник`,
            `CampaignId`,
            `CampaignName`,
            toFloat64(kol_vo_zayavok) AS kol_vo_zayavok,
            toFloat64(korr) AS korr,
            toFloat64(kval) AS kval,
            toFloat64(priezd) AS priezd,
            toFloat64(prodazhi) AS prodazhi,
            `направление`,
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
        FROM ad_analytics.big_analytics_pixel_score
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


def _feed_funnel_pbi_sql(where_sql: str = "") -> str:
    return f"""
        SELECT
            f.date,
            f.placement_feed_key AS placement_feed_key,
            f.campaign_id,
            f.ad_group_id AS adgroup_id,
            f.domain AS domain,
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
            'direct_feed_funnel' AS `источник`,
            now() AS generated_at
        FROM ad_analytics.fact_direct_feed_funnel f
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
    ranges = range_batches(DATE_FROM, days=1)
    for idx, (lo, hi) in enumerate(ranges, start=1):
        client.command(
            f"""
            INSERT INTO {shadow}
            {_feed_funnel_pbi_sql(f"WHERE f.date >= toDate('{lo}') AND f.date < toDate('{hi}')")}
            """,
            settings=SAFE_QUERY_SETTINGS,
        )
        log.info("  pbi_import_fact_direct_feed_funnel daily batch %d/%d: %s -> %s", idx, len(ranges), lo, hi)
    swap_shadow(client, "ad_analytics.pbi_import_fact_direct_feed_funnel", shadow)
    return count_rows(client, "ad_analytics.pbi_import_fact_direct_feed_funnel")


def _region_spend_pbi_sql(where_sql: str = "") -> str:
    return f"""
        SELECT
            toString(cityHash64(toString(f.date), f.campaign_id, ifNull(f.ad_group_id, 0), ifNull(f.ad_network_type, ''), ifNull(f.id_location, 0))) AS row_hash,
            f.date,
            f.campaign_id,
            dc.CampaignName AS campaign_name,
            f.ad_group_id,
            dag.AdGroupName AS ad_group_name,
            f.ad_network_type,
            f.id_location,
            f.location,
            f.`Область`,
            f.GeoRegionType,
            toInt64(ifNull(round(f.distance_km), 0)) AS distance_km,
            f.distance_km_agreg,
            toFloat64(f.cost) AS cost,
            toFloat64(f.clicks) AS clicks,
            toFloat64(f.impressions) AS impressions,
            toInt64(round(f.all_forms)) AS `Все формы`,
            toInt64(round(f.crm_order_created)) AS `CRM: Заказ создан`,
            toInt64(round(f.crm_order_paid)) AS `CRM: Заказ оплачен`,
            toInt64(round(f.crm_spam_order)) AS `CRM: Спам заказ`,
            toInt64(round(f.crm_order_canceled)) AS `CRM: Заказ отменен`,
            f.account_login AS `логин`,
            ds.domain AS domain,
            ds.`специалист` AS `директолог`,
            ds.`салон`,
            ds.`город`,
            ds.`регион`,
            ds.`тип_сайта`,
            ds.`шаблон`,
            ds.`статус`,
            ds.`направление` AS `направление`,
            ds.`проджект`,
            ds.project_manager AS project_manager,
            ds.`id_салона` AS client_id,
            dc.campaign_code AS campaign_code,
            dc.tp AS tp,
            dc.cpc_cpa AS cpc_cpa,
            dc.site_quiz AS site_quiz,
            now() AS updated_at
        FROM ad_analytics.fact_region_spend f
        LEFT JOIN ad_analytics.Dim_Campaign dc ON dc.CampaignId = f.campaign_id
        LEFT JOIN ad_analytics.Dim_AdGroup dag ON dag.AdGroupId = f.ad_group_id
        LEFT JOIN ad_analytics.Dim_Site ds ON ds.site_key = f.site_key
        {where_sql}
    """


def _adformat_spend_pbi_sql(where_sql: str = "") -> str:
    return f"""
        SELECT
            toString(cityHash64(toString(f.date), f.campaign_id, ifNull(f.ad_group_id, 0), ifNull(f.ad_network_type, ''), ifNull(f.ad_format, ''))) AS row_hash,
            f.date,
            f.campaign_id,
            dc.CampaignName AS campaign_name,
            f.ad_group_id,
            dag.AdGroupName AS ad_group_name,
            f.ad_network_type,
            f.ad_format,
            toFloat64(f.cost) AS cost,
            toFloat64(f.clicks) AS clicks,
            toFloat64(f.impressions) AS impressions,
            toInt64(0) AS `Все формы`,
            toInt64(0) AS `CRM: Заказ создан`,
            toInt64(0) AS `CRM: Заказ оплачен`,
            toInt64(0) AS `CRM: Спам заказ`,
            toInt64(0) AS `CRM: Заказ отменен`,
            f.account_login AS `логин`,
            ds.domain AS domain,
            ds.`специалист` AS `директолог`,
            ds.`салон`,
            ds.`город`,
            ds.`регион`,
            ds.`тип_сайта`,
            ds.`шаблон`,
            ds.`статус`,
            ds.`направление` AS `направление`,
            ds.`проджект`,
            ds.project_manager AS project_manager,
            ds.`id_салона` AS client_id,
            dc.campaign_code AS campaign_code,
            dc.tp AS tp,
            dc.cpc_cpa AS cpc_cpa,
            dc.site_quiz AS site_quiz,
            dag.adgroup_code AS adgroup_code,
            dag.`марки авто` AS adgroup_brand,
            now() AS updated_at
        FROM ad_analytics.fact_adformat_spend f
        LEFT JOIN ad_analytics.Dim_Campaign dc ON dc.CampaignId = f.campaign_id
        LEFT JOIN ad_analytics.Dim_AdGroup dag ON dag.AdGroupId = f.ad_group_id
        LEFT JOIN ad_analytics.Dim_Site ds ON ds.site_key = f.site_key
        {where_sql}
    """


def _criterion_spend_pbi_sql(where_sql: str = "") -> str:
    return f"""
        SELECT
            toString(cityHash64(toString(f.date), f.campaign_id, ifNull(f.ad_group_id, 0), ifNull(f.ad_network_type, ''), ifNull(f.criterion_id, 0), ifNull(dcr.criterion, ''))) AS row_hash,
            f.date,
            f.campaign_id,
            dc.CampaignName AS campaign_name,
            f.ad_group_id,
            dag.AdGroupName AS ad_group_name,
            f.ad_network_type,
            f.criterion_id,
            ifNull(dcr.criterion, '') AS criterion,
            dcr.criterion_raw,
            ifNull(dcr.criterion_type, '') AS criterion_type,
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
            f.account_login AS `логин`,
            ds.domain AS domain,
            ds.domain AS `домен`,
            ds.`специалист`,
            ds.`специалист` AS `директолог`,
            ds.`салон`,
            ds.`город`,
            ds.`регион`,
            ds.`тип_сайта`,
            ds.`шаблон`,
            ds.`статус`,
            ds.`направление`,
            ds.`проджект`,
            ds.project_manager AS project_manager,
            ds.`id_салона` AS client_id,
            dc.campaign_code AS campaign_code,
            dc.tp AS tp,
            dc.cpc_cpa AS cpc_cpa,
            dc.site_quiz AS site_quiz,
            dc.`номер кампании | название кампании`,
            CAST(NULL, 'Nullable(String)') AS `шаблон_марка`,
            now() AS updated_at
        FROM ad_analytics.fact_criterion_spend f
        LEFT JOIN ad_analytics.Dim_Campaign dc ON dc.CampaignId = f.campaign_id
        LEFT JOIN ad_analytics.Dim_AdGroup dag ON dag.AdGroupId = f.ad_group_id
        LEFT JOIN ad_analytics.dim_criterion dcr ON dcr.criterion_key = f.criterion_key
        LEFT JOIN ad_analytics.Dim_Site ds ON ds.site_key = f.site_key
        {where_sql}
    """


def _region_zayavki_pbi_sql() -> str:
    return """
        SELECT
            row_hash,
            created_date,
            campaign_id,
            campaign_name,
            id_location,
            location,
            `Область`,
            GeoRegionType,
            distance_km,
            distance_km_agreg,
            CAST(`салон`, 'Nullable(String)') AS `салон`,
            domain_id,
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
        FROM ad_analytics.fact_region_zayavki
    """


def _criterion_zayavki_pbi_sql() -> str:
    return """
        SELECT
            row_hash,
            created_date,
            campaign_id,
            campaign_name,
            criterion,
            CAST(criterion_type, 'Nullable(String)') AS criterion_type,
            criterion_raw,
            CAST(`салон`, 'Nullable(String)') AS `салон`,
            domain_id,
            CAST(`шаблон`, 'Nullable(String)') AS `шаблон`,
            CAST(`шаблон_марка`, 'Nullable(String)') AS `шаблон_марка`,
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
    shadow = "ad_analytics.dim_criterion_new"
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
    swap_shadow(client, "ad_analytics.dim_criterion", shadow)
    return count_rows(client, "ad_analytics.dim_criterion")


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
        LEFT JOIN ad_analytics.Dim_Site ds ON ds.site_key = {_site_key_expr("f")}
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
        "yandex_direct_return_commission_report": [
            ("id", "Nullable(Int64)"), ("client_login", "Nullable(String)"), ("date", "Nullable(Date)"),
            ("ad_network_type", "Nullable(String)"), ("slot", "Nullable(String)"),
            ("campaign_type", "Nullable(String)"), ("ad_type", "Nullable(String)"),
            ("cost", "Nullable(Decimal(18, 6))"), ("cost_with_vat", "Nullable(Decimal(18, 6))"),
            ("manager_login", "Nullable(String)"), ("user_login", "Nullable(String)"),
            ("rate", "Nullable(Decimal(18, 6))"), ("commission", "Nullable(Decimal(18, 6))"),
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


def create_bi_views(client) -> dict[str, int]:
    out: dict[str, int] = {}
    for table in PBI_SOURCE_OBJECTS:
        view_name = f"bi_{table}"
        if table == "fact_direct_feed_funnel":
            select_sql = "SELECT * FROM ad_analytics.pbi_import_fact_direct_feed_funnel"
        elif table == "Dim_Date":
            select_sql = """
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
        elif table == "Dim_Campaign":
            select_sql = """
                SELECT
                    CampaignId,
                    CampaignName,
                    account_login,
                    campaign_code,
                    tp,
                    cpc_cpa,
                    site_quiz,
                    campaign_status AS `статус_кампании`,
                    CAST(NULL, 'Nullable(String)') AS `специалист`,
                    CAST(NULL, 'Nullable(String)') AS manager_login,
                    campaign_status,
                    payment_model,
                    payment_model AS `тип_оплаты`,
                    `номер кампании | название кампании`
                FROM ad_analytics.Dim_Campaign
            """
        elif table == "Dim_AdGroup":
            select_sql = """
                SELECT
                    AdGroupId,
                    AdGroupName,
                    adgroup_code,
                    `номер группы | название группы`,
                    ag_part1,
                    ag_part2,
                    ag_part3,
                    ag_part4,
                    ag_part5,
                    ag_part6,
                    ag_part7,
                    ag_part1 AS ag_part1_name,
                    CAST(NULL, 'Nullable(String)') AS `неверный_кодер_new`,
                    CAST(NULL, 'Nullable(Int64)') AS parent_CampaignId
                FROM ad_analytics.Dim_AdGroup
            """
        elif table == "Dim_Site":
            select_sql = """
                SELECT
                    domain, `салон`, `город`, `регион`, `тип_сайта`, `шаблон`, `направление`,
                    `статус`, status, `специалист`, `проджект`, project_manager, `id_салона`,
                    `менеджер`, `Название crm`
                FROM ad_analytics.Dim_Site
            """
        elif table == "fact_region_spend":
            select_sql = "SELECT * FROM ad_analytics.pbi_import_region_spend"
        elif table == "fact_adformat_spend":
            select_sql = _adformat_spend_pbi_sql()
        elif table == "fact_criterion_spend":
            select_sql = _criterion_spend_pbi_sql()
        elif table == "fact_region_zayavki":
            select_sql = _region_zayavki_pbi_sql()
        elif table == "fact_criterion_zayavki":
            select_sql = _criterion_zayavki_pbi_sql()
        elif table == "dim_criterion":
            select_sql = """
                SELECT criterion, criterion_type, criterion_raw
                FROM ad_analytics.dim_criterion
            """
        elif table == "check_utm_fuck_direct":
            select_sql = """
                SELECT
                    id,
                    login,
                    CampaignId,
                    CampaignName,
                    group_id,
                    group_name,
                    tracking_params,
                    `домен`,
                    toFloat64(cost) AS cost,
                    `специалист`,
                    date,
                    utm_source_type
                FROM ad_analytics.check_utm_fuck_direct
            """
        elif table == "fact_ml_korrektirovki":
            select_sql = """
                SELECT
                    * REPLACE(
                        toFloat64(total_cost) AS total_cost,
                        toFloat64(kol_vo_zayavok) AS kol_vo_zayavok,
                        toFloat64(korr) AS korr,
                        toFloat64(kval) AS kval,
                        toFloat64(priezd) AS priezd,
                        toFloat64(prodazhi) AS prodazhi,
                        toFloat64(Clicks) AS Clicks,
                        toFloat64(Impressions) AS Impressions
                    )
                FROM ad_analytics.fact_ml_korrektirovki
            """
        elif table == "big_analytics_full_arrival":
            select_sql = """
                SELECT
                    * REPLACE(
                        toFloat64(Impressions) AS Impressions,
                        toFloat64(Clicks) AS Clicks,
                        toFloat64(total_cost) AS total_cost,
                        toFloat64(kol_vo_zayavok) AS kol_vo_zayavok,
                        toFloat64(korr) AS korr,
                        toFloat64(kval) AS kval,
                        toFloat64(priezd) AS priezd,
                        toFloat64(prodazhi) AS prodazhi,
                        toFloat64(nekorr) AS nekorr,
                        toFloat64(ne_otvechaet) AS ne_otvechaet,
                        toFloat64(filtr) AS filtr,
                        toFloat64(nedozvon) AS nedozvon,
                        toFloat64(priedet) AS priedet
                    ),
                    ag_part1 AS ag_part1_name
                FROM ad_analytics.big_analytics_full_arrival
            """
        elif table == "yandex_direct_history":
            select_sql = """
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
                    campaign_name,
                    CAST(NULL, 'Nullable(Int64)') AS ad_group_id,
                    CAST(NULL, 'Nullable(String)') AS ad_group_name,
                    old_value,
                    new_value,
                    CAST(NULL, 'Nullable(String)') AS raw_event,
                    `директолог`,
                    domain,
                    salon,
                    updated_at AS loaded_at
                FROM ad_analytics.yandex_direct_history
            """
        elif table == "yandex_direct_minus_snapshot":
            select_sql = """
                SELECT
                    * REPLACE(toInt64(id % 9223372036854775807) AS id)
                FROM ad_analytics.yandex_direct_minus_snapshot
            """
        elif table == "yandex_direct_cookie_analytics_website_pages":
            select_sql = """
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
        else:
            select_sql = f"SELECT * FROM ad_analytics.{q(table)}"
        _replace_view(client, view_name, select_sql)
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
        "pbi_import_region_spend": build_pbi_import_region_spend(client),
        "arp_fact": build_arp_fact(client),
        "arf_fact": build_arf_fact(client),
        "dim_criterion": build_dim_criterion(client),
        "arc_fact": build_arc_fact(client),
    }
    rows.update(create_light_aliases(client))
    rows.update(create_bi_views(client))
    details = ", ".join(f"{k}={v:,}" for k, v in rows.items())
    log.info("build_pbi_compat v6_ch завершён за %.1f сек: %s", time.perf_counter() - t0, details)
    return {"rows": sum(rows.values()), "details": details}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(run())
