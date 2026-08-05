"""Build ClickHouse star/Power BI tables for v6_ch."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_settings import DATE_FROM, VK_AUTO_ACCOUNTS_SQL
from config.ch_utils import SAFE_QUERY_SETTINGS, column_names, count_rows, day_ranges, q, range_batches, swap_shadow, table_exists
from step3_build_sources.step3 import _metric_expr

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("build_star")

FACT_BIG_DIMENSION_COLUMNS = {
    "key3",
    "День недели",
    "week_start",
    "CampaignName",
    "AdGroupName",
    "RlAdjustmentId_total",
    "campaign_code",
    "tp",
    "cpc_cpa",
    "site_quiz",
    "adgroup_code",
    "ag_part1",
    "ag_part2",
    "ag_part3",
    "ag_part4",
    "ag_part5",
    "ag_part6",
    "ag_part7",
    "марки авто",
    "номер кампании | название кампании",
    "номер группы | название группы",
    "аккаунт|сайт",
    "campaign_status",
    "payment_model",
    "key_pixel_score",
    "неверный_кодер_new",
    "status",
    "project_manager",
    "manager_login",
    "AdNetworkType",
    "Device",
    "источник",
    "поставщик",
}


def _create_fact_empty(client, target: str, select_sql: str) -> None:
    client.command(f"DROP TABLE IF EXISTS {target} SYNC")
    client.command(
        f"""
        CREATE TABLE {target}
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(ifNull(`Date`, toDate('2026-01-01')))
        ORDER BY (ifNull(`Date`, toDate('2026-01-01')), `атрибуция`, ifNull(domain, ''))
        AS SELECT {select_sql}
        FROM ad_analytics.big_analytics_unified
        WHERE 0
        """,
        settings=SAFE_QUERY_SETTINGS,
    )


def build_fact(client) -> int:
    source_cols = column_names(client, "ad_analytics", "big_analytics_unified")
    cols = [
        col
        for col in source_cols
        if col not in FACT_BIG_DIMENSION_COLUMNS
    ]
    alias_exprs = []
    if "AdNetworkType" in source_cols:
        alias_exprs.append(
            "lowerUTF8(trim(BOTH ' ' FROM ifNull(`AdNetworkType`, ''))) AS ad_network_type_key"
        )
    if "Device" in source_cols:
        alias_exprs.append(
            "lowerUTF8(trim(BOTH ' ' FROM ifNull(`Device`, ''))) AS device_key"
        )
    if "источник" in source_cols:
        alias_exprs.append(
            "lowerUTF8(trim(BOTH ' ' FROM ifNull(`источник`, ''))) AS source_key"
        )
    if "manager_login" in source_cols:
        alias_exprs.append(
            "if(notEmpty(lowerUTF8(trim(BOTH ' ' FROM ifNull(manager_login, '')))), "
            "cityHash64(lowerUTF8(trim(BOTH ' ' FROM ifNull(manager_login, '')))), toUInt64(0)) "
            "AS manager_login_key"
        )
    target_cols = cols + [expr.rsplit(" AS ", 1)[1] for expr in alias_exprs]
    select_sql = ", ".join([q(col) for col in cols] + alias_exprs)
    cols_sql = ", ".join(q(col) for col in target_cols)
    shadow = "ad_analytics.fact_big_analytics_new"
    _create_fact_empty(client, shadow, select_sql)
    ranges = day_ranges(DATE_FROM)
    for idx, (lo, hi) in enumerate(ranges, start=1):
        client.command(
            f"""
            INSERT INTO {shadow} ({cols_sql})
            SELECT {select_sql}
            FROM ad_analytics.big_analytics_unified
            WHERE `Date` >= toDate('{lo}') AND `Date` < toDate('{hi}')
            """,
            settings=SAFE_QUERY_SETTINGS,
        )
        log.info("  fact daily batch %d/%d: %s -> %s", idx, len(ranges), lo, hi)
    swap_shadow(client, "ad_analytics.fact_big_analytics", shadow)
    return count_rows(client, "ad_analytics.fact_big_analytics")


def build_dims(client) -> dict[str, int]:
    ddl = {
        "Dim_Date": """
            CREATE TABLE ad_analytics.Dim_Date_new
            ENGINE = MergeTree
            ORDER BY Date
            AS
            SELECT DISTINCT
                `Date`,
                `День недели`,
                week_start,
                toYear(`Date`) AS year,
                toMonth(`Date`) AS month,
                toYYYYMM(`Date`) AS month_key,
                formatDateTime(`Date`, '%Y-%m') AS year_month,
                toDayOfMonth(`Date`) AS day
            FROM ad_analytics.big_analytics_unified
            WHERE `Date` IS NOT NULL
        """,
        "Dim_Site": """
            CREATE TABLE ad_analytics.Dim_Site_new
            ENGINE = MergeTree
            ORDER BY site_key
            AS
            SELECT
                site_key,
                domain,
                salon AS `салон`,
                city AS `город`,
                region AS `регион`,
                site_type AS `тип_сайта`,
                template AS `шаблон`,
                direction AS `направление`,
                site_status AS `статус`,
                site_status AS status,
                specialist AS `специалист`,
                project AS `проджект`,
                project AS project_manager,
                salon_id AS `id_салона`,
                manager AS `менеджер`,
                CAST(ifNull(crm_name, ''), 'String') AS `Название crm`
            FROM
            (
                SELECT
                    site_key,
                    argMax(domain, sort_weight) AS domain,
                    argMax(salon, sort_weight) AS salon,
                    argMax(city, sort_weight) AS city,
                    argMax(region, sort_weight) AS region,
                    argMax(site_type, sort_weight) AS site_type,
                    argMax(template, sort_weight) AS template,
                    argMax(direction, sort_weight) AS direction,
                    argMax(site_status, sort_weight) AS site_status,
                    argMax(specialist, sort_weight) AS specialist,
                    argMax(project, sort_weight) AS project,
                    argMax(salon_id, sort_weight) AS salon_id,
                    argMax(manager, sort_weight) AS manager,
                    argMax(crm_name, sort_weight) AS crm_name
                FROM
                (
                    SELECT
                        cityHash64(lowerUTF8(trim(BOTH ' ' FROM ifNull(domain, '')))) AS site_key,
                        domain,
                        `салон` AS salon,
                        `город` AS city,
                        `регион` AS region,
                        `тип_сайта` AS site_type,
                        `шаблон` AS template,
                        ifNull(`направление`, '') AS direction,
                        `статус` AS site_status,
                        `специалист` AS specialist,
                        `проджект` AS project,
                        `id_салона` AS salon_id,
                        `менеджер` AS manager,
                        ifNull(`Название crm`, '') AS crm_name,
                        tuple(toUInt8(2), notEmpty(ifNull(`салон`, '')), lengthUTF8(ifNull(domain, ''))) AS sort_weight
                    FROM ad_analytics.big_analytics_unified
                    WHERE ifNull(domain, '') != ''

                    UNION ALL

                    SELECT
                        cityHash64(lowerUTF8(trim(BOTH ' ' FROM ifNull(domain, '')))) AS site_key,
                        domain,
                        salon,
                        city,
                        region,
                        site_type,
                        template,
                        ifNull(direction, '') AS direction,
                        status AS site_status,
                        directologist AS specialist,
                        project_manager AS project,
                        client_id AS salon_id,
                        sales_manager AS manager,
                        ifNull(crm, '') AS crm_name,
                        tuple(toUInt8(1), notEmpty(ifNull(salon, '')), lengthUTF8(ifNull(domain, ''))) AS sort_weight
                    FROM raw_data.gsheet_sites
                    WHERE ifNull(domain, '') != ''
                )
                GROUP BY site_key
            )
        """,
        "Dim_Campaign": """
            CREATE TABLE ad_analytics.Dim_Campaign_new
            ENGINE = MergeTree
            ORDER BY ifNull(CampaignId, 0)
            AS
            SELECT
                `CampaignId`,
                anyLast(`CampaignName`) AS `CampaignName`,
                anyLast(account_login) AS account_login,
                anyLast(campaign_code) AS campaign_code,
                anyLast(tp) AS tp,
                anyLast(cpc_cpa) AS cpc_cpa,
                anyLast(site_quiz) AS site_quiz,
                anyLast(campaign_status) AS campaign_status,
                anyLast(payment_model) AS payment_model,
                anyLast(`номер кампании | название кампании`) AS `номер кампании | название кампании`
            FROM ad_analytics.big_analytics_unified
            WHERE `CampaignId` IS NOT NULL
            GROUP BY `CampaignId`
        """,
        "Dim_AdGroup": """
            CREATE TABLE ad_analytics.Dim_AdGroup_new
            ENGINE = MergeTree
            ORDER BY ifNull(AdGroupId, 0)
            AS
            SELECT
                `AdGroupId`,
                anyLast(`AdGroupName`) AS `AdGroupName`,
                anyLast(adgroup_code) AS adgroup_code,
                anyLast(`марки авто`) AS `марки авто`,
                anyLast(ag_part1) AS ag_part1,
                anyLast(ag_part2) AS ag_part2,
                anyLast(ag_part3) AS ag_part3,
                anyLast(ag_part4) AS ag_part4,
                anyLast(ag_part5) AS ag_part5,
                anyLast(ag_part6) AS ag_part6,
                anyLast(ag_part7) AS ag_part7,
                anyLast(`номер группы | название группы`) AS `номер группы | название группы`,
                anyLast(`неверный_кодер_new`) AS `неверный_кодер_new`,
                anyLast(`CampaignId`) AS parent_CampaignId
            FROM ad_analytics.big_analytics_unified
            WHERE `AdGroupId` IS NOT NULL
            GROUP BY `AdGroupId`
        """,
        "Dim_Adjustment": """
            CREATE TABLE ad_analytics.Dim_Adjustment_new
            ENGINE = MergeTree
            ORDER BY RlAdjustmentId
            AS
            SELECT
                `RlAdjustmentId`,
                anyLast(`RlAdjustmentId_total`) AS `RlAdjustmentId_total`
            FROM ad_analytics.big_analytics_unified
            WHERE `RlAdjustmentId` IS NOT NULL
            GROUP BY `RlAdjustmentId`
        """,
        "Dim_Location": """
            CREATE TABLE ad_analytics.Dim_Location_new
            ENGINE = MergeTree
            ORDER BY id_location
            AS
            WITH locations AS
            (
                SELECT assumeNotNull(id_location) AS id_location
                FROM ad_analytics.fact_region_spend
                WHERE id_location IS NOT NULL

                UNION DISTINCT

                SELECT assumeNotNull(id_location) AS id_location
                FROM ad_analytics.fact_region_zayavki
                WHERE id_location IS NOT NULL
            )
            SELECT
                id_location,
                '' AS location,
                '' AS `Область`,
                CAST(NULL, 'LowCardinality(Nullable(String))') AS GeoRegionType,
                CAST(NULL, 'Nullable(Int32)') AS distance_km_agreg
            FROM locations
            GROUP BY id_location
        """,
        "Dim_ManagerLogin": """
            CREATE TABLE ad_analytics.Dim_ManagerLogin_new
            ENGINE = MergeTree
            ORDER BY manager_login_key
            AS
            SELECT
                manager_login_key,
                anyLast(manager_login) AS manager_login
            FROM
            (
                SELECT
                    if(notEmpty(lowerUTF8(trim(BOTH ' ' FROM ifNull(manager_login, '')))),
                        cityHash64(lowerUTF8(trim(BOTH ' ' FROM ifNull(manager_login, '')))),
                        toUInt64(0)
                    ) AS manager_login_key,
                    ifNull(manager_login, '') AS manager_login
                FROM ad_analytics.big_analytics_unified
            )
            GROUP BY manager_login_key
        """,
    }
    rows: dict[str, int] = {}
    for table, sql in ddl.items():
        shadow = f"ad_analytics.{table}_new"
        client.command(f"DROP TABLE IF EXISTS {shadow} SYNC")
        client.command(sql, settings=SAFE_QUERY_SETTINGS)
        swap_shadow(client, f"ad_analytics.{table}", shadow)
        rows[table] = count_rows(client, f"ad_analytics.{table}")
        log.info("  %s=%d", table, rows[table])
    return rows


def _ml_korrektirovki_sql(where_sql: str) -> str:
    return f"""
        WITH ml_korr AS
        (
            SELECT
                k.audience_id AS audience_id,
                anyLast(k.modifier_name) AS modifier_name,
                anyLast(k.bid_percent) AS bid_percent,
                anyLast(k.korrektirovki_bid) AS korrektirovki_bid
            FROM raw_data.yandex_direct_korrektirovki AS k
            WHERE positionCaseInsensitive(ifNull(k.korrektirovki_bid, ''), '_ml_') > 0
              AND k.audience_id IS NOT NULL
            GROUP BY k.audience_id
        )
        SELECT
            f.`CampaignId`,
            f.`AdGroupId`,
            f.`RlAdjustmentId`,
            f.priezd_arrival_date,
            f.prodazhi_arrival_date,
            f.dohod_do_kredita,
            f.dobro,
            toDecimal64(f.total_cost, 2) AS total_cost,
            f.kol_vo_zayavok,
            f.korr,
            f.kval,
            f.priezd,
            f.prodazhi,
            f.`Clicks`,
            f.`Impressions`,
            toInt32(f.nekorr) AS nekorr,
            toInt32(f.ne_otvechaet) AS ne_otvechaet,
            toInt32(f.nedozvon) AS nedozvon,
            toInt32(f.filtr) AS filtr,
            toInt32(f.priedet) AS priedet,
            f.`План заявки`,
            f.`План приезда`,
            f.`Date`,
            f.domain,
            f.`атрибуция`,
            f._source_table,
            f.tp,
            f.`источник`,
            f.`AdNetworkType`,
            f.`аккаунт|сайт`,
            f.campaign_code,
            f.`поставщик`,
            f.`Device`,
            f.fid,
            f.cpc_cpa,
            f.`направление`,
            f.site_quiz,
            f.`марки авто`,
            f.`специалист`,
            f.`тип_сайта`,
            f.`статус`,
            f.`салон`,
            f.`шаблон`,
            f.`id_салона`,
            f.`город`,
            f.`регион`,
            f.`проджект`,
            f.`менеджер`,
            f.`Название crm`,
            f.`тип_заявки`,
            f.manager_login,
            k.modifier_name AS ml_audience_name,
            k.bid_percent,
            lower(extract(ifNull(k.modifier_name, ''), '_ml_all_(\\\\d+p(?:_[a-z0-9]+)?)')) AS ml_tier
        FROM ad_analytics.big_analytics_unified f
        INNER JOIN ml_korr k ON f.`RlAdjustmentId` = k.audience_id
        {where_sql}
    """


def build_ml_korrektirovki_fact(client) -> int:
    shadow = "ad_analytics.fact_ml_korrektirovki_new"
    client.command(f"DROP TABLE IF EXISTS {shadow} SYNC")
    client.command(
        f"""
        CREATE TABLE {shadow}
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(ifNull(`Date`, toDate('2026-01-01')))
        ORDER BY (ifNull(`Date`, toDate('2026-01-01')), ifNull(`RlAdjustmentId`, 0), ifNull(domain, ''))
        AS
        {_ml_korrektirovki_sql("WHERE 0")}
        """,
        settings=SAFE_QUERY_SETTINGS,
    )
    ranges = range_batches(DATE_FROM, days=7)
    for idx, (lo, hi) in enumerate(ranges, start=1):
        client.command(
            f"""
            INSERT INTO {shadow}
            {_ml_korrektirovki_sql(f"WHERE f.`Date` >= toDate('{lo}') AND f.`Date` < toDate('{hi}')")}
            """,
            settings=SAFE_QUERY_SETTINGS,
        )
        log.info("  fact_ml_korrektirovki weekly batch %d/%d: %s -> %s", idx, len(ranges), lo, hi)
    swap_shadow(client, "ad_analytics.fact_ml_korrektirovki", shadow)
    rows = count_rows(client, "ad_analytics.fact_ml_korrektirovki")
    log.info("  fact_ml_korrektirovki=%d", rows)
    return rows


def _vk_ads_sql(metrics: str, stats_where_sql: str, lead_source_where_sql: str, zayavka_where_sql: str, visit_where_sql: str) -> str:
    return f"""
        WITH
        vk_leads AS
        (
            SELECT
                created_date,
                arrival_date,
                toInt64OrNull(extract(ifNull(utm_content, ''), '^([0-9]{{5,}})/')) AS ad_group_id,
                toInt64OrNull(extract(ifNull(utm_content, ''), '/([0-9]{{5,}})$')) AS banner_id,
                status,
                reason,
                source_type,
                salon
            FROM ad_analytics.raw_leads
            WHERE lower(ifNull(utm_source, '')) = 'vkads'
              AND is_copy_for_removal = 0
              {lead_source_where_sql}
        ),
        lead_metrics AS
        (
            SELECT
                created_date,
                arrival_date,
                ad_group_id,
                banner_id,
                salon,
                {metrics}
            FROM vk_leads
        ),
        zayavka_agg AS
        (
            SELECT
                created_date AS date,
                ad_group_id,
                banner_id,
                anyLast(salon) AS `салон`,
                toInt64(sum(kol_vo_zayavok)) AS `заявки`,
                toInt64(sum(korr)) AS `заявки_корр`,
                toInt64(sum(priedet)) AS `записи`,
                toInt64(sum(kval)) AS `квал`,
                toInt64(sum(priezd)) AS `визиты`,
                toInt64(sum(prodazhi)) AS `продажи`
            FROM lead_metrics
            WHERE created_date IS NOT NULL
              {zayavka_where_sql}
            GROUP BY date, ad_group_id, banner_id
        ),
        visit_agg AS
        (
            SELECT
                arrival_date AS date,
                ad_group_id,
                banner_id,
                anyLast(salon) AS `салон`,
                toInt64(sum(kol_vo_zayavok)) AS `заявки`,
                toInt64(sum(korr)) AS `заявки_корр`,
                toInt64(sum(priedet)) AS `записи`,
                toInt64(sum(kval)) AS `квал`,
                toInt64(sum(priezd)) AS `визиты`,
                toInt64(sum(prodazhi)) AS `продажи`
            FROM lead_metrics
            WHERE arrival_date IS NOT NULL
              {visit_where_sql}
            GROUP BY date, ad_group_id, banner_id
        ),
        banner_dim AS
        (
            -- VK_AUTO_ACCOUNT_SCOPE_2026-08-05: только свои Авто-аккаунты — зеркало v5,
            -- где banner_dim читал уже суженный `public.local_vk_ads_stats_day`
            -- (`work/big_analytics_v5/star_refactor/build_star.py:1323-1330`).
            SELECT
                b.banner_id AS banner_id,
                anyLast(b.account_id) AS account_id,
                anyLast(b.ad_plan_id) AS ad_plan_id,
                anyLast(b.ad_group_id) AS ad_group_id
            FROM raw_data.vk_ads_stats_day AS b
            WHERE b.banner_id IS NOT NULL
              AND b.account_id IN ({VK_AUTO_ACCOUNTS_SQL})
            GROUP BY b.banner_id
        ),
        salon_dim AS
        (
            SELECT
                lower(trim(ifNull(salon, ''))) AS salon_key,
                anyLast(region) AS `регион`,
                anyLast(site_type) AS `тип_сайта`,
                anyLast(directologist) AS `специалист`
            FROM raw_data.gsheet_sites
            WHERE ifNull(salon, '') != ''
            GROUP BY salon_key
        )
        SELECT
            assumeNotNull(toDateOrNull(s.date)) AS date,
            CAST(s.account_id, 'Nullable(Int64)') AS account_id,
            CAST(NULL, 'LowCardinality(Nullable(String))') AS `салон`,
            CAST(s.ad_plan_id, 'Nullable(Int64)') AS ad_plan_id,
            CAST(s.ad_group_id, 'Nullable(Int64)') AS ad_group_id,
            CAST(s.banner_id, 'Nullable(Int64)') AS banner_id,
            'По дате заявки' AS `атрибуция`,
            toInt64(ifNull(s.shows, 0)) AS shows,
            toInt64(ifNull(s.clicks, 0)) AS clicks,
            toDecimal64(ifNull(s.spent, 0), 2) AS spent,
            toInt64(0) AS `заявки`,
            toInt64(0) AS `заявки_корр`,
            toInt64(0) AS `записи`,
            toInt64(0) AS `квал`,
            toInt64(0) AS `визиты`,
            toInt64(0) AS `продажи`,
            CAST(NULL, 'LowCardinality(Nullable(String))') AS `регион`,
            CAST(NULL, 'LowCardinality(Nullable(String))') AS `тип_сайта`,
            CAST(NULL, 'LowCardinality(Nullable(String))') AS `специалист`
        FROM raw_data.vk_ads_stats_day s
        WHERE toDateOrNull(s.date) >= toDate('{DATE_FROM}')
          {stats_where_sql}
          AND (ifNull(s.shows, 0) != 0 OR ifNull(s.clicks, 0) != 0 OR ifNull(s.spent, 0) != 0)
          -- VK_AUTO_ACCOUNT_SCOPE_2026-08-05: рекламная сторона — только свои Авто-аккаунты.
          AND s.account_id IN ({VK_AUTO_ACCOUNTS_SQL})

        UNION ALL

        SELECT
            assumeNotNull(za.date) AS date,
            CAST(bd.account_id, 'Nullable(Int64)') AS account_id,
            CAST(za.`салон`, 'LowCardinality(Nullable(String))') AS `салон`,
            CAST(bd.ad_plan_id, 'Nullable(Int64)') AS ad_plan_id,
            CAST(ifNull(za.ad_group_id, bd.ad_group_id), 'Nullable(Int64)') AS ad_group_id,
            CAST(za.banner_id, 'Nullable(Int64)') AS banner_id,
            'По дате заявки' AS `атрибуция`,
            toInt64(0) AS shows,
            toInt64(0) AS clicks,
            toDecimal64(0, 2) AS spent,
            za.`заявки`,
            za.`заявки_корр`,
            za.`записи`,
            za.`квал`,
            za.`визиты`,
            za.`продажи`,
            CAST(sd.`регион`, 'LowCardinality(Nullable(String))') AS `регион`,
            CAST(sd.`тип_сайта`, 'LowCardinality(Nullable(String))') AS `тип_сайта`,
            CAST(sd.`специалист`, 'LowCardinality(Nullable(String))') AS `специалист`
        FROM zayavka_agg za
        LEFT JOIN banner_dim bd ON bd.banner_id = za.banner_id
        LEFT JOIN salon_dim sd ON sd.salon_key = lower(trim(ifNull(za.`салон`, '')))

        UNION ALL

        SELECT
            assumeNotNull(va.date) AS date,
            CAST(bd.account_id, 'Nullable(Int64)') AS account_id,
            CAST(va.`салон`, 'LowCardinality(Nullable(String))') AS `салон`,
            CAST(bd.ad_plan_id, 'Nullable(Int64)') AS ad_plan_id,
            CAST(ifNull(va.ad_group_id, bd.ad_group_id), 'Nullable(Int64)') AS ad_group_id,
            CAST(va.banner_id, 'Nullable(Int64)') AS banner_id,
            'По дате визита' AS `атрибуция`,
            toInt64(0) AS shows,
            toInt64(0) AS clicks,
            toDecimal64(0, 2) AS spent,
            va.`заявки`,
            va.`заявки_корр`,
            va.`записи`,
            va.`квал`,
            va.`визиты`,
            va.`продажи`,
            CAST(sd.`регион`, 'LowCardinality(Nullable(String))') AS `регион`,
            CAST(sd.`тип_сайта`, 'LowCardinality(Nullable(String))') AS `тип_сайта`,
            CAST(sd.`специалист`, 'LowCardinality(Nullable(String))') AS `специалист`
        FROM visit_agg va
        LEFT JOIN banner_dim bd ON bd.banner_id = va.banner_id
        LEFT JOIN salon_dim sd ON sd.salon_key = lower(trim(ifNull(va.`салон`, '')))
    """


def build_vk_ads_fact(client) -> int:
    shadow = "ad_analytics.fact_vk_ads_new"
    metrics = _metric_expr("status", "reason", "source_type", "salon")
    client.command(f"DROP TABLE IF EXISTS {shadow} SYNC")
    client.command(
        f"""
        CREATE TABLE {shadow}
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(date)
        ORDER BY (date, ifNull(account_id, 0), ifNull(ad_plan_id, 0), ifNull(ad_group_id, 0), ifNull(banner_id, 0), `атрибуция`)
        AS
        {_vk_ads_sql(metrics, "AND 0", "AND 0", "AND 0", "AND 0")}
        """,
        settings=SAFE_QUERY_SETTINGS,
    )
    ranges = range_batches(DATE_FROM, days=7)
    for idx, (lo, hi) in enumerate(ranges, start=1):
        client.command(
            f"""
            INSERT INTO {shadow}
            {_vk_ads_sql(
                metrics,
                f"AND toDateOrNull(s.date) >= toDate('{lo}') AND toDateOrNull(s.date) < toDate('{hi}')",
                (
                    f"AND ((created_date >= toDate('{lo}') AND created_date < toDate('{hi}')) "
                    f"OR (arrival_date >= toDate('{lo}') AND arrival_date < toDate('{hi}')))"
                ),
                f"AND created_date >= toDate('{lo}') AND created_date < toDate('{hi}')",
                f"AND arrival_date >= toDate('{lo}') AND arrival_date < toDate('{hi}')",
            )}
            """,
            settings=SAFE_QUERY_SETTINGS,
        )
        log.info("  fact_vk_ads weekly batch %d/%d: %s -> %s", idx, len(ranges), lo, hi)
    swap_shadow(client, "ad_analytics.fact_vk_ads", shadow)
    rows = count_rows(client, "ad_analytics.fact_vk_ads")
    log.info("  fact_vk_ads=%d", rows)
    return rows


def build_vk_dims(client) -> dict[str, int]:
    # VK_AUTO_ACCOUNT_SCOPE_2026-08-05: измерения строятся над тем же скоупом, что и
    # fact_vk_ads — иначе в Dim_* попадали кампании/группы/объявления 86 чужих агентских
    # клиентов (медцентры, недвижимость, юристы), у которых нет ни одной строки факта.
    ddl = {
        "Dim_VkAdPlan": f"""
            CREATE TABLE ad_analytics.Dim_VkAdPlan_new
            ENGINE = MergeTree
            ORDER BY ifNull(ad_plan_id, 0)
            AS
            SELECT
                CAST(s.ad_plan_id, 'Nullable(Int64)') AS ad_plan_id,
                anyLast(s.ad_plan_name) AS ad_plan_name,
                CAST(anyLast(s.account_id), 'Nullable(Int64)') AS account_id
            FROM raw_data.vk_ads_stats_day AS s
            WHERE s.ad_plan_id IS NOT NULL
              AND s.account_id IN ({VK_AUTO_ACCOUNTS_SQL})
            GROUP BY s.ad_plan_id
        """,
        "Dim_VkAdGroup": f"""
            CREATE TABLE ad_analytics.Dim_VkAdGroup_new
            ENGINE = MergeTree
            ORDER BY ifNull(ad_group_id, 0)
            AS
            SELECT
                CAST(s.ad_group_id, 'Nullable(Int64)') AS ad_group_id,
                anyLast(s.ad_group_name) AS ad_group_name,
                CAST(anyLast(s.ad_plan_id), 'Nullable(Int64)') AS ad_plan_id
            FROM raw_data.vk_ads_stats_day AS s
            WHERE s.ad_group_id IS NOT NULL
              AND s.account_id IN ({VK_AUTO_ACCOUNTS_SQL})
            GROUP BY s.ad_group_id
        """,
        "Dim_VkBanner": f"""
            CREATE TABLE ad_analytics.Dim_VkBanner_new
            ENGINE = MergeTree
            ORDER BY ifNull(banner_id, 0)
            AS
            SELECT
                CAST(s.banner_id, 'Nullable(Int64)') AS banner_id,
                anyLast(s.banner_name) AS banner_name,
                CAST(anyLast(s.ad_group_id), 'Nullable(Int64)') AS ad_group_id
            FROM raw_data.vk_ads_stats_day AS s
            WHERE s.banner_id IS NOT NULL
              AND s.account_id IN ({VK_AUTO_ACCOUNTS_SQL})
            GROUP BY s.banner_id
        """,
    }
    rows: dict[str, int] = {}
    for table, sql in ddl.items():
        shadow = f"ad_analytics.{table}_new"
        client.command(f"DROP TABLE IF EXISTS {shadow} SYNC")
        client.command(sql, settings=SAFE_QUERY_SETTINGS)
        swap_shadow(client, f"ad_analytics.{table}", shadow)
        rows[table] = count_rows(client, f"ad_analytics.{table}")
        log.info("  %s=%d", table, rows[table])
    return rows


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    log.info("build_star v6_ch: ClickHouse star tables")
    client = get_client()
    t0 = time.perf_counter()
    if not table_exists(client, "ad_analytics", "big_analytics_unified"):
        raise RuntimeError("ad_analytics.big_analytics_unified отсутствует")
    fact_rows = build_fact(client)
    dim_rows = build_dims(client)
    vk_rows = build_vk_ads_fact(client)
    vk_dim_rows = build_vk_dims(client)
    ml_rows = build_ml_korrektirovki_fact(client)
    parts = [
        f"fact_big_analytics={fact_rows:,}",
        *[f"{k}={v:,}" for k, v in dim_rows.items()],
        f"fact_vk_ads={vk_rows:,}",
        *[f"{k}={v:,}" for k, v in vk_dim_rows.items()],
        f"fact_ml_korrektirovki={ml_rows:,}",
    ]
    details = ", ".join(parts)
    log.info("build_star v6_ch завершён за %.1f сек: %s", time.perf_counter() - t0, details)
    return {"rows": fact_rows, "details": details}


def main() -> None:
    run()


if __name__ == "__main__":
    main()
