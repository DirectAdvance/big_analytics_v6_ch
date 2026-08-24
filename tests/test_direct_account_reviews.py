"""Rework-cycle tests for step_cron_night/direct_account_reviews + step3's reviews insert.

REVIEWS_DEDUPE_FANOUT_FIX_2026-08-24 rework (director findings):
  - IMPORTANT #1: step3._insert_reviews_rows must fail loudly if the fan-out regresses,
    not just print a stale source-table count.
  - IMPORTANT #2: load_reviews.parse_records must warn when a duplicate `аккаунт` carries a
    DIFFERENT `сайт` (a case argMax cannot represent, not a data-entry error to silently
    resolve) and must NOT warn on a genuine identical duplicate.

DATA-LOSS INCIDENT 2026-08-24 (PID 32096, killed at account 109/272, -409 rows / -6 logins):
  - A. `Date` was inserted as `str`; clickhouse-connect's Date codec does `(x - epoch).days`
    and raises TypeError on every single row -> every write failed.
  - B. `max(Date)` over a login with zero rows returns ClickHouse's non-nullable-column
    default (1970-01-01), not NULL, so `date_from` arithmetic landed on 1969-12-25.
  - C. `delete_and_insert` deleted from the live table BEFORE inserting -> defect A's 100%
    write-failure rate destroyed data with no way back, for 108 accounts before the kill.
  See `fetch_direct_stats.py` module docstring (ATOMICITY) for the fix: shadow-table +
  `EXCHANGE TABLES` swap, all-or-nothing per run, plus a FAILURE_THRESHOLD early abort.
  Live-ClickHouse disposable-table proof (not part of this offline suite, run manually
  against a throwaway `ad_analytics.zz_ponytail_reviews_proof` table, dropped afterwards):
  forced the exact TypeError, showed the table byte-unchanged; ran a success path with real
  `datetime.date` rows; forced 5 failing logins with FAILURE_THRESHOLD=3 and showed the run
  stopped after login 3, never touching logins 4-5.
"""

import logging
from datetime import date, timedelta

import pytest

from step0_sync_local import step0
from step3_build_sources import step3
from step_cron_night.direct_account_reviews import backfill_from_postgres, fetch_direct_stats, load_reviews


class _FakeResult:
    def __init__(self, value):
        self.result_rows = [[value]]


class _FakeReviewsClient:
    """Stands in for the clickhouse-connect client through _insert_reviews_rows: `.command`
    records the SQL (no execution needed, the insert body is not under test here), `.query`
    returns canned counts for the two count() calls the fix issues."""

    def __init__(self, source_count: int, inserted_count: int):
        self.source_count = source_count
        self.inserted_count = inserted_count
        self.commands: list[str] = []

    def command(self, sql, settings=None):
        self.commands.append(sql)
        return None

    def query(self, sql, settings=None):
        if "yandex_direct_reports_reviews" in sql:
            return _FakeResult(self.source_count)
        return _FakeResult(self.inserted_count)


def test_insert_reviews_rows_matches_source_count() -> None:
    client = _FakeReviewsClient(source_count=6284, inserted_count=6284)
    rows = step3._insert_reviews_rows(client, "ad_analytics.big_analytics_sources_new")
    assert rows == 6284


def test_insert_reviews_rows_raises_on_fanout_mismatch() -> None:
    """A regressed argMax dedupe fans the LEFT JOIN out again: inserted > source."""
    client = _FakeReviewsClient(source_count=6284, inserted_count=6370)
    with pytest.raises(RuntimeError, match="fan-out"):
        step3._insert_reviews_rows(client, "ad_analytics.big_analytics_sources_new")


_HEADER = ["город", "салон", "аккаунт", "сайт", "агентский аккаунт"]


def _sheet_row(city: str, salon: str, account: str, site: str) -> list[str]:
    return [city, salon, account, site, ""]


def test_parse_records_warns_on_conflicting_site_not_on_identical_duplicate(caplog) -> None:
    raw_rows = [
        _HEADER,
        # porg-rw7i2sgf: genuine duplicate, identical сайт both rows -> no warning.
        _sheet_row("Волгоград", "Автоцентр на Жукова", "porg-rw7i2sgf", "avtoexpert-otziv.ru"),
        _sheet_row("Волгоград", "Автоцентр на Жукова", "porg-rw7i2sgf", "avtoexpert-otziv.ru"),
        # porg-j47mlyp5: сайт differs between rows -> must warn.
        _sheet_row("Челябинск", "АвтоСтайл", "porg-j47mlyp5", "car-review.site"),
        _sheet_row("Челябинск", "АвтоСтайл", "porg-j47mlyp5", "avto-world-obzor.site"),
    ]

    with caplog.at_level(logging.WARNING, logger="pipeline.direct_account_reviews.load_reviews"):
        records = load_reviews.parse_records(raw_rows)

    assert len(records) == 4  # every row still loaded — the fix only makes the case visible
    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("porg-j47mlyp5" in w for w in warnings), warnings
    assert not any("porg-rw7i2sgf" in w for w in warnings), warnings


class _FakeMaxDateClient:
    def __init__(self, max_date):
        self.max_date = max_date

    def query(self, sql, settings=None):
        # F4: an empty table gives ClickHouse's non-nullable-column default 1970-01-01 from
        # plain max(), never NULL — _check_reviews_freshness must use maxOrNull() so this
        # fake actually exercises the "no rows" branch instead of silently green-lighting a
        # regression back to max(). Same trap fetch_direct_stats.get_login_date_from hit in
        # the 2026-08-24 incident (defect B).
        assert "maxOrNull" in sql, f"must use maxOrNull, not max(): {sql}"
        return _FakeResult(self.max_date)


def test_check_reviews_freshness_stale_returns_warning_not_raise() -> None:
    """IMPORTANT #1 (director rework): a weekly side source going stale must warn, never
    raise — a raise here used to kill the entire daily pipeline."""
    stale_days_value = step0.REVIEWS_MAX_STALE_DAYS + 1
    stale_date = date.today() - timedelta(days=stale_days_value)
    stale_days, warning = step0._check_reviews_freshness(_FakeMaxDateClient(stale_date))
    assert stale_days == stale_days_value
    assert warning is not None and "stale" in warning


def test_check_reviews_freshness_passes_when_fresh_no_warning() -> None:
    fresh_date = date.today() - timedelta(days=1)
    stale_days, warning = step0._check_reviews_freshness(_FakeMaxDateClient(fresh_date))
    assert stale_days == 1
    assert warning is None


def test_check_reviews_freshness_raises_on_empty_table() -> None:
    with pytest.raises(RuntimeError, match="no rows"):
        step0._check_reviews_freshness(_FakeMaxDateClient(None))


# --- Defect A: Date must be datetime.date, never str -----------------------------------

def test_to_date_parses_valid_iso_string() -> None:
    assert fetch_direct_stats._to_date("2026-08-20", "login") == date(2026, 8, 20)


def test_to_date_raises_on_missing_value() -> None:
    """The pre-fix code silently passed a raw `None`/`str` through to `client.insert` — the
    fix must fail loudly on a missing Date instead of writing something clickhouse-connect
    will only reject later, mid-write."""
    with pytest.raises(ValueError, match="no Date"):
        fetch_direct_stats._to_date(None, "login")


def test_to_date_raises_on_garbage_value() -> None:
    with pytest.raises(ValueError, match="unparseable"):
        fetch_direct_stats._to_date("not-a-date", "login")


def test_parse_tsv_date_field_is_a_date_object_not_str() -> None:
    tsv = (
        "Date\tCampaignId\tCampaignName\tAdGroupId\tAdGroupName\tAdNetworkType\tDevice\t"
        "Impressions\tClicks\tCost\tRlAdjustmentId\n"
        "2026-08-20\t1\tcamp\t1\tgrp\tSEARCH\tDESKTOP\t10\t1\t9.99\t--\n"
    )
    rows = fetch_direct_stats.parse_tsv(tsv, "login-x")
    assert len(rows) == 1
    row_date = rows[0][1]
    assert isinstance(row_date, date) and not isinstance(row_date, str)
    assert row_date == date(2026, 8, 20)


# --- Defect B: no-prior-data must return an explicit floor, not epoch arithmetic -------

class _FakeMaxOrNullClient:
    def __init__(self, value):
        self.value = value

    def query(self, sql, parameters=None, settings=None):
        assert "maxOrNull" in sql, f"must use maxOrNull, not max(): {sql}"
        return _FakeResult(self.value)


def test_get_login_date_from_no_prior_rows_returns_full_date_from() -> None:
    """`maxOrNull` on a login with zero rows returns SQL NULL -> Python None: this must hit
    the explicit FULL_DATE_FROM branch, not the 1969-12-25-style epoch arithmetic the
    non-nullable `max(Date)` default caused in the incident."""
    client = _FakeMaxOrNullClient(None)
    assert fetch_direct_stats.get_login_date_from(client, "tbl", "new-login") == fetch_direct_stats.FULL_DATE_FROM


def test_get_login_date_from_subtracts_safety_days_when_prior_rows_exist() -> None:
    client = _FakeMaxOrNullClient(date(2026, 3, 10))
    expected = (date(2026, 3, 10) - timedelta(days=fetch_direct_stats.SAFETY_DAYS)).strftime("%Y-%m-%d")
    assert fetch_direct_stats.get_login_date_from(client, "tbl", "login") == expected


def test_get_login_date_from_floors_at_full_date_from() -> None:
    """max(Date) - SAFETY_DAYS landing before FULL_DATE_FROM must clamp to FULL_DATE_FROM,
    not silently produce a date before the project's data start."""
    client = _FakeMaxOrNullClient(date(2026, 1, 3))
    assert fetch_direct_stats.get_login_date_from(client, "tbl", "login") == fetch_direct_stats.FULL_DATE_FROM


# --- Defect C: shadow + swap atomicity, failure threshold ------------------------------

class _FakeCountCostResult:
    def __init__(self, rows: int, cost: float):
        self.result_rows = [[rows, cost]]


class _FakeSyncClient:
    """Enough of the clickhouse-connect surface for `sync_reviews_stats`'s shadow+swap flow.
    `.command`/`.insert` record calls instead of executing SQL; `.query` answers the read
    paths the flow issues: `table_engine` (system.tables -> MergeTree, so `swap_shadow` takes
    the EXCHANGE TABLES branch), `maxOrNull(Date)` (always empty -> FULL_DATE_FROM, keeps
    most of these tests focused on abort/swap control flow, not SAFETY_DAYS windowing —
    covered separately above), and `count(), sum(Cost)` for `_check_swap_safe`'s pre-swap
    volume guard — answered per-table via `live_rows`/`live_cost`/`shadow_rows`/`shadow_cost`
    (shadow defaults to equal live, i.e. "no volume drop", unless a test overrides it)."""

    def __init__(self, live_rows: int = 0, live_cost: float = 0.0, shadow_rows=None, shadow_cost=None):
        self.commands: list[str] = []
        self.inserted: list[tuple[str, list[tuple]]] = []
        self.live_rows = live_rows
        self.live_cost = live_cost
        self.shadow_rows = live_rows if shadow_rows is None else shadow_rows
        self.shadow_cost = live_cost if shadow_cost is None else shadow_cost

    def command(self, sql, parameters=None, settings=None):
        self.commands.append(sql)

    def query(self, sql, parameters=None, settings=None):
        if "system.tables" in sql:
            return _FakeResult("MergeTree")
        if "maxOrNull" in sql:
            return _FakeResult(None)
        if "count()" in sql and "sum(" in sql:
            if "_new" in sql:  # shadow table name is always STATS_TABLE_FULL + "_new"
                return _FakeCountCostResult(self.shadow_rows, self.shadow_cost)
            return _FakeCountCostResult(self.live_rows, self.live_cost)
        if sql.startswith("SELECT count() FROM"):  # backfill_from_postgres's plain pre-check
            return _FakeResult(self.live_rows)
        raise AssertionError(f"unexpected query in fake client: {sql}")

    def insert(self, table, rows, column_names=None):
        # Mirrors clickhouse-connect's real Date codec (temporal.py: `(x - epoch).days`) —
        # a str Date must raise TypeError here exactly like the 2026-08-24 incident, not be
        # silently accepted by the fake.
        for row in rows:
            if not isinstance(row[1], date):
                raise TypeError(f"unsupported operand type(s) for -: {type(row[1]).__name__!r} and 'datetime.date'")
        self.inserted.append((table, rows))


def _valid_tsv() -> str:
    return (
        "Date\tCampaignId\tCampaignName\tAdGroupId\tAdGroupName\tAdNetworkType\tDevice\t"
        "Impressions\tClicks\tCost\tRlAdjustmentId\n"
        "2026-08-20\t1\tcamp\t1\tgrp\tSEARCH\tDESKTOP\t10\t1\t9.99\t--\n"
    )


def test_sync_reviews_stats_success_swaps_shadow_into_live(monkeypatch) -> None:
    client = _FakeSyncClient()
    monkeypatch.setattr(fetch_direct_stats, "get_logins", lambda c: ["login-a"])
    monkeypatch.setattr(fetch_direct_stats, "_all_tokens", lambda: [("tok", "agency")])
    monkeypatch.setattr(fetch_direct_stats, "fetch_report", lambda *a, **kw: _valid_tsv())

    result = fetch_direct_stats.sync_reviews_stats(client)

    assert result == {"rows": 1, "details": "ok=1 skip=0 err=0 rows_fetched=1"}
    assert any("EXCHANGE TABLES" in c for c in client.commands), client.commands
    assert len(client.inserted) == 1
    assert isinstance(client.inserted[0][1][0][1], date)  # Date column is a real date object


def test_sync_reviews_stats_write_failure_aborts_without_swap(monkeypatch) -> None:
    """A regression that reintroduces defect A (str Date) — or any other write failure —
    must not swap the shadow into the live table."""

    def _bad_parse_tsv(tsv_text, login):
        return [(login, "2026-08-20", 1, "c", 1, "g", "SEARCH", "DESKTOP", 1, 1, 1.0, None)]

    client = _FakeSyncClient()
    monkeypatch.setattr(fetch_direct_stats, "get_logins", lambda c: ["login-a"])
    monkeypatch.setattr(fetch_direct_stats, "_all_tokens", lambda: [("tok", "agency")])
    monkeypatch.setattr(fetch_direct_stats, "fetch_report", lambda *a, **kw: _valid_tsv())
    monkeypatch.setattr(fetch_direct_stats, "parse_tsv", _bad_parse_tsv)

    with pytest.raises(RuntimeError, match="failed to write"):
        fetch_direct_stats.sync_reviews_stats(client)

    assert not any("EXCHANGE TABLES" in c for c in client.commands), client.commands
    assert any(f"DROP TABLE IF EXISTS {fetch_direct_stats.STATS_TABLE_FULL}_new" in c for c in client.commands)
    # F7: the core invariant of defect C — every ALTER ... DELETE this run issues must target
    # the shadow copy, never the live table, even on the failure path.
    delete_commands = [c for c in client.commands if "ALTER TABLE" in c and "DELETE" in c]
    assert delete_commands, client.commands
    assert all(f"ALTER TABLE {fetch_direct_stats.STATS_TABLE_FULL}_new " in c for c in delete_commands)
    assert not any(c.startswith(f"ALTER TABLE {fetch_direct_stats.STATS_TABLE_FULL} ") for c in delete_commands)


def test_sync_reviews_stats_failure_threshold_stops_early(monkeypatch) -> None:
    attempted: list[str] = []

    def _always_fail(tsv_text, login):
        attempted.append(login)
        raise ValueError(f"forced failure for {login}")

    client = _FakeSyncClient()
    logins = [f"login-{i}" for i in range(5)]
    monkeypatch.setattr(fetch_direct_stats, "get_logins", lambda c: logins)
    monkeypatch.setattr(fetch_direct_stats, "_all_tokens", lambda: [("tok", "agency")])
    monkeypatch.setattr(fetch_direct_stats, "fetch_report", lambda *a, **kw: _valid_tsv())
    monkeypatch.setattr(fetch_direct_stats, "parse_tsv", _always_fail)

    with pytest.raises(RuntimeError, match="aborted early"):
        fetch_direct_stats.sync_reviews_stats(client)

    assert len(attempted) == fetch_direct_stats.FAILURE_THRESHOLD == 3
    assert attempted == logins[: fetch_direct_stats.FAILURE_THRESHOLD]


# --- IMPORTANT #2: pre-swap volume guard ------------------------------------------------

def _empty_tsv() -> str:
    return (
        "Date\tCampaignId\tCampaignName\tAdGroupId\tAdGroupName\tAdNetworkType\tDevice\t"
        "Impressions\tClicks\tCost\tRlAdjustmentId\n"
    )


def test_sync_reviews_stats_zero_ok_refuses_swap(monkeypatch) -> None:
    """Every login skips (no API access) -> ok_count=0, err_count=0 — nothing technically
    failed, but the shadow carries no fresh data and the swap must still be refused."""
    client = _FakeSyncClient()
    monkeypatch.setattr(fetch_direct_stats, "get_logins", lambda c: ["login-a"])
    monkeypatch.setattr(fetch_direct_stats, "_all_tokens", lambda: [("tok", "agency")])
    monkeypatch.setattr(fetch_direct_stats, "fetch_report", lambda *a, **kw: None)

    with pytest.raises(RuntimeError, match="ok_count=0"):
        fetch_direct_stats.sync_reviews_stats(client)

    assert not any("EXCHANGE TABLES" in c for c in client.commands), client.commands
    assert any(f"DROP TABLE IF EXISTS {fetch_direct_stats.STATS_TABLE_FULL}_new" in c for c in client.commands)


def test_sync_reviews_stats_empty_payload_refuses_swap(monkeypatch) -> None:
    """A Reports API 200 with only a header row parses to zero rows; delete_and_insert then
    clears the SAFETY_DAYS window and writes nothing, and the run still counts it `ok`
    (IMPORTANT #2). The pre-swap volume guard must refuse when the shadow ends up far below
    the live table — the only way to tell this apart from a genuinely inactive account."""
    client = _FakeSyncClient(live_rows=100, live_cost=500.0, shadow_rows=0, shadow_cost=0.0)
    monkeypatch.setattr(fetch_direct_stats, "get_logins", lambda c: ["login-a"])
    monkeypatch.setattr(fetch_direct_stats, "_all_tokens", lambda: [("tok", "agency")])
    monkeypatch.setattr(fetch_direct_stats, "fetch_report", lambda *a, **kw: _empty_tsv())

    with pytest.raises(RuntimeError, match="refusing swap"):
        fetch_direct_stats.sync_reviews_stats(client)

    assert not any("EXCHANGE TABLES" in c for c in client.commands), client.commands
    assert any(f"DROP TABLE IF EXISTS {fetch_direct_stats.STATS_TABLE_FULL}_new" in c for c in client.commands)


def test_sync_reviews_stats_small_drop_within_tolerance_swaps(monkeypatch) -> None:
    """A shadow just inside SWAP_MIN_RETENTION_RATIO of the live table (legitimate API
    re-statement inside the SAFETY_DAYS window) must still swap — the guard is a floor, not
    an exact-match requirement."""
    live_rows = 100
    shadow_rows = int(live_rows * fetch_direct_stats.SWAP_MIN_RETENTION_RATIO)  # exactly at floor
    client = _FakeSyncClient(live_rows=live_rows, live_cost=1000.0, shadow_rows=shadow_rows, shadow_cost=1000.0)
    monkeypatch.setattr(fetch_direct_stats, "get_logins", lambda c: ["login-a"])
    monkeypatch.setattr(fetch_direct_stats, "_all_tokens", lambda: [("tok", "agency")])
    monkeypatch.setattr(fetch_direct_stats, "fetch_report", lambda *a, **kw: _valid_tsv())

    result = fetch_direct_stats.sync_reviews_stats(client)

    assert result["rows"] == 1
    assert any("EXCHANGE TABLES" in c for c in client.commands), client.commands


# --- F10: backfill_from_postgres.py must not swap an empty/partial PG fetch into live -----

def test_backfill_force_refuses_on_empty_fetch(monkeypatch) -> None:
    """--force means "reload from PG", not "zero out the table" (director rework 2026-08-24,
    F10): an empty fetch (wrong search_path, dead connection) must not swap."""
    client = _FakeSyncClient(live_rows=100, live_cost=500.0)
    monkeypatch.setattr(backfill_from_postgres, "get_client", lambda: client)
    monkeypatch.setattr(backfill_from_postgres, "_fetch_stats_rows", lambda: [])

    with pytest.raises(RuntimeError, match="ok_count=0"):
        backfill_from_postgres.backfill(force=True)

    assert not any("EXCHANGE TABLES" in c for c in client.commands), client.commands
    assert any(
        f"DROP TABLE IF EXISTS {backfill_from_postgres.STATS_TABLE_FULL}_new" in c for c in client.commands
    )
    assert client.inserted == []


def test_backfill_force_refuses_on_truncated_fetch(monkeypatch) -> None:
    """A non-empty but far-short fetch (partial snapshot) must also refuse — ok_count>0
    alone is not proof of a real reload, hence reusing the ratio floor too."""
    # shadow_rows/cost pinned to what the single fetched row actually amounts to — the fake
    # answers `_check_swap_safe`'s shadow-table query from these, not from `.insert()` calls.
    client = _FakeSyncClient(live_rows=6284, live_cost=1344281.23, shadow_rows=1, shadow_cost=1.0)
    monkeypatch.setattr(backfill_from_postgres, "get_client", lambda: client)
    short_row = ("login-a", date(2026, 8, 20), 1, "c", 1, "g", "SEARCH", "DESKTOP", 1, 1, 1.0, None)
    monkeypatch.setattr(backfill_from_postgres, "_fetch_stats_rows", lambda: [short_row])

    with pytest.raises(RuntimeError, match="refusing swap"):
        backfill_from_postgres.backfill(force=True)

    assert not any("EXCHANGE TABLES" in c for c in client.commands), client.commands
    assert any(
        f"DROP TABLE IF EXISTS {backfill_from_postgres.STATS_TABLE_FULL}_new" in c for c in client.commands
    )


def test_backfill_force_swaps_on_real_fetch(monkeypatch) -> None:
    """Positive control: a fetch at/above the live volume swaps normally."""
    client = _FakeSyncClient(live_rows=1, live_cost=9.99, shadow_rows=1, shadow_cost=9.99)
    monkeypatch.setattr(backfill_from_postgres, "get_client", lambda: client)
    row = ("login-a", date(2026, 8, 20), 1, "c", 1, "g", "SEARCH", "DESKTOP", 1, 1, 9.99, None)
    monkeypatch.setattr(backfill_from_postgres, "_fetch_stats_rows", lambda: [row])

    result = backfill_from_postgres.backfill(force=True)

    assert result == {"stats": 1, "details": "stats=1"}
    assert any("EXCHANGE TABLES" in c for c in client.commands), client.commands
    assert client.inserted and client.inserted[0][1] == [row]
