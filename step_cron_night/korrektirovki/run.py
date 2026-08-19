"""v6_ch nightly compatibility view for Yandex Direct bid corrections."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from config.ch_db import get_client  # noqa: E402
from config.ch_utils import SAFE_QUERY_SETTINGS, count_rows, replace_view, table_exists  # noqa: E402

logger = logging.getLogger("pipeline.korrektirovki")


KORREKTIROVKI_SQL = """
WITH gs_login AS
(
    SELECT
        lower(ifNull(login_key, '')) AS login_key,
        anyLast(directologist) AS directologist
    FROM reference_data.gsheet_sites
    WHERE ifNull(login_key, '') != ''
    GROUP BY login_key
)
SELECT
    modifier_id AS id,
    account_login AS ulogin,
    campaign_id,
    campaign_name,
    ad_group_id,
    level,
    modifier_id,
    enabled,
    modifier_type,
    modifier_name,
    bid_percent,
    korrektirovki_bid,
    audience_id,
    gs.directologist AS `специалист`,
    k.campaign_status,
    CAST(NULL, 'Nullable(String)') AS status,
    parseDateTimeBestEffortOrNull(synced_at) AS loaded_at
FROM raw_data.yandex_direct_korrektirovki k
LEFT JOIN gs_login gs ON gs.login_key = lower(ifNull(k.account_login, ''))
"""


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    client = get_client()
    t0 = time.perf_counter()
    if not table_exists(client, "raw_data", "yandex_direct_korrektirovki"):
        raise RuntimeError("raw_data.yandex_direct_korrektirovki отсутствует")
    replace_view(client, "ad_analytics.yandex_direct_korrektirovki", KORREKTIROVKI_SQL)
    rows = count_rows(client, "ad_analytics.yandex_direct_korrektirovki")
    raw_max = client.query(
        """
        SELECT max(parseDateTimeBestEffortOrNull(synced_at))
        FROM raw_data.yandex_direct_korrektirovki
        """,
        settings=SAFE_QUERY_SETTINGS,
    ).result_rows[0][0]
    details = f"yandex_direct_korrektirovki={rows:,}, raw_max_synced_at={raw_max}"
    logger.info("korrektirovki v6_ch завершён за %.1f сек: %s", time.perf_counter() - t0, details)
    return {"rows": rows, "details": details}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())


if __name__ == "__main__":
    main()
