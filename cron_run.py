#!/usr/bin/env python3
"""CRON_RUNNER_2026-08-16 — обёртка для ежедневного прогона БА6 из cron.

Зачем отдельный файл: `pipeline.py` не шлёт в Telegram ничего — ни успех, ни падение
(проверено 2026-08-16, ни один шаг из `STEPS` не вызывает отправку). Под расписанием это
означает молчаливые провалы, поэтому запускаем прогон отсюда и отчитываемся по факту.

Запуск (cron ставит flock снаружи, как у остальных джобов Victory):
    ~/venv-v6/bin/python3 ~/big_analytics_v6_ch/cron_run.py

Ничего не считает сам: только запускает `pipeline.py`, разбирает его лог и отправляет итог.
Код возврата равен коду возврата пайплайна.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from config.tokens import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_PROXY_VARIANTS
from notifications.telegram import format_ru_amount, send_html

LOG_DIR = BASE / "logs"
KEEP_LOGS = 14
FAIL_TAIL_LINES = 25
EKB = timezone(timedelta(hours=5))

RE_RUN_ID = re.compile(r"run_id=([0-9a-f]+)")
RE_GOLDEN = re.compile(r"golden_kuderko cost=(\S+) delta=(\S+) sales=(\d+)")
RE_FAIL_STEP = re.compile(r"Шаг (\S+) FAIL")


def rotate_logs():
    LOG_DIR.mkdir(exist_ok=True)
    old = sorted(LOG_DIR.glob("cron_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in old[KEEP_LOGS:]:
        path.unlink(missing_ok=True)


def run_pipeline(log_path: Path) -> int:
    with log_path.open("w", encoding="utf-8") as fh:
        proc = subprocess.run(
            [sys.executable, "-u", str(BASE / "pipeline.py")],
            stdout=fh, stderr=subprocess.STDOUT, cwd=str(BASE),
        )
    return proc.returncode


def build_message(rc: int, log_path: Path, minutes: int) -> str:
    text = log_path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    run_id = RE_RUN_ID.search(text)
    golden = RE_GOLDEN.search(text)
    verify_pass = bool(re.search(r"^\S+ INFO PASS$", text, re.M)) or " INFO PASS" in text

    head = "✅ <b>БА6: прогон OK</b>" if rc == 0 else "🔴 <b>БА6: прогон УПАЛ</b>"
    rows = [
        head,
        f"{datetime.now(EKB).strftime('%d.%m %H:%M')} Екб · {minutes} мин",
    ]
    if run_id:
        rows.append(f"run_id <code>{run_id.group(1)}</code>")
    rows.append("verify: PASS" if verify_pass else "verify: <b>нет PASS</b>")
    if golden:
        cost = format_ru_amount(float(golden.group(1)))
        delta = format_ru_amount(float(golden.group(2)))
        rows.append(f"golden Кудерко: {cost} ₽ (Δ {delta}), продажи {golden.group(3)}")

    if rc != 0:
        failed = RE_FAIL_STEP.findall(text)
        if failed:
            rows.append(f"упал шаг: <b>{', '.join(failed)}</b>")
        tail = "\n".join(lines[-FAIL_TAIL_LINES:])
        rows.append(f"\n<code>{tail}</code>")

    rows.append(f"\nлог: <code>{log_path}</code>")
    return "\n".join(rows)


def main() -> int:
    rotate_logs()
    stamp = datetime.now(EKB).strftime("%Y%m%d_%H%M%S")
    log_path = LOG_DIR / f"cron_{stamp}.log"

    started = time.time()
    rc = run_pipeline(log_path)
    minutes = int((time.time() - started) / 60)

    message = build_message(rc, log_path, minutes)
    delivered = send_html(
        message, bot_token=TELEGRAM_BOT_TOKEN, chat_id=TELEGRAM_CHAT_ID,
        proxy_variants=TELEGRAM_PROXY_VARIANTS, timeout=30,
    )
    if not delivered:
        # Не глотаем молча: иначе провал прогона И провал уведомления выглядят одинаково.
        print("CRON_RUNNER: Telegram не доставлен", file=sys.stderr)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
