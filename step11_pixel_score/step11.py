"""Step 11 for v6_ch: pixel attribution materialization in ClickHouse."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_settings import DATE_FROM
from config.ch_utils import SAFE_QUERY_SETTINGS, column_names, count_rows, day_ranges, q, swap_shadow
from step3_build_sources.step3 import SOURCE_STORE, _gs_account_cte

logger = logging.getLogger("pipeline.step11")


def _key_expr(alias: str = "s") -> str:
    return (
        f"concat(ifNull(toString({alias}.`Date`), ''), '|', ifNull({alias}.domain, ''), '|', "
        f"'пиксель_атрибуц', '|', ifNull(toString({alias}.`CampaignId`), ''))"
    )


def _create_empty_from_full(client, target: str) -> None:
    client.command(f"DROP TABLE IF EXISTS {target} SYNC")
    client.command(
        f"""
        CREATE TABLE {target}
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(ifNull(`Date`, toDate('2026-01-01')))
        ORDER BY (ifNull(`Date`, toDate('2026-01-01')), ifNull(domain, ''), ifNull(_source_table, ''))
        AS SELECT * FROM ad_analytics.big_analytics_full WHERE 0
        """,
        settings=SAFE_QUERY_SETTINGS,
    )


def _weekday_expr(date_expr: str) -> str:
    return (
        f"multiIf(toDayOfWeek({date_expr}) = 1, '1_Понедельник', "
        f"toDayOfWeek({date_expr}) = 2, '2_Вторник', "
        f"toDayOfWeek({date_expr}) = 3, '3_Среда', "
        f"toDayOfWeek({date_expr}) = 4, '4_Четверг', "
        f"toDayOfWeek({date_expr}) = 5, '5_Пятница', "
        f"toDayOfWeek({date_expr}) = 6, '6_Суббота', '7_Воскресенье')"
    )


def _insert_weighted_pixel_sql(target: str, lo: str, hi: str) -> str:
    return f"""
INSERT INTO {target}
WITH
{_gs_account_cte()},
toStartOfMonth(toDate('{lo}')) AS batch_month,
addMonths(batch_month, 1) AS next_month,
campaign_monthly AS
(
    SELECT
        toStartOfMonth(`Date`) AS month,
        `салон`,
        domain,
        `источник`,
        `_source_table`,
        `CampaignId`,
        sum(ifNull(`Clicks`, 0)) AS clicks,
        sum(ifNull(total_cost, 0)) AS cost,
        sum(ifNull(kol_vo_zayavok, 0)) AS zayavki,
        sum(ifNull(korr, 0)) AS korr,
        sum(ifNull(kval, 0)) AS kval,
        sum(ifNull(priezd, 0)) AS priezd,
        sum(ifNull(prodazhi, 0)) AS prodazhi
    FROM ad_analytics.big_analytics_full
    WHERE `_source_table` IN ('direct', 'crop_targeting', 'tp8', 'tp9', 'tp10')
      AND `CampaignId` != 0
      AND domain IS NOT NULL
      AND `салон` IS NOT NULL
      AND `Date` IS NOT NULL
      AND `Date` >= batch_month AND `Date` < next_month
    GROUP BY month, `салон`, domain, `источник`, `_source_table`, `CampaignId`
    HAVING cost > 0 AND clicks > 0
),
campaign_score AS
(
    SELECT
        *,
        greatest(
            toDecimal64(0.000001, 6),
            (zayavki + 3 * korr + 10 * kval + 30 * priezd + 100 * prodazhi) / nullIf(clicks, 0)
        ) AS score
    FROM campaign_monthly
),
score_weights AS
(
    SELECT
        month,
        domain,
        `CampaignId`,
        score / nullIf(sum(score) OVER (PARTITION BY month, domain), 0) AS weight
    FROM campaign_score
),
pixel_daily AS
(
    SELECT
        `Date`,
        toStartOfMonth(`Date`) AS month,
        `салон`,
        domain,
        sum(ifNull(total_cost, 0)) AS px_cost,
        sum(ifNull(kol_vo_zayavok, 0)) AS px_zayavki,
        sum(ifNull(korr, 0)) AS px_korr,
        sum(ifNull(kval, 0)) AS px_kval,
        sum(ifNull(priezd, 0)) AS px_priezd,
        sum(ifNull(prodazhi, 0)) AS px_prodazhi
    FROM ad_analytics.{SOURCE_STORE}
    WHERE `_source_table` = 'pixel'
      AND `Date` IS NOT NULL
      AND `Date` >= toDate('{lo}') AND `Date` < toDate('{hi}')
      AND domain IS NOT NULL
      AND `салон` IS NOT NULL
    GROUP BY `Date`, month, `салон`, domain
),
campaign_attrs AS
(
    SELECT
        `CampaignId`,
        anyLast(`CampaignName`) AS `CampaignName`,
        anyLast(campaign_code) AS campaign_code,
        anyLast(tp) AS tp,
        anyLast(cpc_cpa) AS cpc_cpa,
        anyLast(site_quiz) AS site_quiz,
        anyLast(account_login) AS account_login,
        anyLast(manager_login) AS manager_login,
        anyLast(`марки авто`) AS `марки авто`,
        anyLast(`Название crm`) AS `Название crm`,
        anyLast(`статус`) AS `статус`,
        anyLast(`специалист`) AS `специалист`,
        anyLast(`тип_сайта`) AS `тип_сайта`,
        anyLast(`шаблон`) AS `шаблон`,
        anyLast(`салон`) AS `салон`,
        anyLast(`город`) AS `город`,
        anyLast(`регион`) AS `регион`,
        anyLast(direction) AS direction,
        anyLast(`проджект`) AS `проджект`,
        anyLast(`id_салона`) AS `id_салона`,
        anyLast(`менеджер`) AS `менеджер`,
        anyLast(`номер кампании | название кампании`) AS `номер кампании | название кампании`,
        anyLast(`аккаунт|сайт`) AS `аккаунт|сайт`,
        anyLast(`поставщик`) AS `поставщик`,
        anyLast(campaign_status) AS campaign_status,
        anyLast(payment_model) AS payment_model
    FROM ad_analytics.big_analytics_full
    WHERE `_source_table` IN ('direct', 'crop_targeting', 'tp8', 'tp9', 'tp10')
      AND `CampaignId` != 0
      AND `Date` >= batch_month AND `Date` < next_month
    GROUP BY `CampaignId`
),
attributed AS
(
    SELECT
        '' AS key3,
        p.`Date`,
        {_weekday_expr("p.`Date`")} AS `День недели`,
        toStartOfWeek(p.`Date`, 1) AS week_start,
        sw.`CampaignId`,
        ca.`CampaignName`,
        toInt64(0) AS `AdGroupId`,
        CAST(NULL, 'Nullable(String)') AS `AdGroupName`,
        CAST(NULL, 'Nullable(String)') AS `AdNetworkType`,
        CAST(NULL, 'Nullable(String)') AS `Device`,
        toDecimal64(0, 6) AS `Impressions`,
        toDecimal64(0, 6) AS `Clicks`,
        p.px_cost * sw.weight AS total_cost,
        p.domain,
        toInt64(0) AS `RlAdjustmentId`,
        '' AS `RlAdjustmentId_total`,
        ca.campaign_code,
        ifNull(ca.tp, '') AS tp,
        ifNull(ca.cpc_cpa, '') AS cpc_cpa,
        ifNull(ca.site_quiz, '') AS site_quiz,
        CAST(NULL, 'Nullable(String)') AS adgroup_code,
        ifNull(ca.account_login, '') AS account_login,
        ca.manager_login,
        '' AS ag_part1, '' AS ag_part2, '' AS ag_part3, '' AS ag_part4, '' AS ag_part5, '' AS ag_part6, '' AS ag_part7,
        ifNull(ca.`марки авто`, '') AS `марки авто`,
        ifNull(ca.`Название crm`, '') AS `Название crm`,
        'Пиксель_атрибуц' AS `тип_заявки`,
        p.px_zayavki * sw.weight AS kol_vo_zayavok,
        p.px_korr * sw.weight AS korr,
        p.px_kval * sw.weight AS kval,
        p.px_priezd * sw.weight AS priezd,
        p.px_prodazhi * sw.weight AS prodazhi,
        toDecimal64(0, 6) AS nekorr,
        toDecimal64(0, 6) AS ne_otvechaet,
        toDecimal64(0, 6) AS filtr,
        toDecimal64(0, 6) AS nedozvon,
        toDecimal64(0, 6) AS priedet,
        toInt64(0) AS dohod_do_kredita,
        toInt64(0) AS dobro,
        coalesce(nullIf(ca.`статус`, ''), gs_pix.status) AS `статус`,
        coalesce(nullIf(ca.`специалист`, ''), gs_pix.directologist) AS `специалист`,
        coalesce(nullIf(ca.`тип_сайта`, ''), gs_pix.site_type) AS `тип_сайта`,
        coalesce(nullIf(ca.`шаблон`, ''), gs_pix.template) AS `шаблон`,
        coalesce(nullIf(ca.`салон`, ''), nullIf(p.`салон`, ''), gs_pix.salon) AS `салон`,
        coalesce(nullIf(ca.`город`, ''), gs_pix.city) AS `город`,
        coalesce(nullIf(ca.`регион`, ''), gs_pix.region) AS `регион`,
        'Авто' AS direction,
        CAST(NULL, 'Nullable(String)') AS `неверный_кодер_new`,
        CAST(NULL, 'Nullable(String)') AS fid,
        coalesce(nullIf(ca.`проджект`, ''), gs_pix.project_manager) AS `проджект`,
        coalesce(nullIf(ca.`id_салона`, ''), gs_pix.client_id) AS `id_салона`,
        coalesce(nullIf(ca.`менеджер`, ''), gs_pix.sales_manager) AS `менеджер`,
        'Пиксель_атрибуц' AS `источник`,
        'Пиксель_атрибуц' AS `направление`,
        ifNull(ca.`номер кампании | название кампании`, concat(toString(sw.`CampaignId`), '|')) AS `номер кампании | название кампании`,
        '' AS `номер группы | название группы`,
        CAST(NULL, 'Nullable(Int32)') AS `План заявки`,
        CAST(NULL, 'Nullable(Int32)') AS `План приезда`,
        ifNull(ca.`аккаунт|сайт`, concat(ifNull(gs_pix.login_key, ''), '|', ifNull(p.domain, ''))) AS `аккаунт|сайт`,
        CAST(NULL, 'Nullable(Int64)') AS priezd_arrival_date,
        CAST(NULL, 'Nullable(Int64)') AS prodazhi_arrival_date,
        ifNull(ca.`поставщик`, 'Victory') AS `поставщик`,
        'пиксель_атрибуц' AS `_source_table`,
        CAST(NULL, 'Nullable(String)') AS cascade_level,
        ca.campaign_status,
        ca.payment_model,
        concat(toString(p.`Date`), '|', ifNull(p.domain, ''), '|пиксель_атрибуц|', toString(sw.`CampaignId`)) AS key_pixel_score
    FROM pixel_daily p
    INNER JOIN score_weights sw
      ON sw.month = p.month AND sw.domain = p.domain
    LEFT JOIN campaign_attrs ca ON ca.`CampaignId` = sw.`CampaignId`
    LEFT JOIN gs_domain gs_pix ON gs_pix.domain_key = lower(trim(ifNull(p.domain, '')))
),
leftovers AS
(
    SELECT
        '' AS key3,
        p.`Date`,
        {_weekday_expr("p.`Date`")} AS `День недели`,
        toStartOfWeek(p.`Date`, 1) AS week_start,
        toInt64(0) AS `CampaignId`,
        CAST(NULL, 'Nullable(String)') AS `CampaignName`,
        toInt64(0) AS `AdGroupId`,
        CAST(NULL, 'Nullable(String)') AS `AdGroupName`,
        CAST(NULL, 'Nullable(String)') AS `AdNetworkType`,
        CAST(NULL, 'Nullable(String)') AS `Device`,
        toDecimal64(0, 6) AS `Impressions`,
        toDecimal64(0, 6) AS `Clicks`,
        p.px_cost AS total_cost,
        p.domain,
        toInt64(0) AS `RlAdjustmentId`,
        '' AS `RlAdjustmentId_total`,
        CAST(NULL, 'Nullable(String)') AS campaign_code,
        '' AS tp,
        '' AS cpc_cpa,
        '' AS site_quiz,
        CAST(NULL, 'Nullable(String)') AS adgroup_code,
        '' AS account_login,
        CAST(NULL, 'Nullable(String)') AS manager_login,
        '' AS ag_part1, '' AS ag_part2, '' AS ag_part3, '' AS ag_part4, '' AS ag_part5, '' AS ag_part6, '' AS ag_part7,
        '' AS `марки авто`,
        '' AS `Название crm`,
        'Пиксель_атрибуц' AS `тип_заявки`,
        p.px_zayavki AS kol_vo_zayavok,
        p.px_korr AS korr,
        p.px_kval AS kval,
        p.px_priezd AS priezd,
        p.px_prodazhi AS prodazhi,
        toDecimal64(0, 6) AS nekorr,
        toDecimal64(0, 6) AS ne_otvechaet,
        toDecimal64(0, 6) AS filtr,
        toDecimal64(0, 6) AS nedozvon,
        toDecimal64(0, 6) AS priedet,
        toInt64(0) AS dohod_do_kredita,
        toInt64(0) AS dobro,
        gs_pix.status AS `статус`,
        gs_pix.directologist AS `специалист`,
        gs_pix.site_type AS `тип_сайта`,
        gs_pix.template AS `шаблон`,
        coalesce(nullIf(p.`салон`, ''), gs_pix.salon) AS `салон`,
        gs_pix.city AS `город`,
        gs_pix.region AS `регион`,
        'Авто' AS direction,
        CAST(NULL, 'Nullable(String)') AS `неверный_кодер_new`,
        CAST(NULL, 'Nullable(String)') AS fid,
        gs_pix.project_manager AS `проджект`,
        gs_pix.client_id AS `id_салона`,
        gs_pix.sales_manager AS `менеджер`,
        'Пиксель_атрибуц' AS `источник`,
        'Пиксель_атрибуц' AS `направление`,
        '' AS `номер кампании | название кампании`,
        '' AS `номер группы | название группы`,
        CAST(NULL, 'Nullable(Int32)') AS `План заявки`,
        CAST(NULL, 'Nullable(Int32)') AS `План приезда`,
        concat(ifNull(gs_pix.login_key, ''), '|', ifNull(p.domain, '')) AS `аккаунт|сайт`,
        CAST(NULL, 'Nullable(Int64)') AS priezd_arrival_date,
        CAST(NULL, 'Nullable(Int64)') AS prodazhi_arrival_date,
        'Victory' AS `поставщик`,
        'пиксель_атрибуц' AS `_source_table`,
        CAST(NULL, 'Nullable(String)') AS cascade_level,
        CAST(NULL, 'LowCardinality(Nullable(String))') AS campaign_status,
        CAST(NULL, 'LowCardinality(Nullable(String))') AS payment_model,
        concat(toString(p.`Date`), '|', ifNull(p.domain, ''), '|пиксель_атрибуц|0') AS key_pixel_score
    FROM pixel_daily p
    LEFT JOIN gs_domain gs_pix ON gs_pix.domain_key = lower(trim(ifNull(p.domain, '')))
    WHERE (p.month, p.domain) NOT IN (
        SELECT month, domain FROM score_weights
    )
)
SELECT * FROM attributed
UNION ALL
SELECT * FROM leftovers
"""


def _direct_pixel_select_cols(cols: list[str]) -> str:
    fallback = {
        "статус": "coalesce(nullIf(s.`статус`, ''), gs.status)",
        "специалист": "coalesce(nullIf(s.`специалист`, ''), gs.directologist)",
        "тип_сайта": "coalesce(nullIf(s.`тип_сайта`, ''), gs.site_type)",
        "шаблон": "coalesce(nullIf(s.`шаблон`, ''), gs.template)",
        "салон": "coalesce(nullIf(s.`салон`, ''), gs.salon)",
        "город": "coalesce(nullIf(s.`город`, ''), gs.city)",
        "регион": "coalesce(nullIf(s.`регион`, ''), gs.region)",
        "direction": "coalesce(nullIf(s.direction, ''), gs.direction)",
        "проджект": "coalesce(nullIf(s.`проджект`, ''), gs.project_manager)",
        "id_салона": "coalesce(nullIf(s.`id_салона`, ''), gs.client_id)",
        "менеджер": "coalesce(nullIf(s.`менеджер`, ''), gs.sales_manager)",
        "account_login": "coalesce(nullIf(s.account_login, ''), gs.login_key, '')",
        "manager_login": "coalesce(nullIf(s.manager_login, ''), gs.directologist)",
        "аккаунт|сайт": "concat(coalesce(nullIf(s.account_login, ''), gs.login_key, ''), '|', ifNull(s.domain, ''))",
        "key_pixel_score": "CAST(NULL, 'Nullable(String)')",
    }
    return ", ".join(f"{fallback.get(col, f's.{q(col)}')} AS {q(col)}" for col in cols)


def _rebuild_pixel_score(client) -> int:
    shadow = "ad_analytics.big_analytics_pixel_score_new"
    _create_empty_from_full(client, shadow)
    pixel_ranges = day_ranges(DATE_FROM)
    for idx, (lo, hi) in enumerate(pixel_ranges, start=1):
        client.command(_insert_weighted_pixel_sql(shadow, lo, hi), settings=SAFE_QUERY_SETTINGS)
        logger.info("  pixel_score daily batch %d/%d inserted: %s -> %s", idx, len(pixel_ranges), lo, hi)
    swap_shadow(client, "ad_analytics.big_analytics_pixel_score", shadow)
    return count_rows(client, "ad_analytics.big_analytics_pixel_score")


def _rebuild_full_with_pixel(client) -> int:
    cols = column_names(client, "ad_analytics", "big_analytics_full")
    cols_sql = ", ".join(q(col) for col in cols)
    shadow = "ad_analytics.big_analytics_full_new"
    _create_empty_from_full(client, shadow)

    full_ranges = day_ranges(DATE_FROM)
    for idx, (lo, hi) in enumerate(full_ranges, start=1):
        client.command(
            f"""
            INSERT INTO {shadow} ({cols_sql})
            SELECT {cols_sql}
            FROM ad_analytics.big_analytics_full
            WHERE `Date` >= toDate('{lo}') AND `Date` < toDate('{hi}')
              AND _source_table NOT IN ('pixel', 'пиксель_атрибуц')
            """,
            settings=SAFE_QUERY_SETTINGS,
        )
        logger.info("  full keep daily batch %d/%d inserted: %s -> %s", idx, len(full_ranges), lo, hi)

    pixel_ranges = day_ranges(DATE_FROM)
    for idx, (lo, hi) in enumerate(pixel_ranges, start=1):
        client.command(
            f"""
            INSERT INTO {shadow} ({cols_sql})
            SELECT {cols_sql}
            FROM ad_analytics.big_analytics_pixel_score
            WHERE `Date` >= toDate('{lo}') AND `Date` < toDate('{hi}')
            """,
            settings=SAFE_QUERY_SETTINGS,
        )
        logger.info("  full pixel daily batch %d/%d inserted: %s -> %s", idx, len(pixel_ranges), lo, hi)

    source_ranges = day_ranges(DATE_FROM)
    select_cols = _direct_pixel_select_cols(cols)
    for idx, (lo, hi) in enumerate(source_ranges, start=1):
        client.command(
            f"""
            INSERT INTO {shadow} ({cols_sql})
            WITH
            {_gs_account_cte()}
            SELECT {select_cols}
            FROM ad_analytics.{SOURCE_STORE} AS s
            LEFT JOIN gs_domain gs ON gs.domain_key = lower(trim(ifNull(s.domain, '')))
            WHERE s.`Date` >= toDate('{lo}') AND s.`Date` < toDate('{hi}')
              AND s._source_table = 'pixel'
            """,
            settings=SAFE_QUERY_SETTINGS,
        )
        logger.info("  full direct pixel daily batch %d/%d inserted: %s -> %s", idx, len(source_ranges), lo, hi)

    swap_shadow(client, "ad_analytics.big_analytics_full", shadow)
    return count_rows(client, "ad_analytics.big_analytics_full")


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    logger.info("Шаг 11 v6_ch: pixel_score + батчевая доливка в full")
    client = get_client()
    t0 = time.perf_counter()
    pixel_rows = _rebuild_pixel_score(client)
    full_rows = _rebuild_full_with_pixel(client)
    details = f"big_analytics_pixel_score={pixel_rows:,}, big_analytics_full={full_rows:,}"
    logger.info("Шаг 11 v6_ch завершён за %.1f сек: %s", time.perf_counter() - t0, details)
    return {"rows": full_rows, "details": details}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())
