#!/usr/bin/env python3
"""
Read-only diagnostics for big_analytics_v5 pipeline performance and coverage.

This script is intentionally a narrow entrypoint for DB diagnostics, so Codex can
run one stable command after the user approves network access once:

    python3 tools/diagnose_pipeline.py

It does not modify the database.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Iterable

import psycopg2

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from config.settings import DB_DST  # noqa: E402


DEFAULT_MISSING_FULL = [
    "porg-2jd6mkdf",
    "porg-4ne4h2vj",
    "porg-4snj6oh4",
    "porg-6scrv7r3",
    "porg-dihmkfjt",
    "porg-dtkzhemq",
    "porg-dw7ov2yd",
    "porg-eumt43aa",
    "porg-fsl7lli2",
    "porg-iuc5t22r",
    "porg-jgtsarpr",
    "porg-jxv3b5dm",
    "porg-qfif3aby",
    "porg-qsmcjix3",
    "porg-r3abzpoq",
    "porg-rpsw3psq",
    "porg-tcvp45ph",
    "porg-voekkrmy",
    "porg-vy7s53ic",
    "porg-y44wdaar",
    "porg-ybl6hi25",
    "porg-yjuaq34w",
]


TABLES = [
    "big_analytics_full",
    "big_analytics_direct",
    "raw_yandex",
    "raw_leads",
    "big_analytics_full_arrival",
    "big_analytics_unified",
    "fact_big_analytics",
    "arp_fact",
    "fact_region_spend",
    "fact_adformat_spend",
    "fact_criterion_spend",
    "yandex_direct_manager_reports",
    "local_gsheet_sites",
]


HEAVY_STEPS = [
    "step3",
    "corrections",
    "step6",
    "step7",
    "step13_rebuild",
    "build_unified",
    "build_region_spend",
    "build_adformat_spend",
    "build_criterion_spend",
]


def _print_rows(title: str, headers: Iterable[str], rows: Iterable[Iterable[object]]) -> None:
    print(f"\n== {title} ==")
    print("\t".join(headers))
    for row in rows:
        print("\t".join("" if value is None else str(value) for value in row))


def _connect():
    cfg = dict(DB_DST)
    cfg["connect_timeout"] = 30
    return psycopg2.connect(**cfg)


def table_sizes(cur) -> None:
    cur.execute(
        """
        SELECT relname,
               pg_size_pretty(pg_total_relation_size(relid)) AS total_size,
               pg_total_relation_size(relid) AS bytes,
               n_live_tup,
               n_dead_tup,
               CASE WHEN n_live_tup > 0
                    THEN round((100.0 * n_dead_tup / n_live_tup)::numeric, 1)
                    ELSE NULL
               END AS dead_pct,
               last_analyze,
               last_vacuum,
               last_autovacuum
        FROM pg_stat_user_tables
        WHERE relname = ANY(%s::text[])
        ORDER BY pg_total_relation_size(relid) DESC
        """,
        (TABLES,),
    )
    _print_rows(
        "table_sizes",
        [
            "relname",
            "total_size",
            "bytes",
            "n_live_tup",
            "n_dead_tup",
            "dead_pct",
            "last_analyze",
            "last_vacuum",
            "last_autovacuum",
        ],
        cur.fetchall(),
    )


def recent_heavy_steps(cur, days: int, limit: int) -> None:
    cur.execute(
        """
        SELECT run_id,
               step,
               status,
               rows_affected,
               duration_sec,
               left(coalesce(details, ''), 180) AS details,
               run_at
        FROM data_quality_log
        WHERE run_at > now() - (%s::text || ' days')::interval
          AND step = ANY(%s::text[])
        ORDER BY run_at DESC
        LIMIT %s
        """,
        (days, HEAVY_STEPS, limit),
    )
    _print_rows(
        "recent_heavy_steps",
        ["run_id", "step", "status", "rows_affected", "duration_sec", "details", "run_at"],
        cur.fetchall(),
    )


def login_coverage(cur) -> None:
    login_filter = """
        direction = 'Авто'
        AND login_key IS NOT NULL
        AND login_key != ''
        AND login_key != 'Нет'
        AND login_key ~ '^[a-z0-9]'
        AND (
            block_date = '' OR block_date IS NULL
            OR (
                block_date ~ E'^[0-9]{2}\\.[0-9]{2}\\.[0-9]{4}$'
                AND TO_DATE(block_date, 'DD.MM.YYYY') >= '2026-01-01'
            )
        )
    """
    rows = []
    cur.execute(f"SELECT COUNT(DISTINCT login_key) FROM public.local_gsheet_sites WHERE {login_filter}")
    total = int(cur.fetchone()[0] or 0)
    rows.append(("gsheet_active_auto", total, "baseline"))

    cur.execute(
        f"""
        WITH active_logins AS (
            SELECT DISTINCT account_login
            FROM public.yandex_direct_manager_reports
            WHERE total_cost > 0
              AND account_login IS NOT NULL
        )
        SELECT COUNT(DISTINCT gs.login_key)
        FROM public.local_gsheet_sites gs
        WHERE {login_filter}
          AND EXISTS (
              SELECT 1
              FROM active_logins a
              WHERE a.account_login = gs.login_key
          )
        """
    )
    in_fdw = int(cur.fetchone()[0] or 0)
    rows.append(("fdw_cost_gt_0", in_fdw, f"missing={total - in_fdw}"))

    cur.execute(
        f"""
        SELECT COUNT(DISTINCT gs.login_key)
        FROM public.local_gsheet_sites gs
        WHERE {login_filter}
          AND EXISTS (
              SELECT 1
              FROM public.big_analytics_full f
              WHERE f.account_login = gs.login_key
          )
        """
    )
    in_full = int(cur.fetchone()[0] or 0)
    rows.append(("big_analytics_full", in_full, f"missing={total - in_full}"))

    _print_rows("login_coverage", ["bucket", "count", "note"], rows)


def missing_login_details(cur, logins: list[str]) -> None:
    if not logins:
        return
    cur.execute(
        """
        WITH logins(login_key) AS (SELECT unnest(%s::text[])),
        gs AS (
          SELECT login_key,
                 count(*) AS gs_rows,
                 string_agg(DISTINCT coalesce(domain, ''), ', ' ORDER BY coalesce(domain, '')) AS domains,
                 string_agg(DISTINCT coalesce(status, ''), ', ' ORDER BY coalesce(status, '')) AS statuses,
                 string_agg(DISTINCT coalesce(block_date, ''), ', ' ORDER BY coalesce(block_date, '')) AS block_dates,
                 string_agg(DISTINCT coalesce(directologist, ''), ', ' ORDER BY coalesce(directologist, '')) AS specialists
          FROM local_gsheet_sites
          WHERE login_key = ANY(%s::text[])
          GROUP BY login_key
        ),
        fdw AS (
          SELECT account_login AS login_key,
                 count(*) AS fdw_rows,
                 round(sum(total_cost)::numeric, 2) AS fdw_cost,
                 min("Date"::date) AS fdw_min,
                 max("Date"::date) AS fdw_max
          FROM yandex_direct_manager_reports
          WHERE account_login = ANY(%s::text[])
          GROUP BY account_login
        ),
        baf AS (
          SELECT account_login AS login_key,
                 count(*) AS full_rows,
                 round(sum(total_cost)::numeric, 2) AS full_cost,
                 min("Date") AS full_min,
                 max("Date") AS full_max
          FROM big_analytics_full
          WHERE account_login = ANY(%s::text[])
          GROUP BY account_login
        )
        SELECT l.login_key,
               coalesce(gs.gs_rows, 0) AS gs_rows,
               coalesce(fdw.fdw_rows, 0) AS fdw_rows,
               coalesce(fdw.fdw_cost, 0) AS fdw_cost,
               fdw.fdw_min,
               fdw.fdw_max,
               coalesce(baf.full_rows, 0) AS full_rows,
               coalesce(baf.full_cost, 0) AS full_cost,
               gs.statuses,
               gs.block_dates,
               left(coalesce(gs.domains, ''), 160) AS domains,
               gs.specialists
        FROM logins l
        LEFT JOIN gs USING (login_key)
        LEFT JOIN fdw USING (login_key)
        LEFT JOIN baf USING (login_key)
        ORDER BY l.login_key
        """,
        (logins, logins, logins, logins),
    )
    _print_rows(
        "missing_login_details",
        [
            "login",
            "gs_rows",
            "fdw_rows",
            "fdw_cost",
            "fdw_min",
            "fdw_max",
            "full_rows",
            "full_cost",
            "statuses",
            "block_dates",
            "domains",
            "specialists",
        ],
        cur.fetchall(),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=5, help="How many recent days of data_quality_log to show.")
    parser.add_argument("--limit", type=int, default=80, help="Max recent log rows.")
    parser.add_argument(
        "--login",
        action="append",
        default=[],
        help="Login key to inspect. Can be repeated. Defaults to the last reported missing_full list.",
    )
    parser.add_argument(
        "--no-default-logins",
        action="store_true",
        help="Do not inspect the built-in missing_full list unless --login is provided.",
    )
    parser.add_argument(
        "--skip-login-coverage",
        action="store_true",
        help="Skip the FDW-based login coverage scan.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logins = args.login or ([] if args.no_default_logins else DEFAULT_MISSING_FULL)

    with _connect() as conn:
        conn.set_session(readonly=True, autocommit=False)
        with conn.cursor() as cur:
            table_sizes(cur)
            recent_heavy_steps(cur, args.days, args.limit)
            if not args.skip_login_coverage:
                login_coverage(cur)
            missing_login_details(cur, logins)
        conn.rollback()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
