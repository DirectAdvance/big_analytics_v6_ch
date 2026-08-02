"""Build `big_analytics_unified` in ClickHouse."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_utils import column_names, count_rows, q, replace_view

logger = logging.getLogger("pipeline.build_unified")


def _unified_select(source: str, label: str, base_cols: list[str]) -> str:
    select_cols = ", ".join(
        "ifNull(`Название crm`, '') AS `Название crm`" if col == "Название crm" else q(col)
        for col in base_cols
    )
    return f"SELECT {select_cols}, '{label}' AS `атрибуция` FROM ad_analytics.{source}"


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    logger.info("build_unified v6_ch: big_analytics_unified")
    client = get_client()
    t0 = time.perf_counter()
    base_cols = column_names(client, "ad_analytics", "big_analytics_full")
    replace_view(
        client,
        "ad_analytics.big_analytics_unified",
        f"""
        {_unified_select("big_analytics_full", "По дате заявки", base_cols)}
        UNION ALL
        {_unified_select("big_analytics_full_arrival", "По дате визита", base_cols)}
        """,
    )
    rows = count_rows(client, "ad_analytics.big_analytics_unified")
    logger.info("build_unified v6_ch завершён за %.1f сек: rows=%d", time.perf_counter() - t0, rows)
    return {"rows": rows, "details": f"big_analytics_unified={rows:,}"}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())
