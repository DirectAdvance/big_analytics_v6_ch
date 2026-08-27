"""Build BA6-owned effective `gsheet_sites` reference.

`reference_data.gsheet_sites` is CH-managed and read-only for this pipeline user.
BA5 still has the complete Google Sheets mirror in PostgreSQL `public.gsheet_sites`;
we copy that small reference table into `ad_analytics` and use it only to fill
missing domains or blank directologists.
"""

from __future__ import annotations

import hashlib
import logging
import sys
from pathlib import Path

import psycopg2

from config.ch_settings import GSHEET_SITES_EFFECTIVE
from config.ch_utils import SAFE_QUERY_SETTINGS, q, swap_shadow

for _p in Path(__file__).resolve().parents:
    if (_p / ".secret" / "loader.py").exists():
        sys.path.insert(0, str(_p / ".secret"))
        break
from loader import load_db  # noqa: E402

log = logging.getLogger("pipeline.step0.gsheet_sites_overlay")

OVERLAY_TABLE = "ad_analytics.gsheet_sites_pg_overlay"
OVERLAY_SHADOW = "ad_analytics.gsheet_sites_pg_overlay_new"
EFFECTIVE_SHADOW = "ad_analytics.gsheet_sites_effective_new"

SITE_COLUMNS = [
    "row_hash",
    "site_id",
    "domain",
    "status",
    "developer",
    "agency",
    "launch_date",
    "block_date",
    "phone_number",
    "directologist",
    "project_manager",
    "sales_manager",
    "site_type",
    "template",
    "client_id",
    "salon",
    "inn",
    "contract_number",
    "crm",
    "city",
    "region",
    "renewal_date",
    "integration_configured",
    "tags",
    "direction_main",
    "website_id",
    "moderation_days",
    "lifetime",
    "leadgen",
    "login_key",
    "vk_client_id",
    "counter_number",
    "agency_type",
    "channel",
    "agency_account",
    "direction",
    "niche",
    "analytics_table",
]


def _pg_rows() -> list[list[str | None]]:
    db = load_db("victory")
    conn = psycopg2.connect(
        host=db["host"],
        port=db["port"],
        dbname="ad_analytics_bi",
        user=db["user"],
        password=db["password"],
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT DISTINCT ON (lower(trim(domain))) {", ".join(SITE_COLUMNS)}
                FROM public.gsheet_sites
                WHERE coalesce(trim(domain), '') <> ''
                ORDER BY lower(trim(domain)), (coalesce(trim(directologist), '') = ''), row_hash
                """
            )
            rows = [list(row) for row in cur.fetchall()]
    finally:
        conn.close()
    for row in rows:
        if not row[0]:
            row[0] = hashlib.md5(str(row[2]).strip().lower().encode()).hexdigest()
    return rows


def _create_overlay(client, table: str) -> None:
    nullable = ",\n    ".join(f"{q(col)} Nullable(String)" for col in SITE_COLUMNS[1:])
    client.command(
        f"""
        CREATE TABLE {table}
        (
            `row_hash` String,
            {nullable},
            `_loaded_at` DateTime64(3, 'UTC') DEFAULT now64(3)
        )
        ENGINE = MergeTree
        ORDER BY row_hash
        """,
        settings=SAFE_QUERY_SETTINGS,
    )


def _sync_overlay(client) -> int:
    rows = _pg_rows()
    client.command(f"DROP TABLE IF EXISTS {OVERLAY_SHADOW} SYNC", settings=SAFE_QUERY_SETTINGS)
    _create_overlay(client, OVERLAY_SHADOW)
    if rows:
        client.insert(OVERLAY_SHADOW, rows, column_names=SITE_COLUMNS)
    swap_shadow(client, OVERLAY_TABLE, OVERLAY_SHADOW)
    return len(rows)


def _rebuild_effective(client) -> int:
    tuple_cols = ", ".join(q(col) for col in SITE_COLUMNS)
    select_cols = ",\n            ".join(f"best.{idx} AS {q(col)}" for idx, col in enumerate(SITE_COLUMNS, start=1))
    client.command(f"DROP TABLE IF EXISTS {EFFECTIVE_SHADOW} SYNC", settings=SAFE_QUERY_SETTINGS)
    client.command(
        f"""
        CREATE TABLE {EFFECTIVE_SHADOW}
        ENGINE = MergeTree
        ORDER BY row_hash
        AS
        SELECT
            {select_cols},
            now64(3) AS `_loaded_at`
        FROM
        (
            SELECT
                domain_key,
                argMax(tuple({tuple_cols}), weight) AS best
            FROM
            (
                SELECT
                    lowerUTF8(trim(ifNull(domain, ''))) AS domain_key,
                    if(trim(ifNull(directologist, '')) != '', 4, 2) AS weight,
                    {tuple_cols}
                FROM reference_data.gsheet_sites
                WHERE ifNull(domain, '') != ''

                UNION ALL

                SELECT
                    lowerUTF8(trim(ifNull(domain, ''))) AS domain_key,
                    if(trim(ifNull(directologist, '')) != '', 3, 1) AS weight,
                    {tuple_cols}
                FROM {OVERLAY_TABLE}
                WHERE ifNull(domain, '') != ''
            )
            GROUP BY domain_key
        )
        """,
        settings=SAFE_QUERY_SETTINGS,
    )
    swap_shadow(client, GSHEET_SITES_EFFECTIVE, EFFECTIVE_SHADOW)
    return int(client.query(f"SELECT count() FROM {GSHEET_SITES_EFFECTIVE}", settings=SAFE_QUERY_SETTINGS).result_rows[0][0])


def sync_gsheet_sites_effective(client) -> dict[str, int]:
    overlay_rows = _sync_overlay(client)
    effective_rows = _rebuild_effective(client)
    log.info("gsheet_sites overlay=%d effective=%d", overlay_rows, effective_rows)
    return {"gsheet_sites_pg_overlay": overlay_rows, "gsheet_sites_effective": effective_rows}
