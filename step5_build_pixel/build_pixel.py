"""Step 5 for v6_ch: finalize/check pixel source table."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_utils import count_rows, swap_shadow, table_exists
from step3_build_sources.step3 import SOURCE_STORE, _gs_account_cte, _metric_expr, _weekday_expr, recreate_source_views

logger = logging.getLogger("pipeline.step5")


def _required_tables_exist(client) -> list[str]:
    required = [
        ("ad_analytics", SOURCE_STORE),
        ("raw_data", "leads_all"),
        ("ad_analytics", "local_pixel_config"),
        ("ad_analytics", "local_pixel_price_history"),
        ("raw_data", "gsheet_sites"),
    ]
    return [f"{db}.{table}" for db, table in required if not table_exists(client, db, table)]


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
    client.command(
        f"""
        INSERT INTO {shadow}
        WITH
        {_gs_account_cte()},
        valid_domains AS
        (
            SELECT DISTINCT lowerUTF8(trim(ifNull(domain, ''))) AS domain_key
            FROM raw_data.gsheet_sites
            WHERE ifNull(domain, '') != ''
        ),
        lead_scored AS
        (
            SELECT
                la.*,
                concat('pixel_src|', toString(la.id)) AS key3,
                CAST(NULL, 'Nullable(String)') AS fid,
                lowerUTF8(trim(ifNull(la.utm_source, ''))) AS domain_key,
                {_metric_expr("la.status", "la.reason", "la.source_type", "la.salon")}
            FROM raw_data.leads_all la
            WHERE la.is_copy_for_removal = 0
              AND ifNull(la.created_date, toDate('1970-01-01')) >= toDate('2026-01-01')
              AND lowerUTF8(trim(ifNull(la.utm_source, ''))) IN (SELECT domain_key FROM valid_domains)
        ),
        pixel_price_matches AS
        (
            SELECT
                l.created_date AS created_date,
                l.domain_key AS domain_key,
                l.utm_source AS domain,
                l.deal_type AS deal_type,
                l.source_type AS source_type,
                l.salon AS lead_salon,
                pc.pixel_name AS pixel_name,
                l.kol_vo_zayavok AS kol_vo_zayavok,
                l.korr AS korr,
                l.kval AS kval,
                l.priezd AS priezd,
                l.prodazhi AS prodazhi,
                l.nekorr AS nekorr,
                l.ne_otvechaet AS ne_otvechaet,
                l.filtr AS filtr,
                l.nedozvon AS nedozvon,
                l.priedet AS priedet,
                l.dohod_do_kredita AS dohod_do_kredita,
                l.dobro AS dobro,
                ifNull(
                    coalesce(h.cost_per_lead, h.cost_total, pc.cost_per_lead, pc.cost_total),
                    toDecimal64(0, 6)
                ) AS rate,
                ifNull(h.valid_from, toDate('1900-01-01')) AS valid_from
            FROM lead_scored l
            INNER JOIN ad_analytics.local_pixel_config pc
                ON l.source_name = pc.pixel_name
                OR lowerUTF8(trim(ifNull(l.utm_source, ''))) = lowerUTF8(trim(pc.pixel_name))
            LEFT JOIN ad_analytics.local_pixel_price_history h
                ON h.pixel_name = pc.pixel_name
               AND h.valid_from <= l.created_date
               AND (h.valid_to IS NULL OR l.created_date <= h.valid_to)
        ),
        pixel_agg AS
        (
            SELECT
                created_date,
                domain_key,
                domain,
                pixel_name,
                anyLast(deal_type) AS deal_type,
                anyLast(source_type) AS source_type,
                anyLast(lead_salon) AS lead_salon,
                toDecimal64(count(), 6) AS kol_vo_zayavok,
                sum(korr) AS korr,
                sum(kval) AS kval,
                sum(priezd) AS priezd,
                sum(prodazhi) AS prodazhi,
                sum(nekorr) AS nekorr,
                sum(ne_otvechaet) AS ne_otvechaet,
                sum(filtr) AS filtr,
                sum(nedozvon) AS nedozvon,
                sum(priedet) AS priedet,
                toInt64(sum(dohod_do_kredita)) AS dohod_do_kredita,
                toInt64(sum(dobro)) AS dobro,
                max(rate) AS cost_rate
            FROM pixel_price_matches
            GROUP BY created_date, domain_key, domain, pixel_name
        ),
        crm_by_source AS
        (
            SELECT
                created_date,
                domain_key,
                pixel_name,
                multiIf(
                    source_type = 'crmf_excel', 'Фаиг',
                    source_type = 'plex_excel', 'Плекс',
                    source_type = 'mega_crm_excel', 'Мега',
                    source_type = 'marcar_crm_excel', 'Маркар',
                    source_type = 'redauto_excel', 'Ред Авто',
                    source_type = 'genzes_excel', 'Генезис',
                    source_type = 'mauto_excel', 'МаАвто',
                    ifNull(source_type, '')
                ) AS crm_name
            FROM pixel_agg
        )
        SELECT
            s.*
        FROM ad_analytics.{SOURCE_STORE} AS s
        WHERE s._source_table != 'pixel'

        UNION ALL

        SELECT
            concat('pixel|', toString(l.created_date), '|', ifNull(l.domain, ''), '|', ifNull(l.pixel_name, '')) AS key3,
            assumeNotNull(l.created_date) AS `Date`,
            {_weekday_expr("assumeNotNull(l.created_date)")} AS `День недели`,
            toStartOfWeek(assumeNotNull(l.created_date), 1) AS week_start,
            toInt64(0) AS `CampaignId`,
            CAST(l.pixel_name, 'Nullable(String)') AS `CampaignName`,
            toInt64(0) AS `AdGroupId`,
            CAST(l.pixel_name, 'Nullable(String)') AS `AdGroupName`,
            CAST(NULL, 'Nullable(String)') AS `AdNetworkType`,
            CAST(NULL, 'Nullable(String)') AS `Device`,
            toDecimal64(0, 6) AS `Impressions`,
            toDecimal64(0, 6) AS `Clicks`,
            toDecimal64(toFloat64(l.kol_vo_zayavok) * toFloat64(ifNull(l.cost_rate, toDecimal64(0, 6))), 6) AS total_cost,
            CAST(l.domain, 'Nullable(String)') AS domain,
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
            crm.crm_name AS `Название crm`,
            CAST(if(ifNull(l.deal_type, '') = '', 'Заявка', l.deal_type), 'Nullable(String)') AS `тип_заявки`,
            l.kol_vo_zayavok,
            l.korr,
            l.kval,
            l.priezd,
            l.prodazhi,
            l.nekorr,
            l.ne_otvechaet,
            l.filtr,
            l.nedozvon,
            l.priedet,
            l.dohod_do_kredita,
            l.dobro,
            CAST(NULL, 'Nullable(String)') AS `статус`,
            gs.directologist AS `специалист`,
            gs.site_type AS `тип_сайта`,
            gs.template AS `шаблон`,
            coalesce(nullIf(l.lead_salon, ''), gs.salon) AS `салон`,
            gs.city AS `город`,
            gs.region AS `регион`,
            gs.direction AS direction,
            CAST(NULL, 'Nullable(String)') AS `неверный_кодер_new`,
            CAST(NULL, 'Nullable(String)') AS fid,
            gs.project_manager AS `проджект`,
            gs.client_id AS `id_салона`,
            gs.sales_manager AS `менеджер`,
            'Пиксель' AS `источник`,
            if(l.domain LIKE '%pixel\\_pr', 'Перформ', 'Пиксель') AS `направление`,
            '' AS `номер кампании | название кампании`,
            '' AS `номер группы | название группы`,
            CAST(NULL, 'Nullable(Int32)') AS `План заявки`,
            CAST(NULL, 'Nullable(Int32)') AS `План приезда`,
            concat(ifNull(gs.login_key, ''), '|', ifNull(l.domain, '')) AS `аккаунт|сайт`,
            CAST(NULL, 'Nullable(Int64)') AS priezd_arrival_date,
            CAST(NULL, 'Nullable(Int64)') AS prodazhi_arrival_date,
            'Victory' AS `поставщик`,
            'pixel' AS _source_table,
            CAST('source_name_overlay', 'Nullable(String)') AS cascade_level,
            CAST(NULL, 'Nullable(String)') AS campaign_status,
            CAST(NULL, 'Nullable(String)') AS payment_model
        FROM pixel_agg l
        LEFT JOIN gs_domain gs ON gs.domain_key = l.domain_key
        LEFT JOIN crm_by_source crm
            ON crm.created_date = l.created_date
           AND crm.domain_key = l.domain_key
           AND crm.pixel_name = l.pixel_name
        """
    )
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
    logger.info("Шаг 5 v6_ch: применение стоимости pixel из ad_analytics.local_pixel_config")
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
