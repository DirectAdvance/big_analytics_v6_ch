"""
step_cron_night/report_placement/run.py — точка входа: запускает step1 → step2 последовательно.

Запуск:
    ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 step_cron_night/report_placement/run.py"
"""

import logging
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
# Корень big_analytics_v5 (на 2 уровня выше step_cron_night/report_placement/) —
# нужен для import pipeline_mutex (PIPELINE_MUTEX_2026-07-12).
_ROOT = os.path.dirname(os.path.dirname(BASE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger('report_placement.run')


def run(script_name: str) -> None:
    script = os.path.join(BASE, script_name)
    result = subprocess.run([sys.executable, script], check=True)
    return result.returncode


if __name__ == '__main__':
    # ── PIPELINE_MUTEX_2026-07-12: взаимное исключение с pipeline_powerbi /
    # fast_pipeline / build_spend_daily (общий flock). step2 делает TRUNCATE+INSERT
    # analytics_report_placement и конкурирует с пересборкой витрин пайплайном.
    # fd держим открытым весь job (step1→step2) — ОС снимет lock на выходе процесса.
    import pipeline_mutex
    try:
        _mutex_fd = pipeline_mutex.acquire('report_placement')  # noqa: F841 — держим fd весь job
    except pipeline_mutex.PipelineBusy as _busy:
        logger.warning(
            'PIPELINE_MUTEX: активен другой прогон группы (%s) — report_placement пропущен', _busy
        )
        # TG_ON_BUSY_2026-07-12: единообразный алерт при mutex-skip (как build_spend_daily).
        # _ROOT уже в sys.path (см. выше) → берём тот же _send_tg (proxy/токен) из pipeline.
        try:
            from pipeline import _send_tg
            _send_tg(f'⚠️ <b>report_placement</b> SKIP: активен другой прогон группы ({_busy})')
        except Exception as _tg_e:
            logger.warning('TG send failed: %s', _tg_e)
        sys.exit(0)

    run('step1_fetch_direct.py')
    run('step2_build_analytics.py')
