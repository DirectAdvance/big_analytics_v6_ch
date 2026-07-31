"""v6_ch metrika_yandex builder from ClickHouse raw_data.

The v5 script called Metrika API for counters/goals/grants. In v6 the raw
Metrika snapshots already exist in ClickHouse, so this step only materializes
the compatibility table used by Direct Reports/feed builders.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from step_cron_night.metrika_raw_builders import build_metrika_yandex


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    rows = build_metrika_yandex()
    return {"rows": rows, "details": f"metrika_yandex={rows:,}"}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())


if __name__ == "__main__":
    main()
