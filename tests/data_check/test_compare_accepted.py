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

# Минимальный контракт: реестру нужны только словари имён метрик и измерений.
CONTRACT = {
    "metrics": {"продажи": {}, "квал": {}},
    "dimensions": {"Название crm": {}, "специалист": {}, "источник": {}},
}


def _write(tmp_path, obj):
    p = tmp_path / "accepted.json"
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return str(p)


def test_load_accepted_ok(tmp_path):
    assert load_accepted(_write(tmp_path, [ENTRY]), CONTRACT)[0]["значение"] == "Маркар"


def test_load_accepted_rejects_broken(tmp_path):
    p = tmp_path / "accepted.json"
    p.write_text("[{", encoding="utf-8")
    with pytest.raises(ContractError):
        load_accepted(str(p), CONTRACT)


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


def test_load_accepted_rejects_typo_key(tmp_path):
    broken = dict(ENTRY)
    del broken["метрика"]
    broken["метрике"] = "продажи"  # опечатка вместо "метрика"
    with pytest.raises(ContractError) as exc:
        load_accepted(_write(tmp_path, [broken]), CONTRACT)
    assert "метрика" in str(exc.value)


def test_load_accepted_rejects_missing_reshenie(tmp_path):
    broken = dict(ENTRY)
    del broken["решение"]
    with pytest.raises(ContractError) as exc:
        load_accepted(_write(tmp_path, [broken]), CONTRACT)
    assert "решение" in str(exc.value)


def test_load_accepted_rejects_duplicate_triple(tmp_path):
    with pytest.raises(ContractError):
        load_accepted(_write(tmp_path, [ENTRY, dict(ENTRY)]), CONTRACT)


def test_load_accepted_well_formed_registry_still_works(tmp_path):
    second = dict(ENTRY, значение="Генезис", решение="другое решение")
    path = _write(tmp_path, [ENTRY, second])
    accepted = load_accepted(path, CONTRACT)
    assert len(accepted) == 2

    diffs = {
        "Маркар": {"delta": Decimal("-548"), "verdict": MISMATCH},
        "Генезис": {"delta": Decimal("-3"), "verdict": MISMATCH},
    }
    out, stale = apply_accepted(diffs, accepted, "продажи", "Название crm")
    assert out["Маркар"]["verdict"] == ACCEPTED
    assert out["Генезис"]["verdict"] == ACCEPTED
    assert stale == []


def test_load_accepted_rejects_unknown_metric(tmp_path):
    """Опечатка в имени метрики — запись, которая не сработает никогда и никогда не
    будет названа устаревшей. Молчаливый класс, запрещённый §7 спеки."""
    broken = dict(ENTRY, метрика="Продажи")  # регистр не тот, в контракте «продажи»
    with pytest.raises(ContractError) as exc:
        load_accepted(_write(tmp_path, [broken]), CONTRACT)
    assert "Продажи" in str(exc.value) and "контракт" in str(exc.value)


def test_load_accepted_rejects_unknown_dimension(tmp_path):
    broken = dict(ENTRY, измерение="Название CRM")  # в контракте «Название crm»
    with pytest.raises(ContractError) as exc:
        load_accepted(_write(tmp_path, [broken]), CONTRACT)
    assert "Название CRM" in str(exc.value) and "контракт" in str(exc.value)


def test_load_accepted_error_names_known_values(tmp_path):
    """Сообщение обязано подсказать, что контракт вообще знает — иначе опечатку
    придётся искать глазами по contract.json."""
    broken = dict(ENTRY, метрика="приедет")
    with pytest.raises(ContractError) as exc:
        load_accepted(_write(tmp_path, [broken]), CONTRACT)
    assert "квал" in str(exc.value) and "продажи" in str(exc.value)
