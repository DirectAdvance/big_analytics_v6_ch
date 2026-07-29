"""
data_check/run.py — точка входа агента проверки данных.

Запуск:
    python3 data_check/run.py              # полный отчёт в stdout
    python3 data_check/run.py --json       # JSON в stdout
    python3 data_check/run.py --tg         # + отправить в Telegram
    python3 data_check/run.py --days 7     # период (по умолчанию 30)

Exit codes: 0=OK, 1=найдены проблемы, 2=ошибка скрипта
"""

from __future__ import annotations
import argparse
import json
import logging
import sys
import os
from datetime import datetime, timezone

import psycopg2

# Добавляем корень big_analytics_v5 в sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import DB_DST
from config.tokens import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_PROXY, TELEGRAM_PROXY_VARIANTS

from data_check.sheets_reader import read_sheet
from data_check.checks.projects import SPREADSHEET_ID, GID
from data_check.checks import projects, fields, spending, funnel
from data_check.reporter import format_report, send_telegram

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)


def _check_freshness(db_params: dict) -> dict:
    with psycopg2.connect(**db_params) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT MAX(run_at)
                FROM data_quality_log
                WHERE step = 'step8' AND status = 'ok'
            """)
            row = cur.fetchone()
    last_run = row[0] if row and row[0] else None
    if last_run is None:
        return {'last_pipeline_run': None, 'hours_ago': None, 'stale': True}
    now = datetime.now(timezone.utc)
    if last_run.tzinfo is None:
        last_run = last_run.replace(tzinfo=timezone.utc)
    hours_ago = round((now - last_run).total_seconds() / 3600, 1)
    return {
        'last_pipeline_run': last_run.strftime('%d.%m.%Y %H:%M'),
        'hours_ago': hours_ago,
        'stale': hours_ago > 24,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--json', action='store_true', help='JSON output')
    parser.add_argument('--tg',   action='store_true', help='Send Telegram report')
    args = parser.parse_args()

    try:
        logger.info('Reading Google Sheets projects table...')
        sheets_rows = read_sheet(SPREADSHEET_ID, GID)
        logger.info('Read %d rows from Sheets', len(sheets_rows))

        logger.info('Running checks...')
        results = {
            'projects': projects.run(DB_DST, sheets_rows),
            'fields':   fields.run(DB_DST),
            'spending': spending.run(DB_DST),
            'funnel':   funnel.run(DB_DST),
            'freshness': _check_freshness(DB_DST),
        }
    except Exception as exc:
        logger.error('Check failed: %s', exc, exc_info=True)
        return 2

    if args.json:
        print(json.dumps(results, ensure_ascii=False, default=str, indent=2))
    else:
        report = format_report(results)
        print(report)

    if args.tg:
        report = format_report(results)
        send_telegram(report, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
                      proxy_variants=TELEGRAM_PROXY_VARIANTS)  # TG_PROXY_CHAIN_ROTATION_2026-06-17
        logger.info('Telegram report sent')

    # Exit 1 если найдены критичные проблемы
    critical = (
        results['projects']['only_in_sheets']
        + results['projects']['no_analytics_data']
        + results['spending']['spend_no_leads_7d']
        + results['funnel']['invariant_violations']
    )
    return 1 if critical else 0


if __name__ == '__main__':
    sys.exit(main())
