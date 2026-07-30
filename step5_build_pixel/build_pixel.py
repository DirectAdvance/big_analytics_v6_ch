"""Step 5 for v6_ch: finalize/check pixel source table."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client

logger = logging.getLogger("pipeline.step5")


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    logger.info("Шаг 5 v6_ch: проверка big_analytics_pixel")
    client = get_client()
    t0 = time.perf_counter()
    exists = bool(
        client.query(
            """
            SELECT count()
            FROM system.tables
            WHERE database='ad_analytics' AND name='big_analytics_pixel'
            """
        ).result_rows[0][0]
    )
    if not exists:
        raise RuntimeError("ad_analytics.big_analytics_pixel отсутствует — сначала запустить step3")
    rows = int(client.query("SELECT count() FROM ad_analytics.big_analytics_pixel").result_rows[0][0])
    logger.info("Шаг 5 v6_ch завершён за %.1f сек: big_analytics_pixel=%d", time.perf_counter() - t0, rows)
    return {"rows": rows, "details": f"big_analytics_pixel={rows:,}"}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())
