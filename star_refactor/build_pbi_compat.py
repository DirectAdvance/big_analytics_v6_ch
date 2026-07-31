"""Build compatibility tables expected by the existing Power BI semantic model."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_settings import DATE_FROM
from config.ch_utils import SAFE_QUERY_SETTINGS, count_rows, day_ranges, q, swap_shadow, table_exists

log = logging.getLogger("build_pbi_compat")

PBI_FULL_COLS = [
    "Date", "CampaignId", "AdNetworkType", "Device", "total_cost", "campaign_code", "tp", "cpc_cpa",
    "Обращения", "korr", "kval", "priezd", "prodazhi", "nekorr", "ne_otvechaet", "filtr", "nedozvon",
    "RlAdjustmentId", "RlAdjustmentId_total", "fid", "Clicks", "источник", "План заявки", "План приезда",
    "аккаунт|сайт", "поставщик", "домен", "priedet", "dohod_do_kredita", "dobro", "атрибуция",
    "AdGroupId", "направление", "site_quiz", "марки авто", "специалист", "тип_сайта", "статус",
    "status", "салон", "шаблон", "id_салона", "город", "регион", "проджект", "project_manager",
    "менеджер", "Название crm",
    "тип_заявки", "manager_login", "CampaignName", "AdGroupName", "account_login", "adgroup_code",
    "ag_part1", "ag_part2", "ag_part3", "ag_part4", "ag_part5", "ag_part6", "ag_part7",
    "campaign_status", "payment_model", "неверный_кодер_new", "week_start", "День недели",
    "номер группы | название группы", "номер кампании | название кампании",
]

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


def _pbi_full_expr(col: str) -> str:
    if col == "Обращения":
        return "kol_vo_zayavok AS `Обращения`"
    if col == "домен":
        return "domain AS `домен`"
    return q(col)


def build_pbi_full(client) -> int:
    select_sql = ", ".join(_pbi_full_expr(col) for col in PBI_FULL_COLS)
    shadow = "ad_analytics.pbi_import_big_analytics_full_new"
    client.command(f"DROP TABLE IF EXISTS {shadow} SYNC")
    client.command(
        f"""
        CREATE TABLE {shadow}
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(ifNull(`Date`, toDate('2026-01-01')))
        ORDER BY (ifNull(`Date`, toDate('2026-01-01')), ifNull(`CampaignId`, 0), ifNull(`AdGroupId`, 0), ifNull(`домен`, ''))
        AS
        SELECT {select_sql}
        FROM ad_analytics.fact_big_analytics
        WHERE 0
        """,
        settings=SAFE_QUERY_SETTINGS,
    )
    ranges = day_ranges(DATE_FROM)
    for idx, (lo, hi) in enumerate(ranges, start=1):
        client.command(
            f"""
            INSERT INTO {shadow}
            SELECT {select_sql}
            FROM ad_analytics.fact_big_analytics
            WHERE `Date` >= toDate('{lo}') AND `Date` < toDate('{hi}')
            """,
            settings=SAFE_QUERY_SETTINGS,
        )
        log.info("  pbi_import_big_analytics_full daily batch %d/%d: %s -> %s", idx, len(ranges), lo, hi)
    swap_shadow(client, "ad_analytics.pbi_import_big_analytics_full", shadow)
    _replace_view(
        client,
        "pbi_big_analytics_full",
        "SELECT * FROM ad_analytics.pbi_import_big_analytics_full"
    )
    return count_rows(client, "ad_analytics.pbi_big_analytics_full")


def build_pixel_score(client) -> int:
    _replace_view(
        client,
        "pixel_score",
        f"""
        SELECT
            toStartOfMonth(`Date`) AS month,
            `салон`,
            domain,
            `источник`,
            `CampaignId`,
            `CampaignName`,
            kol_vo_zayavok,
            korr,
            kval,
            priezd,
            prodazhi,
            `направление`,
            toDecimal64(1, 4) AS cpl_score,
            kol_vo_zayavok AS `pixel_kol_vo_домена`,
            kol_vo_zayavok AS `pixel_kol_vo_кампании`,
            CAST(NULL, 'Nullable(Decimal(18, 4))') AS `cpl_avg_квал`,
            CAST(NULL, 'Nullable(Decimal(18, 4))') AS `cpl_avg_визит`,
            CAST(NULL, 'Nullable(Decimal(18, 4))') AS `cpl_avg_продажа`,
            CAST(NULL, 'Nullable(Decimal(18, 4))') AS `cpl_кам_квал`,
            CAST(NULL, 'Nullable(Decimal(18, 4))') AS `cpl_кам_визит`,
            CAST(NULL, 'Nullable(Decimal(18, 4))') AS `cpl_кам_продажа`,
            toDecimal64(1, 4) AS `score_квал`,
            toDecimal64(1, 4) AS `score_визит`,
            toDecimal64(1, 4) AS `score_продажа`,
            toDecimal64(1, 4) AS `w_квал`,
            toDecimal64(1, 4) AS `w_визит`,
            toDecimal64(1, 4) AS `w_продажа`,
            'данные' AS `status_квал`,
            'данные' AS `status_визит`,
            'данные' AS `status_продажа`,
            total_cost AS `расход`,
            toDecimal64(100, 4) AS weight,
            kval AS `pixel_квал_домена`,
            kval AS `attr_pixel_квал_кампании`,
            priezd AS `pixel_приезд_домена`,
            priezd AS `attr_pixel_приезд_кампании`,
            prodazhi AS `pixel_продажи_домена`,
            prodazhi AS `attr_pixel_продажи_кампании`
        FROM ad_analytics.big_analytics_pixel_score
        """
    )
    return count_rows(client, "ad_analytics.pixel_score")


def build_dim_placement_feed(client) -> int:
    stage = "ad_analytics.Dim_PlacementFeed_stage_new"
    shadow = "ad_analytics.Dim_PlacementFeed_new"
    client.command(f"DROP TABLE IF EXISTS {stage} SYNC")
    client.command(f"DROP TABLE IF EXISTS {shadow} SYNC")
    client.command(
        f"""
        CREATE TABLE {stage}
        ENGINE = MergeTree
        ORDER BY placement_feed_key
        AS
        SELECT
            lowerUTF8(trim(BOTH ' ' FROM ifNull(placement, ''))) AS placement_feed_key,
            trim(BOTH ' ' FROM ifNull(placement, '')) AS placement,
            trim(BOTH ' ' FROM ifNull(placement, '')) AS feed_key,
            trim(BOTH ' ' FROM ifNull(placement, '')) AS feed_url_key,
            ad_network_type,
            ad_network_type AS AdNetworkType,
            tuple(count(), lengthUTF8(placement)) AS sort_weight
        FROM ad_analytics.fact_direct_feed_funnel
        WHERE 0
        GROUP BY placement_feed_key, placement, ad_network_type
        """,
        settings=SAFE_QUERY_SETTINGS,
    )
    ranges = day_ranges(DATE_FROM)
    for idx, (lo, hi) in enumerate(ranges, start=1):
        client.command(
            f"""
            INSERT INTO {stage}
            SELECT
                lowerUTF8(trim(BOTH ' ' FROM ifNull(placement, ''))) AS placement_feed_key,
                trim(BOTH ' ' FROM ifNull(placement, '')) AS placement,
                trim(BOTH ' ' FROM ifNull(placement, '')) AS feed_key,
                trim(BOTH ' ' FROM ifNull(placement, '')) AS feed_url_key,
                ad_network_type,
                ad_network_type AS AdNetworkType,
                tuple(count(), lengthUTF8(placement)) AS sort_weight
            FROM ad_analytics.fact_direct_feed_funnel
            WHERE date >= toDate('{lo}') AND date < toDate('{hi}')
              AND notEmpty(lowerUTF8(trim(BOTH ' ' FROM ifNull(placement, ''))))
            GROUP BY placement_feed_key, placement, ad_network_type
            """,
            settings=SAFE_QUERY_SETTINGS,
        )
        log.info("  Dim_PlacementFeed daily stage batch %d/%d: %s -> %s", idx, len(ranges), lo, hi)
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
            date,
            lowerUTF8(trim(BOTH ' ' FROM ifNull(placement, ''))) AS placement_feed_key,
            CAST(NULL, 'Nullable(Int64)') AS feed_id,
            CAST(NULL, 'Nullable(String)') AS feed_name,
            CAST(NULL, 'Nullable(String)') AS feed_url,
            placement AS feed_key,
            placement AS feed_url_key,
            campaign_id,
            campaign_name,
            ad_group_id AS adgroup_id,
            ad_group_name AS adgroup_name,
            domain,
            account_login AS login_key,
            cost AS total_cost,
            clicks,
            impressions,
            all_forms AS kol_vo_zayavok,
            crm_order_created AS korr,
            toDecimal64(0, 6) AS nekorr,
            toDecimal64(0, 6) AS kval,
            toDecimal64(0, 6) AS priezd,
            crm_order_paid AS prodazhi,
            toDecimal64(0, 6) AS dobro,
            toDecimal64(0, 6) AS dohod_do_kredita,
            toDecimal64(0, 6) AS filtr,
            all_forms AS attributed_leads,
            toDecimal64(0, 6) AS ne_otvechaet,
            toDecimal64(0, 6) AS nedozvon,
            toDecimal64(0, 6) AS priedet,
            all_forms AS goal_all_forms,
            crm_order_created AS goal_crm_order_created,
            crm_order_paid AS goal_crm_order_paid,
            toDecimal64(0, 6) AS goal_crm_order_canceled,
            toDecimal64(0, 6) AS goal_crm_spam_order,
            concat(toString(date), '|', ifNull(domain, ''), '|', ifNull(placement, '')) AS dk2,
            toUInt8(0) AS is_tp67,
            gs.directologist AS `специалист`,
            gs.site_type AS `тип_сайта`,
            gs.region AS `регион`,
            gs.salon AS `салон`,
            gs.city AS `город`,
            gs.template AS `шаблон`,
            CAST(NULL, 'Nullable(String)') AS `направление`,
            concat(ifNull(account_login, ''), '|', ifNull(domain, '')) AS `аккаунт|сайт`,
            CAST(NULL, 'Nullable(String)') AS `канал`,
            CAST(NULL, 'Nullable(String)') AS `ниша`,
            CAST(NULL, 'Nullable(String)') AS `статус_кампании`,
            CAST(NULL, 'Nullable(String)') AS `тип_оплаты`,
            concat(toString(campaign_id), ' | ', ifNull(campaign_name, '')) AS `номер кампании | название кампании`,
            concat(toString(ad_group_id), ' | ', ifNull(ad_group_name, '')) AS `номер группы | название группы`,
            CAST(NULL, 'Nullable(String)') AS tp,
            CAST(NULL, 'Nullable(String)') AS campaign_code,
            CAST(NULL, 'Nullable(String)') AS `тип_заявки`,
            CAST(NULL, 'Nullable(String)') AS `Название crm`,
            CAST(NULL, 'Nullable(String)') AS `статус`,
            'direct_feed_funnel' AS `источник`,
            domain AS `домен`,
            ad_network_type AS AdNetworkType,
            now() AS generated_at
        FROM ad_analytics.fact_direct_feed_funnel f
        LEFT JOIN raw_data.gsheet_sites gs ON lower(ifNull(gs.login_key, '')) = lower(f.account_login)
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
        ORDER BY (date, campaign_id, adgroup_id, placement_feed_key)
        AS
        {_feed_funnel_pbi_sql("WHERE 0")}
        """,
        settings=SAFE_QUERY_SETTINGS,
    )
    ranges = day_ranges(DATE_FROM)
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


def build_pbi_import_region_spend(client) -> int:
    shadow = "ad_analytics.pbi_import_region_spend_new"
    client.command(f"DROP TABLE IF EXISTS {shadow} SYNC")
    client.command(
        f"""
        CREATE TABLE {shadow}
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(date)
        ORDER BY (date, campaign_id, ifNull(ad_group_id, 0), ifNull(id_location, 0))
        AS
        SELECT *
        FROM ad_analytics.fact_region_spend
        WHERE 0
        """,
        settings=SAFE_QUERY_SETTINGS,
    )
    ranges = day_ranges(DATE_FROM)
    for idx, (lo, hi) in enumerate(ranges, start=1):
        client.command(
            f"""
            INSERT INTO {shadow}
            SELECT *
            FROM ad_analytics.fact_region_spend
            WHERE date >= toDate('{lo}') AND date < toDate('{hi}')
            """,
            settings=SAFE_QUERY_SETTINGS,
        )
        log.info("  pbi_import_region_spend daily batch %d/%d: %s -> %s", idx, len(ranges), lo, hi)
    swap_shadow(client, "ad_analytics.pbi_import_region_spend", shadow)
    return count_rows(client, "ad_analytics.pbi_import_region_spend")


def _arp_fact_sql(where_sql: str = "") -> str:
    return f"""
        SELECT
            date, domain AS `домен`, account_login AS `логин`, ad_network_type, placement,
            lowerUTF8(trim(BOTH ' ' FROM ifNull(placement, ''))) AS placement_feed_key,
            placement AS placement_key, cost, all_forms AS `Все формы`,
            crm_order_created AS `CRM: Заказ создан`, crm_order_paid AS `CRM: Заказ оплачен`,
            toDecimal64(0, 6) AS `CRM: Спам заказ`, toDecimal64(0, 6) AS `CRM: Заказ отменен`,
            CAST(NULL, 'Nullable(String)') AS tp,
            gs.directologist AS `Специалист`, gs.salon AS `салон`, gs.site_type AS `тип_сайта`,
            now() AS updated_at,
            toDecimal64(0, 6) AS kol_vo_zayavok, toDecimal64(0, 6) AS korr, toDecimal64(0, 6) AS kval,
            toDecimal64(0, 6) AS priezd, toDecimal64(0, 6) AS prodazhi, toDecimal64(0, 6) AS nekorr,
            toDecimal64(0, 6) AS priedet,
            concat(toString(campaign_id), '|', ifNull(campaign_name, '')) AS `номер кампании|название кампании`,
            campaign_id AS CampaignId, ad_group_id AS AdGroupId, clicks,
            toDecimal64(0, 6) AS ne_otvechaet, toDecimal64(0, 6) AS nedozvon, toDecimal64(0, 6) AS filtr,
            toInt64(0) AS dohod_do_kredita, toInt64(0) AS dobro,
            date AS Date, domain, CAST(NULL, 'Nullable(String)') AS `тип_заявки`
        FROM ad_analytics.fact_direct_feed_funnel f
        LEFT JOIN raw_data.gsheet_sites gs ON lower(ifNull(gs.login_key, '')) = lower(f.account_login)
        {where_sql}
    """


def build_arp_fact(client) -> int:
    shadow = "ad_analytics.arp_fact_new"
    client.command(f"DROP TABLE IF EXISTS {shadow} SYNC")
    client.command(
        f"""
        CREATE TABLE {shadow}
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(date)
        ORDER BY (date, ifNull(domain, ''), ifNull(placement, ''))
        AS
        {_arp_fact_sql("WHERE 0")}
        """,
        settings=SAFE_QUERY_SETTINGS,
    )
    ranges = day_ranges(DATE_FROM)
    for idx, (lo, hi) in enumerate(ranges, start=1):
        client.command(
            f"""
            INSERT INTO {shadow}
            {_arp_fact_sql(f"WHERE f.date >= toDate('{lo}') AND f.date < toDate('{hi}')")}
            """,
            settings=SAFE_QUERY_SETTINGS,
        )
        log.info("  arp_fact daily batch %d/%d: %s -> %s", idx, len(ranges), lo, hi)
    swap_shadow(client, "ad_analytics.arp_fact", shadow)
    return count_rows(client, "ad_analytics.arp_fact")


def create_light_aliases(client) -> dict[str, int]:
    statements = {
        "arc_fact": "SELECT * FROM ad_analytics.fact_criterion_spend",
        "arf_fact": "SELECT * FROM ad_analytics.pbi_import_fact_direct_feed_funnel",
        "dim_criterion": """
            SELECT
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
                        lowerUTF8(trim(BOTH ' ' FROM criterion)) AS criterion_key,
                        trim(BOTH ' ' FROM criterion) AS criterion,
                        anyLast(criterion_type) AS criterion_type,
                        anyLast(criterion_raw) AS criterion_raw,
                        count() AS rows,
                        2 AS source_priority
                    FROM ad_analytics.fact_criterion_spend
                    WHERE notEmpty(lowerUTF8(trim(BOTH ' ' FROM criterion)))
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
                        lowerUTF8(trim(BOTH ' ' FROM ifNull(criterion, ''))) AS criterion_key,
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
        """,
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
    }
    rows.update(create_light_aliases(client))
    rows.update(create_bi_views(client))
    details = ", ".join(f"{k}={v:,}" for k, v in rows.items())
    log.info("build_pbi_compat v6_ch завершён за %.1f сек: %s", time.perf_counter() - t0, details)
    return {"rows": sum(rows.values()), "details": details}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(run())
