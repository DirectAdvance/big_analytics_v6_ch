"""
region_spend/build_region_spend.py — датамарт «расход по регионам показа».

D2_SPEND_FIX_2026-06-18

ЦЕЛЬ: понимать, на какие РЕГИОНЫ ПОКАЗА (LocationOfPresenceId) кто рекламируется
в разрезе кампаний, с distance_km / distance_km_agreg / location.

ОТЛИЧИЕ ОТ analytics_report_placement (тот же класс датамарта, но):
  • разрез по ЛОКАЦИИ показа вместо placement;
  • метрики берутся ПРЯМО из yandex_direct_manager_reports (total_cost/Clicks/Impressions
    + all_forms/crm_order_* — они ДОСТУПНЫ по локации), БЕЗ Direct API и БЕЗ raw_leads;
  • пиксель-воронка (kol_vo_zayavok..prodazhi) НЕ переносится — её по локации нет
    (она живёт по key3 в основном факте, где golden Кудерко).

ПОЧЕМУ ОТДЕЛЬНАЯ ТАБЛИЦА, А НЕ key3-факт:
  LocationOfPresenceId отсекается на step0 (step0.py не SELECT-ит его) и теряется в
  key3-агрегации step3 (key3 = Date|CampaignId|AdGroupId|Device|RlAdjustmentId — локации
  в нём нет). Поэтому датамарт строится ОТДЕЛЬНО, читая yandex_direct_manager_reports
  НАПРЯМУЮ. В key3 локацию НЕ добавляем (×26 строк → поедет атрибуция + сломается join лидов).

ИНВАРИАНТЫ (проверены в БД 2026-06-11):
  • двойного счёта НЕТ: keys_with_both = 0 (для полного детального ключа строки с
    LocationOfPresenceId и без — взаимоисключающие); Σ total_cost не дублируется;
  • golden Кудерко (public.fact_big_analytics 25 422 774.00 / 47) НЕ затрагивается —
    это другая таблица, мы её не читаем и не пишем;
  • локация есть только у ~43% расхода (278.7М из 654.5М ₽); 57% — NULL (Яндекс не
    отдаёт гео-присутствие) → в PBI это «регион не определён» (id_location IS NULL);
  • незаматчено со справочником ~0.5% (633 id) → location/Область будут NULL, но строка
    остаётся (LEFT JOIN), id_location сохраняется как есть.

ГРАНЬ GROUP BY: date × campaign_id × ad_group_id × ad_network_type × id_location.
row_hash = md5 этой грани (PK).

ИДЕМПОТЕНТНОСТЬ: полный пересбор DROP+CTAS (а не инкремент как ARP). Источник
manager_reports уже агрегирован Яндексом по дням; полный пересбор 9.35М строк →
~datamart-таблица (грубо ≤1М строк после GROUP BY) дёшев и не накапливает дрейф.

D2_SPEND_FIX_2026-06-18: откат фикса E (FDW_SINGLE_PASS_STAGING). Причины:
  1. Фикс E строил staging 9.3GB/12M строк ДО GROUP BY — enrichment на детальной грани.
  2. DDL staging → три билдера делали DROP+DDL+INSERT вместо DROP+CTAS → PK задавался
     дважды (PRIMARY KEY в DDL + ALTER TABLE ADD CONSTRAINT) → "multiple primary keys".
  3. drop_staging вызывался только в criterion_spend и только в нормальном пути (не в
     finally) → при любом падении staging 9.3GB висел до ручной чистки.
  4. SPEND_PREFREE (fast_pipeline L830) теперь освобождает ~13 GB перед spend-фазой
     → диска достаточно для 3 независимых CTAS без staging.
Каждый из 3 билдеров снова читает FDW самостоятельно, но по своей оси (GROUP BY уже,
меньше temp-spill). Фиксы C (work_mem) и F (SAVEPOINT temp_file_limit) сохранены.

ЗАПУСК (вызывается из дневного пайплайна; ручной — отдельно):
  cd ~/big_analytics_v5 && ~/venv/bin/python3 region_spend/build_region_spend.py
"""

import logging
import os
import sys
import time
from datetime import datetime

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import DATE_FROM, DB_DST  # noqa: E402

logger = logging.getLogger('pipeline.build_region_spend')

TARGET_TABLE = 'fact_region_spend'
SRC_MANAGER  = 'yandex_direct_manager_reports'
DICT_LOCATION = 'local_gsheet_yandex_direct_id_location'
GSHEET_SITES = 'local_gsheet_sites'

# ── DDL ───────────────────────────────────────────────────────────────────────
DDL = f"""
CREATE TABLE IF NOT EXISTS public.{TARGET_TABLE} (
    row_hash            TEXT PRIMARY KEY,
    date                DATE,
    campaign_id         BIGINT,
    campaign_name       TEXT,
    ad_group_id         BIGINT,
    ad_network_type     TEXT,
    -- локация показа (LEFT JOIN справочника; NULL = «регион не определён»)
    id_location         BIGINT,
    location            TEXT,
    "Область"           TEXT,
    "GeoRegionType"     TEXT,
    distance_km         INTEGER,
    distance_km_agreg   INTEGER,
    -- метрики из manager_reports (доступны по локации)
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
    ad_network_type,id_location). MAX(CampaignName) — кампания одна на campaign_id.
    Затем LEFT JOIN справочник локаций + LEFT JOIN gsheet_sites по account_login.

    account_login берём MAX по грани (он один на кампанию/аккаунт) — для join к gsheet.
    """
    return f"""
    WITH agg AS (
        SELECT
            m."Date"::date                              AS date,
            m."CampaignId"                              AS campaign_id,
            MAX(m."CampaignName")                       AS campaign_name,
            m."AdGroupId"                               AS ad_group_id,
            m."AdNetworkType"                           AS ad_network_type,
            m."LocationOfPresenceId"                    AS id_location,
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
            m."AdNetworkType", m."LocationOfPresenceId"
    )
    SELECT
        md5(
            COALESCE(a.date::text,'')           || '|' ||
            COALESCE(a.campaign_id::text,'')    || '|' ||
            COALESCE(a.ad_group_id::text,'')    || '|' ||
            COALESCE(a.ad_network_type,'')      || '|' ||
            COALESCE(a.id_location::text,'NULL')
        )                                                   AS row_hash,
        a.date,
        a.campaign_id,
        a.campaign_name,
        a.ad_group_id,
        a.ad_network_type,
        a.id_location,
        d.location,
        d."Область",
        d."GeoRegionType",
        d.distance_km,
        d.distance_km_agreg,
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
    -- id_location в справочнике уникален (16547/16547, 0 дублей), DISTINCT ON — страховка
    -- от будущих дублей (детерминированный tie-break по location).
    LEFT JOIN (
        SELECT DISTINCT ON (id_location)
            id_location, location, "Область", "GeoRegionType", distance_km, distance_km_agreg
        FROM public.{DICT_LOCATION}
        ORDER BY id_location, location
    ) d ON a.id_location = d.id_location
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
            s.id_location,
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
            MAX(s.client_id)           AS client_id,
            MAX(s.location)            AS location,
            MAX(s.oblast)              AS oblast,
            MAX(s.geo_region_type)     AS geo_region_type,
            MAX(s.distance_km)         AS distance_km,
            MAX(s.distance_km_agreg)   AS distance_km_agreg
        FROM public.{STAGING_TABLE} s
        GROUP BY
            s.date, s.campaign_id, s.ad_group_id,
            s.ad_network_type, s.id_location
    )
    SELECT
        md5(
            COALESCE(a.date::text,'')           || '|' ||
            COALESCE(a.campaign_id::text,'')    || '|' ||
            COALESCE(a.ad_group_id::text,'')    || '|' ||
            COALESCE(a.ad_network_type,'')      || '|' ||
            COALESCE(a.id_location::text,'NULL')
        )                                                   AS row_hash,
        a.date,
        a.campaign_id,
        a.campaign_name,
        a.ad_group_id,
        a.ad_network_type,
        a.id_location,
        a.location,
        a.oblast                                            AS "Область",
        a.geo_region_type                                   AS "GeoRegionType",
        a.distance_km,
        a.distance_km_agreg,
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
        NOW()                                               AS updated_at
    FROM agg a
    """


_INDEXES = [
    # INDEX_AUDIT_2026-06-27: idx_frs_date btree→BRIN (24 scans, 10.5M строк, монотонная дата).
    # Удалены мёртвые (idx_scan=0): idx_frs_campaign, idx_frs_salon.
    # Сохранён idx_frs_location (16 scans).
    f'CREATE INDEX IF NOT EXISTS idx_frs_date_brin ON public.{TARGET_TABLE} USING BRIN (date)',
    f'CREATE INDEX IF NOT EXISTS idx_frs_location  ON public.{TARGET_TABLE} (id_location)',
]


def _drop_old_table(conn) -> None:
    """
    DISKFREE_DROP_FIRST_2026-06-22: DROP старой таблицы в отдельной autocommit-транзакции
    ДО начала CTAS.

    Механика: PostgreSQL освобождает heap-файл таблицы только при COMMIT транзакции,
    в которой выполнен DROP. Если DROP и CTAS находятся в одной транзакции — на пике
    одновременно существуют:
      • старая таблица (ещё не удалена, COMMIT не было)        ~7.3 GB
      • новая таблица (строится CTAS)                           ~7.3 GB
      • temp-spill HashAggregate 19M строк FDW                  ~7 GB
    Итого: ~22 GB пикового расхода при 12 GB свободного → disk-full (инцидент 2026-06-22).

    Фикс: отдельный autocommit-коннект для DROP → OS сразу получает 7.3 GB обратно →
    на пике при CTAS: только новая таблица (~7.3 GB) + temp-spill (~7 GB) = ~14.3 GB.
    Экономия на пике: ~7.3 GB (размер старой fact_region_spend).
    """
    import psycopg2 as _psycopg2  # локальный import — не засоряем верхний namespace
    drop_conn = _psycopg2.connect(**DB_DST)
    drop_conn.autocommit = True
    try:
        with drop_conn.cursor() as _dc:
            _dc.execute(f'DROP TABLE IF EXISTS public.{TARGET_TABLE}')
        logger.info(
            'DISKFREE_DROP_FIRST_2026-06-22: DROP public.%s выполнен '
            '(autocommit → OS вернул место ДО CTAS)', TARGET_TABLE
        )
    finally:
        drop_conn.close()


# Список TEXT-колонок fact_region_spend для SET COMPRESSION lz4.
# DISKFREE_LZ4_2026-06-22: без lz4 таблица 7.3 GB (655 байт/строку). lz4 на TEXT
# снижает физический размер на 15-30% на строках с повторяющимися domain/салон/регион.
# Применяется через ALTER COLUMN после CTAS — не нарушает логику, только меняет хранение.
_TEXT_COLS_FOR_LZ4 = [
    'row_hash', 'campaign_name', 'ad_network_type', 'location',
    'Область', 'GeoRegionType', 'domain', 'логин', 'директолог',
    'город', 'регион', 'салон', 'шаблон', 'тип_сайта', 'статус',
    'направление', 'project_manager', 'client_id',
    'campaign_code', 'tp', 'cpc_cpa', 'site_quiz',
]

# Минимум свободного диска перед CTAS (мера 3).
# DISKFREE_GUARD_2026-06-22: после DROP_FIRST старой таблицы нужно:
#   ~7.3 GB (новая fact_region_spend) + ~7 GB (temp-spill 19M строк @ 384MB work_mem)
#   + 0.7 GB индексы = ~15 GB. Порог 16 GB = с запасом.
_MIN_FREE_GB_BEFORE_CTAS = 16.0


def run(conn, run_id: str = None, use_staging: bool = False) -> dict:
    """
    D2_SPEND_FIX_2026-06-18
    DISKFREE_DROP_FIRST_2026-06-22
    DISKFREE_LZ4_2026-06-22
    DISKFREE_GUARD_2026-06-22

    Контракт совпадает с build_unified.run(conn, run_id) → {'rows':..., 'details':...},
    чтобы вызываться из post-loop pipeline.py / fast_pipeline.py единообразно.

    Идемпотентный полный пересбор: DROP (autocommit) + disk-guard + CTAS + lz4 + PK + ANALYZE.
    Соединение conn передаётся пулом пайплайна.

    SPEND_WORKMEM_2026-06-17 (фикс C): SET LOCAL work_mem='384MB' перед CTAS.
    TEMP_FILE_LIMIT_SAFE_2026-06-18 (фикс F): SAVEPOINT вокруг SET LOCAL temp_file_limit.
    DISKFREE_DROP_FIRST_2026-06-22: DROP в отдельном autocommit-коннекте → OS освобождает
        место ДО начала CTAS → пиковый расход снижен с ~22 GB до ~14.3 GB.
    DISKFREE_LZ4_2026-06-22: lz4 на TEXT-колонках → ожидаемое снижение размера на 15-30%.
    DISKFREE_GUARD_2026-06-22: проверка свободного места ДО CTAS, FAIL FAST при нехватке.
    """
    t0 = time.perf_counter()

    # ── Мера 1: DROP в отдельном autocommit-коннекте, ДО CTAS ─────────────────
    # OS сразу освобождает heap-файл старой таблицы (~7.3 GB) → CTAS стартует
    # без конкуренции за диск со старой копией.
    _drop_old_table(conn)  # DISKFREE_DROP_FIRST_2026-06-22

    # ── Мера 3: disk-guard ДО CTAS ────────────────────────────────────────────
    # После DROP старой таблицы проверяем что свободно достаточно для CTAS + spill.
    # При нехватке — FAIL FAST с понятным сообщением (не disk-full посреди CTAS).
    try:
        import shutil as _shutil_rs
        _free_gb = _shutil_rs.disk_usage('/').free / (1024 ** 3)
        logger.info(
            'DISKFREE_GUARD_2026-06-22: свободно %.1f GB перед CTAS %s '
            '(нужно ≥%.0f GB)', _free_gb, TARGET_TABLE, _MIN_FREE_GB_BEFORE_CTAS
        )
        if _free_gb < _MIN_FREE_GB_BEFORE_CTAS:
            raise RuntimeError(
                f'DISKFREE_GUARD_2026-06-22: недостаточно места перед CTAS '
                f'public.{TARGET_TABLE}: {_free_gb:.1f} GB < {_MIN_FREE_GB_BEFORE_CTAS} GB. '
                f'Освободите место вручную (VACUUM FULL тяжёлых таблиц или '
                f'удалите старые логи/бэкапы) и повторите прогон.'
            )
    except RuntimeError:
        raise
    except Exception as _dg_e:
        logger.warning(
            'DISKFREE_GUARD_2026-06-22: не удалось проверить диск (%s) — продолжаем', _dg_e
        )

    with conn.cursor() as cur:
        # SPEND_WORKMEM_2026-06-17 (фикс C): ограничивает work_mem только этой транзакцией.
        # SET LOCAL — не глобально: сброс при commit/rollback.
        # SPEEDUP_WORKMEM_2026-06-18: 192MB → 384MB (×2). При параллельном запуске 3 билдеров
        # пиковое потребление 3×384MB = 1.15GB — умещается в RAM Victory.
        cur.execute("SET LOCAL work_mem = '384MB'")
        # TEMP_FILE_LIMIT_SAFE_2026-06-18 (фикс F): bi_analytic не имеет прав superuser.
        # SAVEPOINT позволяет откатить только неудачный SET temp_file_limit.
        cur.execute("SAVEPOINT before_tfl")
        try:
            cur.execute("SET LOCAL temp_file_limit = '20GB'")
            cur.execute("RELEASE SAVEPOINT before_tfl")
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT before_tfl")
        # DROP уже выполнен выше через _drop_old_table (autocommit).
        # Таблицы нет → просто CREATE TABLE AS.
        try:
            from spend.build_spend_staging import staging_exists
            _use_staging = use_staging and staging_exists(conn)
        except Exception:
            _use_staging = False
        logger.info(
            'build_region_spend: CTAS public.%s (%s)',
            TARGET_TABLE, 'spend_staging' if _use_staging else SRC_MANAGER
        )
        cur.execute(
            f'CREATE TABLE public.{TARGET_TABLE} AS '
            f'{_build_staging_sql() if _use_staging else _build_sql()}'
        )
        rows = cur.rowcount
        # ── Мера 2: lz4 на TEXT-колонках ──────────────────────────────────────
        # DISKFREE_LZ4_2026-06-22: сжатие применяется до ADD CONSTRAINT/индексов,
        # пока таблица ещё без PK (ALTER TABLE быстрее на свежей таблице).
        # lz4 не перезаписывает данные сразу — применится при следующей записи/VACUUM FULL.
        # Для нового CTAS данные пишутся уже с lz4 → размер меньше с первого прогона.
        for _col in _TEXT_COLS_FOR_LZ4:
            try:
                cur.execute(
                    f'ALTER TABLE public.{TARGET_TABLE} '
                    f'ALTER COLUMN "{_col}" SET COMPRESSION lz4'
                )
            except Exception as _lz4_e:
                logger.warning(
                    'DISKFREE_LZ4_2026-06-22: не удалось SET COMPRESSION lz4 на %s: %s',
                    _col, _lz4_e
                )
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
        f'(грань date×campaign_id×ad_group_id×ad_network_type×id_location) '
        f'+ LEFT JOIN {DICT_LOCATION} + LEFT JOIN {GSHEET_SITES} по account_login; '
        f'D2_SPEND_FIX_2026-06-18; DISKFREE_DROP_FIRST_2026-06-22'
    )
    logger.info('build_region_spend: готово за %.1f сек, %d строк', elapsed, rows)
    return {'rows': rows, 'details': details}


# ── Standalone-режим (ручной запуск + DDL-guard на пустой БД) ──────────────────

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )
    start = datetime.now()
    logger.info('build_region_spend СТАРТ: %s', start)
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
        logger.exception('Ошибка build_region_spend: %s', e)
        raise
    finally:
        conn.close()


if __name__ == '__main__':
    main()
