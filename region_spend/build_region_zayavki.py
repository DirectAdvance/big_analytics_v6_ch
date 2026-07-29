"""
region_spend/build_region_zayavki.py — датамарт «воронка по ЗАЯВКАМ в разрезе региона».

ЦЕЛЬ: CRM-статусная воронка (kol_vo_zayavok..dobro) по заявкам, привязанным к РЕГИОНУ
(id_location), распарсенному из utm_content (`geoid:NNN`). Дополняет fact_region_spend
(тот — про РАСХОД по региону из manager_reports; этот — про ЗАЯВКИ по региону из лидов).

ОТЛИЧИЕ ОТ fact_region_spend:
  • источник — public.local_leads_all (CRM-лиды), НЕ manager_reports;
  • метрики — 12 счётчиков CRM-воронки (status/reason) через config.status_sql, НЕ расход;
  • дата — created_date (день ЗАЯВКИ), как у placement step2 (НЕ день показа);
  • грань — БЕЗ ad_network_type/ad_group (у лидов их нет → дробление задвоило бы лиды).

ГРАНЬ GROUP BY (PK row_hash = md5): created_date × campaign_id × id_location.
  • НЕ дробим по ad_network_type/ad_group — у лидов этих осей нет, так нет задвоения.
  • row_hash = md5(created_date | campaign_id | id_location).

ИСТОЧНИК и id_location:
  • public.local_leads_all;
  • id_location = (regexp_match(utm_content,'geoid:([0-9]+)'))[1]::bigint;
  • фильтр (как placement step2): created_date >= DATE_FROM,
      COALESCE(deal_type,'')<>'Звонок'
      AND COALESCE(is_copy_for_removal,false)=false AND campaign_id IS NOT NULL
      AND geoid IS NOT NULL.

ВОРОНКА (12 метрик, целочисленная CRM-статусная — НЕ пиксельная дробная):
  через config.status_sql.build_leads_agg_sql(conn) → kol_vo_zayavok, korr, kval, priezd,
  prodazhi, nekorr, ne_otvechaet, filtr, nedozvon, priedet, dohod_do_kredita, dobro.
  Пиксельную дробную атрибуцию НЕ трогаем (она живёт по key3 в основном факте, инвариант).

ОБОГАЩЕНИЕ:
  • LEFT JOIN local_gsheet_yandex_direct_id_location по id_location →
      location/Область/GeoRegionType/distance_km/distance_km_agreg
      (distance_km_agreg = INTEGER, верхняя граница бакета — синхрон с источником);
  • campaign_name — LEFT JOIN DISTINCT ON (CampaignId) из yandex_direct_manager_reports
      (1:1 на campaign_id, проверено 0 multi-name → без задвоения);
  • salon/domain_id — MAX по грани (НЕ 1:1: 1025 граней имеют >1 салона → MAX, не задвоение;
      метрики целочисленные, MAX скаляр их не размножает).
  Директолог/город/регион НЕ тянем: у лидов нет account_login, надёжной 1:1 связи на грань
  нет → во избежание задвоения оставляем только id_location-атрибуты + campaign_name + salon.

ПОКРЫТИЕ: geoid есть только у ~20% non-call лидов (154 835 заявок); матч на справочник
id_location ~98%. SUM(kol_vo_zayavok) по таблице ДОЛЖЕН дать ~154 835 (НЕ больше — иначе
задвоение по грани).

GOLDEN: fact_region_zayavki golden Кудерко (public.fact_big_analytics 25 422 774.00 / 47)
НЕ затрагивает — отдельная таблица, builder её не читает и не пишет; build_star не тронут.

ИДЕМПОТЕНТНОСТЬ: полный пересбор DROP+CTAS + PK + индексы + ANALYZE.

ЗАПУСК (вызывается из дневного пайплайна; ручной — отдельно):
  cd ~/big_analytics_v5 && ~/venv/bin/python3 region_spend/build_region_zayavki.py
"""

import logging
import os
import sys
import time
from datetime import datetime

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import DATE_FROM, DB_DST  # noqa: E402
from config.status_sql import build_leads_agg_sql  # noqa: E402

logger = logging.getLogger('pipeline.build_region_zayavki')

TARGET_TABLE  = 'fact_region_zayavki'
SRC_LEADS     = 'local_leads_all'
SRC_MANAGER   = 'yandex_direct_manager_reports'
DICT_LOCATION = 'local_gsheet_yandex_direct_id_location'

# id_location из utm_content (geoid:NNN)
_GEOID_EXPR = "(regexp_match(l.utm_content, 'geoid:([0-9]+)'))[1]::bigint"

# Фильтр заявок — как placement step2 (дата проекта + deal_type!='Звонок' AND не копия)
# + campaign_id + geoid. Без даты старые CRM-лиды попадают в PBI-срезы отдельной витрины.
_LEADS_FILTER = (
    f"l.created_date >= DATE '{DATE_FROM}' "
    "AND COALESCE(l.deal_type,'') <> 'Звонок' "
    "AND COALESCE(l.is_copy_for_removal, false) = false "
    "AND l.campaign_id IS NOT NULL "
    "AND l.utm_content ~ 'geoid:[0-9]+'"
)

# ── DDL ───────────────────────────────────────────────────────────────────────
DDL = f"""
CREATE TABLE IF NOT EXISTS public.{TARGET_TABLE} (
    row_hash            TEXT PRIMARY KEY,
    created_date        DATE,
    campaign_id         BIGINT,
    campaign_name       TEXT,
    -- регион заявки (geoid из utm_content; LEFT JOIN справочника)
    id_location         BIGINT,
    location            TEXT,
    "Область"           TEXT,
    "GeoRegionType"     TEXT,
    distance_km         INTEGER,
    distance_km_agreg   INTEGER,
    -- обогащение по грани (НЕ 1:1 → MAX, целочисленные метрики им не размножаются)
    салон               TEXT,
    domain_id           BIGINT,
    -- 12 метрик CRM-воронки (целочисленные счётчики; dohod_do_kredita/dobro NULLABLE)
    kol_vo_zayavok      NUMERIC,
    korr                NUMERIC,
    kval                NUMERIC,
    priezd              NUMERIC,
    prodazhi            NUMERIC,
    nekorr              NUMERIC,
    ne_otvechaet        NUMERIC,
    filtr               NUMERIC,
    nedozvon            NUMERIC,
    priedet             NUMERIC,
    dohod_do_kredita    NUMERIC,
    dobro               NUMERIC,
    updated_at          TIMESTAMP
)
"""


def _build_sql(agg_cases: str) -> str:
    """
    CTE agg: воронка из local_leads_all гранью (created_date, campaign_id, id_location).
    agg_cases — 12 COALESCE(SUM(CASE...)) из config.status_sql.build_leads_agg_sql(conn);
    ссылается на колонки status/reason/source_type/salon БЕЗ table-prefix → leads-CTE
    выбирает их под этими именами (alias 'l' раскрыт в подзапросе).

    Затем LEFT JOIN справочник локаций + LEFT JOIN campaign_name (DISTINCT ON campaign).
    """
    return f"""
    WITH leads AS (
        SELECT
            l.created_date                  AS created_date,
            l.campaign_id                   AS campaign_id,
            {_GEOID_EXPR}                   AS id_location,
            l.status                        AS status,
            l.reason                        AS reason,
            l.source_type                   AS source_type,
            l.salon                         AS salon,
            l.domain_id                     AS domain_id
        FROM public.{SRC_LEADS} l
        WHERE {_LEADS_FILTER}
    ),
    agg AS (
        SELECT
            created_date,
            campaign_id,
            id_location,
            MAX(salon)                      AS салон,
            MAX(domain_id)                  AS domain_id,
{agg_cases}
        FROM leads
        GROUP BY created_date, campaign_id, id_location
    )
    SELECT
        md5(
            COALESCE(a.created_date::text,'')   || '|' ||
            COALESCE(a.campaign_id::text,'')    || '|' ||
            COALESCE(a.id_location::text,'NULL')
        )                                                   AS row_hash,
        a.created_date,
        a.campaign_id,
        cn.campaign_name,
        a.id_location,
        d.location,
        d."Область",
        d."GeoRegionType",
        d.distance_km,
        d.distance_km_agreg,
        a.салон,
        a.domain_id,
        a.kol_vo_zayavok,
        a.korr,
        a.kval,
        a.priezd,
        a.prodazhi,
        a.nekorr,
        a.ne_otvechaet,
        a.filtr,
        a.nedozvon,
        a.priedet,
        a.dohod_do_kredita,
        a.dobro,
        NOW()                                               AS updated_at
    FROM agg a
    -- id_location уникален в справочнике (16547/16547), DISTINCT ON — страховка от дублей
    LEFT JOIN (
        SELECT DISTINCT ON (id_location)
            id_location, location, "Область", "GeoRegionType", distance_km, distance_km_agreg
        FROM public.{DICT_LOCATION}
        ORDER BY id_location, location
    ) d ON a.id_location = d.id_location
    -- campaign_name 1:1 на CampaignId в manager_reports (проверено 0 multi-name);
    -- DISTINCT ON — детерминированный страховочный tie-break (без задвоения грани)
    LEFT JOIN (
        SELECT DISTINCT ON ("CampaignId")
            "CampaignId" AS campaign_id, "CampaignName" AS campaign_name
        FROM public.{SRC_MANAGER}
        WHERE "CampaignId" IS NOT NULL
        ORDER BY "CampaignId", "CampaignName"
    ) cn ON a.campaign_id = cn.campaign_id
    """


_INDEXES = [
    # INDEX_AUDIT_2026-06-27: удалены мёртвые (idx_scan=0):
    #   idx_frz_date, idx_frz_campaign, idx_frz_salon.
    # Сохранён idx_frz_location (1 scan — используется build_star.py для Dim_Location).
    f'CREATE INDEX IF NOT EXISTS idx_frz_location ON public.{TARGET_TABLE} (id_location)',
]


def run(conn, run_id: str = None) -> dict:
    """
    Контракт совпадает с build_unified.run(conn, run_id) → {'rows':..., 'details':...},
    чтобы вызываться из post-loop pipeline.py / fast_pipeline.py единообразно.

    Идемпотентный полный пересбор: DROP TABLE + CTAS + PK + индексы + ANALYZE.
    """
    t0 = time.perf_counter()
    # 12 COALESCE(SUM(CASE...)) воронки строятся из local_crm_statuses (тот же
    # авторитетный источник, что у placement/step3/step13) — НЕ хардкод.
    agg_cases = build_leads_agg_sql(conn)

    with conn.cursor() as cur:
        logger.info('build_region_zayavki: DROP + CTAS public.%s', TARGET_TABLE)
        cur.execute(f'DROP TABLE IF EXISTS public.{TARGET_TABLE}')
        cur.execute(f'CREATE TABLE public.{TARGET_TABLE} AS {_build_sql(agg_cases)}')
        rows = cur.rowcount
        cur.execute(
            f'SELECT COUNT(*)::bigint FROM public.{TARGET_TABLE} '
            f"WHERE created_date < DATE '{DATE_FROM}'"
        )
        old_rows = cur.fetchone()[0]
        if old_rows:
            raise RuntimeError(
                f'{TARGET_TABLE}: DATE_FROM invariant failed, '
                f'{old_rows} rows before {DATE_FROM}'
            )
        cur.execute(
            f'ALTER TABLE public.{TARGET_TABLE} ADD CONSTRAINT {TARGET_TABLE}_pkey '
            f'PRIMARY KEY (row_hash)'
        )
        for isql in _INDEXES:
            cur.execute(isql)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(f'ANALYZE public.{TARGET_TABLE}')
    conn.commit()

    elapsed = time.perf_counter() - t0
    details = (
        f'rows={rows}; {TARGET_TABLE} = воронка заявок из {SRC_LEADS} '
        f'(created_date >= {DATE_FROM}; грань created_date×campaign_id×id_location, '
        f'geoid из utm_content) '
        f'+ LEFT JOIN {DICT_LOCATION} + campaign_name из {SRC_MANAGER}'
    )
    logger.info('build_region_zayavki: готово за %.1f сек, %d строк', elapsed, rows)
    return {'rows': rows, 'details': details}


# ── Standalone-режим (ручной запуск + DDL-guard на пустой БД) ──────────────────

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )
    start = datetime.now()
    logger.info('build_region_zayavki СТАРТ: %s', start)
    conn = psycopg2.connect(
        **DB_DST,
        options='-c statement_timeout=1800000 -c client_encoding=utf8',
    )
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(DDL)  # IF NOT EXISTS — на случай первого ручного запуска
        conn.commit()
        res = run(conn, run_id='manual')
        logger.info('ЗАВЕРШЕНО: %s', res['details'])
    except Exception as e:
        conn.rollback()
        logger.exception('Ошибка build_region_zayavki: %s', e)
        raise
    finally:
        conn.close()


if __name__ == '__main__':
    main()
