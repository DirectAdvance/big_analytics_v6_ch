#!/usr/bin/env python3
"""CRON_RUNNER_2026-08-16 — обёртка для ежедневного прогона БА6 из cron.

Зачем отдельный файл: `pipeline.py` не шлёт в Telegram ничего — ни успех, ни падение
(проверено 2026-08-16, ни один шаг из `STEPS` не вызывает отправку). Под расписанием это
означает молчаливые провалы, поэтому запускаем прогон отсюда и отчитываемся по факту.

Запуск (cron ставит flock снаружи, как у остальных джобов Victory):
    ~/venv-v6/bin/python3 ~/big_analytics_v6_ch/cron_run.py

Ничего не считает сам: запускает `pipeline.py`, разбирает лог и отправляет итог.
Power BI refresh — опциональный шаг ПОСЛЕ успешного pipeline, выключен по умолчанию: BA6 PBIP
ещё не опубликован в Power BI Service (только BA5-датасет "Большая аналитика_v00"), см.
STATE.md 2026-08-21. Включается переменной окружения `BA6_POWERBI_REFRESH=1`; без неё
`refresh_powerbi.py` не запускается и не может повлиять ни на код возврата, ни на заголовок
сообщения.
"""

from __future__ import annotations

import os
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
    "raw_yandex_cost_zero",
    "unified_count_mismatch",
    "fact_unified_count_mismatch",
    "full_before_2026",
    "full_null_source",
    "full_last_day_incomplete",
    "full_funnel_korr_lt_kval",
    "full_funnel_kval_lt_priezd",
    "full_funnel_priezd_lt_prodazhi",
    "full_funnel_dobro_gt_dohod_do_kredita",
    "full_funnel_dohod_do_kredita_gt_priezd",
    "direct_spend_loss_slices",
    "bi_contract_auto_spend_raw_to_pbi_slices",
    "bi_contract_direct_feed_spend_slices",
    "bi_contract_pbi_funnel_slices",
    "bi_contract_closed_month_drift_slices",
    "bi_contract_auto_empty_dims_slices",
    "sales_without_real_specialist_slices",
)
BI_CONTRACTS = (
    ("direct_spend_loss_slices", "расход Директа raw → рабочий слой"),
    ("bi_contract_auto_spend_raw_to_pbi_slices", "расход Авто raw → BI"),
    ("bi_contract_direct_feed_spend_slices", "расход фидов raw → BI"),
    ("bi_contract_pbi_funnel_slices", "воронка заявки/визиты/продажи → BI"),
    ("bi_contract_closed_month_drift_slices", "закрытые месяцы: продажи и CPL продажи ±4%"),
    ("bi_contract_auto_empty_dims_slices", "Авто без специалиста/города/салона"),
    ("sales_without_real_specialist_slices", "продажи без реального специалиста"),
)

RE_RUN_ID = re.compile(r"run_id=([0-9a-f]+)")
RE_GOLDEN = re.compile(r"golden_kuderko cost=(\S+) delta=(\S+) sales=(\d+)")
RE_VERIFY_FAIL = re.compile(r"verify_big_analytics:\s*FAIL:\s*(.+)$", re.M)
RE_FULL_METRICS = re.compile(
    r"full metrics:\s*cost=(\S+)\s+z=(\S+)\s+korr=(\S+)\s+kval=(\S+)\s+priezd=(\S+)\s+prodazhi=(\S+)"
)
RE_FAIL_STEP = re.compile(r"Шаг (\S+) FAIL")
RE_STEP_START = re.compile(r"Шаг\s+(\S+):\s+(.+)")
RE_STEP_DONE = re.compile(r"Шаг\s+(\S+)\s+(OK|FAIL)\s+за\s+([\d.]+)\s+сек")
RE_COUNT = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)=(\d+)\b")
RE_CRON_LOG_DATE = re.compile(r"cron_(\d{8})_\d{6}\.log$")
RE_KUDERKO_COVERAGE = re.compile(
    r"kuderko_raw_coverage: present_any_day=(\d+) present_pre_cutoff=(\d+) total=(\d+)"
)
RE_VERIFY_PASS = re.compile(r"verify_big_analytics:\s*PASS\s*$", re.M)
# Steps report soft problems via a `WARNING=...` tail on their "Шаг N OK" details line
# (step0_sync_local/step0.py::_check_reviews_freshness is the first user) instead of raising —
# a raise would take down the whole daily pipeline for a source that is 0.12% of
# big_analytics_full. parse_counts/build_final_section never look at `details` text, only at
# `key=int` pairs in it, so on a green run this warning used to reach nowhere but the log file
# (BA6 pattern #43: traded a loud failure for an invisible one). Matched per-line so multiple
# steps warning in one run all show up.
RE_STEP_WARNING = re.compile(r"WARNING=(.+)$", re.M)


def rotate_logs():
    LOG_DIR.mkdir(exist_ok=True)
    old = sorted(LOG_DIR.glob("cron_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in old[KEEP_LOGS:]:
        path.unlink(missing_ok=True)


def log_event(log_path: Path, event: str) -> None:
    log_path.parent.mkdir(exist_ok=True)
    stamp = datetime.now(EKB).strftime("%Y-%m-%d %H:%M:%S %Z")
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(f"\n=== {stamp} CRON_RUNNER: {event} ===\n")


def send_status(title: str, log_path: Path) -> bool:
    return send_html(
        f"<b>{escape(title)}</b>\nлог: <code>{escape(str(log_path))}</code>",
        bot_token=TELEGRAM_BOT_TOKEN, chat_id=TELEGRAM_CHAT_ID,
        proxy_variants=TELEGRAM_PROXY_VARIANTS, timeout=30,
    )


def send_report(message: str) -> bool:
    return send_html(
        message, bot_token=TELEGRAM_BOT_TOKEN, chat_id=TELEGRAM_CHAT_ID,
        proxy_variants=TELEGRAM_PROXY_VARIANTS, timeout=30,
    )


def run_pipeline(log_path: Path) -> int:
    with log_path.open("a", encoding="utf-8") as fh:
        proc = subprocess.run(
            [sys.executable, "-u", str(BASE / "pipeline.py")],
            stdout=fh, stderr=subprocess.STDOUT, cwd=str(BASE),
        )
    return proc.returncode


def run_powerbi_refresh(log_path: Path) -> int:
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write("\n=== Power BI BA6 refresh ===\n")
        proc = subprocess.run(
            [sys.executable, "-u", str(BASE / "refresh_powerbi.py"), "--no-notify"],
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


def fmt_money(value: float) -> str:
    return f"{format_ru_amount(value)} ₽"


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


def build_main_section(full_metrics: re.Match[str] | None) -> list[str]:
    if not full_metrics:
        return ["", "<b>главное</b>", "full metrics: <b>нет в логе</b>"]

    cost, zayavki, korr, kval, priezd, prodazhi = (float(v) for v in full_metrics.groups())

    def cpl(count: float) -> str:
        return fmt_money(cost / count) if count else "—"

    return [
        "",
        "<b>главное</b>",
        f"Расход: <code>{fmt_money(cost)}</code>",
        (
            "Воронка: "
            f"заявки {format_ru_amount(zayavki)} → "
            f"корр {format_ru_amount(korr)} → "
            f"квал {format_ru_amount(kval)} → "
            f"приезды {format_ru_amount(priezd)} → "
            f"продажи {format_ru_amount(prodazhi)}"
        ),
        (
            "CPL: "
            f"заявка {cpl(zayavki)}, "
            f"квал {cpl(kval)}, "
            f"приезд {cpl(priezd)}, "
            f"продажа {cpl(prodazhi)}"
        ),
    ]


def parse_verify_failures(verify_fail: re.Match[str] | None) -> list[str]:
    if not verify_fail:
        return []
    return [part.strip() for part in verify_fail.group(1).split(";") if part.strip()]


def build_verify_line(verify_pass: bool, verify_failures: list[str]) -> str:
    if verify_pass:
        return "verify: PASS"
    if verify_failures:
        return f"verify: <b>FAIL</b> <code>{escape('; '.join(verify_failures[:5]))}</code>"
    return "verify: <b>нет PASS</b>"


def build_final_section(counts: dict[str, int], verify_failures: list[str]) -> list[str]:
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
    bad_check_names = set(bad_checks)
    logged_failures = [
        f for f in verify_failures
        if f.split("=", 1)[0] not in bad_check_names
    ]
    bad_checks.extend(logged_failures[:5])
    if bad_checks:
        rows.append(f"инварианты: <b>FAIL</b> <code>{escape(', '.join(bad_checks))}</code>")
    else:
        rows.append("инварианты: OK")
    return rows


def build_bi_contracts_section(counts: dict[str, int]) -> list[str]:
    rows = ["", "<b>BI golden</b>"]
    for key, label in BI_CONTRACTS:
        if key not in counts:
            rows.append(f"{label}: <b>нет в логе</b>")
            continue
        value = counts[key]
        if value:
            rows.append(f"{label}: 🚫 <b>FAIL</b> ({fmt_int(value)} срезов)")
        else:
            rows.append(f"{label}: ✅ OK")
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


def build_warnings_section(text: str) -> list[str]:
    warnings = RE_STEP_WARNING.findall(text)
    if not warnings:
        return []
    rows = ["", "<b>⚠️ предупреждения шагов</b>"]
    rows.extend(f"<code>{escape(w)}</code>" for w in warnings)
    return rows


def build_powerbi_section(pipeline_rc: int, powerbi_rc: int | None, powerbi_enabled: bool) -> list[str]:
    if powerbi_rc == 0:
        status = "OK"
    elif powerbi_rc:
        status = "<b>FAIL</b>"
    elif pipeline_rc != 0:
        status = "не запускался: pipeline FAIL"
    elif powerbi_enabled:
        status = "ожидает запуска после pipeline"
    else:
        status = "пропущен (BA6_POWERBI_REFRESH не включён)"
    return ["", "<b>Power BI</b>", status]


def build_message(
    rc: int,
    log_path: Path,
    minutes: int,
    powerbi_rc: int | None = None,
    powerbi_enabled: bool = False,
) -> str:
    text = log_path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    run_id = RE_RUN_ID.search(text)
    golden = RE_GOLDEN.search(text)
    full_metrics = RE_FULL_METRICS.search(text)
    verify_pass = bool(RE_VERIFY_PASS.search(text))
    verify_failures = parse_verify_failures(RE_VERIFY_FAIL.search(text))
    counts = parse_counts(text)
    prev_log = previous_cron_log(log_path)
    prev_counts = parse_counts(prev_log.read_text(encoding="utf-8", errors="replace")) if prev_log else None

    if rc != 0:
        head = "🔴 <b>БА6: pipeline УПАЛ</b>"
    elif powerbi_rc:
        head = "🔴 <b>БА6: pipeline OK, Power BI УПАЛ</b>"
    elif powerbi_rc == 0:
        head = "✅ <b>БА6: pipeline + Power BI OK</b>"
    else:
        head = "✅ <b>БА6: прогон OK</b>"
    rows = [
        head,
        f"{datetime.now(EKB).strftime('%d.%m %H:%M')} Екб · {minutes} мин",
    ]
    if run_id:
        rows.append(f"run_id <code>{run_id.group(1)}</code>")
    rows.append(build_verify_line(verify_pass, verify_failures))
    rows.extend(build_warnings_section(text))
    rows.extend(build_main_section(full_metrics))
    rows.extend(build_raw_section(counts, prev_counts))
    rows.extend(build_final_section(counts, verify_failures))
    rows.extend(build_bi_contracts_section(counts))
    rows.extend(build_golden_section(text, golden))
    rows.extend(build_powerbi_section(rc, powerbi_rc, powerbi_enabled))
    rows.extend(build_steps_section(text))

    if rc != 0 or powerbi_rc:
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
    powerbi_enabled = os.environ.get("BA6_POWERBI_REFRESH") == "1"
    log_event(log_path, "pipeline started")
    if not send_status("🟡 БА6: pipeline начал работу", log_path):
        print("CRON_RUNNER: Telegram не доставлен", file=sys.stderr)
    pipeline_rc = run_pipeline(log_path)
    log_event(log_path, f"pipeline finished rc={pipeline_rc}")
    if not send_report(build_message(
        pipeline_rc, log_path, int((time.time() - started) / 60), powerbi_enabled=powerbi_enabled,
    )):
        # Не глотаем молча: иначе провал прогона И провал уведомления выглядят одинаково.
        print("CRON_RUNNER: Telegram не доставлен", file=sys.stderr)

    if pipeline_rc != 0 or not powerbi_enabled:
        return pipeline_rc

    log_event(log_path, "Power BI refresh started")
    if not send_status("🟡 БА6: Power BI refresh начал работу", log_path):
        print("CRON_RUNNER: Telegram не доставлен", file=sys.stderr)
    powerbi_rc = run_powerbi_refresh(log_path)
    log_event(log_path, f"Power BI refresh finished rc={powerbi_rc}")
    minutes = int((time.time() - started) / 60)
    if not send_report(build_message(pipeline_rc, log_path, minutes, powerbi_rc, powerbi_enabled=powerbi_enabled)):
        print("CRON_RUNNER: Telegram не доставлен", file=sys.stderr)
    return powerbi_rc


if __name__ == "__main__":
    raise SystemExit(main())
