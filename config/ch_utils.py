"""Small ClickHouse helpers for the v6_ch migration."""

from __future__ import annotations

from datetime import date


def q(name: str) -> str:
    """Quote a ClickHouse identifier."""
    return "`" + name.replace("`", "``") + "`"


def table_exists(client, database: str, table: str) -> bool:
    return bool(
        client.query(
            """
            SELECT count()
            FROM system.tables
            WHERE database={database:String} AND name={table:String}
            """,
            parameters={"database": database, "table": table},
        ).result_rows[0][0]
    )


def column_names(client, database: str, table: str) -> list[str]:
    rows = client.query(
        """
        SELECT name
        FROM system.columns
        WHERE database={database:String} AND table={table:String}
        ORDER BY position
        """,
        parameters={"database": database, "table": table},
    ).result_rows
    return [row[0] for row in rows]


def column_list(client, database: str, table: str, alias: str | None = None) -> str:
    prefix = f"{alias}." if alias else ""
    return ", ".join(f"{prefix}{q(name)}" for name in column_names(client, database, table))


def create_empty_like(client, target: str, source: str, engine_sql: str | None = None) -> None:
    """Create an empty table with the same schema as source.

    ClickHouse `CREATE TABLE t AS source` does not copy engine settings. When an
    engine is important for a large table, pass a full `ENGINE ... ORDER BY ...`
    clause in `engine_sql`.
    """
    client.command(f"DROP TABLE IF EXISTS {target} SYNC")
    if engine_sql:
        client.command(f"CREATE TABLE {target} AS {source} {engine_sql}")
    else:
        client.command(f"CREATE TABLE {target} AS {source}")


def swap_shadow(client, target: str, shadow: str) -> None:
    database, table = target.split(".", 1)
    if table_exists(client, database, table):
        client.command(f"EXCHANGE TABLES {target} AND {shadow}")
        client.command(f"DROP TABLE IF EXISTS {shadow} SYNC")
    else:
        client.command(f"RENAME TABLE {shadow} TO {target}")


def month_ranges_from_table(
    client,
    source_table: str,
    date_expr: str,
    where_sql: str = "1 = 1",
    date_from: str = "2026-01-01",
) -> list[tuple[str, str]]:
    row = client.query(
        f"""
        SELECT min(toDate({date_expr})), max(toDate({date_expr}))
        FROM {source_table}
        WHERE {where_sql}
          AND {date_expr} IS NOT NULL
          AND toDate({date_expr}) >= toDate('{date_from}')
        """
    ).result_rows[0]
    if row[0] is None or row[1] is None:
        return []

    current = max(row[0].replace(day=1), date.fromisoformat(date_from))
    last = row[1].replace(day=1)
    ranges: list[tuple[str, str]] = []
    while current <= last:
        nxt = date(current.year + 1, 1, 1) if current.month == 12 else date(current.year, current.month + 1, 1)
        ranges.append((current.isoformat(), nxt.isoformat()))
        current = nxt
    return ranges


def count_rows(client, table: str) -> int:
    return int(client.query(f"SELECT count() FROM {table}").result_rows[0][0])


def replace_view(client, name: str, select_sql: str) -> None:
    """Replace a ClickHouse table-like object with a normal VIEW."""
    client.command(f"DROP TABLE IF EXISTS {name} SYNC")
    client.command(f"CREATE VIEW {name} AS {select_sql}")
