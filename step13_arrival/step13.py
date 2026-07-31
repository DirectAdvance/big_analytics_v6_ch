"""Step 13 for v6_ch: build arrival mirror table in ClickHouse."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_settings import DATE_FROM
from config.ch_utils import SAFE_QUERY_SETTINGS, column_names, count_rows, day_ranges, q, swap_shadow

logger = logging.getLogger("pipeline.step13")


def _create_empty(client, target: str) -> None:
    client.command(f"DROP TABLE IF EXISTS {target} SYNC")
    client.command(
        f"""
        CREATE TABLE {target}
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(ifNull(`Date`, toDate('2026-01-01')))
        ORDER BY (ifNull(`Date`, toDate('2026-01-01')), ifNull(domain, ''), ifNull(_source_table, ''))
        AS SELECT * FROM ad_analytics.big_analytics_full WHERE 0
        """,
        settings=SAFE_QUERY_SETTINGS,
    )


def _select_exprs(cols: list[str]) -> str:
    exprs: list[str] = []
    for col in cols:
        if col in {"Impressions", "Clicks", "total_cost"}:
            exprs.append(f"toDecimal64(0, 6) AS {q(col)}")
        elif col in {"priezd_arrival_date", "prodazhi_arrival_date"}:
            exprs.append(f"CAST(NULL, 'Nullable(Int64)') AS {q(col)}")
        elif col == "key_pixel_score":
            exprs.append(
                "concat(ifNull(toString(s.`Date`), ''), '|', ifNull(s.domain, ''), '|', "
                "ifNull(s.`источник`, ''), '|', ifNull(toString(s.`CampaignId`), '')) AS key_pixel_score"
            )
        else:
            exprs.append(f"s.{q(col)}")
    return ", ".join(exprs)


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    logger.info("Шаг 13 v6_ch: big_analytics_full_arrival")
    client = get_client()
    t0 = time.perf_counter()
    shadow = "ad_analytics.big_analytics_full_arrival_new"
    _create_empty(client, shadow)
    cols = column_names(client, "ad_analytics", "big_analytics_full")
    target_cols = ", ".join(q(col) for col in cols)
    select_exprs = _select_exprs(cols)
    ranges = day_ranges(DATE_FROM)
    for idx, (lo, hi) in enumerate(ranges, start=1):
        client.command(
            f"""
            INSERT INTO {shadow} ({target_cols})
            SELECT {select_exprs}
            FROM ad_analytics.big_analytics_full AS s
            WHERE s.`Date` >= toDate('{lo}') AND s.`Date` < toDate('{hi}')
              AND (s.priezd > 0 OR s.prodazhi > 0)
            """,
            settings=SAFE_QUERY_SETTINGS,
        )
        logger.info("  arrival daily batch %d/%d inserted: %s -> %s", idx, len(ranges), lo, hi)
    swap_shadow(client, "ad_analytics.big_analytics_full_arrival", shadow)
    rows = count_rows(client, "ad_analytics.big_analytics_full_arrival")
    logger.info("Шаг 13 v6_ch завершён за %.1f сек: rows=%d", time.perf_counter() - t0, rows)
    return {"rows": rows, "details": f"big_analytics_full_arrival={rows:,}"}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())
