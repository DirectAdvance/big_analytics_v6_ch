"""Step 0 for v6_ch: ClickHouse-only source preflight.

The v6 pipeline must not copy ready v5/PostgreSQL facts. Step 0 only checks
that raw ClickHouse sources and CH-managed manual reference tables exist before
downstream steps rebuild derived marts.
"""

from __future__ import annotations

import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_settings import RAW_SOURCE_TABLES
from config.ch_utils import SAFE_QUERY_SETTINGS, table_exists
from step0_sync_local import load_city_tier, load_gsheet_sites_overlay

logger = logging.getLogger("pipeline.step0")


RAW_REQUIRED_EXTRA = {
    "direct_campaigns": "reference_data.direct_campaigns",
    "gsheet_sites": "reference_data.gsheet_sites",
    "telega_in_orders": "raw_data.telega_in_orders",
    "vk_ads_stats_day": "raw_data.vk_ads_stats_day",
    "yandex_direct_korrektirovki": "raw_data.yandex_direct_korrektirovki",
}

# Business-managed inputs kept in ClickHouse; step0 fails loudly if missing, instead of
# silently copying PostgreSQL/v5 tables during the pipeline. Most have no raw_data live
# loader at all — the two reviews tables below are the exception: they ARE pipeline-managed,
# written weekly by step_cron_night/direct_account_reviews/, just not by raw_data.
CH_MANUAL_INPUTS = {
    "local_pixel_config": "ad_analytics.local_pixel_config",
    "local_pixel_price_history": "ad_analytics.local_pixel_price_history",
    "gsheets_crop_targeting_account": "ad_analytics.gsheets_crop_targeting_account",
    "gsheets_crop_targeting_account_leads": "ad_analytics.gsheets_crop_targeting_account_leads",
    "gsheets_crop_targeting_account_pravilo_utm": "ad_analytics.gsheets_crop_targeting_account_pravilo_utm",
    "yandex_direct_cookie_analytics_website_pages": "ad_analytics.yandex_direct_cookie_analytics_website_pages",
    # REVIEWS_CH_NATIVE_2026-08-24: written weekly by step_cron_night/direct_account_reviews/,
    # not raw_data — step3's daily reviews join reads them every run, so a missing/empty table
    # here must fail step0 loudly instead of silently zeroing out reviews in big_analytics_full.
    "yandex_direct_account_reviews": "ad_analytics.yandex_direct_account_reviews",
    "yandex_direct_reports_reviews": "ad_analytics.yandex_direct_reports_reviews",
}


# Weekly cron (0 19 * * 0, see step_cron_night/README.md) + slack for one late/skipped
# week. Non-empty-only checks (CH_MANUAL_INPUTS above) stay green while a job silently
# skipped by the outer `flock -n` (no log/Telegram, unlike pipeline_mutex) rots for weeks —
# BA6 pattern #43. This is the only weekly source, so it gets its own staleness gate.
REVIEWS_MAX_STALE_DAYS = 10


def _check_reviews_freshness(client) -> tuple[int, str | None]:
    """Returns (stale_days, warning). A weekly side source that is 0.12% of
    big_analytics_full must never take down the daily mart (director rework
    2026-08-24: a hard RuntimeError here killed the whole pipeline daily once the
    unstaffed weekly cron let the source age past REVIEWS_MAX_STALE_DAYS). Staleness is
    now a warning surfaced in `details` (logged to data_quality_log, shown in the
    Telegram summary), never a raise. The exists+non-empty gate stays a hard failure —
    that one is handled by `_check_objects` on CH_MANUAL_INPUTS, called before this in
    `run()`, and is correct as-is.

    `maxOrNull` (not `max`) is defence in depth for the same non-nullable-`Date`-column
    trap the 2026-08-24 incident hit in fetch_direct_stats.py: `max()` over an empty
    table returns ClickHouse's type default 1970-01-01, not NULL — with plain `max` an
    empty table would misreport as "~20000 days stale" instead of "has no rows".
    """
    max_date = client.query(
        "SELECT maxOrNull(`Date`) FROM ad_analytics.yandex_direct_reports_reviews",
        settings=SAFE_QUERY_SETTINGS,
    ).result_rows[0][0]
    if max_date is None:
        raise RuntimeError(
            "ClickHouse preflight failed: ad_analytics.yandex_direct_reports_reviews has no rows"
        )
    stale_days = (date.today() - max_date).days
    warning = None
    if stale_days > REVIEWS_MAX_STALE_DAYS:
        warning = (
            f"yandex_direct_reports_reviews stale — max(Date)={max_date} is {stale_days}d old "
            f"(limit {REVIEWS_MAX_STALE_DAYS}d); weekly direct_account_reviews collector "
            "(night step 107) likely skipped or not yet scheduled"
        )
        logger.warning(warning)
    return stale_days, warning


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
    gsheet_sites = load_gsheet_sites_overlay.sync_gsheet_sites_effective(client)

    raw_objects = dict(RAW_SOURCE_TABLES)
    raw_objects.update(RAW_REQUIRED_EXTRA)
    counts = _check_objects(client, raw_objects)
    counts.update(_check_objects(client, CH_MANUAL_INPUTS, allow_empty={"local_pixel_price_history"}))
    reviews_stale_days, reviews_warning = _check_reviews_freshness(client)
    if table_exists(client, "ad_analytics", "gsheet_city_tier"):
        counts["gsheet_city_tier"] = _count_table(client, "ad_analytics.gsheet_city_tier")
    counts.update(gsheet_sites)

    details = ", ".join(f"{name}={rows:,}" for name, rows in sorted(counts.items()))
    details = (
        f"{details}, city_tier_current={city_tier['rows']:,}, "
        f"city_tier_seeded={city_tier['seeded_rows']:,}, reviews_stale_days={reviews_stale_days}"
    )
    if reviews_warning:
        details += f", WARNING={reviews_warning}"
    logger.info("Шаг 0 v6_ch завершён: %s", details)
    return {"rows": sum(counts.values()), "details": details}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())
