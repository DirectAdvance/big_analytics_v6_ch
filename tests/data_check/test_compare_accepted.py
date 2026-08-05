import json
from decimal import Decimal

import pytest

from data_check.compare.differ import (
    ACCEPTED, MISMATCH, load_accepted, apply_accepted,
)
from data_check.compare.contract import ContractError

ENTRY = {
    "метрика": "продажи",
    "измерение": "Название crm",
    "значение": "Маркар",
    "решение": "v6 верен — sale = только COMPLETED",
    "кто": "Семён",
    "дата": "2026-08-05",
}


def _write(tmp_path, obj):
    p = tmp_path / "accepted.json"
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return str(p)


def test_load_accepted_ok(tmp_path):
    assert load_accepted(_write(tmp_path, [ENTRY]))[0]["значение"] == "Маркар"


def test_load_accepted_rejects_broken(tmp_path):
    p = tmp_path / "accepted.json"
    p.write_text("[{", encoding="utf-8")
    with pytest.raises(ContractError):
        load_accepted(str(p))


def test_apply_accepted_suppresses_matching():
    diffs = {"Маркар": {"delta": Decimal("-548"), "verdict": MISMATCH}}
    out, stale = apply_accepted(diffs, [ENTRY], "продажи", "Название crm")
    assert out["Маркар"]["verdict"] == ACCEPTED
    assert out["Маркар"]["original_verdict"] == MISMATCH
    assert stale == []


def test_apply_accepted_ignores_other_metric():
    diffs = {"Маркар": {"delta": Decimal("-548"), "verdict": MISMATCH}}
    out, _ = apply_accepted(diffs, [ENTRY], "квал", "Название crm")
    assert out["Маркар"]["verdict"] == MISMATCH


def test_apply_accepted_ignores_other_dimension():
    diffs = {"Маркар": {"delta": Decimal("-548"), "verdict": MISMATCH}}
    out, _ = apply_accepted(diffs, [ENTRY], "продажи", "специалист")
    assert out["Маркар"]["verdict"] == MISMATCH


def test_apply_accepted_reports_stale_entry():
    diffs = {"Фаиг": {"delta": Decimal("-1"), "verdict": MISMATCH}}
    _, stale = apply_accepted(diffs, [ENTRY], "продажи", "Название crm")
    assert stale and stale[0]["значение"] == "Маркар"
