from decimal import Decimal

import pytest

from data_check.compare.contract import load_contract
from data_check.compare.sources import build_query, fetch, rows_to_map, SourceError

CONTRACT_PATH = "data_check/compare/contract.json"


def test_build_query_v5_has_period_and_attribution():
    c = load_contract(CONTRACT_PATH)
    sql, params = build_query(c, "v5", "месяц")
    assert 'public.fact_big_analytics' in sql
    assert 'f."атрибуция"' in sql
    assert "'пиксель'" in sql
    assert params == ["2026-02-01", "2026-07-31", "По дате заявки"]


def test_build_query_v6_joins_dim_source_for_istochnik():
    c = load_contract(CONTRACT_PATH)
    sql, _ = build_query(c, "v6", "источник")
    assert "LEFT JOIN ad_analytics.Dim_Source ds" in sql
    assert "ds.`источник`" in sql


def test_build_query_v6_has_no_join_for_specialist():
    c = load_contract(CONTRACT_PATH)
    sql, _ = build_query(c, "v6", "специалист")
    assert "Dim_Source" not in sql


def test_build_query_rejects_unknown_dimension():
    c = load_contract(CONTRACT_PATH)
    with pytest.raises(SourceError):
        build_query(c, "v5", "направление")


def test_rows_to_map_keeps_decimal():
    metrics = ["заявки", "продажи"]
    rows = [("Кудерко Семен", Decimal("5964.5"), Decimal("57"))]
    out = rows_to_map(rows, metrics)
    assert out["Кудерко Семен"]["заявки"] == Decimal("5964.5")
    assert isinstance(out["Кудерко Семен"]["продажи"], Decimal)


def test_rows_to_map_rejects_empty():
    with pytest.raises(SourceError):
        rows_to_map([], ["заявки"])


def test_rows_to_map_normalises_null_dimension():
    rows = [(None, Decimal("1"))]
    out = rows_to_map(rows, ["заявки"])
    assert "(пусто)" in out


def test_rows_to_map_treats_null_metric_as_zero():
    rows = [("Кудерко Семен", None)]
    out = rows_to_map(rows, ["заявки"])
    assert out["Кудерко Семен"]["заявки"] == Decimal("0")


class _FakePgCursor:
    """Имитация psycopg2-курсора: не открывает сеть, только фиксирует вызов."""

    def __init__(self, rows):
        self._rows = rows
        self.executed_sql = None
        self.executed_params = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params):
        self.executed_sql = sql
        self.executed_params = params

    def fetchall(self):
        return self._rows


class _FakePgConn:
    def __init__(self, rows):
        self.cur = _FakePgCursor(rows)
        self.closed = False

    def cursor(self):
        return self.cur

    def close(self):
        self.closed = True


class _FakeChResult:
    def __init__(self, rows):
        self.result_rows = rows


class _FakeChClient:
    """Имитация clickhouse-connect клиента: фиксирует SQL и именованные параметры."""

    def __init__(self, rows):
        self._rows = rows
        self.queried_sql = None
        self.queried_parameters = None

    def query(self, sql, parameters=None):
        self.queried_sql = sql
        self.queried_parameters = parameters
        return _FakeChResult(self._rows)


_FULL_ROW = ("Кудерко Семен", Decimal("100"), Decimal("50"), Decimal("40"),
             Decimal("30"), Decimal("20"), Decimal("10"), Decimal("5"), Decimal("2"))


def test_fetch_v5_uses_pg_connect_and_positional_params(monkeypatch):
    c = load_contract(CONTRACT_PATH)
    fake_conn = _FakePgConn([_FULL_ROW])
    monkeypatch.setattr("data_check.compare.sources.pg_connect", lambda: fake_conn)

    out = fetch(c, "v5", "специалист")

    assert out["Кудерко Семен"]["расход"] == Decimal("100")
    assert out["Кудерко Семен"]["продажи"] == Decimal("5")
    assert fake_conn.cur.executed_params == ["2026-02-01", "2026-07-31", "По дате заявки"]
    assert fake_conn.closed is True


def test_fetch_v6_uses_ch_connect_and_named_params(monkeypatch):
    c = load_contract(CONTRACT_PATH)
    fake_client = _FakeChClient([_FULL_ROW])
    monkeypatch.setattr("data_check.compare.sources.ch_connect", lambda: fake_client)

    out = fetch(c, "v6", "специалист")

    assert out["Кудерко Семен"]["расход"] == Decimal("100")
    assert fake_client.queried_parameters == {
        "p0": "2026-02-01", "p1": "2026-07-31", "p2": "По дате заявки",
    }


def test_fetch_v6_empty_result_raises(monkeypatch):
    c = load_contract(CONTRACT_PATH)
    fake_client = _FakeChClient([])
    monkeypatch.setattr("data_check.compare.sources.ch_connect", lambda: fake_client)

    with pytest.raises(SourceError):
        fetch(c, "v6", "специалист")
