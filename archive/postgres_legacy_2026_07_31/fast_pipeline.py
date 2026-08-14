#!/usr/bin/env python3
"""
fast_pipeline.py — быстрый пайплайн big_analytics_v5

Отличия от pipeline.py:
  - step4 (campaign_status): кэш-режим — берётся из таблицы campaign_status
    предыдущего запуска pipeline.py, без Grid API (~5с вместо ~6 мин)
  - step9 (direct_history): пропущен, yandex_direct_history из кэша pipeline.py
  - step7: VACUUM ANALYZE пропущен

Требования перед запуском:
  - pipeline.py должен был запуститься хотя бы раз (создаёт campaign_status и yandex_direct_history)

Запуск:
    python fast_pipeline.py                # шаги 0,1,2,3,5,4,6,7,8
    python fast_pipeline.py --from-step=3  # с шага 3
    python fast_pipeline.py --only-step=6  # только шаг 6
"""

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from datetime import datetime

import config.db as db_module
from config.settings import T_DATA_QUALITY_LOG, T_DIRECT_HISTORY
from pipeline import _send_tg, log_step, ensure_quality_log, run_step, _PriorFailureSkip

# EXPLAIN_BASELINE_2026-06-17: поддержка EXPLAIN_CAPTURE=1 в fast_pipeline
# При установленном флаге run_step получает _explain_log → пишет планы в _logs/
import explain_capture as _explain_cap

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger('fast_pipeline')

MIN_FREE_GB_COMPACTIFY = 25.0
MIN_FREE_GB_STEP11 = 10.0
# SPEND_NIGHT_JOB_2026-06-27: spend-витрины (fact_region/adformat/criterion_spend)
# вынесены в step_cron_night/build_spend_daily.py. Дневной fast_pipeline их не строит.
# Константы MIN_FREE_GB_SPEND_* перенесены в build_spend_daily.py.


def _free_disk_gb(path: str = '/') -> float:
    return shutil.disk_usage(path).free / (1024 ** 3)


def _skip_with_analyze(conn, relname: str, logger_: logging.Logger, reason: str) -> None:
    conn.rollback()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute('SET max_parallel_maintenance_workers = 0')
            cur.execute(f'ANALYZE public.{relname}')
    finally:
        conn.autocommit = False
    logger_.warning('%s: skip, %s; ANALYZE public.%s выполнен', relname, reason, relname)

# FileHandler в /tmp/fast_pipeline.log — потребляется дашбордом через SSH tail -F
# (_PIPELINE_LOG_PATHS в html_bashbort_subagent/dashboard.py)
try:
    _fh = logging.FileHandler('/tmp/fast_pipeline.log', mode='a', encoding='utf-8')
    _fh.setLevel(logging.INFO)
    _fh.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    ))
    # Привязываем к корневому логгеру — тогда логи из pipeline.py / step*.py тоже попадут
    logging.getLogger().addHandler(_fh)
except Exception as _e:
    logger.warning('Не удалось открыть /tmp/fast_pipeline.log: %s', _e)

# P2_STEP13_SINGLE_CALL_2026-06-18: step13 УБРАН из FAST_STEPS.
# Ранее step13 вызывался ДВАЖДЫ:
#   1) Здесь в loop (до step11) — пиксель-ветка читала пустой big_analytics_full
#      (step11 ещё не долил пиксель_атрибуц) → ветка 4 строилась на пустоте.
#   2) step13_rebuild ниже (после step11) — все 4 ветки полные.
# Первый вызов избыточен: между ним и step11 arrival никто не читает
# (normalize_salons только big_analytics_full, step12/crm_mappings_check — не arrival,
# compactify/prefix — только big_analytics_full). Подтверждено grep по всем .py.
# Теперь ЕДИНСТВЕННЫЙ вызов — step13_rebuild после step11 (ниже ~L737).
# raw_leads жива для него: TRUNCATE raw_leads стоит ПОСЛЕ rebuild (~L782).
FAST_STEPS = [
    (0, 'step0_sync_local',      'step0_sync_local.step0',         'step0_sync_local'),
    (1, 'step1_load_raw',        'step1_load_raw.step1',           'step1_load_raw'),
    (2, 'step2_indexes',         'step2_indexes.step2',            'step2_indexes'),
    (3, 'step3_build_sources',   'step3_build_sources.step3',      'step3_build_sources'),
    (5, 'step5_build_pixel',     'step5_build_pixel.build_pixel',  'step5_build_pixel'),
    (4, 'step4_campaign_status', 'step4_campaign_status.step4',    'step4_campaign_status'),
    (6, 'step6_build_full',      'step6_build_full.step6',         'step6_build_full'),
    (7, 'step7_finalize',        'step7_finalize.step7',           'step7_finalize'),
    # (13, 'step13_arrival', ...) — P2_STEP13_SINGLE_CALL_2026-06-18: убран из loop,
    # единственный вызов — step13_rebuild после step11 (все 4 ветки полные).
]

# step8 (статистика+telegram) выполняется ПОСЛЕ дополнительных скриптов
# (load_reviews, load_api_leads, load_crop, normalize_salons, campaign_status_prefix, step11),
# чтобы все шаги попали в финальный отчёт через data_quality_log.
FAST_STEP8_INFO = (8, 'step8_stats', 'step8_stats.step8', 'step8_stats')


class _CompactifySkipped(Exception):
    """O2-sentinel: compactify_full пропущен по низкому bloat (не ошибка)."""


def _table_has_rows(conn, table_name: str) -> bool:
    """Проверить что таблица существует и непустая."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = %s
            )
        """, (table_name,))
        if not cur.fetchone()[0]:
            return False
        cur.execute(f'SELECT EXISTS (SELECT 1 FROM {table_name} LIMIT 1)')
        return cur.fetchone()[0]


def preflight_check(conn) -> bool:
    """
    Проверить наличие кэша yandex_direct_history и campaign_status.
    fast_pipeline пропускает step9 и Grid API → нужны таблицы от последнего pipeline.py.
    """
    ok = True
    if not _table_has_rows(conn, T_DIRECT_HISTORY):
        logger.error(
            'PREFLIGHT FAIL: таблица "%s" пуста или не существует. '
            'Запусти полный pipeline.py сначала.', T_DIRECT_HISTORY
        )
        ok = False

    if not _table_has_rows(conn, 'campaign_status'):
        logger.error(
            'PREFLIGHT FAIL: таблица "campaign_status" пуста или не существует. '
            'Запусти полный pipeline.py сначала.'
        )
        ok = False

    if ok:
        logger.info('Preflight OK: %s и campaign_status найдены', T_DIRECT_HISTORY)
    # FIX-PREFLIGHT (2026-06-11): раньше всегда return True → preflight НИКОГДА не
    # блокировал запуск при пустом кэше (yandex_direct_history/campaign_status).
    # main() делает `if not preflight_check(conn): sys.exit(1)` — возвращаем реальный ok.
    return ok


def main() -> None:
    parser = argparse.ArgumentParser(
        description='big_analytics_v5 fast pipeline (без API Директа)'
    )
    parser.add_argument('--from-step', type=int, default=0,
                        help='Начать с шага N. ⚠️ ЛОВУШКА порядка: в FAST_STEPS '
                             'шаг 5 (build_pixel) стоит РАНЬШЕ шага 4 (campaign_status), '
                             'а фильтр идёт по номеру (s[0] >= N). Поэтому --from-step=5 '
                             'ПРОПУСТИТ шаг 4 (4 < 5), а --from-step=4 выполнит оба '
                             '(5, затем 4). Для рестарта с пиксельной ветки — --from-step=4.')
    parser.add_argument('--only-step', type=int, default=None, help='Только шаг N')
    parser.add_argument('--force-compactify', action='store_true',
                        help='Принудительный CTAS-swap big_analytics_full даже при низком bloat')
    args = parser.parse_args()

    # ── PIPELINE_MUTEX_2026-07-12: взаимное исключение с pipeline_powerbi /
    # build_spend_daily / report_placement (общий flock). Занят другим из группы —
    # выходим рано (skip, exit 0). fd держим открытым весь прогон.
    import pipeline_mutex
    try:
        _mutex_fd = pipeline_mutex.acquire('fast_pipeline')  # noqa: F841 — держим fd весь прогон
    except pipeline_mutex.PipelineBusy as _busy:
        logger.warning(
            'PIPELINE_MUTEX: активен другой прогон группы (%s) — fast_pipeline пропущен', _busy
        )
        # TG_ON_BUSY_2026-07-12: единообразный алерт при mutex-skip (как build_spend_daily).
        _send_tg(f'⚠️ <b>fast_pipeline</b> SKIP: активен другой прогон группы ({_busy})')
        sys.exit(0)

    run_id = str(uuid.uuid4())[:8]
    started_at = datetime.now()
    logger.info('=' * 60)
    logger.info('fast_pipeline  run_id=%s  %s', run_id, started_at.strftime('%Y-%m-%d %H:%M:%S'))
    logger.info('⚡ Режим: step4 через API, step9 history пропущен (из кэша)')
    logger.info('=' * 60)

    # STEP_NOTIFY_BATCH_2026-06-18: счётчик завершённых шагов для периодических уведомлений.
    # Telegram-сообщение слать раз в 5 шагов (на 5-м, 10-м, 15-м…), FAIL — всегда немедленно.
    # _sn_steps: список (имя, elapsed, ok) для сводки. _sn_count: общий счётчик.
    def _fmt_dur(s: float) -> str:
        if s >= 60:
            return f'{int(s // 60)}м {int(s % 60):02d}с'
        return f'{s:.0f}с'

    _sn_count: list = [0]         # mutable обёртка для замыкания
    _sn_batch: list = []          # накопленные шаги текущего batch

    def _step_notify(name: str, elapsed: float, ok: bool) -> None:
        """
        Учитывает завершённый шаг в счётчике.
        FAIL → немедленное TG-сообщение.
        Каждые 5 завершённых шагов → краткая сводка.
        """
        _sn_batch.append((name, elapsed, ok))
        _sn_count[0] += 1
        try:
            if not ok:
                # FAIL всегда уходит немедленно — не теряется из-за счётчика
                _send_tg(
                    f'FAIL  <b>{name}</b> — {_fmt_dur(elapsed)}\n'
                    f'run_id: <code>{run_id}</code>'
                )
                _sn_batch.clear()
                return
            if _sn_count[0] % 5 == 0:
                # Каждые 5 шагов — краткая сводка по batch
                lines = []
                total_elapsed = 0.0
                for _n, _e, _ok in _sn_batch:
                    lines.append(f'{"OK" if _ok else "FAIL"}  {_n} — {_fmt_dur(_e)}')
                    total_elapsed += _e
                body = '\n'.join(lines)
                _send_tg(
                    f'<b>fast_pipeline</b> — шаги #{_sn_count[0] - 4}…#{_sn_count[0]}\n'
                    f'{body}\n'
                    f'Итого: {_fmt_dur(total_elapsed)}  |  run_id: <code>{run_id}</code>'
                )
                _sn_batch.clear()
        except Exception as _sn_e:
            logger.warning('STEP_NOTIFY: не критично: %s', _sn_e)

    # EXPLAIN_BASELINE_2026-06-17: открываем лог планов при EXPLAIN_CAPTURE=1
    _explain_log: str | None = _explain_cap.write_header(run_id) if _explain_cap.EXPLAIN_CAPTURE else None
    if _explain_log:
        logger.info('EXPLAIN_CAPTURE=1: планы будут записаны в %s', _explain_log)

    try:
        db_module.init_pool()
        db_module.init_src_pool()
    except Exception as e:
        logger.error('Не удалось подключиться к БД: %s', e)
        sys.exit(1)

    conn = db_module.get_conn()
    try:
        ensure_quality_log(conn)

        # Preflight: yandex_direct_history нужен (step9 пропущен)
        if args.only_step is None:
            if not preflight_check(conn):
                logger.error('Запусти полный pipeline.py для создания кэша.')
                sys.exit(1)
    finally:
        db_module.put_conn(conn)

    # ── PREFLIGHT_DISK_GUARD_2026-06-29: жёсткая проверка диска ПЕРЕД стартом ──
    # Порог 17 GB (пересчитан 2026-06-29 с учётом PRE_RUN_RECLAIM):
    #   PRE_RUN_RECLAIM (ниже) освобождает ~11 GB (big_analytics_full + intermediates).
    #   Минимальный start для успешного прогона = 14.6 GB (binding: step6 peak).
    #   17 GB = 14.6 + 2.4 GB буфер безопасности.
    #   Дисковый бюджет при 17 GB: +11 reclaim = 28 GB → step3 pик = 5 GB margin → OK;
    #     step6 пик = 2.4 GB margin → OK; build_unified guard (12 GB): 15.4 >= 12 → OK.
    # При недостатке: СТАТУС FAIL (не DEGRADED), alert в Telegram, PBI-refresh НЕ триггерится.
    # fact_big_analytics, pixel_score, Dim_*, fact_criterion_*: НЕ тронуты → /cpl работает.
    # ПОРОГ skip: --only-step или --from-step > 0 → guard не нужен (точечный запуск).
    # Маркер: PREFLIGHT_DISK_GUARD_2026-06-29
    if args.only_step is None and args.from_step == 0:
        import shutil as _shutil
        _disk_free_gb = _shutil.disk_usage('/').free / 1024 ** 3
        _PREFLIGHT_MIN_GB = 17.0
        if _disk_free_gb < _PREFLIGHT_MIN_GB:
            _msg = (
                f'PREFLIGHT_DISK_GUARD_2026-06-29: ABORT — диск {_disk_free_gb:.1f} GB < '
                f'{_PREFLIGHT_MIN_GB} GB. Финальные таблицы СОХРАНЕНЫ (прогон не начат). '
                f'Освободите диск и перезапустите. run_id={run_id}'
            )
            logger.error(_msg)
            _send_tg(f'🔴 {_msg}')
            sys.exit(1)
        else:
            logger.info(
                'PREFLIGHT_DISK_GUARD_2026-06-29: disk free %.1f GB >= %.0f GB — OK',
                _disk_free_gb, _PREFLIGHT_MIN_GB,
            )

    # ── PRE_RUN_RECLAIM_2026-06-29: освобождение диска ДО step1 ─────────────
    # НАХОДКА: /cpl читает НЕ big_analytics_full, а fact_big_analytics (leads_api_perform/app.py L241).
    # Docstring leads_api_perform устарел. fact_big_analytics защищён (не включён в список ниже).
    # Значит big_analytics_full — внутреннее промежуточное, /cpl его не видит → TRUNCATE безопасен.
    #
    # БЕЗ PRE_RUN_RECLAIM: disk_at_step3_start = 21.3 - 8 (raw_yandex) = 13.3 GB < 14 GB (big_analytics_direct) → CRASH.
    # С PRE_RUN_RECLAIM: 21.3 + 11 (big_analytics_full) = 32.3 GB → step3 получает 24.3 GB → OK.
    # Дисковый бюджет (апрокс): start 21.3 → reclaim +11.7 = 33.0 → step1 -8 = 25.0 →
    #   step3 peak -15 = 10.0 → RAW_YANDEX_PREFREE +8 = 18.0 → step6 peak -12 = 6.0 →
    #   PREFREE_BEFORE_PIXEL +14 = 20.0 → build_unified -6 = 14.0 → build_star -2.3 = 11.7 → OK.
    #
    # Защищены (НЕ включены): fact_big_analytics (/cpl), fact_criterion_spend/zayavki/dim_criterion
    #   (живой PBI admin-отчёт), pixel_score (PBI #9), Dim_*, fact_region/adformat_spend (pending PBI),
    #   local_*, yandex_direct_*, analytics_report_*, все прочие финальные таблицы.
    # Маркер: PRE_RUN_RECLAIM_2026-06-29
    if args.only_step is None and args.from_step == 0:
        _reclaim_tables = [
            'big_analytics_full',            # 11 GB — rebuilt by step6 (CTAS/TRUNCATE+INSERT)
            'big_analytics_full_arrival',    # 25 MB — rebuilt by step13
            'big_analytics_unified',         # 0 GB (already empty) — rebuilt by build_unified
            'big_analytics_direct',          # 0 GB (empty after crash) — rebuilt by step3
            'big_analytics_direct_slim',     # DIRECT_SLIM_2026-07-11: дропаем stale slim от
                                             #   упавшего full-прогона → step11 fallback на direct
            'big_analytics_seo',             # intermediate — rebuilt by step3
            'big_analytics_pixel',           # intermediate — rebuilt by step3/step5
            'big_analytics_telegram',        # intermediate — rebuilt by step3
            'big_analytics_crop_targeting',  # intermediate — rebuilt by step10
            'pixel_leads',                   # intermediate — rebuilt by step11
            'raw_yandex',                    # 0 GB (empty) — rebuilt by step1
            'raw_leads',                     # 231 MB (prev run) — rebuilt by step1
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
                'PRE_RUN_RECLAIM_2026-06-29: освобождено ~%d MB, диск свободен %.1f GB',
                _rcl_total_mb, _rcl_disk_gb,
            )
        except Exception as _rcl_e:
            logger.warning(
                'PRE_RUN_RECLAIM_2026-06-29: ошибка (не критично, продолжаем): %s', _rcl_e
            )

    _send_tg(
        f'⚡ <b>fast_pipeline</b> запущен (step9 пропущен, step4 через API)\n'
        f'<code>{started_at.strftime("%d.%m.%Y %H:%M")}</code>  '
        f'run_id: <code>{run_id}</code>'
    )

    if args.only_step is not None:
        to_run = [s for s in FAST_STEPS if s[0] == args.only_step]
        if not to_run:
            logger.error('Шаг %d не найден (доступны в FAST_STEPS: 0,1,2,3,4,5,6,7; '
                         'step13 вызывается только как step13_rebuild после step11)', args.only_step)
            sys.exit(1)
    else:
        to_run = [s for s in FAST_STEPS if s[0] >= args.from_step]

    failed = False
    degraded = False
    degraded_steps: list[str] = []
    step_timings: list = []
    # ── Guard step13: флаг что raw_leads была непустой после step1 ─────────────
    # В fast_pipeline raw_leads TRUNCATE-ится после step3+corrections (до step13).
    # Поэтому проверяем raw_leads сразу после step1, сохраняем флаг.
    # Если step1 не выполнялся в этом run (--from-step >= 2) — флаг не установлен
    # и guard будет консервативно пропускать step13 только при явном skip_guard.
    _raw_leads_populated: bool = False   # устанавливается в блоке after-step1 ниже

    for step_num, name, module_path, _ in to_run:
        logger.info('━' * 60)
        logger.info('Шаг %s: %s', step_num, name)

        # ── Guard step13: проверка флага ─────────────────────────────────────
        # raw_leads намеренно TRUNCATE-ится в этом пайплайне после step3,
        # поэтому прямая проверка COUNT невозможна — используем флаг из step1.
        if step_num == 13:
            if not _raw_leads_populated:
                _guard_msg = (
                    'step13 ПРОПУЩЕН: raw_leads была пустой после step1 '
                    '(вероятно рестарт PostgreSQL обнулил UNLOGGED-таблицу). '
                    'big_analytics_full_arrival не перезаписана — '
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
                    '⚠️ <b>step13 ПРОПУЩЕН</b>: raw_leads пустая после step1\n'
                    '(вероятно рестарт PostgreSQL обнулил UNLOGGED-таблицу)\n'
                    'big_analytics_full_arrival <b>не перезаписана</b> — сохранён предыдущий корректный прогон.\n'
                    f'run_id: <code>{run_id}</code>'
                )
                step_timings.append((step_num, name, 0.0, True))  # не считаем failed
                continue  # пропустить step13

        if step_num == 4:
            # Кэш-режим: campaign_status берётся из предыдущего pipeline.py, Grid API не вызывается
            import importlib as _il
            _step4 = _il.import_module('step4_campaign_status.step4')
            _s4_conn = db_module.get_conn()
            _t0 = time.perf_counter()
            try:
                _res = _step4.run_from_cache(_s4_conn, run_id=run_id)
                elapsed = time.perf_counter() - _t0
                ok = True
                log_step(_s4_conn, run_id, 'step4_campaign_status', 'ok',
                         rows_affected=_res.get('rows', 0), duration_sec=round(elapsed, 2))
                logger.info('step4 (кэш): %s за %.1f сек', _res.get('details', ''), elapsed)
            except Exception as _e:
                elapsed = time.perf_counter() - _t0
                ok = False
                logger.error('step4 (кэш) ОШИБКА: %s', _e)
                _s4_conn.rollback()
            finally:
                db_module.put_conn(_s4_conn)
        elif step_num == 7:
            # P1_STEP7_SKIP_DIRECT_SETLOGGED_2026-06-18: big_analytics_direct (~14 GB)
            # в fast_pipeline EARLY_TRUNCATE-ится (SPEND_PREFREE + EARLY_TRUNCATE_DIRECT_FAST)
            # → SET LOGGED 14 GB WAL-пика ради таблицы, которую обнулят через ~минуты, зря.
            # Передаём список БЕЗ T_DIRECT — экономия ~3-5 мин + ~14 GB WAL.
            # P2_STEP7_SKIP_FULL_SETLOGGED_2026-06-24: big_analytics_full (~5 GB) тоже
            # убираем из SET LOGGED в fast_pipeline. Механика: SET LOGGED записывает
            # полный heap (~5 GB) в WAL → checkpoint не сбрасывается (bi_analytic не superuser)
            # → WAL накапливается ~10-22 GB → при compactify_full (следующий шаг) диск кончается.
            # Решение безопасно: big_analytics_full пересобирается с нуля при каждом fast-прогоне
            # (step6 делает TRUNCATE+INSERT). При crash-recovery она всё равно обнулится
            # (UNLOGGED) и потребует нового прогона — SET LOGGED ничего не спасает.
            # build_unified читает T_FULL — это работает независимо от LOGGED/UNLOGGED статуса.
            # seo/pixel/crop/reviews оставляем — они небольшие (десятки MB, не GB WAL-пика).
            from config.settings import T_SEO, T_PIXEL, T_CROP, T_REVIEWS as _T_REVIEWS
            _step7_tables = [T_SEO, T_PIXEL, T_CROP, _T_REVIEWS]
            ok, elapsed = run_step(step_num, module_path, run_id,
                                   _explain_log=_explain_log, skip_vacuum=True,
                                   set_logged_tables=_step7_tables)
        else:
            ok, elapsed = run_step(step_num, module_path, run_id,
                                   _explain_log=_explain_log)

        step_timings.append((step_num, name, elapsed, ok))

        # STEP_NOTIFY_BATCH_2026-06-18: вместо per-step — каждые 5 шагов + немедленно на FAIL
        _step_notify(f'step{step_num} {name}', elapsed, ok)

        if not ok:
            logger.error('Пайплайн остановлен на шаге %d (%s)', step_num, name)
            failed = True
            break

        # ── Фиксируем флаг raw_leads после step1 (до TRUNCATE в step3) ─────────
        if step_num == 1 and ok:
            _rl_check_conn = db_module.get_conn()
            try:
                with _rl_check_conn.cursor() as _rlc:
                    _rlc.execute("SELECT COUNT(*) FROM public.raw_leads")
                    _rl_cnt = _rlc.fetchone()[0]
                _raw_leads_populated = (_rl_cnt > 0)
                if _raw_leads_populated:
                    logger.info('Guard step13: raw_leads=%d строк — step13 разрешён', _rl_cnt)
                else:
                    logger.warning(
                        'Guard step13: raw_leads пустая после step1 (%d строк) — '
                        'step13 будет ПРОПУЩЕН (UNLOGGED-таблица обнулена при рестарте PostgreSQL?)', _rl_cnt
                    )
            except Exception as _rlce:
                logger.warning('Guard step13: не удалось проверить raw_leads: %s', _rlce)
                _raw_leads_populated = True  # не блокируем при ошибке проверки
            finally:
                db_module.put_conn(_rl_check_conn)

        # PATCH-CRMF-LIDER-DEDUP (2026-06-15, синхрон с pipeline.py L442-461):
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

        if step_num == 3 and ok:
            try:
                import corrections as corr_mod
                corr_conn = db_module.get_conn()
                try:
                    result = corr_mod.apply(corr_conn)
                    logger.info('Корректировки применены: %s строк', result.get('rows', 0))
                finally:
                    db_module.put_conn(corr_conn)
            except Exception as e:
                # FIX-CORR-FAILED (2026-06-11, синхрон с pipeline.py L482-498):
                # corrections.apply применяет rule1_кудерко и пр. правила атрибуции —
                # если она оборвалась (напр. _interim_vacuum упал ДО rule1), данные
                # БИТЫЕ (специалист не переназначен → golden-FAIL). Раньше это
                # глоталось как warning → ложное "УСПЕШНО", verify гнался на битых
                # данных, cleanup TRUNCATE-ил источники. Теперь помечаем прогон failed:
                # verify/cleanup/post-loop не запустятся (guard not failed), уйдёт TG-алёрт.
                logger.error('Ошибка в corrections.apply (прогон помечен failed): %s', e)
                failed = True
                try:
                    _ec = db_module.get_conn()
                    try:
                        log_step(_ec, run_id, 'corrections', 'error', details=str(e)[:500])
                    finally:
                        db_module.put_conn(_ec)
                except Exception:
                    pass
                break

            # VACUUM после массовых UPDATE в corrections (Rule 0b/0c переписывают
            # по 3+ млн строк) — снимает dead tuples, не даёт big_analytics_direct
            # распухать до ~17 GB и переполнять диск. VACUUM нельзя в транзакции →
            # отдельное соединение с autocommit.
            try:
                _vac_conn = db_module.get_conn()
                try:
                    # FIX-VAC-ROLLBACK (2026-06-11, синхрон с pipeline.py L512):
                    # get_conn() пингует SELECT 1 без commit → "idle in transaction".
                    # set_session (=.autocommit=True) внутри tx запрещён psycopg2
                    # ("set_session cannot be used inside a transaction") → VACUUM
                    # молча падал, direct раздувался до 18 ГБ, step6 No space left
                    # (run 8c339c3b). rollback() закрывает ping-tx.
                    _vac_conn.rollback()
                    _vac_conn.autocommit = True
                    with _vac_conn.cursor() as _cur:
                        # Параллельные воркеры VACUUM используют /dev/shm — на этом VPS
                        # он мал → "could not resize shared memory segment". Отключаем
                        # параллелизм двумя способами (как в star_refactor/build_star.py):
                        #   1) SET max_parallel_maintenance_workers = 0 — глобально для сессии
                        #   2) VACUUM (..., PARALLEL 0) — явно на каждом стейтменте (надёжнее,
                        #      не зависит от planner-эвристик при вакууме индексов).
                        # maintenance_work_mem ограничен, чтобы один проход не выедал память.
                        _cur.execute('SET max_parallel_maintenance_workers = 0')
                        _cur.execute("SET maintenance_work_mem = '256MB'")
                        for _vt in ('big_analytics_direct', 'big_analytics_crop_targeting'):
                            _cur.execute("SELECT to_regclass(%s)", (f'public.{_vt}',))
                            if _cur.fetchone()[0] is not None:
                                # Обычный VACUUM (не FULL): реклеймит dead tuples для
                                # ПЕРЕИСПОЛЬЗОВАНИЯ, файл не растёт дальше high-water mark.
                                # FULL переписал бы файл целиком (нужно ~19 ГБ свободного
                                # под копию) и держит ACCESS EXCLUSIVE lock — на UNLOGGED
                                # таблице, пересоздаваемой каждый прогон, это лишний риск
                                # DiskFull. Обычный VACUUM достаточно чтобы стабилизировать
                                # размер на каждом прогоне.
                                _cur.execute(f'VACUUM (ANALYZE, PARALLEL 0) public.{_vt}')
                    logger.info('VACUUM (ANALYZE, PARALLEL 0) big_analytics_direct/crop_targeting после corrections')
                finally:
                    _vac_conn.autocommit = False
                    db_module.put_conn(_vac_conn)
            except Exception as e:
                logger.warning('VACUUM после corrections не удался (не критично): %s', e)

            # ВНИМАНИЕ: raw_leads здесь НЕ truncate-им — её читает step13_arrival
            # (часть "лиды"), который запускается позже по списку STEPS. Раньше TRUNCATE
            # стоял тут → step13 собирал big_analytics_full_arrival без лидов (только
            # звонки из local_leads_all) → визит-приезды занижались втрое.
            # Освобождение перенесено в блок step_num == 13 ниже.

            # RAW_YANDEX_PREFREE: step3 — последний потребитель raw_yandex
            # (yd_agg, _account_manager_map, leads_unmatched). Дальше таблица только
            # занимает ~8 GB и может уронить step6/step11 по диску.
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
                                '(освобождено место перед step6/step11)'
                            )
                finally:
                    _ryp_conn.autocommit = False
                    db_module.put_conn(_ryp_conn)
            except Exception as _ryp_e:
                logger.warning('RAW_YANDEX_PREFREE: не критично, продолжаем: %s', _ryp_e)

        # P2_STEP13_SINGLE_CALL_2026-06-18: raw_leads НЕ освобождаем здесь после step3.
        # raw_leads нужна для step13_rebuild (ниже, после step11) — ветки 1-3
        # (лиды/звонки/посевы) читают raw_leads. TRUNCATE raw_leads — ТОЛЬКО после
        # step13_rebuild (см. блок «── ПЕРЕСБОРКА BFA (step13) ПОСЛЕ step11 ──»).

    if not failed and args.only_step is None:
        _base = os.path.dirname(os.path.abspath(__file__))
        _log_conn = db_module.get_conn()
        try:
            for script_rel, label in [
                ('step_cron_night/direct_account_reviews/load_reviews_to_big_analytics.py', 'load_reviews'),
                ('step10_crop_targeting/load_telega_in_orders.py',                                  'load_api_leads'),
                ('step10_crop_targeting/load_crop_to_big_analytics.py',                      'load_crop'),
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

            try:
                import corrections as corr_mod
                norm_conn = db_module.get_conn()
                _t_norm = time.perf_counter()
                try:
                    # FIX-NORM-ARRIVAL (2026-06-11, синхрон с pipeline.py L573-579):
                    # arrival УБРАН отсюда — раньше его нормализация стояла ДО
                    # step13_rebuild (ниже, после step11), который пересобирает arrival
                    # ЗАНОВО (DROP+CREATE) → нормализация arrival терялась. Теперь
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

            # ── Очистка устаревших данных: удалить строки до DATE_FROM ──────────
            # fast_pipeline пересобирает big_analytics_full из исходников (там есть
            # старые 2025-заявки) → без этой очистки строки до 2026-01-01 возвращаются
            # каждый прогон. В pipeline.py этот шаг есть, в fast его не было.
            # big_analytics_full_arrival включён (на будущее — сейчас в нём нет <2026).
            try:
                from config.settings import DATE_FROM
                _clean_tables = [
                    'big_analytics_full', 'big_analytics_direct',
                    'big_analytics_crop_targeting', 'big_analytics_seo',
                    'big_analytics_pixel', 'big_analytics_reviews',
                    'big_analytics_full_arrival',
                ]
                clean_conn = db_module.get_conn()
                _t_clean = time.perf_counter()
                total_deleted = 0
                try:
                    with clean_conn.cursor() as _cur:
                        for _tbl in _clean_tables:
                            _cur.execute("SELECT to_regclass(%s)", (f'public.{_tbl}',))
                            if _cur.fetchone()[0] is None:
                                continue
                            _cur.execute(f'DELETE FROM public.{_tbl} WHERE "Date" < %s', (DATE_FROM,))
                            total_deleted += _cur.rowcount
                    clean_conn.commit()
                    _clean_elapsed = time.perf_counter() - _t_clean
                    logger.info('cleanup_old_dates: удалено %d строк до %s за %.1f сек',
                                total_deleted, DATE_FROM, _clean_elapsed)
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
                    # FIX-VAC-ROLLBACK (2026-06-11, синхрон с pipeline.py L633):
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
        finally:
            db_module.put_conn(_log_conn)

        # ── Префикс emoji-индикатора статуса кампании ────────────────────────
        # Активна → 🟢, Остановлена → 🟡, прочее (Архив/NULL) → ⚪
        # step6 пересоздаёт big_analytics_full → префикс нужно накладывать после.
        _log_conn2 = db_module.get_conn()
        try:
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
                    log_step(_log_conn2, run_id, 'campaign_status_prefix', 'ok',
                             rows_affected=n_pref, duration_sec=round(_pref_elapsed, 2))
                finally:
                    db_module.put_conn(pref_conn)
            except Exception as e:
                logger.warning('Ошибка в campaign_status_prefix: %s', e)

            # ── PREFREE_BEFORE_PIXEL_2026-06-29 / PREFREE_MOVE_2026-07-02 ─────
            # big_analytics_direct (~14 GB) не нужен compactify/step11/step12/step13/build_unified:
            #   step12 читает big_analytics_full WHERE _source_table='direct' (НЕ big_analytics_direct)
            #   step11 (pixel_score) читает big_analytics_full + local_pixel_config
            #   step13_rebuild, build_unified, build_star — не читают direct
            # PREFREE_MOVE_2026-07-02: перенесён ДО compactify_full — compactify видит
            # +14 GB свободного диска и всегда выполняет CTAS-swap (9→5 GB).
            # cleanup_old_dates (DELETE FROM direct WHERE Date<DATE_FROM) уже выполнен выше.
            # raw_yandex уже TRUNCATED по RAW_YANDEX_PREFREE (после step3) — no-op.
            # Маркер: PREFREE_BEFORE_PIXEL_2026-06-29 / PREFREE_MOVE_2026-07-02
            try:
                _pbp_conn = db_module.get_conn()
                try:
                    _pbp_conn.rollback()
                    _pbp_conn.autocommit = True
                    with _pbp_conn.cursor() as _pbp_cur:
                        for _pbp_tbl in ('big_analytics_direct', 'raw_yandex'):
                            _pbp_cur.execute(
                                'SELECT to_regclass(%s)', (f'public.{_pbp_tbl}',)
                            )
                            if _pbp_cur.fetchone()[0] is not None:
                                _pbp_cur.execute(f'TRUNCATE TABLE public.{_pbp_tbl}')
                                logger.info(
                                    'PREFREE_BEFORE_PIXEL: TRUNCATE public.%s перед compactify_full',
                                    _pbp_tbl,
                                )
                finally:
                    _pbp_conn.autocommit = False
                    db_module.put_conn(_pbp_conn)
            except Exception as _pbp_e:
                logger.warning('PREFREE_BEFORE_PIXEL: не критично, продолжаем: %s', _pbp_e)

            # ── Компактификация big_analytics_full (CTAS-swap) ─────────────────
            # Лечит page bloat от 13-15 UPDATE-проходов после TRUNCATE+INSERT в step6:
            # heap 9.2 GB → ~5 GB. Owner таблицы — bi_analytic (CTAS создаёт от него же).
            # Индексы воспроизводятся из step7 финальных. Гранты — SELECT → ro/vovatraffic.
            # step6 следующего прогона: делает ALTER TABLE SET UNLOGGED + TRUNCATE — OK,
            # CTAS создаёт PERMANENT-таблицу, step6 сам её переводит в UNLOGGED.
            # FIX-COMPACTIFY-GATE (2026-06-11, синхрон с pipeline.py L686-770):
            # CTAS-swap делается ТОЛЬКО при реальном bloat выше порога
            # (dead_pct = n_dead_tup/n_live_tup*100 > COMPACTIFY_BLOAT_PCT). На здоровых
            # прогонах (bloat≈0) swap избыточен — лишь дублирует ~5 ГБ work без выигрыша.
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

                    _free_gb = _free_disk_gb('/')
                    if not args.force_compactify and _free_gb < MIN_FREE_GB_COMPACTIFY:
                        _skip_with_analyze(
                            cmp_conn,
                            'big_analytics_full',
                            logger,
                            f'free disk {_free_gb:.1f} GB < {MIN_FREE_GB_COMPACTIFY:.0f} GB для CTAS-swap',
                        )
                        _cmp_elapsed = time.perf_counter() - _t_cmp
                        step_timings.append(('compactify_full', 'compactify_full', _cmp_elapsed, True))
                        log_step(_log_conn2, run_id, 'compactify_full', 'skipped',
                                 rows_affected=0, duration_sec=round(_cmp_elapsed, 2),
                                 details=(f'skip: free disk {_free_gb:.1f} GB '
                                          f'< {MIN_FREE_GB_COMPACTIFY:.0f} GB'))
                        raise _CompactifySkipped

                    if (not args.force_compactify
                            and _dead_pct is not None
                            and _dead_pct <= COMPACTIFY_BLOAT_PCT):
                        # bloat низкий → swap не нужен, только освежаем статистику
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
                        log_step(_log_conn2, run_id, 'compactify_full', 'skipped',
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
                    log_step(_log_conn2, run_id, 'compactify_full', 'ok',
                             rows_affected=0, duration_sec=round(_cmp_elapsed, 2))
                except _CompactifySkipped:
                    pass  # bloat низкий — swap осознанно пропущен (ANALYZE уже сделан)
                finally:
                    db_module.put_conn(cmp_conn)
            except Exception as e:
                logger.warning('compactify_full failed (не критично, пайплайн продолжен): %s', e)

            # ── step12 + crm_mappings_check: запускаем фоном параллельно step11 ──
            # step12 читает big_analytics_full WHERE _source_table='direct' (НЕ big_analytics_direct)
            # crm_mappings_check читает local_crm_statuses (независим от step11)
            _s12_result: dict = {}
            _cmc_result: dict = {}

            def _run_step12_bg() -> None:
                import importlib as _il
                _mod = _il.import_module('step12_proverka_big_analytics.step12')
                _conn = db_module.get_conn()
                _t = time.perf_counter()
                try:
                    _res = _mod.run(_conn, run_id=run_id)
                    _s12_result['elapsed'] = time.perf_counter() - _t
                    _s12_result['rows'] = _res.get('rows', 0) if isinstance(_res, dict) else 0
                    _s12_result['ok'] = True
                    log_step(_conn, run_id, 'step12', 'ok',
                             rows_affected=_s12_result['rows'],
                             duration_sec=round(_s12_result['elapsed'], 2))
                except Exception as _e:
                    _s12_result['elapsed'] = time.perf_counter() - _t
                    _s12_result['ok'] = False
                    logger.warning('step12 фон ошибка: %s', _e)
                    log_step(_conn, run_id, 'step12', 'error',
                             duration_sec=round(_s12_result['elapsed'], 2), details=str(_e))
                finally:
                    db_module.put_conn(_conn)

            def _run_cmc_bg() -> None:
                import importlib as _il
                _mod = _il.import_module('crm_mappings_check.check')
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
                    _cmc_result['elapsed'] = time.perf_counter() - _t
                    _cmc_result['ok'] = False
                    logger.warning('crm_mappings_check фон ошибка: %s', _e)
                    log_step(_conn, run_id, 'crm_mappings_check', 'error',
                             duration_sec=round(_cmc_result['elapsed'], 2), details=str(_e))
                finally:
                    db_module.put_conn(_conn)

            _t12_thr = threading.Thread(target=_run_step12_bg, daemon=True, name='step12_bg')
            _tcmc_thr = threading.Thread(target=_run_cmc_bg, daemon=True, name='cmc_bg')
            _t12_thr.start()
            _tcmc_thr.start()
            logger.info('Фоновые потоки запущены: step12 + crm_mappings_check (параллельно step11)')

            # ── step11: атрибуция pixel-заявок (foreground) ──
            try:
                import importlib
                step11_mod = importlib.import_module('step11_pixel_score.step11')
                s11_conn = db_module.get_conn()
                _t11 = time.perf_counter()
                try:
                    _free_gb_s11 = _free_disk_gb('/')
                    if _free_gb_s11 < MIN_FREE_GB_STEP11:
                        _s11_elapsed = time.perf_counter() - _t11
                        _msg = (f'step11_pixel_score skipped: free disk {_free_gb_s11:.1f} GB '
                                f'< {MIN_FREE_GB_STEP11:.0f} GB для pgsql_tmp')
                        logger.warning(_msg)
                        degraded = True
                        degraded_steps.append('step11_pixel_score_low_disk')
                        step_timings.append(('step11_pixel_score', 'step11_pixel_score', _s11_elapsed, True))
                        log_step(_log_conn2, run_id, 'step11', 'skipped',
                                 duration_sec=round(_s11_elapsed, 2), details=_msg)
                        raise _CompactifySkipped

                    step11_mod.run(s11_conn, run_id=run_id)
                    _s11_elapsed = time.perf_counter() - _t11
                    logger.info('step11_pixel_score завершён за %.1f сек', _s11_elapsed)
                    step_timings.append(('step11_pixel_score', 'step11_pixel_score', _s11_elapsed, True))
                    # STEP_NOTIFY_BATCH_2026-06-18
                    _step_notify('step11 pixel_score', _s11_elapsed, True)
                    log_step(_log_conn2, run_id, 'step11', 'ok', duration_sec=round(_s11_elapsed, 2))
                    # EXPLAIN_BASELINE_2026-06-17: захват плана step11
                    if _explain_log and _explain_cap.EXPLAIN_CAPTURE:
                        try:
                            _esql11 = getattr(step11_mod, 'get_explain_sql', None)
                            if _esql11 is not None:
                                import inspect as _ins11
                                _sig11 = _ins11.signature(_esql11)
                                _sql11 = _esql11(s11_conn) if len(_sig11.parameters) > 0 else _esql11()
                                _explain_cap.wrap_explain(s11_conn, _sql11, 'step11',
                                                 run_id=run_id, log_file=_explain_log, wall_sec=_s11_elapsed)
                        except Exception as _ce11:
                            logger.warning('explain_capture step11: %s', _ce11)
                except _CompactifySkipped:
                    pass
                finally:
                    db_module.put_conn(s11_conn)
            except Exception as e:
                # FAIL_ON_CRITICAL_STEP_2026-07-12 (зеркало pipeline.py): step11 доливает
                # пиксель_атрибуц в big_analytics_full — реальная ошибка (не low-disk skip,
                # тот идёт через _CompactifySkipped/degraded выше) = НЕПОЛНЫЕ данные. Раньше
                # глушилось в warning → прогон шёл дальше и мог опубликовать неполноту.
                # Теперь failed=True → сработают SKIP_ON_FAILED-гейты ниже и exit-код.
                logger.error('Ошибка в step11_pixel_score (критично): %s', e)
                log_step(_log_conn2, run_id, 'step11', 'error', details=str(e))
                failed = True

            # Ждём фоновые потоки (должны завершиться до step11 или вместе с ним)
            _t12_thr.join()
            _tcmc_thr.join()

            step_timings.append(('step12_proverka', 'step12_proverka',
                                  _s12_result.get('elapsed', 0), _s12_result.get('ok', False)))
            step_timings.append(('crm_mappings_check', 'crm_mappings_check',
                                  _cmc_result.get('elapsed', 0), _cmc_result.get('ok', False)))
            logger.info('step12: %s строк за %.1f сек',
                        _s12_result.get('rows', '?'), _s12_result.get('elapsed', 0))
            logger.info('crm_mappings_check: %s unused за %.1f сек',
                        _cmc_result.get('rows', '?'), _cmc_result.get('elapsed', 0))

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
                logger.exception('Ошибка в spec_fallback_v3(full): %s', e)

            # ── ПЕРЕСБОРКА BFA (step13) ПОСЛЕ step11 ───────────────────────────
            # КРИТИЧНО для пиксель-визит ветки: step13 в FAST_STEPS отрабатывает ДО
            # step11, когда 'пиксель_атрибуц' ещё НЕ долит в big_analytics_full →
            # пиксель-ветка BFA читает пустоту. Здесь, ПОСЛЕ step11 (пиксель_атрибуц
            # уже в BAF) и финализации BAF (normalize/prefix/compactify), повторно
            # строим BFA — теперь пиксель-визит ветка видит дробную атрибуцию и
            # раскладывает её по реальной eff_arrival_date. step13 идемпотентен
            # (DROP+CREATE), raw_leads для пиксель-ветки не нужна (читает BAF+local).
            try:
                # SKIP_ON_FAILED_2026-07-12: предыдущий критичный шаг (step11) уронил failed —
                # НЕ пересобираем arrival (иначе неполные данные доедут до build_unified/star).
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
                    # STEP_NOTIFY_BATCH_2026-06-18
                    _step_notify('step13_arrival (rebuild)', _s13_elapsed, True)
                    log_step(_log_conn2, run_id, 'step13_rebuild', 'ok',
                             rows_affected=_s13_res.get('rows'),
                             duration_sec=round(_s13_elapsed, 2),
                             details=_s13_res.get('details'))
                finally:
                    db_module.put_conn(s13_conn)
            except _PriorFailureSkip:
                logger.error('SKIP_ON_FAILED: step13_rebuild ПРОПУЩЕН — предыдущий критичный '
                             'шаг уронил failed (run_id=%s); big_analytics_full_arrival '
                             'НЕ перезаписываем неполными данными', run_id)
                log_step(_log_conn2, run_id, 'step13_rebuild', 'skipped',
                         details='пропущено из-за предыдущего сбоя (failed=True)')
            except Exception as e:
                # FAIL_ON_CRITICAL_STEP_2026-07-12 (зеркало pipeline.py): step13-rebuild строит
                # финальную big_analytics_full_arrival. Провал = пустой/устаревший arrival.
                logger.error('Ошибка в пересборке step13_arrival после step11 (критично): %s', e)
                log_step(_log_conn2, run_id, 'step13_rebuild', 'error', details=str(e))
                failed = True

            # ── FIX-NORM-ARRIVAL: normalize_salons на ФИНАЛЬНОЙ arrival ──────────
            # (после rebuild, до unified; синхрон с pipeline.py L899-912)
            # build_unified читает big_analytics_full_arrival и требует нормализованный
            # "салон" (TREATAS в PBI по салону). Раньше нормализация arrival стояла ДО
            # rebuild (блок normalize_salons выше) и терялась — теперь применяется к
            # ФИНАЛЬНОЙ таблице, пересобранной step13_rebuild.
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
            # по дате визита независимо от big_analytics_full → специалист NULL.
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
                logger.exception('Ошибка в spec_fallback_v3(arrival): %s', e)

            # P2_STEP13_SINGLE_CALL_2026-06-18: освобождаем raw_leads ТОЛЬКО ЗДЕСЬ —
            # после единственного step13_rebuild, который является последним потребителем
            # raw_leads (ветки 1-3 лиды/звонки/посевы). Пиксель-ветка (4) видит
            # долитый step11 'пиксель_атрибуц'. Все 4 ветки собираются на живых данных.
            try:
                _rl_conn = db_module.get_conn()
                try:
                    with _rl_conn.cursor() as _cur:
                        _cur.execute("SELECT to_regclass('public.raw_leads')")
                        if _cur.fetchone()[0] is not None:
                            _cur.execute('TRUNCATE TABLE public.raw_leads')
                            logger.info('Disk free: TRUNCATE raw_leads после step13-rerun')
                    _rl_conn.commit()
                finally:
                    db_module.put_conn(_rl_conn)
            except Exception as e:
                logger.warning('Disk free: TRUNCATE raw_leads не удался (не критично): %s', e)

            # ── PREFREE_BEFORE_UNIFIED_2026-06-29: освобождение big_analytics_direct ──
            # big_analytics_direct (~14 GB) уже НЕ нужен ни одному последующему шагу:
            #   step6 → уже завершён (big_analytics_full собран) ✓
            #   step7 / step8 / step11 / step12 / step13 → читают big_analytics_full, не direct ✓
            #   build_unified / build_star → читают big_analytics_unified, не direct ✓
            # raw_yandex (~8 GB) уже TRUNCATED на L493 (RAW_YANDEX_PREFREE после step3),
            # поэтому тут он 0 строк — TRUNCATE is a safe no-op.
            # ПОЧЕМУ ЗДЕСЬ, а не в SPEND_PREFREE ниже:
            #   build_unified DISK_GUARD требует >= 12 GB свободно (CTAS ~6 GB + overhead).
            #   Освобождая big_analytics_direct ДО build_unified, мы добавляем ~14 GB headroom,
            #   предотвращая повтор инцидента 2026-06-29 ("7.5 GB < 12.0 GB — build_unified skip").
            #   SPEND_PREFREE ниже становится no-op (пустые таблицы) — это допустимо.
            # Маркер: PREFREE_BEFORE_UNIFIED_2026-06-29
            try:
                _pu_conn = db_module.get_conn()
                try:
                    _pu_conn.rollback()
                    _pu_conn.autocommit = True
                    with _pu_conn.cursor() as _pu_cur:
                        for _pu_tbl in ('big_analytics_direct', 'raw_yandex'):
                            _pu_cur.execute(
                                'SELECT to_regclass(%s)', (f'public.{_pu_tbl}',)
                            )
                            if _pu_cur.fetchone()[0] is not None:
                                _pu_cur.execute(f'TRUNCATE TABLE public.{_pu_tbl}')
                                logger.info(
                                    'PREFREE_BEFORE_UNIFIED: TRUNCATE public.%s перед build_unified',
                                    _pu_tbl,
                                )
                finally:
                    _pu_conn.autocommit = False
                    db_module.put_conn(_pu_conn)
            except Exception as _pu_e:
                logger.warning('PREFREE_BEFORE_UNIFIED: не критично, продолжаем: %s', _pu_e)

            # ── big_analytics_unified (MIRROR+UNION финальный шаг) ─────────────
            # Собирается ПОСЛЕ step11 (пиксель_атрибуц уже в BAF), финализации BAF
            # (normalize/prefix/compactify) и BFA (step13)
            # → unified = big_analytics_full ∪ big_analytics_full_arrival + атрибуция.
            # Идемпотентно (DROP+CTAS, ~2 мин). PBI читает unified как партицию
            # big_analytics_full — без этого шага свежие данные fast-прогона
            # не попадают в Power BI.
            try:
                # SKIP_ON_FAILED_2026-07-12: предыдущий критичный шаг уронил failed —
                # НЕ собираем unified (иначе star соберётся на неполном/устаревшем unified).
                if failed:
                    raise _PriorFailureSkip()
                import importlib as _uni_il
                _uni_mod = _uni_il.import_module('step13_arrival.build_unified')
                uni_conn = db_module.get_conn()
                _t_uni = time.perf_counter()
                try:
                    _uni_res = _uni_mod.run(uni_conn, run_id=run_id)
                    _uni_elapsed = time.perf_counter() - _t_uni
                    logger.info('build_unified: %s за %.1f сек',
                                _uni_res.get('details'), _uni_elapsed)
                    step_timings.append(('build_unified', 'build_unified', _uni_elapsed, True))
                    # STEP_NOTIFY_BATCH_2026-06-18
                    _step_notify('build_unified', _uni_elapsed, True)
                    log_step(_log_conn2, run_id, 'build_unified', 'ok',
                             rows_affected=_uni_res.get('rows'),
                             duration_sec=round(_uni_elapsed, 2),
                             details=_uni_res.get('details'))
                    # EXPLAIN_BASELINE_2026-06-17: захват плана build_unified
                    if _explain_log and _explain_cap.EXPLAIN_CAPTURE:
                        try:
                            _esql_uni = getattr(_uni_mod, 'get_explain_sql', None)
                            if _esql_uni is not None:
                                import inspect as _ins_uni
                                _sig_uni = _ins_uni.signature(_esql_uni)
                                _sql_uni = _esql_uni(uni_conn) if len(_sig_uni.parameters) > 0 else _esql_uni()
                                _explain_cap.wrap_explain(uni_conn, _sql_uni, 'build_unified',
                                                 run_id=run_id, log_file=_explain_log, wall_sec=_uni_elapsed)
                        except Exception as _ce_uni:
                            logger.warning('explain_capture build_unified: %s', _ce_uni)
                finally:
                    db_module.put_conn(uni_conn)
            except _PriorFailureSkip:
                logger.error('SKIP_ON_FAILED: build_unified ПРОПУЩЕН — предыдущий критичный '
                             'шаг уронил failed (run_id=%s); big_analytics_unified '
                             'НЕ перезаписываем неполными данными', run_id)
                log_step(_log_conn2, run_id, 'build_unified', 'skipped',
                         details='пропущено из-за предыдущего сбоя (failed=True)')
            except Exception as e:
                # FAIL_ON_CRITICAL_STEP_2026-07-12 (зеркало pipeline.py): build_unified собирает
                # big_analytics_unified (источник star.fact_big_analytics и golden-сверки).
                logger.error('Ошибка в build_unified (критично): %s', e)
                log_step(_log_conn2, run_id, 'build_unified', 'error', details=str(e))
                failed = True

            # ── SPEND_PREFREE_2026-06-18: освобождение диска перед spend-фазой ──
            # spend_staging делает единый скан FDW 18.9M строк с GROUP BY по 9-колонному
            # ключу → PostgreSQL не может держать весь sort в work_mem (192MB) → spill
            # в pgsql_tmp. При наличии big_analytics_direct (~5-14 GB) + raw_yandex (~8 GB)
            # на диске суммарно 23-28 GB занято → 4-8 GB свободно → temp-spill убивает диск.
            # После build_unified оба объекта НЕ нужны больше ни одному последующему шагу:
            #   build_unified → уже завершён ✓
            #   build_star → читает big_analytics_unified (не direct/raw_yandex) ✓
            #   step8 → перенесён ВЫШЕ (EARLY_TRUNCATE_DIRECT_FAST на L994 делает это повторно
            #             уже после step8; but здесь step8 ещё не запускался — это post-loop
            #             секция, step8 в fast_pipeline вызывается ПОСЛЕ этого блока)
            #   verify/golden → читает big_analytics_unified ✓
            # КРИТИЧНО: raw_yandex (~8 GB) тоже освобождаем здесь — EARLY_TRUNCATE на L994
            # не покрывал его; блок Disk free raw_yandex после step8 (L1092) теперь
            # сработает как no-op (уже пустой — OK).
            # EARLY_TRUNCATE_DIRECT_FAST (L994) сохраняем — он делает TRUNCATE direct
            # ещё раз уже после step8; to_regclass-проверка гарантирует что повторный
            # TRUNCATE пустой таблицы безопасен (no-op по строкам, минимальный overhead).
            # Маркер: SPEND_PREFREE_2026-06-18
            try:
                _spf_conn = db_module.get_conn()
                try:
                    _spf_conn.rollback()
                    _spf_conn.autocommit = True
                    with _spf_conn.cursor() as _spf_cur:
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
            # Дневной fast_pipeline их НЕ строит — они обновляются дневным job (14:00 Екб).
            # SPEND_PREFREE выше (TRUNCATE direct + raw_yandex) сохранён — освобождает
            # ~22 GB перед build_region_zayavki и build_star.
            logger.info(
                'SPEND_NIGHT_JOB_2026-06-27: spend-витрины пропущены в дневном pipeline; '
                'обновляются отдельным job build_spend_daily.py (14:00 Екб / 09:00 UTC)'
            )

            # ── build_region_zayavki — датамарт «воронка по заявкам в разрезе региона» ─
            # ПОСЛЕ build_criterion_spend, ДО build_star. Отдельная таблица
            # public.fact_region_zayavki (DROP+CTAS) из public.local_leads_all по грани
            # created_date×campaign_id×id_location — golden Кудерко НЕ затрагивает.
            try:
                import importlib as _rz_il_mod
                _rz_il = _rz_il_mod.import_module('region_spend.build_region_zayavki')
                _rz_conn = db_module.get_conn()
                _t_rz = time.perf_counter()
                try:
                    _rz_res = _rz_il.run(_rz_conn, run_id=run_id)
                    _rz_elapsed = time.perf_counter() - _t_rz
                    logger.info('build_region_zayavki: %s за %.1f сек',
                                _rz_res.get('details'), _rz_elapsed)
                    step_timings.append(('build_region_zayavki', 'build_region_zayavki',
                                         _rz_elapsed, True))
                    # STEP_NOTIFY_BATCH_2026-06-18
                    _step_notify('build_region_zayavki', _rz_elapsed, True)
                    log_step(_log_conn2, run_id, 'build_region_zayavki', 'ok',
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
            # created_date×campaign_id×criterion — golden Кудерко НЕ затрагивает.
            try:
                import importlib as _cz_il_mod
                _cz_il = _cz_il_mod.import_module('criterion_spend.build_criterion_zayavki')
                _cz_conn = db_module.get_conn()
                _t_cz = time.perf_counter()
                try:
                    _cz_res = _cz_il.run(_cz_conn, run_id=run_id)
                    _cz_elapsed = time.perf_counter() - _t_cz
                    logger.info('build_criterion_zayavki: %s за %.1f сек',
                                _cz_res.get('details'), _cz_elapsed)
                    step_timings.append(('build_criterion_zayavki', 'build_criterion_zayavki',
                                         _cz_elapsed, True))
                    # STEP_NOTIFY_BATCH_2026-06-18
                    _step_notify('build_criterion_zayavki', _cz_elapsed, True)
                    log_step(_log_conn2, run_id, 'build_criterion_zayavki', 'ok',
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
                import importlib as _dc_il_mod
                _dc_il = _dc_il_mod.import_module('criterion_spend.build_dim_criterion')
                _dc_conn = db_module.get_conn()
                _t_dc = time.perf_counter()
                try:
                    _dc_res = _dc_il.run(_dc_conn, run_id=run_id)
                    _dc_elapsed = time.perf_counter() - _t_dc
                    logger.info('build_dim_criterion: %s за %.1f сек',
                                _dc_res.get('details'), _dc_elapsed)
                    step_timings.append(('build_dim_criterion', 'build_dim_criterion',
                                         _dc_elapsed, True))
                    # STEP_NOTIFY_BATCH_2026-06-18
                    _step_notify('build_dim_criterion', _dc_elapsed, True)
                    log_step(_log_conn2, run_id, 'build_dim_criterion', 'ok',
                             rows_affected=_dc_res.get('rows'),
                             duration_sec=round(_dc_elapsed, 2),
                             details=_dc_res.get('details'))
                finally:
                    db_module.put_conn(_dc_conn)
            except Exception as e:
                logger.warning('Ошибка в build_dim_criterion: %s', e)

            # ── build_star — пересборка star-схемы (Этап 1 миграции на звезду) ──
            # ПОСЛЕ build_unified: star.fact_big_analytics — проекция big_analytics_unified,
            # star.arp_fact — проекция analytics_report_placement. Отдельный subprocess
            # (build_star.py — самостоятельный скрипт). PBI Этапа 3 репойнтит партиции.
            try:
                # SKIP_ON_FAILED_2026-07-12: предыдущий критичный шаг уронил failed —
                # НЕ пересобираем star. build_star (subprocess DROP+CTAS) КОММИТИТ в durable
                # public.fact_big_analytics НЕМЕДЛЕННО → живые потребители получили бы неполноту.
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
                # STEP_NOTIFY_BATCH_2026-06-18
                _step_notify('build_star', _star_elapsed, True)
                _star_conn = db_module.get_conn()
                try:
                    log_step(_star_conn, run_id, 'build_star', 'ok',
                             duration_sec=round(_star_elapsed, 2))
                finally:
                    db_module.put_conn(_star_conn)
            except _PriorFailureSkip:
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
                # FAIL_ON_CRITICAL_STEP_2026-07-12 (зеркало pipeline.py): build_star (subprocess
                # check=True) пересобирает durable fact_big_analytics — витрину refresh-гейта и PBI.
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
        finally:
            db_module.put_conn(_log_conn2)

    # ── step8 (статистика + Telegram) — выполняется ПОСЛЕДНИМ ───────────────
    if not failed and (args.only_step is None or args.only_step == 8):
        s8_num, s8_name, s8_module, _ = FAST_STEP8_INFO
        # WALLTIME_FIX_2026-06-18: передаём фактическое wall-clock время прогона в step8,
        # чтобы блок «Время выполнения» показывал реальное время (не сумму шагов).
        # elapsed_total вычисляется ниже (после step8) — берём wall здесь (без ~1 мин step8).
        _pipeline_wall_sec = (datetime.now() - started_at).total_seconds()
        ok, elapsed = run_step(s8_num, s8_module, run_id, _explain_log=_explain_log,
                               pipeline_wall_sec=_pipeline_wall_sec,
                               pipeline_degraded=degraded,
                               degraded_steps=degraded_steps)
        step_timings.append((s8_num, s8_name, elapsed, ok))
        if not ok:
            failed = True

    # ── EARLY_TRUNCATE_DIRECT_FAST_2026-06-18 ────────────────────────────────
    # big_analytics_direct (~14 GB) освобождается ЗДЕСЬ — сразу после step8,
    # который является последним читателем direct в fast_pipeline.
    # Хронология читателей direct:
    #   step3 → пишет (CTAS big_analytics_direct) ✓
    #   corrections.apply() → UPDATE big_analytics_direct ✓
    #   VACUUM after corrections (L357) → big_analytics_direct ✓
    #   cleanup_old_dates (L447) → DELETE FROM big_analytics_direct ✓
    #   step12_bg (L636) → читает big_analytics_direct; join() до step8 ✓
    #   step8 (статистика, L987) → T_DIRECT: сверка расходов + кампании ✓
    # После step8 direct больше НЕ нужен:
    #   verify/golden-лог → big_analytics_unified (не direct) ✓
    #   build_star/build_unified → завершены выше step8 ✓
    #   raw_yandex TRUNCATE → идёт ниже (независим) ✓
    #   финальный _cleanup_tables → убрать big_analytics_direct (нет двойного TRUNCATE) ✓
    # Аналог: pipeline.py EARLY_TRUNCATE_DIRECT_RAW_2026-06-17 (L1350-1375).
    # raw_yandex освобождается отдельным блоком ниже (L1058) — не трогаем здесь.
    if not failed and args.only_step is None:
        try:
            _etd_conn = db_module.get_conn()
            try:
                _etd_conn.rollback()
                _etd_conn.autocommit = True
                with _etd_conn.cursor() as _etd_cur:
                    _etd_cur.execute("SELECT to_regclass('public.big_analytics_direct')")
                    if _etd_cur.fetchone()[0] is not None:
                        _etd_cur.execute('TRUNCATE TABLE public.big_analytics_direct')
                        logger.info('EARLY_TRUNCATE: освобождена public.big_analytics_direct (~14 GB)')
            finally:
                _etd_conn.autocommit = False
                db_module.put_conn(_etd_conn)
        except Exception as _etd_e:
            logger.warning('EARLY_TRUNCATE_DIRECT_FAST: не критично, продолжаем: %s', _etd_e)

    # ── verify_big_analytics — проверки качества после финализации ────────────
    if not failed and args.only_step is None:
        _vba_script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   'data_check', 'verify_big_analytics.py')
        _t_vba = time.perf_counter()
        try:
            subprocess.run([sys.executable, _vba_script, '--tg'], check=True, timeout=120)
            _vba_elapsed = time.perf_counter() - _t_vba
            logger.info('verify_big_analytics: завершён за %.1f сек', _vba_elapsed)
            step_timings.append(('verify_big_analytics', 'verify_big_analytics', _vba_elapsed, True))
        except subprocess.CalledProcessError as _e:
            _vba_elapsed = time.perf_counter() - _t_vba
            logger.warning('verify_big_analytics: проверки не прошли (код %d)', _e.returncode)
            step_timings.append(('verify_big_analytics', 'verify_big_analytics', _vba_elapsed, False))
        except Exception as _e:
            logger.warning('verify_big_analytics: ошибка запуска: %s', _e)

    # ── golden-лог Кудерко в data_quality_log (синхрон с pipeline.py) ──
    # Снимаем golden-числа Кудерко из big_analytics_unified, пока unified жив, и
    # пишем в data_quality_log — персистентный след для аудита (методика
    # GOLDEN_BASELINE.md: unified, специалист='Кудерко Семен', По дате заявки,
    # БЕЗ датного фильтра, источники Я.Директ без пикселя; эталон расход 25422774.00
    # ±100 ₽ / продажи floor≥54). Допуск ±100 ₽ (2026-06-17) = консистентность с
    # verify_big_analytics.py GOLDEN_COST_TOL; пиксельный дрейф by-design +12 ₽.
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
                          AND _source_table IN ('direct','tp8','seo','calls','direct_unmatched','direct_zero')
                    """)
                    _grow = _gc.fetchone()
                _gld_conn.rollback()
                _g_rashod = _grow[0] if _grow else None
                _g_prod   = _grow[1] if _grow else None
                _G_RASHOD_REF, _G_PROD_REF, _G_RASHOD_TOL = 25422774.00, 54, 100.00
                _rashod_ok = (_g_rashod is not None
                              and abs(float(_g_rashod) - _G_RASHOD_REF) <= _G_RASHOD_TOL)
                # продажи — растущая метрика (дозревание CRM), floor а не exact; 2026-06-15
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

    # Освобождаем raw_yandex — step8 (статистика/TG) уже завершён, дальше raw_yandex не нужен
    if args.only_step is None:
        try:
            _ry_conn = db_module.get_conn()
            try:
                with _ry_conn.cursor() as _cur:
                    _cur.execute("SELECT to_regclass('public.raw_yandex')")
                    if _cur.fetchone()[0] is not None:
                        _cur.execute('TRUNCATE TABLE public.raw_yandex')
                        logger.info('Disk free: TRUNCATE raw_yandex после step8')
                _ry_conn.commit()
            finally:
                db_module.put_conn(_ry_conn)
        except Exception as e:
            logger.warning('Disk free: TRUNCATE raw_yandex не удался (не критично): %s', e)

    elapsed_total = (datetime.now() - started_at).total_seconds()

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
            f'❌ <b>fast_pipeline ОШИБКА</b>\n'
            f'<code>{datetime.now().strftime("%d.%m.%Y %H:%M")}</code>  '
            f'run_id: <code>{run_id}</code>\n'
            f'Упал на шаге: <b>{failed_step}</b>\n'
            f'<code>{err_text}</code>'
        )

    # Освобождение диска: TRUNCATE промежуточных таблиц после успешного запуска.
    # big_analytics_full и big_analytics_full_arrival не трогаем — PBI читает их между запусками.
    #
    # ЗАЩИТНАЯ ПРОВЕРКА (guard): TRUNCATE промежуточных таблиц выполняется ТОЛЬКО если
    # ОБЕ финальные таблицы (big_analytics_full + big_analytics_full_arrival) существуют
    # И непустые. Иначе мы рискуем безвозвратно обнулить промежуточные данные при том что
    # финальные по какой-то причине не собрались — PBI останется без данных и восстановить
    # их будет нечем до следующего полного прогона.
    if not failed:
        _final_tables = ['big_analytics_full', 'big_analytics_full_arrival']
        # raw_* таблицы также включаем — в fast_pipeline они больше не нужны после step8.
        # raw_yandex уже мог быть очищен выше (блок Disk free после step8) — to_regclass-проверка
        # в цикле ниже безопасно пропустит уже-пустые таблицы.
        _cleanup_tables = [
            # big_analytics_direct перенесён в EARLY_TRUNCATE_DIRECT_FAST_2026-06-18
            # (сразу после step8 — освобождаем раньше, до verify/golden-лога)
            'big_analytics_seo', 'big_analytics_reviews', 'big_analytics_pixel',
            'big_analytics_crop_targeting', 'pixel_leads', 'pixel_leads_check',
            'raw_yandex', 'raw_leads', 'raw_calls', 'raw_domains',
        ]
        _t_cl = time.perf_counter()
        try:
            _cl_conn = db_module.get_conn()
            try:
                # Шаг 1: проверка финальных таблиц (существуют + непустые)
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
                    # Финальные таблицы не готовы → НЕ обнуляем промежуточные
                    _msg = (f'Cleanup ПРОПУЩЕН: финальные таблицы не готовы '
                            f'(требуется непустые big_analytics_full + big_analytics_full_arrival). '
                            f'Состояние: {_finals_desc}')
                    logger.warning(_msg)
                    log_step(_cl_conn, run_id, 'cleanup_intermediate', 'skipped',
                             details=_msg)
                    _send_tg(
                        '⚠️ <b>fast_pipeline</b>: cleanup промежуточных ПРОПУЩЕН (финальные таблицы пустые)\n'
                        f'Финальные таблицы не готовы: <code>{_finals_desc}</code>\n'
                        f'run_id: <code>{run_id}</code>'
                    )
                else:
                    # Шаг 2: финальные таблицы готовы → TRUNCATE промежуточных
                    _truncated_names: list = []
                    with _cl_conn.cursor() as _cur:
                        for _t in _cleanup_tables:
                            # TRUNCATE TABLE IF EXISTS — невалидный PG-синтаксис, проверяем через to_regclass
                            _cur.execute("SELECT to_regclass(%s)", (f'public.{_t}',))
                            if _cur.fetchone()[0] is not None:
                                _cur.execute(f'TRUNCATE TABLE public.{_t}')
                                _truncated_names.append(_t)
                    _cl_conn.commit()
                    _cl_elapsed = time.perf_counter() - _t_cl
                    _cl_msg = (f'Финальные таблицы OK ({_finals_desc}); '
                               f'TRUNCATE {len(_truncated_names)} промежуточных: '
                               f'{", ".join(_truncated_names)}')
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

    if degraded and not failed:
        try:
            _deg_conn = db_module.get_conn()
            try:
                _deg_details = (
                    'Основной fact_big_analytics собран, но отдельные spend-витрины '
                    f'не обновились: {", ".join(degraded_steps)}'
                )
                log_step(_deg_conn, run_id, 'pipeline_degraded', 'warning',
                         rows_affected=len(degraded_steps), details=_deg_details)
            finally:
                db_module.put_conn(_deg_conn)
        except Exception as _deg_e:
            logger.warning('pipeline_degraded: не удалось записать статус: %s', _deg_e)

    db_module.close_pool()
    db_module.close_src_pool()

    # POST статуса запуска в dashboard (https://seoadvanced.ru/api/project-health)
    try:
        _failed_steps = [s for s, _, _, ok in step_timings if not ok]
        _health_payload = json.dumps({
            "status": "err" if failed else ("degraded" if degraded else "ok"),
            "pipeline_step": "fast_pipeline",
            "msg": (
                f"run_id={run_id} "
                f"({'ошибка: ' + ', '.join(_failed_steps) if failed else ('degraded: ' + ', '.join(degraded_steps) if degraded else 'успешно')}) "
                f"за {elapsed_total:.0f}с"
            ),
            "duration_sec": round(elapsed_total, 1),
            "errors": _failed_steps,
            "warnings": degraded_steps,
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
    elif degraded:
        logger.warning('ЗАВЕРШЕНО DEGRADED за %.0f сек (run_id=%s; spend=%s)',
                       elapsed_total, run_id, ', '.join(degraded_steps))
        try:
            _send_tg(
                f'⚠️ <b>fast_pipeline DEGRADED</b> за {_fmt_dur(elapsed_total)}\n'
                f'Основной fact_big_analytics собран, но spend-витрины не обновились:\n'
                f'<code>{", ".join(degraded_steps)}</code>\n'
                f'run_id: <code>{run_id}</code>'
            )
        except Exception as _fin_e:
            logger.warning('STEP_NOTIFY degraded-финал: %s', _fin_e)
    else:
        logger.info('УСПЕШНО завершено за %.0f сек (run_id=%s)', elapsed_total, run_id)
        # STEP_NOTIFY_BATCH_2026-06-18: финальное SUCCESS-сообщение (гарантированное, вне счётчика)
        try:
            _send_tg(
                f'OK  <b>fast_pipeline УСПЕШНО</b> за {_fmt_dur(elapsed_total)}\n'
                f'run_id: <code>{run_id}</code>'
            )
        except Exception as _fin_e:
            logger.warning('STEP_NOTIFY финал: %s', _fin_e)


if __name__ == '__main__':
    main()
