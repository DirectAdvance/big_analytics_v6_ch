"""Step 5 for v6_ch: finalize/check pixel source table."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_utils import count_rows, swap_shadow, table_exists
from step3_build_sources.step3 import SOURCE_STORE, _weekday_expr, recreate_source_views

logger = logging.getLogger("pipeline.step5")


def _required_tables_exist(client) -> list[str]:
    required = [
        ("ad_analytics", SOURCE_STORE),
        ("reference_data", "victory_pixel_answers"),
        ("reference_data", "gsheet_sites"),
    ]
    return [f"{db}.{table}" for db, table in required if not table_exists(client, db, table)]


def _crm_from_gsheet_expr(crm_expr: str) -> str:
    return f"""
        multiIf(
            {crm_expr} = 'One CRM', 'Фаиг',
            {crm_expr} = 'PLEX', 'Плекс',
            {crm_expr} = 'MEGA CRM', 'Мега',
            {crm_expr} = 'MarCar CRM', 'Маркар',
            {crm_expr} = 'M-Auto CRM', 'МаАвто',
            {crm_expr} = 'RedautoCRM', 'Ред Авто',
            {crm_expr} = 'GenzesCRM', 'Генезис',
            {crm_expr} = 'RivendellCRM', 'Ривендел',
            ifNull(nullIf({crm_expr}, ''), 'Не указана')
        )
    """


def _build_pixel_insert_sql(shadow: str) -> str:
    answer_date = "coalesce(v.bill_day, v.date)"
    crm_name = _crm_from_gsheet_expr("ifNull(gs.crm, gs_salon.crm)")
    return f"""
        INSERT INTO {shadow}
        WITH
        gs_domain AS
        (
            SELECT
                lowerUTF8(trim(ifNull(s.domain, ''))) AS domain_key,
                anyLast(s.domain) AS domain,
                anyLast(s.status) AS status,
                anyLast(s.directologist) AS directologist,
                anyLast(s.site_type) AS site_type,
                anyLast(s.template) AS template,
                anyLast(s.salon) AS salon,
                anyLast(s.city) AS city,
                anyLast(s.region) AS region,
                anyLast(s.direction) AS direction,
                anyLast(s.project_manager) AS project_manager,
                anyLast(s.client_id) AS client_id,
                anyLast(s.sales_manager) AS sales_manager,
                anyLast(s.login_key) AS login_key,
                anyLast(s.crm) AS crm
            FROM reference_data.gsheet_sites AS s
            WHERE ifNull(s.domain, '') != ''
            GROUP BY domain_key
        ),
        gs_salon AS
        (
            SELECT
                lowerUTF8(trim(ifNull(s.salon, ''))) AS salon_key,
                anyLast(s.status) AS status,
                anyLast(s.directologist) AS directologist,
                anyLast(s.site_type) AS site_type,
                anyLast(s.template) AS template,
                anyLast(s.salon) AS salon,
                anyLast(s.city) AS city,
                anyLast(s.region) AS region,
                anyLast(s.direction) AS direction,
                anyLast(s.project_manager) AS project_manager,
                anyLast(s.client_id) AS client_id,
                anyLast(s.sales_manager) AS sales_manager,
                anyLast(s.login_key) AS login_key,
                anyLast(s.crm) AS crm
            FROM reference_data.gsheet_sites AS s
            WHERE ifNull(s.salon, '') != ''
            GROUP BY salon_key
        )
        SELECT
            s.*
        FROM ad_analytics.{SOURCE_STORE} AS s
        WHERE s._source_table != 'pixel'

        UNION ALL

        SELECT
            concat('pixel_answer|', toString(v.id)) AS key3,
            assumeNotNull({answer_date}) AS `Date`,
            {_weekday_expr(f"assumeNotNull({answer_date})")} AS `День недели`,
            toStartOfWeek(assumeNotNull({answer_date}), 1) AS week_start,
            toInt64(0) AS `CampaignId`,
            CAST(coalesce(nullIf(v.project, ''), 'Пиксель'), 'Nullable(String)') AS `CampaignName`,
            toInt64(0) AS `AdGroupId`,
            CAST(coalesce(nullIf(v.project, ''), 'Пиксель'), 'Nullable(String)') AS `AdGroupName`,
            CAST(NULL, 'Nullable(String)') AS `AdNetworkType`,
            CAST(NULL, 'Nullable(String)') AS `Device`,
            toDecimal64(0, 6) AS `Impressions`,
            toDecimal64(0, 6) AS `Clicks`,
            toDecimal64(v.cost, 6) AS total_cost,
            CAST(nullIf(v.site, ''), 'Nullable(String)') AS domain,
            toInt64(0) AS `RlAdjustmentId`,
            '' AS `RlAdjustmentId_total`,
            CAST(NULL, 'Nullable(String)') AS campaign_code,
            '' AS tp,
            '' AS cpc_cpa,
            '' AS site_quiz,
            CAST(NULL, 'Nullable(String)') AS adgroup_code,
            'пиксель' AS account_login,
            CAST('пиксель', 'Nullable(String)') AS manager_login,
            '' AS ag_part1, '' AS ag_part2, '' AS ag_part3, '' AS ag_part4, '' AS ag_part5, '' AS ag_part6, '' AS ag_part7,
            '' AS `марки авто`,
            {crm_name} AS `Название crm`,
            CAST('Пиксель', 'Nullable(String)') AS `тип_заявки`,
            toDecimal64(1, 6) AS kol_vo_zayavok,
            toDecimal64(1, 6) AS korr,
            toDecimal64(0, 6) AS kval,
            toDecimal64(0, 6) AS priezd,
            toDecimal64(0, 6) AS prodazhi,
            toDecimal64(0, 6) AS nekorr,
            toDecimal64(0, 6) AS ne_otvechaet,
            toDecimal64(0, 6) AS filtr,
            toDecimal64(0, 6) AS nedozvon,
            toDecimal64(0, 6) AS priedet,
            toInt64(0) AS dohod_do_kredita,
            toInt64(0) AS dobro,
            CAST(v.status, 'Nullable(String)') AS `статус`,
            coalesce(gs.directologist, gs_salon.directologist) AS `специалист`,
            coalesce(gs.site_type, gs_salon.site_type) AS `тип_сайта`,
            coalesce(gs.template, gs_salon.template) AS `шаблон`,
            coalesce(nullIf(v.salon, ''), gs.salon, gs_salon.salon) AS `салон`,
            coalesce(gs.city, gs_salon.city) AS `город`,
            coalesce(gs.region, gs_salon.region) AS `регион`,
            coalesce(gs.direction, gs_salon.direction, 'Авто') AS direction,
            CAST(NULL, 'Nullable(String)') AS `неверный_кодер_new`,
            CAST(NULL, 'Nullable(String)') AS fid,
            coalesce(gs.project_manager, gs_salon.project_manager) AS `проджект`,
            coalesce(gs.client_id, gs_salon.client_id) AS `id_салона`,
            coalesce(gs.sales_manager, gs_salon.sales_manager) AS `менеджер`,
            'Пиксель' AS `источник`,
            'Пиксель' AS `направление`,
            '' AS `номер кампании | название кампании`,
            '' AS `номер группы | название группы`,
            CAST(NULL, 'Nullable(Int32)') AS `План заявки`,
            CAST(NULL, 'Nullable(Int32)') AS `План приезда`,
            concat(ifNull(coalesce(gs.login_key, gs_salon.login_key), ''), '|', ifNull(v.site, '')) AS `аккаунт|сайт`,
            CAST(NULL, 'Nullable(Int64)') AS priezd_arrival_date,
            CAST(NULL, 'Nullable(Int64)') AS prodazhi_arrival_date,
            'Victory' AS `поставщик`,
            'pixel' AS _source_table,
            CAST('victory_pixel_answers', 'Nullable(String)') AS cascade_level,
            CAST(NULL, 'Nullable(String)') AS campaign_status,
            CAST(NULL, 'Nullable(String)') AS payment_model
        FROM (SELECT * FROM reference_data.victory_pixel_answers FINAL) AS v
        LEFT JOIN gs_domain gs ON gs.domain_key = lowerUTF8(trim(ifNull(v.site, '')))
        LEFT JOIN gs_salon ON gs_salon.salon_key = lowerUTF8(trim(ifNull(v.salon, '')))
        WHERE v.product = 'пиксель'
          AND ifNull(v.bill_month, '') >= '2026-01'
          AND {answer_date} IS NOT NULL
        """


def _apply_pixel_costs(client) -> tuple[int, float]:
    shadow = f"ad_analytics.{SOURCE_STORE}_new"
    client.command(f"DROP TABLE IF EXISTS {shadow} SYNC")
    client.command(
        f"""
        CREATE TABLE {shadow} AS ad_analytics.{SOURCE_STORE}
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(ifNull(`Date`, toDate('2026-01-01')))
        ORDER BY (ifNull(`Date`, toDate('2026-01-01')), ifNull(`CampaignId`, 0), ifNull(key3, ''))
        """
    )
    client.command(_build_pixel_insert_sql(shadow))
    swap_shadow(client, f"ad_analytics.{SOURCE_STORE}", shadow)
    recreate_source_views(client)

    row = client.query(
        """
        SELECT count(), toFloat64(sum(total_cost))
        FROM ad_analytics.big_analytics_pixel
        """
    ).result_rows[0]
    return int(row[0]), float(row[1] or 0)


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    logger.info("Шаг 5 v6_ch: применение pixel из reference_data.victory_pixel_answers")
    client = get_client()
    t0 = time.perf_counter()
    missing = _required_tables_exist(client)
    if missing:
        raise RuntimeError("pixel cost prerequisites missing: " + ", ".join(missing))

    before_rows = count_rows(client, f"ad_analytics.{SOURCE_STORE}")
    pixel_rows, pixel_cost = _apply_pixel_costs(client)
    after_rows = count_rows(client, f"ad_analytics.{SOURCE_STORE}")
    details = (
        f"{SOURCE_STORE}={after_rows:,} ({after_rows - before_rows:+,}), "
        f"big_analytics_pixel={pixel_rows:,}, pixel_cost={pixel_cost:,.2f}"
    )
    logger.info("Шаг 5 v6_ch завершён за %.1f сек: %s", time.perf_counter() - t0, details)
    return {"rows": pixel_rows, "details": details}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())
