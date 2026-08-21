"""Step 0 for v6_ch: ClickHouse-only source preflight.

The v6 pipeline must not copy ready v5/PostgreSQL facts. Step 0 only checks
that raw ClickHouse sources and CH-managed manual reference tables exist before
downstream steps rebuild derived marts.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_settings import RAW_SOURCE_TABLES
from config.ch_utils import SAFE_QUERY_SETTINGS, table_exists
from step0_sync_local import load_city_tier

logger = logging.getLogger("pipeline.step0")


RAW_REQUIRED_EXTRA = {
    "direct_campaigns": "reference_data.direct_campaigns",
    "gsheet_sites": "reference_data.gsheet_sites",
    "telega_in_orders": "raw_data.telega_in_orders",
    "vk_ads_stats_day": "raw_data.vk_ads_stats_day",
    "yandex_direct_korrektirovki": "raw_data.yandex_direct_korrektirovki",
}

# These are business-managed inputs that do not currently have a raw_data live
# loader in this project. We keep them in ClickHouse and fail loudly if missing,
# instead of silently copying PostgreSQL/v5 tables during the pipeline.
CH_MANUAL_INPUTS = {
    "local_pixel_config": "ad_analytics.local_pixel_config",
    "local_pixel_price_history": "ad_analytics.local_pixel_price_history",
    "gsheets_crop_targeting_account": "ad_analytics.gsheets_crop_targeting_account",
    "gsheets_crop_targeting_account_leads": "ad_analytics.gsheets_crop_targeting_account_leads",
    "gsheets_crop_targeting_account_pravilo_utm": "ad_analytics.gsheets_crop_targeting_account_pravilo_utm",
    "yandex_direct_cookie_analytics_website_pages": "ad_analytics.yandex_direct_cookie_analytics_website_pages",
}


def _count_table(client, qualified: str) -> int:
    return int(client.query(f"SELECT count() FROM {qualified}", settings=SAFE_QUERY_SETTINGS).result_rows[0][0])


def _check_objects(client, objects: dict[str, str], *, allow_empty: set[str] | None = None) -> dict[str, int]:
    allow_empty = allow_empty or set()
    counts: dict[str, int] = {}
    missing: list[str] = []
    empty: list[str] = []
    for logical_name, qualified in objects.items():
        database, table = qualified.split(".", 1)
        if not table_exists(client, database, table):
            missing.append(qualified)
            continue
        rows = _count_table(client, qualified)
        counts[logical_name] = rows
        if rows == 0 and logical_name not in allow_empty:
            empty.append(qualified)
    if missing or empty:
        problems = []
        if missing:
            problems.append(f"missing={', '.join(missing)}")
        if empty:
            problems.append(f"empty={', '.join(empty)}")
        raise RuntimeError("ClickHouse preflight failed: " + "; ".join(problems))
    return counts


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    logger.info("Шаг 0 v6_ch: ClickHouse-only preflight без PostgreSQL/v5 sync")
    client = get_client()
    city_tier = load_city_tier.sync_city_tier(client)

    raw_objects = dict(RAW_SOURCE_TABLES)
    raw_objects.update(RAW_REQUIRED_EXTRA)
    counts = _check_objects(client, raw_objects)
    counts.update(_check_objects(client, CH_MANUAL_INPUTS, allow_empty={"local_pixel_price_history"}))
    if table_exists(client, "ad_analytics", "gsheet_city_tier"):
        counts["gsheet_city_tier"] = _count_table(client, "ad_analytics.gsheet_city_tier")

    details = ", ".join(f"{name}={rows:,}" for name, rows in sorted(counts.items()))
    details = f"{details}, city_tier_current={city_tier['rows']:,}, city_tier_seeded={city_tier['seeded_rows']:,}"
    logger.info("Шаг 0 v6_ch завершён: %s", details)
    return {"rows": sum(counts.values()), "details": details}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())
