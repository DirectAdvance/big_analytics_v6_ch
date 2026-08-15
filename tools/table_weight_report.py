"""Снимок веса таблиц/колонок ad_analytics — «до» и «после» оптимизации.

Один и тот же скрипт даёт обе точки замера, иначе цифры несопоставимы.

    .venv/bin/python3 tools/table_weight_report.py                       # снимок + печать
    .venv/bin/python3 tools/table_weight_report.py --compare logs/x.json # diff со старым снимком

Вес таблицы берётся из system.parts, разбивка по колонкам — из system.parts_columns.
У мелких таблиц (part_type=Compact) все колонки лежат в одном файле, поэтому разбивки по
колонкам у них нет (0) — итог по таблице при этом верный.

План оптимизации, который этим замеряется — OPTIMIZATION_PLAN.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client  # noqa: E402

LOGS_DIR = Path(__file__).resolve().parents[1] / "logs"

_TABLE_WEIGHT_SQL = """
SELECT table, sum(data_compressed_bytes) AS bytes
FROM system.parts
WHERE database = %(db)s AND active
GROUP BY table
ORDER BY bytes DESC
"""

_COLUMN_WEIGHT_SQL = """
SELECT table, column, any(type) AS type, sum(column_data_compressed_bytes) AS bytes
FROM system.parts_columns
WHERE database = %(db)s AND active
GROUP BY table, column
HAVING bytes > 0
ORDER BY bytes DESC
"""


def fetch_table_weights(client, database: str) -> dict[str, int]:
    rows = client.query(_TABLE_WEIGHT_SQL, parameters={"db": database}).result_rows
    return {table: int(size) for table, size in rows}


def fetch_column_weights(client, database: str) -> list[dict]:
    rows = client.query(_COLUMN_WEIGHT_SQL, parameters={"db": database}).result_rows
    return [{"table": t, "column": c, "type": ty, "bytes": int(b)} for t, c, ty, b in rows]


def diff_tables(before: dict[str, int], after: dict[str, int]) -> list[tuple[str, int, int, int]]:
    """(таблица, было, стало, дельта) по объединению обоих снимков, худшие дельты первыми."""
    names = set(before) | set(after)
    rows = [(t, before.get(t, 0), after.get(t, 0), after.get(t, 0) - before.get(t, 0)) for t in names]
    return sorted(rows, key=lambda row: row[3])


def mib(num_bytes: int) -> str:
    return f"{num_bytes / 1024 / 1024:8.2f} MiB"


def print_snapshot(tables: dict[str, int], columns: list[dict], top_columns: int = 25) -> None:
    print(f"\n=== Таблицы ({len(tables)}), всего {mib(sum(tables.values()))} ===")
    for table, size in sorted(tables.items(), key=lambda kv: -kv[1]):
        print(f"{mib(size)}  {table}")
    print(f"\n=== Топ-{top_columns} колонок ===")
    for col in columns[:top_columns]:
        print(f"{mib(col['bytes'])}  {col['table']}.{col['column']}  {col['type']}")


def print_diff(before: dict, after: dict) -> None:
    rows = diff_tables(before, after)
    print("\n=== Дельта по таблицам (было → стало) ===")
    for table, old, new, delta in rows:
        if delta:
            print(f"{mib(old)} → {mib(new)}  {mib(delta)}  {table}")
    print(f"\nИТОГО: {mib(sum(row[3] for row in rows))}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="ad_analytics")
    parser.add_argument("--compare", metavar="OLD_SNAPSHOT.json")
    args = parser.parse_args()
    if args.compare and not Path(args.compare).exists():
        parser.error(f"снимок для сравнения не найден: {args.compare}")

    client = get_client()
    snapshot = {
        "db": args.db,
        "taken_at": datetime.now().isoformat(timespec="seconds"),
        "tables": fetch_table_weights(client, args.db),
        "columns": fetch_column_weights(client, args.db),
    }

    LOGS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = LOGS_DIR / f"table_weight_{args.db}_{stamp}.json"
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8")

    print_snapshot(snapshot["tables"], snapshot["columns"])
    if args.compare:
        old = json.loads(Path(args.compare).read_text(encoding="utf-8"))
        print_diff(old["tables"], snapshot["tables"])
    print(f"\nСнимок: {path}")


if __name__ == "__main__":
    main()
