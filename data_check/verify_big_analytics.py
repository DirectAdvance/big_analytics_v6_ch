#!/usr/bin/env python3
"""Master verification for big_analytics_v6_ch ClickHouse tables."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_utils import count_rows, table_exists

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("verify_big_analytics")


REQUIRED_TABLES = [
    "raw_yandex",
    "raw_leads",
    "raw_calls",
    "big_analytics_sources",
    "big_analytics_full",
    "big_analytics_pixel_score",
    "big_analytics_full_arrival",
    "big_analytics_unified",
    "fact_big_analytics",
    "pbi_big_analytics_full",
    "pixel_score",
    "arp_fact",
    "arc_fact",
    "arf_fact",
    "dim_criterion",
]

PBI_SOURCE_OBJECTS = [
    "Dim_AdGroup",
    "Dim_Campaign",
    "Dim_Date",
    "Dim_Location",
    "Dim_PlacementFeed",
    "Dim_Site",
    "arc_fact",
    "arf_fact",
    "arp_fact",
    "pbi_big_analytics_full",
    "big_analytics_full_arrival",
    "check_utm_fuck_direct",
    "dim_criterion",
    "yandex_direct_history",
    "fact_adformat_spend",
    "fact_criterion_spend",
    "fact_criterion_zayavki",
    "fact_direct_feed_funnel",
    "fact_ml_korrektirovki",
    "fact_region_spend",
    "fact_region_zayavki",
    "fact_vk_ads",
    "pixel_score",
    "v_yandex_direct_minus_delta",
    "yandex_direct_404_errors",
    "yandex_direct_cookie_analytics_website_pages",
    "yandex_direct_korrektirovki",
    "yandex_direct_minus_snapshot",
    "yandex_direct_return_commission_report",
]

PBI_EMPTY_ALLOWED = {
    "bi_Dim_Location",
    "bi_check_utm_fuck_direct",
    "bi_fact_criterion_zayavki",
    "bi_fact_ml_korrektirovki",
    "bi_fact_region_zayavki",
    "bi_fact_vk_ads",
    "bi_v_yandex_direct_minus_delta",
    "bi_yandex_direct_404_errors",
    "bi_yandex_direct_cookie_analytics_website_pages",
    "bi_yandex_direct_korrektirovki",
    "bi_yandex_direct_minus_snapshot",
    "bi_yandex_direct_return_commission_report",
}

PBI_COMPAT_OBJECTS = [
    "pbi_import_big_analytics_full",
    "pbi_import_fact_direct_feed_funnel",
    "pbi_import_region_spend",
    "arp_fact",
    "arf_fact",
    "arc_fact",
]

FULL_PHYSICAL_TABLES = [
    "pixel_score",
    "dim_criterion",
    "Dim_AdFormat",
    "Dim_AdNetworkType",
    "Dim_Source",
]


def _scalar(client, sql: str):
    return client.query(sql).result_rows[0][0]


def _engine(client, table: str) -> str | None:
    rows = client.query(
        """
        SELECT engine
        FROM system.tables
        WHERE database='ad_analytics' AND name={table:String}
        """,
        parameters={"table": table},
    ).result_rows
    return rows[0][0] if rows else None


def run(full: bool = False, no_star: bool = False, tg: bool = False) -> int:  # noqa: ARG001
    client = get_client()
    failures: list[str] = []

    for table in REQUIRED_TABLES:
        if no_star and table == "fact_big_analytics":
            continue
        if not table_exists(client, "ad_analytics", table):
            failures.append(f"missing:{table}")
            continue
        rows = count_rows(client, f"ad_analytics.{table}")
        log.info("%s=%d", table, rows)
        if table not in {"big_analytics_crop_targeting"} and rows == 0:
            failures.append(f"empty:{table}")

    for source in PBI_SOURCE_OBJECTS:
        view = f"bi_{source}"
        engine = _engine(client, view)
        if engine is None:
            failures.append(f"missing_pbi_view:{view}")
            continue
        if engine != "View":
            failures.append(f"pbi_not_view:{view}:{engine}")
        rows = count_rows(client, f"ad_analytics.`{view}`")
        log.info("%s=%d engine=%s", view, rows, engine)
        if view not in PBI_EMPTY_ALLOWED and rows == 0:
            failures.append(f"empty_pbi_view:{view}")

    if not no_star:
        for table in PBI_COMPAT_OBJECTS:
            engine = _engine(client, table)
            if engine is None:
                failures.append(f"missing_compat:{table}")
                continue
            rows = count_rows(client, f"ad_analytics.`{table}`")
            log.info("%s=%d engine=%s", table, rows, engine)
            if rows == 0:
                failures.append(f"empty_compat:{table}")

        for table in FULL_PHYSICAL_TABLES:
            engine = _engine(client, table)
            if engine is None:
                failures.append(f"missing_physical:{table}")
                continue
            if engine == "View":
                failures.append(f"physical_is_view:{table}")
            rows = count_rows(client, f"ad_analytics.`{table}`")
            log.info("%s=%d engine=%s", table, rows, engine)
            if rows == 0:
                failures.append(f"empty_physical:{table}")

    checks = {
        "raw_yandex_cost_zero": "SELECT if(sum(total_cost) = 0, 1, 0) FROM ad_analytics.raw_yandex",
        "full_before_2026": "SELECT count() FROM ad_analytics.big_analytics_full WHERE `Date` < toDate('2026-01-01')",
        "full_null_source": "SELECT count() FROM ad_analytics.big_analytics_full WHERE `источник` IS NULL OR `источник` = ''",
        "full_funnel_korr_lt_kval": "SELECT count() FROM ad_analytics.big_analytics_full WHERE korr < kval",
        "full_funnel_kval_lt_priezd": "SELECT count() FROM ad_analytics.big_analytics_full WHERE kval < priezd",
        "full_funnel_priezd_lt_prodazhi": "SELECT count() FROM ad_analytics.big_analytics_full WHERE priezd < prodazhi",
        "unified_count_mismatch": """
            SELECT if(
                (SELECT count() FROM ad_analytics.big_analytics_unified)
                !=
                (SELECT count() FROM ad_analytics.big_analytics_full)
                + (SELECT count() FROM ad_analytics.big_analytics_full_arrival),
                1, 0
            )
        """,
    }
    if not no_star:
        checks["fact_unified_count_mismatch"] = """
            SELECT if(
                (SELECT count() FROM ad_analytics.fact_big_analytics)
                !=
                (SELECT count() FROM ad_analytics.big_analytics_unified),
                1, 0
            )
        """

    for name, sql in checks.items():
        value = int(_scalar(client, sql))
        log.info("%s=%d", name, value)
        if value:
            failures.append(f"{name}={value}")

    if failures:
        log.error("FAIL: %s", "; ".join(failures))
        return 1
    log.info("PASS")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--no-star", action="store_true")
    parser.add_argument("--tg", action="store_true")
    args = parser.parse_args()
    try:
        return run(full=args.full, no_star=args.no_star, tg=args.tg)
    except Exception:
        log.exception("CRASH")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
