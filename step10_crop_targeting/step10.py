"""Step 10 for v6_ch: crop targeting final check.

v6_ch does not load CRM/API data here. Crop/social rows are derived from the
existing ClickHouse `raw_data` snapshot in step3 and are inserted into full in
step6.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_utils import count_rows, table_exists

logger = logging.getLogger("pipeline.step10")


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    logger.info("Шаг 10 v6_ch: проверка посевов ClickHouse")
    client = get_client()
    t0 = time.perf_counter()
    if not table_exists(client, "ad_analytics", "big_analytics_crop_targeting"):
        raise RuntimeError("ad_analytics.big_analytics_crop_targeting отсутствует")
    crop_rows = count_rows(client, "ad_analytics.big_analytics_crop_targeting")
    full_crop_rows = 0
    if table_exists(client, "ad_analytics", "big_analytics_full"):
        full_crop_rows = int(
            client.query(
                """
                SELECT count()
                FROM ad_analytics.big_analytics_full
                WHERE _source_table IN ('crop_targeting', 'crop', 'telegram', 'social_посевы')
                   OR `источник` = 'Посевы'
                """
            ).result_rows[0][0]
        )
    details = f"crop={crop_rows:,}, full_crop_like={full_crop_rows:,}"
    logger.info("Шаг 10 v6_ch завершён за %.1f сек: %s", time.perf_counter() - t0, details)
    return {"rows": crop_rows, "details": details}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())
