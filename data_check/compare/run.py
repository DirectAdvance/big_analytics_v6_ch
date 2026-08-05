"""Гейт перехода v5 -> v6.

Запуск:
    .venv/bin/python3 data_check/compare/run.py
    .venv/bin/python3 data_check/compare/run.py --json

Exit codes: 0=PASS, 1=есть блокеры, 2=ошибка исполнения.

Обе стороны читаются только на SELECT: ни одной записи в PostgreSQL v5
(живой прод-контур Power BI) и в ClickHouse v6 гейт не делает.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from data_check.compare import differ, sources  # noqa: E402
from data_check.compare.contract import ContractError, load_contract, validate_columns  # noqa: E402
from data_check.compare.report import format_report  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_TOTALS_DIMENSION = "месяц"
_EXTRA_DIMENSIONS = ["специалист", "источник", "Название crm", "_source_table"]


class _Collector:
    """Пара срезов v5/v6 по измерению с кэшом: один запрос на измерение на сторону."""

    def __init__(self, contract: dict):
        self._contract = contract
        self._cache = {}

    def get(self, dimension: str):
        if dimension not in self._cache:
            logger.info("срез по измерению «%s»", dimension)
            self._cache[dimension] = (
                sources.fetch(self._contract, "v5", dimension),
                sources.fetch(self._contract, "v6", dimension),
            )
        return self._cache[dimension]


def _validate_schemas(contract: dict) -> None:
    """Схемы сверяются ДО любого запроса за данными — иначе гейт зеленеет от слепоты."""
    conn = sources.pg_connect()
    try:
        validate_columns(contract, "v5",
                         sources.pg_columns(conn, list(contract["columns_required"]["v5"])))
    finally:
        conn.close()
    validate_columns(contract, "v6",
                     sources.ch_columns(sources.ch_connect(),
                                        list(contract["columns_required"]["v6"])))


def _totals(collector: _Collector, metrics):
    left, right = collector.get(_TOTALS_DIMENSION)
    totals_left = {m: sum((v[m] for v in left.values())) for m in metrics}
    totals_right = {m: sum((v[m] for v in right.values())) for m in metrics}
    return differ.compare_totals(totals_left, totals_right, metrics)


def _dimension_slice(collector: _Collector, accepted, metric: str, dimension: str):
    """Один срез метрики по измерению: строки, очаг, погашенные и устаревшие записи."""
    left, right = collector.get(dimension)
    rows = differ.compare_by_dimension(left, right, metric)
    rows, stale = differ.apply_accepted(rows, accepted, metric, dimension)

    hits = [dict(row, метрика=metric, значение=key)
            for key, row in rows.items() if row["verdict"] == differ.ACCEPTED]
    # В concentration идут ТОЛЬКО открытые MISMATCH: у неё нет порога материальности,
    # и дробный шум (<1) слепил бы из ничего «локализованный блокер».
    open_deltas = {key: row["delta"] for key, row in rows.items()
                   if row["verdict"] == differ.MISMATCH}
    payload = {"hotspot": differ.concentration(open_deltas), "rows": rows}
    return payload, hits, stale, bool(open_deltas)


def _drilldown(collector: _Collector, accepted, totals):
    """Спуск по каждой метрике.

    Тотал — сумма по значениям измерения, поэтому +500 в одном салоне и -500 в другом
    схлопываются в ноль: вердикт тотала MATCH при реальном расхождении. Поэтому спуск
    по «месяцу» идёт ВСЕГДА, а выживший per-key MISMATCH поднимает вердикт тотала до
    MISMATCH — иначе гейт напечатает PASS поверх расхождения.
    """
    drilldown, accepted_hits, stale_all = {}, [], []
    for metric, total_row in totals.items():
        payload, hits, stale, has_open = _dimension_slice(
            collector, accepted, metric, _TOTALS_DIMENSION)
        per_dim = {_TOTALS_DIMENSION: payload}
        accepted_hits.extend(hits)
        stale_all.extend(stale)

        if total_row["verdict"] == differ.MISMATCH or has_open:
            for dimension in _EXTRA_DIMENSIONS:
                payload, hits, stale, extra_open = _dimension_slice(
                    collector, accepted, metric, dimension)
                per_dim[dimension] = payload
                accepted_hits.extend(hits)
                stale_all.extend(stale)
                has_open = has_open or extra_open

        if has_open and total_row["verdict"] != differ.MISMATCH:
            logger.info("метрика «%s»: тотал сошёлся, но per-key расхождения остались "
                        "-> вердикт поднят до MISMATCH", metric)
            total_row["verdict"] = differ.MISMATCH
        drilldown[metric] = per_dim
    return drilldown, accepted_hits, stale_all


def _build_result(contract: dict, accepted) -> dict:
    collector = _Collector(contract)
    metrics = list(contract["metrics"].keys())
    totals = _totals(collector, metrics)
    drilldown, accepted_hits, stale_all = _drilldown(collector, accepted, totals)
    return {
        "period": "%s..%s" % (contract["period"]["from"], contract["period"]["to"]),
        "totals": totals,
        "drilldown": drilldown,
        "accepted": accepted_hits,
        "stale": stale_all,
    }


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Гейт перехода v5 -> v6")
    here = os.path.dirname(os.path.abspath(__file__))
    parser.add_argument("--contract", default=os.path.join(here, "contract.json"),
                        help="путь к контракту соответствия")
    parser.add_argument("--accepted", default=os.path.join(here, "accepted.json"),
                        help="путь к реестру осознанных отличий")
    parser.add_argument("--json", action="store_true", help="машинный вывод")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    try:
        contract = load_contract(args.contract)
        accepted = differ.load_accepted(args.accepted)
        _validate_schemas(contract)
        result = _build_result(contract, accepted)
    except (ContractError, sources.SourceError) as exc:
        logger.error("гейт не выполнен: %s", exc)
        return 2
    except Exception as exc:  # коннект, сеть, битый ответ БД — это тоже ошибка исполнения
        logger.error("гейт не выполнен: %s: %s", type(exc).__name__, exc)
        return 2

    if args.json:
        print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    else:
        print(format_report(result))

    blockers = [n for n, r in result["totals"].items() if r["verdict"] == differ.MISMATCH]
    return 1 if blockers else 0


if __name__ == "__main__":
    sys.exit(main())
