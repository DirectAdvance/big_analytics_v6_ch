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
from html import escape
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from config.tokens import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_PROXY_VARIANTS
from notifications.telegram import format_ru_amount, send_html

LOG_DIR = BASE / "logs"
KEEP_LOGS = 14
FAIL_TAIL_LINES = 25
EKB = timezone(timedelta(hours=5))
RAW_TABLES = ("raw_yandex", "raw_leads", "raw_calls")
FINAL_TABLES = (
    "big_analytics_full",
    "big_analytics_full_arrival",
    "big_analytics_unified",
    "fact_big_analytics",
    "pbi_big_analytics_full",
    "pbi_import_big_analytics_full",
)
FINAL_CHECKS = (
    "unified_count_mismatch",
    "fact_unified_count_mismatch",
    "full_before_2026",
    "full_null_source",
    "full_funnel_korr_lt_kval",
    "full_funnel_kval_lt_priezd",
    "full_funnel_priezd_lt_prodazhi",
)

RE_RUN_ID = re.compile(r"run_id=([0-9a-f]+)")
RE_GOLDEN = re.compile(r"golden_kuderko cost=(\S+) delta=(\S+) sales=(\d+)")
RE_FAIL_STEP = re.compile(r"Шаг (\S+) FAIL")
RE_STEP_START = re.compile(r"Шаг\s+(\S+):\s+(.+)")
RE_STEP_DONE = re.compile(r"Шаг\s+(\S+)\s+(OK|FAIL)\s+за\s+([\d.]+)\s+сек")
RE_COUNT = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)=(\d+)\b")
RE_CRON_LOG_DATE = re.compile(r"cron_(\d{8})_\d{6}\.log$")
RE_KUDERKO_COVERAGE = re.compile(
    r"kuderko_raw_coverage: present_any_day=(\d+) present_pre_cutoff=(\d+) total=(\d+)"
)


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


def parse_counts(text: str) -> dict[str, int]:
    return {name: int(value) for name, value in RE_COUNT.findall(text)}


def parse_step_timings(text: str) -> list[tuple[str, str, str, float]]:
    names: dict[str, str] = {}
    timings: list[tuple[str, str, str, float]] = []
    for line in text.splitlines():
        started = RE_STEP_START.search(line)
        if started:
            names[started.group(1)] = started.group(2).strip()
            continue
        done = RE_STEP_DONE.search(line)
        if done:
            step, status, seconds = done.groups()
            timings.append((step, names.get(step, ""), status, float(seconds)))
    return timings


def previous_cron_log(log_path: Path) -> Path | None:
    current_day = cron_log_day(log_path)
    if current_day is None:
        return None
    target_day = current_day - timedelta(days=1)
    logs = sorted(LOG_DIR.glob("cron_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in logs:
        if path != log_path and cron_log_day(path) == target_day:
            return path
    return None


def cron_log_day(path: Path):
    match = RE_CRON_LOG_DATE.fullmatch(path.name)
    return datetime.strptime(match.group(1), "%Y%m%d").date() if match else None


def fmt_int(value: int) -> str:
    return format_ru_amount(float(value))


def fmt_seconds(seconds: float) -> str:
    seconds_int = int(round(seconds))
    if seconds_int < 60:
        return f"{seconds_int}с"
    minutes, rest = divmod(seconds_int, 60)
    return f"{minutes}м{rest:02d}с"


def build_raw_section(current: dict[str, int], previous: dict[str, int] | None) -> list[str]:
    rows = ["", "<b>raw vs вчера</b>"]
    for table in RAW_TABLES:
        now = current.get(table)
        if now is None:
            rows.append(f"{table}: <b>нет в логе</b>")
            continue
        if not previous or table not in previous:
            rows.append(f"{table}: {fmt_int(now)} (нет базы сравнения)")
            continue
        delta = now - previous[table]
        status = "OK" if delta >= 0 else "<b>ПРОСАДКА</b>"
        rows.append(f"{table}: {fmt_int(now)} ({delta:+,} строк) {status}".replace(",", " "))
    return rows


def build_final_section(counts: dict[str, int]) -> list[str]:
    rows = ["", "<b>raw → итоговые</b>"]
    full = counts.get("big_analytics_full")
    arrival = counts.get("big_analytics_full_arrival")
    unified = counts.get("big_analytics_unified")
    fact = counts.get("fact_big_analytics")
    if None not in (full, arrival, unified, fact):
        expected = full + arrival
        mark = "OK" if unified == expected and fact == unified else "<b>FAIL</b>"
        rows.append(
            f"full+arrival={fmt_int(expected)}, unified={fmt_int(unified)}, "
            f"fact={fmt_int(fact)} {mark}"
        )

    missing = [name for name in FINAL_TABLES if name not in counts]
    if missing:
        rows.append(f"нет counts: <code>{', '.join(missing)}</code>")

    bad_checks = [name for name in FINAL_CHECKS if counts.get(name, 0)]
    if bad_checks:
        rows.append(f"инварианты: <b>FAIL</b> <code>{', '.join(bad_checks)}</code>")
    else:
        rows.append("инварианты: OK")
    return rows


def build_golden_section(text: str, golden: re.Match[str] | None) -> list[str]:
    rows = ["", "<b>golden</b>"]
    if golden:
        cost = format_ru_amount(float(golden.group(1)))
        delta = format_ru_amount(float(golden.group(2)))
        rows.append(f"Кудерко: {cost} ₽ (Δ {delta}), продажи {golden.group(3)}")
    else:
        rows.append("Кудерко: <b>нет строки golden_kuderko</b>")

    coverage = RE_KUDERKO_COVERAGE.search(text)
    if coverage:
        present, pre_cutoff, total = coverage.groups()
        suffix = " <b>сырьё неполно</b>" if "KUDERKO_RAW_INCOMPLETE" in text else ""
        rows.append(f"raw Кудерко: {present}/{total}, до cutoff {pre_cutoff}/{total}{suffix}")
    return rows


def build_steps_section(text: str) -> list[str]:
    timings = parse_step_timings(text)
    if not timings:
        return ["", "<b>время по шагам</b>", "<code>нет таймингов в логе</code>"]
    lines = []
    for step, name, status, seconds in timings:
        label = f"{step} {name}".strip()
        lines.append(f"{label:<42} {status:<4} {fmt_seconds(seconds)}")
    return ["", "<b>время по шагам</b>", f"<code>{escape(chr(10).join(lines))}</code>"]


def build_message(rc: int, log_path: Path, minutes: int) -> str:
    text = log_path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    run_id = RE_RUN_ID.search(text)
    golden = RE_GOLDEN.search(text)
    verify_pass = bool(re.search(r"^\S+ INFO PASS$", text, re.M)) or " INFO PASS" in text
    counts = parse_counts(text)
    prev_log = previous_cron_log(log_path)
    prev_counts = parse_counts(prev_log.read_text(encoding="utf-8", errors="replace")) if prev_log else None

    head = "✅ <b>БА6: прогон OK</b>" if rc == 0 else "🔴 <b>БА6: прогон УПАЛ</b>"
    rows = [
        head,
        f"{datetime.now(EKB).strftime('%d.%m %H:%M')} Екб · {minutes} мин",
    ]
    if run_id:
        rows.append(f"run_id <code>{run_id.group(1)}</code>")
    rows.append("verify: PASS" if verify_pass else "verify: <b>нет PASS</b>")
    rows.extend(build_raw_section(counts, prev_counts))
    rows.extend(build_final_section(counts))
    rows.extend(build_golden_section(text, golden))
    rows.extend(build_steps_section(text))

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
