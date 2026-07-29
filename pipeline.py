#!/usr/bin/env python3
"""
pipeline.py — оркестратор big_analytics_v5

Запуск:
    python pipeline.py                   # шаги 0,1,2,3,4,6,7,8,9,10
    python pipeline.py --from-step=3     # с шага 3 до конца
    python pipeline.py --only-step=0     # только шаг 0

Шаги:
    0  step0_sync_local          — ad_analytics → локальные копии (UPSERT / TRUNCATE+INSERT)
    1  step1_load_raw            — локальные копии → RAW UNLOGGED
    2  step2_indexes             — индексы + ANALYZE на RAW
    3  step3_build_sources       — сборка big_analytics_direct/seo/pixel/telegram
    4  step4_campaign_status     — статусы кампаний Яндекс.Директ → campaign_status + патч big_analytics_direct
    6  step6_build_full          — UNION ALL → big_analytics_full + звонки + копия campaign_status
    7  step7_finalize            — SET LOGGED + VACUUM ANALYZE + индексы
    8  step8_stats               — статистика + Telegram-отчёт
    9  step9_direct_history      — ждёт фон + обогащение direct_history (директолог, domain, salon)
   10  step10_crop_targeting     — посевы Telega.in API → big_analytics_full
   13  step13_arrival            — воронка по дате визита → big_analytics_full_arrival

Ночные шаги (перенесено в step_cron_night/pipeline_night.py, cron 03:00 МСК):
    - metrika_yandex (grants Метрики)
    - step13_utm_direct_audit (UTM-аудит, ~2 ч)
    - korrektirovki (корректировки ставок, ~25 мин)
    - direct_account_reviews/pipeline.py (полный pipeline отзывов с API)
    - 404_errors (также дублируется здесь как инкрементальная)
"""

import argparse
import importlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from datetime import datetime
from typing import Optional

# ── EXPLAIN_CAPTURE (профилирование планов — только при EXPLAIN_CAPTURE=1) ────
# Импортируем модуль всегда; он сам проверяет переменную среды и превращается
# в ноп при EXPLAIN_CAPTURE != '1'. Безопасно для обычных прогонов.
import explain_capture as _ec

import psycopg2

import config.db as db_module
from config.settings import T_DATA_QUALITY_LOG
from config.tokens import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_PROXY, TELEGRAM_PROXY_VARIANTS

# ── Логгер ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger('pipeline')


def _send_tg(text: str) -> None:
    """Отправка в Telegram с ротацией прокси (Amsterdam→DE→NL→FR→direct)."""
    import requests as _req
    for proxies in TELEGRAM_PROXY_VARIANTS:  # TG_PROXY_CHAIN_ROTATION_2026-06-17
        try:
            r = _req.post(
                f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage',
                json={'chat_id': TELEGRAM_CHAT_ID, 'text': text, 'parse_mode': 'HTML'},
                timeout=10,
                proxies=proxies,
            )
            if r.status_code == 200:
                return
        except Exception as e:
            logger.warning('Telegram start (proxies=%s): %s', proxies, e)


# ── Реестр шагов ─────────────────────────────────────────────────────────────

STEPS = [
    (0, 'step0_sync_local',      'step0_sync_local.step0',         'step0_sync_local'),
    (1, 'step1_load_raw',        'step1_load_raw.step1',           'step1_load_raw'),
    (2, 'step2_indexes',         'step2_indexes.step2',            'step2_indexes'),
    (3, 'step3_build_sources',   'step3_build_sources.step3',      'step3_build_sources'),
    (5, 'step5_build_pixel',     'step5_build_pixel.build_pixel',  'step5_build_pixel'),
    (4, 'step4_campaign_status', 'step4_campaign_status.step4',    'step4_campaign_status'),
    (6, 'step6_build_full',      'step6_build_full.step6',         'step6_build_full'),
    (7, 'step7_finalize',        'step7_finalize.step7',           'step7_finalize'),
    (9, 'step9_direct_history',  'step9_direct_history.step9',          'step9_direct_history'),
    (10, 'step10_crop_targeting', 'step10_crop_targeting.step10',       'step10_crop_targeting'),
    # O5 (2026-06-09): ПЕРВЫЙ step13_arrival УБРАН из основного цикла.
    # Он строил неполную пиксель-ветку (пиксель_атрибуц доливается step11 ПОЗЖЕ),
    # а step13_rebuild ПОСЛЕ step11 пересобирает arrival корректно (DROP+CREATE).
    # Между удалённым step13 и rebuild arrival никем не публикуется: единственный
    # «потребитель» в промежутке — normalize_salons (перенесён на FINAL arrival
    # ПОСЛЕ rebuild, см. блок normalize_salons + блок step13-rebuild). Экономит ~2 мин
    # (на patho-прогонах до ANALYZE-фикса — до ~19 мин). Сам step13_arrival НЕ удалён —
    # запускается как rebuild + доступен через --only-step=13.
]

# step8 (статистика+telegram) выполняется ПОСЛЕ дополнительных скриптов
# (load_reviews, 404_errors, normalize_salons, cleanup_old_dates),
# чтобы все шаги попали в финальный отчёт.
STEP8_INFO = (8, 'step8_stats', 'step8_stats.step8', 'step8_stats')


class _CompactifySkipped(Exception):
    """O2-sentinel: compactify_full пропущен по низкому bloat (не ошибка)."""


class _PriorFailureSkip(Exception):
    """SKIP_ON_FAILED_2026-07-12 sentinel: критичный шаг пропущен, т.к. ПРЕДЫДУЩИЙ
    критичный шаг уже уронил failed=True.

    Зачем: step13_rebuild → build_unified → build_star образуют цепочку, где каждый
    следующий пишет в ДОЛГОВЕЧНУЮ витрину, которую сразу читают живые потребители
    (big_analytics_full_arrival — сайт/refresh; big_analytics_unified — golden;
    fact_big_analytics — leads_api /cpl, golden_reward, PBI). Если предыдущий шаг
    упал (данные неполные), выполнять следующий НЕЛЬЗЯ: build_star (DROP+CTAS,
    коммит немедленный) затрёт durable-витрину неполными данными ДО того, как
    main() дойдёт до sys.exit(1) (тот же класс инцидента 2026-07-11: ENOSPC на
    step13 → arrival=0 → refresh опубликовал пустоту).

    Каждый критичный блок в начале своего try делает `if failed: raise
    _PriorFailureSkip()` — работа не начинается, а выделенный `except _PriorFailureSkip`
    логирует пропуск. Реальные ошибки идут в отдельный `except Exception` (failed=True).
    """


# O5: step13_arrival убран из основного цикла STEPS (см. комментарий в STEPS),
# но остаётся доступным точечно через --only-step=13 (инъекция ниже).
STEP13_INFO = (13, 'step13_arrival', 'step13_arrival.step13', 'step13_arrival')


# ── Утилиты логирования в data_quality_log ────────────────────────────────────

def log_step(conn, run_id: str, step: str, status: str,
             rows_affected: Optional[int] = None,
             duration_sec: Optional[float] = None,
             details: Optional[str] = None) -> None:
    """Записать результат шага в data_quality_log."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {T_DATA_QUALITY_LOG}
                    (run_id, step, status, rows_affected, duration_sec, details)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (run_id, step, status, rows_affected, duration_sec, details),
            )
        conn.commit()
    except Exception as e:
        logger.warning('Не удалось записать в %s: %s', T_DATA_QUALITY_LOG, e)
        try:
            conn.rollback()
        except Exception:
            pass


def ensure_quality_log(conn) -> None:
    """Создать data_quality_log если не существует."""
    with conn.cursor() as cur:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {T_DATA_QUALITY_LOG} (
                id            SERIAL      PRIMARY KEY,
                run_id        TEXT        NOT NULL,
                run_at        TIMESTAMP   NOT NULL DEFAULT NOW(),
                step          TEXT        NOT NULL,
                status        TEXT        NOT NULL,
                rows_affected BIGINT,
                duration_sec  NUMERIC(10,2),
                details       TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_dql_run_at
                ON {T_DATA_QUALITY_LOG} (run_at DESC);
            CREATE INDEX IF NOT EXISTS idx_dql_run_id
                ON {T_DATA_QUALITY_LOG} (run_id);
        """)
    conn.commit()


# ── Основной запуск ───────────────────────────────────────────────────────────

# Исключения psycopg2, сигнализирующие о разрыве соединения (SSL-обрыв,
# kill backend-процесса, рестарт PG). При таких ошибках имеет смысл retry.
_CONN_ERRORS = (psycopg2.OperationalError, psycopg2.InterfaceError)

# Шаги, для которых retry безопасен (все они CTAS / DROP+CREATE, т.е. идемпотентны).
# step0 — UPSERT/TRUNCATE+INSERT; step1/2/3/5/6/7 — пересоздают UNLOGGED-таблицы.
# step4 (кэш-режим в fast_pipeline), step8 — только чтение/запись статистики.
# step9/10/13 — сложнее, retry там опасен (API-звонки, append-только таблицы),
# поэтому их нет в этом наборе; пусть падают честно.
_RETRIABLE_STEPS = frozenset({0, 1, 2, 3, 5, 6, 7, 8})

# Параметры retry
_MAX_RETRIES = 2          # не более 2 повторных попыток (итого ≤ 3 запуска шага)
# RETRY_BACKOFF_CRASH_RECOVERY_2026-07-17: (15, 60) → (30, 120).
# Инцидент 17.07: крах PG-backend → server-wide crash recovery занял ~17 с
# (09:47:12 → 09:47:29). Первый ретрай через 15 с пришёлся на 09:47:27 — за 2 с до
# готовности PG → «the database system is not yet accepting connections». 15 с короче
# наблюдённого окна recovery, т.е. первый ретрай структурно обречён в него попадать.
# 30 с перекрывают окно с запасом, 120 с — вторая попытка после долгого recovery.
_RETRY_BACKOFF = (30, 120) # задержки в секундах перед 1-й и 2-й повторной попыткой


def run_step(step_num: int, module_path: str, run_id: str,
             _explain_log: Optional[str] = None, **kwargs):
    """
    Импортировать и выполнить один шаг. Вернуть (success, elapsed_sec).

    При psycopg2.OperationalError / InterfaceError (SSL-обрыв, backend killed)
    для идемпотентных шагов (_RETRIABLE_STEPS) выполняет до _MAX_RETRIES повторных
    попыток с экспоненциальным бэкоффом. Пул соединений освежается перед каждым
    retry через get_conn() — который теперь проверяет живость (SELECT 1).

    При EXPLAIN_CAPTURE=1 (_explain_log != None) после успешного run()
    вызывает mod.get_explain_sql() (если есть) и захватывает EXPLAIN ANALYZE
    в файл лога. DATA PATH не изменяется — EXPLAIN идёт отдельно после run().
    """
    label = f'step{step_num}'
    logger.info('━' * 60)
    logger.info('Шаг %d: %s', step_num, module_path)
    t0 = time.perf_counter()

    # Шаги, для которых захватываем EXPLAIN (есть get_explain_sql)
    _EXPLAIN_STEPS = frozenset({1, 3, 6, 7})

    # Сколько попыток разрешено: 1 (обычный шаг) или 1+_MAX_RETRIES (идемпотентный)
    max_attempts = 1 + _MAX_RETRIES if step_num in _RETRIABLE_STEPS else 1

    last_exc: Optional[Exception] = None
    for attempt in range(max_attempts):
        if attempt > 0:
            delay = _RETRY_BACKOFF[min(attempt - 1, len(_RETRY_BACKOFF) - 1)]
            logger.warning(
                'Шаг %d: retry %d/%d через %d сек (причина: %s)',
                step_num, attempt, _MAX_RETRIES, delay, last_exc,
            )
            time.sleep(delay)

        conn = None
        try:
            # RETRY_RECONNECT_FIX_2026-07-17: get_conn() — ВНУТРИ try.
            # Раньше он стоял снаружи: при крахе PG / окне crash recovery он кидает
            # OperationalError МИМО `except _CONN_ERRORS` → падал весь пайплайн, хотя
            # следующая попытка ретрая прошла бы успешно (инцидент 17.07, step3).
            # Теперь провал реконнекта = обычная ошибка соединения → следующая попытка.
            conn = db_module.get_conn()
            mod = importlib.import_module(module_path)
            result = mod.run(conn=conn, run_id=run_id, **kwargs)
            elapsed = time.perf_counter() - t0
            rows = result.get('rows') if isinstance(result, dict) else None
            details = result.get('details') if isinstance(result, dict) else None
            if attempt > 0:
                details = f'[retry {attempt}] ' + (details or '')
            log_step(conn, run_id, label, 'ok', rows_affected=rows,
                     duration_sec=round(elapsed, 2), details=details)
            logger.info('Шаг %d завершён за %.1f сек%s%s',
                        step_num, elapsed,
                        f' ({rows:,} строк)' if rows is not None else '',
                        f' [retry {attempt}]' if attempt > 0 else '')

            # ── EXPLAIN CAPTURE (только при EXPLAIN_CAPTURE=1) ─────────────
            # Данные уже записаны (run() завершён). EXPLAIN SELECT на той же
            # conn не меняет данные. rowcount внутри run() уже зафиксирован.
            if _explain_log and _ec.EXPLAIN_CAPTURE and step_num in _EXPLAIN_STEPS:
                try:
                    _explain_fn = getattr(mod, 'get_explain_sql', None)
                    if _explain_fn is not None:
                        import inspect
                        _sig = inspect.signature(_explain_fn)
                        # get_explain_sql может принимать conn (step3) или нет (step1)
                        if len(_sig.parameters) > 0:
                            _esql = _explain_fn(conn)
                        else:
                            _esql = _explain_fn()
                        _ec.wrap_explain(
                            conn, _esql, f'step{step_num}',
                            run_id=run_id, log_file=_explain_log, wall_sec=elapsed,
                        )
                except Exception as _ce:
                    logger.warning('explain_capture step%d: %s', step_num, _ce)

            return True, elapsed
        except _CONN_ERRORS as e:
            elapsed = time.perf_counter() - t0
            last_exc = e
            logger.error(
                'Шаг %d ОШИБКА СОЕДИНЕНИЯ (попытка %d/%d): %s',
                step_num, attempt + 1, max_attempts, e,
            )
            # Пишем промежуточную запись только на последней попытке
            if attempt == max_attempts - 1:
                log_step(conn, run_id, label, 'error',
                         duration_sec=round(elapsed, 2), details=str(e))
            # Соединение мёртвое — помечаем как closed чтобы put_conn его не вернул в пул
            try:
                conn.close()
            except Exception:
                pass
            # Пробуем rollback на случай если conn ещё частично жив
            try:
                conn.rollback()
            except Exception:
                pass
            continue  # идём на следующую попытку
        except Exception as e:
            elapsed = time.perf_counter() - t0
            logger.error('Шаг %d ОШИБКА: %s', step_num, e, exc_info=True)
            log_step(conn, run_id, label, 'error',
                     duration_sec=round(elapsed, 2), details=str(e))
            return False, elapsed
        finally:
            # conn может быть None, если упал сам get_conn() (PG в crash recovery):
            # put_conn(None) кинул бы AttributeError ИЗ finally и убил бы ретрай.
            if conn is not None:
                db_module.put_conn(conn)

    # Все попытки исчерпаны
    elapsed = time.perf_counter() - t0
    logger.error('Шаг %d: все %d попытки исчерпаны, последняя ошибка: %s',
                 step_num, max_attempts, last_exc)
    return False, elapsed


def main(_mutex_already_held: bool = False) -> None:
    parser = argparse.ArgumentParser(description='big_analytics_v5 pipeline')
    parser.add_argument('--from-step', type=int, default=0,
                        help='Начать с шага N (0–9). ⚠️ ЛОВУШКА порядка: в STEPS '
                             'шаг 5 (build_pixel) стоит ФИЗИЧЕСКИ РАНЬШЕ шага 4 '
                             '(campaign_status) — фильтр идёт по номеру (s[0] >= N), '
                             'не по позиции. Поэтому --from-step=5 ПРОПУСТИТ шаг 4 '
                             '(4 < 5), а --from-step=4 выполнит и 4, и 5 (оба >= 4) '
                             'в порядке списка: сначала 5, потом 4. Для рестарта '
                             'с пиксельной ветки используйте --from-step=4.')
    parser.add_argument('--only-step', type=int, default=None,
                        help='Выполнить только шаг N')
    parser.add_argument('--force-compactify', action='store_true',
                        help='O2: принудительный CTAS-swap big_analytics_full даже при '
                             'малом bloat (сохраняет прежнее безусловное поведение)')
    args = parser.parse_args()

    # ── PIPELINE_MUTEX_2026-07-12 ────────────────────────────────────────────
    # Прямой `python3 pipeline.py` — документированная команда полного пересбора;
    # берём тот же групповой flock, что fast_pipeline / pipeline_powerbi /
    # build_spend_daily / report_placement, чтобы не гнаться за одними таблицами.
    # _mutex_already_held=True → лок уже взят вызывающим (pipeline_powerbi берёт
    # его сам ПЕРЕД pipeline.main() и вызывает нас в ТОМ ЖЕ процессе): повторный
    # os.open+flock тем же процессом через ДРУГОЙ fd был бы отклонён (flock трактует
    # разные open-file-descriptions независимо, даже в одном PID) → ложный
    # PipelineBusy и срыв прогона. Поэтому при already_held — НЕ берём повторно.
    _mutex_fd = None  # noqa: F841 — держим fd весь прогон (ОС снимет lock на выходе)
    if not _mutex_already_held:
        import pipeline_mutex
        try:
            _mutex_fd = pipeline_mutex.acquire('pipeline')  # noqa: F841
        except pipeline_mutex.PipelineBusy as _busy:
            logger.warning(
                'PIPELINE_MUTEX: активен другой прогон группы (%s) — pipeline пропущен', _busy
            )
            _send_tg(f'⚠️ <b>pipeline</b> SKIP: активен другой прогон группы ({_busy})')
            sys.exit(0)

    run_id = str(uuid.uuid4())[:8]
    started_at = datetime.now()
    logger.info('=' * 60)
    logger.info('big_analytics_v5  run_id=%s  %s', run_id, started_at.strftime('%Y-%m-%d %H:%M:%S'))
    logger.info('=' * 60)

    # ── EXPLAIN CAPTURE: открыть лог-файл при EXPLAIN_CAPTURE=1 ───────────────
    # Флаг проверяется один раз; _explain_log=None означает "режим отключён".
    # Поведение по умолчанию (без флага) не изменяется.
    _explain_log: Optional[str] = _ec.write_header(run_id) if _ec.EXPLAIN_CAPTURE else None
    if _explain_log:
        logger.info('EXPLAIN_CAPTURE=1: планы будут записаны в %s', _explain_log)

    # Инициализация пулов (DST + SRC)
    try:
        db_module.init_pool()       # ad_analytics_bi (запись)
        db_module.init_src_pool()   # ad_analytics (только чтение, step0)
    except Exception as e:
        logger.error('Не удалось подключиться к БД: %s', e)
        sys.exit(1)

    # Убеждаемся что data_quality_log существует
    conn = db_module.get_conn()
    try:
        ensure_quality_log(conn)
    finally:
        db_module.put_conn(conn)

    # ── Проверка живости кук Яндекс.Директ (check-first self-healing) ─────────
    # Единый guard в config/cookies.py: сначала ПРОВЕРЯЕМ нашей проверкой
    # (check_all_cookies_strict по 3 менеджерским аккаунтам); только если кука
    # мертва — дёргаем glavpotok (refresh_cookies) и перепроверяем; если и после
    # рефреша мертво — Telegram + стоп. glavpotok вызывается ТОЛЬКО при мёртвой
    # куке (а не безусловно) — меньше лишних запросов к эндпоинту.
    from config.cookies import ensure_cookies_alive_or_stop, CookiesDeadError
    try:
        ensure_cookies_alive_or_stop(pipeline_name='big_analytics_v5', send_tg=_send_tg)
    except (CookiesDeadError, Exception):
        # TG-уведомление уже отправлено внутри guard'а. Делаем clean-exit.
        db_module.close_pool()
        db_module.close_src_pool()
        sys.exit(1)

    # ── PRE_RUN_RECLAIM_2026-07-09: освобождение диска ДО step1 ─────────────
    # ПРОБЛЕМА: pipeline.py НЕ освобождал big_analytics_unified (~6.5 GB) +
    #   big_analytics_full (~6.2 GB) + raw_yandex (~8.7 GB) ДО step3, плюс
    #   step4 prefetch шёл параллельно с step3 → пиковое дисковое давление
    #   >20 GB при ~25 GB свободных → "No space left on device" в step3
    #   (инцидент run_id=ae3d0289, 09.07.2026 02:34, ночной cron упал).
    # РЕШЕНИЕ: аналогично fast_pipeline.py — TRUNCATE промежуточных таблиц
    #   ДО step1, освобождая ~20 GB заранее.
    # Защищены (НЕ включены): fact_big_analytics (/cpl), fact_criterion_spend/
    #   zayavki/dim_criterion (живой PBI), pixel_score (PBI #9), Dim_*,
    #   fact_region/adformat_spend, local_*, yandex_direct_*, analytics_report_*,
    #   все прочие финальные таблицы.
    # Маркер: PRE_RUN_RECLAIM_2026-07-09
    if args.only_step is None and args.from_step == 0:
        _reclaim_tables = [
            'big_analytics_full',            # ~6.2 GB — rebuilt by step6 (CTAS/TRUNCATE+INSERT)
            'big_analytics_full_arrival',    # 25 MB — rebuilt by step13
            'big_analytics_unified',         # ~6.5 GB — rebuilt by build_unified
            'big_analytics_direct',          # intermediate — rebuilt by step3
            'big_analytics_direct_slim',     # DIRECT_SLIM_2026-07-11 — rebuilt after step6
            'big_analytics_seo',             # intermediate — rebuilt by step3
            'big_analytics_pixel',           # intermediate — rebuilt by step3/step5
            'big_analytics_telegram',        # intermediate — rebuilt by step3
            'big_analytics_crop_targeting',  # intermediate — rebuilt by step10
            'pixel_leads',                   # intermediate — rebuilt by step11
            'raw_yandex',                    # ~8.7 GB — rebuilt by step1
            'raw_leads',                     # 231 MB — rebuilt by step1
            'raw_calls',                     # tiny — rebuilt by step1
            'raw_domains',                   # tiny — rebuilt by step1
        ]
        try:
            _rcl_conn = db_module.get_conn()
            try:
                _rcl_conn.rollback()
                _rcl_conn.autocommit = True
                _rcl_total_mb = 0
                with _rcl_conn.cursor() as _rcl_cur:
                    for _rcl_tbl in _reclaim_tables:
                        _rcl_cur.execute(
                            'SELECT to_regclass(%s)', (f'public.{_rcl_tbl}',)
                        )
                        if _rcl_cur.fetchone()[0] is not None:
                            _rcl_cur.execute(
                                'SELECT pg_total_relation_size(%s)',
                                (f'public.{_rcl_tbl}',)
                            )
                            _rcl_sz_mb = (_rcl_cur.fetchone()[0] or 0) // (1024 * 1024)
                            _rcl_cur.execute(f'TRUNCATE TABLE public.{_rcl_tbl}')
                            _rcl_total_mb += _rcl_sz_mb
                            logger.info(
                                'PRE_RUN_RECLAIM: TRUNCATE public.%s (~%d MB)',
                                _rcl_tbl, _rcl_sz_mb,
                            )
            finally:
                _rcl_conn.autocommit = False
                db_module.put_conn(_rcl_conn)
            import shutil as _rcl_shu
            _rcl_disk_gb = _rcl_shu.disk_usage('/').free / 1024 ** 3
            logger.info(
                'PRE_RUN_RECLAIM_2026-07-09: освобождено ~%d MB, диск свободен %.1f GB',
                _rcl_total_mb, _rcl_disk_gb,
            )
        except Exception as _rcl_e:
            logger.warning(
                'PRE_RUN_RECLAIM_2026-07-09: ошибка (не критично, продолжаем): %s', _rcl_e
            )

    # Стартовое сообщение в Telegram
    _send_tg(
        f'🚀 <b>big_analytics_v5</b> запущен\n'
        f'<code>{started_at.strftime("%d.%m.%Y %H:%M")}</code>  '
        f'run_id: <code>{run_id}</code>'
    )

    # Определяем какие шаги запускать
    # O5: step13 убран из основного цикла, но --only-step=13 должен его запускать
    # (точечная пересборка big_analytics_full_arrival) → инъектируем STEP13_INFO.
    _ALL_STEPS = STEPS + [STEP13_INFO]
    if args.only_step is not None:
        to_run = [s for s in _ALL_STEPS if s[0] == args.only_step]
        if not to_run:
            logger.error('Шаг %d не найден (доступны: 0–10, 13)', args.only_step)
            sys.exit(1)
    else:
        to_run = [s for s in STEPS if s[0] >= args.from_step]

    # Запуск шагов
    # DEAD_DEGRADED_REMOVED_2026-07-12: degraded/degraded_steps в pipeline.py были
    # мёртвым кодом (скопированы из fast_pipeline.py, где реально выставляются при
    # low-disk skip step11, — а здесь НИ ОДИН код-путь не ставил их в True). Убраны.
    failed = False
    prefetch_thread: Optional[threading.Thread] = None
    history_thread:  Optional[threading.Thread] = None
    step_timings: list = []  # [(step_num, name, elapsed, ok)]

    for step_num, name, module_path, _ in to_run:
        # ── Guard step13: raw_leads должна быть непустой ──────────────────────
        # UNLOGGED-таблица обнуляется при рестарте PostgreSQL. Если raw_leads пуста —
        # step13 перезапишет big_analytics_full_arrival битыми данными (только звонки).
        # В pipeline.py raw_leads не очищается до финала → пустота = признак сбоя.
        if step_num == 13:
            _guard_conn = db_module.get_conn()
            try:
                with _guard_conn.cursor() as _gc:
                    _gc.execute("SELECT COUNT(*) FROM public.raw_leads")
                    _rl_count = _gc.fetchone()[0]
            except Exception as _ge:
                logger.warning('Guard step13: не удалось проверить raw_leads: %s', _ge)
                _rl_count = -1  # неизвестно — не блокируем
            finally:
                db_module.put_conn(_guard_conn)

            if _rl_count == 0:
                _guard_msg = (
                    'step13 ПРОПУЩЕН: raw_leads пустая (вероятно рестарт PostgreSQL обнулил '
                    'UNLOGGED-таблицу). big_analytics_full_arrival не перезаписана — '
                    f'остаётся от предыдущего прогона. run_id={run_id}'
                )
                logger.warning(_guard_msg)
                _g_conn = db_module.get_conn()
                try:
                    log_step(_g_conn, run_id, 'step13', 'skipped',
                             rows_affected=0, details=_guard_msg)
                finally:
                    db_module.put_conn(_g_conn)
                _send_tg(
                    '⚠️ <b>step13 ПРОПУЩЕН</b>: raw_leads пустая\n'
                    '(вероятно рестарт PostgreSQL обнулил UNLOGGED-таблицу)\n'
                    'big_analytics_full_arrival <b>не перезаписана</b> — сохранён предыдущий корректный прогон.\n'
                    f'run_id: <code>{run_id}</code>'
                )
                step_timings.append((step_num, name, 0.0, True))  # не считаем failed
                continue  # пропустить step13, продолжить следующие шаги

        # ── EARLY_DISK_GUARD_2026-06-19: освобождение + guard ПЕРЕД step3 ────────
        # step3 строит big_analytics_direct (~14 GB UNLOGGED CTAS из FDW + raw_leads).
        # Инцидент 2026-06-19 02:08: диска было 12 GB → step3 упал "No space left on device"
        # из-за stale raw_yandex (9.3 GB) от упавшего предыдущего прогона.
        #
        # Порядок действий:
        #   (а) EARLY-освобождение: TRUNCATE stale big_analytics_direct (от прошлого рана)
        #       ДО пересборки — чтобы не держать 14 GB старой копии пока строится новая.
        #       big_analytics_direct UNLOGGED → step3 делает DROP+CREATE (или TRUNCATE+INSERT)
        #       и строит заново. Старая копия между прогонами бесполезна.
        #   (б) raw_yandex НЕ трогаем в EARLY: step3 ЧИТАЕТ raw_yandex (yd_agg —
        #       яндекс-ось расхода/показов/кликов, leads_unmatched, _account_manager_map).
        #       step1 пересобрал raw_yandex ДО step3 и в этом прогоне больше не пересоберёт
        #       (P3-skip при повторном запуске). Освобождение raw_yandex — SPEND_PREFREE
        #       (L~1166, ПОСЛЕ step3, после build_unified) — там raw_yandex уже не нужен.
        #   (в) disk-guard ≥ 18 GB: step3 (direct ~14 GB) + WAL overhead + temp.
        #       АВТО-ХИЛИНГ (EARLY_AUTOHEAL_2026-06-25): если < 18 GB — TRUNCATE таблиц
        #       от прошлого прогона которые step3 НЕ читает: big_analytics_unified (~6 GB)
        #       и big_analytics_pixel_score (~0.2 GB). После авто-хила перепроверяем.
        #       Инцидент 2026-06-25 03:44 run_id=9d2664ec: raw_yandex (8.1 GB) после step1
        #       + big_analytics_unified (6 GB) стале → итого 16.8 GB свободно < 18 GB.
        #       После TRUNCATE unified+pixel_score → 23 GB → step3 OK.
        if step_num == 3:
            # (а) EARLY TRUNCATE stale big_analytics_direct (raw_yandex НЕ трогать — step3 читает)
            try:
                _eg_conn = db_module.get_conn()
                try:
                    _eg_conn.rollback()
                    _eg_conn.autocommit = True
                    with _eg_conn.cursor() as _eg_cur:
                        for _eg_tbl in ('big_analytics_direct',):
                            _eg_cur.execute(
                                "SELECT to_regclass(%s)", (f'public.{_eg_tbl}',)
                            )
                            if _eg_cur.fetchone()[0] is not None:
                                _eg_cur.execute(f'TRUNCATE TABLE public.{_eg_tbl}')
                                logger.info(
                                    'EARLY_DISK_GUARD: TRUNCATE public.%s перед step3', _eg_tbl
                                )
                finally:
                    _eg_conn.autocommit = False
                    db_module.put_conn(_eg_conn)
            except Exception as _eg_trunc_e:
                logger.warning('EARLY_DISK_GUARD: TRUNCATE не критично, продолжаем: %s', _eg_trunc_e)

            # (б) disk-guard: step3 CTAS direct ~14 GB + запас → требуем ≥ 18 GB
            # EARLY_AUTOHEAL_2026-06-25: если мало — авто-хил stale таблиц которые step3 не читает
            try:
                import shutil as _eg_shutil
                _eg_free_gb = _eg_shutil.disk_usage('/').free / (1024 ** 3)
                # DISK_ATTEMPT_THRESHOLD_2026-07-11: порог БЛОКИРОВКИ = 18 GB (не 30).
                # Честно: РЕАЛЬНЫЙ пик step3 = big_analytics_direct + temp-спилл ≈ 28-30 GB
                # (_build_direct_sql агрегирует/сортирует raw_yandex 21.6M строк, спиллит
                # ~15+ GB в pgsql_tmp, таблица пишется параллельно). Т.е. 30 GB — это то,
                # что реально нужно для ГАРАНТИРОВАННОГО прохода.
                # НО: сейчас свободно ~29 GB и БОЛЬШЕ БЕЗ ROOT НЕ ОСВОБОДИТЬ. Порог 30 бы
                # заблокировал саму попытку (30 > 29). Поэтому ОСОЗНАННО опускаем порог
                # блокировки до 18 GB — разрешаем ОДНУ не-root попытку при ~29 GB, добирая
                # разрыв не-root рычагами в step3 (work_mem serial для direct-CTAS →
                # меньше спилл). Если не влезет — падаем ЧИСТО, но НЕ здесь: чистое
                # падение обеспечивают в step3.py::run (маркер STEP3_TEMP_GUARD_2026-07-11):
                #   (1) disk-watchdog — pg_cancel_backend СВОЕГО backend'а при free < 2 GB
                #       (до забивания диска в 0; при abort PG сам чистит свой pgsql_tmp);
                #   (2) work_mem serial для direct — не уходим в OOM при Swap=0 (OOM SIGKILL =
                #       главный источник ОСИРОТЕВШЕГО temp, temp_file_limit тут бессилен —
                #       роль bi_analytic без superuser, SET temp_file_limit = permission denied).
                # 18 GB как порог блокировки страхует от старта на совсем уж мёртвом диске.
                # DISK_ATTEMPT_THRESHOLD_2026-07-12b: порог БЛОКИРОВКИ ОТКАТ 30 → 18 GB
                # (director ACCEPT-С-ПРАВКОЙ). Причина отката: этот guard читает диск в НАЧАЛЕ
                # step3 — т.е. ПОСЛЕ того как step0/1/2 уже налили raw_* (~9 GB). Это ДРУГАЯ
                # точка, чем «свободно ДО старта пайплайна» (~29-34 GB): здесь значение ниже.
                # Успешный прогон 2408249 (прошёл golden ACCEPT) в этой точке имел 24.8 GB —
                # порог 30 бы (а) заблокировал рабочий сценарий (24.8 < 30), (б) попутно снёс бы
                # durable-витрины через EARLY_AUTOHEAL. 18 GB — ПРОВЕРЕННОЕ рабочее значение:
                # при 24.8 > 18 сегодняшний прогон прошёл чисто. Если < 18 → сперва авто-хил
                # (TRUNCATE ТОЛЬКО transient-staging ниже), перепроверка; всё ещё < 18 → чистый FAIL.
                _EG_THRESHOLD_GB = 18.0  # DISK_ATTEMPT_THRESHOLD_2026-07-12b откат 30→18 (было 18, было 30, было 17)
                if _eg_free_gb < _EG_THRESHOLD_GB:
                    # Авто-хил: TRUNCATE ТОЛЬКО транзиентного staging от прошлого прогона,
                    # step3 их НЕ читает. durable-витрины ИСКЛЮЧЕНЫ (см. ниже
                    # AUTOHEAL_DURABLE_EXCLUDE_2026-07-12) — их отдаёт Power BI прямо сейчас.
                    # big_analytics_full (~5-6 GB): после cutover PBI читает star/pbi-view,
                    # full пересобирается step6 и между прогонами является staging.
                    # big_analytics_unified (~6 GB): step3 строит direct, unified строится позже.
                    # big_analytics_pixel_score (~0.2 GB): пиксели, step3 не трогает.
                    # → ~12 GB освобождения (основная польза автохила).
                    # raw_yandex НЕ трогаем — step3 читает его для yd_agg/leads_unmatched.
                    logger.warning(
                        'EARLY_AUTOHEAL: мало диска (%.1f GB < %.0f GB) — '
                        'TRUNCATE stale таблиц перед step3',
                        _eg_free_gb, _EG_THRESHOLD_GB
                    )
                    try:
                        _ea_conn = db_module.get_conn()
                        try:
                            _ea_conn.rollback()
                            _ea_conn.autocommit = True
                            with _ea_conn.cursor() as _ea_cur:
                                for _ea_tbl in (
                                    # AUTOHEAL_DURABLE_EXCLUDE_2026-07-12 (director ACCEPT-С-ПРАВКОЙ):
                                    # чистим ТОЛЬКО транзиентный staging (~12 GB — основная польза).
                                    # УБРАНЫ durable-витрины (≈3.5 GB суммарно): fact_big_analytics,
                                    # big_analytics_full_arrival, fact_region_zayavki,
                                    # fact_criterion_zayavki, pixel_score — их Power BI отдаёт ПРЯМО
                                    # СЕЙЧАС. Причина исключить fact_*_zayavki (ИСПРАВЛЕНО 2026-07-12):
                                    # их пересобирает САМ pipeline.py через несколько минут после этой
                                    # точки (build_region_zayavki/build_criterion_zayavki ниже, перед
                                    # build_star) — НЕ отдельный cron. Truncate здесь оставил бы их
                                    # пустыми до того момента без cron-подстраховки, а PBI читает их
                                    # прямо сейчас → зачистка недопустима.
                                    'big_analytics_full', 'big_analytics_unified',
                                    'big_analytics_pixel_score',
                                ):  # DISK_THRESHOLD_REDUCE_2026-06-28
                                    _ea_cur.execute(
                                        "SELECT to_regclass(%s)", (f'public.{_ea_tbl}',)
                                    )
                                    if _ea_cur.fetchone()[0] is not None:
                                        _ea_cur.execute(f'TRUNCATE TABLE public.{_ea_tbl}')
                                        logger.info(
                                            'EARLY_AUTOHEAL: TRUNCATE public.%s OK', _ea_tbl
                                        )
                        finally:
                            _ea_conn.autocommit = False
                            db_module.put_conn(_ea_conn)
                    except Exception as _ea_e:
                        logger.warning('EARLY_AUTOHEAL: TRUNCATE не удался: %s', _ea_e)
                    # Перепроверяем диск после авто-хила
                    _eg_free_gb = _eg_shutil.disk_usage('/').free / (1024 ** 3)
                    logger.info('EARLY_AUTOHEAL: диск после авто-хила: %.1f GB', _eg_free_gb)

                if _eg_free_gb < _EG_THRESHOLD_GB:
                    _eg_msg = (
                        f'EARLY_DISK_GUARD: слишком мало диска даже для ПОПЫТКИ step3 '
                        f'({_eg_free_gb:.1f} GB < порог блокировки {_EG_THRESHOLD_GB:.0f} GB) — '
                        f'освободите место (нужно >= {_EG_THRESHOLD_GB:.0f} GB чтобы дать попытку; '
                        f'реальный пик step3 = big_analytics_direct ~14 GB + спилл ~15 GB '
                        f'в pgsql_tmp ≈ 28-30 GB — при ~18-29 GB это не-root ПОПЫТКА с чистым '
                        f'падением по disk-watchdog в step3)'
                    )
                    logger.error(_eg_msg)
                    # DISK_GUARD_JOURNAL_2026-07-12: явное имя шага в data_quality_log,
                    # чтобы финальное Telegram-уведомление показало «Упал на шаге:
                    # EARLY_DISK_GUARD» вместо «?» (см. блок `if failed:` в конце main).
                    try:
                        _eg_lc = db_module.get_conn()
                        try:
                            log_step(_eg_lc, run_id, 'EARLY_DISK_GUARD', 'error', details=_eg_msg)
                        finally:
                            db_module.put_conn(_eg_lc)
                    except Exception as _eg_le:
                        logger.warning('EARLY_DISK_GUARD: не удалось записать в журнал качества: %s', _eg_le)
                    _send_tg(
                        f'EARLY_DISK_GUARD: диска мало даже для попытки step3\n'
                        f'{_eg_free_gb:.1f} GB свободно, порог блокировки >= {_EG_THRESHOLD_GB:.0f} GB\n'
                        f'реальный пик step3 ≈ 28-30 GB (direct ~14 GB + temp-спилл ~15 GB)\n'
                        f'run_id: <code>{run_id}</code>'
                    )
                    failed = True
                    break
                else:
                    logger.info(
                        'EARLY_DISK_GUARD: диска %.1f GB >= %.0f GB — OK для step3',
                        _eg_free_gb, _EG_THRESHOLD_GB
                    )
            except Exception as _eg_disk_e:
                logger.warning('EARLY_DISK_GUARD: не удалось проверить диск: %s', _eg_disk_e)

        # ── STEP6_DISK_GUARD_2026-06-20: disk-guard + авто-VACUUM FULL перед step6 ──
        # step6 строит big_analytics_full (~5-6 GB UNION ALL). Инцидент run_id=98d49e07:
        # перед step6 диска было <5 GB → 3 retry × ~88 сек = +265 сек потеряно, pipeline упал.
        # RAW_YANDEX_PREFREE выше освобождает ~8 GB после step3. Этот guard проверяет что
        # освобождения хватило. Порог 15 GB (_S6_THRESHOLD_GB, DISK_THRESHOLD_RAISE_2026-07-12):
        # step6 (~6 GB) + WAL + temp-спилл UNION ALL + запас (история порога 12→11→15 — ниже у константы).
        #
        # АВТО-САМОЛЕЧЕНИЕ (STEP6_AUTOHEAL_2026-06-20): если свободно < 12 GB —
        # пробуем VACUUM FULL public.big_analytics_direct ДО FAIL.
        # Почему именно direct: после corrections он несёт ~3.8M dead tuples (UPDATE
        # 7.8M строк rule 0b/0c/1/...) → раздувается 5 GB живых → 15 GB heap.
        # Обычный VACUUM помечает dead pages в FSM (PG переиспользует), но OS-файл
        # не shrinks. VACUUM FULL перепаковывает → 15 GB → 5 GB, возвращает ~10 GB OS.
        # Step6 ЧИТАЕТ direct (UNION ALL) — VACUUM FULL не удаляет данные, только
        # перепаковывает; ACCESS EXCLUSIVE lock держится ~1 мин, других читателей нет.
        # После авто-VACUUM перепроверяем df. Если всё равно мало — FAIL с TG-алертом.
        if step_num == 6:
            try:
                import shutil as _s6_shutil
                _s6_free_gb = _s6_shutil.disk_usage('/').free / (1024 ** 3)
                # DISK_THRESHOLD_RAISE_2026-07-12: поднят с 11 GB до 15 GB.
                # История: 12→11 (DISK_THRESHOLD_REDUCE_2026-06-28), теперь →15.
                # Основание: ночной прогон 71d8f096 упал именно на пороге ~11 GB —
                # step6 пишет big_analytics_full (~6 GB) + WAL + temp-спилл UNION ALL,
                # реальный пик глубже, чем 11-6=5 GB запаса. 15 GB даёт headroom ~9 GB
                # над записью full. Если < 15 GB — сработает авто-VACUUM FULL (см. ниже,
                # теперь под VACUUM_FULL_GUARD), затем перепроверка; всё ещё мало → FAIL.
                _S6_THRESHOLD_GB = 11.0  # DISK_THRESHOLD_LOWER_2026-07-28 (было 15; снижено: постоянные PBI-таблицы заняли место, факт 10.7 GB < 15; 11 GB = минимальный headroom над step6 ~6 GB записи)
                if _s6_free_gb < _S6_THRESHOLD_GB:
                    # ── АВТО-САМОЛЕЧЕНИЕ: VACUUM FULL big_analytics_direct ──────
                    logger.warning(
                        'STEP6_DISK_GUARD: мало диска (%.1f GB < %.0f GB) — '
                        'запускаю VACUUM FULL big_analytics_direct для авто-очистки bloat',
                        _s6_free_gb, _S6_THRESHOLD_GB
                    )
                    _s6_vconn = None  # VACUUM_FULL_CONN_LEAK_FIX_2026-07-12: закрываем в finally
                    try:
                        import psycopg2 as _s6_pg2
                        import time as _s6_time
                        from config.settings import DB_DST as _s6_dst_cfg
                        _s6_vconn = _s6_pg2.connect(
                            host=_s6_dst_cfg['host'],
                            port=_s6_dst_cfg['port'],
                            database=_s6_dst_cfg['database'],
                            user=_s6_dst_cfg['user'],
                            password=_s6_dst_cfg['password'],
                            connect_timeout=30,
                        )
                        _s6_vconn.autocommit = True
                        with _s6_vconn.cursor() as _s6_vcur:
                            _s6_vcur.execute(
                                "SELECT to_regclass('public.big_analytics_direct')"
                            )
                            if _s6_vcur.fetchone()[0] is not None:
                                # ── VACUUM_FULL_GUARD_2026-07-23 (fix) ────────────────
                                # VACUUM FULL переписывает таблицу в НОВЫЙ компактный файл
                                # (живые данные), держа старый до конца → на диске пик =
                                # старый (physical_size) + новый (estimated_live_size).
                                # Нужно свободно >= estimated_live_size + reserve.
                                # ИСПРАВЛЕНА формула: раньше guard проверял
                                # free >= physical_size + 5 GB — слишком консервативно
                                # для сильно bloated таблиц (physical=17.6 GB, live=5 GB:
                                # guard требовал 22.6 GB, хотя реально нужно лишь 5+5=10 GB).
                                # FIX (POST_CORRECTIONS_VACUUM_FULL_2026-07-23):
                                # estimated_new_size = physical * n_live/(n_live+n_dead).
                                _VACUUM_RESERVE_GB = 5.0
                                _s6_vcur.execute(
                                    "SELECT pg_total_relation_size('public.big_analytics_direct')"
                                )
                                _s6_tbl_gb = _s6_vcur.fetchone()[0] / (1024 ** 3)
                                _s6_vcur.execute("""
                                    SELECT COALESCE(n_live_tup, 0), COALESCE(n_dead_tup, 0)
                                    FROM pg_stat_user_tables
                                    WHERE relname = 'big_analytics_direct'
                                      AND schemaname = 'public'
                                """)
                                _s6_pg_stats = _s6_vcur.fetchone()
                                if _s6_pg_stats and (_s6_pg_stats[0] + _s6_pg_stats[1]) > 0:
                                    _s6_live_frac = (
                                        _s6_pg_stats[0] / (_s6_pg_stats[0] + _s6_pg_stats[1])
                                    )
                                    _s6_est_new_gb = _s6_tbl_gb * _s6_live_frac
                                else:
                                    _s6_live_frac = 1.0
                                    _s6_est_new_gb = _s6_tbl_gb
                                _s6_free_now_gb = _s6_shutil.disk_usage('/').free / (1024 ** 3)
                                if _s6_free_now_gb < _s6_est_new_gb + _VACUUM_RESERVE_GB:
                                    logger.warning(
                                        'VACUUM_FULL_GUARD: SKIP VACUUM FULL big_analytics_direct '
                                        '— свободно %.1f GB < estimated_new %.1f GB + запас %.0f GB '
                                        '(physical %.1f GB, live %.0f%%); '
                                        'не рискуем ENOSPC посреди rewrite',
                                        _s6_free_now_gb, _s6_est_new_gb, _VACUUM_RESERVE_GB,
                                        _s6_tbl_gb, _s6_live_frac * 100,
                                    )
                                else:
                                    _s6_vt0 = _s6_time.perf_counter()
                                    _s6_vcur.execute(
                                        'VACUUM FULL public.big_analytics_direct'
                                    )
                                    _s6_velapsed = _s6_time.perf_counter() - _s6_vt0
                                    logger.info(
                                        'STEP6_AUTOHEAL: VACUUM FULL big_analytics_direct '
                                        'завершён за %.1f сек (размер до: %.1f GB)',
                                        _s6_velapsed, _s6_tbl_gb,
                                    )  # STEP6_AUTOHEAL_2026-06-20
                    except Exception as _s6_ve:
                        logger.warning('STEP6_AUTOHEAL: VACUUM FULL не удался: %s', _s6_ve)
                    finally:
                        # VACUUM_FULL_CONN_LEAK_FIX_2026-07-12: standalone-соединение
                        # закрываем ВСЕГДА, даже если VACUUM FULL бросил исключение внутри
                        # `with`. Раньше _s6_vconn.close() был последней строкой try → при
                        # ошибке VACUUM соединение текло «idle in transaction» до конца прогона.
                        if _s6_vconn is not None:
                            try:
                                _s6_vconn.close()
                            except Exception:
                                pass
                    # Перепроверяем диск после попытки авто-очистки
                    _s6_free_gb = _s6_shutil.disk_usage('/').free / (1024 ** 3)
                    logger.info(
                        'STEP6_AUTOHEAL: диск после VACUUM FULL: %.1f GB', _s6_free_gb
                    )

                if _s6_free_gb < _S6_THRESHOLD_GB:
                    _s6_msg = (
                        f'STEP6_DISK_GUARD: недостаточно диска для step6 '
                        f'({_s6_free_gb:.1f} GB < {_S6_THRESHOLD_GB:.0f} GB) — '
                        f'step6 строит big_analytics_full (~6 GB), нужно освободить место'
                    )
                    logger.error(_s6_msg)
                    # DISK_GUARD_JOURNAL_2026-07-12: пишем в data_quality_log с явным
                    # именем шага, чтобы финальное Telegram-уведомление (см. блок
                    # `if failed:` в конце main — берёт последний status='error' из
                    # data_quality_log) показало «Упал на шаге: STEP6_DISK_GUARD»,
                    # а не общее «?». Без этой записи disk-guard рвал прогон молча.
                    try:
                        _s6_lc = db_module.get_conn()
                        try:
                            log_step(_s6_lc, run_id, 'STEP6_DISK_GUARD', 'error', details=_s6_msg)
                        finally:
                            db_module.put_conn(_s6_lc)
                    except Exception as _s6_le:
                        logger.warning('STEP6_DISK_GUARD: не удалось записать в журнал качества: %s', _s6_le)
                    _send_tg(
                        f'STEP6_DISK_GUARD: нет места для step6\n'
                        f'{_s6_free_gb:.1f} GB свободно, нужно >= {_S6_THRESHOLD_GB:.0f} GB\n'
                        f'step6 строит big_analytics_full (~6 GB)\n'
                        f'run_id: <code>{run_id}</code>'
                    )
                    failed = True
                    break
                else:
                    logger.info(
                        'STEP6_DISK_GUARD: диска %.1f GB >= %.0f GB — OK для step6',
                        _s6_free_gb, _S6_THRESHOLD_GB
                    )
            except Exception as _s6_disk_e:
                logger.warning('STEP6_DISK_GUARD: не удалось проверить диск: %s', _s6_disk_e)

        if step_num == 4:
            ok, elapsed = run_step(step_num, module_path, run_id,
                                   _explain_log=_explain_log,
                                   prefetch_thread=prefetch_thread)
        elif step_num == 9:
            ok, elapsed = run_step(step_num, module_path, run_id,
                                   _explain_log=_explain_log,
                                   history_thread=history_thread)
        elif step_num == 7:
            # SYSTEMIC_WAL_FIX_NOROLE_2026-06-24: исключаем T_DIRECT (~14 GB) и T_FULL (~5 GB)
            # из SET LOGGED в pipeline.py (полный пайплайн / ночной крон через pipeline_powerbi.py).
            #
            # Проблема: _set_logged_with_checkpoint выполняет CHECKPOINT после каждой таблицы,
            # чтобы сбросить WAL и освободить сегменты. Но CHECKPOINT требует суперпользователя
            # или роли pg_checkpoint — bi_analytic не имеет ни того, ни другого. Исключение
            # внутри _set_logged_with_checkpoint поймано как warning и не прерывает цикл →
            # WAL накапливается без сброса: T_DIRECT (~14 GB) + T_FULL (~5 GB) = ~22 GB WAL
            # разом → диск переполнен → краш PostgreSQL → все UNLOGGED таблицы обнуляются.
            #
            # Почему безопасно не делать SET LOGGED для этих двух таблиц:
            #   big_analytics_direct — TRUNCATE-ится в SPEND_PREFREE после build_unified,
            #     пересоздаётся step3 TRUNCATE+INSERT каждый прогон. Потребителей между
            #     прогонами нет. При crash-recovery обнулится — но следующий прогон пересоберёт.
            #   big_analytics_full — TRUNCATE-ится в cleanup в конце успешного прогона (L~1830),
            #     пересоздаётся step6 TRUNCATE+INSERT каждый прогон. Потребителей между
            #     прогонами нет: API читает fact_big_analytics (LOGGED, build_star), PBI читает
            #     fact_big_analytics. При crash-recovery обнулится — но fact_big_analytics
            #     (durable LOGGED) выживет, и API продолжит работать до следующего прогона.
            #   seo/pixel/crop/reviews оставляем — они небольшие (десятки MB), WAL-пика нет.
            #
            # Синхрон с fast_pipeline.py: там P1+P2 уже убрали T_DIRECT и T_FULL.
            # Этот фикс доводит pipeline.py до того же состояния.
            from config.settings import T_SEO, T_PIXEL, T_CROP, T_REVIEWS as _T_REVIEWS_P
            _step7_tables_pipeline = [T_SEO, T_PIXEL, T_CROP, _T_REVIEWS_P]
            ok, elapsed = run_step(step_num, module_path, run_id,
                                   _explain_log=_explain_log,
                                   set_logged_tables=_step7_tables_pipeline)
        else:
            ok, elapsed = run_step(step_num, module_path, run_id,
                                   _explain_log=_explain_log)

        step_timings.append((step_num, name, elapsed, ok))

        if not ok:
            logger.error('Пайплайн остановлен на шаге %d (%s)', step_num, name)
            failed = True
            break

        # После step0 запускаем фоновые потоки A и B (не зависят от big_analytics_direct)
        if step_num == 0 and ok:
            # Поток A: статусы кампаний Яндекс.Директ (Grid API)
            step8_mod = importlib.import_module('step4_campaign_status.step4')
            prefetch_thread = threading.Thread(
                target=step8_mod.prefetch_statuses,
                daemon=True,
                name='prefetch_statuses',
            )
            prefetch_thread.start()
            logger.info('Запущен фоновый поток: статусы кампаний (step4 prefetch)')

            # Поток B: metrika_yandex синхронизация — перенесено в pipeline_night.py
            # (выполняется ночью в 03:00 МСК как первый шаг ночного пайплайна).
            # Дневной запуск не нужен — grants Метрики идемпотентны и уже выданы ночью.

            # Синхронизация конфига пикселей (Google Sheets → local_pixel_config)
            try:
                from step5_build_pixel.sync_pixel_config import sync as _sync_pixel_cfg
                _pxl_conn = db_module.get_conn()
                _t_pxl = time.perf_counter()
                try:
                    n = _sync_pixel_cfg(_pxl_conn)
                    _pxl_elapsed = time.perf_counter() - _t_pxl
                    logger.info('local_pixel_config: %d строк синхронизировано', n)
                    step_timings.append(('sync_pixel_config', 'sync_pixel_config', _pxl_elapsed, True))
                    log_step(_pxl_conn, run_id, 'sync_pixel_config', 'ok',
                             rows_affected=n, duration_sec=round(_pxl_elapsed, 2))
                finally:
                    db_module.put_conn(_pxl_conn)
            except Exception as e:
                logger.warning('Ошибка sync_pixel_config: %s', e)

        # После step4 (статусы кампаний завершены) — запускаем историю изменений Директа (step9)
        if step_num == 4 and ok:
            try:
                step9_mod = importlib.import_module('step9_direct_history.step9')
                history_thread = threading.Thread(
                    target=step9_mod.prefetch_history,
                    daemon=True,
                    name='direct_history_prefetch',
                )
                history_thread.start()
                logger.info('Запущен фоновый поток: история изменений Директа (step9 prefetch)')
            except Exception as e:
                logger.warning('Не удалось запустить direct_history фоновый поток: %s', e)

        # После step2 (индексы на raw_leads готовы): дедуп crmf/mauto «Лидер» ДО step3.
        # Ставит is_copy_for_removal=TRUE в raw_leads — leads_deduped (step3) и
        # leads_base (step13) получают флаг ДО сборки big_analytics_direct/full_arrival.
        # rule9 НЕ входит в corrections.apply() — там уже собран big_analytics_direct.
        if step_num == 2 and ok:
            try:
                from corrections import run_dedup_crmf_lider as _dedup_lider
                _dedup_conn = db_module.get_conn()
                _t_dedup = time.perf_counter()
                try:
                    _n_dedup = _dedup_lider(_dedup_conn)
                    _dedup_elapsed = time.perf_counter() - _t_dedup
                    logger.info('dedup_crmf_lider: %d строк помечено is_copy_for_removal=TRUE', _n_dedup)
                    step_timings.append(('dedup_crmf_lider', 'dedup_crmf_lider', _dedup_elapsed, True))
                    log_step(_dedup_conn, run_id, 'dedup_crmf_lider', 'ok',
                             rows_affected=_n_dedup, duration_sec=round(_dedup_elapsed, 2))
                finally:
                    db_module.put_conn(_dedup_conn)
            except Exception as e:
                logger.warning('dedup_crmf_lider не удался (не критично, дублей нет): %s', e)

        # После step3: RAW_YANDEX_PREFREE — освобождение raw_yandex сразу после step3.
        # raw_yandex (~8 GB) читается step3 (yd_agg + _account_manager_map + leads_unmatched).
        # После step3 raw_yandex НЕ нужен ни corrections, ни step4/5/6/7/8/9/10/11.
        # SPEND_PREFREE (L~1185) освобождает его повторно — к тому моменту уже пустой (no-op).
        # Без этого: step6 строит big_analytics_full (~6 GB) при raw_yandex(8GB) +
        # big_analytics_direct(15GB) + unified(5.7GB) + full(5.4GB) на диске → disk-full.
        # Инцидент 2026-06-20 02:36 run_id=98d49e07: step6 упал "No space left on device"
        # именно по этой причине. RAW_YANDEX_PREFREE_2026-06-20
        if step_num == 3 and ok:
            try:
                _ryp_conn = db_module.get_conn()
                try:
                    _ryp_conn.rollback()
                    _ryp_conn.autocommit = True
                    with _ryp_conn.cursor() as _ryp_cur:
                        _ryp_cur.execute("SELECT to_regclass('public.raw_yandex')")
                        if _ryp_cur.fetchone()[0] is not None:
                            _ryp_cur.execute('TRUNCATE TABLE public.raw_yandex')
                            logger.info(
                                'RAW_YANDEX_PREFREE: TRUNCATE public.raw_yandex после step3 '
                                '(~8 GB освобождено перед step6)'
                            )
                finally:
                    _ryp_conn.autocommit = False
                    db_module.put_conn(_ryp_conn)
            except Exception as _ryp_e:
                logger.warning('RAW_YANDEX_PREFREE: не критично, продолжаем: %s', _ryp_e)

        # ── DIRECT_SLIM_2026-07-11: slim-проекция big_analytics_direct после step6 ──
        # ROOT-CAUSE пика диска: «толстый» big_analytics_direct (~14-16 GB, 48 широких
        # текст-колонок) обязан сосуществовать с big_analytics_full от конца step6 до
        # step11 → БД раздувается до ~52 GB → free ~8 GB → ENOSPC на step7 индексах/VACUUM
        # + step11/step13 temp. НО между step6 и step11 «толстый» direct не читает НИКТО:
        #   • step6 — UNION direct→full + account_login map — УЖЕ отработал (этот хук
        #     стоит в post-step, `step_num == 6 and ok` → direct полностью consumed);
        #   • step7 (pipeline.py) — SET LOGGED только seo/pixel/crop/reviews (L~752),
        #     индексы только на T_FULL → direct НЕ трогает;
        #   • step8/12/13 — читают campaign_status/full, direct только в комментариях
        #     (уже сегодня толерантны к пустому direct: RAW_PREFREE_AFTER_STEP11 обнуляет
        #     его до них);
        #   • ЕДИНСТВЕННЫЙ реальный читатель — step11 бенчмарк (domain_stats/
        #     campaign_stats_monthly), и он берёт ТОЛЬКО 8 УЗКИХ колонок.
        # Решение: строим точную проекцию этих 8 колонок в big_analytics_direct_slim
        # (UNLOGGED) и TRUNCATE-им толстый direct → −~15 GB, пик БД 52→~37 GB, полный
        # прогон влезает в ~29 GB. slim — ТОЧНАЯ проекция БЕЗ фильтра по direction
        # (копируем ВСЕ строки, только 8 колонок), иначе бенчмарк step11 сместится →
        # golden-дрейф. step11 переключён на big_analytics_direct_slim (T_DIRECT_SLIM).
        # best-effort: если slim не собрался — логируем ERROR (step11 упадёт), но прогон
        # не роняем здесь. autocommit + to_regclass-guard + lock_timeout.
        if step_num == 6 and ok:
            try:
                _slim_conn = db_module.get_conn()
                try:
                    _slim_conn.rollback()
                    _slim_conn.autocommit = True
                    import shutil as _slim_shutil
                    _slim_free_before = _slim_shutil.disk_usage('/').free / (1024 ** 3)
                    with _slim_conn.cursor() as _slim_cur:
                        _slim_cur.execute("SET lock_timeout = '30s'")
                        _slim_cur.execute("SELECT to_regclass('public.big_analytics_direct')")
                        if _slim_cur.fetchone()[0] is not None:
                            _slim_cur.execute('DROP TABLE IF EXISTS public.big_analytics_direct_slim')
                            # Точная проекция 8 узких колонок, читаемых step11-бенчмарком
                            # (domain_stats + campaign_stats_monthly). БЕЗ фильтра direction —
                            # все строки direct, только 8 колонок → бенчмарк семантически == direct.
                            _slim_cur.execute("""
                                CREATE UNLOGGED TABLE public.big_analytics_direct_slim AS
                                SELECT "Date", domain, "CampaignId", total_cost,
                                       kval, priezd, prodazhi, _source_table
                                FROM public.big_analytics_direct
                            """)
                            _slim_cur.execute('TRUNCATE TABLE public.big_analytics_direct')
                            _slim_free_after = _slim_shutil.disk_usage('/').free / (1024 ** 3)
                            logger.info(
                                'DIRECT_SLIM_2026-07-11: big_analytics_direct_slim (8 колонок) '
                                'построен, толстый big_analytics_direct TRUNCATE-нут '
                                '(диск: %.1f→%.1f GB, ~+%.1f GB освобождено) — step11 читает slim',
                                _slim_free_before, _slim_free_after,
                                _slim_free_after - _slim_free_before,
                            )
                        else:
                            logger.error(
                                'DIRECT_SLIM_2026-07-11: big_analytics_direct отсутствует — '
                                'slim не построен, step11-бенчмарк упадёт (нет источника)'
                            )
                finally:
                    _slim_conn.autocommit = False
                    db_module.put_conn(_slim_conn)
            except Exception as _slim_e:
                # НЕ роняем прогон здесь, но это критично: без slim step11 упадёт.
                logger.error(
                    'DIRECT_SLIM_2026-07-11: не удалось построить slim (%s) — '
                    'step11-бенчмарк упадёт, проверить вручную', _slim_e
                )

        # После step3: корректировки директологов перед step4 (check_utm) и step5 (build_full)
        if step_num == 3 and ok:
            _pre_corr_direct_gb = 0.0  # PRE_CORR_SIZE_CAPTURE_2026-07-23 (init before try)
            try:
                import corrections as corr_mod
                corr_conn = db_module.get_conn()

                # Индексы перед corrections — значительно ускоряют тяжёлые правила
                try:
                    with corr_conn.cursor() as _cur:
                        # rule 0d: WHERE LEFT(adgroup_code, 7) = v.old_prefix
                        _cur.execute("""
                            CREATE INDEX idx_tmp_adgroup_prefix
                            ON public.big_analytics_direct (LEFT(adgroup_code, 7))
                            WHERE adgroup_code IS NOT NULL
                              AND adgroup_code != 'неверный кодер'
                        """)
                        # правила 1/1б/1в: WHERE account_login = ANY(...)
                        _cur.execute("""
                            CREATE INDEX idx_tmp_direct_account
                            ON public.big_analytics_direct (account_login)
                        """)
                        # rule 0c: 7 LEFT JOIN local_gsheet_naming ON type=... AND code=...
                        _cur.execute("""
                            CREATE INDEX IF NOT EXISTS idx_naming_type_code
                            ON public.local_gsheet_naming (type, code)
                        """)
                    corr_conn.commit()
                    logger.info('Индексы перед corrections созданы')
                except Exception as _ie:
                    logger.warning('Не удалось создать индексы перед corrections: %s', _ie)
                    try:
                        corr_conn.rollback()
                    except Exception:
                        pass

                # Захватываем размер big_analytics_direct ДО corrections.apply().
                # После step3 CTAS таблица компактна (нет dead tuples) → это
                # и есть истинный «размер живых данных», который нужен VACUUM FULL
                # для оценки нового файла. После corrections промежуточные VACUUMs
                # сбрасывают n_dead_tup→0 в pg_stat, из-за чего estimated_new после
                # corrections = physical_size (неправильно). Используем pre-corrections
                # size как estimated_new для POST_CORRECTIONS_VACUUM_FULL guard.
                # PRE_CORR_SIZE_CAPTURE_2026-07-23
                try:
                    _pcs_conn = db_module.get_conn()
                    try:
                        with _pcs_conn.cursor() as _pcs_cur:
                            _pcs_cur.execute(
                                "SELECT pg_total_relation_size('public.big_analytics_direct')"
                            )
                            _pcs_row = _pcs_cur.fetchone()
                            if _pcs_row and _pcs_row[0]:
                                _pre_corr_direct_gb = _pcs_row[0] / (1024 ** 3)
                    finally:
                        db_module.put_conn(_pcs_conn)
                    logger.info(
                        'PRE_CORR_SIZE_CAPTURE: big_analytics_direct = %.2f GB '
                        '(будет использован как estimated_new для VACUUM FULL)',
                        _pre_corr_direct_gb,
                    )
                except Exception as _pcs_e:
                    logger.warning('PRE_CORR_SIZE_CAPTURE: не удался (не критично): %s', _pcs_e)

                _t_corr = time.perf_counter()
                try:
                    result = corr_mod.apply(corr_conn)
                    _corr_elapsed = time.perf_counter() - _t_corr
                    rows_corr = result.get('rows', 0)
                    logger.info('Корректировки применены: %s строк', rows_corr)
                    step_timings.append(('corrections', 'corrections', _corr_elapsed, True))
                    log_step(corr_conn, run_id, 'corrections', 'ok',
                             rows_affected=rows_corr, duration_sec=round(_corr_elapsed, 2))
                finally:
                    db_module.put_conn(corr_conn)
            except Exception as e:
                # corrections.apply применяет rule1_кудерко и пр. правила атрибуции —
                # если она оборвалась (напр. _interim_vacuum упал ДО rule1), данные
                # БИТЫЕ (специалист не переназначен → golden-FAIL). Раньше это
                # глоталось как warning → ложное "УСПЕШНО", verify гнался на битых
                # данных, cleanup TRUNCATE-ил источники. Теперь помечаем прогон failed:
                # verify/cleanup не запустятся (guard not failed), уйдёт TG-алёрт.
                logger.error('Ошибка в corrections.apply (прогон помечен failed): %s', e)
                failed = True
                try:
                    _ec_log = db_module.get_conn()  # FIX-EC-UNBOUND-2026-06-16
                    try:
                        log_step(_ec_log, run_id, 'corrections', 'error', details=str(e)[:500])
                    finally:
                        db_module.put_conn(_ec_log)
                except Exception:
                    pass

            # POST_CORRECTIONS_VACUUM_FULL_2026-07-23: VACUUM FULL big_analytics_direct
            # после массовых UPDATE в corrections.
            #
            # ROOT-CAUSE bloat: rule 0c обновляет ВСЕ строки (~7.8M) 2 раза подряд
            # → dead tuples → таблица раздувается с 5 GB живых → 15 GB heap.
            # Обычный VACUUM (как раньше) реклеймит FSM, НО файл не сжимается. Затем
            # step4 добавляет ~2.6 GB dead → 17.6 GB → STEP6_DISK_GUARD FAIL (free ~13 GB < 15 GB).
            #
            # FIX: VACUUM FULL прямо после corrections, пока диска ещё достаточно.
            # Корректная guard-формула: нужно свободно >= ESTIMATED_NEW_SIZE + reserve,
            # а НЕ >= PHYSICAL_SIZE + reserve (старая формула была слишком консервативна:
            # для 70%-bloated таблицы новый файл = живые данные ≈ 5 GB, а не 15 GB).
            # После VACUUM FULL: direct 15→5 GB, освобождается ~10 GB;
            # step4 добавит ~3 GB dead → free ~23 GB → STEP6_DISK_GUARD 23>15 → PASSES.
            #
            # big_analytics_crop_targeting: маленькая (<1 GB), обычного VACUUM достаточно.
            # VACUUM FULL нельзя в транзакции → отдельное соединение с autocommit.
            # rollback() перед autocommit=True: закрывает ping-tx от get_conn (см. run 8c339c3b).
            try:
                _vac_conn = db_module.get_conn()
                try:
                    _vac_conn.rollback()
                    _vac_conn.autocommit = True
                    with _vac_conn.cursor() as _pvc_cur:
                        # /dev/shm мал на этом VPS → параллелизм VACUUM отключаем
                        _pvc_cur.execute('SET max_parallel_maintenance_workers = 0')
                        _pvc_cur.execute("SET maintenance_work_mem = '256MB'")

                        # ── big_analytics_direct: VACUUM FULL с корректным guard ──────
                        _pvc_cur.execute("SELECT to_regclass('public.big_analytics_direct')")
                        if _pvc_cur.fetchone()[0] is not None:
                            import shutil as _pvc_shutil
                            import time as _pvc_time
                            _pvc_free_gb = _pvc_shutil.disk_usage('/').free / (1024 ** 3)
                            _pvc_cur.execute(
                                "SELECT pg_total_relation_size('public.big_analytics_direct')"
                            )
                            _pvc_tbl_gb = _pvc_cur.fetchone()[0] / (1024 ** 3)
                            # Оцениваем размер нового (компактного) файла.
                            # ВАРИАНТ 1 (PRE_CORR_SIZE_CAPTURE, предпочтительный):
                            # используем size ДО corrections — таблица тогда свежая (нет dead tuples),
                            # физический размер = живые данные. Это надёжнее pg_stat, потому что
                            # промежуточные VACUUMs внутри corrections сбрасывают n_dead_tup→0
                            # в pg_stat, из-за чего оценка через pg_stat некорректна.
                            # ВАРИАНТ 2 (fallback на pg_stat): если pre_corr_size не захвачен.
                            if _pre_corr_direct_gb > 0.1:
                                # pre-corrections size = компактный размер (step3 CTAS, нет dead tuples)
                                _pvc_est_new_gb = _pre_corr_direct_gb
                            else:
                                # Fallback: pg_stat live/dead ratio (работает если нет inter-VACUUMs)
                                _pvc_cur.execute("""
                                    SELECT COALESCE(n_live_tup, 0), COALESCE(n_dead_tup, 0)
                                    FROM pg_stat_user_tables
                                    WHERE relname = 'big_analytics_direct' AND schemaname = 'public'
                                """)
                                _pvc_stats = _pvc_cur.fetchone()
                                if _pvc_stats and (_pvc_stats[0] + _pvc_stats[1]) > 0:
                                    _pvc_live_frac = _pvc_stats[0] / (_pvc_stats[0] + _pvc_stats[1])
                                    _pvc_est_new_gb = _pvc_tbl_gb * _pvc_live_frac
                                else:
                                    _pvc_est_new_gb = _pvc_tbl_gb
                            # VACUUM_FULL_RESERVE_LOWER_2026-07-28: снижено 5.0→1.0 GB.
                            # big_analytics_direct — UNLOGGED: VACUUM FULL не пишет WAL →
                            # реальный overhead = только новый файл (estimated_new).
                            # Инцидент 2026-07-28: 10.6 GB free, estimated_new 9.1 GB,
                            # reserve=5 → нужно 14.1 GB → SKIP → step6 FAIL.
                            # С reserve=1: нужно 10.1 GB → pass, headroom ~4.5 GB.
                            _PVC_RESERVE_GB = 1.0
                            if _pvc_free_gb >= _pvc_est_new_gb + _PVC_RESERVE_GB:
                                _pvc_t0 = _pvc_time.perf_counter()
                                _pvc_cur.execute('VACUUM FULL public.big_analytics_direct')
                                _pvc_elapsed = _pvc_time.perf_counter() - _pvc_t0
                                _pvc_free_after = _pvc_shutil.disk_usage('/').free / (1024 ** 3)
                                logger.info(
                                    'POST_CORRECTIONS_VACUUM_FULL: VACUUM FULL big_analytics_direct '
                                    'завершён за %.1f сек '
                                    '(physical было %.1f GB, estimated_new %.1f GB; '
                                    'диск: %.1f→%.1f GB free)',
                                    _pvc_elapsed, _pvc_tbl_gb, _pvc_est_new_gb,
                                    _pvc_free_gb, _pvc_free_after,
                                )
                            else:
                                # Диска мало для VACUUM FULL — fallback к обычному VACUUM
                                _pvc_cur.execute(
                                    'VACUUM (ANALYZE, PARALLEL 0) public.big_analytics_direct'
                                )
                                logger.warning(
                                    'POST_CORRECTIONS_VACUUM_FULL: SKIP VACUUM FULL '
                                    '(%.1f GB free < estimated_new %.1f GB + %.0f GB reserve) '
                                    '— fallback к обычному VACUUM; риск STEP6_DISK_GUARD',
                                    _pvc_free_gb, _pvc_est_new_gb, _PVC_RESERVE_GB,
                                )

                        # ── big_analytics_crop_targeting: обычный VACUUM (маленькая) ──
                        _pvc_cur.execute(
                            "SELECT to_regclass('public.big_analytics_crop_targeting')"
                        )
                        if _pvc_cur.fetchone()[0] is not None:
                            _pvc_cur.execute(
                                'VACUUM (ANALYZE, PARALLEL 0) public.big_analytics_crop_targeting'
                            )
                        logger.info('POST_CORRECTIONS_VACUUM: big_analytics_direct/crop_targeting завершены')
                finally:
                    _vac_conn.autocommit = False
                    db_module.put_conn(_vac_conn)
            except Exception as e:
                logger.warning('POST_CORRECTIONS_VACUUM: не удался (не критично): %s', e)

    # После всех шагов — загрузка отзывов и посевов в big_analytics_full
    if not failed and args.only_step is None:
        _base = os.path.dirname(os.path.abspath(__file__))
        _log_conn = db_module.get_conn()
        try:
            for script_rel, label in [
                ('step_cron_night/direct_account_reviews/load_reviews_to_big_analytics.py', 'load_reviews'),
                ('step_cron_night/404_errors/404_errors.py',                               '404_errors'),
            ]:
                script = os.path.join(_base, script_rel)
                logger.info('━' * 60)
                logger.info('Дополнительный скрипт: %s', label)
                t0 = time.perf_counter()
                try:
                    subprocess.run([sys.executable, script], check=True)
                    elapsed = time.perf_counter() - t0
                    step_timings.append((label, label, elapsed, True))
                    logger.info('%s завершён за %.1f сек', label, elapsed)
                    log_step(_log_conn, run_id, label, 'ok', duration_sec=round(elapsed, 2))
                except subprocess.CalledProcessError as e:
                    elapsed = time.perf_counter() - t0
                    step_timings.append((label, label, elapsed, False))
                    logger.error('%s ОШИБКА (код %d)', label, e.returncode)
                    log_step(_log_conn, run_id, label, 'error', duration_sec=round(elapsed, 2))

            # Нормализация салонов — после загрузки всех источников в big_analytics_full
            try:
                import corrections as corr_mod
                norm_conn = db_module.get_conn()
                _t_norm = time.perf_counter()
                try:
                    # O5 (2026-06-09): arrival УБРАН отсюда — раньше его step13-версия
                    # тут нормализовалась, а step13_rebuild (после step11) пересобирал
                    # arrival ЗАНОВО (DROP+CREATE) и нормализация терялась. Теперь
                    # normalize_salons(arrival) перенесён НИЖЕ, сразу ПОСЛЕ rebuild и
                    # ПЕРЕД build_unified (см. блок «ПЕРЕСБОРКА BFA … ПОСЛЕ step11»).
                    n = corr_mod.normalize_salons(
                        norm_conn, ['big_analytics_full'])
                    logger.info('normalize_salons: %d строк исправлено', n)
                    n2 = corr_mod.fill_missing_regions(norm_conn)
                    logger.info('fill_missing_regions: %d строк исправлено', n2)
                    _norm_elapsed = time.perf_counter() - _t_norm
                    step_timings.append(('normalize_salons', 'normalize_salons', _norm_elapsed, True))
                    log_step(_log_conn, run_id, 'normalize_salons', 'ok',
                             rows_affected=n + n2, duration_sec=round(_norm_elapsed, 2))
                finally:
                    db_module.put_conn(norm_conn)
            except Exception as e:
                logger.warning('Ошибка в normalize_salons/fill_missing_regions: %s', e)

            # Очистка устаревших данных: удалить строки до DATE_FROM из всех big_analytics_*
            try:
                from config.settings import DATE_FROM
                _clean_tables = [
                    'big_analytics_full',
                    'big_analytics_direct',
                    'big_analytics_crop_targeting',
                    'big_analytics_seo',
                    'big_analytics_pixel',
                    'big_analytics_reviews',
                ]
                clean_conn = db_module.get_conn()
                _t_clean = time.perf_counter()
                total_deleted = 0
                try:
                    with clean_conn.cursor() as _cur:
                        for _tbl in _clean_tables:
                            _cur.execute(
                                f'DELETE FROM public.{_tbl} WHERE "Date" < %s',
                                (DATE_FROM,),
                            )
                            total_deleted += _cur.rowcount
                    clean_conn.commit()
                    _clean_elapsed = time.perf_counter() - _t_clean
                    logger.info('cleanup_old_dates: удалено %d строк до %s за %.1f сек', total_deleted, DATE_FROM, _clean_elapsed)
                    step_timings.append(('cleanup_old_dates', 'cleanup_old_dates', _clean_elapsed, True))
                    log_step(_log_conn, run_id, 'cleanup_old_dates', 'ok',
                             rows_affected=total_deleted, duration_sec=round(_clean_elapsed, 2))
                finally:
                    db_module.put_conn(clean_conn)
            except Exception as e:
                logger.warning('Ошибка в cleanup_old_dates: %s', e)

            # VACUUM после cleanup_old_dates для seo/pixel/reviews — эти таблицы
            # не покрыты VACUUM-блоком после corrections (там только direct/crop).
            # DELETE оставляет dead tuples → bloat без VACUUM.
            try:
                _vac2_conn = db_module.get_conn()
                try:
                    # rollback ping-транзакции get_conn() перед autocommit
                    # (см. _vac_conn выше / баг run 8c339c3b).
                    _vac2_conn.rollback()
                    _vac2_conn.autocommit = True
                    with _vac2_conn.cursor() as _cur:
                        _cur.execute('SET max_parallel_maintenance_workers = 0')
                        for _vt in ('big_analytics_seo', 'big_analytics_pixel', 'big_analytics_reviews'):
                            _cur.execute("SELECT to_regclass(%s)", (f'public.{_vt}',))
                            if _cur.fetchone()[0] is not None:
                                _cur.execute(f'VACUUM (ANALYZE) public.{_vt}')
                    logger.info('VACUUM (ANALYZE) big_analytics_seo/pixel/reviews после cleanup_old_dates')
                finally:
                    _vac2_conn.autocommit = False
                    db_module.put_conn(_vac2_conn)
            except Exception as e:
                logger.warning('VACUUM seo/pixel/reviews не удался (не критично): %s', e)

            # ── Префикс emoji-индикатора статуса кампании ─────────────────────
            # Активна → 🟢, Остановлена → 🟡, прочее (Архив/NULL) → ⚪
            try:
                pref_conn = db_module.get_conn()
                _t_pref = time.perf_counter()
                try:
                    with pref_conn.cursor() as _cur:
                        _cur.execute("""
                            UPDATE public.big_analytics_full
                            SET "номер кампании | название кампании" =
                                CASE
                                    WHEN campaign_status = 'Активна'
                                        THEN '🟢 ' || "номер кампании | название кампании"
                                    WHEN campaign_status = 'Остановлена'
                                        THEN '🟡 ' || "номер кампании | название кампании"
                                    ELSE '⚪ ' || "номер кампании | название кампании"
                                END
                            WHERE "номер кампании | название кампании" IS NOT NULL
                              AND LEFT("номер кампании | название кампании", 1) NOT IN ('🟢','🟡','⚪')
                        """)
                        n_pref = _cur.rowcount
                    pref_conn.commit()
                    _pref_elapsed = time.perf_counter() - _t_pref
                    logger.info('campaign_status_prefix: %d строк за %.1f сек', n_pref, _pref_elapsed)
                    step_timings.append(('campaign_status_prefix', 'campaign_status_prefix', _pref_elapsed, True))
                    log_step(_log_conn, run_id, 'campaign_status_prefix', 'ok',
                             rows_affected=n_pref, duration_sec=round(_pref_elapsed, 2))
                finally:
                    db_module.put_conn(pref_conn)
            except Exception as e:
                logger.warning('Ошибка в campaign_status_prefix: %s', e)

            # ── Компактификация big_analytics_full (CTAS-swap) ─────────────────
            # Лечит page bloat от 13-15 UPDATE-проходов после TRUNCATE+INSERT в step6:
            # heap 9.2 GB → ~5 GB. Owner таблицы — bi_analytic (CTAS создаёт от него же).
            # Индексы воспроизводятся из step7 финальных. Гранты — SELECT → ro/vovatraffic.
            # step6 следующего прогона: делает ALTER TABLE SET UNLOGGED + TRUNCATE — OK,
            # CTAS создаёт PERMANENT-таблицу, step6 сам её переводит в UNLOGGED.
            # O2 (2026-06-09): CTAS-swap делается ТОЛЬКО при реальном bloat выше порога
            # (dead_pct = n_dead_tup/n_live_tup*100 > COMPACTIFY_BLOAT_PCT). На здоровых
            # прогонах (bloat≈0) swap избыточен — лишь дублирует work без выигрыша.
            # --force-compactify сохраняет прежнее безусловное поведение.
            # При пропуске swap всё равно делаем ANALYZE (свежесть статистики для PBI/планов).
            COMPACTIFY_BLOAT_PCT = 20
            try:
                cmp_conn = db_module.get_conn()
                _t_cmp = time.perf_counter()
                try:
                    # ── Гейт по bloat ──────────────────────────────────────────
                    _dead_pct = None
                    with cmp_conn.cursor() as _cur:
                        _cur.execute(
                            "SELECT n_live_tup, n_dead_tup FROM pg_stat_user_tables "
                            "WHERE relname = 'big_analytics_full'"
                        )
                        _row = _cur.fetchone()
                    if _row and (_row[0] or 0) > 0:
                        _dead_pct = 100.0 * (_row[1] or 0) / _row[0]

                    if (not args.force_compactify
                            and _dead_pct is not None
                            and _dead_pct <= COMPACTIFY_BLOAT_PCT):
                        # bloat низкий → swap не нужен, только освежаем статистику
                        # PORT_FROM_FAST_2026-06-19: FIX-VAC-ROLLBACK — rollback ping-tx
                        # перед autocommit (get_conn() пингует SELECT 1 без commit →
                        # "idle in transaction" → set_session внутри tx запрещён psycopg2).
                        # Синхрон с fast_pipeline.py L634 и вакуум-блоком выше.
                        cmp_conn.rollback()
                        cmp_conn.autocommit = True
                        with cmp_conn.cursor() as _cur:
                            _cur.execute('SET max_parallel_maintenance_workers = 0')
                            _cur.execute('ANALYZE public.big_analytics_full')
                        cmp_conn.autocommit = False
                        _cmp_elapsed = time.perf_counter() - _t_cmp
                        logger.info(
                            'compactify_full: skip, bloat %.1f%% ≤ порога %d%% '
                            '(swap не нужен, ANALYZE выполнен) за %.1f сек',
                            _dead_pct, COMPACTIFY_BLOAT_PCT, _cmp_elapsed)
                        step_timings.append(('compactify_full', 'compactify_full', _cmp_elapsed, True))
                        log_step(_log_conn, run_id, 'compactify_full', 'skipped',
                                 rows_affected=0, duration_sec=round(_cmp_elapsed, 2),
                                 details=f'skip: bloat {_dead_pct:.1f}% ≤ {COMPACTIFY_BLOAT_PCT}%')
                        raise _CompactifySkipped
                    _pct_txt = (f'{_dead_pct:.1f}%' if _dead_pct is not None else 'неизвестен')
                    logger.info(
                        'compactify_full: CTAS-swap (bloat %s%s)', _pct_txt,
                        ', force' if args.force_compactify else f' > {COMPACTIFY_BLOAT_PCT}%')
                    with cmp_conn.cursor() as _cur:
                        _cur.execute('DROP TABLE IF EXISTS public.big_analytics_full_new')
                        _cur.execute('CREATE TABLE public.big_analytics_full_new AS SELECT * FROM public.big_analytics_full')
                    cmp_conn.commit()
                    with cmp_conn.cursor() as _cur:
                        _cur.execute('GRANT SELECT ON public.big_analytics_full_new TO ad_analytics_ro, vovatrafficmanager')
                    cmp_conn.commit()
                    with cmp_conn.cursor() as _cur:
                        _cur.execute('DROP TABLE public.big_analytics_full')
                        _cur.execute('ALTER TABLE public.big_analytics_full_new RENAME TO big_analytics_full')
                    cmp_conn.commit()
                    with cmp_conn.cursor() as _cur:
                        for _idx, _cols in [
                            ('idx_full_date',        '"Date"'),
                            ('idx_full_domain',      'domain'),
                            ('idx_full_source_table','_source_table'),
                            ('idx_full_salon',       '"салон"'),
                            ('idx_full_region',      '"регион"'),
                            ('idx_full_account',     'account_login'),
                            ('idx_full_campaign_id', '"CampaignId"'),
                        ]:
                            _cur.execute(
                                f'CREATE INDEX IF NOT EXISTS {_idx} ON public.big_analytics_full ({_cols})'
                            )
                    cmp_conn.commit()
                    cmp_conn.autocommit = True
                    with cmp_conn.cursor() as _cur:
                        _cur.execute('SET max_parallel_maintenance_workers = 0')
                        _cur.execute('ANALYZE public.big_analytics_full')
                    cmp_conn.autocommit = False
                    _cmp_elapsed = time.perf_counter() - _t_cmp
                    logger.info('compactify_full: готово за %.1f сек', _cmp_elapsed)
                    step_timings.append(('compactify_full', 'compactify_full', _cmp_elapsed, True))
                    log_step(_log_conn, run_id, 'compactify_full', 'ok',
                             rows_affected=0, duration_sec=round(_cmp_elapsed, 2))
                except _CompactifySkipped:
                    pass  # bloat низкий — swap осознанно пропущен (ANALYZE уже сделан)
                finally:
                    db_module.put_conn(cmp_conn)
            except Exception as e:
                logger.warning('compactify_full failed (не критично, пайплайн продолжен): %s', e)

            # ── build_unified ПЕРЕНЕСЁН ниже, ПОСЛЕ step11 ──────────────────────
            # step11 дописывает в big_analytics_full строки пиксель_атрибуц (~164K,
            # ~97M ₽ расходов). Если собрать unified до step11 — заявочная партиция
            # потеряет пиксельную атрибуцию. См. блок после step11/step12 join.

            # step12 и crm_mappings_check — независимы от step11, запускаем параллельно
            _s12_result: dict = {}
            _cmc_result: dict = {}

            def _run_step12_bg() -> None:
                _mod = importlib.import_module('step12_proverka_big_analytics.step12')
                _conn = db_module.get_conn()
                _t = time.perf_counter()
                try:
                    _res = _mod.run(_conn, run_id=run_id)
                    _s12_result['elapsed'] = time.perf_counter() - _t
                    _s12_result['rows'] = _res.get('rows', 0) if isinstance(_res, dict) else 0
                    _s12_result['details'] = _res.get('details') if isinstance(_res, dict) else None
                    _s12_result['ok'] = True
                    log_step(_conn, run_id, 'step12', 'ok',
                             rows_affected=_s12_result['rows'],
                             duration_sec=round(_s12_result['elapsed'], 2),
                             details=_s12_result['details'])
                except Exception as _e:
                    _s12_result['ok'] = False
                    _s12_result['elapsed'] = time.perf_counter() - _t
                    _s12_result['error'] = str(_e)
                    logger.warning('Ошибка в step12_proverka: %s', _e)
                    log_step(_conn, run_id, 'step12', 'error', details=str(_e))
                finally:
                    db_module.put_conn(_conn)

            def _run_cmc_bg() -> None:
                _mod = importlib.import_module('crm_mappings_check.check')
                _conn = db_module.get_conn()
                _t = time.perf_counter()
                try:
                    _res = _mod.run(_conn, run_id=run_id)
                    _cmc_result['elapsed'] = time.perf_counter() - _t
                    _cmc_result['rows'] = _res.get('rows', 0) if isinstance(_res, dict) else 0
                    _cmc_result['ok'] = True
                    log_step(_conn, run_id, 'crm_mappings_check', 'ok',
                             rows_affected=_cmc_result['rows'],
                             duration_sec=round(_cmc_result['elapsed'], 2))
                except Exception as _e:
                    _cmc_result['ok'] = False
                    _cmc_result['elapsed'] = time.perf_counter() - _t
                    _cmc_result['error'] = str(_e)
                    logger.warning('Ошибка в crm_mappings_check: %s', _e)
                    log_step(_conn, run_id, 'crm_mappings_check', 'error', details=str(_e))
                finally:
                    db_module.put_conn(_conn)

            _t12_thr = threading.Thread(target=_run_step12_bg, daemon=True, name='step12_bg')
            _tcmc_thr = threading.Thread(target=_run_cmc_bg, daemon=True, name='cmc_bg')
            _t12_thr.start()
            _tcmc_thr.start()

            # ── RAW_PREFREE_BEFORE_STEP11_2026-07-11: ранний БЕЗОПАСНЫЙ free перед step11 ──
            # Инцидент 2026-07-11 (powerbi_run_20260711_064937.log): step11 ENOSPC 07:55,
            # step13 ENOSPC 08:09 — оба вошли на диск ~0. RAW_PREFREE (освобождал ~17 GB
            # через TRUNCATE big_analytics_direct/raw_leads/raw_calls/raw_domains) стоял
            # ПОСЛЕ step13 (L~1490 build_unified) — слишком поздно.
            # РАЗБОР ЗАВИСИМОСТЕЙ (что из RAW_PREFREE можно освободить РАНЬШЕ):
            #   • big_analytics_direct — step11 ЧИТАЕТ (step11.py L268/L284: FROM {T_DIRECT}
            #     — domain/campaign-бенчмарки) → НЕЛЬЗЯ трогать до step11.
            #   • raw_leads — step13 ЧИТАЕТ (step13.py L310: FROM {T_RAW_LEADS} — авто-лиды/
            #     Контекст-лиды) → НЕЛЬЗЯ трогать до step13.
            #   • raw_calls / raw_domains — НЕ читаются ни step11, ни step13, ни build_unified
            #     (grep: 0 вхождений FROM/JOIN) → БЕЗОПАСНО освободить здесь, до step11.
            # Это ЕДИНСТВЕННАЯ безопасная часть, что можно дать step11 (небольшой выигрыш;
            # основной хог — big_analytics_direct ~14 GB — step11 читает, освобождаем ПОСЛЕ него).
            try:
                _rpf11_conn = db_module.get_conn()
                try:
                    _rpf11_conn.rollback()
                    _rpf11_conn.autocommit = True
                    with _rpf11_conn.cursor() as _rpf11_cur:
                        for _rpf11_tbl in ('raw_calls', 'raw_domains'):
                            _rpf11_cur.execute("SELECT to_regclass(%s)", (f'public.{_rpf11_tbl}',))
                            if _rpf11_cur.fetchone()[0] is not None:
                                _rpf11_cur.execute(f'TRUNCATE TABLE public.{_rpf11_tbl}')
                                logger.info(
                                    'RAW_PREFREE_BEFORE_STEP11: TRUNCATE public.%s OK', _rpf11_tbl
                                )
                finally:
                    _rpf11_conn.autocommit = False
                    db_module.put_conn(_rpf11_conn)
            except Exception as _rpf11_e:
                logger.warning('RAW_PREFREE_BEFORE_STEP11: не критично, продолжаем: %s', _rpf11_e)

            # ── STEP11_DISK_GUARD_2026-07-11: диск виден ДО step11 (не ENOSPC в середине) ──
            # step11 сканирует big_analytics_direct (~14 GB, JOIN к pixel) + доливает ~188k
            # пиксель-строк в big_analytics_full → temp-спилл. big_analytics_direct освободить
            # ДО step11 нельзя (step11 его читает), поэтому это НЕ hard-exit, а ГРОМКИЙ сигнал:
            # если места мало — понятный WARNING+TG ДО падения; фактическую неполноту потом
            # ловит refresh-гейт в pipeline_powerbi.py (arrival непуст + пиксель_атрибуц есть).
            # Порог 8 GB — консервативный сигнальный (не блокирующий): при <8 GB риск ENOSPC высок.
            _STEP11_WARN_GB = 8.0
            try:
                import shutil as _s11_shutil
                _s11_free_gb = _s11_shutil.disk_usage('/').free / (1024 ** 3)
                if _s11_free_gb < _STEP11_WARN_GB:
                    logger.warning(
                        'STEP11_DISK_GUARD: мало диска перед step11 (%.1f GB < %.0f GB) — '
                        'риск ENOSPC в доливке пикселя; big_analytics_direct освободить нельзя '
                        '(step11 читает). Продолжаем попытку, неполноту поймает refresh-гейт.',
                        _s11_free_gb, _STEP11_WARN_GB,
                    )
                    _send_tg(
                        '⚠️ <b>big_analytics_v5</b>: мало диска перед step11 '
                        f'({_s11_free_gb:.1f} GB < {_STEP11_WARN_GB:.0f} GB) — риск неполного пикселя\n'
                        f'run_id: <code>{run_id}</code>'
                    )
                else:
                    logger.info('STEP11_DISK_GUARD: диска %.1f GB перед step11 — OK', _s11_free_gb)
            except Exception as _s11_disk_e:
                logger.warning('STEP11_DISK_GUARD: не удалось проверить диск: %s', _s11_disk_e)

            # step11: атрибуция pixel-заявок по источникам (после полной сборки full)
            try:
                step11_mod = importlib.import_module('step11_pixel_score.step11')
                s11_conn = db_module.get_conn()
                _t11 = time.perf_counter()
                try:
                    s11_res = step11_mod.run(s11_conn, run_id=run_id)
                    _s11_elapsed = time.perf_counter() - _t11
                    s11_rows = s11_res.get('rows', 0) if isinstance(s11_res, dict) else 0
                    s11_details = s11_res.get('details') if isinstance(s11_res, dict) else None
                    step_timings.append(('step11_pixel_score', 'step11_pixel_score', _s11_elapsed, True))
                    log_step(_log_conn, run_id, 'step11', 'ok',
                             rows_affected=s11_rows, duration_sec=round(_s11_elapsed, 2),
                             details=s11_details)
                    # ── EXPLAIN CAPTURE step11 ──────────────────────────────────
                    if _explain_log and _ec.EXPLAIN_CAPTURE:
                        try:
                            _esql11 = step11_mod.get_explain_sql(s11_conn)
                            _ec.wrap_explain(
                                s11_conn, _esql11, 'step11',
                                run_id=run_id, log_file=_explain_log, wall_sec=_s11_elapsed,
                            )
                        except Exception as _ce11:
                            logger.warning('explain_capture step11: %s', _ce11)
                finally:
                    db_module.put_conn(s11_conn)
            except Exception as e:
                # FAIL_ON_CRITICAL_STEP_2026-07-12: step11 доливает пиксель_атрибуц в
                # big_analytics_full — его провал = НЕПОЛНЫЕ данные. Раньше глушился в
                # warning → main() возвращал код 0 → refresh публиковал неполноту.
                # Теперь failed=True: exit-code и CLEANUP_ON_FAILURE реагируют корректно.
                logger.error('Ошибка в step11_pixel_score (критично): %s', e)
                log_step(_log_conn, run_id, 'step11', 'error', details=str(e))
                failed = True

            # ── RAW_PREFREE_AFTER_STEP11_2026-07-11: подстраховочный TRUNCATE (обычно no-op) ──
            # ВНИМАНИЕ (уточнено 2026-07-12): реальное освобождение ~15 GB под big_analytics_direct
            # уже произошло РАНЬШЕ — в хуке DIRECT_SLIM после step6 (L~970: строит slim из 8 колонок
            # и сразу `TRUNCATE big_analytics_direct`). step11 с тех пор читает УЗКИЙ
            # big_analytics_direct_slim, а не толстый direct. Поэтому здесь big_analytics_direct,
            # как правило, УЖЕ пуст → этот TRUNCATE — безопасный no-op (нечего освобождать).
            # Оставлен как подстраховка на случай, если DIRECT_SLIM-хук не отработал (best-effort,
            # ловит ошибку в ERROR но прогон не роняет). step13 его НЕ читает (маппинг спец идёт
            # из big_analytics_full — step13.py L693), build_unified читает только
            # big_analytics_full ∪ big_analytics_full_arrival. raw_leads НЕ трогаем — step13 его
            # читает (см. RAW_PREFREE_BEFORE_UNIFIED). Идемпотентно (to_regclass-guard).
            try:
                _rpf13_conn = db_module.get_conn()
                try:
                    _rpf13_conn.rollback()
                    _rpf13_conn.autocommit = True
                    with _rpf13_conn.cursor() as _rpf13_cur:
                        _rpf13_cur.execute("SELECT to_regclass('public.big_analytics_direct')")
                        if _rpf13_cur.fetchone()[0] is not None:
                            _rpf13_cur.execute('TRUNCATE TABLE public.big_analytics_direct')
                            logger.info(
                                'RAW_PREFREE_AFTER_STEP11: TRUNCATE public.big_analytics_direct OK '
                                '(подстраховочный no-op — реальное освобождение произошло в '
                                'DIRECT_SLIM после step6; здесь таблица уже пуста)'
                            )
                finally:
                    _rpf13_conn.autocommit = False
                    db_module.put_conn(_rpf13_conn)
            except Exception as _rpf13_e:
                logger.warning('RAW_PREFREE_AFTER_STEP11: не критично, продолжаем: %s', _rpf13_e)

            _t12_thr.join()
            _tcmc_thr.join()
            step_timings.append(('step12_proverka', 'step12_proverka',
                                  _s12_result.get('elapsed', 0), _s12_result.get('ok', False)))
            step_timings.append(('crm_mappings_check', 'crm_mappings_check',
                                  _cmc_result.get('elapsed', 0), _cmc_result.get('ok', False)))

            # ── SPEC_FALLBACK_V3 на big_analytics_full (до step13_rebuild) ──────
            # SPEC_FALLBACK_V3_2026-07-03: calls/пиксель/пиксель_атрибуц/crop_targeting
            # добавляются в big_analytics_full ПОСЛЕ corrections.apply() (step6/step10/step11)
            # и имеют NULL специалист. Заполняем ДО step13_rebuild — чтобы arrival
            # унаследовал корректный специалист из big_analytics_full.
            try:
                import corrections as _corr_v3f
                _v3f_conn = db_module.get_conn()
                try:
                    _v3f_n = _corr_v3f.apply_spec_fallback_v3(  # SPEC_FALLBACK_V3_2026-07-03
                        _v3f_conn, ['big_analytics_full'])
                    logger.info('spec_fallback_v3(full): %d строк', _v3f_n)
                finally:
                    db_module.put_conn(_v3f_conn)
            except Exception as e:
                logger.warning('Ошибка в spec_fallback_v3(full): %s', e)

            # ── ПЕРЕСБОРКА BFA (step13-rerun) ПОСЛЕ step11 ─────────────────────
            # FIX-PIXEL-DURABLE-PROD (2026-06-09): перенос проверенного фикса из
            # fast_pipeline.py в боевой pipeline.py (его зовёт pipeline_powerbi.py,
            # cron 04:00 UTC → собирает arrival для сайта).
            # ПРОБЛЕМА: единственный step13 идёт в упорядоченном цикле (step_num=13)
            # ДО блока доливки step11 → когда 'пиксель_атрибуц' ещё НЕ долит в
            # big_analytics_full, пиксель-визит ветка BFA читает пустоту (arrival ~32k
            # без пикселя). build_unified/build_star/refresh затем публикуют arrival
            # БЕЗ пикселя на сайт → срез 'пиксель'+По дате визита = 0/0.
            # ФИКС: здесь, ПОСЛЕ step11 (пиксель_атрибуц уже в BAF) и финализации BAF
            # (normalize/cleanup/prefix/compactify), повторно строим BFA — теперь
            # пиксель-визит ветка видит дробную атрибуцию и раскладывает её по реальной
            # eff_arrival_date (arrival ~94k). step13 идемпотентен (DROP+CREATE).
            # ВНУТРИ step13.run: ANALYZE big_analytics_full (FIX-ANALYZE) ПЕРЕД CTAS —
            # свежая статистика пиксель-строк step11 → план без 2ч-висяка.
            # raw_leads НЕ труncate-им (в отличие от fast_pipeline): в pipeline.py
            # raw_leads живёт до финального disk-cleanup (L~937 явно исключён) —
            # rebuild читает ЖИВУЮ raw_leads — ветка Контекст-лиды цела.
            # O5 (2026-06-09): это ЕДИНСТВЕННЫЙ step13 в pipeline.py (первый, в основном
            # цикле, убран). Здесь же — нормализация салонов arrival (перенесена из
            # блока normalize_salons выше: раньше нормализовалась устаревшая step13-версия,
            # затиравшаяся этим rebuild; теперь — на финальной arrival перед build_unified).
            # ── STEP13_DISK_GUARD_2026-07-11: диск виден ДО step13 (не ENOSPC в CTAS) ────
            # step13 пересобирает big_analytics_full_arrival (CTAS с FDW-джойнами
            # yandex_direct_manager_reports + raw_leads + big_analytics_full → temp-спилл).
            # Инцидент 2026-07-11: DROP прошёл, CREATE упал ENOSPC 08:09 → arrival=0 (пусто),
            # и refresh опубликовал пустоту. RAW_PREFREE_AFTER_STEP11 выше уже освободил
            # big_analytics_direct (~14 GB) → сюда входим с бо́льшим запасом. Гейт сигнальный:
            # мало диска → громкий WARNING+TG ДО падения; фактическую пустоту arrival всё
            # равно поймает refresh-гейт (pipeline_powerbi.py) и cleanup-гейт (L~2055).
            _STEP13_WARN_GB = 8.0
            try:
                import shutil as _s13_shutil
                _s13_free_gb = _s13_shutil.disk_usage('/').free / (1024 ** 3)
                if _s13_free_gb < _STEP13_WARN_GB:
                    logger.warning(
                        'STEP13_DISK_GUARD: мало диска перед step13 (%.1f GB < %.0f GB) — '
                        'риск ENOSPC в CTAS arrival; неполноту поймает refresh-гейт.',
                        _s13_free_gb, _STEP13_WARN_GB,
                    )
                    _send_tg(
                        '⚠️ <b>big_analytics_v5</b>: мало диска перед step13 '
                        f'({_s13_free_gb:.1f} GB < {_STEP13_WARN_GB:.0f} GB) — риск пустого arrival\n'
                        f'run_id: <code>{run_id}</code>'
                    )
                else:
                    logger.info('STEP13_DISK_GUARD: диска %.1f GB перед step13 — OK', _s13_free_gb)
            except Exception as _s13_disk_e:
                logger.warning('STEP13_DISK_GUARD: не удалось проверить диск: %s', _s13_disk_e)

            try:
                # SKIP_ON_FAILED_2026-07-12: если предыдущий критичный шаг (step11)
                # уронил failed — НЕ пересобираем arrival (иначе неполные данные доедут
                # до build_unified/build_star → durable-витрины). Пропуск, не работа.
                if failed:
                    raise _PriorFailureSkip()
                import importlib as _s13_il
                _s13_mod = _s13_il.import_module('step13_arrival.step13')
                s13_conn = db_module.get_conn()
                _t_s13 = time.perf_counter()
                try:
                    _s13_res = _s13_mod.run(s13_conn, run_id=run_id)
                    _s13_elapsed = time.perf_counter() - _t_s13
                    logger.info('step13_arrival (пересборка после step11): %s за %.1f сек',
                                _s13_res.get('details'), _s13_elapsed)
                    step_timings.append(('step13_rebuild', 'step13_rebuild', _s13_elapsed, True))
                    log_step(_log_conn, run_id, 'step13_rebuild', 'ok',
                             rows_affected=_s13_res.get('rows'),
                             duration_sec=round(_s13_elapsed, 2),
                             details=_s13_res.get('details'))
                    # ── EXPLAIN CAPTURE step13 ──────────────────────────────────
                    if _explain_log and _ec.EXPLAIN_CAPTURE:
                        try:
                            _esql13 = _s13_mod.get_explain_sql(s13_conn)
                            _ec.wrap_explain(
                                s13_conn, _esql13, 'step13_rebuild',
                                run_id=run_id, log_file=_explain_log, wall_sec=_s13_elapsed,
                            )
                        except Exception as _ce13:
                            logger.warning('explain_capture step13: %s', _ce13)
                finally:
                    db_module.put_conn(s13_conn)
            except _PriorFailureSkip:
                # SKIP_ON_FAILED_2026-07-12: предыдущий критичный шаг уже уронил failed.
                logger.error('SKIP_ON_FAILED: step13_rebuild ПРОПУЩЕН — предыдущий критичный '
                             'шаг уронил failed (run_id=%s); big_analytics_full_arrival '
                             'НЕ перезаписываем неполными данными', run_id)
                log_step(_log_conn, run_id, 'step13_rebuild', 'skipped',
                         details='пропущено из-за предыдущего сбоя (failed=True)')
            except Exception as e:
                # FAIL_ON_CRITICAL_STEP_2026-07-12: step13-rebuild строит финальную
                # big_analytics_full_arrival (ось «по дате визита» для сайта/PBI). Провал =
                # пустой/устаревший arrival → нельзя публиковать. Раньше warning → код 0.
                # Теперь failed=True + запись 'error' в data_quality_log (чтобы Telegram
                # показал конкретный шаг, а не «?»).
                logger.error('Ошибка в пересборке step13_arrival после step11 (критично): %s', e)
                log_step(_log_conn, run_id, 'step13_rebuild', 'error', details=str(e))
                failed = True

            # ── O5: normalize_salons на ФИНАЛЬНОЙ arrival (после rebuild, до unified) ──
            # build_unified читает big_analytics_full_arrival и требует нормализованный
            # "салон" (TREATAS в PBI по салону). Раньше нормализация arrival стояла ДО
            # rebuild и терялась — теперь применяется к финальной таблице.
            try:
                import corrections as _corr_arr
                _na_conn = db_module.get_conn()
                try:
                    _na = _corr_arr.normalize_salons(_na_conn, ['big_analytics_full_arrival'])
                    logger.info('normalize_salons(arrival, после rebuild): %d строк', _na)
                finally:
                    db_module.put_conn(_na_conn)
            except Exception as e:
                logger.warning('Ошибка в normalize_salons(arrival) после rebuild: %s', e)

            # ── SPEC_FALLBACK_V3 на arrival (после step13_rebuild) ───────────────
            # SPEC_FALLBACK_V3_2026-07-03: step13_rebuild строит строки direct/crop
            # по дате визита независимо от big_analytics_full → специалист NULL
            # (подтверждено: в big_analytics_full direct строк с NULL=0, но в
            # fact_big_analytics arrival-direct 617 строк с NULL после прогона).
            try:
                import corrections as _corr_v3a
                _v3a_conn = db_module.get_conn()
                try:
                    _v3a_n = _corr_v3a.apply_spec_fallback_v3(  # SPEC_FALLBACK_V3_2026-07-03
                        _v3a_conn, ['big_analytics_full_arrival'])
                    logger.info('spec_fallback_v3(arrival): %d строк', _v3a_n)
                finally:
                    db_module.put_conn(_v3a_conn)
            except Exception as e:
                logger.warning('Ошибка в spec_fallback_v3(arrival): %s', e)

            # ── DATE_FROM guard на ФИНАЛЬНОЙ arrival ─────────────────────────
            # cleanup_old_dates выше выполняется ДО step13_rebuild, поэтому
            # декабрьские строки по дате визита могли вернуться в
            # big_analytics_full_arrival и затем протечь в unified/PBI.
            try:
                from config.settings import DATE_FROM
                _ad_conn = db_module.get_conn()
                try:
                    with _ad_conn.cursor() as _ad:
                        _ad.execute(
                            'DELETE FROM public.big_analytics_full_arrival WHERE "Date" < %s',
                            (DATE_FROM,),
                        )
                        _ad_deleted = _ad.rowcount
                        _ad.execute(
                            'UPDATE public.big_analytics_full_arrival '
                            'SET week_start = %s WHERE week_start < %s',
                            (DATE_FROM, DATE_FROM),
                        )
                        _ad_week_fixed = _ad.rowcount
                    _ad_conn.commit()
                    if _ad_deleted or _ad_week_fixed:
                        logger.info(
                            'cleanup_old_dates(arrival после rebuild): удалено %d строк до %s, '
                            'week_start исправлен у %d строк',
                            _ad_deleted, DATE_FROM, _ad_week_fixed,
                        )
                    log_step(_log_conn, run_id, 'cleanup_old_dates_arrival', 'ok',
                             rows_affected=_ad_deleted + _ad_week_fixed)
                finally:
                    db_module.put_conn(_ad_conn)
            except Exception as e:
                logger.warning('Ошибка cleanup_old_dates(arrival после rebuild): %s', e)

            # ── campaign_code-метки для НЕ-tp источников (BA_CC_LABELS) ────────
            # Идемпотентный catch-all: проставляет осмысленные метки в campaign_code
            # для источников БЕЗ tp-схемы, чтобы в PBI-разрезе по campaign_code шли
            # ОТДЕЛЬНЫЕ строки SEO/Звонки/Отзывы/Посевы, а не NULL/lowercase-свалка.
            # Запускается ПОСЛЕ всех загрузчиков (step6 calls + load_reviews +
            # load_crop) и ДО build_unified/build_star — единая авторитетная точка,
            # покрывает все позиционные ветки источников без правки их INSERT-списков.
            # БЬЁТ СТРОГО по текущим NULL/'' или lowercase seo/звонки → НИКОГДА не
            # перезаписывает валидный tp-код (tp8/tp9/tp10 с направление=посевы исключены, т.к.
            # их _source_table='tp8'/'tp9'/'tp10', а источник<>'звонки'). golden Кудерко не двигается
            # (меняется только текстовая метка, ни одна мера/сумма не зависит от неё).
            # Применяется к ОБЕИМ осям атрибуции: big_analytics_full (по дате заявки)
            # И big_analytics_full_arrival (по дате визита) — обе вливаются в unified
            # (build_unified = full ∪ arrival), чтобы метка была единой на любой оси.
            try:
                _cc_conn = db_module.get_conn()
                _cc_total = 0
                try:
                    with _cc_conn.cursor() as _cc:
                        for _tbl in ('big_analytics_full', 'big_analytics_full_arrival'):
                            # SEO  ← _source_table='seo'
                            _cc.execute(
                                f"UPDATE {_tbl} SET campaign_code = 'SEO' "
                                "WHERE _source_table = 'seo' "
                                "  AND (campaign_code IS NULL OR campaign_code IN ('', 'seo'))"
                            )
                            _cc_total += _cc.rowcount
                            # Звонки  ← источник='звонки'
                            _cc.execute(
                                f"UPDATE {_tbl} SET campaign_code = 'Звонки' "
                                "WHERE \"источник\" = 'Звонки' "
                                "  AND (campaign_code IS NULL OR campaign_code IN ('', 'звонки', 'Звонки'))"
                            )
                            _cc_total += _cc.rowcount
                            # Отзывы  ← _source_table='direct_account_reviews'
                            _cc.execute(
                                f"UPDATE {_tbl} SET campaign_code = 'Отзывы' "
                                "WHERE _source_table = 'direct_account_reviews' "
                                "  AND (campaign_code IS NULL OR campaign_code = '')"
                            )
                            _cc_total += _cc.rowcount
                            # Посевы  ← _source_table in crop_targeting/telegram/social_посевы
                            _cc.execute(
                                f"UPDATE {_tbl} SET campaign_code = 'Посевы' "
                                "WHERE _source_table IN ('crop_targeting', 'telegram', 'social_посевы') "
                                "  AND (campaign_code IS NULL OR campaign_code = '')"
                            )
                            _cc_total += _cc.rowcount
                    _cc_conn.commit()
                    logger.info('campaign_code-метки (SEO/Звонки/Отзывы/Посевы): %d строк', _cc_total)
                    log_step(_log_conn, run_id, 'cc_labels', 'ok', rows_affected=_cc_total)
                finally:
                    db_module.put_conn(_cc_conn)
            except Exception as e:
                logger.warning('Ошибка в campaign_code-метках (не критично): %s', e)

            # RAW_PREFREE_BEFORE_UNIFIED_2026-06-27: финальный free ДО CTAS build_unified.
            # STAGED_PREFREE_2026-07-11: часть таблиц теперь освобождается РАНЬШЕ (дефицит диска):
            #   • raw_calls/raw_domains — уже обнулены RAW_PREFREE_BEFORE_STEP11 (здесь no-op).
            #   • big_analytics_direct — уже обнулён RAW_PREFREE_AFTER_STEP11 (здесь no-op).
            #   • raw_leads — step13 читал его (FROM {T_RAW_LEADS}), поэтому обнуляется ИМЕННО
            #     здесь, ПОСЛЕ step13 rebuild — единственная таблица, реально освобождаемая тут.
            # build_unified читает ТОЛЬКО big_analytics_full + big_analytics_full_arrival.
            # raw_yandex: уже освобождён RAW_YANDEX_PREFREE после step3. Идемпотентно (to_regclass).
            try:
                _rpf_conn = db_module.get_conn()
                try:
                    _rpf_conn.rollback()
                    _rpf_conn.autocommit = True
                    with _rpf_conn.cursor() as _rpf_cur:
                        for _rpf_tbl in ('big_analytics_direct', 'raw_leads', 'raw_calls', 'raw_domains'):
                            _rpf_cur.execute(
                                "SELECT to_regclass(%s)", (f'public.{_rpf_tbl}',)
                            )
                            if _rpf_cur.fetchone()[0] is not None:
                                _rpf_cur.execute(f'TRUNCATE TABLE public.{_rpf_tbl}')
                                logger.info(
                                    'RAW_PREFREE_BEFORE_UNIFIED: TRUNCATE public.%s OK', _rpf_tbl
                                )
                finally:
                    _rpf_conn.autocommit = False
                    db_module.put_conn(_rpf_conn)
            except Exception as _rpf_e:
                logger.warning('RAW_PREFREE_BEFORE_UNIFIED: не критично, продолжаем: %s', _rpf_e)

            # ── big_analytics_unified (MIRROR+UNION финальный шаг) ─────────────
            # Собирается ПОСЛЕ step11 (пиксель_атрибуц уже в BAF), финализации BAF
            # (normalize/cleanup/prefix/compactify) и BFA (step13)
            # → unified = big_analytics_full ∪ big_analytics_full_arrival + атрибуция.
            # Идемпотентно (DROP+CTAS). PBI читает unified как партицию big_analytics_full.
            try:
                # SKIP_ON_FAILED_2026-07-12: если предыдущий критичный шаг уронил failed —
                # НЕ собираем unified (иначе star соберётся на неполном/устаревшем unified
                # и опубликуется в durable fact_big_analytics). Пропуск, не работа.
                if failed:
                    raise _PriorFailureSkip()
                _uni_mod = importlib.import_module('step13_arrival.build_unified')
                uni_conn = db_module.get_conn()
                _t_uni = time.perf_counter()
                try:
                    _uni_res = _uni_mod.run(uni_conn, run_id=run_id)
                    _uni_elapsed = time.perf_counter() - _t_uni
                    logger.info('build_unified: %s за %.1f сек',
                                _uni_res.get('details'), _uni_elapsed)
                    step_timings.append(('build_unified', 'build_unified', _uni_elapsed, True))
                    log_step(_log_conn, run_id, 'build_unified', 'ok',
                             rows_affected=_uni_res.get('rows'),
                             duration_sec=round(_uni_elapsed, 2),
                             details=_uni_res.get('details'))
                    # ── EXPLAIN CAPTURE build_unified ───────────────────────────
                    if _explain_log and _ec.EXPLAIN_CAPTURE:
                        try:
                            _esql_uni = _uni_mod.get_explain_sql(uni_conn)
                            _ec.wrap_explain(
                                uni_conn, _esql_uni, 'build_unified',
                                run_id=run_id, log_file=_explain_log, wall_sec=_uni_elapsed,
                            )
                        except Exception as _ce_uni:
                            logger.warning('explain_capture build_unified: %s', _ce_uni)
                finally:
                    db_module.put_conn(uni_conn)
            except _PriorFailureSkip:
                # SKIP_ON_FAILED_2026-07-12: предыдущий критичный шаг уже уронил failed.
                logger.error('SKIP_ON_FAILED: build_unified ПРОПУЩЕН — предыдущий критичный '
                             'шаг уронил failed (run_id=%s); big_analytics_unified '
                             'НЕ перезаписываем неполными данными', run_id)
                log_step(_log_conn, run_id, 'build_unified', 'skipped',
                         details='пропущено из-за предыдущего сбоя (failed=True)')
            except Exception as e:
                # FAIL_ON_CRITICAL_STEP_2026-07-12: build_unified собирает
                # big_analytics_unified (источник star.fact_big_analytics и golden-сверки).
                # Провал = star соберётся на неполном/устаревшем unified. Раньше warning →
                # код 0. Теперь failed=True + запись 'error' для Telegram.
                logger.error('Ошибка в build_unified (критично): %s', e)
                log_step(_log_conn, run_id, 'build_unified', 'error', details=str(e))
                failed = True

            # ── PORT_FROM_FAST_2026-06-19: SPEND_PREFREE ────────────────────────
            # Освобождение диска ПЕРЕД spend-фазой. Механика: три спенд-CTAS читают
            # FDW (yandex_direct_manager_reports, 19M строк) с temp-spill в pgsql_tmp.
            # При наличии big_analytics_direct (~14 GB) + raw_yandex (~8 GB) на диске
            # суммарно ~22-28 GB занято → диска под temp-spill почти нет → "No space
            # left on device" (инцидент pipeline_powerbi, причина disk-full крона).
            # SNAPSHOT_ON_UNIFIED_2026-06-20: после build_unified big_analytics_full НЕ нужна
            # ни одному последующему шагу. Читатели big_analytics_full ПОСЛЕ этой точки:
            #   build_star → читает big_analytics_unified (не full) ✓
            #   pipeline_log_snapshot → переключён на big_analytics_unified ✓
            #   funnel_drift_snapshot → переключён на big_analytics_unified ✓
            #   step8 (control-суммы) → читает big_analytics_full только через T_FULL константу
            #     в step8.py — step8 идёт ПОСЛЕДНИМ, к тому моменту full уже пустая;
            #     контрольный скан big_analytics_full вернёт 0 строк (направление=None, cost=0).
            #     step8 сверяет расход через fact_big_analytics (durable) — не через full. ✓
            #   verify/golden → читает big_analytics_unified ✓
            # EARLY_TRUNCATE_DIRECT_RAW_2026-06-17 (L~1629) делает повторный TRUNCATE
            # прямых таблиц после step8 — к тому моменту уже пустые, повторный TRUNCATE
            # безопасен (no-op по строкам, минимальный overhead).
            # Синхрон: fast_pipeline.py L902-940 (SPEND_PREFREE_2026-06-18).
            try:
                _spf_conn = db_module.get_conn()
                try:
                    _spf_conn.rollback()
                    _spf_conn.autocommit = True
                    with _spf_conn.cursor() as _spf_cur:
                        # SPEND_PREFREE_REVERT_2026-06-20: big_analytics_full УБРАНА из списка.
                        # Добавление big_analytics_full в SPEND_PREFREE_FULL_2026-06-20 создало
                        # дефект A: step8 (_collect_final_stats) читает T_FULL (big_analytics_full)
                        # ПОСЛЕ spend-фазы → полный TRUNCATE → step8 получал 0 строк вместо
                        # реальных ~3.9M (все счётчики rows_full/total_leads/total_cost = 0).
                        # funnel_drift_snapshot и pipeline_log_snapshot переключены на
                        # big_analytics_unified (SNAPSHOT_ON_UNIFIED_2026-06-20) — им full не нужна.
                        # Но step8._collect_final_stats пишет в Telegram-отчёт из T_FULL — нужна живой.
                        # Решение диска: disk-guard перед параллелью поднят до 25 GB
                        # (SPEND_PARALLEL_DISKGUARD_2026-06-20 ниже). При <25 GB диска → sequential
                        # (один спенд за раз, max ~7 GB temp-spill вместо 21 GB) → вписывается.
                        # big_analytics_full_arrival оставляем — нужна build_unified (уже завершён).
                        for _spf_tbl in ('big_analytics_direct', 'raw_yandex'):
                            _spf_cur.execute(
                                "SELECT to_regclass(%s)", (f'public.{_spf_tbl}',)
                            )
                            if _spf_cur.fetchone()[0] is not None:
                                _spf_cur.execute(f'TRUNCATE TABLE public.{_spf_tbl}')
                                logger.info(
                                    'SPEND_PREFREE: TRUNCATE public.%s перед spend-фазой', _spf_tbl
                                )
                finally:
                    _spf_conn.autocommit = False
                    db_module.put_conn(_spf_conn)
            except Exception as _spf_e:
                logger.warning('SPEND_PREFREE: не критично, продолжаем: %s', _spf_e)

            # SPEND_NIGHT_JOB_2026-06-27: fact_region_spend / fact_adformat_spend /
            # fact_criterion_spend вынесены в step_cron_night/build_spend_daily.py.
            # Дневной pipeline их НЕ строит — они обновляются дневным job (14:00 Екб).
            # SPEND_PREFREE выше (TRUNCATE direct + raw_yandex) сохранён — освобождает
            # ~22 GB перед build_region_zayavki и build_star.
            logger.info(
                'SPEND_NIGHT_JOB_2026-06-27: spend-витрины пропущены в дневном pipeline; '
                'обновляются отдельным job build_spend_daily.py (14:00 Екб / 09:00 UTC)'
            )

            # ── build_region_zayavki — датамарт «воронка по заявкам в разрезе региона» ─
            # ПОСЛЕ build_criterion_spend, ДО build_star. Отдельная таблица
            # public.fact_region_zayavki (DROP+CTAS) из public.local_leads_all по грани
            # created_date×campaign_id×id_location (geoid из utm_content) — golden Кудерко
            # НЕ затрагивает (другая таблица). Контракт run(conn, run_id) как у build_unified.
            try:
                _rz_mod = importlib.import_module('region_spend.build_region_zayavki')
                _rz_conn = db_module.get_conn()
                _t_rz = time.perf_counter()
                try:
                    _rz_res = _rz_mod.run(_rz_conn, run_id=run_id)
                    _rz_elapsed = time.perf_counter() - _t_rz
                    logger.info('build_region_zayavki: %s за %.1f сек',
                                _rz_res.get('details'), _rz_elapsed)
                    step_timings.append(('build_region_zayavki', 'build_region_zayavki',
                                         _rz_elapsed, True))
                    log_step(_log_conn, run_id, 'build_region_zayavki', 'ok',
                             rows_affected=_rz_res.get('rows'),
                             duration_sec=round(_rz_elapsed, 2),
                             details=_rz_res.get('details'))
                finally:
                    db_module.put_conn(_rz_conn)
            except Exception as e:
                logger.warning('Ошибка в build_region_zayavki: %s', e)

            # ── build_criterion_zayavki — датамарт «воронка по заявкам в разрезе критерия» ─
            # ПОСЛЕ build_region_zayavki, ДО build_star. Отдельная таблица
            # public.fact_criterion_zayavki (DROP+CTAS) из public.local_leads_all по грани
            # created_date×campaign_id×criterion (нормализованный utm_term) — golden Кудерко
            # НЕ затрагивает (другая таблица). Контракт run(conn, run_id) как у build_unified.
            try:
                _cz_mod = importlib.import_module('criterion_spend.build_criterion_zayavki')
                _cz_conn = db_module.get_conn()
                _t_cz = time.perf_counter()
                try:
                    _cz_res = _cz_mod.run(_cz_conn, run_id=run_id)
                    _cz_elapsed = time.perf_counter() - _t_cz
                    logger.info('build_criterion_zayavki: %s за %.1f сек',
                                _cz_res.get('details'), _cz_elapsed)
                    step_timings.append(('build_criterion_zayavki', 'build_criterion_zayavki',
                                         _cz_elapsed, True))
                    log_step(_log_conn, run_id, 'build_criterion_zayavki', 'ok',
                             rows_affected=_cz_res.get('rows'),
                             duration_sec=round(_cz_elapsed, 2),
                             details=_cz_res.get('details'))
                finally:
                    db_module.put_conn(_cz_conn)
            except Exception as e:
                logger.warning('Ошибка в build_criterion_zayavki: %s', e)

            # ── build_dim_criterion — измерение критерия для Power BI ──────────
            # DIM_CRITERION_2026-06-29: UNION fact_criterion_spend + fact_criterion_zayavki
            # по полю criterion (очищенный текст). Даёт общую ось 1:* к обеим таблицам
            # фактов — пользователь протягивает связи в Power BI Desktop. Golden НЕ затрагивает.
            try:
                _dc_mod = importlib.import_module('criterion_spend.build_dim_criterion')
                _dc_conn = db_module.get_conn()
                _t_dc = time.perf_counter()
                try:
                    _dc_res = _dc_mod.run(_dc_conn, run_id=run_id)
                    _dc_elapsed = time.perf_counter() - _t_dc
                    logger.info('build_dim_criterion: %s за %.1f сек',
                                _dc_res.get('details'), _dc_elapsed)
                    step_timings.append(('build_dim_criterion', 'build_dim_criterion',
                                         _dc_elapsed, True))
                    log_step(_log_conn, run_id, 'build_dim_criterion', 'ok',
                             rows_affected=_dc_res.get('rows'),
                             duration_sec=round(_dc_elapsed, 2),
                             details=_dc_res.get('details'))
                finally:
                    db_module.put_conn(_dc_conn)
            except Exception as e:
                logger.warning('Ошибка в build_dim_criterion: %s', e)

            # ── build_star — пересборка star-схемы (Этап 1 миграции на звезду) ──
            # ПОСЛЕ build_unified: star.fact_big_analytics — проекция big_analytics_unified,
            # star.arp_fact — проекция analytics_report_placement. Запускаем отдельным
            # процессом (subprocess), т.к. build_star.py — самостоятельный скрипт с
            # собственным соединением. PBI Этапа 3 репойнтит партиции на эти star-таблицы.
            try:
                # SKIP_ON_FAILED_2026-07-12: если предыдущий критичный шаг уронил failed —
                # НЕ пересобираем star. build_star (subprocess DROP+CTAS) КОММИТИТ в durable
                # public.fact_big_analytics НЕМЕДЛЕННО, до sys.exit(1) в конце main() → живые
                # потребители (leads_api /cpl, golden_reward, PBI) получили бы неполноту.
                # Пропуск, не работа (durable star остаётся от предыдущего корректного прогона).
                if failed:
                    raise _PriorFailureSkip()
                import subprocess as _sp, sys as _sys, os as _os
                _star_conn = db_module.get_conn()
                try:
                    log_step(_star_conn, run_id, 'build_star', 'start')
                finally:
                    db_module.put_conn(_star_conn)
                _t_star = time.perf_counter()
                _sp.run(
                    [_sys.executable,
                     _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                                   'star_refactor', 'build_star.py')],
                    check=True,
                )
                _star_elapsed = time.perf_counter() - _t_star
                logger.info('build_star: готово за %.1f сек', _star_elapsed)
                step_timings.append(('build_star', 'build_star', _star_elapsed, True))
                _star_conn = db_module.get_conn()
                try:
                    log_step(_star_conn, run_id, 'build_star', 'ok',
                             duration_sec=round(_star_elapsed, 2))
                finally:
                    db_module.put_conn(_star_conn)
            except _PriorFailureSkip:
                # SKIP_ON_FAILED_2026-07-12: предыдущий критичный шаг уже уронил failed.
                logger.error('SKIP_ON_FAILED: build_star ПРОПУЩЕН — предыдущий критичный '
                             'шаг уронил failed (run_id=%s); durable fact_big_analytics '
                             'НЕ перезаписываем неполными данными', run_id)
                try:
                    _bs_sk = db_module.get_conn()
                    try:
                        log_step(_bs_sk, run_id, 'build_star', 'skipped',
                                 details='пропущено из-за предыдущего сбоя (failed=True)')
                    finally:
                        db_module.put_conn(_bs_sk)
                except Exception:
                    pass
            except Exception as e:
                # FAIL_ON_CRITICAL_STEP_2026-07-12: build_star (subprocess check=True →
                # CalledProcessError сюда) пересобирает durable star.fact_big_analytics —
                # ту самую витрину, что читает refresh-гейт (Bug 2 fix) и PBI. Провал =
                # star не пересобран/неполон. Раньше warning → код 0 → refresh мог
                # опубликовать устаревшую star. Теперь failed=True + запись 'error'.
                logger.error('Ошибка в build_star (критично): %s', e)
                try:
                    _bs_lc = db_module.get_conn()
                    try:
                        log_step(_bs_lc, run_id, 'build_star', 'error', details=str(e))
                    finally:
                        db_module.put_conn(_bs_lc)
                except Exception:
                    pass
                failed = True

            # step13_utm_direct_audit — перенесено в pipeline_night.py
            # (выполняется ночью в 03:00 МСК, ~2 ч с квотой Метрики).
            # Дневной запуск был дублированием: после ночи данные UTM-аудита
            # в check_utm/check_utm_fuck_direct уже свежие на сегодняшнюю дату.
            # Для ручного запуска: ~/venv/bin/python3 step_cron_night/step13_utm_direct_audit/run.py
        finally:
            db_module.put_conn(_log_conn)

    # ── pipeline_log_snapshot — снимок воронки по месяцам в data_pipeline_log ──
    # Идёт ПЕРЕД step8: big_analytics_full уже финализирован (step7 VACUUM),
    # значит агрегаты по месяцам отражают итоговое состояние данного run_id.
    # Используется дашбордом /api/pipeline-delta (subagent) для сравнения
    # текущего run vs предыдущего.
    if not failed:
        try:
            snap_mod = importlib.import_module('step8_stats.pipeline_log_snapshot')
            _sn_conn = db_module.get_conn()
            _t_snap = time.perf_counter()
            try:
                _sn_res = snap_mod.run(_sn_conn, run_id=run_id)
                _sn_elapsed = time.perf_counter() - _t_snap
                _sn_rows = _sn_res.get('rows', 0) if isinstance(_sn_res, dict) else 0
                step_timings.append(('pipeline_log_snapshot',
                                     'pipeline_log_snapshot',
                                     _sn_elapsed, True))
                _log_conn2 = db_module.get_conn()
                try:
                    log_step(_log_conn2, run_id, 'pipeline_log_snapshot', 'ok',
                             rows_affected=_sn_rows,
                             duration_sec=round(_sn_elapsed, 2),
                             details=f"months={_sn_res.get('months') if isinstance(_sn_res, dict) else '?'}")
                finally:
                    db_module.put_conn(_log_conn2)
            finally:
                db_module.put_conn(_sn_conn)
        except Exception as e:
            logger.warning('Ошибка в pipeline_log_snapshot: %s', e)
            try:
                _log_conn2 = db_module.get_conn()
                try:
                    log_step(_log_conn2, run_id, 'pipeline_log_snapshot',
                             'error', details=str(e))
                finally:
                    db_module.put_conn(_log_conn2)
            except Exception:
                pass

    # ── funnel_drift_snapshot — снимок воронки (month × источник) + алерт дрейфа ──
    # Идёт после pipeline_log_snapshot и ПЕРЕД step8 (step8 — финальный, после него —
    # только verify/golden-лог). big_analytics_full финализирован (step7 VACUUM).
    # Append-only (ON CONFLICT DO NOTHING) → safe для повторных запусков.
    if not failed:
        try:
            _fd_mod = importlib.import_module('step8_stats.funnel_drift_snapshot')
            _fd_conn = db_module.get_conn()
            _t_fd = time.perf_counter()
            try:
                _fd_res = _fd_mod.run(_fd_conn, run_id=run_id, alert=True)
                _fd_elapsed = time.perf_counter() - _t_fd
                _fd_rows = _fd_res.get('rows', 0) if isinstance(_fd_res, dict) else 0
                step_timings.append(('funnel_drift_snapshot',
                                     'funnel_drift_snapshot',
                                     _fd_elapsed, True))
                _log_conn_fd = db_module.get_conn()
                try:
                    log_step(_log_conn_fd, run_id, 'funnel_drift_snapshot', 'ok',
                             rows_affected=_fd_rows,
                             duration_sec=round(_fd_elapsed, 2),
                             details=(
                                 f"months={_fd_res.get('months', '?')},"
                                 f"sources={_fd_res.get('sources', '?')},"
                                 f"alert_rows={_fd_res.get('alert_rows', 0)}"
                             ))
                finally:
                    db_module.put_conn(_log_conn_fd)
            finally:
                db_module.put_conn(_fd_conn)
        except Exception as e:
            logger.exception('Ошибка в funnel_drift_snapshot: %s', e)
            try:
                _log_conn_fd = db_module.get_conn()
                try:
                    log_step(_log_conn_fd, run_id, 'funnel_drift_snapshot',
                             'error', details=str(e))
                finally:
                    db_module.put_conn(_log_conn_fd)
            except Exception:
                pass

    # ── step8 (статистика + Telegram) — выполняется ПОСЛЕДНИМ ───────────────
    # Чтобы все шаги (load_reviews, 404_errors, normalize_salons, cleanup_old_dates,
    # sync_pixel_config) попали в финальный отчёт через data_quality_log.
    if not failed and (args.only_step is None or args.only_step == 8):
        s8_num, s8_name, s8_module, _ = STEP8_INFO
        # PORT_FROM_FAST_2026-06-19: передаём фактическое wall-clock время прогона
        # в step8, чтобы блок «Время выполнения» показывал реальное время (не сумму
        # шагов). После переноса параллельных спендов сумма шагов > wall (3×spend
        # считаются суммарно). Синхрон с fast_pipeline.py L1141 (WALLTIME_FIX_2026-06-18).
        _pipeline_wall_sec_pl = (datetime.now() - started_at).total_seconds()
        ok, elapsed = run_step(s8_num, s8_module, run_id,
                               pipeline_wall_sec=_pipeline_wall_sec_pl)
        step_timings.append((s8_num, s8_name, elapsed, ok))
        if not ok:
            failed = True

    # ── EARLY_TRUNCATE_DIRECT_RAW_2026-06-17 ─────────────────────────────────
    # big_analytics_direct (~14 GB) и raw_yandex (~3-4 GB) освобождаются ЗДЕСЬ —
    # сразу после step8, который последним их читает. Точки чтения после step8:
    #   verify/golden-лог — big_analytics_unified (не direct/raw_yandex) ✓
    #   build_star/build_unified — уже завершены выше ✓
    #   спенд-билдеры (region/adformat/criterion) — только yandex_direct_manager_reports ✓
    #   step9 (background) — yandex_direct_history, не raw_yandex/direct ✓
    # Читает до этой точки: step8 (T_DIRECT — сверка расходов L427-447; raw_yandex — EXISTS L144).
    # Из финального _cleanup_tables эти две таблицы убраны — нет двойного TRUNCATE.
    if not failed and args.only_step is None:
        try:
            _et_conn = db_module.get_conn()
            try:
                _et_conn.rollback()
                _et_conn.autocommit = True
                with _et_conn.cursor() as _et_cur:
                    for _et_tbl in ('big_analytics_direct', 'raw_yandex'):
                        _et_cur.execute("SELECT to_regclass(%s)", (f'public.{_et_tbl}',))
                        if _et_cur.fetchone()[0] is not None:
                            _et_cur.execute(f'TRUNCATE TABLE public.{_et_tbl}')
                            logger.info('EARLY_TRUNCATE: освобождена public.%s', _et_tbl)
            finally:
                _et_conn.autocommit = False
                db_module.put_conn(_et_conn)
        except Exception as _et_e:
            logger.warning('EARLY_TRUNCATE_DIRECT_RAW: не критично, продолжаем: %s', _et_e)

    # ── verify_big_analytics — проверки качества после финализации ────────────
    if not failed and args.only_step is None:
        _vba_script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   'data_check', 'verify_big_analytics.py')
        _t_vba = time.perf_counter()
        try:
            subprocess.run(
                [sys.executable, _vba_script, '--tg', '--pipeline', 'pipeline'],
                check=True, timeout=120,
            )  # PIPELINE_DIGEST_2026-06-17: --pipeline → заголовок дайджеста
            _vba_elapsed = time.perf_counter() - _t_vba
            logger.info('verify_big_analytics: завершён за %.1f сек', _vba_elapsed)
            step_timings.append(('verify_big_analytics', 'verify_big_analytics', _vba_elapsed, True))
        except subprocess.CalledProcessError as _e:
            _vba_elapsed = time.perf_counter() - _t_vba
            logger.warning('verify_big_analytics: проверки не прошли (код %d)', _e.returncode)
            step_timings.append(('verify_big_analytics', 'verify_big_analytics', _vba_elapsed, False))
        except Exception as _e:
            logger.warning('verify_big_analytics: ошибка запуска: %s', _e)

    # ── golden-лог Кудерко в data_quality_log (МЕЖДУ verify и cleanup) ─────────
    # cleanup_intermediate (L~1092) TRUNCATE-ит big_analytics_unified → постфактум
    # golden-сверка по unified станет невозможна (unified=0). Поэтому СНИМАЕМ golden-
    # числа Кудерко ЗДЕСЬ, пока unified ещё жив, и пишем их в data_quality_log —
    # персистентный след для последующей проверки/аудита (методика GOLDEN_BASELINE.md:
    # unified, специалист='Кудерко Семен', По дате заявки, БЕЗ фильтра по Date, источники
    # Я.Директ без пикселя; эталон расход 25422774.00 ±15 ₽ / продажи floor≥54).
    # Допуск расширен 2026-06-17: ±15 ₽ (было ±10) — пиксельный дрейф by-design +12.20.
    # Датный фильтр Date убран 2026-06-17: golden считается БЕЗ фильтра по Date
    # (GOLDEN_BASELINE.md §5) — с фильтром 01-01..06-04 давало 53 продажи вместо 54.
    # Дробный пиксель НЕ затрагивается: SUM(...) долей, ROUND только итога. Read-only.
    if not failed and args.only_step is None:
        try:
            _gld_conn = db_module.get_conn()
            try:
                with _gld_conn.cursor() as _gc:
                    _gc.execute("""
                        SELECT ROUND(SUM(total_cost)::numeric, 2),
                               ROUND(SUM(prodazhi))
                        FROM public.big_analytics_unified
                        WHERE "специалист" = 'Кудерко Семен'
                          AND "атрибуция" = 'По дате заявки'
                          AND _source_table IN ('direct','tp8','tp9','tp10','seo','calls','direct_unmatched','direct_zero')
                    """)
                    _grow = _gc.fetchone()
                _gld_conn.rollback()
                _g_rashod = _grow[0] if _grow else None
                _g_prod   = _grow[1] if _grow else None
                # Допуск по расходу ±100 ₽ (расширен 2026-06-17; консистентность с verify_big_analytics.py
                # GOLDEN_COST_TOL=100; пиксельный дрейф by-design +12 ₽, запас на CRM-дозревание.
                # GOLDEN_BASELINE.md. REF 25422774.00 неизменён).
                # продажи — растущая метрика (дозревание CRM), floor а не exact; 2026-06-15
                _G_RASHOD_REF, _G_PROD_REF, _G_RASHOD_TOL = 25422774.00, 54, 100.00
                _rashod_ok = (_g_rashod is not None
                              and abs(float(_g_rashod) - _G_RASHOD_REF) <= _G_RASHOD_TOL)
                _prod_ok = (_g_prod is not None and int(_g_prod) >= _G_PROD_REF)
                _gld_status = 'ok' if (_rashod_ok and _prod_ok) else 'fail'
                _gld_details = (
                    f'golden Кудерко (unified, По дате заявки): '
                    f'расход={_g_rashod} (эталон {_G_RASHOD_REF}±{_G_RASHOD_TOL}, '
                    f'{"OK" if _rashod_ok else "FAIL"}); '
                    f'продажи={_g_prod} (floor>={_G_PROD_REF}, '
                    f'{"OK" if _prod_ok else "FAIL"})'
                )
                logger.info('golden_kuderko: %s', _gld_details)
                log_step(_gld_conn, run_id, 'golden_kuderko', _gld_status,
                         rows_affected=int(_g_prod) if _g_prod is not None else None,
                         details=_gld_details)
            finally:
                db_module.put_conn(_gld_conn)
        except Exception as _ge:
            logger.warning('golden_kuderko: не удалось снять golden-числа: %s', _ge)

    elapsed_total = (datetime.now() - started_at).total_seconds()

    # Итоговая таблица времён
    logger.info('=' * 60)
    logger.info('%-4s  %-28s  %8s  %s', 'Шаг', 'Название', 'Секунды', 'Статус')
    logger.info('-' * 60)
    for s_num, s_name, s_elapsed, s_ok in step_timings:
        status = 'OK' if s_ok else 'ОШИБКА'
        logger.info('%-8s  %-28s  %7.1f с  %s', str(s_num), s_name, s_elapsed, status)
    logger.info('=' * 60)

    # Telegram-уведомление об ошибке (до закрытия пулов — нужен conn для data_quality_log)
    if failed:
        failed_step = '?'
        err_text = ''
        try:
            _c = db_module.get_conn()
            try:
                with _c.cursor() as _cur:
                    _cur.execute(f"""
                        SELECT step, details FROM {T_DATA_QUALITY_LOG}
                        WHERE run_id=%s AND status='error'
                        ORDER BY id DESC LIMIT 1
                    """, (run_id,))
                    _r = _cur.fetchone()
                    if _r:
                        failed_step = _r[0] or '?'
                        err_text = (_r[1] or '')[:500]
            finally:
                db_module.put_conn(_c)
        except Exception:
            pass
        _send_tg(
            f'❌ <b>big_analytics_v5 ОШИБКА</b>\n'
            f'<code>{datetime.now().strftime("%d.%m.%Y %H:%M")}</code>  '
            f'run_id: <code>{run_id}</code>\n'
            f'Упал на шаге: <b>{failed_step}</b>\n'
            f'<code>{err_text}</code>'
        )

    # Освобождение диска: TRUNCATE промежуточных таблиц после успешного запуска.
    # big_analytics_full_arrival НЕ трогаем — PBI читает его напрямую из public
    # (PBI_TABLES.md: big_analytics_full_arrival → public.big_analytics_full_arrival).
    #
    # МЕРА №5 (2026-06-09, освобождение ~12 GB между прогонами): big_analytics_full и
    # big_analytics_unified ДОБАВЛЕНЫ в TRUNCATE. Основание: cutover на star-схему
    # состоялся — PBI читает star.fact_big_analytics, а full/unified (public) PBI
    # больше НЕ читает (PBI_TABLES.md L113, проверено 2026-06-08). Это промежуточные
    # build-таблицы: full собирается step6, unified собирается build_unified, star.fact
    # строится из unified в build_star. К этой точке (самый конец pipeline.py) ВСЕ их
    # потребители УЖЕ отработали ВЫШЕ по коду:
    #   • build_unified (unified ← full)          — L~904-921
    #   • build_star    (star.fact ← unified)     — L~923-952  (subprocess, check=True)
    #   • verify_big_analytics --tg               — L~1011-1027
    #       └─ golden Кудерко (блок 1) читает big_analytics_unified;
    #          воронка/расход/продажи читают big_analytics_full.
    # Поэтому TRUNCATE full/unified ЗДЕСЬ безопасен — данные уже спроецированы в star
    # и сверены golden-проверкой. Следующий прогон пересоберёт обе с нуля (DROP/CTAS).
    #
    # ⚠️ ЗАЩИТНАЯ ПРОВЕРКА (guard): весь cleanup-блок выполняется ТОЛЬКО при `not failed`
    # (пайплайн дошёл до конца без сбоя — verify+build_star гарантированно отработали)
    # И только если ОБЕ финальные таблицы (big_analytics_full + big_analytics_full_arrival)
    # существуют и непустые на момент проверки (проверка идёт ДО любого TRUNCATE ниже).
    # Если прогон сорвался раньше или финальные не собрались — НИЧЕГО не truncate-им.
    # DISK_CLEANUP_RAW_SIZE_LOG_2026-06-17
    # Освобождение диска: TRUNCATE промежуточных таблиц ТОЛЬКО при успешном прогоне.
    # При failed=True — пропускаем: данные нужны для разбора сбоя.
    #
    # Таблицы к очистке и обоснование безопасности:
    #   big_analytics_direct/seo/reviews/pixel/crop_targeting — пересоздаются step3/6
    #     каждый прогон (DROP+CREATE UNLOGGED); к этой точке все downstream (step6/7/8/
    #     step11/step13/build_unified/build_star/verify) уже завершены.
    #   pixel_leads / pixel_leads_check — промежуточные таблицы step5/step11, не читаются
    #     PBI и не нужны между прогонами.
    #   big_analytics_full — собирается step6 каждый прогон; к финалу уже спроецирован
    #     в big_analytics_unified → fact_big_analytics (star) → PBI читает star.
    #   big_analytics_unified — собирается build_unified каждый прогон; к финалу
    #     golden-проверка (verify_big_analytics) и build_star уже завершены.
    #   raw_yandex / raw_leads / raw_calls / raw_domains — пересоздаются step1 каждый
    #     прогон (DROP+CREATE UNLOGGED); к финалу pipeline.py все шаги (step6/7/8/9/10/
    #     11/13/build_unified/build_star/verify) их уже не читают. Добавлены 2026-06-17
    #     после инцидента «No space left on device» (диск 154/164 GB): raw_* держали
    #     несколько GB между прогонами, step3 INSERT падал на pgsql_tmp.
    #   НЕ трогаем: big_analytics_full_arrival (PBI читает напрямую), local_*/campaign_status
    #     (живут между прогонами), fact_big_analytics / star.* (финальные витрины PBI).
    #
    # ЗАЩИТНАЯ ПРОВЕРКА: весь блок выполняется ТОЛЬКО если ОБЕ финальные таблицы
    # (big_analytics_full + big_analytics_full_arrival) существуют и непустые.
    if not failed:
        _final_tables = ['big_analytics_full', 'big_analytics_full_arrival']
        _cleanup_tables = [
            # big_analytics_direct и raw_yandex перенесены в EARLY_TRUNCATE_DIRECT_RAW_2026-06-17
            # (сразу после step8) — здесь их нет, чтобы не было двойного TRUNCATE
            'big_analytics_seo', 'big_analytics_reviews', 'big_analytics_pixel',
            'big_analytics_crop_targeting',
            # DIRECT_SLIM_2026-07-11: узкая проекция direct (строится после step6,
            # читается только step11-бенчмарком) — между прогонами не нужна.
            'big_analytics_direct_slim',
            # PORT_FROM_FAST_2026-06-19: pixel_leads + pixel_leads_check добавлены
            # в синхрон с fast_pipeline.py — промежуточные таблицы step5/step11,
            # PBI не читает, не нужны между прогонами.
            'pixel_leads', 'pixel_leads_check',
            'big_analytics_full', 'big_analytics_unified',
            # raw_* добавлены 2026-06-17: пересоздаются step1 — безопасны к TRUNCATE в финале
            # raw_yandex перенесён в EARLY_TRUNCATE (освобождается раньше); остальные — здесь
            'raw_leads', 'raw_calls', 'raw_domains',
        ]
        _t_cl = time.perf_counter()
        try:
            _cl_conn = db_module.get_conn()
            try:
                # ── Шаг 1: проверка финальных таблиц (существуют + непустые) ────
                _finals_ok = True
                _final_counts: dict = {}
                with _cl_conn.cursor() as _cur:
                    for _ft in _final_tables:
                        _cur.execute("SELECT to_regclass(%s)", (f'public.{_ft}',))
                        if _cur.fetchone()[0] is None:
                            _finals_ok = False
                            _final_counts[_ft] = 'MISSING'
                            continue
                        _cur.execute(f'SELECT COUNT(*) FROM public.{_ft}')
                        _cnt = _cur.fetchone()[0]
                        _final_counts[_ft] = _cnt
                        if _cnt == 0:
                            _finals_ok = False
                _cl_conn.rollback()  # закрыть read-транзакцию проверки

                _finals_desc = ', '.join(f'{k}={v}' for k, v in _final_counts.items())

                if not _finals_ok:
                    # ── Финальные таблицы не готовы → НЕ обнуляем промежуточные ──
                    _msg = (f'Cleanup ПРОПУЩЕН: финальные таблицы не готовы '
                            f'(требуется непустые big_analytics_full + big_analytics_full_arrival). '
                            f'Состояние: {_finals_desc}')
                    logger.warning(_msg)
                    log_step(_cl_conn, run_id, 'cleanup_intermediate', 'skipped',
                             details=_msg)
                    _send_tg(
                        '⚠️ <b>big_analytics_v5</b>: cleanup промежуточных таблиц ПРОПУЩЕН\n'
                        f'Финальные таблицы не готовы: <code>{_finals_desc}</code>\n'
                        f'run_id: <code>{run_id}</code>'
                    )
                else:
                    # ── Шаг 2: снимаем размеры ДО TRUNCATE ─────────────────────
                    _sizes_before: dict[str, int] = {}
                    with _cl_conn.cursor() as _cur:
                        for _t in _cleanup_tables:
                            _cur.execute("SELECT to_regclass(%s)", (f'public.{_t}',))
                            if _cur.fetchone()[0] is not None:
                                _cur.execute(
                                    "SELECT pg_total_relation_size(%s)",
                                    (f'public.{_t}',)
                                )
                                _sizes_before[_t] = _cur.fetchone()[0]
                    _cl_conn.rollback()

                    # ── Шаг 3: финальные таблицы готовы → TRUNCATE промежуточных ──
                    _truncated_names: list = []
                    with _cl_conn.cursor() as _cur:
                        for _t in _cleanup_tables:
                            if _t in _sizes_before:
                                _cur.execute(f'TRUNCATE TABLE public.{_t}')
                                _truncated_names.append(_t)
                    _cl_conn.commit()

                    # ── Шаг 4: размер ДО → лог освобождённого места ─────────────
                    _total_freed = sum(_sizes_before.values())
                    def _fmt_bytes(b: int) -> str:
                        for unit in ('B', 'KB', 'MB', 'GB'):
                            if b < 1024:
                                return f'{b:.1f} {unit}'
                            b //= 1024
                        return f'{b:.1f} TB'
                    _sizes_desc = ', '.join(
                        f'{t}={_fmt_bytes(sz)}' for t, sz in _sizes_before.items()
                    )
                    _cl_elapsed = time.perf_counter() - _t_cl
                    _cl_msg = (
                        f'Финальные таблицы OK ({_finals_desc}); '
                        f'TRUNCATE {len(_truncated_names)} промежуточных, '
                        f'освобождено ~{_fmt_bytes(_total_freed)}: '
                        f'{_sizes_desc}'
                    )
                    logger.info('Disk cleanup: %s', _cl_msg)
                    step_timings.append(('cleanup_intermediate', 'cleanup_intermediate',
                                         _cl_elapsed, True))
                    log_step(_cl_conn, run_id, 'cleanup_intermediate', 'ok',
                             rows_affected=len(_truncated_names),
                             duration_sec=round(_cl_elapsed, 2),
                             details=_cl_msg)
            finally:
                db_module.put_conn(_cl_conn)
        except Exception as _e:
            logger.warning('Disk cleanup TRUNCATE ошибка: %s', _e)
            try:
                _err_conn = db_module.get_conn()
                try:
                    log_step(_err_conn, run_id, 'cleanup_intermediate', 'error', details=str(_e))
                finally:
                    db_module.put_conn(_err_conn)
            except Exception:
                pass

    # ── CLEANUP_ON_FAILURE_2026-07-11 ────────────────────────────────────────
    # При failed=True основной cleanup_intermediate (L~2003) и EARLY_TRUNCATE
    # (L~1824) ПРОПУСКАЮТСЯ — оба под `if not failed`. Из-за этого тяжёлые
    # транзиентные ВХОДНЫЕ таблицы (raw_yandex ~8.8 GB, raw_leads/calls/domains,
    # big_analytics_direct) остаются висеть и забивают диск до следующего прогона.
    # Инцидент: ночной прогон упал на step3 → cleanup пропущен → raw_yandex 8.8 GB
    # остался → диск переполнился, чистили руками.
    #
    # Чистим ТОЛЬКО транзиентные ВХОДЫ, которые step1/step3 гарантированно
    # пересоберут каждый прогон (UNLOGGED, DROP+CREATE; raw_yandex форсит rebuild
    # через freshness-guard step1.py:418-432 при пустой). НЕ трогаем промежуточные
    # big_analytics_unified/full — они могут понадобиться для разбора сбоя. НЕ
    # трогаем живые витрины (fact_*/Dim_*/pixel_score/arrival/local_*).
    # big_analytics_direct — транзиентный вход (не метрика-витрина), безопасен.
    #
    # best-effort и НЕ маскирует причину падения: блок ничего не перехватывает из
    # основного потока и не меняет failed — исходная ошибка уже залогирована выше
    # (log_step 'error') и приведёт к sys.exit(1) ниже. Внутренний try/except лишь
    # гарантирует, что сама очистка не уронит прогон новой ошибкой.
    # TRUNCATE при диск-переполнении безопасен (освобождает место, не требует его);
    # lock_timeout=5s — не зависать на локе при аварийном завершении.
    if failed and args.only_step is None:
        _cof_tables = ['raw_yandex', 'raw_leads', 'raw_calls', 'raw_domains',
                       'big_analytics_direct',
                       # COF_SLIM_2026-07-12: slim (~несколько GB, UNLOGGED, строится после
                       # step6) при падении после step6 иначе остаётся на диске до PRE_RUN_RECLAIM.
                       'big_analytics_direct_slim']
        try:
            _cof_conn = db_module.get_conn()
            try:
                _cof_conn.rollback()
                _cof_conn.autocommit = True
                _cof_freed = 0
                _cof_done: list = []
                with _cof_conn.cursor() as _cof_cur:
                    _cof_cur.execute("SET lock_timeout = '5s'")
                    for _cof_tbl in _cof_tables:
                        _cof_cur.execute("SELECT to_regclass(%s)", (f'public.{_cof_tbl}',))
                        if _cof_cur.fetchone()[0] is None:
                            continue
                        _cof_cur.execute("SELECT pg_total_relation_size(%s)",
                                         (f'public.{_cof_tbl}',))
                        _cof_sz = _cof_cur.fetchone()[0]
                        try:
                            _cof_cur.execute(f'TRUNCATE TABLE public.{_cof_tbl}')
                            _cof_freed += _cof_sz
                            _cof_done.append(f'{_cof_tbl}={_cof_sz // (1024 * 1024)}MB')
                        except Exception as _cof_te:
                            logger.warning(
                                'CLEANUP_ON_FAILURE: TRUNCATE public.%s не удался '
                                '(пропускаю, не критично): %s', _cof_tbl, _cof_te
                            )
            finally:
                _cof_conn.autocommit = False
                db_module.put_conn(_cof_conn)
            _cof_msg = (
                f'CLEANUP_ON_FAILURE: после падения освобождены транзиентные входы '
                f'~{_cof_freed // (1024 * 1024)} MB '
                f'({", ".join(_cof_done) if _cof_done else "нечего чистить"})'
            )
            logger.warning(_cof_msg)
            try:
                _cof_log_conn = db_module.get_conn()
                try:
                    log_step(_cof_log_conn, run_id, 'cleanup_on_failure', 'ok',
                             rows_affected=len(_cof_done), details=_cof_msg)
                finally:
                    db_module.put_conn(_cof_log_conn)
            except Exception:
                pass
        except Exception as _cof_e:
            # Очистка-при-падении сама не должна ронять прогон и маскировать
            # исходную ошибку — только warning, идём дальше к sys.exit(1).
            logger.warning('CLEANUP_ON_FAILURE: не критично, продолжаем: %s', _cof_e)

    db_module.close_pool()
    db_module.close_src_pool()

    # POST статуса запуска в dashboard (https://seoadvanced.ru/api/project-health)
    try:
        _failed_steps = [s for s, _, _, ok in step_timings if not ok]
        _health_payload = json.dumps({
            "status": "err" if failed else "ok",
            "pipeline_step": "pipeline",
            "msg": (
                f"run_id={run_id} "
                f"({'ошибка: ' + ', '.join(_failed_steps) if failed else 'успешно'}) "
                f"за {elapsed_total:.0f}с"
            ),
            "duration_sec": round(elapsed_total, 1),
            "errors": _failed_steps,
            "warnings": [],
        }).encode()
        _req = urllib.request.Request(
            "https://seoadvanced.ru/api/project-health",
            data=_health_payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(_req, timeout=5)
    except Exception as _e:
        logger.warning("dashboard health POST failed: %s", _e)

    if failed:
        logger.error('ЗАВЕРШЕНО С ОШИБКОЙ за %.0f сек (run_id=%s)', elapsed_total, run_id)
        sys.exit(1)
    else:
        logger.info('УСПЕШНО завершено за %.0f сек (run_id=%s)', elapsed_total, run_id)


if __name__ == '__main__':
    main()
