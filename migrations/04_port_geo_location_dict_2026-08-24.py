#!/usr/bin/env python3
"""04_port_geo_location_dict_2026-08-24 — one-time port of the BA5 geo-location dictionary.

Root cause (oleg_read_bd, GEO_LOCATION_JOIN_LOST): the JOIN to the geo dictionary was dropped in
the very first ClickHouse-migration commit `2c6c928` and never flagged in KNOWN_ISSUES/PLAN/
RAW_DATA_LOAD_GAPS. Commit `451b80e` later removed the always-NULL columns from the staging query,
which is why `star_refactor/build_star.py` hardcodes NULL/'' for `location`/`Область`/
`GeoRegionType`/`distance_km_agreg` in `Dim_Location` today.

Source of truth: Victory Postgres `ad_analytics_bi.public.local_gsheet_yandex_direct_id_location`
(BA5). It is a FROZEN SNAPSHOT there too (not in BA5's TRUNCATE_INSERT_TABLES) — a one-time port,
not a recurring sync. Do NOT wire this into step0/cron; if the sheet behind it ever needs to move
again, that is a new, deliberate decision, not an automatic re-sync.

Placement: `ad_analytics` (not `reference_data` — the pipeline role only has SELECT there, see
migrations/02_status_mapping_ab_2026-08-05.py; not `raw_data` — that DB is the external loader's
exclusive write zone). Mirrors the existing frozen-snapshot pattern of `ad_analytics.gsheet_city_tier`
(step0_sync_local/load_city_tier.py).

Run (idempotent, safe to repeat):
    .venv/bin/python3 "migrations/04_port_geo_location_dict_2026-08-24.py" [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from config.ch_db import get_client  # noqa: E402
from config.ch_utils import count_rows, swap_shadow  # noqa: E402
from config.settings import DB_DST  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("port_geo_location_dict")

TARGET = "ad_analytics.gsheet_yandex_direct_id_location"
PG_SOURCE = "public.local_gsheet_yandex_direct_id_location"

_COLUMNS = ["id_location", "location", "GeoRegionType", "Область", "distance_km", "distance_km_agreg"]

DDL = f"""
CREATE TABLE {{table}}
(
    id_location Int64,
    location String,
    `GeoRegionType` LowCardinality(Nullable(String)),
    `Область` LowCardinality(Nullable(String)),
    distance_km Nullable(Int32),
    distance_km_agreg Nullable(Int32),
    loaded_at DateTime DEFAULT now()
)
ENGINE = MergeTree
ORDER BY id_location
"""


def fetch_rows() -> list[tuple]:
    import psycopg2

    conn = psycopg2.connect(**DB_DST)
    try:
        cur = conn.cursor()
        cur.execute(
            f'SELECT id_location, location, "GeoRegionType", "Область", distance_km, distance_km_agreg '
            f"FROM {PG_SOURCE} ORDER BY id_location"
        )
        return cur.fetchall()
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="fetch + report, no CH write")
    args = parser.parse_args(argv)

    rows = fetch_rows()
    distinct_ids = len({row[0] for row in rows})
    with_distance = sum(1 for row in rows if row[4] is not None)
    log.info("PG source: rows=%d distinct id_location=%d distance filled=%d", len(rows), distinct_ids, with_distance)
    if distinct_ids != len(rows):
        log.error("id_location is not unique in the source — refusing to port a dirty key")
        return 1
    if args.dry_run:
        return 0

    client = get_client()
    shadow = f"{TARGET}_new"
    client.command(f"DROP TABLE IF EXISTS {shadow} SYNC")
    client.command(DDL.format(table=shadow))
    client.insert(shadow, rows, column_names=_COLUMNS)
    swap_shadow(client, TARGET, shadow)

    final_rows = count_rows(client, TARGET)
    log.info("done: %s rows=%d", TARGET, final_rows)
    if final_rows != len(rows):
        log.error("row count mismatch after swap: pg=%d ch=%d", len(rows), final_rows)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
