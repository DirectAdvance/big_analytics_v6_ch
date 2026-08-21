"""Step 5 for v6_ch: finalize/check pixel source table."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_utils import count_rows, swap_shadow, table_exists
from step3_build_sources.step3 import (
    SOURCE_STORE,
    _crm_name_expr,
    _metric_expr,
    _weekday_expr,
    recreate_source_views,
)

logger = logging.getLogger("pipeline.step5")

PIXEL_REFERENCE_CUTOFF = "2026-06-03"

_GSHEET_LOOKUP_COLUMNS = [
    "status",
    "directologist",
    "site_type",
    "template",
    "salon",
    "city",
    "region",
    "direction",
    "project_manager",
    "client_id",
    "sales_manager",
    "login_key",
    "crm",
]


def _required_tables_exist(client) -> list[str]:
    required = [
        ("ad_analytics", SOURCE_STORE),
        ("ad_analytics", "local_pixel_config"),
        ("ad_analytics", "local_pixel_price_history"),
        ("reference_data", "victory_answers"),
        ("reference_data", "gsheet_sites"),
        ("raw_data", "leads_all"),
        ("raw_data", "gsheet_autosalony_clients"),
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


def _phone10_expr(expr: str) -> str:
    return f"right(replaceRegexpAll(ifNull({expr}, ''), '[^0-9]', ''), 10)"


def _salon_client_id_expr(alias: str) -> str:
    return f"nullIf(lowerUTF8(extract(ifNull({alias}.salon, ''), '^([A-Za-z]+_[0-9]+)')), '')"


def _resolved_salon_expr(alias: str, client_alias: str) -> str:
    return f"coalesce(nullIf({client_alias}.salon, ''), {alias}.salon)"


def _gsheet_lookup_cte(name: str, key_name: str, key_expr: str, populated_expr: str) -> str:
    columns = ",\n                ".join(
        f"anyLast(s.{col}) AS {col}" for col in _GSHEET_LOOKUP_COLUMNS
    )
    return f"""
        {name} AS
        (
            SELECT
                {key_expr} AS {key_name},
                {columns}
            FROM reference_data.gsheet_sites AS s
            WHERE ifNull({populated_expr}, '') != ''
            GROUP BY {key_name}
        )"""


def _crm_from_raw_or_gsheet_expr(source_type_expr: str, gsheet_crm_expr: str) -> str:
    raw_crm = _crm_name_expr(source_type_expr)
    return f"if({raw_crm} = 'Не указана', {_crm_from_gsheet_expr(gsheet_crm_expr)}, {raw_crm})"


def _build_pixel_insert_sql(shadow: str) -> str:
    answer_date_raw = "coalesce(v.bill_day, v.date)"
    answer_date = f"assumeNotNull({answer_date_raw})"
    crm_name = _crm_from_raw_or_gsheet_expr("m.source_type", "ifNull(gs.crm, gs_salon.crm)")
    matched_metrics = _metric_expr("m.status", "m.reason", "m.source_type", "m.raw_salon")
    legacy_metrics = _metric_expr("status", "reason", "source_type", "raw_salon")
    lead_phone10 = _phone10_expr("l.phone")
    return f"""
        INSERT INTO {shadow}
        WITH
        ref_answers AS
        (
            SELECT
                id,
                {answer_date} AS answer_date,
                bill_month,
                project,
                {_resolved_salon_expr("v", "answer_salon_client")} AS salon,
                toDecimal64(cost, 6) AS cost,
                status,
                phone,
                site
            FROM (SELECT * FROM reference_data.victory_answers FINAL) AS v
            LEFT JOIN raw_data.gsheet_autosalony_clients AS answer_salon_client
              ON lowerUTF8(trim(ifNull(answer_salon_client.client_id, ''))) = {_salon_client_id_expr("v")}
            WHERE v.product = 'пиксель'
              AND {answer_date_raw} >= toDate('{PIXEL_REFERENCE_CUTOFF}')
              AND {answer_date_raw} IS NOT NULL
        ),
        ref_phone_months AS
        (
            SELECT DISTINCT phone, toYYYYMM(answer_date) AS ym
            FROM ref_answers
            WHERE phone != ''
        ),
        {_gsheet_lookup_cte("gs_domain", "domain_key", "lowerUTF8(trim(ifNull(s.domain, '')))", "s.domain")},
        {_gsheet_lookup_cte("gs_salon", "salon_key", "lowerUTF8(trim(ifNull(s.salon, '')))", "s.salon")},
        legacy_raw AS
        (
            SELECT
                l.id AS lead_id,
                assumeNotNull(l.created_date) AS created_date,
                ifNull(l.status, '') AS status,
                ifNull(l.reason, '') AS reason,
                ifNull(l.source_type, '') AS source_type,
                ifNull({_resolved_salon_expr("l", "legacy_salon_client")}, '') AS raw_salon,
                pc.pixel_name AS pixel_name,
                lowerUTF8(trim(ifNull(l.utm_source, ''))) AS domain,
                if(
                    ifNull(h.pixel_name, '') != '',
                    coalesce(h.cost_per_lead, h.cost_total, toDecimal64(0, 6)),
                    coalesce(pc.cost_per_lead, pc.cost_total, toDecimal64(0, 6))
                ) AS lead_cost
            FROM raw_data.leads_all AS l
            INNER JOIN ad_analytics.local_pixel_config AS pc
              ON ifNull(l.source_name, '') = pc.pixel_name
              OR lowerUTF8(trim(ifNull(l.utm_source, ''))) = lowerUTF8(trim(pc.pixel_name))
            LEFT JOIN ad_analytics.local_pixel_price_history AS h
              ON h.pixel_name = pc.pixel_name
             AND h.valid_from <= l.created_date
             AND (h.valid_to IS NULL OR l.created_date <= h.valid_to)
            LEFT JOIN raw_data.gsheet_autosalony_clients AS legacy_salon_client
              ON lowerUTF8(trim(ifNull(legacy_salon_client.client_id, ''))) = {_salon_client_id_expr("l")}
            WHERE l.is_copy_for_removal = 0
              AND l.created_date >= toDate('2026-01-01')
              AND l.created_date < toDate('{PIXEL_REFERENCE_CUTOFF}')
              AND lowerUTF8(trim(ifNull(l.utm_source, ''))) IN (SELECT domain_key FROM gs_domain)
              AND ({lead_phone10}, toYYYYMM(l.created_date))
                    NOT IN (SELECT phone, ym FROM ref_phone_months)
        ),
        legacy_scored AS
        (
            SELECT
                lead_id,
                created_date,
                pixel_name,
                domain,
                source_type,
                raw_salon,
                lead_cost,
                {legacy_metrics}
            FROM legacy_raw
        ),
        legacy_agg AS
        (
            SELECT
                domain,
                created_date,
                pixel_name,
                max(lead_cost) AS lead_cost,
                anyLast(source_type) AS source_type,
                anyLast(nullIf(raw_salon, '')) AS pixel_salon_raw,
                sum(kol_vo_zayavok) AS kol_vo_zayavok,
                sum(korr) AS korr,
                sum(kval) AS kval,
                sum(priezd) AS priezd,
                sum(prodazhi) AS prodazhi,
                sum(nekorr) AS nekorr,
                sum(ne_otvechaet) AS ne_otvechaet,
                sum(filtr) AS filtr,
                sum(nedozvon) AS nedozvon,
                sum(priedet) AS priedet,
                sum(dohod_do_kredita) AS dohod_do_kredita,
                sum(dobro) AS dobro
            FROM legacy_scored
            GROUP BY domain, created_date, pixel_name
        ),
        raw_phone_candidates AS
        (
            SELECT
                v.id AS answer_id,
                l.id AS lead_id,
                assumeNotNull(l.created_date) AS created_date,
                ifNull(l.status, '') AS status,
                ifNull(l.reason, '') AS reason,
                ifNull(l.source_type, '') AS source_type,
                ifNull({_resolved_salon_expr("l", "matched_salon_client")}, '') AS raw_salon,
                row_number() OVER (
                    PARTITION BY v.id
                    ORDER BY
                        if(lowerUTF8(trim(ifNull(l.utm_source, ''))) = lowerUTF8(trim(ifNull(v.site, ''))) AND ifNull(v.site, '') != '', 0, 1),
                        if(ifNull({_resolved_salon_expr("l", "matched_salon_client")}, '') = ifNull(v.salon, '') AND ifNull(v.salon, '') != '', 0, 1),
                        if(ifNull(l.status, '') != '', 0, 1),
                        abs(dateDiff('day', l.created_date, v.answer_date)),
                        l.created_date DESC,
                        l.id DESC
                ) AS rn
            FROM ref_answers AS v
            LEFT JOIN raw_data.leads_all AS l
              ON {lead_phone10} = v.phone
             AND toYYYYMM(l.created_date) = toYYYYMM(v.answer_date)
             AND l.is_copy_for_removal = 0
             AND l.created_date IS NOT NULL
            LEFT JOIN raw_data.gsheet_autosalony_clients AS matched_salon_client
              ON lowerUTF8(trim(ifNull(matched_salon_client.client_id, ''))) = {_salon_client_id_expr("l")}
        ),
        matched_raw AS
        (
            SELECT *
            FROM raw_phone_candidates
            WHERE rn = 1
        ),
        matched_scored AS
        (
            SELECT
                answer_id,
                {matched_metrics}
            FROM matched_raw AS m
        )
        SELECT
            s.*
        FROM ad_analytics.{SOURCE_STORE} AS s
        WHERE s._source_table != 'pixel'

        UNION ALL

        SELECT
            concat('pixel_legacy|', toString(agg.created_date), '|', agg.domain, '|', agg.pixel_name) AS key3,
            agg.created_date AS `Date`,
            {_weekday_expr("agg.created_date")} AS `День недели`,
            toStartOfWeek(agg.created_date, 1) AS week_start,
            toInt64(0) AS `CampaignId`,
            CAST(agg.pixel_name, 'Nullable(String)') AS `CampaignName`,
            toInt64(0) AS `AdGroupId`,
            CAST(agg.pixel_name, 'Nullable(String)') AS `AdGroupName`,
            CAST(NULL, 'Nullable(String)') AS `AdNetworkType`,
            CAST(NULL, 'Nullable(String)') AS `Device`,
            toDecimal64(0, 6) AS `Impressions`,
            toDecimal64(0, 6) AS `Clicks`,
            agg.kol_vo_zayavok * agg.lead_cost AS total_cost,
            CAST(agg.domain, 'Nullable(String)') AS domain,
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
            {_crm_name_expr("agg.source_type")} AS `Название crm`,
            CAST('Заявки', 'Nullable(String)') AS `тип_заявки`,
            agg.kol_vo_zayavok AS kol_vo_zayavok,
            agg.korr AS korr,
            agg.kval AS kval,
            agg.priezd AS priezd,
            agg.prodazhi AS prodazhi,
            agg.nekorr AS nekorr,
            agg.ne_otvechaet AS ne_otvechaet,
            agg.filtr AS filtr,
            agg.nedozvon AS nedozvon,
            agg.priedet AS priedet,
            agg.dohod_do_kredita AS dohod_do_kredita,
            agg.dobro AS dobro,
            CAST(gs.status, 'Nullable(String)') AS `статус`,
            gs.directologist AS `специалист`,
            gs.site_type AS `тип_сайта`,
            gs.template AS `шаблон`,
            coalesce(agg.pixel_salon_raw, gs.salon) AS `салон`,
            gs.city AS `город`,
            gs.region AS `регион`,
            gs.direction AS direction,
            CAST(NULL, 'Nullable(String)') AS `неверный_кодер_new`,
            CAST(NULL, 'Nullable(String)') AS fid,
            gs.project_manager AS `проджект`,
            gs.client_id AS `id_салона`,
            gs.sales_manager AS `менеджер`,
            'Пиксель' AS `источник`,
            if(agg.domain LIKE '%pixel\\_pr', 'Перформ', 'Пиксель') AS `направление`,
            '' AS `номер кампании | название кампании`,
            '' AS `номер группы | название группы`,
            CAST(NULL, 'Nullable(Int32)') AS `План заявки`,
            CAST(NULL, 'Nullable(Int32)') AS `План приезда`,
            concat(ifNull(gs.login_key, ''), '|', agg.domain) AS `аккаунт|сайт`,
            CAST(NULL, 'Nullable(Int64)') AS priezd_arrival_date,
            CAST(NULL, 'Nullable(Int64)') AS prodazhi_arrival_date,
            'Victory' AS `поставщик`,
            'pixel' AS _source_table,
            CAST('leads_all_before_2026_06_03', 'Nullable(String)') AS cascade_level,
            CAST(NULL, 'Nullable(String)') AS campaign_status,
            CAST(NULL, 'Nullable(String)') AS payment_model
        FROM legacy_agg AS agg
        LEFT JOIN gs_domain AS gs ON gs.domain_key = agg.domain

        UNION ALL

        SELECT
            concat('pixel_answer|', toString(v.id)) AS key3,
            v.answer_date AS `Date`,
            {_weekday_expr("v.answer_date")} AS `День недели`,
            toStartOfWeek(v.answer_date, 1) AS week_start,
            toInt64(0) AS `CampaignId`,
            CAST(coalesce(nullIf(v.project, ''), 'Пиксель'), 'Nullable(String)') AS `CampaignName`,
            toInt64(0) AS `AdGroupId`,
            CAST(coalesce(nullIf(v.project, ''), 'Пиксель'), 'Nullable(String)') AS `AdGroupName`,
            CAST(NULL, 'Nullable(String)') AS `AdNetworkType`,
            CAST(NULL, 'Nullable(String)') AS `Device`,
            toDecimal64(0, 6) AS `Impressions`,
            toDecimal64(0, 6) AS `Clicks`,
            v.cost AS total_cost,
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
            ifNull(ms.kol_vo_zayavok, toDecimal64(0, 6)) AS kol_vo_zayavok,
            ifNull(ms.korr, toDecimal64(0, 6)) AS korr,
            ifNull(ms.kval, toDecimal64(0, 6)) AS kval,
            ifNull(ms.priezd, toDecimal64(0, 6)) AS priezd,
            ifNull(ms.prodazhi, toDecimal64(0, 6)) AS prodazhi,
            ifNull(ms.nekorr, toDecimal64(0, 6)) AS nekorr,
            ifNull(ms.ne_otvechaet, toDecimal64(0, 6)) AS ne_otvechaet,
            ifNull(ms.filtr, toDecimal64(0, 6)) AS filtr,
            ifNull(ms.nedozvon, toDecimal64(0, 6)) AS nedozvon,
            ifNull(ms.priedet, toDecimal64(0, 6)) AS priedet,
            ifNull(ms.dohod_do_kredita, 0) AS dohod_do_kredita,
            ifNull(ms.dobro, 0) AS dobro,
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
            CAST('victory_answers_from_2026_06_03', 'Nullable(String)') AS cascade_level,
            CAST(NULL, 'Nullable(String)') AS campaign_status,
            CAST(NULL, 'Nullable(String)') AS payment_model
        FROM ref_answers AS v
        LEFT JOIN matched_raw AS m ON m.answer_id = v.id
        LEFT JOIN matched_scored AS ms ON ms.answer_id = v.id
        LEFT JOIN gs_domain AS gs ON gs.domain_key = lowerUTF8(trim(ifNull(v.site, '')))
        LEFT JOIN gs_salon ON gs_salon.salon_key = lowerUTF8(trim(ifNull(v.salon, '')))
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
    logger.info(
        "Шаг 5 v6_ch: гибридный pixel leads_all<%s + victory_answers>=%s",
        PIXEL_REFERENCE_CUTOFF,
        PIXEL_REFERENCE_CUTOFF,
    )
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
