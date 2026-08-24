"""Yandex Direct Reports API v5 -> ad_analytics.yandex_direct_reports_reviews (ClickHouse).

Ported from
archive/postgres_legacy_2026_07_31/step_cron_night/direct_account_reviews/fetch_direct_stats.py.
Same incremental-per-account strategy and Reports API queue handling (the ~40 min
bottleneck this whole collector exists to isolate into a weekly night step); sink is
ClickHouse instead of PostgreSQL public.*.

Incremental strategy (per account, same as the archived script):
  1. Table empty / account not seen before -> full load from FULL_DATE_FROM.
  2. Account has rows           -> date_from = max(Date) - SAFETY_DAYS,
                                    delete rows >= date_from, reload date_from..today.

ATOMICITY (2026-08-24 postmortem — PID 32096 destroyed 409 rows / 6 logins before this fix):
`delete_and_insert` never touches the live table directly. Every run works on a shadow copy
(`{STATS_TABLE}_new`, seeded from the live table) and only `EXCHANGE TABLES`-swaps it into
`STATS_TABLE` if EVERY login wrote cleanly. Any single write failure (including the Date
serialization bug that caused the incident) drops the shadow and raises — the live table is
guaranteed byte-identical to how it started. A run also aborts early (breaks the per-login
loop) once failures reach FAILURE_THRESHOLD, instead of grinding through all remaining logins.

RECONCILIATION CRITERION (read this before comparing a run to any baseline —
2026-08-24 rework of a wrong criterion director caught): because of SAFETY_DAYS above,
every run re-pulls `max(Date) - SAFETY_DAYS .. today` per login from the live Reports
API, and Yandex Direct legitimately restates figures retroactively within that window.
So:
  - Rows with `Date <= max(Date) - SAFETY_DAYS` at the time of the PREVIOUS run
    (i.e. strictly outside that run's re-pull window) MUST be unchanged by a later run.
    Any movement there is a real defect (data corruption, a fetch bug), not restatement.
  - Rows inside the last SAFETY_DAYS window MAY legitimately move between runs. Quantify
    any delta there and attribute it to API re-statement — do not wave it away, but do not
    treat it as a defect either.
Do NOT compare a full-table snapshot against a baseline with a flat cutoff date picked
after the fact without accounting for SAFETY_DAYS, or the "delta" you get is mostly noise
from legitimate re-statement, not signal.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path
from time import sleep

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

for _p in Path(__file__).resolve().parents:
    if (_p / ".secret" / "loader.py").exists():
        sys.path.insert(0, str(_p / ".secret"))
        break

from loader import load_yandex_direct, load_yandex_direct_reviews  # noqa: E402

from config.ch_db import get_client  # noqa: E402
from config.ch_utils import SAFE_QUERY_SETTINGS, swap_shadow  # noqa: E402

logger = logging.getLogger("pipeline.direct_account_reviews.fetch_direct_stats")

ACCOUNTS_TABLE = "ad_analytics.yandex_direct_account_reviews"
STATS_DATABASE = "ad_analytics"
STATS_TABLE = "yandex_direct_reports_reviews"
STATS_TABLE_FULL = f"{STATS_DATABASE}.{STATS_TABLE}"

FULL_DATE_FROM = "2026-01-01"
SAFETY_DAYS = 7
# Abort the whole run (no swap, live table untouched) once this many logins fail to write —
# 315/272 failures grinding on to account 109 is what destroyed data on 2026-08-24.
FAILURE_THRESHOLD = 3

# Pre-swap volume guard (2026-08-24 director rework, IMPORTANT #2): a Reports API 200 with
# only a header row makes parse_tsv return [], delete_and_insert then clears the SAFETY_DAYS
# window and writes nothing, and the per-login loop still counts it `ok` — indistinguishable
# from a genuinely inactive account without comparing row/cost totals against the live table.
# Tolerance from the table this guard runs against (2026-08-24): 6284 rows / 88 logins over
# 2026-01-01..today (~230d) is ~27 rows/day system-wide; a run only re-pulls SAFETY_DAYS=7
# days per login, so one login's window legitimately going empty (Yandex retroactively
# zeroing a few rows) costs a handful of rows out of 6284 — nowhere near 5%. 0.95 (~314 rows
# floor on today's table) comfortably absorbs several logins' worth of real re-statement
# while still catching a run that silently emptied a meaningful slice of the table (broken
# token file, parse regression, partial API outage).
SWAP_MIN_RETENTION_RATIO = 0.95

REPORTS_URL = "https://api.direct.yandex.com/json/v5/reports"
RETRY_TIMEOUT = 20
MAX_ERRORS = 5

FIELD_NAMES = [
    "Date", "CampaignId", "CampaignName", "AdGroupId", "AdGroupName",
    "AdNetworkType", "Device", "Impressions", "Clicks", "Cost", "RlAdjustmentId",
]

INSERT_COLUMNS = [
    "login", "Date", "CampaignId", "CampaignName", "AdGroupId", "AdGroupName",
    "AdNetworkType", "Device", "Impressions", "Clicks", "Cost", "RlAdjustmentId",
]

SESSION = requests.Session()

# Accounts with no working token as of the last full audit (archived
# fetch_direct_stats.py) — known, not reported as an anomaly.
KNOWN_NO_TOKEN = {
    "porg-26u7d4o2", "porg-3xrlykv5", "porg-4cd6tcsg",
    "porg-ej4tydh7", "porg-ew54weam", "porg-gt2if6bv", "porg-hihccjx7",
}


def _all_tokens() -> list[tuple[str, str]]:
    """[(oauth_token, login), ...] — reviews-specific tokens first (cache hit for most
    accounts), then the main agency tokens, same order as the archived script."""
    reviews_tokens = list(load_yandex_direct_reviews())
    reviews_set = set(reviews_tokens)
    main_tokens = [
        (spec["oauth_token"], login)
        for login, spec in load_yandex_direct().get("tokens", {}).items()
        if spec.get("oauth_token")
    ]
    return reviews_tokens + [t for t in main_tokens if t not in reviews_set]


def _ensure_table(client) -> None:
    client.command(
        f"""
        CREATE TABLE IF NOT EXISTS {STATS_DATABASE}.{STATS_TABLE}
        (
            login String,
            `Date` Date,
            `CampaignId` Int64,
            `CampaignName` String,
            `AdGroupId` Int64,
            `AdGroupName` String,
            `AdNetworkType` String,
            `Device` String,
            `Impressions` Int64,
            `Clicks` Int64,
            `Cost` Decimal(18, 6),
            `RlAdjustmentId` Nullable(Int64),
            loaded_at DateTime DEFAULT now()
        )
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(`Date`)
        ORDER BY (login, `Date`)
        """,
        settings=SAFE_QUERY_SETTINGS,
    )


def get_logins(client) -> list[str]:
    rows = client.query(
        f"SELECT DISTINCT `аккаунт` FROM {ACCOUNTS_TABLE} WHERE `аккаунт` != ''",
        settings=SAFE_QUERY_SETTINGS,
    ).result_rows
    return sorted(row[0] for row in rows)


def get_login_date_from(client, table: str, login: str) -> str:
    """`Date` is a non-nullable ClickHouse column, so `max(Date)` over zero matching rows
    returns the type default 1970-01-01, not NULL — `maxOrNull` is what makes "no prior data"
    an explicit, checkable case instead of silent arithmetic on a garbage epoch date (was
    producing date_from='1969-12-25' for every never-seen login, deleting the account's
    entire history on the next write)."""
    max_date = client.query(
        f"SELECT maxOrNull(`Date`) FROM {table} WHERE login = {{login:String}}",
        parameters={"login": login},
        settings=SAFE_QUERY_SETTINGS,
    ).result_rows[0][0]
    if max_date is None:
        return FULL_DATE_FROM
    date_from = max_date - timedelta(days=SAFETY_DAYS)
    return max(date_from, date.fromisoformat(FULL_DATE_FROM)).strftime("%Y-%m-%d")


def delete_and_insert(client, table: str, login: str, date_from: str, rows: list[tuple]) -> None:
    """`table` is the shadow copy during a real run (`sync_reviews_stats` never passes the
    live table) — a failed `client.insert` here only corrupts the disposable shadow, which
    gets dropped instead of swapped in. See module docstring, ATOMICITY."""
    client.command(
        f"ALTER TABLE {table} DELETE "
        f"WHERE login = {{login:String}} AND `Date` >= {{date_from:String}}",
        parameters={"login": login, "date_from": date_from},
        settings={**SAFE_QUERY_SETTINGS, "mutations_sync": 1},
    )
    if rows:
        client.insert(table, rows, column_names=INSERT_COLUMNS)


def _check_swap_safe(client, live_table: str, shadow_table: str, ok_count: int) -> None:
    """Called right before `swap_shadow`; raises instead of letting a successful-but-empty
    run silently overwrite the live table. See SWAP_MIN_RETENTION_RATIO above for the
    tolerance and its derivation. Caller is responsible for dropping the shadow on raise
    (same contract as every other failure path in `sync_reviews_stats`)."""
    if ok_count == 0:
        raise RuntimeError(
            "fetch_direct_stats: refusing swap — ok_count=0, every login skipped or failed, "
            "shadow carries no fresh data"
        )
    live_rows, live_cost = client.query(
        f"SELECT count(), sum(`Cost`) FROM {live_table}", settings=SAFE_QUERY_SETTINGS
    ).result_rows[0]
    shadow_rows, shadow_cost = client.query(
        f"SELECT count(), sum(`Cost`) FROM {shadow_table}", settings=SAFE_QUERY_SETTINGS
    ).result_rows[0]
    live_cost = float(live_cost or 0)
    shadow_cost = float(shadow_cost or 0)
    floor_rows = live_rows * SWAP_MIN_RETENTION_RATIO
    floor_cost = live_cost * SWAP_MIN_RETENTION_RATIO
    if shadow_rows < floor_rows or shadow_cost < floor_cost:
        raise RuntimeError(
            f"fetch_direct_stats: refusing swap — shadow rows={shadow_rows} (live={live_rows}, "
            f"floor={floor_rows:.0f}), shadow cost={shadow_cost:.2f} (live={live_cost:.2f}, "
            f"floor={floor_cost:.2f}); looks like a successful-but-empty write, not a real update"
        )


def _headers(token: str, login: str) -> dict:
    return {
        "Authorization": "Bearer " + token,
        "Client-Login": login,
        "Accept-Language": "ru",
        "processingMode": "auto",
        "returnMoneyInMicros": "false",
        "skipReportHeader": "true",
        "skipColumnHeader": "false",
        "skipReportSummary": "true",
    }


def _body(login: str, date_from: str, date_to: str) -> str:
    return json.dumps({
        "params": {
            "SelectionCriteria": {"DateFrom": date_from, "DateTo": date_to},
            "FieldNames": FIELD_NAMES,
            "ReportName": f"reviews_{login}_{date_from}_{date_to}",
            "Page": {"Limit": 10_000_000},
            "ReportType": "CUSTOM_REPORT",
            "DateRangeType": "CUSTOM_DATE",
            "Format": "TSV",
            "IncludeVAT": "YES",
            "IncludeDiscount": "NO",
        }
    })


def fetch_report(token: str, login: str, date_from: str, date_to: str) -> str | None:
    """TSV text on success, None on access denial (400/403/404); raises RuntimeError
    on repeated network/queue errors. Handles the Reports API async queue (201/202)."""
    headers = _headers(token, login)
    body = _body(login, date_from, date_to)
    err_count = 0
    while True:
        try:
            resp = SESSION.post(REPORTS_URL, body, headers=headers, timeout=120)
            resp.encoding = "utf-8"
        except requests.RequestException as exc:
            err_count += 1
            if err_count >= MAX_ERRORS:
                raise RuntimeError(f"network errors > {MAX_ERRORS}: {exc}") from exc
            sleep(10)
            continue

        if resp.status_code == 200:
            return resp.text
        if resp.status_code in (400, 403, 404):
            return None
        if resp.status_code == 201:
            sleep(RETRY_TIMEOUT)
            continue
        if resp.status_code == 202:
            sleep(int(resp.headers.get("retryIn", RETRY_TIMEOUT)))
            continue
        err_count += 1
        if err_count >= MAX_ERRORS:
            raise RuntimeError(f"status {resp.status_code} repeated {MAX_ERRORS} times")
        sleep(5)


def _to_int(value: str | None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value: str | None) -> float:
    try:
        return float((value or "0").replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


def _to_date(value: str | None, login: str) -> date:
    """`Date` is written as `datetime.date`, never `str` — clickhouse-connect's Date codec
    does `(value - epoch).days` and raises TypeError on a str (this is exactly the 2026-08-24
    bug: 100% of inserts failed here, silently, because the DELETE half of delete_and_insert
    had already succeeded — see ATOMICITY). Raising here, before any table is touched, is what
    makes parse_tsv's caller safe to run before the delete."""
    if not value:
        raise ValueError(f"{login}: report row has no Date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{login}: unparseable Date {value!r}") from exc


def parse_tsv(tsv_text: str, login: str) -> list[tuple]:
    lines = tsv_text.strip().splitlines()
    if len(lines) < 2:
        return []
    header = lines[0].split("\t")
    rows = []
    for line in lines[1:]:
        if not line.strip():
            continue
        cells = dict(zip(header, line.split("\t")))

        def v(field: str) -> str | None:
            val = cells.get(field, "").strip()
            return None if val in ("", "--") else val

        rows.append((
            login,
            _to_date(v("Date"), login),
            _to_int(v("CampaignId")) or 0,
            v("CampaignName") or "",
            _to_int(v("AdGroupId")) or 0,
            v("AdGroupName") or "",
            v("AdNetworkType") or "",
            v("Device") or "",
            _to_int(v("Impressions")) or 0,
            _to_int(v("Clicks")) or 0,
            _to_float(v("Cost")),
            _to_int(v("RlAdjustmentId")),
        ))
    return rows


def sync_reviews_stats(client=None) -> dict:
    """Writes go to a shadow copy of STATS_TABLE; the shadow only replaces the live table
    (via `swap_shadow`, same EXCHANGE TABLES primitive as step3.py/load_reviews.py) if every
    login wrote cleanly. Any error — including one login's write failing — drops the shadow
    and raises instead of swapping, so a partial/bad run leaves the live table exactly as it
    was. See module docstring, ATOMICITY."""
    client = client or get_client()
    _ensure_table(client)
    logins = get_logins(client)
    if not logins:
        raise RuntimeError(f"{ACCOUNTS_TABLE} has no accounts to fetch stats for")

    shadow = f"{STATS_TABLE_FULL}_new"
    client.command(f"DROP TABLE IF EXISTS {shadow} SYNC", settings=SAFE_QUERY_SETTINGS)
    client.command(f"CREATE TABLE {shadow} AS {STATS_TABLE_FULL}", settings=SAFE_QUERY_SETTINGS)
    client.command(f"INSERT INTO {shadow} SELECT * FROM {STATS_TABLE_FULL}", settings=SAFE_QUERY_SETTINGS)

    tokens = _all_tokens()
    ok_count = skip_count = err_count = 0
    total_rows = 0
    skip_logins: list[str] = []
    aborted_at: str | None = None
    date_to = date.today().strftime("%Y-%m-%d")

    for idx, login in enumerate(logins, start=1):
        date_from = get_login_date_from(client, shadow, login)
        logger.info("[%d/%d] %s %s -> %s", idx, len(logins), login, date_from, date_to)

        tsv_text = None
        for token, agency in tokens:
            try:
                result = fetch_report(token, login, date_from, date_to)
            except RuntimeError as exc:
                logger.warning("  %s token %s error: %s", login, agency, exc)
                continue
            if result is not None:
                tsv_text = result
                break

        if tsv_text is None:
            skip_count += 1
            skip_logins.append(login)
            continue

        try:
            rows = parse_tsv(tsv_text, login)
            delete_and_insert(client, shadow, login, date_from, rows)
        except Exception:
            logger.exception("  %s write failed", login)
            err_count += 1
            if err_count >= FAILURE_THRESHOLD:
                aborted_at = f"[{idx}/{len(logins)}] {login}"
                logger.error(
                    "fetch_direct_stats: %d write failures >= FAILURE_THRESHOLD=%d, "
                    "aborting at %s instead of grinding through the remaining %d logins",
                    err_count, FAILURE_THRESHOLD, aborted_at, len(logins) - idx,
                )
                break
            continue
        ok_count += 1
        total_rows += len(rows)

    unexpected_skips = [login for login in skip_logins if login not in KNOWN_NO_TOKEN]
    details = f"ok={ok_count} skip={skip_count} err={err_count} rows_fetched={total_rows}"
    if unexpected_skips:
        details += f", unexpected_skips={unexpected_skips[:20]}"
        logger.warning("direct_account_reviews: no API access for %s", unexpected_skips)

    if err_count:
        client.command(f"DROP TABLE IF EXISTS {shadow} SYNC", settings=SAFE_QUERY_SETTINGS)
        reason = f", aborted early at {aborted_at}" if aborted_at else ""
        raise RuntimeError(f"fetch_direct_stats: {err_count} accounts failed to write ({details}){reason}")

    try:
        _check_swap_safe(client, STATS_TABLE_FULL, shadow, ok_count)
    except RuntimeError:
        client.command(f"DROP TABLE IF EXISTS {shadow} SYNC", settings=SAFE_QUERY_SETTINGS)
        raise

    swap_shadow(client, STATS_TABLE_FULL, shadow)
    logger.info("yandex_direct_reports_reviews sync: %s", details)
    return {"rows": total_rows, "details": details}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(sync_reviews_stats())


if __name__ == "__main__":
    main()
