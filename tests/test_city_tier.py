from datetime import date
from types import SimpleNamespace

from google.oauth2 import service_account
from googleapiclient import discovery
from googleapiclient.errors import HttpError
from star_refactor import build_pbi_compat
from step0_sync_local import load_city_tier

MONTH = date(2026, 8, 1)

RAW_ROWS = [
    ["Гео", "Tier"],
    ["Сургут", "tier3"],
    ["Казань", "tier2"],
    ["Казань (по августу, в среднем это тир 2)", "tier1"],
    ["Тольятти", "тир 2"],
    ["Тольятти", "Tier 2"],
    ["Пермь", "???"],
]


def test_city_tier_parser_matches_ba5_rules():
    rows, skipped = load_city_tier.parse_rows(RAW_ROWS, MONTH)
    resolved_rows, resolved = load_city_tier.resolve_conflicts(rows)

    assert skipped == [("Пермь", "???")]
    assert len(resolved_rows) == 3
    assert resolved["Казань"]["by"] == "note"
    assert [row for row in resolved_rows if row[1] == "Казань"][0][2] == "tier1"


def test_city_tier_backfill_excludes_current_and_existing_months():
    assert load_city_tier.months_to_seed(
        date(2026, 1, 1),
        date(2026, 4, 1),
        {date(2026, 2, 1)},
    ) == [date(2026, 1, 1), date(2026, 3, 1)]


def test_read_sheet_retries_transient_google_error(monkeypatch):
    calls = {"execute": 0}
    sleeps = []

    class FakeRequest:
        def execute(self):
            calls["execute"] += 1
            if calls["execute"] == 1:
                raise HttpError(SimpleNamespace(status=503, reason="Unavailable"), b"")
            return {"values": [["Гео", "Tier"], ["Сургут", "tier3"]]}

    class FakeValues:
        def get(self, **_kwargs):
            return FakeRequest()

    class FakeSpreadsheets:
        def values(self):
            return FakeValues()

    class FakeService:
        def spreadsheets(self):
            return FakeSpreadsheets()

    monkeypatch.setattr(load_city_tier, "_find_service_account", lambda: "service.json")
    monkeypatch.setattr(
        service_account.Credentials,
        "from_service_account_file",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(discovery, "build", lambda *_args, **_kwargs: FakeService())
    monkeypatch.setattr(load_city_tier.time, "sleep", lambda seconds: sleeps.append(seconds))

    assert load_city_tier.read_sheet() == [["Гео", "Tier"], ["Сургут", "tier3"]]
    assert calls == {"execute": 2}
    assert sleeps == [5]


def test_pbi_full_exposes_city_tier_key_and_dim_builder_registered():
    sql = build_pbi_compat._pbi_full_sql()

    assert "AS city_tier_key" in sql
    assert "Dim_City_Tier" in build_pbi_compat.PBI_SOURCE_OBJECTS
    assert build_pbi_compat.PBI_VIEW_SQL_BUILDERS["Dim_City_Tier"] is build_pbi_compat._dim_city_tier_pbi_sql
    dim_sql = build_pbi_compat.build_dim_city_tier.__code__.co_consts
    assert any("nullIf(mt.tier, '')" in str(part) for part in dim_sql)
