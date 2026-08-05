"""Контракт соответствия v5 <-> v6.

Контракт — ДАННЫЕ (contract.json), этот модуль только читает и проверяет.
Любое несоответствие живой схеме — исключение, а не тихий пропуск:
гейт не имеет права позеленеть от того, что перестал видеть колонку.
"""
from __future__ import annotations

import json
from typing import Dict, Set

_REQUIRED_KEYS = ("version", "period", "attribution", "sides", "metrics",
                  "dimensions", "columns_required")


class ContractError(Exception):
    """Контракт битый или разошёлся с живой схемой."""


def load_contract(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            contract = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ContractError("не читается контракт %s: %s" % (path, exc))

    missing = [k for k in _REQUIRED_KEYS if k not in contract]
    if missing:
        raise ContractError("в контракте нет обязательных ключей: %s" % ", ".join(missing))
    for side in ("v5", "v6"):
        if side not in contract["sides"]:
            raise ContractError("в контракте нет описания стороны %s" % side)
    return contract


def validate_columns(contract: dict, side: str, existing: Dict[str, Set[str]]) -> None:
    """existing: {'схема.таблица': {'колонка', ...}} — снимок живой схемы."""
    required = contract["columns_required"].get(side, {})
    problems = []
    for table, columns in required.items():
        if table not in existing:
            problems.append("%s: таблицы нет" % table)
            continue
        absent = [c for c in columns if c not in existing[table]]
        if absent:
            problems.append("%s: нет колонок %s" % (table, ", ".join(absent)))
    if problems:
        raise ContractError("контракт разошёлся со схемой %s -> %s" % (side, "; ".join(problems)))
