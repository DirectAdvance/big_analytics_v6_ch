"""Build ClickHouse star/Power BI tables for v6_ch."""

from __future__ import annotations

import logging
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_settings import DATE_FROM, VK_AUTO_ACCOUNTS_SQL
from config.ch_utils import (
    SAFE_QUERY_SETTINGS as BASE_SAFE_QUERY_SETTINGS,
    apply_storage_codecs,
    column_names,
    count_rows,
    day_ranges,
    q,
    range_batches,
    swap_shadow,
    table_engine,
    table_exists,
)
from step3_build_sources.step3 import _crm_name_expr, _metric_expr

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("build_star")

SAFE_QUERY_SETTINGS = {
    **BASE_SAFE_QUERY_SETTINGS,
    "max_execution_time": 600,
}

FACT_BIG_DIMENSION_COLUMNS = {
    "key3",
    "День недели",
    "week_start",
    "CampaignName",
    "AdGroupName",
    "RlAdjustmentId_total",
    "campaign_code",
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
    "account_login",
    "Название crm",
    "тип_заявки",
    "статус",
    "cascade_level",
    "салон",
    "город",
    "регион",
    "тип_сайта",
    "шаблон",
    "специалист",
    "проджект",
    "менеджер",
    "id_салона",
    "направление",
    "direction",
}


ACCOUNT_KEY_COLUMNS = ["account_login"]
CRM_STATUS_KEY_COLUMNS = ["Название crm", "тип_заявки", "статус", "cascade_level"]
SALON_KEY_COLUMNS = [
    "салон",
    "город",
    "регион",
    "тип_сайта",
    "шаблон",
    "специалист",
    "проджект",
    "менеджер",
    "id_салона",
    "направление",
]


FACT_SWAP_COMPAT_OBJECTS = [
    "bi_pbi_big_analytics_full",
    "bi_pbi_import_big_analytics_full",
    "bi_big_analytics_full_arrival",
    "pbi_big_analytics_full",
    "pbi_import_big_analytics_full",
    "big_analytics_direct",
    "big_analytics_seo",
    "big_analytics_pixel",
    "big_analytics_crop_targeting",
    "big_analytics_reviews",
    "big_analytics_unified",
    "big_analytics_full_arrival",
    "big_analytics_full",
]
# PIXEL_DEDUP_2026-08-15: big_analytics_pixel_score deliberately excluded here.
# It used to be dropped in this pass and rebuilt by cleanup_wide_intermediates.py
# as a compat view; that recreation was removed (self-referencing view crash —
# see cleanup_wide_intermediates.py), so this table now stays untouched end to
# end as the physical table step11 produces. Dropping it here with nothing left
# to recreate it broke step13's pixel branch and build_pbi_compat.build_pixel_score().


def _normalized_string_expr(column: str) -> str:
    return f"lowerUTF8(trim(BOTH ' ' FROM ifNull({q(column)}, '')))"


def _canonical_crm_name_sql(crm_expr: str) -> str:
    return (
        "multiIf("
        f"ifNull({crm_expr}, '') = '', 'Не указана', "
        f"{crm_expr} = 'One CRM', 'Фаиг', "
        f"{crm_expr} = 'PLEX', 'Плекс', "
        f"{crm_expr} = 'MEGA CRM', 'Мега', "
        f"{crm_expr} = 'MarCar CRM', 'Маркар', "
        f"{crm_expr} = 'M-Auto CRM', 'МаАвто', "
        f"{crm_expr} = 'RedautoCRM', 'Ред Авто', "
        f"{crm_expr} = 'GenzesCRM', 'Генезис', "
        f"{crm_expr} = 'crmf', 'Фаиг', "
        f"{crm_expr} IN ('RivendellCRM', 'rivendell_excel', 'perform_api'), 'Ривендел', "
        f"{crm_expr})"
    )


def _manager_login_key_sql(expr: str = "manager_login", alias: str = "manager_login_key") -> str:
    normalized = f"lowerUTF8(trim(BOTH ' ' FROM ifNull({expr}, '')))"
    return f"if(position({normalized}, '@') > 0, cityHash64({normalized}), toUInt64(0)) AS {alias}"


def _manager_login_label_sql(expr: str = "manager_login", alias: str = "manager_login") -> str:
    normalized = f"trim(BOTH ' ' FROM ifNull({expr}, ''))"
    return f"if(position(lowerUTF8({normalized}), '@') > 0, {normalized}, '') AS {alias}"


def _dimension_key_sql(columns: list[str], alias: str) -> str:
    normalized = [_normalized_string_expr(column) for column in columns]
    has_value = " OR ".join(f"notEmpty({expr})" for expr in normalized)
    values = ", ".join(normalized)
    return f"if({has_value}, cityHash64(concatWithSeparator('\\t', {values})), toUInt64(0)) AS {alias}"


def _create_fact_empty(client, target: str, select_sql: str) -> None:
    client.command(f"DROP TABLE IF EXISTS {target} SYNC")
    client.command(
        f"""
        CREATE TABLE {target}
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(ifNull(`Date`, toDate('2026-01-01')))
        ORDER BY (ifNull(`Date`, toDate('2026-01-01')), `атрибуция`, site_key, ifNull(domain, ''))
        AS SELECT {select_sql}
        FROM ad_analytics.big_analytics_unified
        WHERE 0
        """,
        settings=SAFE_QUERY_SETTINGS,
    )
    # FACT_WEIGHT_2026-08-14: схема факта выводится из big_analytics_unified и заранее неизвестна,
    # поэтому кодеки навешиваются на уже созданную пустую shadow-таблицу (OPTIMIZATION_PLAN.md).
    apply_storage_codecs(client, target)


def drop_fact_compat_objects(client) -> None:
    for name in FACT_SWAP_COMPAT_OBJECTS:
        engine = table_engine(client, "ad_analytics", name)
        if engine == "View":
            client.command(f"DROP VIEW IF EXISTS ad_analytics.{q(name)} SYNC", settings=SAFE_QUERY_SETTINGS)
        elif engine:
            client.command(f"DROP TABLE IF EXISTS ad_analytics.{q(name)} SYNC", settings=SAFE_QUERY_SETTINGS)


def build_fact_projection(source_cols: list[str]) -> tuple[str, list[str]]:
    cols = [
        col
        for col in source_cols
        if col not in FACT_BIG_DIMENSION_COLUMNS
    ]
    alias_exprs = []
    if "domain" in source_cols:
        alias_exprs.append(
            "if(notEmpty(lowerUTF8(trim(BOTH ' ' FROM ifNull(domain, '')))), "
            "cityHash64(lowerUTF8(trim(BOTH ' ' FROM ifNull(domain, '')))), toUInt64(0)) "
            "AS site_key"
        )
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
        alias_exprs.append(_manager_login_key_sql())
    if all(column in source_cols for column in ACCOUNT_KEY_COLUMNS):
        alias_exprs.append(_dimension_key_sql(ACCOUNT_KEY_COLUMNS, "account_key"))
    if all(column in source_cols for column in CRM_STATUS_KEY_COLUMNS):
        alias_exprs.append(_dimension_key_sql(CRM_STATUS_KEY_COLUMNS, "crm_status_key"))
    if all(column in source_cols for column in SALON_KEY_COLUMNS):
        alias_exprs.append(_dimension_key_sql(SALON_KEY_COLUMNS, "salon_key"))
    target_cols = cols + [expr.rsplit(" AS ", 1)[1] for expr in alias_exprs]
    select_sql = ", ".join([q(col) for col in cols] + alias_exprs)
    return select_sql, target_cols


def build_fact(client) -> int:
    source_cols = column_names(client, "ad_analytics", "big_analytics_unified")
    select_sql, target_cols = build_fact_projection(source_cols)
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
    drop_fact_compat_objects(client)
    swap_shadow(client, "ad_analytics.fact_big_analytics", shadow)
    return count_rows(client, "ad_analytics.fact_big_analytics")


DIM_DDL = {
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
                toLowCardinality(arrayElement([
                    'Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
                    'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь'
                ], toMonth(`Date`))) AS year_month,
                toDayOfMonth(`Date`) AS day
            FROM ad_analytics.big_analytics_unified
            WHERE `Date` IS NOT NULL
        """,
        # DIM_SITE_TIEBREAK_FIX_2026-08-07: old sort_weight was
        # tuple(toUInt8(class), notEmpty(salon), lengthUTF8(domain)) -- components
        # 2/3 are CONSTANT within one domain group (lengthUTF8(domain) never
        # changes for the same domain; notEmpty(salon) is true almost always), so
        # whenever a domain had >1 distinct fact-row `салон` (etc.) value, argMax
        # picked an ARBITRARY one (measured live: 414 domains with conflicting
        # `салон` values in fact rows). Replaced with two deterministic sources:
        # domains covered by the master directory (reference_data.gsheet_sites) always
        # win via direct join/aggregation (no tie possible -- duplicates in
        # gsheet_sites are verified byte-identical, `any()` is safe); only domains
        # ABSENT from the master directory fall back to fact rows, tie-broken by
        # a MEANINGFUL weight -- majority attribute-combination, most recent
        # `Date`, then `domain` itself for full determinism (DIM_SITE_TIEBREAK_FIX
        # 2 below) -- instead of string length.
        #
        # DIM_SITE_COLUMN_AUTHORITY_FIX_2026-08-07 (director rework, patch 2 of
        # the review): the directory (reference_data.gsheet_sites) is authoritative ONLY
        # for the 9 attributes it uniquely owns -- салон/город/регион/тип_сайта/
        # шаблон/статус/проджект/менеджер/id_салона (CANON has no separate source
        # for these; the sheet IS the source). `направление`, `специалист` and
        # `Название crm` are NOT among them -- the fact rows carry their OWN
        # taxonomy for these three, set literally by step3/step6 ETL code
        # (направление: 'Контекст'/'Комплекс'/'Пиксель'/'Пиксель_атрибуц' --
        # CANON.md; Название crm: 'Фаиг'/'Плекс'/'Маркар'/'Мега'/... -- step3.py:
        # 538-546, consumed literally by sales_attribution/build.py:139,159 and
        # FUNNEL.md:186; специалист: filled by the spec_fallback.py step-115
        # cascade run BEFORE this step in the pipeline, so by the time Dim_Site is
        # built big_analytics_unified.специалист is already the final resolved
        # value). The directory's own `direction`/`directologist`/`crm` columns
        # hold a DIFFERENT taxonomy (`Авто`/`Внутренний маркетинг`/`Digital`/`PR`
        # department, and raw CRM software names like `One CRM`/`PLEX`) -- taking
        # them for these 3 columns was blocker A/B/C of the review: 1594 keys of
        # `направление` and 1554 keys of `Название crm` were silently replaced by
        # the wrong taxonomy, and `специалист` got WORSE (3025 -> 3309 empty,
        # because the sheet's `directologist` field is blank for many domains the
        # fact-row cascade had already resolved). Fix: fact_direction/
        # fact_specialist/fact_crm CTEs below compute the FACT-side majority
        # (non-empty values only) per site_key for these 3 columns and are
        # COALESCEd in ahead of the directory's own value -- directory value is
        # used ONLY as a last-resort fallback when the domain has zero non-empty
        # fact rows for that column (e.g. a catalog domain with no traffic yet).
        # Measured: специалист empty count returns to the pre-bug baseline 3025
        # and 284 previously-lost values come back with zero new losses; the 9
        # directory-owned attributes are byte-for-byte unchanged (0 diff). Branch
        # 2 (domains the directory does not cover at all) is untouched -- it
        # already sourced these 3 columns from fact, so it was never part of the
        # A/B/C bug.
        "Dim_Site": f"""
            CREATE TABLE ad_analytics.Dim_Site_new
            ENGINE = MergeTree
            ORDER BY site_key
            AS
            WITH
            fact_direction AS
            (
                SELECT
                    site_key,
                    argMax(direction, tuple(cnt, max_date, direction)) AS direction
                FROM
                (
                    SELECT
                        cityHash64(lowerUTF8(trim(BOTH ' ' FROM ifNull(domain, '')))) AS site_key,
                        `направление` AS direction,
                        count() AS cnt,
                        max(`Date`) AS max_date
                    FROM ad_analytics.big_analytics_unified
                    WHERE ifNull(domain, '') != '' AND ifNull(`направление`, '') != ''
                    GROUP BY site_key, direction
                )
                GROUP BY site_key
            ),
            fact_specialist AS
            (
                SELECT
                    site_key,
                    argMax(specialist, tuple(cnt, max_date, specialist)) AS specialist
                FROM
                (
                    SELECT
                        cityHash64(lowerUTF8(trim(BOTH ' ' FROM ifNull(domain, '')))) AS site_key,
                        `специалист` AS specialist,
                        count() AS cnt,
                        max(`Date`) AS max_date
                    FROM ad_analytics.big_analytics_unified
                    WHERE ifNull(domain, '') != '' AND ifNull(`специалист`, '') != ''
                    GROUP BY site_key, specialist
                )
                GROUP BY site_key
            ),
            fact_crm AS
            (
                SELECT
                    site_key,
                    argMax(crm_name, tuple(cnt, max_date, crm_name)) AS crm_name
                FROM
                (
                    SELECT
                        cityHash64(lowerUTF8(trim(BOTH ' ' FROM ifNull(domain, '')))) AS site_key,
                        `Название crm` AS crm_name,
                        count() AS cnt,
                        max(`Date`) AS max_date
                    FROM ad_analytics.big_analytics_unified
                    WHERE ifNull(domain, '') != '' AND ifNull(`Название crm`, '') != ''
                    GROUP BY site_key, crm_name
                )
                GROUP BY site_key
            ),
            raw_crm AS
            (
                SELECT
                    site_key,
                    argMax(crm_name, tuple(cnt, max_date, crm_name)) AS crm_name
                FROM
                (
                    SELECT
                        cityHash64(lowerUTF8(trim(BOTH ' ' FROM ifNull(utm_source, '')))) AS site_key,
                        {_crm_name_expr("source_type")} AS crm_name,
                        count() AS cnt,
                        max(created_date) AS max_date
                    FROM raw_data.leads_all
                    WHERE is_copy_for_removal = 0
                      AND created_date >= toDate('2026-01-01')
                      AND ifNull(utm_source, '') != ''
                      AND ifNull(source_type, '') != ''
                    GROUP BY site_key, crm_name
                    HAVING crm_name != 'Не указана'
                )
                GROUP BY site_key
            )
            SELECT
                u.site_key,
                u.domain,
                u.salon AS `салон`,
                u.city AS `город`,
                u.region AS `регион`,
                u.site_type AS `тип_сайта`,
                u.template AS `шаблон`,
                u.direction AS `направление`,
                u.site_status AS `статус`,
                u.site_status AS status,
                u.specialist AS `специалист`,
                u.project AS `проджект`,
                u.project AS project_manager,
                u.salon_id AS `id_салона`,
                u.manager AS `менеджер`,
                CAST(
                    {_canonical_crm_name_sql("if(ifNull(u.crm_name, '') IN ('', 'Не указана'), if(ifNull(rc.crm_name, '') = '', 'Не указана', rc.crm_name), u.crm_name)")},
                    'String'
                ) AS `Название crm`
            FROM
            (
                -- 1) domains covered by the master directory: the 9
                -- directory-owned attributes always win (no argMax/tie-break
                -- needed -- dup rows in gsheet_sites are byte-identical
                -- duplicates, verified live). направление/специалист/Название
                -- crm come from the FACT majority (fact_direction/
                -- fact_specialist/fact_crm), falling back to the directory's
                -- own (differently-taxonomised) value only when the domain has
                -- no non-empty fact data for that column.
                SELECT
                    d.site_key AS site_key,
                    d.domain AS domain,
                    d.salon AS salon,
                    d.city AS city,
                    d.region AS region,
                    d.site_type AS site_type,
                    d.template AS template,
                    coalesce(nullIf(fd.direction, ''), d.direction) AS direction,
                    d.site_status AS site_status,
                    coalesce(nullIf(fs.specialist, ''), d.specialist) AS specialist,
                    d.project AS project,
                    d.salon_id AS salon_id,
                    d.manager AS manager,
                    coalesce(nullIf(fc.crm_name, ''), d.crm_name) AS crm_name
                FROM
                (
                    SELECT
                        site_key,
                        any(domain) AS domain,
                        any(salon) AS salon,
                        any(city) AS city,
                        any(region) AS region,
                        any(site_type) AS site_type,
                        any(template) AS template,
                        any(direction) AS direction,
                        any(site_status) AS site_status,
                        any(specialist) AS specialist,
                        any(project) AS project,
                        any(salon_id) AS salon_id,
                        any(manager) AS manager,
                        any(crm_name) AS crm_name
                    FROM
                    (
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
                            ifNull(crm, '') AS crm_name
                        FROM reference_data.gsheet_sites
                        WHERE ifNull(domain, '') != ''
                    )
                    GROUP BY site_key
                ) d
                LEFT JOIN fact_direction fd ON fd.site_key = d.site_key
                LEFT JOIN fact_specialist fs ON fs.site_key = d.site_key
                LEFT JOIN fact_crm fc ON fc.site_key = d.site_key

                UNION ALL

                -- 2) domains fact has but the master directory does not: not
                -- part of blocker A/B/C (these 3 columns already came from fact
                -- here) -- unchanged, only the tie-break weight gets a `domain`
                -- component added (DIM_SITE_TIEBREAK_FIX 2: count()/max(Date)
                -- alone can still tie when two attribute-combinations of a
                -- domain have equal frequency and equal max Date; `domain` is
                -- constant per group so it does not change today's winner, but
                -- makes the ORDER BY fully deterministic for the general case).
                SELECT
                    site_key,
                    argMax(domain, w) AS domain,
                    argMax(salon, w) AS salon,
                    argMax(city, w) AS city,
                    argMax(region, w) AS region,
                    argMax(site_type, w) AS site_type,
                    argMax(template, w) AS template,
                    argMax(direction, w) AS direction,
                    argMax(site_status, w) AS site_status,
                    argMax(specialist, w) AS specialist,
                    argMax(project, w) AS project,
                    argMax(salon_id, w) AS salon_id,
                    argMax(manager, w) AS manager,
                    argMax(crm_name, w) AS crm_name
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
                        tuple(count(), max(`Date`), domain) AS w
                    FROM ad_analytics.big_analytics_unified
                    WHERE ifNull(domain, '') != ''
                      AND lowerUTF8(trim(BOTH ' ' FROM ifNull(domain, ''))) NOT IN (
                          SELECT DISTINCT lowerUTF8(trim(BOTH ' ' FROM ifNull(domain, '')))
                          FROM reference_data.gsheet_sites
                          WHERE ifNull(domain, '') != ''
                      )
                    GROUP BY site_key, domain, salon, city, region, site_type, template,
                             direction, site_status, specialist, project, salon_id, manager, crm_name
                )
                GROUP BY site_key
            ) AS u
            LEFT JOIN raw_crm rc ON rc.site_key = u.site_key
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
                replaceAll(anyLast(campaign_code), 'kviz', 'quiz') AS campaign_code,
                anyLast(tp) AS tp,
                anyLast(cpc_cpa) AS cpc_cpa,
                anyLast(site_quiz) AS site_quiz,
                anyLast(campaign_status) AS campaign_status,
                anyLast(payment_model) AS payment_model,
                coalesce(
                    nullIf(trim(BOTH ' ' FROM ifNull(anyLast(`номер кампании | название кампании`), '')), ''),
                    concat(toString(`CampaignId`), ' | ', ifNull(anyLast(`CampaignName`), ''))
                ) AS `номер кампании | название кампании`
            FROM ad_analytics.big_analytics_unified
            WHERE `CampaignId` IS NOT NULL
            GROUP BY `CampaignId`
        """,
        "Dim_AdGroup": """
            CREATE TABLE ad_analytics.Dim_AdGroup_new
            ENGINE = MergeTree
            ORDER BY ifNull(AdGroupId, 0)
            AS
            WITH
            raw_adgroups AS
            (
                SELECT
                    group_id AS AdGroupId,
                    argMax(group_name, synced_at) AS AdGroupName,
                    argMax(campaign_id, synced_at) AS parent_CampaignId
                FROM reference_data.direct_adgroups
                WHERE group_id != 0
                GROUP BY AdGroupId
            ),
            fact_adgroups AS
            (
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
            )
            SELECT
                r.AdGroupId,
                coalesce(r.AdGroupName, f.AdGroupName) AS AdGroupName,
                f.adgroup_code AS adgroup_code,
                ifNull(f.`марки авто`, '') AS `марки авто`,
                ifNull(f.ag_part1, '') AS ag_part1,
                ifNull(f.ag_part2, '') AS ag_part2,
                ifNull(f.ag_part3, '') AS ag_part3,
                ifNull(f.ag_part4, '') AS ag_part4,
                ifNull(f.ag_part5, '') AS ag_part5,
                ifNull(f.ag_part6, '') AS ag_part6,
                ifNull(f.ag_part7, '') AS ag_part7,
                coalesce(
                    nullIf(trim(BOTH ' ' FROM ifNull(f.`номер группы | название группы`, '')), ''),
                    concat(toString(r.AdGroupId), ' | ', ifNull(r.AdGroupName, ''))
                ) AS `номер группы | название группы`,
                f.`неверный_кодер_new` AS `неверный_кодер_new`,
                ifNull(r.parent_CampaignId, f.parent_CampaignId) AS parent_CampaignId
            FROM raw_adgroups r
            LEFT JOIN fact_adgroups f ON f.AdGroupId = r.AdGroupId

            UNION ALL

            SELECT
                f.AdGroupId,
                f.AdGroupName,
                f.adgroup_code,
                ifNull(f.`марки авто`, '') AS `марки авто`,
                ifNull(f.ag_part1, '') AS ag_part1,
                ifNull(f.ag_part2, '') AS ag_part2,
                ifNull(f.ag_part3, '') AS ag_part3,
                ifNull(f.ag_part4, '') AS ag_part4,
                ifNull(f.ag_part5, '') AS ag_part5,
                ifNull(f.ag_part6, '') AS ag_part6,
                ifNull(f.ag_part7, '') AS ag_part7,
                coalesce(
                    nullIf(trim(BOTH ' ' FROM ifNull(f.`номер группы | название группы`, '')), ''),
                    concat(toString(f.AdGroupId), ' | ', ifNull(f.AdGroupName, ''))
                ) AS `номер группы | название группы`,
                f.`неверный_кодер_new`,
                f.parent_CampaignId
            FROM fact_adgroups f
            LEFT JOIN raw_adgroups r ON r.AdGroupId = f.AdGroupId
            WHERE r.AdGroupId = 0
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
        "Dim_ManagerLogin": f"""
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
                    {_manager_login_key_sql()},
                    {_manager_login_label_sql()}
                FROM ad_analytics.big_analytics_unified
            )
            GROUP BY manager_login_key
        """,
        "Dim_Account": f"""
            CREATE TABLE ad_analytics.Dim_Account_new
            ENGINE = MergeTree
            ORDER BY account_key
            AS
            SELECT
                account_key,
                anyLast(account_login) AS account_login
            FROM
            (
                SELECT
                    {_dimension_key_sql(ACCOUNT_KEY_COLUMNS, "account_key")},
                    ifNull(account_login, '') AS account_login
                FROM ad_analytics.big_analytics_unified
            )
            GROUP BY account_key
        """,
        "Dim_CRMStatus": f"""
            CREATE TABLE ad_analytics.Dim_CRMStatus_new
            ENGINE = MergeTree
            ORDER BY crm_status_key
            AS
            SELECT
                crm_status_key,
                anyLast(`Название crm`) AS `Название crm`,
                anyLast(`тип_заявки`) AS `тип_заявки`,
                anyLast(`статус`) AS `статус`,
                anyLast(cascade_level) AS cascade_level
            FROM
            (
                SELECT
                    {_dimension_key_sql(CRM_STATUS_KEY_COLUMNS, "crm_status_key")},
                    CAST({_canonical_crm_name_sql("`Название crm`")}, 'String') AS `Название crm`,
                    `тип_заявки`,
                    `статус`,
                    cascade_level
                FROM ad_analytics.big_analytics_unified
            )
            GROUP BY crm_status_key
        """,
        "Dim_Salon": f"""
            CREATE TABLE ad_analytics.Dim_Salon_new
            ENGINE = MergeTree
            ORDER BY salon_key
            AS
            SELECT
                salon_key,
                anyLast(`салон`) AS `салон`,
                anyLast(`город`) AS `город`,
                anyLast(`регион`) AS `регион`,
                anyLast(`тип_сайта`) AS `тип_сайта`,
                anyLast(`шаблон`) AS `шаблон`,
                anyLast(`специалист`) AS `специалист`,
                anyLast(nullIf(trim(BOTH ' ' FROM ifNull(`проджект`, '')), '')) AS `проджект`,
                anyLast(nullIf(trim(BOTH ' ' FROM ifNull(`менеджер`, '')), '')) AS `менеджер`,
                anyLast(`id_салона`) AS `id_салона`,
                anyLast(`направление`) AS `направление`
            FROM
            (
                SELECT
                    {_dimension_key_sql(SALON_KEY_COLUMNS, "salon_key")},
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
                FROM ad_analytics.big_analytics_unified
            )
            GROUP BY salon_key
        """,
}


def build_dim_adgroup(client) -> int:
    shadow = "ad_analytics.Dim_AdGroup_new"
    stage = "ad_analytics.Dim_AdGroup_fact_parts_new"
    source_table = (
        "ad_analytics.big_analytics_sources"
        if table_exists(client, "ad_analytics", "big_analytics_sources")
        else "ad_analytics.big_analytics_unified"
    )
    if source_table.endswith("big_analytics_unified"):
        log.info("  Dim_AdGroup: big_analytics_sources отсутствует, использую big_analytics_unified")
    client.command(f"DROP TABLE IF EXISTS {shadow} SYNC")
    client.command(f"DROP TABLE IF EXISTS {stage} SYNC")
    client.command(
        f"""
        CREATE TABLE {stage}
        ENGINE = MergeTree
        ORDER BY AdGroupId
        AS
        SELECT
            toInt64(0) AS AdGroupId,
            CAST(NULL, 'Nullable(String)') AS AdGroupName,
            CAST(NULL, 'Nullable(String)') AS adgroup_code,
            '' AS `марки авто`,
            '' AS ag_part1,
            '' AS ag_part2,
            '' AS ag_part3,
            '' AS ag_part4,
            '' AS ag_part5,
            '' AS ag_part6,
            '' AS ag_part7,
            '' AS `номер группы | название группы`,
            CAST(NULL, 'Nullable(String)') AS `неверный_кодер_new`,
            toInt64(0) AS parent_CampaignId
        WHERE 0
        """,
        settings=SAFE_QUERY_SETTINGS,
    )

    ranges = day_ranges(DATE_FROM)
    for idx, (lo, hi) in enumerate(ranges, start=1):
        client.command(
            f"""
            INSERT INTO {stage}
            SELECT
                AdGroupId,
                anyLast(AdGroupName) AS AdGroupName,
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
                anyLast(CampaignId) AS parent_CampaignId
            FROM {source_table}
            WHERE Date >= toDate('{lo}')
              AND Date < toDate('{hi}')
              AND AdGroupId != 0
            GROUP BY AdGroupId
            """,
            settings=SAFE_QUERY_SETTINGS,
        )
        if idx == len(ranges) or idx % 30 == 0:
            log.info("  Dim_AdGroup fact batch %d/%d: %s -> %s", idx, len(ranges), lo, hi)

    client.command(
        f"""
        CREATE TABLE {shadow}
        ENGINE = MergeTree
        ORDER BY ifNull(AdGroupId, 0)
        AS SELECT * FROM {stage} WHERE 0
        """,
        settings=SAFE_QUERY_SETTINGS,
    )

    # Probe 2026-08-17: one merge bucket passed under the CH memory cap and was
    # faster than 8/16 buckets. Re-split only if this join starts hitting memory.
    bucket_count = 1
    for bucket in range(bucket_count):
        raw_filter = "group_id != 0"
        fact_filter = "1 = 1"
        if bucket_count > 1:
            raw_filter += f" AND modulo(group_id, {bucket_count}) = {bucket}"
            fact_filter = f"modulo(AdGroupId, {bucket_count}) = {bucket}"
        client.command(
            f"""
            INSERT INTO {shadow}
            WITH
            raw_adgroups AS
            (
                SELECT
                    group_id AS AdGroupId,
                    argMax(group_name, synced_at) AS AdGroupName,
                    argMax(campaign_id, synced_at) AS parent_CampaignId
                FROM reference_data.direct_adgroups
                WHERE {raw_filter}
                GROUP BY AdGroupId
            ),
            fact_adgroups AS
            (
                SELECT
                    AdGroupId,
                    anyLast(AdGroupName) AS AdGroupName,
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
                    anyLast(parent_CampaignId) AS parent_CampaignId
                FROM {stage}
                WHERE {fact_filter}
                GROUP BY AdGroupId
            )
            SELECT
                r.AdGroupId,
                coalesce(r.AdGroupName, f.AdGroupName) AS AdGroupName,
                f.adgroup_code AS adgroup_code,
                ifNull(f.`марки авто`, '') AS `марки авто`,
                ifNull(f.ag_part1, '') AS ag_part1,
                ifNull(f.ag_part2, '') AS ag_part2,
                ifNull(f.ag_part3, '') AS ag_part3,
                ifNull(f.ag_part4, '') AS ag_part4,
                ifNull(f.ag_part5, '') AS ag_part5,
                ifNull(f.ag_part6, '') AS ag_part6,
                ifNull(f.ag_part7, '') AS ag_part7,
                coalesce(
                    nullIf(trim(BOTH ' ' FROM ifNull(f.`номер группы | название группы`, '')), ''),
                    concat(toString(r.AdGroupId), ' | ', ifNull(r.AdGroupName, ''))
                ) AS `номер группы | название группы`,
                f.`неверный_кодер_new` AS `неверный_кодер_new`,
                ifNull(r.parent_CampaignId, f.parent_CampaignId) AS parent_CampaignId
            FROM raw_adgroups r
            LEFT JOIN fact_adgroups f ON f.AdGroupId = r.AdGroupId

            UNION ALL

            SELECT
                f.AdGroupId,
                f.AdGroupName,
                f.adgroup_code,
                ifNull(f.`марки авто`, '') AS `марки авто`,
                ifNull(f.ag_part1, '') AS ag_part1,
                ifNull(f.ag_part2, '') AS ag_part2,
                ifNull(f.ag_part3, '') AS ag_part3,
                ifNull(f.ag_part4, '') AS ag_part4,
                ifNull(f.ag_part5, '') AS ag_part5,
                ifNull(f.ag_part6, '') AS ag_part6,
                ifNull(f.ag_part7, '') AS ag_part7,
                coalesce(
                    nullIf(trim(BOTH ' ' FROM ifNull(f.`номер группы | название группы`, '')), ''),
                    concat(toString(f.AdGroupId), ' | ', ifNull(f.AdGroupName, ''))
                ) AS `номер группы | название группы`,
                f.`неверный_кодер_new`,
                f.parent_CampaignId
            FROM fact_adgroups f
            LEFT JOIN raw_adgroups r ON r.AdGroupId = f.AdGroupId
            WHERE r.AdGroupId = 0
            """,
            settings=SAFE_QUERY_SETTINGS,
        )
        if bucket == bucket_count - 1 or (bucket + 1) % 16 == 0:
            log.info("  Dim_AdGroup merge bucket %d/%d", bucket + 1, bucket_count)
    swap_shadow(client, "ad_analytics.Dim_AdGroup", shadow)
    client.command(f"DROP TABLE IF EXISTS {stage} SYNC")
    rows = count_rows(client, "ad_analytics.Dim_AdGroup")
    log.info("  Dim_AdGroup=%d", rows)
    return rows


def build_dim_campaign(client, bucket_count: int = 1) -> int:
    """Build Dim_Campaign, optionally split by CampaignId buckets if memory requires it."""
    table = "Dim_Campaign"
    shadow = f"ad_analytics.{table}_new"
    client.command(f"DROP TABLE IF EXISTS {shadow} SYNC")
    client.command(
        f"""
        CREATE TABLE {shadow}
        ENGINE = MergeTree
        ORDER BY ifNull(CampaignId, 0)
        AS
        SELECT
            `CampaignId`,
            anyLast(`CampaignName`) AS `CampaignName`,
            anyLast(account_login) AS account_login,
            replaceAll(anyLast(campaign_code), 'kviz', 'quiz') AS campaign_code,
            anyLast(tp) AS tp,
            anyLast(cpc_cpa) AS cpc_cpa,
            anyLast(site_quiz) AS site_quiz,
            anyLast(campaign_status) AS campaign_status,
            anyLast(payment_model) AS payment_model,
            coalesce(
                nullIf(trim(BOTH ' ' FROM ifNull(anyLast(`номер кампании | название кампании`), '')), ''),
                concat(toString(`CampaignId`), ' | ', ifNull(anyLast(`CampaignName`), ''))
            ) AS `номер кампании | название кампании`
        FROM ad_analytics.big_analytics_unified
        WHERE 0 AND `CampaignId` IS NOT NULL
        GROUP BY `CampaignId`
        """,
        settings=SAFE_QUERY_SETTINGS,
    )
    for bucket in range(bucket_count):
        bucket_filter = "`CampaignId` IS NOT NULL"
        if bucket_count > 1:
            bucket_filter += f" AND modulo(ifNull(`CampaignId`, 0), {bucket_count}) = {bucket}"
        client.command(
            f"""
            INSERT INTO {shadow}
            SELECT
                `CampaignId`,
                anyLast(`CampaignName`) AS `CampaignName`,
                anyLast(account_login) AS account_login,
                replaceAll(anyLast(campaign_code), 'kviz', 'quiz') AS campaign_code,
                anyLast(tp) AS tp,
                anyLast(cpc_cpa) AS cpc_cpa,
                anyLast(site_quiz) AS site_quiz,
                anyLast(campaign_status) AS campaign_status,
                anyLast(payment_model) AS payment_model,
                coalesce(
                    nullIf(trim(BOTH ' ' FROM ifNull(anyLast(`номер кампании | название кампании`), '')), ''),
                    concat(toString(`CampaignId`), ' | ', ifNull(anyLast(`CampaignName`), ''))
                ) AS `номер кампании | название кампании`
            FROM ad_analytics.big_analytics_unified
            WHERE {bucket_filter}
            GROUP BY `CampaignId`
            """,
            settings=SAFE_QUERY_SETTINGS,
        )
        if bucket == bucket_count - 1 or (bucket + 1) % 16 == 0:
            log.info("  Dim_Campaign bucket %d/%d", bucket + 1, bucket_count)
    swap_shadow(client, "ad_analytics.Dim_Campaign", shadow)
    rows = count_rows(client, "ad_analytics.Dim_Campaign")
    log.info("  Dim_Campaign=%d", rows)
    return rows


def build_dim(client, table: str) -> int:
    if table not in DIM_DDL:
        available = ", ".join(sorted(DIM_DDL))
        raise ValueError(f"Unknown dimension {table!r}. Available: {available}")
    if table == "Dim_Campaign":
        return build_dim_campaign(client)
    if table == "Dim_AdGroup":
        return build_dim_adgroup(client)
    shadow = f"ad_analytics.{table}_new"
    client.command(f"DROP TABLE IF EXISTS {shadow} SYNC")
    client.command(DIM_DDL[table], settings=SAFE_QUERY_SETTINGS)
    swap_shadow(client, f"ad_analytics.{table}", shadow)
    rows = count_rows(client, f"ad_analytics.{table}")
    log.info("  %s=%d", table, rows)
    return rows


def build_dims(client, tables: list[str] | None = None) -> dict[str, int]:
    rows: dict[str, int] = {}
    for table in (tables or list(DIM_DDL)):
        t_table = time.perf_counter()
        rows[table] = build_dim(client, table)
        log.info("  %s built in %.1f sec", table, time.perf_counter() - t_table)
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
            FROM reference_data.gsheet_sites
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
        WHERE s.date >= '{DATE_FROM}'
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
                f"AND s.date >= '{lo}' AND s.date < '{hi}'",
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


def build_extension_dims(client) -> dict[str, int]:
    from star_refactor import build_star_extensions

    builders = {
        "Dim_AdFormat": build_star_extensions.build_dim_adformat,
        "Dim_AdNetworkType": build_star_extensions.build_dim_adnetwork,
        "Dim_Device": build_star_extensions.build_dim_device,
        "Dim_Source": build_star_extensions.build_dim_source,
    }
    rows: dict[str, int] = {}
    for table, builder in builders.items():
        t_table = time.perf_counter()
        rows[table] = builder(client)
        log.info("  %s built in %.1f sec", table, time.perf_counter() - t_table)
    return rows


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    log.info("build_star v6_ch: ClickHouse star tables")
    client = get_client()
    t0 = time.perf_counter()
    if not table_exists(client, "ad_analytics", "big_analytics_unified"):
        raise RuntimeError("ad_analytics.big_analytics_unified отсутствует")
    t_part = time.perf_counter()
    dim_rows = build_dims(client)
    log.info("build_star dims total %.1f sec", time.perf_counter() - t_part)
    t_part = time.perf_counter()
    extension_dim_rows = build_extension_dims(client)
    log.info("build_star extension dims total %.1f sec", time.perf_counter() - t_part)
    t_part = time.perf_counter()
    vk_rows = build_vk_ads_fact(client)
    log.info("build_star fact_vk_ads total %.1f sec", time.perf_counter() - t_part)
    t_part = time.perf_counter()
    vk_dim_rows = build_vk_dims(client)
    log.info("build_star vk dims total %.1f sec", time.perf_counter() - t_part)
    t_part = time.perf_counter()
    ml_rows = build_ml_korrektirovki_fact(client)
    log.info("build_star fact_ml_korrektirovki total %.1f sec", time.perf_counter() - t_part)
    t_part = time.perf_counter()
    fact_rows = build_fact(client)
    log.info("build_star fact_big_analytics total %.1f sec", time.perf_counter() - t_part)
    parts = [
        f"fact_big_analytics={fact_rows:,}",
        *[f"{k}={v:,}" for k, v in dim_rows.items()],
        *[f"{k}={v:,}" for k, v in extension_dim_rows.items()],
        f"fact_vk_ads={vk_rows:,}",
        *[f"{k}={v:,}" for k, v in vk_dim_rows.items()],
        f"fact_ml_korrektirovki={ml_rows:,}",
    ]
    details = ", ".join(parts)
    log.info("build_star v6_ch завершён за %.1f сек: %s", time.perf_counter() - t0, details)
    return {"rows": fact_rows, "details": details}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--only-dim",
        action="append",
        choices=sorted(DIM_DDL),
        help="Rebuild only the selected dimension. Can be passed more than once.",
    )
    args = parser.parse_args(argv)
    if args.only_dim:
        client = get_client()
        rows = build_dims(client, args.only_dim)
        details = ", ".join(f"{key}={value:,}" for key, value in rows.items())
        log.info("selected dimensions rebuilt: %s", details)
        return
    run()


if __name__ == "__main__":
    main()
