#!/usr/bin/env python3
"""
step_cron_night/build_spend_daily.py — дневной job (14:00 Екб / 09:00 UTC): сборка 3 spend-витрин.

SPEND_NIGHT_JOB_2026-06-27

Вынесен из дневного fast_pipeline.py / pipeline.py в отдельный job.
Запускается раз в сутки в 14:00 Екб (09:00 UTC) отдельным cron-entry.

Cron (Victory VPS, UTC):
  # spend-витрины — 14:00 Екб = 09:00 UTC (UTC+5)
  0 9 * * *  cd ~/big_analytics_v5 && ~/venv/bin/python3 step_cron_night/build_spend_daily.py >> /tmp/build_spend_daily.log 2>&1

Строит три витрины:
  - public.fact_region_spend    (~7 GB, ~860k строк/день, id_location-ось)
  - public.fact_adformat_spend  (~1.5 GB, ad_format-ось)
  - public.fact_criterion_spend (~2.6 GB, criterion-ось)

Режимы disk-guard (выбирается автоматически по свободному месту):
  >= 35 GB → staging: единый FDW-скан → _spend_staging_tmp → 3 роллапа → DROP staging
             (1 проход FDW вместо 3; суммарный пик ~20 GB)
  10–35 GB → sequential: 3 билдера по очереди, прямой FDW (~7 GB temp-spill на каждый)
  <  10 GB → WARNING + skip (витрины остаются от предыдущего прогона, не fatal)

Зависимость build_star от fact_region_spend:
  build_star.py читает fact_region_spend для построения Dim_Location + Dim_Distance.
  fact_big_analytics и golden Кудерко (расход 25 422 774 / продажи >= 54) НЕ зависят
  от этих витрин. Для получения свежих данных того же дня build_star (и основной pipeline)
  должен запускаться ПОСЛЕ завершения этого job (не ранее 14:30 Екб / 09:30 UTC).

Лог: /tmp/build_spend_daily.log (cron дописывает >>).
"""

import logging
import os  # PIPELINE_GUARD_2026-07-03
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo  # TG_START_FINISH_EKB_2026-06-27

_TZ_EKB = ZoneInfo('Asia/Yekaterinburg')

# Корень big_analytics_v5 (на уровень выше step_cron_night/)
BASE_DIR = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(BASE_DIR))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger('build_spend_daily')

# ── disk-guard пороги ─────────────────────────────────────────────────────────
# >= 35 GB → staging (безопасен: 1 FDW-скан ~12 GB + staging ~5 GB + 1 роллап ~5 GB)
# 10-35 GB → sequential (1 FDW-скан за раз: ~7 GB temp-spill, после DROP ~7 GB назад)
# <  10 GB → skip + warning
MIN_FREE_GB_SPEND_STAGING    = 35.0
MIN_FREE_GB_SPEND_SEQUENTIAL = 10.0

# PIPELINE_GUARD_2026-07-03: максимум ожидания и интервал опроса (в секундах)
GUARD_WAIT_MAX_SEC = 30 * 60   # 30 мин — обычно pipeline заканчивается раньше
GUARD_POLL_SEC     = 2 * 60    # проверяем каждые 2 мин

# Три spend-билдера в порядке запуска
_SPEND_BUILDERS = [
    ('build_region_spend',    'region_spend.build_region_spend'),
    ('build_adformat_spend',  'adformat_spend.build_adformat_spend'),
    ('build_criterion_spend', 'criterion_spend.build_criterion_spend'),
]


def _find_running_pipeline_pid() -> int | None:
    """PIPELINE_GUARD_2026-07-03: ищет живой процесс pipeline.py / fast_pipeline.py /
    pipeline_powerbi.py в /proc (Linux). Исключает собственный PID (build_spend_daily.py).
    Возвращает PID первого найденного или None.
    """
    _targets = ('pipeline.py', 'fast_pipeline.py', 'pipeline_powerbi.py')
    _my_pid = os.getpid()
    for _pid_str in os.listdir('/proc'):
        if not _pid_str.isdigit():
            continue
        _pid = int(_pid_str)
        if _pid == _my_pid:
            continue
        try:
            with open(f'/proc/{_pid}/cmdline', 'rb') as _f:
                _cmd = _f.read().decode('utf-8', errors='replace').replace('\x00', ' ')
            if any(t in _cmd for t in _targets):
                return _pid
        except (FileNotFoundError, PermissionError):
            pass
    return None


def _free_disk_gb() -> float:
    return shutil.disk_usage('/').free / (1024 ** 3)


def _send_tg(msg: str) -> None:
    """Отправить TG через _send_tg из pipeline.py (тот же proxy/токен)."""
    try:
        from pipeline import _send_tg as _ptg
        _ptg(msg)
    except Exception as e:
        logger.warning('TG send failed: %s', e)


def _run_one(name: str, module_path: str, conn, use_staging: bool = False) -> dict:
    """
    Запустить один spend-билдер на переданном соединении.
    use_staging=True → билдер читает из _spend_staging_tmp (если она существует).
    use_staging=False → билдер сам идёт в FDW yandex_direct_manager_reports.
    """
    import importlib
    mod = importlib.import_module(module_path)
    t0 = time.perf_counter()
    try:
        res = mod.run(conn, run_id='spend_daily', use_staging=use_staging)
        elapsed = time.perf_counter() - t0
        logger.info('%s OK за %.1f сек — %s', name, elapsed, res.get('details', ''))
        return {'name': name, 'ok': True, 'elapsed': elapsed,
                'rows': res.get('rows', 0), 'details': res.get('details', '')}
    except Exception as e:
        elapsed = time.perf_counter() - t0
        logger.warning('%s ОШИБКА (%.1f сек): %s', name, elapsed, e)
        try:
            conn.rollback()
        except Exception:
            pass
        return {'name': name, 'ok': False, 'elapsed': elapsed, 'rows': 0, 'details': str(e)}


def main() -> None:
    # PIPELINE_GUARD_2026-07-03: не стартовать, пока идёт прогон pipeline.
    # Мотивация: build_spend_daily DROP+CTAS fact_criterion_spend конфликтует
    # с pipeline.py::build_dim_criterion (читает ту же таблицу).
    # Ждём до GUARD_WAIT_MAX_SEC, проверяем каждые GUARD_POLL_SEC.
    # Если pipeline не завершился — TG-уведомление и выход без ошибки (exit 0).
    _gpid = _find_running_pipeline_pid()
    if _gpid is not None:
        logger.info(
            '=== PIPELINE_GUARD: обнаружен прогон pipeline PID=%d — жду до %d мин ===',
            _gpid, GUARD_WAIT_MAX_SEC // 60,
        )
        _g0 = time.monotonic()
        while _gpid is not None:
            _waited = time.monotonic() - _g0
            if _waited >= GUARD_WAIT_MAX_SEC:
                _msg = (
                    f'build_spend_daily ПРОПУЩЕН: pipeline PID={_gpid} '
                    f'не завершился за {GUARD_WAIT_MAX_SEC // 60} мин. '
                    f'Витрины fact_*_spend остаются от предыдущего прогона.'
                )
                logger.warning('PIPELINE_GUARD timeout: %s', _msg)
                _send_tg(f'⚠️ <b>build_spend_daily SKIP</b> (pipeline guard)\n{_msg}')
                return
            logger.info(
                'PIPELINE_GUARD: PID=%d жив (ждём %.0f с), пауза %d с',
                _gpid, _waited, GUARD_POLL_SEC,
            )
            time.sleep(GUARD_POLL_SEC)
            _gpid = _find_running_pipeline_pid()
        logger.info('PIPELINE_GUARD: pipeline завершился — продолжаю')

    # ── PIPELINE_MUTEX_2026-07-12: после PIPELINE_GUARD (дождались pipeline*.py)
    # берём общий flock группы — взаимное исключение с report_placement и закрытие
    # гонки с pipeline_powerbi/fast_pipeline, которую сканер процессов мог пропустить.
    # НЕблокирующе: занято другим из группы → skip (exit 0), не висим.
    # Ставится ПОСЛЕ guard-ожидания, чтобы сохранить существующее поведение
    # «дождись pipeline, потом стартуй» (guard) и добавить сверху взаимное исключение.
    import pipeline_mutex
    try:
        _mutex_fd = pipeline_mutex.acquire('build_spend_daily')  # noqa: F841 — держим fd весь прогон
    except pipeline_mutex.PipelineBusy as _busy:
        _msg = f'build_spend_daily ПРОПУЩЕН: активен другой прогон группы ({_busy})'
        logger.warning('PIPELINE_MUTEX: %s', _msg)
        _send_tg(f'⚠️ <b>build_spend_daily SKIP</b> (pipeline mutex)\n{_msg}')
        return

    # TG_START_FINISH_EKB_2026-06-27: старт + финиш в Telegram, время Екатеринбург
    t_total = time.perf_counter()
    start_ekb = datetime.now(_TZ_EKB)
    start = datetime.now()

    # ── TG: старт ─────────────────────────────────────────────────────────────
    _send_tg(
        f'🚀 <b>build_spend_daily</b> стартовал '
        f'{start_ekb.strftime("%H:%M %d.%m.%Y")} (Екб)'
    )
    logger.info('=== build_spend_daily START %s ===', start.strftime('%Y-%m-%d %H:%M:%S'))

    import config.db as db_module
    db_module.init_pool()

    results: list[dict] = []
    use_staging_mode = False
    _fatal_error: str | None = None  # None = нормальный выход / skip; str = падение

    try:
        free_gb = _free_disk_gb()
        logger.info('Свободно диска: %.1f GB', free_gb)

        # ── disk-guard ────────────────────────────────────────────────────────
        if free_gb < MIN_FREE_GB_SPEND_SEQUENTIAL:
            msg = (
                f'build_spend_daily: пропущено — диска {free_gb:.1f} GB '
                f'< {MIN_FREE_GB_SPEND_SEQUENTIAL:.0f} GB. '
                f'fact_*_spend остаются от предыдущего прогона.'
            )
            logger.warning(msg)
            _send_tg(f'⚠️ <b>build_spend_daily SKIP</b>\n{msg}')
            return  # finally всё равно выполнится (results=[], _fatal_error=None → TG не дублируется)

        if free_gb >= MIN_FREE_GB_SPEND_STAGING:
            logger.info(
                'РЕЖИМ: staging (%.1f GB >= %.0f GB) — единый FDW-скан → 3 роллапа',
                free_gb, MIN_FREE_GB_SPEND_STAGING
            )
            # ensure_staging: DROP старого + CREATE UNLOGGED + единый INSERT FDW
            from spend import build_spend_staging as _stg
            stg_conn = db_module.get_conn()
            try:
                t_stg = time.perf_counter()
                stg_rows = _stg.ensure_staging(stg_conn)
                logger.info(
                    'STAGING готово за %.1f сек, %d строк (1 FDW-скан)',
                    time.perf_counter() - t_stg, stg_rows
                )
                use_staging_mode = True
            except Exception as e_stg:
                logger.warning(
                    'STAGING: не удалось создать (%s) — переход на sequential из FDW', e_stg
                )
                try:
                    stg_conn.rollback()
                except Exception:
                    pass
                use_staging_mode = False
            finally:
                db_module.put_conn(stg_conn)
        else:
            logger.info(
                'РЕЖИМ: sequential из FDW (%.1f GB, 10–35 GB) — 3 билдера по очереди '
                '(~7 GB temp-spill каждый, но sequential → пик не складывается)',
                free_gb
            )

        # ── 3 билдера последовательно ─────────────────────────────────────────
        for name, module_path in _SPEND_BUILDERS:
            conn = db_module.get_conn()
            try:
                r = _run_one(name, module_path, conn, use_staging=use_staging_mode)
            finally:
                db_module.put_conn(conn)
            results.append(r)

        # ── DROP staging (всегда в finally, если staging был создан) ─────────
        if use_staging_mode:
            drop_conn = db_module.get_conn()
            try:
                from spend import build_spend_staging as _stg
                _stg.drop_staging(drop_conn)
            except Exception as e_drop:
                logger.warning('STAGING DROP: %s', e_drop)
                try:
                    drop_conn.rollback()
                except Exception:
                    pass
            finally:
                db_module.put_conn(drop_conn)

    except Exception as _exc:
        _fatal_error = str(_exc)
        logger.error('FATAL: %s', _fatal_error, exc_info=True)
        raise  # пере-бросаем — finally всё равно выполнится

    finally:
        # ── close pool ────────────────────────────────────────────────────────
        try:
            db_module.close_pool()
        except Exception as _e:
            logger.warning('close_pool: %s', _e)

        # ── TG: финиш (всегда — успех / skip / падение) ───────────────────────
        elapsed_total = time.perf_counter() - t_total
        end_ekb = datetime.now(_TZ_EKB)

        if _fatal_error is not None:
            # ПАДЕНИЕ: raise уже выброшен выше, финиш-отбивка отправляется из finally
            _send_tg(
                f'❌ <b>build_spend_daily</b> упал за {elapsed_total:.0f}с\n'
                f'{end_ekb.strftime("%H:%M")} Екб\n'
                f'{_fatal_error[:300]}'
            )
        elif results:
            # НОРМАЛЬНЫЙ ФИНИШ: есть результаты билдеров
            ok_cnt  = sum(1 for r in results if r['ok'])
            err_cnt = len(results) - ok_cnt
            lines = [
                f'  {r["name"]}: {"OK" if r["ok"] else "FAIL"} {r["elapsed"]:.0f}с rows={r["rows"]}'
                for r in results
            ]
            icon = '✅' if err_cnt == 0 else '⚠️'
            tg_msg = (
                f'{icon} <b>build_spend_daily</b> завершён за {elapsed_total:.0f}с\n'
                f'витрины: region/criterion/adformat обновлены\n'
                f'OK={ok_cnt} FAIL={err_cnt}\n'
                + '\n'.join(lines)
            )
            logger.info('=== ИТОГ: OK=%d FAIL=%d за %.0f сек ===', ok_cnt, err_cnt, elapsed_total)
            _send_tg(tg_msg)

            # ── SPEND_REFRESH_PBI_2026-07-14: полный успех → триггерим полный refresh PBI ──
            # ТОЛЬКО при err_cnt == 0 (все 3 витрины OK). При FAIL (err_cnt>0),
            # SKIP (results=[] → сюда не заходим) и FATAL (_fatal_error → ветка выше)
            # refresh НЕ запускается.
            # ОТЦЕПЛЁННО (start_new_session): build_spend_daily тут же завершится и
            # освободит PIPELINE_MUTEX, а refresh_powerbi (поллит статус до 60 мин)
            # крутится независимо и сам шлёт свой Telegram про Completed/Failed.
            # НЕ синхронно in-process: mutex держится весь прогон → заблокировал бы
            # report_placement/pipeline на всё время refresh.
            if err_cnt == 0:
                try:
                    import subprocess
                    _refresh_log = open('/tmp/build_spend_refresh.log', 'ab')
                    _refresh_proc = subprocess.Popen(
                        [sys.executable, str(BASE_DIR / 'refresh_powerbi.py')],
                        cwd=str(BASE_DIR),           # корень проекта — loader/config discovery
                        stdout=_refresh_log,
                        stderr=_refresh_log,
                        start_new_session=True,      # detached от build_spend_daily
                    )
                    logger.info(
                        'SPEND_REFRESH_PBI: полный refresh Power BI запущен detached '
                        'PID=%d (лог /tmp/build_spend_refresh.log)', _refresh_proc.pid
                    )
                except Exception as _e_ref:
                    logger.warning('SPEND_REFRESH_PBI: не удалось запустить refresh: %s', _e_ref)
        # else: skip-case — ⚠️ уже отправлено выше, не дублируем


if __name__ == '__main__':
    main()
