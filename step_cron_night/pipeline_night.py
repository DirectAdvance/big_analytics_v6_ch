#!/usr/bin/env python3
"""Night ClickHouse pipeline for big_analytics_v6_ch."""

from __future__ import annotations

import argparse
import importlib
import logging
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from config.ch_db import get_client  # noqa: E402
from config.tokens import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_PROXY_VARIANTS  # noqa: E402
from notifications.telegram import build_pipeline_error_message, render_html, send_html  # noqa: E402
from pipeline import ensure_quality_log, log_step  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("pipeline_night")


NIGHT_STEPS = [
    (101, "step_cron_night.metrika_yandex", "night_metrika_yandex"),
    (102, "step_cron_night.step13_utm_direct_audit.run", "night_check_utm"),
    (103, "step_cron_night.korrektirovki.run", "night_korrektirovki"),
    (104, "step_cron_night.404_errors.404_errors", "night_404_errors"),
    (105, "step_cron_night.404_errors.recheck_404", "night_recheck_404"),
    (106, "step_cron_night.build_ml_korrektirovki_night", "night_ml_korrektirovki"),
    (114, "step14_minus_snapshot.step14", "night_minus_snapshot"),
]

LEGACY_PG_NOT_IN_NIGHT = {
    "archive/postgres_legacy_2026_07_31/step_cron_night/direct_account_reviews/pipeline.py": (
        "weekly v5 job; CH live-fetch not ported yet"
    ),
    "archive/postgres_legacy_2026_07_31/step_cron_night/report_placement/run.py": (
        "weekly v5 job; v6 currently builds bi_arp_fact from fact_direct_feed_funnel"
    ),
    "archive/postgres_legacy_2026_07_31/step_cron_night/build_spend_daily.py": (
        "v6 spend builders are root pipeline steps 141-143/1431-1432"
    ),
    "archive/postgres_legacy_2026_07_31/step_cron_night/revoke_metrika_grants.py": (
        "grant revoke API job is not ported to raw_data-first v6"
    ),
}


def _send_tg(text: str) -> None:
    """`text` is pre-built HTML (`build_pipeline_error_message`/local report lines) —
    goes through `send_html`, the one sender+sanitize path for this project."""
    if not send_html(text, bot_token=TELEGRAM_BOT_TOKEN, chat_id=TELEGRAM_CHAT_ID,
                     proxy_variants=TELEGRAM_PROXY_VARIANTS):
        logger.error("TG send failed on all proxies")


def _fmt_dur(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}ч {m}м"
    return f"{m}м {s}с"


def _selected_steps(only_step: int | None) -> list[tuple[int, str, str]]:
    if only_step is None:
        return NIGHT_STEPS
    return [item for item in NIGHT_STEPS if item[0] == only_step]


def run_step(client, run_id: str, step_num: int, module_path: str, label: str, send_tg: bool) -> tuple[bool, float, str]:
    logger.info("━" * 60)
    logger.info("Night step %s: %s", step_num, module_path)
    t0 = time.perf_counter()
    try:
        mod = importlib.import_module(module_path)
        result = mod.run(conn=None, run_id=run_id)
        elapsed = time.perf_counter() - t0
        rows = result.get("rows") if isinstance(result, dict) else None
        details = result.get("details") if isinstance(result, dict) else None
        log_step(client, run_id, label, "OK", rows, elapsed, details)
        logger.info("Night step %s OK за %.1f сек: %s", step_num, elapsed, details)
        return True, elapsed, details or ""
    except Exception as exc:  # noqa: BLE001
        elapsed = time.perf_counter() - t0
        details = str(exc)
        log_step(client, run_id, label, "FAIL", None, elapsed, details)
        logger.exception("Night step %s FAIL за %.1f сек", step_num, elapsed)
        if send_tg:
            # VERDICT_FIRST_FACT_ONLY_2026-08-14 (ported from big_analytics_v5 bd192a2):
            # Telegram gets the fact only — which step stopped the run — never the
            # exception message/traceback. Full traceback: logger.exception above —
            # only lands in a file if THIS run was launched with `> log 2>&1`
            # (RUNBOOK.md "Ночной pipeline под cron/nohup" example); a bare foreground
            # run has no log file, only the terminal. No v6 night cron entry exists
            # yet — whoever schedules it must use the redirect form. Exception text
            # (until then): data_quality_log.details for this run_id.
            msg = build_pipeline_error_message(
                pipeline_name="big_analytics_v6 night", run_id=run_id, failed_step=label,
            )
            _send_tg(render_html(msg))
        return False, elapsed, details


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only-step", type=int)
    parser.add_argument("--no-tg", action="store_true")
    parser.add_argument("--list-steps", action="store_true")
    args = parser.parse_args(argv)

    if args.list_steps:
        for step_num, module_path, label in NIGHT_STEPS:
            print(f"{step_num}\t{label}\t{module_path}")
        print("legacy_pg_not_in_night:")
        for path, reason in LEGACY_PG_NOT_IN_NIGHT.items():
            print(f"- {path}: {reason}")
        return 0

    steps = _selected_steps(args.only_step)
    if not steps:
        raise SystemExit(f"Night step not found: {args.only_step}")

    run_id = uuid.uuid4().hex[:12]
    started = datetime.now()
    client = get_client()
    ensure_quality_log(client)

    logger.info("big_analytics_v6_ch night pipeline start run_id=%s", run_id)
    if not args.no_tg:
        _send_tg(
            f"🌙 <b>big_analytics_v6 night</b> запущен\n"
            f"{started.strftime('%Y-%m-%d %H:%M')}\n"
            f"run_id={run_id}"
        )

    results: list[tuple[str, bool, float, str]] = []
    failed = False
    for step_num, module_path, label in steps:
        ok, elapsed, details = run_step(client, run_id, step_num, module_path, label, send_tg=not args.no_tg)
        results.append((label, ok, elapsed, details))
        if not ok:
            failed = True
            break

    finished = datetime.now()
    total = (finished - started).total_seconds()
    lines = ["🌙 <b>big_analytics_v6 night</b> завершён"]
    lines.append(f"{started.strftime('%Y-%m-%d')} {started.strftime('%H:%M')}→{finished.strftime('%H:%M')}")
    lines.append(f"run_id={run_id}")
    lines.append("")
    for label, ok, elapsed, details in results:
        icon = "✅" if ok else "❌"
        # VERDICT_FIRST_FACT_ONLY_2026-08-14: `details` on a FAIL row is the raw
        # exception text (see run_step) — keep it out of the summary too; OK rows
        # keep their informational details (row counts etc.) unchanged.
        tail = f" — {details[:160]}" if details and ok else ""
        lines.append(f"{icon} {label}: {_fmt_dur(elapsed)}{tail}")
    lines.append("")
    lines.append("⚠️ Есть ошибки" if failed else "✅ Всё прошло успешно")
    lines.append(f"⏱ Итого: {_fmt_dur(total)}")
    report = "\n".join(lines)
    logger.info("\n%s", report)
    if not args.no_tg:
        _send_tg(report)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
