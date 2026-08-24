"""v6_ch weekly direct_account_reviews collector: Google Sheets + Direct Reports API v5.

Registered in `step_cron_night/pipeline_night.py` NIGHT_STEPS but excluded from the
default (nightly) run via WEEKLY_DEFAULT_STEPS — cron runs it once a week with
`--only-step` (see step_cron_night/README.md for the exact line).
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pipeline_mutex  # noqa: E402
from config.ch_db import get_client  # noqa: E402
from pipeline import BA6_PIPELINE_LOCK_PATH  # noqa: E402
from step_cron_night.direct_account_reviews import fetch_direct_stats, load_reviews  # noqa: E402

logger = logging.getLogger("pipeline.direct_account_reviews")


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    client = get_client()
    t0 = time.perf_counter()

    accounts = load_reviews.sync_reviews_accounts(client)
    stats = fetch_direct_stats.sync_reviews_stats(client)

    details = f"accounts={accounts['rows']:,}, {stats['details']}"
    logger.info("direct_account_reviews завершён за %.1f сек: %s", time.perf_counter() - t0, details)
    return {"rows": stats["rows"], "details": details}


def main() -> None:
    """CLI entry point only (`run()` itself stays lock-free — pipeline_night.py's own main()
    already holds this same flock when it calls `run()` as night step 107; acquiring it again
    from inside `run()` would self-deadlock). A direct
    `python3 -m step_cron_night.direct_account_reviews.run` bypassed pipeline_mutex entirely,
    letting this collector's `ALTER TABLE ... DELETE` (fetch_direct_stats.py) overlap a step3
    read of the same tables — close that hole here."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    try:
        pipeline_mutex.acquire("ba6_direct_account_reviews_standalone", lock_path=BA6_PIPELINE_LOCK_PATH)
    except pipeline_mutex.PipelineBusy as busy:
        logger.warning("direct_account_reviews standalone run skipped: BA6 pipeline lock busy (%s)", busy)
        return
    print(run())


if __name__ == "__main__":
    main()
