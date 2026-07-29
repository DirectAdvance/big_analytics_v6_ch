"""
step2_indexes/step2.py — индексы на RAW таблицах + ANALYZE

Индексы создаются ПОСЛЕ вставки данных (шаг 1), не до.
Так планировщик строит их один раз по готовым данным — быстрее.
"""

import logging
import time

import psycopg2

from config.settings import (
    T_RAW_YANDEX, T_RAW_LEADS, T_RAW_CALLS, T_RAW_DOMAINS,
    T_RAW_PERFORM_LEADS,  # PERFORM_LEADS_2026-07-01
)

logger = logging.getLogger('pipeline.step2')

# ── Список индексов ───────────────────────────────────────────────────────────
# Формат: (table, index_name, columns_sql)

INDEXES = [
    # raw_yandex — индексы УДАЛЕНЫ (idx_scan=0, аудит 2026-06-05):
    # Все запросы к raw_yandex делают полный GROUP BY key3 / account_login →
    # seq scan дешевле index scan на 3.5M строк UNLOGGED таблицы.
    # Таблица пересоздаётся DROP+CTAS каждый прогон (step1), индексы всё равно бы падали.

    # raw_leads
    (T_RAW_LEADS,  'idx_raw_leads_domain_id',     'domain_id'),
    (T_RAW_LEADS,  'idx_raw_leads_created',        'created_date'),
    (T_RAW_LEADS,  'idx_raw_leads_utm_source',     'utm_source'),
    (T_RAW_LEADS,  'idx_raw_leads_utm_campaign',   'utm_campaign'),

    # raw_calls
    (T_RAW_CALLS,  'idx_raw_calls_domain_id',     'domain_id'),
    (T_RAW_CALLS,  'idx_raw_calls_created',        'created_date'),

    # raw_domains
    (T_RAW_DOMAINS, 'idx_raw_domains_id',         'id'),

    # PERFORM_LEADS_2026-07-01: raw_perform_leads — индексы для leads_deduped UNION и step3
    (T_RAW_PERFORM_LEADS, 'idx_raw_perform_leads_key3',    'key3'),
    (T_RAW_PERFORM_LEADS, 'idx_raw_perform_leads_domain',  'domain'),
    (T_RAW_PERFORM_LEADS, 'idx_raw_perform_leads_phone',   'phone'),
    (T_RAW_PERFORM_LEADS, 'idx_raw_perform_leads_yclid',   'yclid'),
]

# LATERAL_IDX_2026-07-06: local_gsheet_sites должна быть ANALYZEd ДО step3.
# После step0 TRUNCATE+INSERT planner не видит статистику → выбирает seq scan вместо index.
# ANALYZE обновляет reltuples (4542) → planner правильно оценивает стоимость и выбирает индекс.
ANALYZE_TABLES = [T_RAW_YANDEX, T_RAW_LEADS, T_RAW_CALLS, T_RAW_DOMAINS, T_RAW_PERFORM_LEADS,
                  'local_gsheet_sites']

# Индексы на постоянных (не RAW) таблицах — LATERAL_IDX_2026-07-06
# local_gsheet_sites используется в step3 LATERAL JOIN (SELECT + ORDER BY + LIMIT 1).
# Без индекса на login_key — nested loop x21M строк raw_yandex = x3.5 замедление step3.
# TRUNCATE в step0 не удаляет индексы, поэтому индекс переживает каждый прогон.
INDEXES_STATIC = [
    ('local_gsheet_sites', 'idx_local_gsheet_sites_login_key', 'login_key'),
]


# ── Точка входа шага ─────────────────────────────────────────────────────────

def _exec_with_retry(conn, sql: str, label: str, attempts: int = 3, backoff: float = 2.0) -> bool:
    """Выполнить SQL в собственной транзакции с retry на deadlock/lock_timeout.
    Возвращает True если успех, False если все попытки исчерпаны."""
    for attempt in range(1, attempts + 1):
        try:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL lock_timeout = '30s'")
                cur.execute(sql)
            conn.commit()
            return True
        except (psycopg2.errors.DeadlockDetected, psycopg2.errors.LockNotAvailable) as e:
            conn.rollback()
            if attempt < attempts:
                logger.warning('%s: %s (попытка %d/%d), retry через %.1fс',
                               label, type(e).__name__, attempt, attempts, backoff)
                time.sleep(backoff)
            else:
                logger.error('%s: deadlock/lock_timeout после %d попыток — пропускаем',
                             label, attempts)
                return False
        except Exception:
            conn.rollback()
            raise


def run(conn, run_id: str) -> dict:
    logger.info('Шаг 2: создание индексов на RAW таблицах')
    created = 0
    skipped = 0
    t0 = time.perf_counter()

    # Индексы — каждый отдельной транзакцией (короткие локи, retry на deadlock)
    for table, idx_name, cols in INDEXES:
        sql = f'CREATE INDEX IF NOT EXISTS {idx_name} ON {table} ({cols})'
        ok = _exec_with_retry(conn, sql, f'CREATE INDEX {idx_name}')
        if ok:
            created += 1
            logger.debug('  INDEX %s ON %s (%s)', idx_name, table, cols)
        else:
            skipped += 1

    # Индексы на постоянных таблицах (LATERAL_IDX_2026-07-06)
    for table, idx_name, cols in INDEXES_STATIC:
        sql = f'CREATE INDEX IF NOT EXISTS {idx_name} ON {table} ({cols})'
        ok = _exec_with_retry(conn, sql, f'CREATE INDEX {idx_name}')
        if ok:
            created += 1
            logger.debug('  INDEX %s ON %s (%s)', idx_name, table, cols)
        else:
            skipped += 1

    # ANALYZE — каждый отдельной транзакцией + retry на deadlock
    for table in ANALYZE_TABLES:
        ok = _exec_with_retry(conn, f'ANALYZE {table}', f'ANALYZE {table}')
        if ok:
            logger.debug('  ANALYZE %s', table)
        else:
            skipped += 1

    elapsed = time.perf_counter() - t0
    logger.info('Шаг 2 завершён: %d индексов, %d ANALYZE, %d пропущено за %.1f сек',
                created, len(ANALYZE_TABLES), skipped, elapsed)
    return {'rows': created,
            'details': f'{created} индексов + ANALYZE x{len(ANALYZE_TABLES)} (skipped={skipped})'}
