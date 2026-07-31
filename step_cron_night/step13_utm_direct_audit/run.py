"""v6_ch UTM audit builder from raw_data.metrika_yandex_utm_daily."""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from config.ch_db import get_client
from step_cron_night.metrika_raw_builders import build_check_utm

log = logging.getLogger("pipeline.utm_direct_audit")


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    client = get_client()
    t0 = time.perf_counter()
    date_from = os.getenv("CHECK_UTM_DATE_FROM", "2026-01-01")
    date_to = os.getenv("CHECK_UTM_DATE_TO") or None
    check_rows, bad_rows = build_check_utm(client, date_from=date_from, date_to=date_to)
    details = f"check_utm={check_rows:,}, check_utm_fuck_direct={bad_rows:,}"
    log.info("utm_direct_audit v6_ch завершён за %.1f сек: %s", time.perf_counter() - t0, details)
    return {"rows": check_rows, "details": details}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())


if __name__ == "__main__":
    main()
