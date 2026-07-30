"""Step 0 for v6_ch: validate the existing ClickHouse raw_data layer.

In v5 this step copied FDW sources into Postgres local_* tables. For v6_ch the
source layer already exists in ClickHouse (`raw_data`) and is maintained outside
this project, so step0 is intentionally read-only.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_settings import CH_RAW_DB, RAW_SOURCE_TABLES

logger = logging.getLogger("pipeline.step0")


def _count_table(client, table: str) -> int:
    return int(client.query(f"SELECT count() FROM {table}").result_rows[0][0])


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    """Verify required raw_data tables exist and are non-empty."""
    logger.info("Шаг 0 v6_ch: проверка raw_data в ClickHouse")
    client = get_client(CH_RAW_DB)

    counts: dict[str, int] = {}
    missing: list[str] = []
    empty: list[str] = []

    existing = {
        row[0]
        for row in client.query(
            "SELECT name FROM system.tables WHERE database = {db:String}",
            parameters={"db": CH_RAW_DB},
        ).result_rows
    }

    for logical_name, qualified in RAW_SOURCE_TABLES.items():
        table_name = qualified.split(".", 1)[1]
        if table_name not in existing:
            missing.append(qualified)
            continue
        rows = _count_table(client, qualified)
        counts[logical_name] = rows
        if rows == 0:
            empty.append(qualified)

    if missing or empty:
        problems = []
        if missing:
            problems.append(f"missing={', '.join(missing)}")
        if empty:
            problems.append(f"empty={', '.join(empty)}")
        raise RuntimeError("raw_data preflight failed: " + "; ".join(problems))

    details = ", ".join(f"{name}={rows:,}" for name, rows in sorted(counts.items()))
    logger.info("Шаг 0 v6_ch завершён: %s", details)
    return {"rows": sum(counts.values()), "details": details}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())
