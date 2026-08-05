import json
import pytest
from data_check.compare.contract import load_contract, validate_columns, ContractError


def _write(tmp_path, obj):
    p = tmp_path / "contract.json"
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return str(p)


MINIMAL = {
    "version": 1,
    "period": {"from": "2026-02-01", "to": "2026-07-31"},
    "attribution": "По дате заявки",
    "sides": {
        "v5": {"kind": "postgres", "table": "public.fact_big_analytics", "alias": "f",
               "date_expr": 'f."Date"', "attribution_expr": 'f."атрибуция"',
               "exclude_expr": "f.\"_source_table\" <> 'пиксель'", "joins": []},
        "v6": {"kind": "clickhouse", "table": "ad_analytics.fact_big_analytics", "alias": "f",
               "date_expr": "f.Date", "attribution_expr": "f.`атрибуция`",
               "exclude_expr": "f.`_source_table` <> 'pixel'", "joins": []},
    },
    "metrics": {"продажи": {"v5": "f.prodazhi", "v6": "f.prodazhi"}},
    "dimensions": {"специалист": {"v5": 'f."специалист"', "v6": "f.`специалист`"}},
    "columns_required": {
        "v5": {"public.fact_big_analytics": ["prodazhi", "специалист"]},
        "v6": {"ad_analytics.fact_big_analytics": ["prodazhi", "специалист"]},
    },
}


def test_load_contract_ok(tmp_path):
    c = load_contract(_write(tmp_path, MINIMAL))
    assert c["metrics"]["продажи"]["v6"] == "f.prodazhi"


def test_load_contract_rejects_broken_json(tmp_path):
    p = tmp_path / "contract.json"
    p.write_text("{не json", encoding="utf-8")
    with pytest.raises(ContractError):
        load_contract(str(p))


def test_load_contract_requires_metrics(tmp_path):
    bad = {k: v for k, v in MINIMAL.items() if k != "metrics"}
    with pytest.raises(ContractError) as e:
        load_contract(_write(tmp_path, bad))
    assert "metrics" in str(e.value)


def test_validate_columns_reports_all_missing(tmp_path):
    c = load_contract(_write(tmp_path, MINIMAL))
    existing = {"public.fact_big_analytics": {"prodazhi"}}
    with pytest.raises(ContractError) as e:
        validate_columns(c, "v5", existing)
    assert "специалист" in str(e.value)


def test_validate_columns_passes_when_all_present(tmp_path):
    c = load_contract(_write(tmp_path, MINIMAL))
    existing = {"public.fact_big_analytics": {"prodazhi", "специалист"}}
    validate_columns(c, "v5", existing)


def test_validate_columns_rejects_missing_table(tmp_path):
    c = load_contract(_write(tmp_path, MINIMAL))
    with pytest.raises(ContractError) as e:
        validate_columns(c, "v5", {})
    assert "public.fact_big_analytics" in str(e.value)
