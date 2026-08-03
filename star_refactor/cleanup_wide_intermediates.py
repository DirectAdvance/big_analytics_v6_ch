"""Replace wide intermediate tables with star-backed compatibility views."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_utils import SAFE_QUERY_SETTINGS, count_rows, q, replace_view

log = logging.getLogger("cleanup_wide_intermediates")


def _wide_fact_sql(where_sql: str) -> str:
    return f"""
        SELECT
            toString(cityHash64(
                f.`атрибуция`,
                toString(f.`Date`),
                f.`_source_table`,
                ifNull(toString(f.`CampaignId`), ''),
                ifNull(toString(f.`AdGroupId`), ''),
                ifNull(f.domain, ''),
                ifNull(f.fid, '')
            )) AS key3,
            f.`Date` AS `Date`,
            dd.`День недели` AS `День недели`,
            dd.week_start AS week_start,
            f.`CampaignId` AS `CampaignId`,
            dc.`CampaignName` AS `CampaignName`,
            f.`AdGroupId` AS `AdGroupId`,
            dag.`AdGroupName` AS `AdGroupName`,
            dan.`AdNetworkType` AS `AdNetworkType`,
            ddev.`Device` AS `Device`,
            f.`Impressions` AS `Impressions`,
            f.`Clicks` AS `Clicks`,
            f.total_cost AS total_cost,
            f.domain AS domain,
            f.`RlAdjustmentId` AS `RlAdjustmentId`,
            da.`RlAdjustmentId_total` AS `RlAdjustmentId_total`,
            dc.campaign_code AS campaign_code,
            dc.tp AS tp,
            dc.cpc_cpa AS cpc_cpa,
            dc.site_quiz AS site_quiz,
            dag.adgroup_code AS adgroup_code,
            f.account_login AS account_login,
            dml.manager_login AS manager_login,
            dag.ag_part1 AS ag_part1,
            dag.ag_part2 AS ag_part2,
            dag.ag_part3 AS ag_part3,
            dag.ag_part4 AS ag_part4,
            dag.ag_part5 AS ag_part5,
            dag.ag_part6 AS ag_part6,
            dag.ag_part7 AS ag_part7,
            dag.`марки авто` AS `марки авто`,
            f.`Название crm` AS `Название crm`,
            f.`тип_заявки` AS `тип_заявки`,
            f.kol_vo_zayavok AS kol_vo_zayavok,
            f.korr AS korr,
            f.kval AS kval,
            f.priezd AS priezd,
            f.prodazhi AS prodazhi,
            f.nekorr AS nekorr,
            f.ne_otvechaet AS ne_otvechaet,
            f.filtr AS filtr,
            f.nedozvon AS nedozvon,
            f.priedet AS priedet,
            f.dohod_do_kredita AS dohod_do_kredita,
            f.dobro AS dobro,
            f.`статус` AS `статус`,
            f.`специалист` AS `специалист`,
            f.`тип_сайта` AS `тип_сайта`,
            f.`шаблон` AS `шаблон`,
            f.`салон` AS `салон`,
            f.`город` AS `город`,
            f.`регион` AS `регион`,
            f.direction AS direction,
            dag.`неверный_кодер_new` AS `неверный_кодер_new`,
            f.fid AS fid,
            f.`проджект` AS `проджект`,
            f.`id_салона` AS `id_салона`,
            f.`менеджер` AS `менеджер`,
            ds.`источник` AS `источник`,
            f.`направление` AS `направление`,
            dc.`номер кампании | название кампании` AS `номер кампании | название кампании`,
            dag.`номер группы | название группы` AS `номер группы | название группы`,
            f.`План заявки` AS `План заявки`,
            f.`План приезда` AS `План приезда`,
            concat(ifNull(f.account_login, ''), '|', ifNull(f.domain, '')) AS `аккаунт|сайт`,
            f.priezd_arrival_date AS priezd_arrival_date,
            f.prodazhi_arrival_date AS prodazhi_arrival_date,
            ds.`поставщик` AS `поставщик`,
            f.`_source_table` AS `_source_table`,
            f.cascade_level AS cascade_level,
            dc.campaign_status AS campaign_status,
            dc.payment_model AS payment_model,
            concat(ifNull(toString(f.`Date`), ''), '|', ifNull(f.domain, ''), '|', ifNull(ds.`источник`, ''), '|', ifNull(toString(f.`CampaignId`), '')) AS key_pixel_score
        FROM ad_analytics.fact_big_analytics f
        LEFT JOIN ad_analytics.Dim_Date dd ON dd.`Date` = f.`Date`
        LEFT JOIN ad_analytics.Dim_Campaign dc ON dc.`CampaignId` = f.`CampaignId`
        LEFT JOIN ad_analytics.Dim_AdGroup dag ON dag.`AdGroupId` = f.`AdGroupId`
        LEFT JOIN ad_analytics.Dim_Adjustment da ON da.`RlAdjustmentId` = f.`RlAdjustmentId`
        LEFT JOIN ad_analytics.Dim_AdNetworkType dan ON dan.ad_network_type_key = f.ad_network_type_key
        LEFT JOIN ad_analytics.Dim_Device ddev ON ddev.device_key = f.device_key
        LEFT JOIN ad_analytics.Dim_Source ds ON ds.source_key = f.source_key
        LEFT JOIN ad_analytics.Dim_ManagerLogin dml ON dml.manager_login_key = f.manager_login_key
        {where_sql}
    """


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    client = get_client()
    t0 = time.perf_counter()

    views = {
        "big_analytics_full": "WHERE f.`атрибуция` = 'По дате заявки'",
        "big_analytics_full_arrival": "WHERE f.`атрибуция` = 'По дате визита'",
        "big_analytics_pixel_score": "WHERE f.`атрибуция` = 'По дате заявки' AND f.`_source_table` = 'пиксель_атрибуц'",
    }
    rows: dict[str, int] = {}
    for table, where_sql in views.items():
        replace_view(client, f"ad_analytics.{table}", _wide_fact_sql(where_sql))
        rows[table] = count_rows(client, f"ad_analytics.{q(table)}")
        log.info("  %s view rows=%d", table, rows[table])

    client.command("DROP TABLE IF EXISTS ad_analytics.big_analytics_sources SYNC", settings=SAFE_QUERY_SETTINGS)
    replace_view(
        client,
        "ad_analytics.big_analytics_unified",
        """
        SELECT *, 'По дате заявки' AS `атрибуция` FROM ad_analytics.big_analytics_full
        UNION ALL
        SELECT *, 'По дате визита' AS `атрибуция` FROM ad_analytics.big_analytics_full_arrival
        """,
    )
    rows["big_analytics_unified"] = count_rows(client, "ad_analytics.big_analytics_unified")
    details = ", ".join(f"{key}={value:,}" for key, value in rows.items())
    log.info("cleanup_wide_intermediates завершён за %.1f сек: %s", time.perf_counter() - t0, details)
    return {"rows": sum(rows.values()), "details": details}


if __name__ == "__main__":
    print(run())
