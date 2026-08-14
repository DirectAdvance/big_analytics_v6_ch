"""Night rebuild of ClickHouse fact_ml_korrektirovki."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client  # noqa: E402
from star_refactor.build_star import build_ml_korrektirovki_fact  # noqa: E402

logger = logging.getLogger("pipeline.build_ml_korrektirovki_night")


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    client = get_client()
    t0 = time.perf_counter()
    rows = build_ml_korrektirovki_fact(client)
    details = f"fact_ml_korrektirovki={rows:,}"
    logger.info("build_ml_korrektirovki_night v6_ch завершён за %.1f сек: %s", time.perf_counter() - t0, details)
    return {"rows": rows, "details": details}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())


if __name__ == "__main__":
    main()
