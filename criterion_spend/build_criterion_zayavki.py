"""
criterion_spend/build_criterion_zayavki.py — датамарт «воронка по ЗАЯВКАМ в разрезе
ключевой фразы (criterion)».

ЦЕЛЬ: CRM-статусная воронка (kol_vo_zayavok..dobro) по заявкам, стыкуемым к КРИТЕРИЮ
(ключевой фразе / autotargeting / ретаргетинг / интересу) через utm_term лида.
Дополняет fact_criterion_spend (тот — про РАСХОД по критерию из manager_reports;
этот — про ЗАЯВКИ по критерию из лидов).

ОТЛИЧИЕ ОТ fact_criterion_spend:
  • источник — public.local_leads_all (CRM-лиды), НЕ manager_reports;
  • метрики — 12 счётчиков CRM-воронки (status/reason) через config.status_sql, НЕ расход;
  • дата — created_date (день ЗАЯВКИ), как у placement step2 (НЕ день показа);
  • грань — БЕЗ ad_network_type/ad_group/criterion_id — у лидов их нет → дробление
    задвоило бы лиды (зеркально логике region_zayavki vs region_spend).

ГРАНЬ GROUP BY (PK row_hash = md5): created_date × campaign_id × criterion.
  • НЕ дробим по ad_network_type/ad_group/criterion_id — у лидов этих осей нет.
  • row_hash = md5(created_date | campaign_id | criterion).

ИСТОЧНИК и criterion:
  • public.local_leads_all;
  • criterion = lower(split_part(utm_term,'|',1)) — нормализованный текст фразы из UTM;
  • фильтр (как placement step2): created_date >= DATE_FROM,
      COALESCE(deal_type,'')<>'Звонок'
      AND COALESCE(is_copy_for_removal,false)=false AND campaign_id IS NOT NULL
      AND utm_term IS NOT NULL AND trim(utm_term) <> '';
  • стыковка с criterion: нормализованный utm_term совпадает с очищенным criterion
    из fact_criterion_spend по тексту (criterion_id почти везде NULL/0 → только текст).

ВОРОНКА (12 метрик, целочисленная CRM-статусная — НЕ пиксельная дробная):
  через config.status_sql.build_leads_agg_sql(conn) → kol_vo_zayavok, korr, kval, priezd,
  prodazhi, nekorr, ne_otvechaet, filtr, nedozvon, priedet, dohod_do_kredita, dobro.
  Пиксельную дробную атрибуцию НЕ трогаем (она живёт по key3 в основном факте, инвариант).

ОБОГАЩЕНИЕ:
  • criterion_type / criterion_raw (DISTINCT ON criterion) из fact_criterion_spend
    (там criterion = очищенный текст; criterion_raw = исходный до очистки);
    DISTINCT ON — страховка от кратного входа одного criterion (разные campaign/ad_group).
  • campaign_name — LEFT JOIN DISTINCT ON (CampaignId) из yandex_direct_manager_reports
    (1:1 на campaign_id; проверено 0 multi-name → без задвоения грани);
  • салон/domain_id — MAX по грани (НЕ 1:1: возможны многозначные → MAX, не задвоение;
    метрики целочисленные, MAX скаляр их не размножает).
  Директолог/город/регион НЕ тянем: у лидов нет account_login, надёжной 1:1 связи на грань
  нет → во избежание задвоения оставляем только criterion-атрибуты + campaign_name + salon.

ПОКРЫТИЕ: utm_term непустой у 186 783 из 930 026 лидов; матч на справочник criterion ~90%
(6751 из 7490 уникальных нормализованных utm_term). Матч лоссовый по дате (как у региона
~36%) — ожидаемо: created_date заявки ≠ дата показа.

GOLDEN: fact_criterion_zayavki golden Кудерко (public.fact_big_analytics 25 422 774.00 / 47)
НЕ затрагивает — отдельная таблица, builder её не читает и не пишет; build_star не тронут.

ИДЕМПОТЕНТНОСТЬ: полный пересбор DROP+CTAS + PK + индексы + ANALYZE.

ЗАПУСК (вызывается из дневного пайплайна; ручной — отдельно):
  cd ~/big_analytics_v5 && ~/venv/bin/python3 criterion_spend/build_criterion_zayavki.py
"""

# PATCH-CRITERION-ZAYAVKI-2026-06-15
# PATCH-ZAYAVKI-CASE-INSENSITIVE-JOIN-2026-06-15

import logging
import os
import sys
import time
from datetime import datetime

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import DATE_FROM, DB_DST  # noqa: E402
from config.status_sql import build_leads_agg_sql  # noqa: E402

logger = logging.getLogger('pipeline.build_criterion_zayavki')

TARGET_TABLE = 'fact_criterion_zayavki'
SRC_LEADS    = 'local_leads_all'
SRC_MANAGER  = 'yandex_direct_manager_reports'
SRC_SPEND    = 'fact_criterion_spend'

# Нормализация utm_term лида → ПОЛНАЯ цепочка _CRIT_CLEAN (зеркально build_criterion_spend),
# чтобы '---autotargeting' из utm_term совпадал с 'autotargeting' из fact_criterion_spend.
# split_part по '|' (берём первый сегмент) → lower → та же 4-шаговая очистка что в spend:
#   0) NBSP-нормализация; 1) срезать ведущие дефисы; 2) обрезать минус-слова;
#   3) удалить операторы ! + [ ]; 4) схлопнуть пробелы + trim.
# Без полной нормализации '---autotargeting' (utm_term) ≠ 'autotargeting' (spend.criterion)
# → 73.6% заявок получают NULL criterion_type (непригодно).
_UTM_TERM_EXPR = (
    r"""trim(regexp_replace(regexp_replace(regexp_replace(regexp_replace("""
    r"""translate(lower(split_part(l.utm_term, '|', 1)), chr(160)||chr(8239)||chr(8201), '   '), """
    r"""'^-+', ''), '\s+-.*$', ''), '[!+\[\]]', '', 'g'), '\s+', ' ', 'g'))"""
)

# Фильтр заявок — как placement step2 (дата проекта + deal_type!='Звонок' AND не копия)
# + campaign_id + utm_term. Без даты старые CRM-лиды попадают в PBI-срезы отдельной витрины.
_LEADS_FILTER = (
    f"l.created_date >= DATE '{DATE_FROM}' "
    "AND COALESCE(l.deal_type,'') <> 'Звонок' "
    "AND COALESCE(l.is_copy_for_removal, false) = false "
    "AND l.campaign_id IS NOT NULL "
    "AND l.utm_term IS NOT NULL "
    "AND trim(l.utm_term) <> ''"
)

# ── DDL ───────────────────────────────────────────────────────────────────────
DDL = f"""
CREATE TABLE IF NOT EXISTS public.{TARGET_TABLE} (
    row_hash            TEXT PRIMARY KEY,
    created_date        DATE,
    campaign_id         BIGINT,
    campaign_name       TEXT,
    -- критерий заявки (нормализованный lower(split_part(utm_term,'|',1)))
    criterion           TEXT,
    criterion_type      TEXT,   -- autotargeting/retargeting/interests/keyword (из fact_criterion_spend)
    criterion_raw       TEXT,   -- исходный текст до очистки (из fact_criterion_spend, для аудита)
    -- обогащение по грани (НЕ 1:1 → MAX, целочисленные метрики им не размножаются)
    салон               TEXT,
    domain_id           BIGINT,
    шаблон              TEXT,
    шаблон_марка        TEXT,
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
    CTE leads: фильтр local_leads_all → нормализованный criterion из utm_term.
    CTE agg: воронка гранью (created_date, campaign_id, criterion).
    agg_cases — 12 COALESCE(SUM(CASE...)) из config.status_sql.build_leads_agg_sql(conn);
    ссылается на колонки status/reason/source_type/salon БЕЗ table-prefix → leads-CTE
    выбирает их под этими именами.

    Затем LEFT JOIN fact_criterion_spend (criterion_type/criterion_raw по нормализованному
    тексту criterion) + LEFT JOIN campaign_name из yandex_direct_manager_reports.

    ВАЖНО: LEFT JOIN fact_criterion_spend может не найти строку — лид с utm_term
    без matching criterion в spend (матч ~90%) → criterion_type/criterion_raw = NULL.
    Это ожидаемо (как NULL location в region_zayavki у ~2% незаматченных).
    """
    return f"""
    WITH leads AS (
        SELECT
            l.created_date                  AS created_date,
            l.campaign_id                   AS campaign_id,
            {_UTM_TERM_EXPR}                AS criterion,
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
            criterion,
            MAX(salon)                      AS салон,
            MAX(domain_id)                  AS domain_id,
{agg_cases}
        FROM leads
        GROUP BY created_date, campaign_id, criterion
    )
    SELECT
        md5(
            COALESCE(a.created_date::text,'')   || '|' ||
            COALESCE(a.campaign_id::text,'')    || '|' ||
            COALESCE(a.criterion,'NULL')
        )                                                   AS row_hash,
        a.created_date,
        a.campaign_id,
        cn.campaign_name,
        a.criterion,
        cs.criterion_type,
        cs.criterion_raw,
        a.салон,
        a.domain_id,
        fcs.шаблон                                                  AS шаблон,
        fcs.шаблон_марка                                            AS шаблон_марка,
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
    -- criterion_type/criterion_raw из fact_criterion_spend по нормализованному тексту.
    -- DISTINCT ON (criterion) — страховка: один criterion может встречаться
    -- в нескольких campaign_id/ad_group_id в spend-таблице; берём одну строку.
    LEFT JOIN (
        SELECT DISTINCT ON (lower(criterion))
            criterion, criterion_type, criterion_raw
        FROM public.{SRC_SPEND}
        WHERE criterion IS NOT NULL
        ORDER BY lower(criterion), criterion_type, criterion_raw
    ) cs ON lower(cs.criterion) = a.criterion
    -- campaign_name 1:1 на CampaignId в manager_reports (проверено 0 multi-name);
    -- DISTINCT ON — детерминированный страховочный tie-break (без задвоения грани)
    LEFT JOIN (
        SELECT DISTINCT ON ("CampaignId")
            "CampaignId" AS campaign_id, "CampaignName" AS campaign_name
        FROM public.{SRC_MANAGER}
        WHERE "CampaignId" IS NOT NULL
        ORDER BY "CampaignId", "CampaignName"
    ) cn ON a.campaign_id = cn.campaign_id
    -- шаблон/шаблон_марка по campaign_id из fact_criterion_spend (campaign_id → шаблон 1:1)
    -- требует что fact_criterion_spend уже пересобрана
    LEFT JOIN (
        SELECT DISTINCT ON (campaign_id)
            campaign_id,
            шаблон,
            шаблон_марка
        FROM public.{SRC_SPEND}
        WHERE шаблон IS NOT NULL AND trim(шаблон) <> '' AND шаблон <> 'НЕ УКАЗАН'
        ORDER BY campaign_id, шаблон
    ) fcs ON a.campaign_id = fcs.campaign_id
    """


_INDEXES = [
    # INDEX_AUDIT_2026-06-27: удалены все 5 мёртвых (idx_scan=0):
    #   idx_fcz_date, idx_fcz_campaign, idx_fcz_criterion, idx_fcz_crit_type, idx_fcz_salon.
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
        logger.info('build_criterion_zayavki: DROP + CTAS public.%s', TARGET_TABLE)
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
        f'(created_date >= {DATE_FROM}; грань created_date×campaign_id×criterion, '
        f'criterion=lower(split_part(utm_term,|,1))) '
        f'+ LEFT JOIN {SRC_SPEND} (criterion_type/raw) + campaign_name из {SRC_MANAGER}'
    )
    logger.info('build_criterion_zayavki: готово за %.1f сек, %d строк', elapsed, rows)
    return {'rows': rows, 'details': details}


# ── Standalone-режим (ручной запуск + DDL-guard на пустой БД) ──────────────────

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )
    start = datetime.now()
    logger.info('build_criterion_zayavki СТАРТ: %s', start)
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
        logger.exception('Ошибка build_criterion_zayavki: %s', e)
        raise
    finally:
        conn.close()


if __name__ == '__main__':
    main()
