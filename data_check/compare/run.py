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


def _restrict_to_comparable(contract: dict, dimension: str, left: dict, right: dict):
    """Оставляет в обоих срезах только значения из `values_comparable` контракта.

    У контуров разные справочники: после фикса KNOWN_ISSUES.md #34 (2026-08-06) v6 тоже кладёт
    часть звонков посевных доменов под «Посевы_Звонки» (crop-ветка `crop=True` в
    `step6_build_full/step6.py::_calls_select`), но остаточный разрыв всё равно есть — v5 красит
    в «Посевы_Звонки» ШИРЕ (динамический EXISTS-гейт по посевной активности домена «когда-либо»,
    v5 `step6.py:521-575` UPDATE 3c, НЕ портирован), поэтому часть строк по-прежнему остаётся под
    «Звонки» в v6, а под «Посевы_Звонки» в v5. Без ограничения спуск покажет очаг в РАЗНИЦЕ
    НАЗВАНИЙ и уведёт читателя от настоящего дефекта данных. Отброшенное не исчезает молча: значения
    считаются, называются в INFO-логе и уезжают в результат — читатель обязан видеть,
    что срез сужен и насколько.

    Измерение без `values_comparable` сравнивается целиком и не трогается.
    """
    allowed = contract["dimensions"][dimension].get("values_comparable")
    if not allowed:
        return left, right, None

    allowed = set(allowed)
    kept_left = {k: v for k, v in left.items() if k in allowed}
    kept_right = {k: v for k, v in right.items() if k in allowed}
    excluded_left = sorted(set(left) - allowed)
    excluded_right = sorted(set(right) - allowed)
    if not kept_left or not kept_right:
        raise sources.SourceError(
            "измерение «%s»: после ограничения по values_comparable не осталось значений "
            "(v5 %d, v6 %d) — сравнивать нечего"
            % (dimension, len(kept_left), len(kept_right)))

    info = {
        "dimension": dimension,
        "kept": sorted(set(kept_left) | set(kept_right)),
        "excluded_v5": excluded_left,
        "excluded_v6": excluded_right,
    }
    logger.info("измерение «%s» ограничено сопоставимыми значениями: оставлено %d (%s); "
                "исключено v5 %d (%s); исключено v6 %d (%s)",
                dimension, len(info["kept"]), ", ".join(info["kept"]),
                len(excluded_left), ", ".join(excluded_left) or "—",
                len(excluded_right), ", ".join(excluded_right) or "—")
    return kept_left, kept_right, info


class _Collector:
    """Пара срезов v5/v6 по измерению с кэшом: один запрос на измерение на сторону."""

    def __init__(self, contract: dict):
        self._contract = contract
        self._cache = {}
        self.restrictions = {}

    def get(self, dimension: str):
        if dimension not in self._cache:
            logger.info("срез по измерению «%s»", dimension)
            left, right, info = _restrict_to_comparable(
                self._contract, dimension,
                sources.fetch(self._contract, "v5", dimension),
                sources.fetch(self._contract, "v6", dimension))
            if info:
                self.restrictions[dimension] = info
            self._cache[dimension] = (left, right)
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
    """Один срез метрики по измерению: строки, очаг, погашенные записи и факт открытых дельт.

    Устаревшие записи реестра здесь НЕ собираются: их считает _drilldown одной
    глобальной развёрткой в конце прогона (см. там же).
    """
    left, right = collector.get(dimension)
    rows = differ.compare_by_dimension(left, right, metric)
    rows, _stale_here = differ.apply_accepted(rows, accepted, metric, dimension)

    # Измерение едет в hits вместе с метрикой и значением: по этой тройке _drilldown
    # потом отличает сработавшую запись реестра от устаревшей.
    hits = [dict(row, метрика=metric, измерение=dimension, значение=key)
            for key, row in rows.items() if row["verdict"] == differ.ACCEPTED]
    # В concentration идут ТОЛЬКО открытые MISMATCH: у неё нет порога материальности,
    # и дробный шум (<1) слепил бы из ничего «локализованный блокер».
    open_deltas = {key: row["delta"] for key, row in rows.items()
                   if row["verdict"] == differ.MISMATCH}
    payload = {"hotspot": differ.concentration(open_deltas), "rows": rows}
    return payload, hits, bool(open_deltas)


def _drilldown(collector: _Collector, accepted, totals):
    """Спуск по каждой метрике — ПО ВСЕМ измерениям, безусловно.

    Тотал — сумма по значениям измерения, поэтому +500 в одном салоне и -500 в другом
    схлопываются в ноль: вердикт тотала MATCH при реальном расхождении. Выживший
    per-key MISMATCH поднимает вердикт тотала до MISMATCH — иначе гейт напечатает PASS
    поверх расхождения.

    Спуск в остальные измерения тоже безусловный, и это НЕ перестраховка. Расхождение
    может быть нейтральным к месяцу: помесячные итоги совпадают байт в байт, а строки
    привязаны к другому специалисту / источнику / CRM / _source_table. Ровно этот класс
    и есть подпись миграции — §11.3 спеки описывает 143 908 строк с неверным
    campaign_code из-за схлопывания Dim по CampaignId = 0, §11.4 — целые классы
    _source_table, которых в v6 нет. Условный спуск такое расхождение не запросил бы
    ни разу и вернул бы exit 0. Цена безусловности близка к нулю: коллектор кэширует
    срез по измерению на все восемь метрик — это 4 лишних запроса на сторону за прогон.
    """
    drilldown, accepted_hits = {}, []
    for metric, total_row in totals.items():
        per_dim = {}
        has_open = False
        for dimension in [_TOTALS_DIMENSION] + _EXTRA_DIMENSIONS:
            payload, hits, dim_open = _dimension_slice(
                collector, accepted, metric, dimension)
            per_dim[dimension] = payload
            accepted_hits.extend(hits)
            has_open = has_open or dim_open

        if has_open and total_row["verdict"] != differ.MISMATCH:
            logger.info("метрика «%s»: тотал сошёлся (вердикт %s), но per-key расхождения "
                        "остались -> вердикт поднят до MISMATCH",
                        metric, total_row["verdict"])
            # delta и pct у такой строки остаются нулевыми — они честные, тотал
            # действительно сошёлся. Метки нужны, чтобы потребитель --json отличал
            # настоящее расхождение итога от поднятого вердикта.
            total_row["original_verdict"] = total_row["verdict"]
            total_row["escalated"] = True
            total_row["verdict"] = differ.MISMATCH
        drilldown[metric] = per_dim

    # Глобальная развёртка устаревших: запись реестра устарела ровно тогда, когда за
    # ВЕСЬ прогон она не погасила ничего. Развёртка внутри одного среза этого не видит —
    # запись, чьё измерение не попало в спуск (например, добавили измерение в контракт,
    # но не в _EXTRA_DIMENSIONS), не проверялась бы ни разу и молча жила бы вечно.
    applied = {(h["метрика"], h["измерение"], h["значение"]) for h in accepted_hits}
    stale_all = [entry for entry in accepted
                 if (entry["метрика"], entry["измерение"],
                     entry["значение"]) not in applied]
    return drilldown, accepted_hits, stale_all


def _provenance(contract: dict) -> dict:
    """Паспорт обеих витрин для шапки отчёта (спека §5).

    v6 дополнительно удостоверяется run_id прогона: §8 фиксирует, что текущая витрина
    собрана частичным прогоном `7313aec1fd42` со step6, без фикса rivendell. Вердикт
    без этой строки нельзя отличить от вердикта по полностью пересобранной v6.
    """
    prov = {side: sources.fetch_provenance(contract, side) for side in ("v5", "v6")}
    run_id, run_at = sources.fetch_v6_run(contract)
    prov["v6"]["run_id"] = run_id
    prov["v6"]["run_at"] = run_at
    return prov


def _build_result(contract: dict, accepted) -> dict:
    collector = _Collector(contract)
    metrics = list(contract["metrics"].keys())
    provenance = _provenance(contract)
    totals = _totals(collector, metrics)
    drilldown, accepted_hits, stale_all = _drilldown(collector, accepted, totals)
    return {
        "period": "%s..%s" % (contract["period"]["from"], contract["period"]["to"]),
        "attribution": contract["attribution"],
        "provenance": provenance,
        "totals": totals,
        "drilldown": drilldown,
        "accepted": accepted_hits,
        "stale": stale_all,
        "restrictions": collector.restrictions,
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
        accepted = differ.load_accepted(args.accepted, contract)
        _validate_schemas(contract)
        result = _build_result(contract, accepted)
    except (ContractError, sources.SourceError) as exc:
        logger.error("гейт не выполнен: %s", exc)
        return 2
    except Exception as exc:  # коннект, сеть, битый ответ БД — это тоже ошибка исполнения
        # exc_info: падение против живого прод-контура должно быть разбираемо
        # с одного прогона, без «повтори и посмотри, где именно».
        logger.error("гейт не выполнен: %s: %s", type(exc).__name__, exc, exc_info=True)
        return 2

    if args.json:
        print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    else:
        print(format_report(result))

    blockers = [n for n, r in result["totals"].items() if r["verdict"] == differ.MISMATCH]
    return 1 if blockers else 0


if __name__ == "__main__":
    sys.exit(main())
