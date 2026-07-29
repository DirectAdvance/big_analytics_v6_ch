"""
explain_all.py — EXPLAIN ANALYZE замер всех измеримых шагов пайплайна.
READ-ONLY: только EXPLAIN (ANALYZE, BUFFERS, VERBOSE) SELECT. БЕЗ DDL/INSERT/UPDATE.
Пайплайн НЕ запускается, данные НЕ меняются.
Конфиг сессии — ДЕФОЛТНЫЙ (work_mem не трогаем — нужен baseline, сравнимый с реальным прогоном).
"""

import sys
import time
import os

sys.path.insert(0, os.path.expanduser('~/big_analytics_v5'))

import psycopg2
from config.settings import DB_DST, EXCLUDED_DOMAIN_IDS, T_YANDEX_LOCAL, T_LEADS_ALL_LOCAL, T_DOMAINS_LOCAL
from config.status_sql import build_leads_agg_sql

# ──────────────────────────────────────────────────────────────────────────────
# Подключение: autocommit=True, statement_timeout=2400с, БЕЗ set work_mem.
# ──────────────────────────────────────────────────────────────────────────────
conn = psycopg2.connect(
    **DB_DST,
    options='-c statement_timeout=2400000 -c client_encoding=utf8',
)
conn.autocommit = True
print('Connected to DB.', flush=True)

# ──────────────────────────────────────────────────────────────────────────────
# Утилита замера
# ──────────────────────────────────────────────────────────────────────────────
def explain_step(name: str, select_sql: str):
    """Выполнить EXPLAIN ANALYZE на select_sql и напечатать план с разделителем."""
    print(f'\n{"="*60}', flush=True)
    print(f'===== {name} [START] =====', flush=True)
    print(f'{"="*60}', flush=True)
    sys.stdout.flush()

    t0 = time.perf_counter()
    try:
        with conn.cursor() as cur:
            cur.execute(f'EXPLAIN (ANALYZE, BUFFERS, VERBOSE) {select_sql}')
            rows = cur.fetchall()
        elapsed = time.perf_counter() - t0
        print(f'===== {name} wall={elapsed:.1f}с =====', flush=True)
        for row in rows:
            print(row[0], flush=True)
    except Exception as e:
        elapsed = time.perf_counter() - t0
        print(f'===== {name} ERROR after {elapsed:.1f}с: {e} =====', flush=True)
    sys.stdout.flush()


def skip_step(name: str, reason: str):
    print(f'\n===== {name} SKIPPED: {reason} =====', flush=True)
    sys.stdout.flush()


# ──────────────────────────────────────────────────────────────────────────────
# STEP 1a: raw_yandex — SELECT-часть из _build_raw_yandex_sql()
# Источник: local_yandex (10.78М строк, персистентная)
# Оригинальный SQL = DROP + CREATE UNLOGGED TABLE AS <SELECT>.
# Берём только SELECT (без DDL).
# ──────────────────────────────────────────────────────────────────────────────
_raw_yandex_select = f"""
    SELECT
        id,
        "Date",
        "CampaignId",
        "CampaignName",
        "AdGroupId",
        "AdGroupName",
        "AdNetworkType",
        "Device",
        "RlAdjustmentId",
        "Impressions",
        "Clicks",
        total_cost,
        account_login,
        manager_login,
        (REGEXP_MATCH(
            SPLIT_PART("AdGroupName", ' — ', 1),
            '(ct\\d+_(?:aoff|aon)_n\\d+_r\\d+_ct\\d+_ag\\d+_g\\d+)'
        ))[1] AS adgroup_code,
        REPLACE(REPLACE(
            (REGEXP_MATCH(REPLACE("CampaignName", chr(1089), 'c'),
                '(?i)(tp\\d+_(?:cpc|cpa)_(?:site|kviz|quiz))'))[1],
            'kviz', 'quiz'), 'Kviz', 'Quiz') AS campaign_code,
        LOWER(SPLIT_PART(
            COALESCE((REGEXP_MATCH(REPLACE("CampaignName", chr(1089), 'c'),
                '(?i)(tp\\d+_(?:cpc|cpa)_(?:site|kviz|quiz))'))[1], ''),
            '_', 1)) AS tp,
        LOWER(SPLIT_PART(
            COALESCE((REGEXP_MATCH(REPLACE("CampaignName", chr(1089), 'c'),
                '(?i)(tp\\d+_(?:cpc|cpa)_(?:site|kviz|quiz))'))[1], ''),
            '_', 2)) AS cpc_cpa,
        REPLACE(LOWER(SPLIT_PART(
            COALESCE((REGEXP_MATCH(REPLACE("CampaignName", chr(1089), 'c'),
                '(?i)(tp\\d+_(?:cpc|cpa)_(?:site|kviz|quiz))'))[1], ''),
            '_', 3)), 'kviz', 'quiz') AS site_quiz,
        DATE_TRUNC('week', "Date")::date AS week_start,
        LOWER(
            COALESCE(TO_CHAR("Date"::date, 'YYYY-MM-DD'), '') || '|' ||
            COALESCE("CampaignId"::TEXT, '0') ||
            CASE
                WHEN (REGEXP_MATCH("CampaignName",
                    '(?i)(tp[67]_)'))[1] IS NOT NULL THEN '|0'
                ELSE '|' || COALESCE("AdGroupId"::TEXT, '0')
            END || '|' ||
            COALESCE(
                CASE LOWER(COALESCE("Device",''))
                    WHEN 'mobile'   THEN 'mobile'
                    WHEN 'desktop'  THEN 'desktop'
                    WHEN 'tablet'   THEN 'tablet'
                    WHEN 'smart_tv' THEN 'smart_tv'
                    ELSE '0'
                END, '0'
            ) || '|' ||
            COALESCE("RlAdjustmentId"::TEXT, '0')
        ) AS key3
    FROM {T_YANDEX_LOCAL}
    WHERE "CampaignId" IS NOT NULL AND "CampaignId" != 0
"""

# ──────────────────────────────────────────────────────────────────────────────
# STEP 1b: raw_leads — SELECT-часть из _build_raw_leads_sql()
# Источник: local_leads_all (930К) + local_domains
# ──────────────────────────────────────────────────────────────────────────────
_excl_sql = ''
if EXCLUDED_DOMAIN_IDS:
    ids = ', '.join(str(i) for i in EXCLUDED_DOMAIN_IDS)
    _excl_sql = f' AND (l.domain_id NOT IN ({ids}) OR l.domain_id IS NULL)'

_raw_leads_select = f"""
    SELECT
        l.id,
        l.created_date,
        l.arrival_date,
        l.domain_id,
        d.name AS domain,
        l.deal_type,
        l.status,
        l.source_type,
        l.campaign_id,
        l.group_id,
        l.correction_id,
        l.utm_source,
        l.utm_medium,
        l.utm_campaign,
        l.utm_content,
        l.utm_term,
        l.phone,
        l.yclid,
        l.is_copy_for_removal,
        l.reason,
        l.salon,
        CASE
            WHEN utm_content LIKE '%fid:%'
            THEN TRIM(REGEXP_REPLACE(
                SPLIT_PART(utm_content, 'fid:', 2), '\\|.*', ''))
            ELSE NULL
        END AS fid,
        LOWER(
            COALESCE(TO_CHAR(created_date, 'YYYY-MM-DD'), '') || '|' ||
            COALESCE(campaign_id::TEXT, '0') ||
            CASE
                WHEN utm_campaign ~* 'tp[67]' THEN '|0'
                ELSE '|' || COALESCE(group_id::TEXT, '0')
            END || '|' ||
            COALESCE(
                CASE
                    WHEN utm_content LIKE '%dev:mobile%'   THEN 'mobile'
                    WHEN utm_content LIKE '%dev:desktop%'  THEN 'desktop'
                    WHEN utm_content LIKE '%dev:tablet%'   THEN 'tablet'
                    WHEN utm_content LIKE '%dev:smart_tv%' THEN 'smart_tv'
                    ELSE '0'
                END, '0'
            ) || '|' ||
            COALESCE(correction_id::TEXT, '0')
        ) AS key3,
        CASE WHEN arrival_date IS NOT NULL THEN
            LOWER(
                COALESCE(TO_CHAR(arrival_date, 'YYYY-MM-DD'), '') || '|' ||
                COALESCE(campaign_id::TEXT, '0') ||
                CASE
                    WHEN utm_campaign ~* 'tp[67]' THEN '|0'
                    ELSE '|' || COALESCE(group_id::TEXT, '0')
                END || '|' ||
                COALESCE(
                    CASE
                        WHEN utm_content LIKE '%dev:mobile%'   THEN 'mobile'
                        WHEN utm_content LIKE '%dev:desktop%'  THEN 'desktop'
                        WHEN utm_content LIKE '%dev:tablet%'   THEN 'tablet'
                        WHEN utm_content LIKE '%dev:smart_tv%' THEN 'smart_tv'
                        ELSE '0'
                    END, '0'
                ) || '|' ||
                COALESCE(correction_id::TEXT, '0')
            )
        ELSE NULL END AS key3_arrival_date
    FROM {T_LEADS_ALL_LOCAL} l
    LEFT JOIN local_domains d ON d.id = l.domain_id
    WHERE l.deal_type != 'Звонок'{_excl_sql}
"""

# ──────────────────────────────────────────────────────────────────────────────
# build_adformat_spend — _build_sql() из adformat_spend/build_adformat_spend.py
# Источник: yandex_direct_manager_reports (FDW-таблица)
# ──────────────────────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.expanduser('~/big_analytics_v5/adformat_spend'))
try:
    from build_adformat_spend import _build_sql as _adformat_build_sql
    _adformat_select = _adformat_build_sql()
    _adformat_ok = True
except Exception as e:
    _adformat_ok = False
    _adformat_err = str(e)

# ──────────────────────────────────────────────────────────────────────────────
# build_criterion_spend — _build_sql() из criterion_spend/build_criterion_spend.py
# Источник: yandex_direct_manager_reports (FDW), heavy regex (_CRIT_CLEAN)
# ──────────────────────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.expanduser('~/big_analytics_v5/criterion_spend'))
try:
    from build_criterion_spend import _build_sql as _criterion_spend_build_sql
    _criterion_spend_select = _criterion_spend_build_sql()
    _criterion_spend_ok = True
except Exception as e:
    _criterion_spend_ok = False
    _criterion_spend_err = str(e)

# ──────────────────────────────────────────────────────────────────────────────
# build_region_zayavki — _build_sql(agg_cases) из region_spend/build_region_zayavki.py
# Источник: local_leads_all (930К) — agg_cases строится READ-ONLY из local_crm_statuses
# ──────────────────────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.expanduser('~/big_analytics_v5/region_spend'))
try:
    from build_region_zayavki import _build_sql as _region_zayavki_build_sql
    _agg_cases_region = build_leads_agg_sql(conn)
    _region_zayavki_select = _region_zayavki_build_sql(_agg_cases_region)
    _region_zayavki_ok = True
except Exception as e:
    _region_zayavki_ok = False
    _region_zayavki_err = str(e)

# ──────────────────────────────────────────────────────────────────────────────
# build_criterion_zayavki — _build_sql(agg_cases)
# Источник: local_leads_all + fact_criterion_spend (должна существовать на Victory)
# ──────────────────────────────────────────────────────────────────────────────
try:
    from build_criterion_zayavki import _build_sql as _criterion_zayavki_build_sql
    # agg_cases уже получен выше (_agg_cases_region = те же 12 метрик)
    _criterion_zayavki_select = _criterion_zayavki_build_sql(_agg_cases_region)
    _criterion_zayavki_ok = True
except Exception as e:
    _criterion_zayavki_ok = False
    _criterion_zayavki_err = str(e)

# ──────────────────────────────────────────────────────────────────────────────
# build_unified — воспроизводим только SQL-сборку (без DDL).
# Читает information_schema + big_analytics_full + big_analytics_full_arrival.
# Логика из step13_arrival/build_unified.py::run() скопирована inline
# (без DROP/CREATE — только SELECT).
# ──────────────────────────────────────────────────────────────────────────────
try:
    _NULLIF_EMPTY_DIMS = {
        'марки авто', 'специалист', 'шаблон', 'account_login', 'domain',
        'салон', 'город', 'регион', 'cpc_cpa', 'site_quiz',
    }
    _TP_DIRECTION_LABELS = {
        'посевы': 'Посевы',
        'отзывы': 'отзывы',
        'SEO': 'SEO',
        'SEO Flow': 'SEO Flow',
        'пиксель': 'пиксель',
        'пиксель_атрибуц': 'пиксель_атрибуц',
    }
    _tp_label_when = '\n            '.join(
        f"WHEN \"направление\" = '{src}' THEN '{lbl}'"
        for src, lbl in _TP_DIRECTION_LABELS.items()
    )
    _TP_LABEL_EXPR = (
        'COALESCE(NULLIF("tp", \'\'), CASE\n            '
        f'{_tp_label_when}\n            '
        'ELSE NULL\n        END) AS "tp"'
    )

    def _col_expr(c: str) -> str:
        if c == 'tp':
            return _TP_LABEL_EXPR
        if c in _NULLIF_EMPTY_DIMS:
            return f'NULLIF("{c}", \'\') AS "{c}"'
        return f'"{c}"'

    with conn.cursor() as _cur:
        _cur.execute(
            """SELECT column_name FROM information_schema.columns
               WHERE table_schema='public' AND table_name='big_analytics_full'
               ORDER BY ordinal_position""")
        _baf_cols = [r[0] for r in _cur.fetchall()]
        _cur.execute(
            """SELECT column_name FROM information_schema.columns
               WHERE table_schema='public' AND table_name='big_analytics_full_arrival'
               ORDER BY ordinal_position""")
        _bfa_cols = [r[0] for r in _cur.fetchall()]

    if not _baf_cols:
        raise RuntimeError('big_analytics_full: 0 колонок — таблица пуста или не существует')
    if _baf_cols != _bfa_cols:
        raise RuntimeError(
            f'Схемы BAF ({len(_baf_cols)}) и BFA ({len(_bfa_cols)}) расходятся — '
            f'только в BAF: {[c for c in _baf_cols if c not in _bfa_cols]}; '
            f'только в BFA: {[c for c in _bfa_cols if c not in _baf_cols]}'
        )

    _col_list = ', '.join(_col_expr(c) for c in _baf_cols)
    _unified_select = f"""
        SELECT {_col_list}, 'По дате заявки'::text AS "атрибуция"
        FROM big_analytics_full
        UNION ALL
        SELECT {_col_list}, 'По дате визита'::text AS "атрибуция"
        FROM big_analytics_full_arrival
    """
    _unified_ok = True
except Exception as e:
    _unified_ok = False
    _unified_err = str(e)


# ──────────────────────────────────────────────────────────────────────────────
# ЗАПУСК ЗАМЕРОВ — последовательно
# ──────────────────────────────────────────────────────────────────────────────
print('\n' + '='*60, flush=True)
print('EXPLAIN ANALYZE ЗАМЕР ВСЕХ ШАГОВ', flush=True)
print(f'Дата: {time.strftime("%Y-%m-%d %H:%M:%S")}', flush=True)
print('='*60, flush=True)

# 1a. step1 raw_yandex
explain_step('step1_raw_yandex (local_yandex 10.78М + regex campaign_code)', _raw_yandex_select)

# 1b. step1 raw_leads
explain_step('step1_raw_leads (local_leads_all 930К + local_domains JOIN)', _raw_leads_select)

# 2. build_adformat_spend
if _adformat_ok:
    explain_step('build_adformat_spend (manager_reports FDW + gsheet_sites)', _adformat_select)
else:
    skip_step('build_adformat_spend', f'import ошибка: {_adformat_err}')

# 3. build_criterion_spend
if _criterion_spend_ok:
    explain_step('build_criterion_spend (manager_reports FDW + _CRIT_CLEAN regex)', _criterion_spend_select)
else:
    skip_step('build_criterion_spend', f'import ошибка: {_criterion_spend_err}')

# 4. build_region_zayavki
if _region_zayavki_ok:
    explain_step('build_region_zayavki (local_leads_all geoid + id_location dict + campaign_name)', _region_zayavki_select)
else:
    skip_step('build_region_zayavki', f'ошибка: {_region_zayavki_err}')

# 5. build_criterion_zayavki
if _criterion_zayavki_ok:
    explain_step('build_criterion_zayavki (local_leads_all utm_term + fact_criterion_spend JOIN)', _criterion_zayavki_select)
else:
    skip_step('build_criterion_zayavki', f'ошибка: {_criterion_zayavki_err}')

# 6. build_unified
if _unified_ok:
    explain_step(f'build_unified (big_analytics_full {len(_baf_cols)} кол. UNION ALL big_analytics_full_arrival)', _unified_select)
else:
    skip_step('build_unified', f'ошибка построения SQL: {_unified_err}')

# ──────────────────────────────────────────────────────────────────────────────
print('\n' + '='*60, flush=True)
print(f'ЗАМЕР ЗАВЕРШЁН: {time.strftime("%Y-%m-%d %H:%M:%S")}', flush=True)
print('='*60, flush=True)

conn.close()
