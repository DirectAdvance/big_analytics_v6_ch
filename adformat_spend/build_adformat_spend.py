"""
adformat_spend/build_adformat_spend.py — датамарт «расход по формату объявления».

D2_SPEND_FIX_2026-06-18

ЦЕЛЬ: понимать, на какие ФОРМАТЫ ОБЪЯВЛЕНИЙ (AdFormat) кто тратит в разрезе кампаний
и сетей показа. Близнец region_spend, но грань — по ad_format вместо локации.

ОТЛИЧИЕ ОТ region_spend:
  • вместо id_location/справочника локаций — одна колонка ad_format (= manager_reports."AdFormat") как есть;
  • НИКАКОГО справочника локаций — AdFormat берётся напрямую в грань;
  • метрики и справочное обогащение из local_gsheet_sites — те же.

ИНВАРИАНТЫ (проверены в БД 2026-06-11):
  • двойного счёта НЕТ: AdFormat — часть детального ключа manager_reports (keys_with_both=0);
  • golden Кудерко (public.fact_big_analytics 25 422 774.00 / 47) НЕ затрагивается — другая таблица;
  • покрытие ~43% расхода (остальное NULL — Яндекс не отдаёт разрез); AdFormat: 5 значений.

ГРАНЬ GROUP BY: date × campaign_id × ad_group_id × ad_network_type × ad_format.
row_hash = md5 этой грани (PK; вкл. COALESCE(ad_format,'NULL')).

СКОУП НИШИ: фильтр по нише «Авто» (domain ∈ Авто-множество local_gsheet_sites) — для
единообразия со star/criterion (таблица крошечная, но фильтр держит её в Авто-скоупе,
чтобы PBI-слайсы были консистентны с остальными датамартами).

ИДЕМПОТЕНТНОСТЬ: полный пересбор DROP+CTAS.

D2_SPEND_FIX_2026-06-18: откат фикса E (FDW_SINGLE_PASS_STAGING). Причины — см.
build_region_spend.py (двойной PK, staging 9.3GB, drop_staging не в finally).
VARA_DROP_LOCAL_YANDEX_FDW_2026-06-17: SRC_YANDEX = FDW (не local_yandex).
  adgroup_code вычисляется regex из "AdGroupName" напрямую из FDW.
  ВАЖНО: regex в f-string (не raw) — используем \\\\d вместо \\d (иначе SyntaxWarning + неверная regex на Python 3.12+).

ЗАПУСК (вызывается из дневного пайплайна; ручной — отдельно):
  cd ~/big_analytics_v5 && ~/venv/bin/python3 adformat_spend/build_adformat_spend.py
"""

import logging
import os
import sys
import time
from datetime import datetime

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import DATE_FROM, DB_DST  # noqa: E402

logger = logging.getLogger('pipeline.build_adformat_spend')

TARGET_TABLE   = 'fact_adformat_spend'
SRC_MANAGER    = 'yandex_direct_manager_reports'
GSHEET_SITES   = 'local_gsheet_sites'
GSHEET_NAMING  = 'local_gsheet_naming'
# VARA_DROP_LOCAL_YANDEX_FDW_2026-06-17: adgroup_code берём напрямую из FDW.
# В FDW нет отдельного столбца adgroup_code — вычисляем regex из "AdGroupName" в JOIN.
# SRC_YANDEX теперь = FDW (не local_yandex).
SRC_YANDEX     = 'yandex_direct_manager_reports'

# Скоуп ниши «Авто» — 1:1 как star_refactor/build_star.py AUTO_DOMAIN_SET.
_AUTO_DOMAIN_SET = (
    "SELECT DISTINCT lower(trim(domain)) FROM public.local_gsheet_sites "
    "WHERE niche = 'Авто' AND domain IS NOT NULL AND trim(domain) <> ''"
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
    -- формат объявления (русское название: IMAGE→графический, TEXT→текстовый (ТГО),
    --   VIDEO→видео, SMART_*→смарт-баннер, ADAPTIVE_IMAGE→адаптивный графический,
    --   multicard→комбинаторное объявление; неизвестные коды оставляются как есть; NULL = «формат не определён»)
    ad_format           TEXT,
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
    -- обогащение группой объявлений из FDW (JOIN по ad_group_id = AdGroupId)
    -- ПАТЧ-adgroup-enrich-2026-06-15 / VARA_DROP_LOCAL_YANDEX_FDW_2026-06-17
    adgroup_code        TEXT,
    adgroup_brand       TEXT,
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

# VARA_DROP_LOCAL_YANDEX_FDW_2026-06-17: regex для adgroup_code из "AdGroupName" в FDW.
# Вынесена как отдельная константа (не inline в f-string) чтобы избежать SyntaxWarning
# на \d в f-string в Python 3.12+. В SQL-паттерне \d — корректный regex-токен PostgreSQL.
_ADGROUP_CODE_RE = r"'(ct\d+_(?:aoff|aon)_n\d+_r\d+_ct\d+_ag\d+_g\d+)'"


def _build_sql() -> str:
    """
    CTE agg: агрегируем manager_reports гранью (date,campaign_id,ad_group_id,
    ad_network_type,ad_format). MAX(CampaignName) — кампания одна на campaign_id.
    Затем LEFT JOIN gsheet_sites по account_login и фильтр по нише «Авто» (gs.domain).

    adgroup_code: из FDW yandex_direct_manager_reports по ad_group_id = AdGroupId.
    VARA_DROP_LOCAL_YANDEX_FDW_2026-06-17: В FDW нет готового столбца adgroup_code
    - вычисляем regex из "AdGroupName" (та же логика что step1._build_raw_yandex_sql).
    DISTINCT ON (AdGroupId) гасит fan-out.

    adgroup_brand: split_part(adgroup_code,'_',1) → lookup в local_gsheet_naming
    WHERE type='ag_part1'. Строки не ct-формата дадут NULL в brand (первая часть не матчит).
    """
    return f"""
    WITH agg AS (
        SELECT
            m."Date"::date                              AS date,
            m."CampaignId"                              AS campaign_id,
            MAX(m."CampaignName")                       AS campaign_name,
            m."AdGroupId"                               AS ad_group_id,
            m."AdNetworkType"                           AS ad_network_type,
            -- ПАТЧ-adformat-ru: маппинг AdFormat → русские названия
            CASE m."AdFormat"
                WHEN 'IMAGE'          THEN 'графический'
                WHEN 'TEXT'           THEN 'текстовый (ТГО)'
                WHEN 'VIDEO'          THEN 'видео'
                WHEN 'SMART_SINGLE'   THEN 'смарт-баннер'
                WHEN 'SMART_MULTIPLE' THEN 'смарт-баннер'
                WHEN 'SMART_TILE'     THEN 'смарт-баннер'
                WHEN 'ADAPTIVE_IMAGE' THEN 'адаптивный графический'
                WHEN 'multicard'      THEN 'комбинаторное объявление'
                ELSE m."AdFormat"
            END                                         AS ad_format,
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
            m."AdNetworkType",
            CASE m."AdFormat"
                WHEN 'IMAGE'          THEN 'графический'
                WHEN 'TEXT'           THEN 'текстовый (ТГО)'
                WHEN 'VIDEO'          THEN 'видео'
                WHEN 'SMART_SINGLE'   THEN 'смарт-баннер'
                WHEN 'SMART_MULTIPLE' THEN 'смарт-баннер'
                WHEN 'SMART_TILE'     THEN 'смарт-баннер'
                WHEN 'ADAPTIVE_IMAGE' THEN 'адаптивный графический'
                WHEN 'multicard'      THEN 'комбинаторное объявление'
                ELSE m."AdFormat"
            END
    )
    SELECT
        md5(
            COALESCE(a.date::text,'')           || '|' ||
            COALESCE(a.campaign_id::text,'')    || '|' ||
            COALESCE(a.ad_group_id::text,'')    || '|' ||
            COALESCE(a.ad_network_type,'')      || '|' ||
            COALESCE(a.ad_format,'NULL')
        )                                                   AS row_hash,
        a.date,
        a.campaign_id,
        a.campaign_name,
        a.ad_group_id,
        a.ad_network_type,
        a.ad_format,
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
        gs."site_type"                                      AS тип_сайта,
        gs."status"                                         AS статус,
        gs."direction"                                      AS направление,
        NULLIF(TRIM(gs."project_manager"), '')              AS project_manager,
        gs."client_id"                                      AS client_id,
        a.campaign_code,
        a.tp,
        a.cpc_cpa,
        a.site_quiz,
        -- ПАТЧ-adgroup-enrich-2026-06-15 / VARA_DROP_LOCAL_YANDEX_FDW_2026-06-17:
        -- adgroup_code из FDW yandex_direct_manager_reports (regex из "AdGroupName").
        ly.adgroup_code,
        gn.name                                             AS adgroup_brand,
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
    -- VARA_DROP_LOCAL_YANDEX_FDW_2026-06-17: adgroup_code из FDW (не local_yandex).
    -- _ADGROUP_CODE_RE вынесена как raw-константа вне f-string (Python 3.12 SyntaxWarning).
    LEFT JOIN (
        SELECT DISTINCT ON ("AdGroupId") "AdGroupId",
            (REGEXP_MATCH(
                SPLIT_PART("AdGroupName", ' — ', 1),
                {_ADGROUP_CODE_RE}
            ))[1] AS adgroup_code
        FROM public.{SRC_YANDEX}
        WHERE "AdGroupName" IS NOT NULL
        ORDER BY "AdGroupId", "AdGroupName"
    ) ly ON a.ad_group_id = ly."AdGroupId"
    -- adgroup_brand: split_part(...,'_',1) → local_gsheet_naming type='ag_part1'
    LEFT JOIN (
        SELECT code, name
        FROM public.{GSHEET_NAMING}
        WHERE type = 'ag_part1'
    ) gn ON gn.code = split_part(ly.adgroup_code, '_', 1)
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
            s.ad_format,
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
            MAX(s.site_type)           AS site_type,
            MAX(s.status)              AS status,
            MAX(s.direction)           AS direction,
            MAX(s.project_manager)     AS project_manager,
            MAX(s.client_id)           AS client_id
        FROM public.{STAGING_TABLE} s
        GROUP BY
            s.date, s.campaign_id, s.ad_group_id,
            s.ad_network_type, s.ad_format
    ),
    adgroup_dict AS (
        SELECT DISTINCT ON (ad_group_id)
            ad_group_id,
            adgroup_code
        FROM public.{STAGING_TABLE}
        WHERE ad_group_name IS NOT NULL
        ORDER BY ad_group_id, ad_group_name
    )
    SELECT
        md5(
            COALESCE(a.date::text,'')           || '|' ||
            COALESCE(a.campaign_id::text,'')    || '|' ||
            COALESCE(a.ad_group_id::text,'')    || '|' ||
            COALESCE(a.ad_network_type,'')      || '|' ||
            COALESCE(a.ad_format,'NULL')
        )                                                   AS row_hash,
        a.date,
        a.campaign_id,
        a.campaign_name,
        a.ad_group_id,
        a.ad_network_type,
        a.ad_format,
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
        a.site_type                                         AS тип_сайта,
        a.status                                            AS статус,
        a.direction                                         AS направление,
        a.project_manager,
        a.client_id,
        a.campaign_code,
        a.tp,
        a.cpc_cpa,
        a.site_quiz,
        ag.adgroup_code,
        gn.name                                             AS adgroup_brand,
        NOW()                                               AS updated_at
    FROM agg a
    LEFT JOIN adgroup_dict ag ON a.ad_group_id = ag.ad_group_id
    LEFT JOIN (
        SELECT code, name
        FROM public.{GSHEET_NAMING}
        WHERE type = 'ag_part1'
    ) gn ON gn.code = split_part(ag.adgroup_code, '_', 1)
    WHERE lower(trim(a.domain)) IN ({_AUTO_DOMAIN_SET})
    """


_INDEXES = [
    # INDEX_AUDIT_2026-06-27: удалены мёртвые (idx_scan=0):
    #   idx_fafs_campaign, idx_fafs_format, idx_fafs_salon.
    # Сохранён idx_fafs_date.
    f'CREATE INDEX IF NOT EXISTS idx_fafs_date     ON public.{TARGET_TABLE} (date)',
]


def run(conn, run_id: str = None, use_staging: bool = False) -> dict:
    """
    D2_SPEND_FIX_2026-06-18

    Контракт совпадает с build_unified.run(conn, run_id) → {'rows':..., 'details':...},
    чтобы вызываться из post-loop pipeline.py / fast_pipeline.py единообразно.

    Идемпотентный полный пересбор: DROP TABLE + CTAS + индексы + ANALYZE.

    SPEND_WORKMEM_2026-06-17 (фикс C): SET LOCAL work_mem='192MB' перед CTAS.
    TEMP_FILE_LIMIT_SAFE_2026-06-18 (фикс F): SAVEPOINT вокруг SET LOCAL temp_file_limit
    — bi_analytic не имеет привилегии superuser → SET temp_file_limit = permission denied.
    SAVEPOINT позволяет откатить только неудачный SET, не трогая work_mem и транзакцию.
    DISKFREE_DROP_FIRST_2026-06-22: DROP в autocommit ДО CTAS — OS освобождает место
    старой таблицы (~1.5 GB) до начала построения новой, снижает пиковый расход на диске.
    """
    t0 = time.perf_counter()

    # DISKFREE_DROP_FIRST_2026-06-22: DROP в отдельном autocommit-коннекте.
    # Механика и обоснование — см. build_region_spend._drop_old_table.
    import psycopg2 as _psycopg2_af
    _drop_conn_af = _psycopg2_af.connect(**DB_DST)
    _drop_conn_af.autocommit = True
    try:
        with _drop_conn_af.cursor() as _ddc:
            _ddc.execute(f'DROP TABLE IF EXISTS public.{TARGET_TABLE}')
        logger.info(
            'DISKFREE_DROP_FIRST_2026-06-22: DROP public.%s (autocommit)', TARGET_TABLE
        )
    finally:
        _drop_conn_af.close()

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
            'build_adformat_spend: CTAS public.%s (%s)',
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
        f'(грань date×campaign_id×ad_group_id×ad_network_type×ad_format) '
        f'+ LEFT JOIN {GSHEET_SITES} по account_login, скоуп ниши «Авто»; '
        f'D2_SPEND_FIX_2026-06-18; DISKFREE_DROP_FIRST_2026-06-22'
    )
    logger.info('build_adformat_spend: готово за %.1f сек, %d строк', elapsed, rows)
    return {'rows': rows, 'details': details}


# ── Standalone-режим (ручной запуск + DDL-guard на пустой БД) ──────────────────

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )
    start = datetime.now()
    logger.info('build_adformat_spend СТАРТ: %s', start)
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
        logger.exception('Ошибка build_adformat_spend: %s', e)
        raise
    finally:
        conn.close()


if __name__ == '__main__':
    main()
