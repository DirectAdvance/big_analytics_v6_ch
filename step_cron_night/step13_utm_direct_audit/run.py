"""
step13_utm_direct_audit/run.py
==============================
UTM-аудит групп Яндекс.Директ — шаг 13 пайплайна (вызывается из pipeline.py).

Проверяет TrackingParams всех групп активных аккаунтов.
Результат: public.check_utm + public.check_utm_fuck_direct
По завершении — Telegram-уведомление со сводкой.

Запуск (на сервере из папки big_analytics_v5/):
    ~/venv/bin/python3 step13_utm_direct_audit/run.py

Запуск через ssh с локального:
    ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 step13_utm_direct_audit/run.py"
"""

import sys
import os
import time
import traceback
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import psycopg2
import requests

from config.settings import DB_DST
from config.tokens import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_PROXY, TELEGRAM_PROXY_VARIANTS
from step4_campaign_status.check_utm.utm_direct_audit import main


def _send_tg(text: str) -> None:
    """Отправка в Telegram с ротацией прокси (Amsterdam→DE→NL→FR→direct)."""
    for proxies in TELEGRAM_PROXY_VARIANTS:  # TG_PROXY_CHAIN_ROTATION_2026-06-17
        try:
            r = requests.post(
                f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage',
                json={'chat_id': TELEGRAM_CHAT_ID, 'text': text, 'parse_mode': 'HTML'},
                proxies=proxies,
                timeout=10,
            )
            if r.status_code == 200:
                return
        except Exception:
            pass


def _get_stats() -> dict:
    conn = psycopg2.connect(**DB_DST)
    try:
        with conn.cursor() as cur:
            # check_utm_fuck_direct — поденевая история (date, login, CampaignId, group_id, cost, ...)
            # Схема: нет колонок utm_done / first_bad_check / last_bad_check / bad_days / checked_at
            cur.execute("""
                SELECT
                    COUNT(*) AS total_rows,
                    COUNT(DISTINCT (login, "CampaignId", COALESCE(group_id, 0))) AS unique_groups,
                    MAX(date) AS last_bad_date
                FROM public.check_utm_fuck_direct
            """)
            row = cur.fetchone()
            total, open_cnt, max_last = row if row else (0, 0, None)

            # Максимальное число дней с расходом на одну (login, CampaignId, group_id)
            cur.execute("""
                SELECT MAX(cnt) FROM (
                    SELECT COUNT(DISTINCT date) AS cnt
                    FROM public.check_utm_fuck_direct
                    GROUP BY login, "CampaignId", COALESCE(group_id, 0)
                ) sub
            """)
            max_days = (cur.fetchone() or [0])[0] or 0

            cur.execute("SELECT COUNT(*) FROM public.check_utm")
            check_utm_total = cur.fetchone()[0]

        return {
            'check_utm_total': check_utm_total,
            'fuck_total': total,
            'fuck_open': open_cnt,        # уникальных (login, CampaignId, group_id) с плохой меткой
            'fuck_with_dates': open_cnt,  # то же самое — у всех есть хотя бы одна дата
            'last_check': max_last,
            'max_last_bad': max_last,
            'max_bad_days': max_days,
        }
    finally:
        conn.close()


if __name__ == '__main__':
    started = datetime.now()
    t0 = time.perf_counter()
    _send_tg(
        f'▶️ <b>utm_direct_audit</b> старт\n'
        f'<code>{started.strftime("%d.%m.%Y %H:%M")} UTC</code>'
    )
    try:
        main()
        elapsed = time.perf_counter() - t0
        try:
            s = _get_stats()
            msg = (
                f'✅ <b>utm_direct_audit</b> завершён\n'
                f'⏱ {elapsed/60:.1f} мин\n\n'
                f'<b>check_utm:</b> {s["check_utm_total"]}\n'
                f'<b>check_utm_fuck_direct:</b> {s["fuck_total"]} (открыто: {s["fuck_open"]})\n'
                f'  с датами: {s["fuck_with_dates"]}\n'
                f'  max bad_days: {s["max_bad_days"]}\n'
                f'  last_bad_check: <code>{s["max_last_bad"]}</code>\n'
                f'  checked_at: <code>{s["last_check"]}</code>'
            )
        except Exception as e:
            msg = f'✅ utm_direct_audit завершён за {elapsed/60:.1f} мин\n⚠️ Не удалось получить stats: {e}'
        _send_tg(msg)
    except Exception as e:
        elapsed = time.perf_counter() - t0
        tb = traceback.format_exc()[-1500:]
        _send_tg(
            f'❌ <b>utm_direct_audit</b> упал за {elapsed/60:.1f} мин\n'
            f'<pre>{tb}</pre>'
        )
        sys.exit(1)
