"""
step7_finalize/step7.py — финализация таблиц

SET LOGGED + индексы + ANALYZE (+ VACUUM по условию bloat) на результирующих таблицах.

Порядок:
  1. ALTER TABLE ... SET LOGGED (для всех result-таблиц)
  2. CREATE INDEX на big_analytics_full
  3. ANALYZE big_analytics_full — ВСЕГДА (свежесть статистики критична)
  4. VACUUM big_analytics_full — только при bloat > VACUUM_BLOAT_PCT
     (O6, 2026-06-09; skip_vacuum=True отключает VACUUM полностью)
"""

import logging
import threading
import time

from config.db import get_conn, put_conn
from config.settings import (
    T_DIRECT, T_SEO, T_PIXEL, T_CROP, T_REVIEWS, T_FULL,
)

logger = logging.getLogger('pipeline.step5')

# O6 (2026-06-09): порог bloat (% dead-tuple) для запуска VACUUM в step7.
# Ниже порога — VACUUM пропускается (ANALYZE выполняется всегда отдельно).
VACUUM_BLOAT_PCT = 20

SOURCE_TABLES = [T_DIRECT, T_SEO, T_PIXEL, T_CROP, T_REVIEWS]


def _dead_pct(cur, table: str):
    """% dead-tuple (n_dead_tup/n_live_tup*100) по pg_stat_user_tables.

    table может быть 'public.big_analytics_full' или 'big_analytics_full' —
    в pg_stat_user_tables relname без схемы, поэтому берём последнюю часть.
    Возвращает float или None (если статистики нет / live=0)."""
    relname = table.split('.')[-1].strip('"')
    try:
        cur.execute(
            "SELECT n_live_tup, n_dead_tup FROM pg_stat_user_tables "
            "WHERE relname = %s",
            (relname,),
        )
        row = cur.fetchone()
    except Exception as e:
        logger.warning('  _dead_pct(%s): %s', relname, e)
        return None
    if not row:
        return None
    n_live, n_dead = row[0] or 0, row[1] or 0
    if n_live <= 0:
        return None
    return 100.0 * n_dead / n_live

INDEXES_FULL = [
    ('idx_full_date',          '"Date"'),
    ('idx_full_domain',        'domain'),
    ('idx_full_source_table',  '_source_table'),
    ('idx_full_salon',         '"салон"'),
    ('idx_full_region',        '"регион"'),
    ('idx_full_account',       'account_login'),
    ('idx_full_campaign_id',   '"CampaignId"'),
]


def _disk_free_gb(cur) -> float:
    """Свободное место на диске через pg_stat_file('/') — быстрый индикатор.

    Использует pg_database_size как прокси: абсолютное свободное место недоступно
    без суперпользователя, поэтому логируем размер БД + занятое pg_tablespace_size.
    Возвращает размер БД в GB для сравнения до/после SET LOGGED.
    """
    try:
        cur.execute("SELECT pg_size_pretty(pg_database_size(current_database()))")
        row = cur.fetchone()
        return row[0] if row else '?'
    except Exception:
        return '?'


def _set_logged_with_checkpoint(table: str) -> None:
    """SET LOGGED для одной таблицы в отдельном autocommit-соединении.

    SET_LOGGED_CHECKPOINT_FIX_2026-06-17: каждый ALTER TABLE SET LOGGED выполняется
    в отдельном autocommit-соединении.

    Механика: SET LOGGED = полная перезапись таблицы в WAL. Для big_analytics_direct
    (~14 GB) WAL-пик = ~14 GB. Сброс накопленного WAL и освобождение сегментов делает
    автоматический checkpoint PostgreSQL (по checkpoint_timeout / max_wal_size).

    CHECKPOINT_REMOVED_2026-07-13: ручной CHECKPOINT после ALTER убран. Роль bi_analytic
    НЕ суперюзер и НЕ в pg_checkpoint (прав не будет — решение Семёна), поэтому
    `cur.execute('CHECKPOINT')` ВСЕГДА падал с "must be superuser or have privileges of
    pg_checkpoint to do CHECKPOINT" и ловился в except ниже как WARNING, засоряя каждый
    лог. Сам SET LOGGED (autocommit → commit до строки CHECKPOINT) применялся штатно;
    ручной сброс WAL никогда не срабатывал. Убираем мёртвый вызов — WAL сбросит
    авто-checkpoint Postgres, поведение SET LOGGED не меняется.
    """
    c = get_conn()
    old_ac = c.autocommit
    try:
        # SET_SESSION_ROLLBACK_FIX_2026-06-17: _conn_is_alive() делает SELECT 1 без
        # commit/rollback — соединение возвращается в пул в состоянии idle-in-transaction.
        # psycopg2 set_session (вызывается при смене autocommit) требует отсутствия
        # открытой транзакции → ProgrammingError. Явный rollback сбрасывает эту
        # транзакцию. Паттерн идентичен фиксу _interim_vacuum (KNOWN_ISSUES #14).
        try:
            c.rollback()
        except Exception:
            pass
        c.autocommit = True
        with c.cursor() as cur:
            # Проверяем persistence: если уже LOGGED — пропускаем (нет WAL-пика)
            cur.execute(
                "SELECT relpersistence FROM pg_class WHERE relname=%s AND relkind='r'",
                (table.strip('"'),)
            )
            row = cur.fetchone()
            if row and row[0] == 'p':
                logger.info('  SET LOGGED %s: уже LOGGED, пропускаем', table)
                return
            # SET LOGGED — может занять минуты на большой таблице
            logger.info('  SET LOGGED %s: начало...', table)
            t_sl = time.perf_counter()
            cur.execute(f'ALTER TABLE {table} SET LOGGED')
            elapsed_sl = time.perf_counter() - t_sl
            logger.info('  SET LOGGED %s: %.1f сек', table, elapsed_sl)
            # CHECKPOINT_REMOVED_2026-07-13: ручной CHECKPOINT убран (нет прав у bi_analytic,
            # всегда падал в WARNING). WAL сбрасывает авто-checkpoint Postgres.
    except Exception as e:
        logger.warning('  SET LOGGED %s: %s', table, e)
        # Не прерываем — продолжаем с остальными таблицами
    finally:
        # SET_SESSION_ROLLBACK_FIX_2026-06-17: rollback перед сменой autocommit —
        # если внутри try была ошибка, соединение может остаться с открытой транзакцией.
        # autocommit=True сбросит все изменения, возврат к old_ac требует чистого состояния.
        try:
            c.rollback()
        except Exception:
            pass
        c.autocommit = old_ac
        put_conn(c)


def run(conn, run_id: str, skip_vacuum: bool = False,
        set_logged_tables=None) -> dict:
    """set_logged_tables — список таблиц для SET LOGGED (None = полный список по умолчанию).

    P1_STEP7_SKIP_DIRECT_SETLOGGED_2026-06-18: fast_pipeline передаёт список БЕЗ T_DIRECT,
    т.к. big_analytics_direct (~14 GB) в fast_pipeline EARLY_TRUNCATE-ится сразу после
    build_unified и перед spend-фазой — делать SET LOGGED 14 GB WAL/CHECKPOINT ради
    таблицы, которую через минуты обнулят, зря (~3-5 мин). pipeline.py передаёт None →
    полный список (обратная совместимость).
    """
    # P1_STEP7_SKIP_DIRECT_SETLOGGED_2026-06-18
    _DEFAULT_SET_LOGGED_ORDER = [T_DIRECT, T_FULL, T_SEO, T_PIXEL, T_CROP, T_REVIEWS]
    _actual_set_logged = set_logged_tables if set_logged_tables is not None else _DEFAULT_SET_LOGGED_ORDER

    logger.info('Шаг 7: финализация (SET LOGGED + индексы + ANALYZE%s)',
                '' if skip_vacuum else ' + VACUUM-по-bloat')
    if set_logged_tables is not None:
        _skipped = [t for t in _DEFAULT_SET_LOGGED_ORDER if t not in set_logged_tables]
        if _skipped:
            logger.info('  SET LOGGED ПРОПУЩЕН для: %s (P1_STEP7_SKIP_DIRECT_SETLOGGED_2026-06-18)',
                        ', '.join(_skipped))
    t0 = time.perf_counter()
    vacuum_ran = False

    # 1. SET LOGGED — последовательно, с CHECKPOINT после каждой таблицы
    # SET_LOGGED_CHECKPOINT_FIX_2026-06-17: исправление "No space left on device"
    # при массовой конвертации UNLOGGED→LOGGED. Порядок: сначала крупные (direct, full),
    # затем мелкие — чтобы WAL пик от big_analytics_direct не перекрывался с остальными.
    # P1_STEP7_SKIP_DIRECT_SETLOGGED_2026-06-18: fast_pipeline может передать урезанный
    # список через set_logged_tables, исключая T_DIRECT (он truncate-ится до spend-фазы).
    with conn.cursor() as cur:
        db_size_before = _disk_free_gb(cur)
    logger.info('  БД размер до SET LOGGED: %s', db_size_before)

    for table in _actual_set_logged:
        _set_logged_with_checkpoint(table)

    with conn.cursor() as cur:
        db_size_after = _disk_free_gb(cur)
    logger.info('  БД размер после SET LOGGED: %s (было %s)', db_size_after, db_size_before)

    with conn.cursor() as cur:
        # 1.5. ag_part1_name — имя без кода (ct0XXX - Name → Name)
        try:
            cur.execute(f'ALTER TABLE {T_FULL} ADD COLUMN IF NOT EXISTS ag_part1_name TEXT')
            cur.execute(f"""
                UPDATE {T_FULL}
                SET ag_part1_name = SPLIT_PART(ag_part1, ' - ', 2)
                WHERE ag_part1 LIKE '% - %'
            """)
            conn.commit()
            logger.debug('  ag_part1_name обновлён')
        except Exception as e:
            logger.warning('  ag_part1_name: %s', e)
            conn.rollback()

    # 2. Индексы на big_analytics_full — параллельно (каждый поток = своё соединение)
    #
    # ⚠️ PoolError fix (2026-06-10): step7 идёт ПЕРЕД step9, а фоновый поток step9
    # prefetch_history крутится во время step7 и держит до ~4 conn из пула. 7 idx-
    # потоков, каждый делает get_conn(), + step9 (~4) > maxconn → "connection pool
    # exhausted", idx_full_campaign_id не строился (run 8c7fadd2). Семафор ограничивает
    # ОДНОВРЕМЕННЫЕ get_conn() в step7 до _IDX_MAX_PARALLEL=3: индексы всё равно строятся
    # параллельно (волнами по 3), скоростной выигрыш сохраняется, но пул не голодает
    # (3 idx + ~4 step9 + headroom ≤ maxconn=12). Семафор берётся ДО get_conn и
    # освобождается ПОСЛЕ put_conn — соединение не удерживается дольше работы.
    _IDX_MAX_PARALLEL = 3
    _idx_sem = threading.Semaphore(_IDX_MAX_PARALLEL)

    def _build_index(idx_name: str, cols: str) -> None:
        _idx_sem.acquire()
        try:
            c = get_conn()
        except Exception:
            _idx_sem.release()
            raise
        try:
            with c.cursor() as cur2:
                cur2.execute(
                    f'CREATE INDEX IF NOT EXISTS {idx_name} ON {T_FULL} ({cols})'
                )
            c.commit()
            logger.debug('  INDEX %s', idx_name)
        except Exception as e:
            logger.warning('  INDEX %s: %s', idx_name, e)
            try:
                c.rollback()
            except Exception:
                pass
        finally:
            put_conn(c)
            _idx_sem.release()

    threads = [
        threading.Thread(target=_build_index, args=(idx_name, cols), daemon=True, name=f'idx_{idx_name}')
        for idx_name, cols in INDEXES_FULL
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 3. ANALYZE всегда + VACUUM по условию bloat (O6, 2026-06-09)
    # ──────────────────────────────────────────────────────────────────────
    # ANALYZE big_analytics_full — ВСЕГДА (статистика критична планировщику/индексам,
    #   особенно после только что построенных индексов; без свежей статистики
    #   последующие шаги (step13 CTAS и т.п.) выбирают патологичные планы).
    # VACUUM — только если есть реальный bloat: после step6 (TRUNCATE+INSERT свежей
    #   таблицы) dead-tuple обычно мало → полный VACUUM избыточен. Гейтим по
    #   n_dead_tup/n_live_tup (pg_stat_user_tables) с порогом VACUUM_BLOAT_PCT.
    #   skip_vacuum=True (fast_pipeline) по-прежнему полностью отключает VACUUM.
    # ANALYZE/VACUUM нельзя в транзакции → conn.autocommit = True на время.
    # max_parallel_maintenance_workers=0 — иначе "could not resize shared memory".
    old_autocommit = conn.autocommit
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute('SET max_parallel_maintenance_workers = 0')

            # ANALYZE — безусловно (свежесть статистики после индексов)
            try:
                cur.execute(f'ANALYZE {T_FULL}')
                logger.debug('  ANALYZE %s', T_FULL)
            except Exception as e:
                logger.warning('  ANALYZE %s: %s', T_FULL, e)

            # VACUUM — по условию bloat (и только если не skip_vacuum)
            if skip_vacuum:
                logger.info('  VACUUM пропущен (skip_vacuum=True)')
            else:
                dead_pct = _dead_pct(cur, T_FULL)
                if dead_pct is not None and dead_pct <= VACUUM_BLOAT_PCT:
                    logger.info(
                        '  VACUUM пропущен: bloat %.1f%% ≤ порога %d%% '
                        '(dead-tuple мало после step6 TRUNCATE+INSERT)',
                        dead_pct, VACUUM_BLOAT_PCT)
                    vacuum_ran = False
                else:
                    _pct_txt = f'{dead_pct:.1f}%' if dead_pct is not None else 'неизвестен'
                    logger.info('  VACUUM запущен: bloat %s > порога %d%%',
                                _pct_txt, VACUUM_BLOAT_PCT)
                    try:
                        cur.execute(f'VACUUM ANALYZE {T_FULL}')
                        logger.debug('  VACUUM ANALYZE %s', T_FULL)
                        vacuum_ran = True
                    except Exception as e:
                        logger.warning('  VACUUM ANALYZE %s: %s', T_FULL, e)
                        vacuum_ran = False
    finally:
        conn.autocommit = old_autocommit

    elapsed = time.perf_counter() - t0
    logger.info('Шаг 7 завершён за %.1f сек', elapsed)
    return {
        'rows': len(INDEXES_FULL),
        'details': (
            f'SET LOGGED x{len(_actual_set_logged)}, '
            f'индексов={len(INDEXES_FULL)}, ANALYZE'
            f'{", VACUUM" if vacuum_ran else ""}'
        ),
    }


def get_explain_sql(conn) -> str:
    """SELECT с использованием индексов big_analytics_full для EXPLAIN ANALYZE.

    Используется explain_capture при EXPLAIN_CAPTURE=1. Индексы уже созданы
    и ANALYZE выполнен внутри run() — план отражает реальное состояние таблицы
    после финализации. Запрос типичный для PBI (группировка по специалисту+дата).
    """
    return f"""
        SELECT
            "специалист",
            "Date",
            _source_table,
            COUNT(*)            AS rows,
            SUM(total_cost)     AS total_cost,
            SUM(prodazhi)       AS prodazhi,
            SUM(priezd)         AS priezd
        FROM {T_FULL}
        WHERE "Date" >= '2026-01-01'
        GROUP BY "специалист", "Date", _source_table
        ORDER BY "Date" DESC, total_cost DESC NULLS LAST
    """
