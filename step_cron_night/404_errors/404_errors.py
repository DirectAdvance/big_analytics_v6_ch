"""v6_ch 404 errors builder from raw_data.metrika_yandex_not_found_daily."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from step_cron_night.metrika_raw_builders import build_404_errors


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    rows = build_404_errors()
    return {"rows": rows, "details": f"yandex_direct_404_errors={rows:,}"}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())


if __name__ == "__main__":
    main()
