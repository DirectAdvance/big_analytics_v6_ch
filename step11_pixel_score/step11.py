"""Step 11 for v6_ch: pixel attribution materialization in ClickHouse."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_utils import column_names, count_rows, month_ranges_from_table, q, swap_shadow
from step3_build_sources.step3 import SOURCE_STORE

logger = logging.getLogger("pipeline.step11")


def _key_expr(alias: str = "s") -> str:
    return (
        f"concat(ifNull(toString({alias}.`Date`), ''), '|', ifNull({alias}.domain, ''), '|', "
        f"'Пиксель', '|', ifNull(toString({alias}.`CampaignId`), ''))"
    )


def _create_empty_from_full(client, target: str) -> None:
    client.command(f"DROP TABLE IF EXISTS {target} SYNC")
    client.command(
        f"""
        CREATE TABLE {target}
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(ifNull(`Date`, toDate('2026-01-01')))
        ORDER BY (ifNull(`Date`, toDate('2026-01-01')), ifNull(domain, ''), ifNull(_source_table, ''))
        AS SELECT * FROM ad_analytics.big_analytics_full WHERE 0
        """
    )


def _pixel_select(base_cols: list[str], lo: str, hi: str) -> str:
    exprs: list[str] = []
    for col in base_cols:
        if col == "источник":
            exprs.append("'Пиксель' AS `источник`")
        elif col == "направление":
            exprs.append("'пиксель_атрибуц' AS `направление`")
        elif col == "_source_table":
            exprs.append("'пиксель_атрибуц' AS _source_table")
        elif col == "key_pixel_score":
            exprs.append(f"{_key_expr('s')} AS key_pixel_score")
        else:
            exprs.append(f"s.{q(col)}")
    return f"""
SELECT
    {", ".join(exprs)}
FROM ad_analytics.{SOURCE_STORE} AS s
WHERE s.`Date` >= toDate('{lo}') AND s.`Date` < toDate('{hi}')
  AND s._source_table = 'pixel'
"""


def _rebuild_pixel_score(client) -> int:
    base_cols = column_names(client, "ad_analytics", "big_analytics_full")
    shadow = "ad_analytics.big_analytics_pixel_score_new"
    _create_empty_from_full(client, shadow)
    ranges = month_ranges_from_table(
        client,
        f"ad_analytics.{SOURCE_STORE}",
        "`Date`",
        "`Date` IS NOT NULL AND _source_table = 'pixel'",
    )
    target_cols = ", ".join(q(col) for col in base_cols)
    for idx, (lo, hi) in enumerate(ranges, start=1):
        before = count_rows(client, shadow)
        client.command(f"INSERT INTO {shadow} ({target_cols})\n{_pixel_select(base_cols, lo, hi)}")
        after = count_rows(client, shadow)
        logger.info("  pixel_score batch %d/%d: +%d строк", idx, len(ranges), after - before)
    swap_shadow(client, "ad_analytics.big_analytics_pixel_score", shadow)
    return count_rows(client, "ad_analytics.big_analytics_pixel_score")


def _rebuild_full_with_pixel(client) -> int:
    cols = column_names(client, "ad_analytics", "big_analytics_full")
    cols_sql = ", ".join(q(col) for col in cols)
    shadow = "ad_analytics.big_analytics_full_new"
    _create_empty_from_full(client, shadow)

    full_ranges = month_ranges_from_table(
        client,
        "ad_analytics.big_analytics_full",
        "`Date`",
        "`Date` IS NOT NULL",
    )
    for idx, (lo, hi) in enumerate(full_ranges, start=1):
        before = count_rows(client, shadow)
        client.command(
            f"""
            INSERT INTO {shadow} ({cols_sql})
            SELECT {cols_sql}
            FROM ad_analytics.big_analytics_full
            WHERE `Date` >= toDate('{lo}') AND `Date` < toDate('{hi}')
              AND _source_table != 'пиксель_атрибуц'
            """
        )
        after = count_rows(client, shadow)
        logger.info("  full keep batch %d/%d: +%d строк", idx, len(full_ranges), after - before)

    pixel_ranges = month_ranges_from_table(
        client,
        "ad_analytics.big_analytics_pixel_score",
        "`Date`",
        "`Date` IS NOT NULL",
    )
    for idx, (lo, hi) in enumerate(pixel_ranges, start=1):
        before = count_rows(client, shadow)
        client.command(
            f"""
            INSERT INTO {shadow} ({cols_sql})
            SELECT {cols_sql}
            FROM ad_analytics.big_analytics_pixel_score
            WHERE `Date` >= toDate('{lo}') AND `Date` < toDate('{hi}')
            """
        )
        after = count_rows(client, shadow)
        logger.info("  full pixel batch %d/%d: +%d строк", idx, len(pixel_ranges), after - before)

    swap_shadow(client, "ad_analytics.big_analytics_full", shadow)
    return count_rows(client, "ad_analytics.big_analytics_full")


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    logger.info("Шаг 11 v6_ch: pixel_score + батчевая доливка в full")
    client = get_client()
    t0 = time.perf_counter()
    pixel_rows = _rebuild_pixel_score(client)
    full_rows = _rebuild_full_with_pixel(client)
    details = f"big_analytics_pixel_score={pixel_rows:,}, big_analytics_full={full_rows:,}"
    logger.info("Шаг 11 v6_ch завершён за %.1f сек: %s", time.perf_counter() - t0, details)
    return {"rows": full_rows, "details": details}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())
