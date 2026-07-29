"""
step1_load_raw/step1.py — загрузка RAW UNLOGGED таблиц из локальных копий

UNLOGGED = без записи в WAL → INSERT в 2–3× быстрее.
Таблицы пересоздаются каждый запуск — всегда чистый срез данных.

raw_yandex  ← yandex_local       (без фильтров)
raw_leads   ← leads_local        (deal_type != 'Звонок' AND domain_id NOT IN excluded)
raw_calls   ← leads_local        (deal_type = 'Звонок' AND domain_id IS NOT NULL)
raw_domains ← domains_local      (без фильтров)

P3_FRESHNESS_SKIP_2026-06-18
Перед пересборкой raw_yandex проверяется «отпечаток свежести» FDW-источника
(COUNT + MAX("Date") с тем же фильтром что и основная загрузка).
Отпечаток хранится в public._step1_freshness (LOGGED, живёт между прогонами).
Skip активируется ТОЛЬКО если:
  1) raw_yandex существует И не пустая (guard против SPEND_PREFREE truncate)
  2) COUNT и MAX("Date") FDW совпали с сохранённым отпечатком
Иначе — полная пересборка + обновление отпечатка.
Три остальные таблицы (raw_leads/raw_calls/raw_domains) пересобираются всегда.

STEP1_PARALLEL_FDW_2026-06-18
Ветка «полная пересборка» (rebuild) выполняется параллельно по месячным партициям:
  1) DROP TABLE IF EXISTS raw_yandex
  2) CREATE UNLOGGED TABLE raw_yandex (пустая, схема из parsed_src WHERE FALSE)
  3) Генерация динамических партиций от '2026-01-01' до fdw_max_date (уже вычислен P3).
     Последняя партиция ОТКРЫТАЯ ("Date" >= 'lo' без верхней границы) — гарантия
     покрытия будущих месяцев без хардкода потолка.
  4) N потоков (ThreadPoolExecutor, N = число партиций): каждый берёт свой get_conn(),
     SET LOCAL work_mem='256MB', INSERT INTO raw_yandex WITH parsed_src AS (...) SELECT ...,
     commit, put_conn().
  5) COUNT-проверка: SUM вставленных строк должен равняться fdw_count из P3-отпечатка.
     Несовпадение → DROP raw_yandex + raise (чистое падение, step2+ не стартуют).
  6) Падение любого потока (future.result()) → DROP raw_yandex + raise до save_fingerprint.
Date-фильтр в WHERE строго ТЕКСТОВЫЙ ("Date" >= 'lo') — без ::DATE-каста (pushdown FDW).
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

from config.settings import (
    T_LEADS_ALL_LOCAL, T_YANDEX_LOCAL, T_DOMAINS_LOCAL,
    T_RAW_YANDEX, T_RAW_LEADS, T_RAW_CALLS, T_RAW_DOMAINS,
    T_LOCAL_PERFORM_LEADS, T_RAW_PERFORM_LEADS,  # PERFORM_LEADS_2026-07-01
    T_CRM_STATUSES,  # PERFORM_DEDUP_2026-07-10: sale-статусы для приоритета дедупа
    EXCLUDED_DOMAIN_IDS,
    SRC_YANDEX,
)
from config.db import get_conn, put_conn

logger = logging.getLogger('pipeline.step1')


# ── SQL создания RAW таблиц ───────────────────────────────────────────────────

def _excluded_domains_sql() -> str:
    """SQL-фрагмент для исключения domain_id."""
    if not EXCLUDED_DOMAIN_IDS:
        return ''
    ids = ', '.join(str(i) for i in EXCLUDED_DOMAIN_IDS)
    return f' AND (l.domain_id NOT IN ({ids}) OR l.domain_id IS NULL)'


# ── STEP1_PARALLEL_FDW_2026-06-18: параллельная загрузка raw_yandex ──────────

def _build_raw_yandex_create_empty_sql() -> str:
    """
    STEP1_PARALLEL_FDW_2026-06-18 — шаг 1 из 3 rebuild-цепочки.

    DROP + CREATE UNLOGGED TABLE raw_yandex (пустая структура).
    Схема берётся из той же parsed_src CTE через CTAS WHERE FALSE —
    PostgreSQL выводит типы колонок из SELECT-листа CTE без чтения строк FDW.

    VARA_DROP_LOCAL_YANDEX_FDW_2026-06-17 / T2_STEP1_REGEX_DEDUP_2026-06-18 /
    COST_FIX_2026-06-18 / D1_RAW_YANDEX_FILTER_FIX_2026-06-18 — маркеры сохранены.
    """
    return f"""
        DROP TABLE IF EXISTS {T_RAW_YANDEX};
        CREATE UNLOGGED TABLE {T_RAW_YANDEX} AS
        -- T2_STEP1_REGEX_DEDUP_2026-06-18: CTE вычисляет regex один раз на строку
        WITH parsed_src AS (
            SELECT
                id,
                "Date"::DATE                                         AS "Date",
                "CampaignId",
                "CampaignName",
                "AdGroupId",
                "AdGroupName",
                "AdNetworkType",
                "Device",
                "RlAdjustmentId",
                "Impressions",
                "Clicks",
                -- COST_FIX_2026-06-18: total_cost (итоговый расход с корректировками офлайн-конверсий)
                -- а НЕ "Cost" (базовый расход без корректировок).
                total_cost::NUMERIC                                  AS total_cost,
                account_login,
                manager_login,
                (REGEXP_MATCH(
                    SPLIT_PART("AdGroupName", ' — ', 1),
                    '(ct\\d+_(?:aoff|aon)_n\\d+_r\\d+_ct\\d+_ag\\d+_g\\d+)'
                ))[1]                                                AS adgroup_code,
                (REGEXP_MATCH(
                    REGEXP_REPLACE(REPLACE("CampaignName", chr(1089), 'c'), '__+', '_', 'g'),
                    '(?i)(tp\\d+_(?:cpc|cpa)_(?:site|kviz|quiz))'
                ))[1]                                                AS campaign_match,  -- DOUBLE_UNDERSCORE_FIX_2026-07-03
                (REGEXP_MATCH("CampaignName", '(?i)(tp[67]_)'))[1]  AS tp67_check,
                DATE_TRUNC('week', "Date"::DATE)::DATE               AS week_start
            FROM public.{SRC_YANDEX}
            WHERE "Date" >= '2026-01-01'
              AND "CampaignId" IS NOT NULL AND "CampaignId" != 0
        )
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
            adgroup_code,
            campaign_match                                           AS campaign_code,
            LOWER(SPLIT_PART(COALESCE(campaign_match, ''), '_', 1)) AS tp,
            LOWER(SPLIT_PART(COALESCE(campaign_match, ''), '_', 2)) AS cpc_cpa,
            REPLACE(REPLACE(
                LOWER(SPLIT_PART(COALESCE(campaign_match, ''), '_', 3)),
                'kviz', 'quiz'), 'kviz', 'quiz')                     AS site_quiz,
            week_start,
            LOWER(
                COALESCE(TO_CHAR("Date", 'YYYY-MM-DD'), '') || '|' ||
                COALESCE("CampaignId"::TEXT, '0') ||
                CASE
                    WHEN tp67_check IS NOT NULL THEN '|0'
                    ELSE '|' || COALESCE("AdGroupId"::TEXT, '0')
                END || '|' ||
                COALESCE(
                    CASE LOWER(COALESCE("Device", ''))
                        WHEN 'mobile'   THEN 'mobile'
                        WHEN 'desktop'  THEN 'desktop'
                        WHEN 'tablet'   THEN 'tablet'
                        WHEN 'smart_tv' THEN 'smart_tv'
                        ELSE '0'
                    END, '0'
                ) || '|' ||
                COALESCE("RlAdjustmentId"::TEXT, '0')
            )                                                        AS key3
        FROM parsed_src
        WHERE FALSE;
    """


def _build_partition_insert_sql(lo: str, hi: str | None) -> str:
    """
    STEP1_PARALLEL_FDW_2026-06-18 — шаг 2: INSERT одной месячной партиции.

    lo / hi — строки вида 'YYYY-MM-DD'.
    Последняя партиция: hi=None → открытый конец ("Date" >= 'lo' без верхней границы).

    ВАЖНО: Date-фильтр ТЕКСТОВЫЙ ("Date" >= 'lo') — БЕЗ ::DATE-каста.
    Причина: "Date" в FDW — text; textовый фильтр pushdown'ится на FDW-сервер.
    Добавление ::DATE вынуждает PG тянуть все строки и кастовать локально.

    T2_STEP1_REGEX_DEDUP_2026-06-18 / COST_FIX_2026-06-18 / VARA_DROP_LOCAL_YANDEX_FDW_2026-06-17
    — те же regex/поля что и в CTAS-схеме, гарантируется идентичность результата.
    """
    if hi is not None:
        date_filter = f'"Date" >= \'{lo}\' AND "Date" < \'{hi}\''
    else:
        # STEP1_PARALLEL_FDW_2026-06-18: открытый конец последней партиции —
        # новые месяцы (июль, август, ...) не теряются без хардкода потолка.
        date_filter = f'"Date" >= \'{lo}\''

    return f"""
        SET LOCAL work_mem = '256MB';
        INSERT INTO {T_RAW_YANDEX}
        -- T2_STEP1_REGEX_DEDUP_2026-06-18: CTE вычисляет regex один раз на строку
        WITH parsed_src AS (
            SELECT
                id,
                "Date"::DATE                                         AS "Date",
                "CampaignId",
                "CampaignName",
                "AdGroupId",
                "AdGroupName",
                "AdNetworkType",
                "Device",
                "RlAdjustmentId",
                "Impressions",
                "Clicks",
                -- COST_FIX_2026-06-18: total_cost — итоговый расход с офлайн-корр.
                total_cost::NUMERIC                                  AS total_cost,
                account_login,
                manager_login,
                (REGEXP_MATCH(
                    SPLIT_PART("AdGroupName", ' — ', 1),
                    '(ct\\d+_(?:aoff|aon)_n\\d+_r\\d+_ct\\d+_ag\\d+_g\\d+)'
                ))[1]                                                AS adgroup_code,
                (REGEXP_MATCH(
                    REGEXP_REPLACE(REPLACE("CampaignName", chr(1089), 'c'), '__+', '_', 'g'),
                    '(?i)(tp\\d+_(?:cpc|cpa)_(?:site|kviz|quiz))'
                ))[1]                                                AS campaign_match,  -- DOUBLE_UNDERSCORE_FIX_2026-07-03
                (REGEXP_MATCH("CampaignName", '(?i)(tp[67]_)'))[1]  AS tp67_check,
                DATE_TRUNC('week', "Date"::DATE)::DATE               AS week_start
            FROM public.{SRC_YANDEX}
            WHERE {date_filter}
              AND "CampaignId" IS NOT NULL AND "CampaignId" != 0
        )
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
            adgroup_code,
            campaign_match                                           AS campaign_code,
            LOWER(SPLIT_PART(COALESCE(campaign_match, ''), '_', 1)) AS tp,
            LOWER(SPLIT_PART(COALESCE(campaign_match, ''), '_', 2)) AS cpc_cpa,
            REPLACE(REPLACE(
                LOWER(SPLIT_PART(COALESCE(campaign_match, ''), '_', 3)),
                'kviz', 'quiz'), 'kviz', 'quiz')                     AS site_quiz,
            week_start,
            LOWER(
                COALESCE(TO_CHAR("Date", 'YYYY-MM-DD'), '') || '|' ||
                COALESCE("CampaignId"::TEXT, '0') ||
                CASE
                    WHEN tp67_check IS NOT NULL THEN '|0'
                    ELSE '|' || COALESCE("AdGroupId"::TEXT, '0')
                END || '|' ||
                COALESCE(
                    CASE LOWER(COALESCE("Device", ''))
                        WHEN 'mobile'   THEN 'mobile'
                        WHEN 'desktop'  THEN 'desktop'
                        WHEN 'tablet'   THEN 'tablet'
                        WHEN 'smart_tv' THEN 'smart_tv'
                        ELSE '0'
                    END, '0'
                ) || '|' ||
                COALESCE("RlAdjustmentId"::TEXT, '0')
            )                                                        AS key3
        FROM parsed_src;
    """


def _generate_partitions(fdw_max_date: date) -> list[tuple[str, str | None]]:
    """
    STEP1_PARALLEL_FDW_2026-06-18 — динамическая генерация месячных партиций.

    Возвращает список (lo, hi) где:
      - lo = первый день месяца (строка 'YYYY-MM-DD')
      - hi = первый день следующего месяца (строка 'YYYY-MM-DD') или None для последней

    Нижняя граница первой партиции = '2026-01-01' (DATE_FROM — инвариант CANON.md).
    Верхняя граница последней партиции = None (открытый конец) →
    гарантирует покрытие ВСЕХ будущих месяцев без хардкода потолка.

    Пример при fdw_max_date = 2026-06-17:
      ('2026-01-01', '2026-02-01')  ← январь
      ('2026-02-01', '2026-03-01')  ← февраль
      ('2026-03-01', '2026-04-01')  ← март
      ('2026-04-01', '2026-05-01')  ← апрель
      ('2026-05-01', '2026-06-01')  ← май
      ('2026-06-01', None)          ← июнь+ (открытый конец)
    """
    # Первая партиция начинается с 2026-01-01
    start = date(2026, 1, 1)
    # Последняя партиция = месяц fdw_max_date (он же текущий активный месяц)
    last_month_start = fdw_max_date.replace(day=1)

    partitions: list[tuple[str, str | None]] = []
    current = start
    while current <= last_month_start:
        lo_str = current.strftime('%Y-%m-%d')
        # Следующий месяц
        if current.month == 12:
            nxt = date(current.year + 1, 1, 1)
        else:
            nxt = date(current.year, current.month + 1, 1)

        if current < last_month_start:
            # Закрытая партиция [lo, hi)
            hi_str: str | None = nxt.strftime('%Y-%m-%d')
        else:
            # Последняя партиция — открытый конец (покрывает будущие данные текущего и следующих месяцев)
            hi_str = None

        partitions.append((lo_str, hi_str))
        current = nxt

    return partitions


def _insert_partition(lo: str, hi: str | None, partition_idx: int) -> int:
    """
    STEP1_PARALLEL_FDW_2026-06-18 — воркер одного потока.

    Берёт соединение из пула, выполняет INSERT одной партиции,
    возвращает rowcount, возвращает соединение в пул.
    SET LOCAL work_mem='256MB' — изолирован per-connection (не глобальный).
    """
    hi_label = hi if hi is not None else 'open'
    t0 = time.time()
    conn = get_conn()
    try:
        sql = _build_partition_insert_sql(lo, hi)
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.rowcount
        conn.commit()
        elapsed = time.time() - t0
        logger.info(
            'PARALLEL partition[%d] [%s, %s): %d строк за %.1f с',
            partition_idx, lo, hi_label, rows, elapsed,
        )
        return rows
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        put_conn(conn)


# ── P3: freshness-skip для raw_yandex ────────────────────────────────────────

# P3_FRESHNESS_SKIP_2026-06-18
# Имя служебной таблицы отпечатка. LOGGED — переживает любой TRUNCATE raw_yandex.
_FRESHNESS_TABLE = 'public._step1_freshness'

# FDW-запрос отпечатка — ТОТ ЖЕ фильтр что и в _build_raw_yandex_sql()
_FRESHNESS_FDW_SQL = """
    SELECT COUNT(*) AS fdw_count, MAX("Date"::DATE) AS fdw_max_date
    FROM public.{src}
    WHERE "Date"::DATE >= '2026-01-01'
      AND "CampaignId" IS NOT NULL AND "CampaignId" != 0
""".format(src=SRC_YANDEX)

_FRESHNESS_DDL = """
    CREATE TABLE IF NOT EXISTS {tbl} (
        id            SMALLINT PRIMARY KEY DEFAULT 1
                      CHECK (id = 1),          -- единственная строка
        fdw_count     BIGINT       NOT NULL,
        fdw_max_date  DATE         NOT NULL,
        updated_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
    )
""".format(tbl=_FRESHNESS_TABLE)


def _ensure_freshness_table(conn) -> None:
    """Создаёт _step1_freshness если не существует. Идемпотентно."""
    with conn.cursor() as cur:
        cur.execute(_FRESHNESS_DDL)
    conn.commit()


def _get_fdw_fingerprint(conn) -> tuple:
    """
    Возвращает (fdw_count: int, fdw_max_date: date) — отпечаток FDW-источника.
    Лёгкий агрегат: PostgreSQL pushdown'ит COUNT+MAX на FDW сервер.
    """
    with conn.cursor() as cur:
        cur.execute(_FRESHNESS_FDW_SQL)
        row = cur.fetchone()
    return (int(row[0]), row[1])   # (count, max_date)


def _get_saved_fingerprint(conn) -> tuple | None:
    """
    Читает сохранённый отпечаток из _step1_freshness.
    Возвращает (fdw_count, fdw_max_date) или None если таблица пустая (первый прогон).
    """
    with conn.cursor() as cur:
        cur.execute(f'SELECT fdw_count, fdw_max_date FROM {_FRESHNESS_TABLE} WHERE id = 1')
        row = cur.fetchone()
    if row is None:
        return None
    return (int(row[0]), row[1])


def _save_fingerprint(conn, fdw_count: int, fdw_max_date) -> None:
    """UPSERT отпечатка после полной пересборки raw_yandex."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {_FRESHNESS_TABLE} (id, fdw_count, fdw_max_date, updated_at)
            VALUES (1, %s, %s, now())
            ON CONFLICT (id) DO UPDATE
                SET fdw_count    = EXCLUDED.fdw_count,
                    fdw_max_date = EXCLUDED.fdw_max_date,
                    updated_at   = EXCLUDED.updated_at
            """,
            (fdw_count, fdw_max_date),
        )
    conn.commit()


def _raw_yandex_is_populated(conn) -> bool:
    """
    Guard: raw_yandex существует И содержит строки.
    Если таблица была TRUNCATE-нута (SPEND_PREFREE / EARLY_TRUNCATE_DIRECT_FAST),
    вернёт False → skip не сработает, будет полная пересборка.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT to_regclass(%s) IS NOT NULL",
            (T_RAW_YANDEX,),
        )
        exists = cur.fetchone()[0]
        if not exists:
            return False
        cur.execute(f'SELECT EXISTS(SELECT 1 FROM {T_RAW_YANDEX} LIMIT 1)')
        return cur.fetchone()[0]


def _build_raw_leads_sql() -> str:
    """
    raw_leads содержит все поля leads_local (не-звонки, без excluded domains) плюс:
      - key3            — Date|campaign_id|group_id|device|correction_id
      - key3_arrival_date — аналогично но по arrival_date
      - fid             — ID с фида из utm_content
    """
    excl = _excluded_domains_sql()
    return f"""
        DROP TABLE IF EXISTS {T_RAW_LEADS};
        CREATE UNLOGGED TABLE {T_RAW_LEADS} AS
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
            -- fid из utm_content (формат: ...fid:12345|...)
            CASE
                WHEN utm_content LIKE '%fid:%'
                THEN TRIM(REGEXP_REPLACE(
                    SPLIT_PART(utm_content, 'fid:', 2), '\\|.*', ''))
                ELSE NULL
            END AS fid,
            -- key3 по created_date
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
            -- key3_arrival_date по дате приезда
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
        LEFT JOIN {T_DOMAINS_LOCAL} d ON d.id = l.domain_id
        WHERE l.deal_type != 'Звонок'{excl};
    """


def _build_raw_perform_leads_sql() -> str:
    """
    PERFORM_LEADS_2026-07-01
    raw_perform_leads содержит заявки perform-аккаунтов из local_perform_leads.
    Структура и логика идентичны raw_leads (те же вычисляемые поля key3/key3_arrival_date/fid).
    Источник: local_perform_leads (не local_leads_all).

    PERFORM_FUNNEL_2026-07-07-v4-step1: status читается напрямую из local_perform_leads.status.
    step0._apply_perform_statuses уже обновил matched-строки (v3-матчинг, 3 ветки source_names).
    При совпадении:   status = сырой CRM-статус из leads_all (напр. 'Купил', 'В работе').
    При несовпадении: local_perform_leads.status = '' (из FDW) → NULLIF → 'без статуса'.
      → kol_vo_zayavok = 1 (обращение реально было, статус неизвестен пока).
      → korr/kval/priezd/prodazhi/nekorr = 0 (не классифицировано — НЕ nekorr!).
    """
    excl = _excluded_domains_sql()

    # PERFORM_DEDUP_2026-07-10: набор sale-статусов читаем из local_crm_statuses
    # (drift-proof — не хардкодим список). Используется как приоритет дедупа (Правка №1)
    # и как признак «спорной продажи» ветки (b) (Правка №2).
    sale_subq = (
        f"SELECT crm_status FROM {T_CRM_STATUSES} "
        f"WHERE lead_status = 'sale' AND COALESCE(kind, 'status') = 'status'"
    )

    # Perform-когорта ветки (b): те же source_names, что и в основном фильтре.
    # Вынесено в хелпер, чтобы переиспользовать в анти-JOIN спорного телефона (Правка №2).
    _crmf_names = (
        "'LeadV Perform Автопарк Южный', 'LeadV Perform Южный Обход', "
        "'LeadV Perform Нижний Центр Авто', 'LeadV Perform АвтоМаркет', "
        "'LeadV Perform Нави Кар', 'LeadV Perform Кубань Драйв'"
    )
    _mauto_names = "'LeadV Перформ Лидер', 'LeadV Перформ КТ Лидер'"

    def _cohort(al: str) -> str:
        return (
            f"(({al}.source_type = 'crmf_excel' AND {al}.source_name IN ({_crmf_names})) "
            f"OR ({al}.source_type = 'mauto_excel' AND {al}.source_name IN ({_mauto_names})))"
        )

    return f"""
        DROP TABLE IF EXISTS {T_RAW_PERFORM_LEADS};
        CREATE UNLOGGED TABLE {T_RAW_PERFORM_LEADS} AS
        -- ═══════════════════════════════════════════════════════════════════════
        -- Ветка (a): perform_leads + ДЕДУП кросс-доменной ПРОДАЖИ-задвоения.
        -- PERFORM_DEDUP_2026-07-10 (Правка №1): один и тот же телефон, записанный
        -- на ДВУХ Перформ-доменах (разные domain_id, обе is_copy_for_removal=False),
        -- давал 2 строки факта → 2 продажи (напр. +79197450401 на cars-rus.ru и
        -- automobili-rus.ru, обе 'Купил'). Между Перформ-доменами дедупа по норм.
        -- телефону не было. Схлопываем в ОДНУ строку — НО ТОЛЬКО для групп, где
        -- телефон встречается на >1 домене И среди них есть ПРОДАЖА (иначе не трогаем:
        -- сотни не-продажных кросс-доменных повторов остаются как есть — вне scope).
        -- Приоритет keeper: продажа > визит > детерминированный тай-брейк.
        -- ═══════════════════════════════════════════════════════════════════════
        SELECT
            id, created_date, arrival_date, domain_id, domain, deal_type, status,
            source_type, campaign_id, group_id, correction_id, utm_source, utm_medium,
            utm_campaign, utm_content, utm_term, phone, yclid, is_copy_for_removal,
            reason, salon, fid, key3, key3_arrival_date
        FROM (
            SELECT a.*,
                ROW_NUMBER() OVER (
                    PARTITION BY a._phone_norm
                    ORDER BY
                        (1 - a._is_sale),                                       -- продажа выше
                        CASE WHEN a.arrival_date IS NOT NULL THEN 0 ELSE 1 END,  -- визит выше
                        a.created_date, a.domain_id, a.id                        -- детерминизм
                ) AS _rn,
                -- кросс-доменность: min(domain)<>max(domain) ⇔ ≥2 разных domain_id
                -- (COUNT(DISTINCT ...) OVER не поддерживается PostgreSQL).
                MIN(a.domain_id) OVER (PARTITION BY a._phone_norm) AS _dmin,
                MAX(a.domain_id) OVER (PARTITION BY a._phone_norm) AS _dmax,
                MAX(a._is_sale)  OVER (PARTITION BY a._phone_norm) AS _hassale
            FROM (
                SELECT
                    l.id,
                    l.created_date,
                    l.arrival_date,
                    l.domain_id,
                    d.name AS domain,
                    l.deal_type,
                    -- PERFORM_FUNNEL_2026-07-07-v4-step1: статус из local_perform_leads.status.
                    -- step0._apply_perform_statuses обновил matched строки; unmatched = '' от FDW.
                    -- NULLIF('','')→NULL → COALESCE→'без статуса' (kol_vo_zayavok=1, voronka=0, не nekorr).
                    COALESCE(NULLIF(TRIM(l.status), ''), 'без статуса') AS status,
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
                    -- fid из utm_content (формат: ...fid:12345|...)
                    CASE
                        WHEN utm_content LIKE '%fid:%'
                        THEN TRIM(REGEXP_REPLACE(
                            SPLIT_PART(utm_content, 'fid:', 2), '\\|.*', ''))
                        ELSE NULL
                    END AS fid,
                    -- key3 по created_date
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
                    -- key3_arrival_date по дате приезда
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
                    ELSE NULL END AS key3_arrival_date,
                    -- норм. телефон (последние 10 цифр) для дедупа между доменами
                    RIGHT(REGEXP_REPLACE(COALESCE(l.phone, ''), '[^0-9]', '', 'g'), 10) AS _phone_norm,
                    -- признак продажи (по общему набору sale-статусов из local_crm_statuses)
                    CASE WHEN COALESCE(NULLIF(TRIM(l.status), ''), 'без статуса') IN ({sale_subq})
                         THEN 1 ELSE 0 END AS _is_sale
                FROM {T_LOCAL_PERFORM_LEADS} l
                LEFT JOIN {T_DOMAINS_LOCAL} d ON d.id = l.domain_id
                -- perform_leads: deal_type IS NULL (не разделяются на звонки/заявки — всегда заявки)
                -- PERFORM_LEADS_2026-07-01: используем IS NULL OR != чтобы не терять NULL-строки
                WHERE (l.deal_type IS NULL OR l.deal_type != 'Звонок'){excl}
            ) a
        ) dd
        -- Убираем ТОЛЬКО излишние строки кросс-доменной группы, содержащей продажу.
        -- Всё остальное (пустой телефон, одиночный домен, не-продажные повторы) — сохраняем.
        WHERE NOT (_phone_norm <> '' AND _dmin <> _dmax AND _hassale = 1 AND _rn > 1)
        -- PERFORM_FUNNEL_2026-07-06-v2: ветка (b) — CRM-контакты с явными Perform source_names,
        -- телефон которых НЕ найден в perform_leads (пришли через LeadV, не отследила Метрика).
        -- Domain='cars-rus.ru' (avto_0415) → step3 даёт salon='Перформ РФ' → direction='Перформ'.
        -- cost=0 (key3=NULL → нет матча с raw_yandex).
        -- Только LeadV (не LeadVDL): LeadVDL-контакты на ~99% уже в perform_leads.
        -- Дедупликация: NOT EXISTS по phone_norm в local_perform_leads.
        UNION ALL
        SELECT
            l.id,
            l.created_date,
            NULL::date   AS arrival_date,
            d_perf.id    AS domain_id,
            d_perf.name  AS domain,
            NULL::text   AS deal_type,
            l.status,
            'perform_api'::text AS source_type,
            NULL::bigint AS campaign_id,
            NULL::bigint AS group_id,
            NULL::bigint AS correction_id,
            NULL::text   AS utm_source,
            NULL::text   AS utm_medium,
            NULL::text   AS utm_campaign,
            NULL::text   AS utm_content,
            NULL::text   AS utm_term,
            l.phone,
            NULL::text   AS yclid,
            l.is_copy_for_removal,
            l.reason,
            l.salon,
            NULL::text   AS fid,
            NULL::text   AS key3,
            NULL::text   AS key3_arrival_date
        FROM {T_LEADS_ALL_LOCAL} l
        CROSS JOIN (
            SELECT id, name FROM {T_DOMAINS_LOCAL} WHERE name = 'cars-rus.ru' LIMIT 1
        ) d_perf
        WHERE (l.deal_type IS NULL OR l.deal_type != 'Звонок')
          AND l.phone IS NOT NULL AND l.phone != ''
          AND {_cohort('l')}
          AND NOT EXISTS (
            SELECT 1 FROM {T_LOCAL_PERFORM_LEADS} pl
            WHERE RIGHT(REGEXP_REPLACE(pl.phone, '[^0-9]', '', 'g'), 10)
                = RIGHT(REGEXP_REPLACE(l.phone,   '[^0-9]', '', 'g'), 10)
          )
          -- PERFORM_DEDUP_2026-07-10 (Правка №2): не засчитывать СПОРНУЮ ПРОДАЖУ ветки (b) —
          -- строку-продажу, чей норм. телефон заявлен ДРУГИМ Perform-дилером (др. source_name)
          -- с НЕ-продажным статусом (напр. 79054975136: 'Купил'@Южный Обход vs 'Фильтр'@Автопарк
          -- Южный). Условие срабатывает ТОЛЬКО для продажных строк с конфликтующим не-продажным
          -- двойником → не режет валидные ветки-b лиды (не-продажные спорные повторы остаются).
          AND NOT (
            l.status IN ({sale_subq})
            AND EXISTS (
              SELECT 1 FROM {T_LEADS_ALL_LOCAL} l2
              WHERE {_cohort('l2')}
                AND l2.id <> l.id
                AND l2.source_name <> l.source_name
                AND l2.phone IS NOT NULL AND l2.phone != ''
                AND RIGHT(REGEXP_REPLACE(l2.phone, '[^0-9]', '', 'g'), 10)
                  = RIGHT(REGEXP_REPLACE(l.phone,  '[^0-9]', '', 'g'), 10)
                AND l2.status NOT IN ({sale_subq})
            )
          );
    """


def _build_raw_calls_sql() -> str:
    return f"""
        DROP TABLE IF EXISTS {T_RAW_CALLS};
        CREATE UNLOGGED TABLE {T_RAW_CALLS} AS
        SELECT
            l.id,
            l.created_date,
            l.domain_id,
            d.name AS domain,
            l.deal_type,
            l.status,
            l.reason,
            l.source_type,
            l.phone,
            l.utm_source,
            l.utm_medium,
            l.utm_campaign
        FROM {T_LEADS_ALL_LOCAL} l
        LEFT JOIN {T_DOMAINS_LOCAL} d ON d.id = l.domain_id
        WHERE l.deal_type = 'Звонок'
          AND l.domain_id IS NOT NULL;
    """


def _build_raw_domains_sql() -> str:
    return f"""
        DROP TABLE IF EXISTS {T_RAW_DOMAINS};
        CREATE UNLOGGED TABLE {T_RAW_DOMAINS} AS
        SELECT * FROM {T_DOMAINS_LOCAL};
    """


# ── Подсчёт строк после создания ─────────────────────────────────────────────

def _count(conn, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f'SELECT COUNT(*) FROM {table}')
        return cur.fetchone()[0]


# ── Точка входа шага ─────────────────────────────────────────────────────────

def run(conn, run_id: str) -> dict:
    logger.info('Шаг 1: создание RAW UNLOGGED таблиц')

    total = 0
    details_parts = []

    # ── P3_FRESHNESS_SKIP_2026-06-18: raw_yandex (тяжёлая, ~22 мин) ──────────
    # Остальные три таблицы (raw_leads/raw_calls/raw_domains) пересобираются всегда —
    # они быстрые (из локальных таблиц, не FDW) и не имеют смысла кэшировать.
    _ensure_freshness_table(conn)

    skip_raw_yandex = False
    fdw_count, fdw_max_date = _get_fdw_fingerprint(conn)
    saved = _get_saved_fingerprint(conn)

    if saved is not None:
        saved_count, saved_max_date = saved
        fingerprint_match = (fdw_count == saved_count and fdw_max_date == saved_max_date)
        populated = _raw_yandex_is_populated(conn)
        if fingerprint_match and populated:
            skip_raw_yandex = True

    if skip_raw_yandex:
        rows_yandex = _count(conn, T_RAW_YANDEX)
        logger.info(
            'P3 freshness-skip: raw_yandex актуальна '
            '(fdw_count=%d, fdw_max_date=%s) — пропуск rebuild',
            fdw_count, fdw_max_date,
        )
        # STEP1_SKIP_FRESHNESS_BUMP_2026-07-16: skip означает «FDW не изменился,
        # raw_yandex актуальна» — это НЕ "stale", а верифицированная свежесть.
        # Без этого BUILD_UNIFIED_GATE (pipeline_powerbi.py) видел старый updated_at
        # ← был баг: step1 skip не трогал _step1_freshness → gate false-fire → PBI blocked.
        # Теперь gate проверяет data_quality_log.build_unified (не step1_freshness),
        # но bump всё равно полезен для аудита / STEP1_FRESHNESS_LOG в build_star.
        _save_fingerprint(conn, fdw_count, fdw_max_date)
        logger.info(
            'P3 freshness-skip: updated_at обновлён в %s (count=%d)',
            _FRESHNESS_TABLE, fdw_count,
        )
    else:
        reason = (
            'первый прогон' if saved is None
            else 'raw_yandex пустая/отсутствует' if not _raw_yandex_is_populated(conn)
            else f'FDW изменился: count {saved[0]}→{fdw_count}, max_date {saved[1]}→{fdw_max_date}'
        )
        logger.info('P3 freshness: полная пересборка raw_yandex (%s)', reason)

        # ── STEP1_PARALLEL_FDW_2026-06-18: параллельная пересборка ───────────────
        # Шаг 1: создаём пустую UNLOGGED таблицу (схема из CTAS WHERE FALSE).
        with conn.cursor() as cur:
            cur.execute(_build_raw_yandex_create_empty_sql())
        conn.commit()
        logger.info('PARALLEL: raw_yandex создана (пустая), начинаем параллельную загрузку')

        # Шаг 2: генерируем динамические месячные партиции.
        # fdw_max_date уже вычислен _get_fdw_fingerprint() выше — повторного запроса нет.
        partitions = _generate_partitions(fdw_max_date)
        n = len(partitions)
        logger.info(
            'PARALLEL: %d партиций (2026-01-01 → %s, последняя открытая)',
            n, fdw_max_date,
        )
        for i, (lo, hi) in enumerate(partitions):
            logger.info('  partition[%d]: [%s, %s)', i, lo, hi if hi else 'open')

        # Шаг 3: N параллельных потоков — каждый INSERT одной партиции.
        # При падении любого потока: DROP raw_yandex + raise (partial-загрузка недопустима).
        inserted_rows: list[int] = [0] * n
        parallel_ok = False
        try:
            with ThreadPoolExecutor(max_workers=n) as executor:
                futures = {
                    executor.submit(_insert_partition, lo, hi, i): i
                    for i, (lo, hi) in enumerate(partitions)
                }
                for future in as_completed(futures):
                    idx = futures[future]
                    inserted_rows[idx] = future.result()  # кидает если поток упал
            parallel_ok = True
        finally:
            if not parallel_ok:
                # Падение потока → чистим частично заполненную таблицу
                logger.error(
                    'PARALLEL: один из потоков упал, DROP raw_yandex (partial-загрузка)'
                )
                try:
                    with conn.cursor() as cur:
                        cur.execute(f'DROP TABLE IF EXISTS {T_RAW_YANDEX}')
                    conn.commit()
                except Exception as drop_err:
                    logger.error('PARALLEL: не удалось DROP raw_yandex: %s', drop_err)
                # raise пробрасывается из finally автоматически

        total_inserted = sum(inserted_rows)
        logger.info(
            'PARALLEL: все потоки завершены, total_inserted=%d, fdw_count=%d',
            total_inserted, fdw_count,
        )

        # Шаг 4: COUNT-проверка — сумма вставленных = fdw_count из отпечатка.
        # Несовпадение → DROP + raise (чистое падение, step2+ не стартуют).
        if total_inserted != fdw_count:
            logger.error(
                'PARALLEL COUNT MISMATCH: inserted=%d != fdw_count=%d — DROP raw_yandex',
                total_inserted, fdw_count,
            )
            with conn.cursor() as cur:
                cur.execute(f'DROP TABLE IF EXISTS {T_RAW_YANDEX}')
            conn.commit()
            raise RuntimeError(
                f'STEP1_PARALLEL_FDW: COUNT mismatch: '
                f'inserted {total_inserted} != fdw_count {fdw_count}'
            )

        rows_yandex = total_inserted
        # Шаг 5: отпечаток сохраняется ТОЛЬКО после успешной COUNT-проверки.
        _save_fingerprint(conn, fdw_count, fdw_max_date)
        logger.info(
            'P3 freshness: отпечаток обновлён (count=%d, max_date=%s)',
            fdw_count, fdw_max_date,
        )
        # ── конец STEP1_PARALLEL_FDW_2026-06-18 ──────────────────────────────────

    total += rows_yandex
    details_parts.append(f'raw_yandex={rows_yandex:,}')
    logger.info('  raw_yandex: %d строк', rows_yandex)

    # ── RAW_YANDEX_COST_GUARD_2026-06-22: предохранитель нулевого расхода ────
    # После загрузки raw_yandex проверяем SUM(total_cost).
    # Если 0 или NULL — FDW вернул нулевой расход (транзиент загрузки источника).
    # Останавливаем прогон немедленно, не собираем молча нулевую витрину.
    with conn.cursor() as cur:
        cur.execute(f'SELECT SUM(total_cost) FROM {T_RAW_YANDEX}')
        _cost_sum = cur.fetchone()[0]
    if not _cost_sum:
        raise RuntimeError(
            f'RAW_YANDEX_COST_GUARD_2026-06-22: raw_yandex.total_cost=0 после загрузки '
            f'(SUM={_cost_sum}) — FDW вернул нулевой расход, прогон остановлен. '
            f'Проверьте yandex_direct_manager_reports (FDW источник). '
            f'raw_yandex содержит {rows_yandex:,} строк.'
        )
    logger.info(
        'RAW_YANDEX_COST_GUARD_2026-06-22: SUM(total_cost)=%.2f — расход ненулевой, OK',
        float(_cost_sum),
    )
    # ── конец RAW_YANDEX_COST_GUARD_2026-06-22 ────────────────────────────────

    # ── конец P3_FRESHNESS_SKIP_2026-06-18 ────────────────────────────────────

    # raw_leads / raw_calls / raw_domains / raw_perform_leads — всегда пересобираются (быстрые, локальные)
    for name, sql, table_const in [
        ('raw_leads',         _build_raw_leads_sql(),         T_RAW_LEADS),
        ('raw_calls',         _build_raw_calls_sql(),         T_RAW_CALLS),
        ('raw_domains',       _build_raw_domains_sql(),       T_RAW_DOMAINS),
        ('raw_perform_leads', _build_raw_perform_leads_sql(), T_RAW_PERFORM_LEADS),  # PERFORM_LEADS_2026-07-01
    ]:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
        rows = _count(conn, table_const)
        total += rows
        details_parts.append(f'{name}={rows:,}')
        logger.info('  %s: %d строк', name, rows)

    details = ', '.join(details_parts)
    logger.info('Шаг 1 завершён: %s', details)
    return {'rows': total, 'details': details}


# ── EXPLAIN_BASELINE_2026-06-17: SELECT-эквивалент для EXPLAIN ANALYZE ──────
# Используется explain_capture при EXPLAIN_CAPTURE=1 (post-run, данные уже записаны).
# Показывает план FDW-чтения local_yandex → raw_yandex (самая тяжёлая из 4 таблиц).
# Data path не изменяется — это SELECT-only читалка уже готовой raw_yandex.

def get_explain_sql(conn) -> str:  # noqa: ARG001
    """SELECT-эквивалент для EXPLAIN ANALYZE по raw_yandex (самая тяжёлая таблица step1)."""
    return f'SELECT COUNT(*) FROM {T_RAW_YANDEX}'
