"""Replace wide intermediate tables with star-backed compatibility views."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_utils import SAFE_QUERY_SETTINGS, count_rows, q, replace_view
from step3_build_sources.step3 import CROP_SOURCE_TYPES, DIRECT_SOURCE_TYPES

log = logging.getLogger("cleanup_wide_intermediates")

# SOURCE_VIEWS_2026-08-06 — вьюхи источников, которые step3 строит НАД широкой
# промежуточной `big_analytics_sources`. Эта таблица дропается здесь же (её роль
# закончена: звезда собрана), и до правки все пять вьюх после каждого полного
# прогона падали с `Code: 60 UNKNOWN_TABLE` — они оставались висеть на дропнутой
# таблице. Дропать их вместе с таблицей нельзя: имена — публичный контракт
# (data_check, step12, ручные разборы), а `SELECT count()` по ним обязан
# работать. Поэтому они переводятся на звезду — ровно тем же приёмом, которым
# этот модуль уже переводит `big_analytics_full`/`_arrival`/`_pixel_score`.
# `EXCEPT(key_pixel_score)` — чтобы набор колонок совпал со step3-версией вьюхи
# (широкий факт добавляет к нему ровно одну колонку).
SOURCE_VIEWS = {
    "big_analytics_direct": DIRECT_SOURCE_TYPES,
    "big_analytics_seo": ("seo",),
    "big_analytics_pixel": ("pixel",),
    "big_analytics_crop_targeting": CROP_SOURCE_TYPES,
    # REVIEWS_TAG_2026-08-16 — тегов два. step3 пишет отзывы как 'direct_account_reviews'
    # (`step3_build_sources/step3.py:1742` создаёт вьюху с обоими значениями), а этот модуль
    # пересоздавал её только по 'reviews' — которого в данных нет. Итог: после каждого полного
    # прогона `big_analytics_reviews` отдавала 0 строк при 4 996 строках в факте.
    "big_analytics_reviews": ("reviews", "direct_account_reviews"),
}


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
            dac.account_login AS account_login,
            dml.manager_login AS manager_login,
            dag.ag_part1 AS ag_part1,
            dag.ag_part2 AS ag_part2,
            dag.ag_part3 AS ag_part3,
            dag.ag_part4 AS ag_part4,
            dag.ag_part5 AS ag_part5,
            dag.ag_part6 AS ag_part6,
            dag.ag_part7 AS ag_part7,
            dag.`марки авто` AS `марки авто`,
            if(
                ifNull(dcs.`Название crm`, '') IN ('', 'Не указана'),
                if(ifNull(dsite.`Название crm`, '') = '', 'Не указана', dsite.`Название crm`),
                dcs.`Название crm`
            ) AS `Название crm`,
            dcs.`тип_заявки` AS `тип_заявки`,
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
            dcs.`статус` AS `статус`,
            dsl.`специалист` AS `специалист`,
            dsl.`тип_сайта` AS `тип_сайта`,
            dsl.`шаблон` AS `шаблон`,
            dsl.`салон` AS `салон`,
            dsl.`город` AS `город`,
            dsl.`регион` AS `регион`,
            dsl.`направление` AS direction,
            dag.`неверный_кодер_new` AS `неверный_кодер_new`,
            f.fid AS fid,
            dsl.`проджект` AS `проджект`,
            dsl.`id_салона` AS `id_салона`,
            dsl.`менеджер` AS `менеджер`,
            ds.`источник` AS `источник`,
            dsl.`направление` AS `направление`,
            dc.`номер кампании | название кампании` AS `номер кампании | название кампании`,
            dag.`номер группы | название группы` AS `номер группы | название группы`,
            f.`План заявки` AS `План заявки`,
            f.`План приезда` AS `План приезда`,
            concat(ifNull(dac.account_login, ''), '|', ifNull(f.domain, '')) AS `аккаунт|сайт`,
            f.priezd_arrival_date AS priezd_arrival_date,
            f.prodazhi_arrival_date AS prodazhi_arrival_date,
            ds.`поставщик` AS `поставщик`,
            f.`_source_table` AS `_source_table`,
            dcs.cascade_level AS cascade_level,
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
        LEFT JOIN ad_analytics.Dim_Account dac ON dac.account_key = f.account_key
        LEFT JOIN ad_analytics.Dim_CRMStatus dcs ON dcs.crm_status_key = f.crm_status_key
        LEFT JOIN ad_analytics.Dim_Salon dsl ON dsl.salon_key = f.salon_key
        LEFT JOIN ad_analytics.Dim_Site dsite ON dsite.site_key = f.site_key
        {where_sql}
    """


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    client = get_client()
    t0 = time.perf_counter()

    views = {
        "big_analytics_full": "WHERE f.`атрибуция` = 'По дате заявки'",
        "big_analytics_full_arrival": "WHERE f.`атрибуция` = 'По дате визита'",
    }
    rows: dict[str, int] = {}
    for table, where_sql in views.items():
        replace_view(client, f"ad_analytics.{table}", _wide_fact_sql(where_sql))
        rows[table] = count_rows(client, f"ad_analytics.{q(table)}")
        log.info("  %s view rows=%d", table, rows[table])

    # PIXEL_DEDUP_2026-08-15: step11 no longer copies `пиксель_атрибуц` rows into
    # big_analytics_full (that was the lead-axis duplicate of `_source_table='pixel'`),
    # so this step can no longer reconstruct big_analytics_pixel_score as a view over
    # fact_big_analytics/big_analytics_full — that tag doesn't exist there anymore, and
    # a view pointing back at big_analytics_pixel_score itself is a self-reference (the
    # object this same step drops/recreates). big_analytics_pixel_score simply stays the
    # physical table step11 produced: small (241k rows), untouched here, still readable
    # by step13's pixel branch and build_pbi_compat::build_pixel_score().

    # Сначала снять вьюхи источников с широкой таблицы, потом дропать её:
    # обратный порядок оставил бы окно, в котором они уже сломаны.
    for view, source_types in SOURCE_VIEWS.items():
        types_sql = ", ".join(f"'{item}'" for item in source_types)
        replace_view(
            client,
            f"ad_analytics.{view}",
            "SELECT * EXCEPT(key_pixel_score) FROM ad_analytics.big_analytics_full "
            f"WHERE `_source_table` IN ({types_sql})",
        )
        rows[view] = count_rows(client, f"ad_analytics.{q(view)}")
        log.info("  %s view rows=%d", view, rows[view])

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
    # Вьюхи источников — срез того же факта, что и big_analytics_full: в сумму
    # строк их не берём, иначе итог в data_quality_log считает одни и те же строки дважды.
    total = sum(value for key, value in rows.items() if key not in SOURCE_VIEWS)
    return {"rows": total, "details": details}


if __name__ == "__main__":
    print(run())
