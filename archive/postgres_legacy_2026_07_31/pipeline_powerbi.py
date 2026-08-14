#!/usr/bin/env python3
"""
pipeline_powerbi.py — пайплайн big_analytics_v5 с проверкой расходов и триггером Power BI

Порядок:
  1. yandex_direct_checking_report.report.run()
     Если |Σ разница| > 200 000 ₽ → стоп (уведомление в Telegram)
  2. pipeline.main() — полный пересбор big_analytics_*
  3. Триггер обновления датасета в Power BI Service

Запуск:
    ~/venv/bin/python3 pipeline_powerbi.py
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

import requests

# ── Путь к big_analytics_v5 ───────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from config.tokens import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_PROXY, TELEGRAM_PROXY_VARIANTS  # noqa: E402
from refresh_powerbi import refresh_powerbi, _ALL_TABLES as _REFRESH_TABLES  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)

DELTA_THRESHOLD = 200_000.0  # ₽ — максимально допустимая |Σ разница|


# ── Telegram ──────────────────────────────────────────────────────────────────
def _send_telegram(text: str) -> None:  # TG_PROXY_ROTATE_2026-07-03: ротация PROXY_VARIANTS + logger.error при полном отказе
    url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage'
    payload = json.dumps({
        'chat_id': TELEGRAM_CHAT_ID,
        'text': text,
        'parse_mode': 'HTML',
    }).encode()
    for proxies in TELEGRAM_PROXY_VARIANTS:
        try:
            r = requests.post(url, data=payload,
                              headers={'Content-Type': 'application/json'},
                              proxies=proxies, timeout=10)
            if r.status_code == 200:
                return
        except Exception as e:
            logger.warning('Telegram proxy fail (%s): %s', proxies, e)
            continue
    logger.error('TG_SEND_FAIL_2026-07-03: все proxy-варианты отказали, сообщение потеряно')




DASHBOARD_URL = 'http://localhost:5056/api/last-run-summary'
DASHBOARD_LOGS_URL = 'http://localhost:5056/api/logs'


def _post_dashboard_summary(delta: float, rows: int, accounts: int) -> None:
    from datetime import date
    text = (
        f"Сверка расходов Директа: {date.today()}\n"
        f"{rows} строк · {accounts} аккаунтов\n"
        f"Σ разница: {delta:+,.0f} ₽".replace(',', ' ')
    )
    try:
        requests.post(DASHBOARD_URL, json={'text': text}, timeout=3)
    except Exception:
        pass


def _post_dashboard_log(level: str, msg: str) -> None:
    try:
        requests.post(DASHBOARD_LOGS_URL, json={'agent': 'pipeline_powerbi', 'level': level, 'msg': msg}, timeout=3)
    except Exception:
        pass


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    # PIPELINE_MUTEX_2026-07-12: групповой flock берётся НЕ здесь, а НЕПОСРЕДСТВЕННО
    # перед pipeline.main() (см. ниже, MUTEX_ACQUIRE_LATE_2026-07-12). Причина: cost-delta
    # проверка + возможный 30-мин retry-sleep НЕ трогают общие таблицы — держать лок
    # всё это время значит зря блокировать fast_pipeline/build_spend_daily/report_placement.

    # ── Шаг 0: проверка кук Яндекс.Директ (check-first self-healing) ─────────
    # Единый guard в config/cookies.py: сначала ПРОВЕРЯЕМ нашей проверкой
    # (check_all_cookies_strict по 3 менеджерским аккаунтам); только если кука
    # мертва — дёргаем glavpotok (refresh_cookies) и перепроверяем; если и после
    # рефреша мертво — Telegram + стоп. glavpotok вызывается ТОЛЬКО при мёртвой
    # куке (а не безусловно). Должно идти ДО step1/step2, которые используют куки
    # (yandex_direct_checking_report → Grid API + pipeline.main() → step4/step9).
    logger.info('=== Шаг 0: проверка кук ===')
    from config.cookies import ensure_cookies_alive_or_stop, CookiesDeadError
    try:
        ensure_cookies_alive_or_stop(pipeline_name='pipeline_powerbi', send_tg=_send_telegram)
    except (CookiesDeadError, Exception):
        # TG-уведомление уже отправлено внутри guard'а.
        sys.exit(1)

    # ── Шаг 1: проверка расходов ─────────────────────────────────────────────
    logger.info('=== Шаг 1: yandex_direct_checking_report ===')
    from yandex_direct_checking_report.report import run as run_checking
    try:
        total_delta, check_rows, check_accounts = run_checking()
    except Exception as e:
        logger.error('Checking report упал: %s', e)
        _post_dashboard_log('err', f'Checking report упал: {e}')
        _send_telegram(f'❌ pipeline_powerbi: checking report упал\n{e}')
        sys.exit(1)

    _post_dashboard_summary(total_delta, check_rows, check_accounts)

    logger.info('Σ разница: %.2f ₽', total_delta)

    # NEGATIVE_DELTA_PROCEED_2026-07-04: отрицательная Σ = checking_API < manager-отчётов CSD
    # (BI не завышает расходы — лаг обновления; безопасно → WARNING + TG + продолжаем)
    # СТОП срабатывает ТОЛЬКО при total_delta > +DELTA_THRESHOLD (BI завышает расходы)
    if total_delta < 0:
        logger.warning(
            'NEGATIVE_DELTA_PROCEED_2026-07-04: Σ разница отрицательная (%.0f ₽): '
            'checking_API < manager-отчётов CSD — BI не завышает расходы, продолжаем по правилу',
            total_delta,
        )
        _post_dashboard_log(
            'warn',
            f'Сверка: Σ разница отрицательная {total_delta:+,.0f} ₽ — продолжаем по правилу'.replace(',', ' '),
        )
        _send_telegram(
            f'⚠️ Σ разница отрицательная ({total_delta:,.0f} ₽) — продолжаем по правилу\n'
            f'(checking_API < manager-отчётов CSD; пайплайн запущен)'
        )
    elif total_delta > DELTA_THRESHOLD:
        retry_wait = 30 * 60  # 30 минут
        logger.warning(
            'Σ разница %.0f ₽ > порог %.0f ₽ — ждём %d мин, потом повторная проверка',
            total_delta, DELTA_THRESHOLD, retry_wait // 60,
        )
        _post_dashboard_log('warn', f'Сверка: Σ разница {total_delta:+,.0f} ₽ > порог — retry через 30 мин'.replace(',', ' '))
        _send_telegram(
            f'⚠️ pipeline_powerbi: превышен порог расходов\n'
            f'Σ разница = {total_delta:,.0f} ₽ (порог {DELTA_THRESHOLD:,.0f} ₽)\n'
            f'Повторная проверка через 30 минут.'
        )
        time.sleep(retry_wait)

        logger.info('=== Шаг 1 (retry): yandex_direct_checking_report ===')
        try:
            total_delta, check_rows, check_accounts = run_checking()
        except Exception as e:
            logger.error('Checking report (retry) упал: %s', e)
            _post_dashboard_log('err', f'Checking report (retry) упал: {e}')
            _send_telegram(f'❌ pipeline_powerbi: checking report (retry) упал\n{e}')
            sys.exit(1)

        _post_dashboard_summary(total_delta, check_rows, check_accounts)
        logger.info('Σ разница (retry): %.2f ₽', total_delta)

        # NEGATIVE_DELTA_PROCEED_2026-07-04: после retry — отрицательная тоже пропускаем
        if total_delta < 0:
            logger.warning(
                'NEGATIVE_DELTA_PROCEED_2026-07-04: Σ разница (retry) отрицательная (%.0f ₽) — продолжаем по правилу',
                total_delta,
            )
            _post_dashboard_log(
                'warn',
                f'Сверка (retry): Σ разница отрицательная {total_delta:+,.0f} ₽ — продолжаем по правилу'.replace(',', ' '),
            )
            _send_telegram(
                f'⚠️ Σ разница (retry) отрицательная ({total_delta:,.0f} ₽) — продолжаем по правилу'
            )
        elif total_delta > DELTA_THRESHOLD:
            from datetime import date as _date
            _post_dashboard_log(
                'err',
                f'Сверка расходов {_date.today()}: {check_rows} строк · {check_accounts} акк · Σ разница {total_delta:+,.0f} ₽ — СТОП'.replace(',', ' '),
            )
            msg = (
                f'🚨 pipeline_powerbi: СТОП (повторная проверка)\n'
                f'Σ разница = {total_delta:,.0f} ₽ — превышает порог {DELTA_THRESHOLD:,.0f} ₽\n'
                f'Пайплайн не запущен. Проверьте данные вручную.'
            )
            logger.error('Σ разница %.0f ₽ > порог %.0f ₽ (retry) — стоп', total_delta, DELTA_THRESHOLD)
            _send_telegram(msg)
            sys.exit(2)

    logger.info('Σ разница в норме — продолжаем')

    # ── MUTEX_ACQUIRE_LATE_2026-07-12: групповой flock ТОЛЬКО сейчас ─────────
    # После всех cost-delta проверок и retry-sleep — прямо перед тем, как pipeline.main()
    # начнёт трогать общие таблицы. Держим fd весь прогон + refresh (ОС снимет lock на
    # выходе процесса). pipeline.main() вызывается IN-PROCESS → передаём
    # _mutex_already_held=True, чтобы он НЕ брал лок повторно (второй flock тем же
    # процессом через другой fd был бы отклонён → ложный PipelineBusy и срыв прогона).
    import pipeline_mutex
    try:
        _mutex_fd = pipeline_mutex.acquire('pipeline_powerbi')  # noqa: F841 — держим fd весь прогон
    except pipeline_mutex.PipelineBusy as _busy:
        logger.warning(
            'PIPELINE_MUTEX: активен другой прогон группы (%s) — pipeline_powerbi пропущен', _busy
        )
        _post_dashboard_log('warn', f'pipeline_powerbi SKIP: активен другой прогон группы ({_busy})')
        _send_telegram(
            f'⚠️ <b>pipeline_powerbi</b> SKIP: активен другой прогон группы ({_busy})\n'
            f'Power BI не обновлён (другой пайплайн уже пересобирает витрины).'
        )
        sys.exit(0)

    # ── STEP1_FRESHNESS_GATE_2026-07-16: фиксируем время старта pipeline.main() ──
    # Инцидент 2026-07-16: build_star запускался вручную поверх УСТАРЕВШИХ unified-данных.
    # pipeline.main() формально возвращал exit 0 (build_star строил из прошлого прогона),
    # гейты arrival/pixel проходили (данные от предыдущего дня ненулевые) → PBI получал
    # стаей данные. 3 новые кампании 142-avtomir.ru (Jul 13-14) и июльский разрыв Jul 9-15
    # для существующих кампаний (~167 тыс. ₽) молча не попали в BI.
    # ФИХ: запоминаем UTC-момент ДО pipeline.main(); после — проверяем что
    # _step1_freshness.updated_at >= _run_start_utc. Если step1 не запускался в этом
    # прогоне → partial run → PBI НЕ обновляем.
    from datetime import datetime as _datetime, timezone as _timezone
    _run_start_utc = _datetime.now(_timezone.utc)
    logger.info(
        'STEP1_FRESHNESS_GATE: фиксируем run_start=%s UTC',
        _run_start_utc.strftime('%Y-%m-%d %H:%M:%S'),
    )

    # ── Шаг 2: основной пайплайн ─────────────────────────────────────────────
    logger.info('=== Шаг 2: pipeline.main() ===')
    import pipeline as _pipeline
    try:
        _pipeline.main(_mutex_already_held=True)
    except SystemExit as e:
        if e.code != 0:
            logger.error('pipeline.main() завершился с ошибкой (код %s)', e.code)
            _post_dashboard_log('err', f'pipeline.main() упал (код {e.code})')
            _send_telegram(f'❌ pipeline_powerbi: pipeline.main() упал (код {e.code})\nPower BI не обновлён.')
            sys.exit(e.code)
    except Exception as e:
        logger.error('pipeline.main() упал: %s', e)
        _post_dashboard_log('err', f'pipeline.main() упал: {e}')
        _send_telegram(f'❌ pipeline_powerbi: pipeline.main() упал\n{e}\nPower BI не обновлён.')
        sys.exit(1)

    # ── BUILD_UNIFIED_GATE_2026-07-16 (v2 rework): проверяем что build_unified завершился в ЭТОМ прогоне ──
    # Blocker 1 fix: _step1_freshness был НЕВЕРНЫМ артефактом для гейта.
    # Root cause инцидента 07-16: стейл big_analytics_unified от предыдущего прогона.
    # step1 freshness НЕ отражает свежесть unified (шаг step1→unified разрывается partial run).
    # Правильный артефакт: запись data_quality_log (step='build_unified', status='ok')
    # с run_at >= _run_start_utc — прямое доказательство пересборки unified в ЭТОМ прогоне.
    _run_start_naive = _run_start_utc.replace(tzinfo=None)  # naive UTC для сравнения с DB timestamp (без tz)
    logger.info('=== BUILD_UNIFIED_GATE: проверка что build_unified выполнен в этом прогоне ===')
    try:
        import psycopg2 as _psycopg2_s1
        from config.settings import DB_DST as _DB_DST_S1
        _s1_conn = _psycopg2_s1.connect(
            host=_DB_DST_S1['host'],
            port=_DB_DST_S1['port'],
            database=_DB_DST_S1['database'],
            user=_DB_DST_S1['user'],
            password=_DB_DST_S1['password'],
            connect_timeout=30,
        )
        try:
            with _s1_conn.cursor() as _s1c:
                _s1c.execute(
                    "SELECT COUNT(*) FROM public.data_quality_log "
                    "WHERE step = 'build_unified' AND status = 'ok' "
                    "AND run_at >= %s",
                    (_run_start_naive,),
                )
                _buni_count = _s1c.fetchone()[0]
        finally:
            _s1_conn.close()
    except Exception as _s1_e:
        logger.error('BUILD_UNIFIED_GATE: ошибка чтения data_quality_log: %s', _s1_e)
        _send_telegram(
            f'❌ pipeline_powerbi: BUILD_UNIFIED_GATE упал\n{_s1_e}\nPower BI НЕ обновлён (fail-closed).'
        )
        sys.exit(1)

    if _buni_count == 0:
        logger.error(
            'BUILD_UNIFIED_GATE: build_unified НЕ найден в data_quality_log за этот прогон '
            '(run_at >= %s UTC) — ЧАСТИЧНЫЙ ПРОГОН — Power BI НЕ обновляем',
            _run_start_naive.strftime('%Y-%m-%d %H:%M:%S'),
        )
        _post_dashboard_log(
            'err',
            f'BUILD_UNIFIED_GATE FAIL: build_unified не выполнен '
            f'(run_start={_run_start_naive})',
        )
        _send_telegram(
            f'🚨 pipeline_powerbi: ЧАСТИЧНЫЙ ПРОГОН — Power BI НЕ обновлён\n'
            f'build_unified не выполнен в этом прогоне\n'
            f'run_start: {_run_start_naive.strftime("%Y-%m-%d %H:%M:%S")} UTC\n'
            f'big_analytics_unified может быть устаревшим.\n'
            f'Нужен полный перезапуск pipeline_powerbi.py от step0.'
        )
        sys.exit(1)

    logger.info(
        'BUILD_UNIFIED_GATE: OK — build_unified выполнен за этот прогон (%d записей, run_start=%s UTC)',
        _buni_count,
        _run_start_naive.strftime('%Y-%m-%d %H:%M:%S'),
    )

    # ── REFRESH_COMPLETENESS_GATE_2026-07-11: не публиковать НЕПОЛНЫЕ данные ──────
    # Инцидент 2026-07-11 (powerbi_run_20260711_064937.log): step11 ENOSPC 07:55 и
    # step13 ENOSPC 08:09 глушатся в WARNING внутри pipeline.main() (pipeline.py L~1288,
    # L~1365) → main() выходит с кодом 0 → refresh МОЛЧА публикует big_analytics_full_arrival=0
    # и пиксель_атрибуц=0. Refresh НЕ был завязан на гейт непустого arrival (в отличие от
    # cleanup, pipeline.py L~2055, который корректно пропустился).
    # ЭТОТ ГЕЙТ = тот же принцип, что cleanup: refresh идёт ТОЛЬКО если обе финальные оси
    # непусты И step11 отработал (пиксель_атрибуц долит). Иначе — громкий алерт, НЕ публикуем.
    # ── REFRESH_GATE_FRESH_CONN_2026-07-11 ───────────────────────────────────────
    # pipeline.main() в своём finally закрывает DST-пул (config.db.close_pool →
    # _dst_pool.closeall()). Объект пула НЕ обнуляется, у него лишь выставляется
    # closed=True, поэтому предыдущий вызов _db_module.get_conn() здесь падал с
    # "connection pool is closed" → гейт ловил это как ошибку → fail-closed → refresh
    # НЕ публиковался ДАЖЕ при полных данных (run 211654: golden PASS,
    # пиксель_атрибуц=206557, arrival непуст — но BI молча не обновился).
    # Фикс: гейт открывает СВЕЖЕЕ standalone-соединение psycopg2.connect(DB_DST) в обход
    # закрытого пула и закрывает его после проверки. Смысл гейта НЕ ослаблен: неполнота
    # (arrival==0 или пиксель_атрибуц==0) по-прежнему → fail-closed без публикации.
    logger.info('=== Гейт полноты данных перед Power BI refresh ===')
    try:
        import psycopg2 as _psycopg2
        from config.settings import DB_DST as _DB_DST
        _gate_conn = _psycopg2.connect(
            host=_DB_DST['host'],
            port=_DB_DST['port'],
            database=_DB_DST['database'],
            user=_DB_DST['user'],
            password=_DB_DST['password'],
            connect_timeout=30,
        )
        try:
            with _gate_conn.cursor() as _gc:
                _gc.execute("SELECT to_regclass('public.big_analytics_full_arrival')")
                _arr_exists = _gc.fetchone()[0] is not None
                _arr_cnt = 0
                if _arr_exists:
                    _gc.execute('SELECT COUNT(*) FROM public.big_analytics_full_arrival')
                    _arr_cnt = _gc.fetchone()[0]
                # ── BUG2_GATE_FIX_2026-07-12: пиксель-проверку читаем из ДОЛГОВЕЧНОЙ
                # public.fact_big_analytics, НЕ из транзиентной public.big_analytics_full.
                # Root: pipeline.main() на УСПЕХЕ (cleanup_intermediate, МЕРА №5,
                # pipeline.py L~2214) TRUNCATE-ит big_analytics_full ДО того, как этот гейт
                # запускается (гейт стартует уже ПОСЛЕ возврата из pipeline.main()) →
                # гейт всегда видел пиксель_атрибуц=0 и БЛОКИРОВАЛ refresh даже при полных
                # данных (инцидент 2026-07-12, PID 2408249: durable fact полон, но PBI не
                # обновился). fact_big_analytics — durable витрина build_star, cleanup её НЕ
                # трогает; несёт тот же _source_table='пиксель_атрибуц' с ненулевым COUNT.
                # Проверку arrival НЕ меняем — она уже смотрит на durable big_analytics_full_arrival.
                _gc.execute("SELECT to_regclass('public.fact_big_analytics')")
                _fact_exists = _gc.fetchone()[0] is not None
                _pix_cnt = 0
                if _fact_exists:
                    _gc.execute(
                        "SELECT COUNT(*) FROM public.fact_big_analytics "
                        "WHERE _source_table = 'пиксель_атрибуц'"
                    )
                    _pix_cnt = _gc.fetchone()[0]
        finally:
            _gate_conn.close()
    except Exception as _gate_e:
        logger.error('REFRESH_GATE: не удалось проверить полноту данных: %s', _gate_e)
        _send_telegram(
            f'❌ pipeline_powerbi: не удалось проверить полноту данных перед refresh\n{_gate_e}\n'
            f'Power BI НЕ обновлён (fail-closed).'
        )
        sys.exit(1)

    if _arr_cnt == 0 or _pix_cnt == 0:
        _reason = []
        if _arr_cnt == 0:
            _reason.append('big_analytics_full_arrival ПУСТ (step13 упал/ENOSPC?)')
        if _pix_cnt == 0:
            _reason.append('пиксель_атрибуц отсутствует в fact_big_analytics (step11/build_star упал/ENOSPC?)')
        _reason_s = '; '.join(_reason)
        logger.error('REFRESH_GATE: данные НЕПОЛНЫЕ — %s — Power BI НЕ обновляем', _reason_s)
        _post_dashboard_log('err', f'Refresh пропущен: данные неполные ({_reason_s})')
        _send_telegram(
            '🚨 pipeline_powerbi: пайплайн формально ОК, но данные НЕПОЛНЫЕ — '
            'Power BI НЕ обновлён (fail-closed)\n'
            f'{_reason_s}\n'
            f'arrival={_arr_cnt}, пиксель_атрибуц={_pix_cnt}'
        )
        sys.exit(1)

    logger.info(
        'REFRESH_GATE: данные полные (arrival=%d, пиксель_атрибуц=%d) — публикуем',
        _arr_cnt, _pix_cnt,
    )

    # ── Шаг 3: триггер Power BI ───────────────────────────────────────────────
    logger.info('=== Шаг 3: Power BI refresh ===')
    try:
        refresh_powerbi(tables=_REFRESH_TABLES)
    except Exception as e:
        logger.error('Power BI refresh упал: %s', e)
        _send_telegram(f'⚠️ pipeline_powerbi: пайплайн ОК, но Power BI refresh упал\n{e}')


if __name__ == '__main__':
    main()
