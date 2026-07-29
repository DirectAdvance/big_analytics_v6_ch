"""
criterion_spend/build_criterion_spend.py — датамарт «расход по критерию (ключ/таргетинг)».

D2_SPEND_FIX_2026-06-18

ЦЕЛЬ: понимать, по каким КРИТЕРИЯМ (ключевые фразы / autotargeting / ретаргетинг /
интересы) кто тратит в разрезе кампаний и сетей показа. Близнец region_spend, но
грань — по criterion_id × criterion вместо локации.

ОТЛИЧИЕ ОТ region_spend:
  • вместо id_location/справочника локаций — criterion_id (= "CriterionId") +
    criterion (ОЧИЩЕННЫЙ текст _CRIT_CLEAN от "Criterion") + criterion_raw (исходный
    "Criterion" до очистки, для аудита) + производная criterion_type (CASE по очищенному);
  • НИКАКОГО справочника — criterion берётся напрямую в грань;
  • метрики и справочное обогащение из local_gsheet_sites — те же.

НОРМАЛИЗАЦИЯ criterion (_CRIT_CLEAN, порядок важен): срезать ведущие дефисы → обрезать
минус-слова (пробел+дефис и правее; дефис ВНУТРИ слова «бизнес-класс» сохраняется) →
удалить операторы ! + [ ] (двоеточие НЕ трогаем) → схлопнуть пробелы + trim.

criterion_type (CASE по тексту, доминирующие категории подтверждены в БД 2026-06-11):
  • '%autotargeting%'                    → 'autotargeting' (~5.2M строк, top «---autotargeting»)
  • '%ретаргетинг%'                      → 'retargeting'   (~176k, «офферный ретаргетинг»)
  • '%интерес%' OR '%привычк%'           → 'interests'     (~40k, «Интересы и привычки»)
  • иначе                                → 'keyword'       (ключевые фразы: «lada niva» и т.п.)

ИНВАРИАНТЫ (проверены в БД 2026-06-11):
  • двойного счёта НЕТ: CriterionId — часть детального ключа manager_reports (keys_with_both=0);
  • golden Кудерко (public.fact_big_analytics 25 422 774.00 / 47) НЕ затрагивается — другая таблица;
  • покрытие ~43% расхода (остальное NULL); 83.5k уникальных текстов / 283k CriterionId.

ГРАНЬ GROUP BY: date × campaign_id × ad_group_id × ad_network_type × criterion_id ×
ОЧИЩЕННЫЙ criterion (вариант B — criterion_id остаётся в грани).
row_hash = md5 этой грани (PK; вкл. criterion_id и ОЧИЩЕННЫЙ criterion — иначе разные
сырые тексты с одним id схлопывались бы в один очищенный → PK-коллизия).

СКОУП НИШИ: ОБЯЗАТЕЛЕН фильтр по нише «Авто» (domain ∈ Авто-множество local_gsheet_sites) —
1:1 как arp_fact/build_star.py — иначе таблица раздувается до 2-4М строк (детальная грань
по criterion). Авто-скоуп держит её в разумных границах и консистентной с остальными витринами.

ИДЕМПОТЕНТНОСТЬ: полный пересбор DROP+CTAS.

D2_SPEND_FIX_2026-06-18: откат фикса E (FDW_SINGLE_PASS_STAGING). Причины — см.
build_region_spend.py (двойной PK, staging 9.3GB, drop_staging не в finally).
Этот билдер больше НЕ является «последним» — drop_staging не вызывается (staging нет).

ЗАПУСК (вызывается из дневного пайплайна; ручной — отдельно):
  cd ~/big_analytics_v5 && ~/venv/bin/python3 criterion_spend/build_criterion_spend.py
"""

import logging
import os
import sys
import time
from datetime import datetime

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import DATE_FROM, DB_DST  # noqa: E402

logger = logging.getLogger('pipeline.build_criterion_spend')

TARGET_TABLE = 'fact_criterion_spend'
SRC_MANAGER  = 'yandex_direct_manager_reports'
GSHEET_SITES = 'local_gsheet_sites'

# Скоуп ниши «Авто» — 1:1 как star_refactor/build_star.py AUTO_DOMAIN_SET.
_AUTO_DOMAIN_SET = (
    "SELECT DISTINCT lower(trim(domain)) FROM public.local_gsheet_sites "
    "WHERE niche = 'Авто' AND domain IS NOT NULL AND trim(domain) <> ''"
)

# Нормализация текста "Criterion" (порядок важен), образец — _CAMPAIGN_CODE_EXPR в region_spend:
#   0) ПЕРВЫМ — заменить неразрывные/узкие пробелы на обычный пробел (в PostgreSQL `\s` НЕ ловит
#      U+00A0/NBSP, U+202F/narrow-NBSP, U+2009/thin → шаг 4 их не схлопывал, и «Интересы и
#      привычки» с NBSP (37 байт) не схлопывалась с обычным вариантом (36 байт) в PBI-матрице):
#      translate(crit, chr(160)||chr(8239)||chr(8201), '   ')
#   1) срезать ведущие дефисы: ---autotargeting → autotargeting   regexp_replace(crit,'^-+','')
#   2) обрезать минус-слова от первого ПРОБЕЛ+ДЕФИС и правее (дефис ВНУТРИ слова без пробела,
#      «бизнес-класс», сохраняется):                              regexp_replace(...,'\s+-.*$','')
#   3) удалить операторы ! + [ ] (ДВОЕТОЧИЕ НЕ трогаем):          regexp_replace(...,'[!+\[\]]','','g')
#   4) схлопнуть двойные пробелы + trim:                          trim(regexp_replace(...,'\s+',' ','g'))
# Проверено на реальных данных Victory 2026-06-11 (8 синтетич. примеров + minus-words + дефис-в-слове
# Changan UNI-K не режется; NBSP-нормализация: оба варианта «Интересы и привычки» (36/37 байт) → один).
_CRIT_CLEAN = (
    r"""trim(regexp_replace(regexp_replace(regexp_replace(regexp_replace("""
    r"""translate(m."Criterion", chr(160)||chr(8239)||chr(8201), '   '), """
    r"""'^-+', ''), '\s+-.*$', ''), '[!+\[\]]', '', 'g'), '\s+', ' ', 'g'))"""
)

# ── DDL ───────────────────────────────────────────────────────────────────────
DDL = f"""
CREATE TABLE IF NOT EXISTS public.{TARGET_TABLE} (
    row_hash            TEXT PRIMARY KEY,
    date                DATE,
    campaign_id         BIGINT,
    campaign_name       TEXT,
    ad_group_id         BIGINT,
    ad_network_type     TEXT,
    -- критерий
    criterion_id        BIGINT,
    criterion           TEXT,   -- ОЧИЩЕННЫЙ текст (_CRIT_CLEAN) — основной отображаемый
    criterion_raw       TEXT,   -- исходный текст до очистки (аудит/проверка)
    criterion_type      TEXT,   -- производная: autotargeting/retargeting/interests/keyword
    -- метрики из manager_reports (доступны по разрезу)
    impressions         BIGINT,
    clicks              BIGINT,
    cost                NUMERIC,
    "Все формы"           NUMERIC,
    "CRM: Заказ создан"   NUMERIC,
    "CRM: Заказ оплачен"  NUMERIC,
    "CRM: Спам заказ"     NUMERIC,
    "CRM: Заказ отменен"  NUMERIC,
    -- справочное обогащение из local_gsheet_sites по account_login = login_key
    domain              TEXT,
    логин               TEXT,
    директолог          TEXT,
    город               TEXT,
    регион              TEXT,
    салон               TEXT,
    шаблон              TEXT,
    шаблон_марка        TEXT,
    тип_сайта           TEXT,
    статус              TEXT,
    направление         TEXT,
    project_manager     TEXT,
    client_id           TEXT,
    -- кодер из CampaignName (та же логика что step1.py)
    campaign_code       TEXT,
    tp                  TEXT,
    cpc_cpa             TEXT,
    site_quiz           TEXT,
    updated_at          TIMESTAMP
)
"""

# Нормализация CampaignName и извлечение кодера — 1:1 как step1_load_raw/step1.py L74-90.
# REPLACE(chr(1089),'c') — кириллическая 'с'(U+0441) → латинская 'c' (иначе regex не матчит).
_CN_NORM = f'REPLACE(m."CampaignName", chr(1089), \'c\')'
_CODE_RE = "'(?i)(tp\\d+_(?:cpc|cpa)_(?:site|kviz|quiz))'"

_CAMPAIGN_CODE_EXPR = (
    f"REPLACE(REPLACE((REGEXP_MATCH({_CN_NORM}, {_CODE_RE}))[1], 'kviz','quiz'),'Kviz','Quiz')"
)
_TP_EXPR = (
    f"LOWER(SPLIT_PART(COALESCE((REGEXP_MATCH({_CN_NORM}, {_CODE_RE}))[1], ''), '_', 1))"
)
_CPC_CPA_EXPR = (
    f"LOWER(SPLIT_PART(COALESCE((REGEXP_MATCH({_CN_NORM}, {_CODE_RE}))[1], ''), '_', 2))"
)
_SITE_QUIZ_EXPR = (
    f"REPLACE(LOWER(SPLIT_PART(COALESCE((REGEXP_MATCH({_CN_NORM}, {_CODE_RE}))[1], ''), '_', 3)),"
    f" 'kviz','quiz')"
)


def _build_sql() -> str:
    """
    CTE agg: агрегируем manager_reports гранью (date,campaign_id,ad_group_id,
    ad_network_type,criterion_id,criterion). MAX(CampaignName) — кампания одна на campaign_id.
    criterion_type вычисляется от criterion-текста (он один на criterion в грани).
    Затем LEFT JOIN gsheet_sites по account_login + ОБЯЗАТЕЛЬНЫЙ фильтр ниши «Авто».
    """
    return f"""
    WITH agg AS (
        SELECT
            m."Date"::date                              AS date,
            m."CampaignId"                              AS campaign_id,
            MAX(m."CampaignName")                       AS campaign_name,
            m."AdGroupId"                               AS ad_group_id,
            m."AdNetworkType"                           AS ad_network_type,
            m."CriterionId"                             AS criterion_id,
            {_CRIT_CLEAN}                               AS criterion,
            MAX(m."Criterion")                          AS criterion_raw,
            MAX(LOWER(TRIM(m.account_login)))           AS account_login,
            SUM(m."Impressions")                        AS impressions,
            SUM(m."Clicks")                             AS clicks,
            ROUND(SUM(m.total_cost)::numeric, 2)         AS cost,
            SUM(m.all_forms)                            AS all_forms,
            SUM(m.crm_order_created)                    AS crm_order_created,
            SUM(m.crm_order_paid)                       AS crm_order_paid,
            SUM(m.crm_spam_order)                       AS crm_spam_order,
            SUM(m.crm_order_canceled)                   AS crm_order_canceled,
            MAX({_CAMPAIGN_CODE_EXPR})                  AS campaign_code,
            MAX({_TP_EXPR})                             AS tp,
            MAX({_CPC_CPA_EXPR})                        AS cpc_cpa,
            MAX({_SITE_QUIZ_EXPR})                      AS site_quiz
        FROM public.{SRC_MANAGER} m
        WHERE m."Date" IS NOT NULL
          AND m."Date" >= '{DATE_FROM}'
        GROUP BY
            m."Date"::date, m."CampaignId", m."AdGroupId",
            m."AdNetworkType", m."CriterionId", {_CRIT_CLEAN}
    )
    SELECT
        md5(
            COALESCE(a.date::text,'')               || '|' ||
            COALESCE(a.campaign_id::text,'')        || '|' ||
            COALESCE(a.ad_group_id::text,'')        || '|' ||
            COALESCE(a.ad_network_type,'')          || '|' ||
            COALESCE(a.criterion_id::text,'NULL')   || '|' ||
            COALESCE(a.criterion,'NULL')
        )                                                   AS row_hash,
        a.date,
        a.campaign_id,
        a.campaign_name,
        a.ad_group_id,
        a.ad_network_type,
        a.criterion_id,
        a.criterion,
        a.criterion_raw,
        -- criterion_type inline CASE (детерминирован от очищенного criterion)
        CASE
            WHEN a.criterion ILIKE '%autotargeting%'                              THEN 'autotargeting'
            WHEN a.criterion ILIKE '%ретаргетинг%'                                THEN 'retargeting'
            WHEN a.criterion ILIKE '%интерес%' OR a.criterion ILIKE '%привычк%'   THEN 'interests'
            ELSE 'keyword'
        END                                                 AS criterion_type,
        a.impressions,
        a.clicks,
        a.cost,
        a.all_forms                                         AS "Все формы",
        a.crm_order_created                                 AS "CRM: Заказ создан",
        a.crm_order_paid                                    AS "CRM: Заказ оплачен",
        a.crm_spam_order                                    AS "CRM: Спам заказ",
        a.crm_order_canceled                                AS "CRM: Заказ отменен",
        gs."domain"                                         AS domain,
        a.account_login                                     AS логин,
        gs."directologist"                                  AS директолог,
        gs."city"                                           AS город,
        gs."region"                                         AS регион,
        gs."salon"                                          AS салон,
        gs."template"                                       AS шаблон,
        CASE
            WHEN gs."template" IS NULL OR trim(gs."template") = '' OR gs."template" = 'НЕ УКАЗАН' THEN NULL
            WHEN gs."template" NOT LIKE '%%.vitmp.ru' THEN 'jac'
            WHEN SPLIT_PART(gs."template", '.vitmp.ru', 1) ~ '^quiz-\\d+$' THEN 'quiz'
            ELSE trim(regexp_replace(regexp_replace(regexp_replace(
                    regexp_replace(SPLIT_PART(gs."template", '.vitmp.ru', 1), '^quiz-', ''),
                    '-bu-\\d+$', ''),
                    '-v\\d+$', ''),
                    '-\\d+$', ''))
        END                                                 AS шаблон_марка,
        gs."site_type"                                      AS тип_сайта,
        gs."status"                                         AS статус,
        gs."direction"                                      AS направление,
        NULLIF(TRIM(gs."project_manager"), '')              AS project_manager,
        gs."client_id"                                      AS client_id,
        a.campaign_code,
        a.tp,
        a.cpc_cpa,
        a.site_quiz,
        NOW()                                               AS updated_at
    FROM agg a
    -- 1:1-обогащение: login_key в local_gsheet_sites НЕ уникален (4285 строк / 1168 ключей,
    -- 32 дубля: '' ×2663, 'Нет' ×420, реальные porg-*/e-*/direct19 ×2 — один аккаунт несколько
    -- доменов). Голый LEFT JOIN размножал агрегат → дубль row_hash → PK-коллизия CTAS. Гасим
    -- fan-out DISTINCT ON (login_key) с ДЕТЕРМИНИРОВАННЫМ tie-break (row_hash справочника) —
    -- ровно одна строка обогащения на ключ. Грань GROUP BY и состав row_hash НЕ меняются.
    LEFT JOIN (
        SELECT DISTINCT ON (login_key)
            login_key, domain, directologist, city, region, salon, template,
            site_type, status, direction, project_manager, client_id
        FROM public.{GSHEET_SITES}
        ORDER BY login_key, row_hash
    ) gs ON a.account_login = gs.login_key
    WHERE lower(trim(gs."domain")) IN ({_AUTO_DOMAIN_SET})
    """


def _build_staging_sql() -> str:
    """Тот же датамарт из public._spend_staging_tmp вместо повторного FDW-скана."""
    from spend.build_spend_staging import STAGING_TABLE
    return f"""
    WITH agg AS (
        SELECT
            s.date,
            s.campaign_id,
            MAX(s.campaign_name)       AS campaign_name,
            s.ad_group_id,
            s.ad_network_type,
            s.criterion_id,
            s.criterion,
            MAX(s.criterion_raw)       AS criterion_raw,
            MAX(s.account_login)       AS account_login,
            SUM(s.impressions)         AS impressions,
            SUM(s.clicks)              AS clicks,
            ROUND(SUM(s.cost)::numeric, 2) AS cost,
            SUM(s.all_forms)           AS all_forms,
            SUM(s.crm_order_created)   AS crm_order_created,
            SUM(s.crm_order_paid)      AS crm_order_paid,
            SUM(s.crm_spam_order)      AS crm_spam_order,
            SUM(s.crm_order_canceled)  AS crm_order_canceled,
            MAX(s.campaign_code)       AS campaign_code,
            MAX(s.tp)                  AS tp,
            MAX(s.cpc_cpa)             AS cpc_cpa,
            MAX(s.site_quiz)           AS site_quiz,
            MAX(s.domain)              AS domain,
            MAX(s.directologist)       AS directologist,
            MAX(s.city)                AS city,
            MAX(s.region)              AS region,
            MAX(s.salon)               AS salon,
            MAX(s.template)            AS template,
            MAX(s.шаблон_марка)        AS шаблон_марка,
            MAX(s.site_type)           AS site_type,
            MAX(s.status)              AS status,
            MAX(s.direction)           AS direction,
            MAX(s.project_manager)     AS project_manager,
            MAX(s.client_id)           AS client_id
        FROM public.{STAGING_TABLE} s
        GROUP BY
            s.date, s.campaign_id, s.ad_group_id,
            s.ad_network_type, s.criterion_id, s.criterion
    )
    SELECT
        md5(
            COALESCE(a.date::text,'')               || '|' ||
            COALESCE(a.campaign_id::text,'')        || '|' ||
            COALESCE(a.ad_group_id::text,'')        || '|' ||
            COALESCE(a.ad_network_type,'')          || '|' ||
            COALESCE(a.criterion_id::text,'NULL')   || '|' ||
            COALESCE(a.criterion,'NULL')
        )                                                   AS row_hash,
        a.date,
        a.campaign_id,
        a.campaign_name,
        a.ad_group_id,
        a.ad_network_type,
        a.criterion_id,
        a.criterion,
        a.criterion_raw,
        CASE
            WHEN a.criterion ILIKE '%autotargeting%'                              THEN 'autotargeting'
            WHEN a.criterion ILIKE '%ретаргетинг%'                                THEN 'retargeting'
            WHEN a.criterion ILIKE '%интерес%' OR a.criterion ILIKE '%привычк%'   THEN 'interests'
            ELSE 'keyword'
        END                                                 AS criterion_type,
        a.impressions,
        a.clicks,
        a.cost,
        a.all_forms                                         AS "Все формы",
        a.crm_order_created                                 AS "CRM: Заказ создан",
        a.crm_order_paid                                    AS "CRM: Заказ оплачен",
        a.crm_spam_order                                    AS "CRM: Спам заказ",
        a.crm_order_canceled                                AS "CRM: Заказ отменен",
        a.domain,
        a.account_login                                     AS логин,
        a.directologist                                     AS директолог,
        a.city                                              AS город,
        a.region                                            AS регион,
        a.salon                                             AS салон,
        a.template                                          AS шаблон,
        a.шаблон_марка                                      AS шаблон_марка,
        a.site_type                                         AS тип_сайта,
        a.status                                            AS статус,
        a.direction                                         AS направление,
        a.project_manager,
        a.client_id,
        a.campaign_code,
        a.tp,
        a.cpc_cpa,
        a.site_quiz,
        NOW()                                               AS updated_at
    FROM agg a
    WHERE lower(trim(a.domain)) IN ({_AUTO_DOMAIN_SET})
    """


_INDEXES = [
    # INDEX_AUDIT_2026-06-27: удалены мёртвые (idx_scan=0):
    #   idx_fcs_campaign, idx_fcs_crit_id, idx_fcs_criterion_lower, idx_fcs_crit_type, idx_fcs_salon.
    # Сохранён idx_fcs_date.
    f'CREATE INDEX IF NOT EXISTS idx_fcs_date     ON public.{TARGET_TABLE} (date)',
]


def run(conn, run_id: str = None, use_staging: bool = False) -> dict:
    """
    D2_SPEND_FIX_2026-06-18

    Контракт совпадает с build_unified.run(conn, run_id) → {'rows':..., 'details':...},
    чтобы вызываться из post-loop pipeline.py / fast_pipeline.py единообразно.

    Идемпотентный полный пересбор: DROP TABLE + CTAS + индексы + ANALYZE.

    D2_SPEND_FIX_2026-06-18: этот билдер больше НЕ является «последним» из трёх спенд-
    билдеров — drop_staging() не вызывается (staging-таблицы нет после отката фикса E).

    SPEND_WORKMEM_2026-06-17 (фикс C): SET LOCAL work_mem='192MB' перед CTAS.
    TEMP_FILE_LIMIT_SAFE_2026-06-18 (фикс F): SAVEPOINT вокруг SET LOCAL temp_file_limit
    — bi_analytic не имеет привилегии superuser → SET temp_file_limit = permission denied.
    SAVEPOINT позволяет откатить только неудачный SET, не трогая work_mem и транзакцию.
    DISKFREE_DROP_FIRST_2026-06-22: DROP в autocommit ДО CTAS — OS освобождает место
    старой таблицы (~2.6 GB) до начала построения новой, снижает пиковый расход на диске.
    """
    t0 = time.perf_counter()

    # DISKFREE_DROP_FIRST_2026-06-22: DROP в отдельном autocommit-коннекте.
    # Механика и обоснование — см. build_region_spend._drop_old_table.
    import psycopg2 as _psycopg2_cs
    _drop_conn_cs = _psycopg2_cs.connect(**DB_DST)
    _drop_conn_cs.autocommit = True
    try:
        with _drop_conn_cs.cursor() as _ddc:
            _ddc.execute(f'DROP TABLE IF EXISTS public.{TARGET_TABLE}')
        logger.info(
            'DISKFREE_DROP_FIRST_2026-06-22: DROP public.%s (autocommit)', TARGET_TABLE
        )
    finally:
        _drop_conn_cs.close()

    with conn.cursor() as cur:
        # SPEND_WORKMEM_2026-06-17 (фикс C): ограничивает work_mem только этой транзакцией.
        # SPEEDUP_WORKMEM_2026-06-18: 192MB → 384MB (см. build_region_spend.py за обоснование).
        cur.execute("SET LOCAL work_mem = '384MB'")
        # TEMP_FILE_LIMIT_SAFE_2026-06-18 (фикс F): bi_analytic не имеет прав superuser.
        cur.execute("SAVEPOINT before_tfl")
        try:
            cur.execute("SET LOCAL temp_file_limit = '20GB'")
            cur.execute("RELEASE SAVEPOINT before_tfl")
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT before_tfl")
        # DROP уже выполнен выше через autocommit.
        try:
            from spend.build_spend_staging import staging_exists
            _use_staging = use_staging and staging_exists(conn)
        except Exception:
            _use_staging = False
        logger.info(
            'build_criterion_spend: CTAS public.%s (%s)',
            TARGET_TABLE, 'spend_staging' if _use_staging else SRC_MANAGER
        )
        cur.execute(
            f'CREATE TABLE public.{TARGET_TABLE} AS '
            f'{_build_staging_sql() if _use_staging else _build_sql()}'
        )
        rows = cur.rowcount
        # PK + индексы (CTAS не копирует constraints из DDL-шаблона — ADD CONSTRAINT единственный)
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
        f'rows={rows}; {TARGET_TABLE} = manager_reports '
        f'(грань date×campaign_id×ad_group_id×ad_network_type×criterion_id×criterion) '
        f'+ LEFT JOIN {GSHEET_SITES} по account_login, скоуп ниши «Авто»; '
        f'D2_SPEND_FIX_2026-06-18; DISKFREE_DROP_FIRST_2026-06-22'
    )
    logger.info('build_criterion_spend: готово за %.1f сек, %d строк', elapsed, rows)
    return {'rows': rows, 'details': details}


# ── Standalone-режим (ручной запуск + DDL-guard на пустой БД) ──────────────────

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )
    start = datetime.now()
    logger.info('build_criterion_spend СТАРТ: %s', start)
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
        logger.exception('Ошибка build_criterion_spend: %s', e)
        raise
    finally:
        conn.close()


if __name__ == '__main__':
    main()
