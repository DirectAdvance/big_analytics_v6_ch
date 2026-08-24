"""Google Sheets "Power BI" A:E -> ad_analytics.yandex_direct_account_reviews (ClickHouse).

Ported from archive/postgres_legacy_2026_07_31/step_cron_night/direct_account_reviews/load_reviews.py.
Same spreadsheet/range/columns; sink is ClickHouse instead of PostgreSQL public.*.
"""

from __future__ import annotations

import logging
from pathlib import Path

from config.ch_db import get_client
from config.ch_utils import SAFE_QUERY_SETTINGS, swap_shadow

logger = logging.getLogger("pipeline.direct_account_reviews.load_reviews")

SPREADSHEET_ID = "1ec_qL-1zSfXA-cftEcFjrMJmM9yI78qVVpTxICGCQFU"
SHEET_NAME = "Power BI"
DATA_RANGE = f"{SHEET_NAME}!A:E"

DATABASE = "ad_analytics"
TABLE = "yandex_direct_account_reviews"


def _find_service_account() -> str:
    """Walk up to <repo>/.secret/service_account.json, falling back to home candidates
    (same search order as `step0_sync_local/load_city_tier.py`)."""
    candidates: list[Path] = []
    path = Path(__file__).resolve()
    for parent in path.parents:
        candidates.append(parent / ".secret" / "service_account.json")
    candidates.extend(
        [
            Path.home() / ".secret" / "service_account.json",
            Path.home() / ".secret" / "google" / "service_account.json",
            Path.home() / "cedar-gearbox-464117-e5-676d6cc8937e.json",
        ]
    )
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    raise FileNotFoundError("Google service_account.json not found (.secret/service_account.json)")


def read_sheet() -> list[list[str]]:
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_service_account_file(
        _find_service_account(),
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    resp = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=SPREADSHEET_ID, range=DATA_RANGE, valueRenderOption="FORMATTED_VALUE")
        .execute()
    )
    return resp.get("values", [])


def _cell(row: list[str], idx: int) -> str:
    return row[idx].strip() if idx < len(row) else ""


def parse_records(raw_rows: list[list[str]]) -> list[tuple]:
    """Row 0 is the header (город/салон/аккаунт/сайт/агентский аккаунт); rows 1+ are data,
    one row per account. Returns (id, город, салон, аккаунт, сайт, агентский_аккаунт) tuples,
    `id` = 1-based position in the sheet (pick key for step3's `argMax(col, id)` dedupe-by-
    account join).

    This table has no date/validity column, so it can only hold ONE `сайт` per `аккаунт`.
    step3's dedupe is correct when a repeated `аккаунт` is a genuine data-entry duplicate
    (identical `сайт`) — but when `сайт` genuinely changed over time for that account, the
    model has no way to represent both periods: `argMax` will silently pick one and
    mis-attribute the other. Warn instead of resolving it quietly, so a human decides
    whether the sheet needs a validity-period column (see step3.py `_build_reviews_sql`)."""
    records = []
    by_account: dict[str, list[tuple[int, str]]] = {}
    for idx, row in enumerate(raw_rows[1:], start=1):
        account = _cell(row, 2)
        if not account:
            continue
        site = _cell(row, 3)
        records.append((idx, _cell(row, 0), _cell(row, 1), account, site, _cell(row, 4)))
        by_account.setdefault(account, []).append((idx, site))

    for account, entries in by_account.items():
        sites = {site for _, site in entries}
        if len(entries) > 1 and len(sites) > 1:
            logger.warning(
                "%s: аккаунт %r has %d rows with DIFFERENT сайт (%s) — step3's "
                "argMax(col, id) dedupe cannot represent a site that changed over time "
                "(no date column here); it will silently mis-attribute one period's rows "
                "to the other account's сайт. Rows (sheet_id, сайт): %s",
                TABLE, account, len(entries), sorted(sites), entries,
            )
    return records


def _ensure_table(client) -> None:
    client.command(
        f"""
        CREATE TABLE IF NOT EXISTS {DATABASE}.{TABLE}
        (
            id Int32,
            `город` String,
            `салон` String,
            `аккаунт` String,
            `сайт` String,
            `агентский аккаунт` String,
            loaded_at DateTime DEFAULT now()
        )
        ENGINE = MergeTree
        ORDER BY (`аккаунт`, id)
        """,
        settings=SAFE_QUERY_SETTINGS,
    )


def sync_reviews_accounts(client=None) -> dict:
    """Sheet is read and validated (non-empty) BEFORE anything touches the table. Load into
    a shadow + `swap_shadow` (same pattern as step3_build_sources/step3.py) rather than
    `TRUNCATE` + insert — a crash mid-load used to leave the reference table empty until
    the next run, and step3 reads it every day."""
    records = parse_records(read_sheet())
    if not records:
        raise RuntimeError("Google Sheets 'Power BI' A:E returned no account rows")

    client = client or get_client()
    _ensure_table(client)
    shadow = f"{DATABASE}.{TABLE}_new"
    client.command(f"DROP TABLE IF EXISTS {shadow} SYNC", settings=SAFE_QUERY_SETTINGS)
    client.command(f"CREATE TABLE {shadow} AS {DATABASE}.{TABLE}", settings=SAFE_QUERY_SETTINGS)
    client.insert(
        shadow,
        records,
        column_names=["id", "город", "салон", "аккаунт", "сайт", "агентский аккаунт"],
    )
    swap_shadow(client, f"{DATABASE}.{TABLE}", shadow)
    logger.info("%s: loaded %d records from Google Sheets", TABLE, len(records))
    return {"rows": len(records)}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(sync_reviews_accounts())


if __name__ == "__main__":
    main()
