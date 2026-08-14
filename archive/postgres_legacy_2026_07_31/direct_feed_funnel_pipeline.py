"""Pipeline for Direct feed funnel.

Step 1 validates the Yandex feed report source table.
Step 2 builds the physical keyed funnel tables.

The actual Yandex API loader for public.yandex_direct_feeds_report is external
to this package for now; this pipeline fails early if that source is empty or
stale, so the funnel is not rebuilt from bad input.
"""

from __future__ import annotations

import argparse
import logging
import time
import uuid
from datetime import date, timedelta

from config.db import close_pool, get_conn, init_pool, put_conn
from config.settings import DATE_FROM

from . import build_keyed, build_report_criterion, build_report_feed

logger = logging.getLogger("pipeline.direct_feed_funnel")


def _source_stats() -> tuple[int, date | None, date | None, int, float]:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    count(*)::bigint,
                    min(date),
                    max(date),
                    coalesce(sum(clicks), 0)::bigint,
                    coalesce(sum(cost), 0)::double precision
                FROM public.yandex_direct_feeds_report
                WHERE date >= DATE %s
                """,
                (DATE_FROM,),
            )
            count_rows, min_date, max_date, clicks, cost = cur.fetchone()
            return int(count_rows), min_date, max_date, int(clicks), float(cost)
    finally:
        put_conn(conn)


def check_yandex_feed_source(max_lag_days: int) -> None:
    count_rows, min_date, max_date, clicks, cost = _source_stats()
    logger.info(
        "source yandex_direct_feeds_report: rows=%s date=%s..%s clicks=%s cost=%.2f",
        count_rows,
        min_date,
        max_date,
        clicks,
        cost,
    )

    if count_rows == 0 or max_date is None:
        raise RuntimeError("public.yandex_direct_feeds_report is empty")

    min_allowed_date = date.today() - timedelta(days=max_lag_days)
    if max_date < min_allowed_date:
        raise RuntimeError(
            "public.yandex_direct_feeds_report is stale: "
            f"max_date={max_date}, expected >= {min_allowed_date}"
        )


def run(skip_source_check: bool, max_source_lag_days: int, no_build: bool) -> None:
    run_id = uuid.uuid4().hex[:8]
    started = time.monotonic()
    logger.info("direct_feed_funnel started run_id=%s", run_id)

    if not skip_source_check:
        t0 = time.monotonic()
        check_yandex_feed_source(max_source_lag_days)
        logger.info("OK source_check %.1fs", time.monotonic() - t0)

    if not no_build:
        t0 = time.monotonic()
        build_keyed.build()
        logger.info("OK build_keyed %.1fs", time.monotonic() - t0)

        # ANALYTICS_REPORT_FEED_2026-06-29: пересборка денормализованного отчёта
        # analytics_report_feed для PBI-страницы «Фиды» (DROP + CREATE AS SELECT
        # из fact_direct_feed_funnel + local_gsheet_sites + Dim_Campaign + Dim_AdGroup).
        t0 = time.monotonic()
        stats = build_report_feed.build()
        logger.info(
            "OK build_report_feed rows=%s date=%s..%s специалист=%.0f%% тип_сайта=%.0f%% %.1fs",
            stats["rows"], stats["min_date"], stats["max_date"],
            stats["specialist_pct"], stats["site_type_pct"],
            time.monotonic() - t0,
        )

        # BUILD_REPORT_CRITERION_2026-06-29: пересборка денормализованного отчёта
        # analytics_report_criterion для PBI-страницы «Критерий» (DROP + CREATE AS SELECT
        # из local_leads_all + fact_criterion_spend + local_gsheet_sites, grain date×domain×tp).
        t0 = time.monotonic()
        stats_c = build_report_criterion.build()
        logger.info(
            "OK build_report_criterion rows=%s date=%s..%s cost=%.2f kol_vo=%s korr=%s"
            " kval=%s priezd=%s prodazhi=%s %.1fs",
            stats_c["rows"], stats_c["min_date"], stats_c["max_date"],
            stats_c["total_cost"], stats_c["kol_vo_zayavok"], stats_c["korr"],
            stats_c["kval"], stats_c["priezd"], stats_c["prodazhi"],
            time.monotonic() - t0,
        )

    logger.info("direct_feed_funnel finished run_id=%s wall=%.1fs", run_id, time.monotonic() - started)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-source-check", action="store_true")
    parser.add_argument("--max-source-lag-days", type=int, default=3)
    parser.add_argument("--no-build", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    init_pool()
    try:
        run(
            skip_source_check=args.skip_source_check,
            max_source_lag_days=args.max_source_lag_days,
            no_build=args.no_build,
        )
    finally:
        close_pool()


if __name__ == "__main__":
    main()
