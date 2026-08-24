"""One-off: reload ClickHouse ad_analytics.yandex_direct_reports_reviews from the frozen
Victory PostgreSQL yandex_direct_raw snapshot (max Date 2026-08-16 — the old v5 weekly cron
was disabled, this is the only copy of 2026-01-01..2026-08-16 history).

Run once during cutover; from here on `step_cron_night/direct_account_reviews/fetch_direct_stats.py`
owns the table via its own weekly incremental sync. Guarded (2026-08-24 director rework,
IMPORTANT #3, after this script's TRUNCATE silently rewound the live table 2026-08-24 mid
incident-recovery): refuses to run against a populated table without --force, and never
touches `yandex_direct_account_reviews` at all — that table is Sheets-managed
(`load_reviews.sync_reviews_accounts()`), this script has no business rewinding it back to a
PG snapshot.

F10 (same rework, second pass): `--force` on a populated table still goes through
`fetch_direct_stats._check_swap_safe` before the swap — an empty or truncated PG fetch
(wrong search_path, half-open connection, partial snapshot) refuses instead of swapping an
empty/short shadow into live. `--force` means "I intend to reload", not "I intend to zero it
out".

    python3 step_cron_night/direct_account_reviews/backfill_from_postgres.py --force
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from config.ch_db import get_client  # noqa: E402
from config.ch_utils import SAFE_QUERY_SETTINGS, swap_shadow  # noqa: E402
from config.settings import DB_DST  # noqa: E402
from step_cron_night.direct_account_reviews.fetch_direct_stats import (  # noqa: E402
    INSERT_COLUMNS as STATS_COLUMNS,
    STATS_DATABASE,
    STATS_TABLE,
    _check_swap_safe,
    _ensure_table as _ensure_stats_table,
)

logger = logging.getLogger("pipeline.direct_account_reviews.backfill")

STATS_TABLE_FULL = f"{STATS_DATABASE}.{STATS_TABLE}"


def _pg_connect():
    return psycopg2.connect(**DB_DST)


def _fetch_stats_rows() -> list[tuple]:
    pg = _pg_connect()
    try:
        with pg.cursor() as cur:
            cur.execute(
                'SELECT login, "Date", "CampaignId", "CampaignName", "AdGroupId", "AdGroupName", '
                '"AdNetworkType", "Device", "Impressions", "Clicks", "Cost", "RlAdjustmentId" '
                'FROM yandex_direct_raw.yandex_direct_reports_reviews ORDER BY id'
            )
            return cur.fetchall()
    finally:
        pg.close()


def backfill(force: bool = False) -> dict:
    client = get_client()
    _ensure_stats_table(client)

    live_rows = int(
        client.query(f"SELECT count() FROM {STATS_TABLE_FULL}", settings=SAFE_QUERY_SETTINGS).result_rows[0][0]
    )
    if live_rows and not force:
        raise RuntimeError(
            f"backfill_from_postgres: refusing to run — {STATS_TABLE_FULL} already has "
            f"{live_rows:,} rows (this is a one-off cutover tool, not a recurring resync; a "
            f"later run would silently rewind live data to the frozen 2026-08-16 PG snapshot, "
            f"exactly what happened during the 2026-08-24 incident). Pass --force to override."
        )

    stats_rows = _fetch_stats_rows()

    shadow = f"{STATS_TABLE_FULL}_new"
    client.command(f"DROP TABLE IF EXISTS {shadow} SYNC", settings=SAFE_QUERY_SETTINGS)
    client.command(f"CREATE TABLE {shadow} AS {STATS_TABLE_FULL}", settings=SAFE_QUERY_SETTINGS)
    if stats_rows:
        client.insert(shadow, stats_rows, column_names=STATS_COLUMNS)

    # F10 (director rework 2026-08-24): `--force` means "reload from PG", not "zero out the
    # table" — reuse the same pre-swap volume guard fetch_direct_stats.py's weekly sync uses.
    # ok_count=0 catches an empty fetch (wrong search_path, connection to the wrong DB) via its
    # own raise; the row/cost floor vs. the CURRENT live table catches a truncated snapshot
    # even when it isn't literally empty. Same failure contract as the rest of this script:
    # drop the shadow, re-raise, live table untouched.
    try:
        _check_swap_safe(client, STATS_TABLE_FULL, shadow, len(stats_rows))
    except RuntimeError:
        client.command(f"DROP TABLE IF EXISTS {shadow} SYNC", settings=SAFE_QUERY_SETTINGS)
        raise

    swap_shadow(client, STATS_TABLE_FULL, shadow)

    details = f"stats={len(stats_rows):,}"
    logger.info("backfill_from_postgres: %s", details)
    return {"stats": len(stats_rows), "details": details}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="Run even if the live table already has rows")
    args = parser.parse_args()
    print(backfill(force=args.force))


if __name__ == "__main__":
    main()
