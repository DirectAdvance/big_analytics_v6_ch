"""Load monthly city tier snapshots from Google Sheets into ClickHouse."""

from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from config.ch_db import get_client
from config.ch_settings import DATE_FROM
from config.ch_utils import SAFE_QUERY_SETTINGS

logger = logging.getLogger("pipeline.step0.city_tier")

SPREADSHEET_ID = "1-FfqACiIFDHxtlL4zANIJs3VqLqZD-f15QKDGxEaqic"
DATA_RANGE = "Лист1!A1:B"
DATABASE = "ad_analytics"
TABLE = "gsheet_city_tier"
SHEET_TZ = ZoneInfo("Europe/Moscow")
READ_SHEET_ATTEMPTS = 4
TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}

_NOTE_RE = re.compile(r"^(?P<city>[^(]+?)\s*\((?P<note>.+)\)\s*$")
_TIER_RE = re.compile(r"^(?:tier|тир)\s*(?P<n>[1-9]\d*)$", re.IGNORECASE)


def _find_service_account() -> str:
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
    raise FileNotFoundError("Google service_account.json not found")


def read_sheet() -> list[list[str]]:
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    creds = Credentials.from_service_account_file(
        _find_service_account(),
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    for attempt in range(1, READ_SHEET_ATTEMPTS + 1):
        try:
            resp = (
                service.spreadsheets()
                .values()
                .get(
                    spreadsheetId=SPREADSHEET_ID,
                    range=DATA_RANGE,
                    valueRenderOption="FORMATTED_VALUE",
                )
                .execute()
            )
            return resp.get("values", [])
        except HttpError as exc:
            status = getattr(exc.resp, "status", None)
            if status not in TRANSIENT_HTTP_STATUSES or attempt == READ_SHEET_ATTEMPTS:
                raise
            delay = 5 * attempt
            logger.warning(
                "gsheet_city_tier: Google Sheets HTTP %s, retry %d/%d in %ds",
                status,
                attempt + 1,
                READ_SHEET_ATTEMPTS,
                delay,
            )
            time.sleep(delay)
    raise RuntimeError("unreachable Google Sheets retry state")


def month_start(now: datetime | None = None) -> date:
    now = now or datetime.now(SHEET_TZ)
    return date(now.year, now.month, 1)


def _cell(row: list[str], idx: int) -> str:
    return " ".join(str(row[idx] if idx < len(row) else "").split())


def parse_city(raw: str) -> tuple[str, str | None]:
    match = _NOTE_RE.match(raw)
    if not match:
        return raw, None
    return match.group("city").strip(), match.group("note").strip()


def normalize_tier(raw: str) -> str | None:
    match = _TIER_RE.match(raw.strip())
    if not match:
        return None
    return f"tier{match.group('n')}"


def parse_rows(raw_rows: list[list[str]], month: date) -> tuple[list[tuple], list[tuple[str, str]]]:
    rows, skipped, seen = [], [], set()
    for raw in raw_rows:
        city_raw, tier_raw = _cell(raw, 0), _cell(raw, 1)
        if not city_raw or not tier_raw or city_raw.casefold() == "гео":
            continue
        tier = normalize_tier(tier_raw)
        if tier is None:
            skipped.append((city_raw, tier_raw))
            continue
        city, note = parse_city(city_raw)
        key = (city, tier, note)
        if key in seen:
            continue
        seen.add(key)
        rows.append((month, city, tier, note, city_raw))
    return rows, skipped


def find_conflicts(rows: list[tuple]) -> dict[str, list[str]]:
    tiers_by_city: dict[str, set[str]] = {}
    for _month, city, tier, _note, _raw in rows:
        tiers_by_city.setdefault(city, set()).add(tier)
    return {city: sorted(tiers) for city, tiers in tiers_by_city.items() if len(tiers) > 1}


def resolve_conflicts(rows: list[tuple]) -> tuple[list[tuple], dict[str, dict]]:
    by_city: dict[str, list[tuple]] = {}
    for row in rows:
        by_city.setdefault(row[1], []).append(row)

    result, resolved = [], {}
    for city, city_rows in by_city.items():
        if len(city_rows) == 1:
            result.append(city_rows[0])
            continue
        noted = [row for row in city_rows if row[3]]
        winner = noted[0] if len(noted) == 1 else city_rows[0]
        resolved[city] = {
            "tier": winner[2],
            "dropped": [row[2] for row in city_rows if row is not winner],
            "by": "note" if len(noted) == 1 else "sheet_order",
        }
        result.append(winner)
    return result, resolved


def _month_range(start: date, end_exclusive: date) -> list[date]:
    months, year, month = [], start.year, start.month
    while (year, month) < (end_exclusive.year, end_exclusive.month):
        months.append(date(year, month, 1))
        month, year = (1, year + 1) if month == 12 else (month + 1, year)
    return months


def months_to_seed(start_month: date, current_month: date, existing_months: set[date]) -> list[date]:
    return [month for month in _month_range(start_month, current_month) if month not in existing_months]


def build_backfill_rows(rows: list[tuple], month: date) -> list[tuple]:
    return [(month, city, tier, note, city_raw, True) for _m, city, tier, note, city_raw in rows]


def _ensure_table(client) -> None:
    client.command(
        f"""
        CREATE TABLE IF NOT EXISTS {DATABASE}.{TABLE}
        (
            month Date,
            gorod String,
            tier LowCardinality(String),
            note Nullable(String),
            gorod_raw String,
            is_backfill Bool,
            loaded_at DateTime DEFAULT now()
        )
        ENGINE = MergeTree
        ORDER BY (month, gorod)
        """,
        settings=SAFE_QUERY_SETTINGS,
    )


def sync_city_tier(client=None, month: date | None = None) -> dict:
    month = month or month_start()
    rows, skipped = parse_rows(read_sheet(), month)
    conflicts = find_conflicts(rows)
    rows, resolved = resolve_conflicts(rows)
    if not rows:
        logger.warning("gsheet_city_tier: empty sheet for %s, keeping existing ClickHouse rows", month)
        return {"month": month, "rows": 0, "skipped": len(skipped), "seeded_months": [], "seeded_rows": 0}

    client = client or get_client()
    _ensure_table(client)
    existing_months = {
        row[0]
        for row in client.query(
            f"SELECT DISTINCT month FROM {DATABASE}.{TABLE}",
            settings=SAFE_QUERY_SETTINGS,
        ).result_rows
    }

    client.command(
        f"ALTER TABLE {DATABASE}.{TABLE} DELETE WHERE month = toDate('{month.isoformat()}')",
        settings={**SAFE_QUERY_SETTINGS, "mutations_sync": 1},
    )
    current_rows = [(m, city, tier, note, raw, False) for m, city, tier, note, raw in rows]
    client.insert(
        f"{DATABASE}.{TABLE}",
        current_rows,
        column_names=["month", "gorod", "tier", "note", "gorod_raw", "is_backfill"],
    )

    start = date.fromisoformat(DATE_FROM)
    start_month = date(start.year, start.month, 1)
    seed_months = months_to_seed(start_month, month, existing_months)
    seeded_rows = 0
    for seed_month in seed_months:
        seed_rows = build_backfill_rows(rows, seed_month)
        if seed_rows:
            client.insert(
                f"{DATABASE}.{TABLE}",
                seed_rows,
                column_names=["month", "gorod", "tier", "note", "gorod_raw", "is_backfill"],
            )
            seeded_rows += len(seed_rows)

    logger.info(
        "gsheet_city_tier: month=%s rows=%d seeded_months=%d seeded_rows=%d",
        month,
        len(rows),
        len(seed_months),
        seeded_rows,
    )
    if skipped:
        logger.warning("gsheet_city_tier: skipped rows with unknown tier: %s", skipped)
    if resolved:
        logger.info("gsheet_city_tier: resolved conflicts for %s: %s", month, resolved)
    return {
        "month": month,
        "rows": len(rows),
        "skipped": len(skipped),
        "conflicts": conflicts,
        "resolved": resolved,
        "seeded_months": [m.isoformat() for m in seed_months],
        "seeded_rows": seeded_rows,
    }
