"""Build ClickHouse star/Power BI tables for v6_ch."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_settings import DATE_FROM
from config.ch_utils import SAFE_QUERY_SETTINGS, column_names, count_rows, day_ranges, q, range_batches, swap_shadow, table_exists
from step3_build_sources.step3 import _metric_expr

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("build_star")


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
    cols = column_names(client, "ad_analytics", "big_analytics_unified")
    alias_exprs = []
    if "status" not in cols:
        alias_exprs.append("`статус` AS status")
    if "project_manager" not in cols:
        alias_exprs.append("`проджект` AS project_manager")
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
            ORDER BY (ifNull(domain, ''), ifNull(`салон`, ''))
            AS
            SELECT
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
                crm_name AS `Название crm`
            FROM
            (
                SELECT
                    domain,
                    anyLast(`салон`) AS salon,
                    anyLast(`город`) AS city,
                    anyLast(`регион`) AS region,
                    anyLast(`тип_сайта`) AS site_type,
                    anyLast(`шаблон`) AS template,
                    anyLast(`направление`) AS direction,
                    anyLast(`статус`) AS site_status,
                    anyLast(`специалист`) AS specialist,
                    anyLast(`проджект`) AS project,
                    anyLast(`id_салона`) AS salon_id,
                    anyLast(`менеджер`) AS manager,
                    anyLast(`Название crm`) AS crm_name
                FROM ad_analytics.big_analytics_unified
                WHERE ifNull(domain, '') != ''
                GROUP BY domain
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
                anyLast(ag_part1) AS ag_part1,
                anyLast(ag_part2) AS ag_part2,
                anyLast(ag_part3) AS ag_part3,
                anyLast(ag_part4) AS ag_part4,
                anyLast(ag_part5) AS ag_part5,
                anyLast(ag_part6) AS ag_part6,
                anyLast(ag_part7) AS ag_part7,
                anyLast(`номер группы | название группы`) AS `номер группы | название группы`
            FROM ad_analytics.big_analytics_unified
            WHERE `AdGroupId` IS NOT NULL
            GROUP BY `AdGroupId`
        """,
        "Dim_Location": """
            CREATE TABLE ad_analytics.Dim_Location_new
            ENGINE = MergeTree
            ORDER BY id_location
            AS
            WITH locations AS
            (
                SELECT
                    assumeNotNull(id_location) AS id_location,
                    location,
                    `Область`,
                    GeoRegionType,
                    distance_km_agreg,
                    1 AS priority
                FROM ad_analytics.fact_region_spend
                WHERE id_location IS NOT NULL
                UNION ALL
                SELECT
                    assumeNotNull(id_location) AS id_location,
                    location,
                    `Область`,
                    GeoRegionType,
                    distance_km_agreg,
                    2 AS priority
                FROM ad_analytics.fact_region_zayavki
                WHERE id_location IS NOT NULL
            )
            SELECT
                id_location,
                ifNull(argMin(location, if(ifNull(location, '') = '', 99, priority)), '') AS location,
                ifNull(argMin(`Область`, if(ifNull(`Область`, '') = '', 99, priority)), '') AS `Область`,
                CAST(argMin(GeoRegionType, if(ifNull(GeoRegionType, '') = '', 99, priority)), 'LowCardinality(Nullable(String))') AS GeoRegionType,
                argMin(distance_km_agreg, if(distance_km_agreg IS NULL, 99, priority)) AS distance_km_agreg
            FROM locations
            GROUP BY id_location
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
        FROM ad_analytics.fact_big_analytics f
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
            SELECT
                banner_id,
                anyLast(account_id) AS account_id,
                anyLast(ad_plan_id) AS ad_plan_id,
                anyLast(ad_plan_name) AS ad_plan_name,
                anyLast(ad_group_id) AS ad_group_id,
                anyLast(ad_group_name) AS ad_group_name,
                anyLast(banner_name) AS banner_name
            FROM raw_data.vk_ads_stats_day
            WHERE banner_id IS NOT NULL
            GROUP BY banner_id
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
            s.ad_plan_name,
            CAST(s.ad_group_id, 'Nullable(Int64)') AS ad_group_id,
            s.ad_group_name,
            CAST(s.banner_id, 'Nullable(Int64)') AS banner_id,
            s.banner_name,
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

        UNION ALL

        SELECT
            assumeNotNull(za.date) AS date,
            CAST(bd.account_id, 'Nullable(Int64)') AS account_id,
            CAST(za.`салон`, 'LowCardinality(Nullable(String))') AS `салон`,
            CAST(bd.ad_plan_id, 'Nullable(Int64)') AS ad_plan_id,
            bd.ad_plan_name,
            CAST(ifNull(za.ad_group_id, bd.ad_group_id), 'Nullable(Int64)') AS ad_group_id,
            bd.ad_group_name,
            CAST(za.banner_id, 'Nullable(Int64)') AS banner_id,
            bd.banner_name,
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
            bd.ad_plan_name,
            CAST(ifNull(va.ad_group_id, bd.ad_group_id), 'Nullable(Int64)') AS ad_group_id,
            bd.ad_group_name,
            CAST(va.banner_id, 'Nullable(Int64)') AS banner_id,
            bd.banner_name,
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


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    log.info("build_star v6_ch: ClickHouse star tables")
    client = get_client()
    t0 = time.perf_counter()
    if not table_exists(client, "ad_analytics", "big_analytics_unified"):
        raise RuntimeError("ad_analytics.big_analytics_unified отсутствует")
    fact_rows = build_fact(client)
    dim_rows = build_dims(client)
    vk_rows = build_vk_ads_fact(client)
    ml_rows = build_ml_korrektirovki_fact(client)
    parts = [
        f"fact_big_analytics={fact_rows:,}",
        *[f"{k}={v:,}" for k, v in dim_rows.items()],
        f"fact_vk_ads={vk_rows:,}",
        f"fact_ml_korrektirovki={ml_rows:,}",
    ]
    details = ", ".join(parts)
    log.info("build_star v6_ch завершён за %.1f сек: %s", time.perf_counter() - t0, details)
    return {"rows": fact_rows, "details": details}


def main() -> None:
    run()


if __name__ == "__main__":
    main()
