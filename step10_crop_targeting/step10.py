"""Step 10 for v6_ch: add crop/Telega/VK cost overlays.

The lead funnel for crop/social traffic is already derived from raw leads in
step3/step6. Historical v5 cost logic also used separate cost tables: crop
Google Sheets before May, Telega.in lead table from May, and VK Ads spend. This
step adds those costs as zero-funnel overlay rows to avoid double-counting leads.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_utils import count_rows, replace_view, swap_shadow, table_exists
from step3_build_sources.step3 import SOURCE_STORE

logger = logging.getLogger("pipeline.step10")

COST_OVERLAY_TABLE = "big_analytics_cost_overlays"


_GS_DATE = "assumeNotNull(toDate(parseDateTimeBestEffortOrNull(ifNull(g.`Дата`, ''))))"
_GS_COST = (
    "ifNull(toDecimal64OrNull(replaceAll(replaceRegexpAll(ifNull(g.total_cost, ''), '[^0-9,.-]', ''), ',', '.'), 6), "
    "toDecimal64(0, 6))"
)
_GS_SOURCE = """
    coalesce(
        multiIf(
            trim(ifNull(g.`Источник`, '')) = 'Telegram', 'Посевы_Telegram',
            trim(ifNull(g.`Источник`, '')) = 'VK', 'Посевы_VK',
            trim(ifNull(g.`Источник`, '')) = 'Max', 'Посевы_Max',
            nullIf(trim(ifNull(g.`Источник`, '')), '')
        ),
        multiIf(
            match(lowerUTF8(ifNull(g.`utm утвержденная`, '')), '(^|_)vk($|_|[0-9])'), 'Посевы_VK',
            match(lowerUTF8(ifNull(g.`utm утвержденная`, '')), '(^|_)max($|_|[0-9])'), 'Посевы_Max',
            'Посевы_Telegram'
        )
    )
"""
_API_SOURCE = """
    multiIf(
        lowerUTF8(ifNull(t.`источник`, '')) IN ('telegram', 'instagram'), 'Посевы_Telegram',
        ifNull(t.`источник`, '') = 'VK', 'Посевы_VK',
        ifNull(t.`источник`, '') = 'Max', 'Посевы_Max',
        ifNull(t.`источник`, '') = '', 'Посевы_Telegram',
        concat('Посевы_', ifNull(t.`источник`, ''))
    )
"""


def _require(client, database: str, table: str) -> None:
    if not table_exists(client, database, table):
        raise RuntimeError(f"{database}.{table} отсутствует")


def _create_empty_overlay(client, shadow: str) -> None:
    client.command(f"DROP TABLE IF EXISTS {shadow} SYNC")
    client.command(
        f"""
        CREATE TABLE {shadow} AS ad_analytics.{SOURCE_STORE}
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(ifNull(`Date`, toDate('2026-01-01')))
        ORDER BY (ifNull(`Date`, toDate('2026-01-01')), ifNull(domain, ''), ifNull(key3, ''))
        """
    )


def _insert_crop_gsheet_costs(client, target: str) -> None:
    client.command(
        f"""
        INSERT INTO {target}
        WITH
        gs_salon AS
        (
            SELECT
                lowerUTF8(trim(ifNull(salon, ''))) AS salon_key,
                anyLast(project_manager) AS project_manager,
                anyLast(sales_manager) AS sales_manager,
                anyLast(client_id) AS client_id,
                anyLast(site_type) AS site_type,
                anyLast(city) AS city,
                anyLast(region) AS region
            FROM raw_data.gsheet_sites
            WHERE ifNull(salon, '') != ''
            GROUP BY salon_key
        ),
        gs_domain AS
        (
            SELECT
                lowerUTF8(trim(ifNull(domain, ''))) AS domain_key,
                anyLast(site_type) AS site_type,
                anyLast(city) AS city,
                anyLast(region) AS region,
                anyLast(login_key) AS login_key
            FROM raw_data.gsheet_sites
            WHERE ifNull(domain, '') != ''
            GROUP BY domain_key
        )
        SELECT
            concat('crop_cost|gs|', toString(g.id)) AS key3,
            {_GS_DATE} AS `Date`,
            multiIf(toDayOfWeek({_GS_DATE}) = 1, '1_Понедельник', toDayOfWeek({_GS_DATE}) = 2, '2_Вторник',
                    toDayOfWeek({_GS_DATE}) = 3, '3_Среда', toDayOfWeek({_GS_DATE}) = 4, '4_Четверг',
                    toDayOfWeek({_GS_DATE}) = 5, '5_Пятница', toDayOfWeek({_GS_DATE}) = 6, '6_Суббота', '7_Воскресенье') AS `День недели`,
            toStartOfWeek({_GS_DATE}, 1) AS week_start,
            toInt64(0) AS `CampaignId`,
            CAST(g.`Канал`, 'Nullable(String)') AS `CampaignName`,
            toInt64(0) AS `AdGroupId`,
            CAST(NULL, 'Nullable(String)') AS `AdGroupName`,
            CAST(NULL, 'Nullable(String)') AS `AdNetworkType`,
            CAST(NULL, 'Nullable(String)') AS `Device`,
            toDecimal64(0, 6) AS `Impressions`,
            toDecimal64(0, 6) AS `Clicks`,
            {_GS_COST} AS total_cost,
            CAST(g.`Сайт`, 'Nullable(String)') AS domain,
            toInt64(0) AS `RlAdjustmentId`,
            '' AS `RlAdjustmentId_total`,
            CAST('Посевы', 'Nullable(String)') AS campaign_code,
            '' AS tp,
            'посевы' AS cpc_cpa,
            'посевы' AS site_quiz,
            CAST(NULL, 'Nullable(String)') AS adgroup_code,
            '' AS account_login,
            CAST('посевы', 'Nullable(String)') AS manager_login,
            '' AS ag_part1, '' AS ag_part2, '' AS ag_part3, '' AS ag_part4, '' AS ag_part5, '' AS ag_part6, '' AS ag_part7,
            '' AS `марки авто`,
            '' AS `Название crm`,
            CAST('Заявки', 'Nullable(String)') AS `тип_заявки`,
            toDecimal256(0, 6) AS kol_vo_zayavok,
            toDecimal256(0, 6) AS korr,
            toDecimal256(0, 6) AS kval,
            toDecimal256(0, 6) AS priezd,
            toDecimal256(0, 6) AS prodazhi,
            toDecimal256(0, 6) AS nekorr,
            toDecimal256(0, 6) AS ne_otvechaet,
            toDecimal256(0, 6) AS filtr,
            toDecimal256(0, 6) AS nedozvon,
            toDecimal256(0, 6) AS priedet,
            toInt64(0) AS dohod_do_kredita,
            toInt64(0) AS dobro,
            CAST(NULL, 'Nullable(String)') AS `статус`,
            CAST(g.`Специалист`, 'Nullable(String)') AS `специалист`,
            CAST(coalesce(gd.site_type, gs.site_type), 'Nullable(String)') AS `тип_сайта`,
            CAST(NULL, 'Nullable(String)') AS `шаблон`,
            CAST(replaceAll(replaceAll(replaceAll(ifNull(g.`Гео`, ''), 'АЦ на Жукова', 'Автоцентр на Жукова'), 'АвтоПарк Южный', 'Автопарк Южный'), 'М-Авто', 'М-авто'), 'Nullable(String)') AS `салон`,
            CAST(coalesce(nullIf(g.`Гео2`, ''), gd.city, gs.city), 'Nullable(String)') AS `город`,
            CAST(coalesce(gd.region, gs.region), 'Nullable(String)') AS `регион`,
            CAST('Авто', 'Nullable(String)') AS direction,
            CAST(NULL, 'Nullable(String)') AS `неверный_кодер_new`,
            CAST(NULL, 'Nullable(String)') AS fid,
            CAST(gs.project_manager, 'Nullable(String)') AS `проджект`,
            CAST(gs.client_id, 'Nullable(String)') AS `id_салона`,
            CAST(gs.sales_manager, 'Nullable(String)') AS `менеджер`,
            {_GS_SOURCE} AS `источник`,
            'Комплекс' AS `направление`,
            '' AS `номер кампании | название кампании`,
            '' AS `номер группы | название группы`,
            CAST(NULL, 'Nullable(Int32)') AS `План заявки`,
            CAST(NULL, 'Nullable(Int32)') AS `План приезда`,
            concat(ifNull(gd.login_key, ''), '|', ifNull(g.`Сайт`, '')) AS `аккаунт|сайт`,
            CAST(NULL, 'Nullable(Int64)') AS priezd_arrival_date,
            CAST(NULL, 'Nullable(Int64)') AS prodazhi_arrival_date,
            ifNull(nullIf(trim(ifNull(g.`Тип закупа`, '')), ''), 'посевы') AS `поставщик`,
            'crop_targeting' AS _source_table,
            CAST('cost_overlay', 'Nullable(String)') AS cascade_level,
            CAST(NULL, 'Nullable(String)') AS campaign_status,
            CAST(NULL, 'Nullable(String)') AS payment_model
        FROM ad_analytics.gsheets_crop_targeting_account_leads g
        LEFT JOIN gs_salon gs ON gs.salon_key = lowerUTF8(trim(replaceAll(replaceAll(replaceAll(ifNull(g.`Гео`, ''), 'АЦ на Жукова', 'Автоцентр на Жукова'), 'АвтоПарк Южный', 'Автопарк Южный'), 'М-Авто', 'М-авто')))
        LEFT JOIN gs_domain gd ON gd.domain_key = lowerUTF8(trim(ifNull(g.`Сайт`, '')))
        WHERE parseDateTimeBestEffortOrNull(ifNull(g.`Дата`, '')) IS NOT NULL
          AND {_GS_DATE} >= toDate('2026-01-01')
          AND {_GS_DATE} < toDate('2026-05-01')
          AND {_GS_COST} != 0
        """
    )


def _insert_crop_api_costs(client, target: str) -> None:
    client.command(
        f"""
        INSERT INTO {target}
        WITH gs_salon AS
        (
            SELECT
                lowerUTF8(trim(ifNull(salon, ''))) AS salon_key,
                anyLast(project_manager) AS project_manager,
                anyLast(sales_manager) AS sales_manager,
                anyLast(client_id) AS client_id
            FROM raw_data.gsheet_sites
            WHERE ifNull(salon, '') != ''
            GROUP BY salon_key
        )
        SELECT
            concat('crop_cost|api|', toString(t.id)) AS key3,
            assumeNotNull(t.`Date`) AS `Date`,
            multiIf(toDayOfWeek(assumeNotNull(t.`Date`)) = 1, '1_Понедельник', toDayOfWeek(assumeNotNull(t.`Date`)) = 2, '2_Вторник',
                    toDayOfWeek(assumeNotNull(t.`Date`)) = 3, '3_Среда', toDayOfWeek(assumeNotNull(t.`Date`)) = 4, '4_Четверг',
                    toDayOfWeek(assumeNotNull(t.`Date`)) = 5, '5_Пятница', toDayOfWeek(assumeNotNull(t.`Date`)) = 6, '6_Суббота', '7_Воскресенье') AS `День недели`,
            toStartOfWeek(assumeNotNull(t.`Date`), 1) AS week_start,
            toInt64(0) AS `CampaignId`,
            CAST(t.CampaignName, 'Nullable(String)') AS `CampaignName`,
            toInt64(0) AS `AdGroupId`,
            CAST(NULL, 'Nullable(String)') AS `AdGroupName`,
            CAST(NULL, 'Nullable(String)') AS `AdNetworkType`,
            CAST(NULL, 'Nullable(String)') AS `Device`,
            toDecimal64(0, 6) AS `Impressions`,
            toDecimal64(0, 6) AS `Clicks`,
            toDecimal64(ifNull(t.total_cost, toDecimal64(0, 6)), 6) AS total_cost,
            CAST(t.domain, 'Nullable(String)') AS domain,
            toInt64(0) AS `RlAdjustmentId`,
            '' AS `RlAdjustmentId_total`,
            CAST('Посевы', 'Nullable(String)') AS campaign_code,
            '' AS tp,
            'посевы' AS cpc_cpa,
            'посевы' AS site_quiz,
            CAST(NULL, 'Nullable(String)') AS adgroup_code,
            '' AS account_login,
            CAST('посевы', 'Nullable(String)') AS manager_login,
            '' AS ag_part1, '' AS ag_part2, '' AS ag_part3, '' AS ag_part4, '' AS ag_part5, '' AS ag_part6, '' AS ag_part7,
            '' AS `марки авто`,
            '' AS `Название crm`,
            CAST('Заявки', 'Nullable(String)') AS `тип_заявки`,
            toDecimal256(0, 6) AS kol_vo_zayavok,
            toDecimal256(0, 6) AS korr,
            toDecimal256(0, 6) AS kval,
            toDecimal256(0, 6) AS priezd,
            toDecimal256(0, 6) AS prodazhi,
            toDecimal256(0, 6) AS nekorr,
            toDecimal256(0, 6) AS ne_otvechaet,
            toDecimal256(0, 6) AS filtr,
            toDecimal256(0, 6) AS nedozvon,
            toDecimal256(0, 6) AS priedet,
            toInt64(0) AS dohod_do_kredita,
            toInt64(0) AS dobro,
            CAST(t.`статус`, 'Nullable(String)') AS `статус`,
            CAST(t.`специалист`, 'Nullable(String)') AS `специалист`,
            CAST(t.`тип_сайта`, 'Nullable(String)') AS `тип_сайта`,
            CAST(t.`шаблон`, 'Nullable(String)') AS `шаблон`,
            CAST(t.`салон`, 'Nullable(String)') AS `салон`,
            CAST(t.`город`, 'Nullable(String)') AS `город`,
            CAST(t.`регион`, 'Nullable(String)') AS `регион`,
            CAST(coalesce(t.direction, 'Авто'), 'Nullable(String)') AS direction,
            CAST(NULL, 'Nullable(String)') AS `неверный_кодер_new`,
            CAST(NULL, 'Nullable(String)') AS fid,
            CAST(gs.project_manager, 'Nullable(String)') AS `проджект`,
            CAST(gs.client_id, 'Nullable(String)') AS `id_салона`,
            CAST(gs.sales_manager, 'Nullable(String)') AS `менеджер`,
            {_API_SOURCE} AS `источник`,
            'Комплекс' AS `направление`,
            '' AS `номер кампании | название кампании`,
            '' AS `номер группы | название группы`,
            CAST(NULL, 'Nullable(Int32)') AS `План заявки`,
            CAST(NULL, 'Nullable(Int32)') AS `План приезда`,
            concat('|', ifNull(t.domain, '')) AS `аккаунт|сайт`,
            CAST(NULL, 'Nullable(Int64)') AS priezd_arrival_date,
            CAST(NULL, 'Nullable(Int64)') AS prodazhi_arrival_date,
            ifNull(nullIf(trim(ifNull(t.`поставщик`, '')), ''), 'посевы') AS `поставщик`,
            'crop_targeting' AS _source_table,
            CAST('cost_overlay', 'Nullable(String)') AS cascade_level,
            CAST(NULL, 'Nullable(String)') AS campaign_status,
            CAST(NULL, 'Nullable(String)') AS payment_model
        FROM ad_analytics.crop_targeting_api_telegain_lead t
        LEFT JOIN gs_salon gs ON gs.salon_key = lowerUTF8(trim(ifNull(t.`салон`, '')))
        WHERE t.`Date` >= toDate('2026-05-01')
          AND ifNull(t.total_cost, toDecimal64(0, 6)) != 0
        """
    )


def _insert_vk_ads_costs(client, target: str) -> None:
    _require(client, "ad_analytics", "local_vk_ads_stats_day")
    client.command(
        f"""
        INSERT INTO {target}
        SELECT
            concat('vk_ads_cost|', toString(date), '|', toString(ifNull(account_id, 0)), '|', toString(ifNull(ad_plan_id, 0))) AS key3,
            date AS `Date`,
            multiIf(toDayOfWeek(date) = 1, '1_Понедельник', toDayOfWeek(date) = 2, '2_Вторник',
                    toDayOfWeek(date) = 3, '3_Среда', toDayOfWeek(date) = 4, '4_Четверг',
                    toDayOfWeek(date) = 5, '5_Пятница', toDayOfWeek(date) = 6, '6_Суббота', '7_Воскресенье') AS `День недели`,
            toStartOfWeek(date, 1) AS week_start,
            toInt64(ifNull(ad_plan_id, 0)) AS `CampaignId`,
            anyLast(ad_plan_name) AS `CampaignName`,
            toInt64(0) AS `AdGroupId`,
            CAST(NULL, 'Nullable(String)') AS `AdGroupName`,
            CAST(NULL, 'Nullable(String)') AS `AdNetworkType`,
            CAST(NULL, 'Nullable(String)') AS `Device`,
            toDecimal64(0, 6) AS `Impressions`,
            toDecimal64(0, 6) AS `Clicks`,
            toDecimal64(sum(spent), 6) AS total_cost,
            CAST(NULL, 'Nullable(String)') AS domain,
            toInt64(0) AS `RlAdjustmentId`,
            '' AS `RlAdjustmentId_total`,
            CAST('VK Ads', 'Nullable(String)') AS campaign_code,
            '' AS tp,
            'cpc' AS cpc_cpa,
            '' AS site_quiz,
            CAST(NULL, 'Nullable(String)') AS adgroup_code,
            '' AS account_login,
            CAST('VK Ads', 'Nullable(String)') AS manager_login,
            '' AS ag_part1, '' AS ag_part2, '' AS ag_part3, '' AS ag_part4, '' AS ag_part5, '' AS ag_part6, '' AS ag_part7,
            '' AS `марки авто`,
            '' AS `Название crm`,
            CAST('Заявки', 'Nullable(String)') AS `тип_заявки`,
            toDecimal256(0, 6) AS kol_vo_zayavok,
            toDecimal256(0, 6) AS korr,
            toDecimal256(0, 6) AS kval,
            toDecimal256(0, 6) AS priezd,
            toDecimal256(0, 6) AS prodazhi,
            toDecimal256(0, 6) AS nekorr,
            toDecimal256(0, 6) AS ne_otvechaet,
            toDecimal256(0, 6) AS filtr,
            toDecimal256(0, 6) AS nedozvon,
            toDecimal256(0, 6) AS priedet,
            toInt64(0) AS dohod_do_kredita,
            toInt64(0) AS dobro,
            CAST(NULL, 'Nullable(String)') AS `статус`,
            CAST(NULL, 'Nullable(String)') AS `специалист`,
            CAST(NULL, 'Nullable(String)') AS `тип_сайта`,
            CAST(NULL, 'Nullable(String)') AS `шаблон`,
            CAST(NULL, 'Nullable(String)') AS `салон`,
            CAST(NULL, 'Nullable(String)') AS `город`,
            CAST(NULL, 'Nullable(String)') AS `регион`,
            CAST('Авто', 'Nullable(String)') AS direction,
            CAST(NULL, 'Nullable(String)') AS `неверный_кодер_new`,
            CAST(NULL, 'Nullable(String)') AS fid,
            CAST(NULL, 'Nullable(String)') AS `проджект`,
            CAST(NULL, 'Nullable(String)') AS `id_салона`,
            CAST(NULL, 'Nullable(String)') AS `менеджер`,
            'VK Ads' AS `источник`,
            'Комплекс' AS `направление`,
            '' AS `номер кампании | название кампании`,
            '' AS `номер группы | название группы`,
            CAST(NULL, 'Nullable(Int32)') AS `План заявки`,
            CAST(NULL, 'Nullable(Int32)') AS `План приезда`,
            '' AS `аккаунт|сайт`,
            CAST(NULL, 'Nullable(Int64)') AS priezd_arrival_date,
            CAST(NULL, 'Nullable(Int64)') AS prodazhi_arrival_date,
            'VK Ads' AS `поставщик`,
            'vk_ads' AS _source_table,
            CAST('cost_overlay', 'Nullable(String)') AS cascade_level,
            CAST(NULL, 'Nullable(String)') AS campaign_status,
            CAST(NULL, 'Nullable(String)') AS payment_model
        FROM ad_analytics.local_vk_ads_stats_day
        WHERE date >= toDate('2026-01-01')
          AND spent != 0
        GROUP BY date, account_id, ad_plan_id
        """
    )


def _rebuild_cost_overlays(client) -> tuple[int, float]:
    _require(client, "ad_analytics", SOURCE_STORE)
    _require(client, "ad_analytics", "gsheets_crop_targeting_account_leads")
    _require(client, "ad_analytics", "crop_targeting_api_telegain_lead")
    _require(client, "ad_analytics", "local_vk_ads_stats_day")

    shadow = f"ad_analytics.{COST_OVERLAY_TABLE}_new"
    _create_empty_overlay(client, shadow)
    _insert_crop_gsheet_costs(client, shadow)
    _insert_crop_api_costs(client, shadow)
    _insert_vk_ads_costs(client, shadow)
    swap_shadow(client, f"ad_analytics.{COST_OVERLAY_TABLE}", shadow)

    replace_view(
        client,
        "ad_analytics.big_analytics_crop_targeting",
        f"""
        SELECT * FROM ad_analytics.{SOURCE_STORE} WHERE _source_table = 'crop_targeting'
        UNION ALL
        SELECT * FROM ad_analytics.{COST_OVERLAY_TABLE} WHERE _source_table = 'crop_targeting'
        """,
    )
    row = client.query(f"SELECT count(), toFloat64(sum(total_cost)) FROM ad_analytics.{COST_OVERLAY_TABLE}").result_rows[0]
    return int(row[0]), float(row[1] or 0)


def _overlay_full(client) -> tuple[int, float, float]:
    if not table_exists(client, "ad_analytics", "big_analytics_full"):
        return 0, 0.0, 0.0

    before = client.query(
        """
        SELECT
            toFloat64(sum(total_cost)),
            toFloat64(sum(kol_vo_zayavok) + sum(korr) + sum(kval) + sum(priezd) + sum(prodazhi))
        FROM ad_analytics.big_analytics_full
        """
    ).result_rows[0]
    before_cost = float(before[0] or 0)
    before_funnel = float(before[1] or 0)

    shadow = "ad_analytics.big_analytics_full_new"
    client.command(f"DROP TABLE IF EXISTS {shadow} SYNC")
    client.command(
        """
        CREATE TABLE ad_analytics.big_analytics_full_new AS ad_analytics.big_analytics_full
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(ifNull(`Date`, toDate('2026-01-01')))
        ORDER BY (ifNull(`Date`, toDate('2026-01-01')), ifNull(domain, ''), ifNull(_source_table, ''))
        """
    )
    client.command(
        """
        INSERT INTO ad_analytics.big_analytics_full_new
        SELECT *
        FROM ad_analytics.big_analytics_full
        WHERE NOT startsWith(key3, 'crop_cost|')
          AND NOT startsWith(key3, 'vk_ads_cost|')
        """
    )
    client.command(
        f"""
        INSERT INTO ad_analytics.big_analytics_full_new
        SELECT c.*, CAST(NULL, 'Nullable(String)') AS key_pixel_score
        FROM ad_analytics.{COST_OVERLAY_TABLE} AS c
        """
    )
    swap_shadow(client, "ad_analytics.big_analytics_full", shadow)

    after = client.query(
        """
        SELECT
            count(),
            toFloat64(sum(total_cost)),
            toFloat64(sum(kol_vo_zayavok) + sum(korr) + sum(kval) + sum(priezd) + sum(prodazhi))
        FROM ad_analytics.big_analytics_full
        """
    ).result_rows[0]
    after_rows = int(after[0])
    after_cost = float(after[1] or 0)
    after_funnel = float(after[2] or 0)
    if abs(after_funnel - before_funnel) > 0.0001:
        raise RuntimeError(f"cost overlay changed funnel metrics: {before_funnel} -> {after_funnel}")
    return after_rows, before_cost, after_cost

def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    logger.info("Шаг 10 v6_ch: применение стоимости crop/Telega/VK")
    client = get_client()
    t0 = time.perf_counter()
    overlay_rows, overlay_cost = _rebuild_cost_overlays(client)
    full_rows, before_cost, after_cost = _overlay_full(client)
    crop_rows = count_rows(client, "ad_analytics.big_analytics_crop_targeting")
    full_crop_rows = 0
    if table_exists(client, "ad_analytics", "big_analytics_full"):
        full_crop_rows = int(
            client.query(
                """
                SELECT count()
                FROM ad_analytics.big_analytics_full
                WHERE _source_table IN ('crop_targeting', 'crop', 'telegram', 'social_посевы')
                   OR `источник` = 'Посевы'
                """
            ).result_rows[0][0]
        )
    details = (
        f"overlay={overlay_rows:,}, overlay_cost={overlay_cost:,.2f}, "
        f"crop={crop_rows:,}, full_crop_like={full_crop_rows:,}, "
        f"full={full_rows:,}, full_cost={before_cost:,.2f}->{after_cost:,.2f}"
    )
    logger.info("Шаг 10 v6_ch завершён за %.1f сек: %s", time.perf_counter() - t0, details)
    return {"rows": overlay_rows, "details": details}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())
