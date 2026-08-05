import json
from decimal import Decimal

import pytest

from data_check.compare.contract import load_contract
from data_check.compare.sources import build_query, rows_to_map, SourceError

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
