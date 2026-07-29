#!/usr/bin/env python3
"""
pipeline_night.py — ночной пайплайн big_analytics_v5

Запускается в 03:00 МСК (00:00 UTC) через cron.
Выполняет тяжёлые API-шаги последовательно:
  1. metrika_yandex        — grants доступа к счётчикам Метрики (~3 мин)
  2. step13_utm_audit      — UTM-аудит групп (~2 ч)
  3. korrektirovki         — корректировки ставок (~25 мин)
  4. 404_errors            — ошибки 404 (~1 мин)
  5. recheck_404           — HTTP-перепроверка URL из 404_errors, удаление живых (~15-30 мин)
  6. ml_korrektirovki_rebuild — пересборка fact_ml_korrektirovki (~1 мин)

Отзывы (direct_account_reviews/pipeline.py) запускаются отдельно раз в неделю
(пн 02:00 МСК / вс 21:00 UTC) — данные меняются медленно, ежедневный запуск избыточен.

По завершении — Telegram-уведомление с итогами каждого шага.
Лог: /tmp/pipeline_night.log
"""

import subprocess
import sys
import time
import logging
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger('pipeline_night')

# Корень big_analytics_v5 (на уровень выше step_cron_night/)
BASE_DIR = Path(__file__).parent.parent.resolve()
PYTHON = sys.executable
_NIGHT = BASE_DIR / 'step_cron_night'

STEPS = [
    ('metrika_yandex',           _NIGHT / 'metrika_yandex.py'),
    ('step13_utm_audit',         _NIGHT / 'step13_utm_direct_audit' / 'run.py'),
    ('korrektirovki',            _NIGHT / 'korrektirovki' / 'run.py'),
    ('404_errors',               _NIGHT / '404_errors' / '404_errors.py'),
    # RECHECK_404_2026-07-27: HTTP-перепроверка URL из 404_errors — удаляем те, что снова живые.
    # Порядок load→recheck осознан: если чистить ДО загрузки, инкремент вернёт удалённое обратно.
    ('recheck_404',              _NIGHT / '404_errors' / 'recheck_404.py'),
    # ML_KORREKTIROVKI_FACT_2026-07-27: пересборка fact_ml_korrektirovki ПОСЛЕ шага korrektirovki
    # (yandex_direct_korrektirovki только что обновлена) × fact_big_analytics (от последнего pipeline.py).
    ('ml_korrektirovki_rebuild', _NIGHT / 'build_ml_korrektirovki_night.py'),
]

TIMEOUTS = {
    'metrika_yandex':          30 * 60,
    'step13_utm_audit':         4 * 3600,
    'korrektirovki':           90 * 60,
    '404_errors':              15 * 60,
    'recheck_404':             45 * 60,   # ~15-30 мин на ~5K URL; 45 мин с запасом
    'ml_korrektirovki_rebuild': 10 * 60,  # ~секунды–минуты; 10 мин с запасом
}


def _send_tg(text: str) -> None:
    try:
        sys.path.insert(0, str(BASE_DIR))
        from config.tokens import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_PROXY
        import requests
        proxy_variants = [{'https': TELEGRAM_PROXY}] if TELEGRAM_PROXY else []
        for proxies in proxy_variants + [None]:
            try:
                requests.post(
                    f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage',
                    json={'chat_id': TELEGRAM_CHAT_ID, 'text': text, 'parse_mode': 'HTML'},
                    timeout=10,
                    proxies=proxies,
                )
                return
            except Exception as e:
                logger.warning('TG (proxies=%s): %s', proxies, e)
    except Exception as e:
        logger.error('TG import error: %s', e)


def run_step(name: str, script: Path) -> tuple[bool, float]:
    logger.info('▶ %s', name)
    t0 = time.perf_counter()
    timeout = TIMEOUTS.get(name, 2 * 3600)
    try:
        result = subprocess.run(
            [PYTHON, str(script)],
            cwd=str(BASE_DIR),
            timeout=timeout,
        )
        elapsed = time.perf_counter() - t0
        ok = result.returncode == 0
        logger.info('%s %s — %.0fs', '✅' if ok else '❌', name, elapsed)
        return ok, elapsed
    except subprocess.TimeoutExpired:
        elapsed = time.perf_counter() - t0
        logger.error('TIMEOUT %s (%.0fs)', name, elapsed)
        return False, elapsed
    except Exception as e:
        elapsed = time.perf_counter() - t0
        logger.error('ERROR %s: %s', name, e)
        return False, elapsed


def _fmt_dur(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f'{h}ч {m}м'
    return f'{m}м {s}с'


def main() -> None:
    started = datetime.now()
    logger.info('=== pipeline_night START %s ===', started.strftime('%Y-%m-%d %H:%M'))
    _send_tg(
        f'🌙 <b>pipeline_night</b> запущен\n'
        f'{started.strftime("%Y-%m-%d %H:%M")}'
    )

    results: list[tuple[str, bool, float]] = []
    for name, script in STEPS:
        ok, elapsed = run_step(name, script)
        results.append((name, ok, elapsed))

    finished = datetime.now()
    total = (finished - started).total_seconds()

    lines = ['🌙 <b>pipeline_night</b> завершён']
    lines.append(
        f'{started.strftime("%Y-%m-%d")} '
        f'{started.strftime("%H:%M")}→{finished.strftime("%H:%M")}'
    )
    lines.append('')
    for name, ok, elapsed in results:
        icon = '✅' if ok else '❌'
        lines.append(f'{icon} {name}: {_fmt_dur(elapsed)}')

    failed = [n for n, ok, _ in results if not ok]
    lines.append('')
    if failed:
        lines.append(f'⚠️ Ошибки: {", ".join(failed)}')
    else:
        lines.append('✅ Всё прошло успешно')
    lines.append(f'⏱ Итого: {_fmt_dur(total)}')

    report = '\n'.join(lines)
    logger.info('\n%s', report)
    _send_tg(report)


if __name__ == '__main__':
    main()
