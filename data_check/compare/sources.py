"""Исполнитель контракта: два коннекта, один общий формат результата.

Своего знания о схемах не имеет — всё берёт из contract.json.
Пустой результат = ошибка: гейт не должен зеленеть от слепоты.
"""
from __future__ import annotations

import os
import sys
from decimal import Decimal
from typing import Dict, List, Set, Tuple


class SourceError(Exception):
    """Не удалось получить сопоставимые данные с одной из сторон."""


# v5 — живой прод-контур Power BI, и гейт вычитывает с него ~4.2 млн строк на срез.
# Таймаут не даёт запросу висеть вечно на чужой блокировке (KNOWN_ISSUES #21).
PG_STATEMENT_TIMEOUT_MS = 300000


def _find_secret_loader() -> str:
    """Ищем .secret/loader.py вверх по дереву — путь до репо не хардкодим."""
    cur = os.path.abspath(os.path.dirname(__file__))
    while True:
        candidate = os.path.join(cur, ".secret", "loader.py")
        if os.path.isfile(candidate):
            return os.path.dirname(candidate)
        parent = os.path.dirname(cur)
        if parent == cur:
            raise SourceError("не найден .secret/loader.py вверх по дереву")
        cur = parent


def pg_connect():
    """Коннект к v5. Сессия сразу переводится в READ ONLY и получает statement_timeout.

    READ ONLY ставится на СЕССИЮ (`SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY`),
    а не на одну транзакцию: запись в прод-контур должна быть невозможна физически,
    а не по договорённости, — и это одинаково для каждого чтения гейта, включая
    интроспекцию схемы. Оба SET выполняются здесь, поэтому ни один вызывающий код
    не может их случайно обойти.
    """
    loader_dir = _find_secret_loader()
    if loader_dir not in sys.path:
        sys.path.insert(0, loader_dir)
    from loader import load_db  # noqa: E402
    import psycopg2  # noqa: E402
    conn = psycopg2.connect(**load_db("victory"))
    try:
        conn.set_session(readonly=True)
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = %s", (PG_STATEMENT_TIMEOUT_MS,))
        conn.commit()
    except Exception:
        conn.close()
        raise
    return conn


def ch_connect():
    from config.ch_db import get_client
    return get_client()


def build_query(contract: dict, side: str, dimension: str) -> Tuple[str, List[str]]:
    dims = contract["dimensions"]
    if dimension not in dims:
        raise SourceError("измерение %r не описано в контракте" % dimension)
    spec = contract["sides"][side]
    dim_expr = dims[dimension].get(side)
    if not dim_expr:
        raise SourceError("измерение %r не выразимо на стороне %s" % (dimension, side))

    joins = list(spec.get("joins", []))
    joins.extend(dims[dimension].get("%s_joins" % side, []))

    metric_names = list(contract["metrics"].keys())
    metric_exprs = ["SUM(%s)" % contract["metrics"][m][side] for m in metric_names]

    if spec["kind"] == "postgres":
        marks = ["%s", "%s", "%s"]
    else:
        marks = ["{p0:String}", "{p1:String}", "{p2:String}"]

    sql = (
        "SELECT {dim} AS dim_value, {metrics}\n"
        "FROM {table} {alias}\n"
        "{joins}\n"
        "WHERE {date} >= {p0} AND {date} <= {p1}\n"
        "  AND {attr} = {p2}\n"
        "  AND {exclude}\n"
        "GROUP BY dim_value\n"
        "ORDER BY dim_value"
    ).format(
        dim=dim_expr,
        metrics=", ".join(metric_exprs),
        table=spec["table"],
        alias=spec["alias"],
        joins="\n".join(joins),
        date=spec["date_expr"],
        attr=spec["attribution_expr"],
        exclude=spec["exclude_expr"],
        p0=marks[0], p1=marks[1], p2=marks[2],
    )
    params = [contract["period"]["from"], contract["period"]["to"], contract["attribution"]]
    return sql, params


def fetch(contract: dict, side: str, dimension: str) -> Dict[str, Dict[str, Decimal]]:
    """Строит запрос, выполняет его на нужной стороне и приводит к общему формату.

    Postgres принимает параметры позиционно (`%s`); ClickHouse — по имени, поэтому
    список параметров мапится в {"p0": ..., "p1": ..., "p2": ...} под плейсхолдеры
    `{pN:String}`, которые уже расставлены в build_query.
    """
    sql, params = build_query(contract, side, dimension)
    metric_names = list(contract["metrics"].keys())
    kind = contract["sides"][side]["kind"]

    if kind == "postgres":
        conn = pg_connect()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        finally:
            conn.close()
    else:
        client = ch_connect()
        param_map = {"p%d" % i: value for i, value in enumerate(params)}
        rows = client.query(sql, parameters=param_map).result_rows

    return rows_to_map(rows, metric_names)


def rows_to_map(rows, metric_names: List[str]) -> Dict[str, Dict[str, Decimal]]:
    if not rows:
        raise SourceError("запрос вернул 0 строк — это ошибка исполнения, а не нулевые метрики")
    out = {}
    for row in rows:
        key = row[0]
        if key is None or str(key).strip() == "":
            key = "(пусто)"
        out[str(key)] = {
            # SUM() возвращает NULL именно когда в группе нет ни одного NOT NULL значения —
            # это легитимное состояние данных, а не слепота гейта. Слепота уже перехвачена
            # выше: 0 строк -> SourceError, отсутствующая колонка -> ContractError в
            # validate_columns. Поэтому NULL-метрика здесь осознанно становится Decimal(0).
            name: Decimal(str(row[i + 1] if row[i + 1] is not None else 0))
            for i, name in enumerate(metric_names)
        }
    return out


def pg_columns(conn, tables: List[str]) -> Dict[str, Set[str]]:
    existing = {}
    with conn.cursor() as cur:
        for full in tables:
            schema, name = full.split(".", 1)
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s", (schema, name))
            cols = {r[0] for r in cur.fetchall()}
            if cols:
                existing[full] = cols
    return existing


def ch_columns(client, tables: List[str]) -> Dict[str, Set[str]]:
    existing = {}
    for full in tables:
        database, name = full.split(".", 1)
        rows = client.query(
            "SELECT name FROM system.columns WHERE database = {d:String} AND table = {t:String}",
            parameters={"d": database, "t": name}).result_rows
        cols = {r[0] for r in rows}
        if cols:
            existing[full] = cols
    return existing
