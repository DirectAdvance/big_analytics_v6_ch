"""Drop transient Direct spend/feed staging after dependent full rebuilds."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from spend.build_direct_spend_staging import cleanup

logger = logging.getLogger("pipeline.direct_spend_staging_cleanup")


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    logger.info("direct_spend_staging cleanup")
    client = get_client()
    t0 = time.perf_counter()
    cleanup(client)
    logger.info("direct_spend_staging cleanup завершён за %.1f сек", time.perf_counter() - t0)
    return {"rows": 0, "details": "direct_spend_staging dropped"}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())
