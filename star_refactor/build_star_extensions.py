"""Optional extra star dimensions for the ClickHouse Power BI model.

These builders are intentionally not wired into the default pipeline. They are
for the next PBI remap phase: build dimensions, audit coverage, then remove
duplicated descriptive columns from large facts only after parity checks.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_utils import SAFE_QUERY_SETTINGS, count_rows, q, swap_shadow, table_exists

log = logging.getLogger("build_star_extensions")


def _replace_table(client, name: str, engine_sql: str, select_sql: str) -> int:
    shadow = f"ad_analytics.{name}_new"
    client.command(f"DROP TABLE IF EXISTS {shadow} SYNC")
    client.command(
        f"""
        CREATE TABLE {shadow}
        {engine_sql}
        AS
        {select_sql}
        """,
        settings=SAFE_QUERY_SETTINGS,
    )
    swap_shadow(client, f"ad_analytics.{name}", shadow)
    rows = count_rows(client, f"ad_analytics.{q(name)}")
    log.info("  %s=%d", name, rows)
    return rows


def build_dim_adformat(client) -> int:
    return _replace_table(
        client,
        "Dim_AdFormat",
        "ENGINE = MergeTree ORDER BY ad_format_key",
        """
        SELECT
            ad_format_key,
            anyLast(ad_format) AS ad_format
        FROM
        (
            SELECT
                lowerUTF8(trim(BOTH ' ' FROM ifNull(ad_format, ''))) AS ad_format_key,
                ad_format
            FROM ad_analytics.fact_adformat_spend
            WHERE notEmpty(lowerUTF8(trim(BOTH ' ' FROM ifNull(ad_analytics.fact_adformat_spend.ad_format, ''))))
        )
        GROUP BY ad_format_key
        """,
    )


def build_dim_adnetwork(client) -> int:
    return _replace_table(
        client,
        "Dim_AdNetworkType",
        "ENGINE = MergeTree ORDER BY ad_network_type_key",
        """
        SELECT
            ad_network_type_key,
            anyLast(ad_network_type) AS ad_network_type,
            anyLast(AdNetworkType) AS AdNetworkType
        FROM
        (
            SELECT
                lowerUTF8(trim(BOTH ' ' FROM ifNull(AdNetworkType, ''))) AS ad_network_type_key,
                AdNetworkType AS ad_network_type,
                AdNetworkType
            FROM ad_analytics.big_analytics_unified
            WHERE notEmpty(lowerUTF8(trim(BOTH ' ' FROM ifNull(AdNetworkType, ''))))

            UNION ALL

            SELECT
                ad_network_type_key,
                upperUTF8(ad_network_type_key) AS ad_network_type,
                upperUTF8(ad_network_type_key) AS AdNetworkType
            FROM ad_analytics.fact_region_spend
            WHERE notEmpty(ad_network_type_key)

            UNION ALL

            SELECT
                ad_network_type_key,
                upperUTF8(ad_network_type_key) AS ad_network_type,
                upperUTF8(ad_network_type_key) AS AdNetworkType
            FROM ad_analytics.fact_adformat_spend
            WHERE notEmpty(ad_network_type_key)

            UNION ALL

            SELECT
                ad_network_type_key,
                upperUTF8(ad_network_type_key) AS ad_network_type,
                upperUTF8(ad_network_type_key) AS AdNetworkType
            FROM ad_analytics.fact_criterion_spend
            WHERE notEmpty(ad_network_type_key)
        )
        GROUP BY ad_network_type_key
        """,
    )


def build_dim_device(client) -> int:
    return _replace_table(
        client,
        "Dim_Device",
        "ENGINE = MergeTree ORDER BY device_key",
        """
        SELECT
            device_key,
            anyLast(Device) AS Device
        FROM
        (
            SELECT
                lowerUTF8(trim(BOTH ' ' FROM ifNull(Device, ''))) AS device_key,
                Device
            FROM ad_analytics.big_analytics_unified
            WHERE notEmpty(lowerUTF8(trim(BOTH ' ' FROM ifNull(Device, ''))))
        )
        GROUP BY device_key
        """,
    )


def build_dim_source(client) -> int:
    return _replace_table(
        client,
        "Dim_Source",
        "ENGINE = MergeTree ORDER BY source_key",
        """
        SELECT
            source_key,
            anyLast(`источник`) AS `источник`,
            anyLast(`поставщик`) AS `поставщик`,
            anyLast(_source_table) AS _source_table
        FROM
        (
            SELECT
                lowerUTF8(trim(BOTH ' ' FROM ifNull(`источник`, ''))) AS source_key,
                `источник`,
                `поставщик`,
                _source_table
            FROM ad_analytics.big_analytics_unified
            WHERE notEmpty(lowerUTF8(trim(BOTH ' ' FROM ifNull(`источник`, ''))))
        )
        GROUP BY source_key
        """,
    )


WIDE_SOURCED_DIM_BUILDERS = {
    "Dim_AdNetworkType": build_dim_adnetwork,
    "Dim_Device": build_dim_device,
    "Dim_Source": build_dim_source,
}


def _preserve_existing_dim(client, table: str) -> int:
    if not table_exists(client, "ad_analytics", table):
        raise RuntimeError(f"ad_analytics.{table} отсутствует, а big_analytics_unified уже снят")
    rows = count_rows(client, f"ad_analytics.{q(table)}")
    log.info("  %s preserved rows=%d", table, rows)
    return rows


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    client = get_client()
    t0 = time.perf_counter()
    rows = {"Dim_AdFormat": build_dim_adformat(client)}
    if table_exists(client, "ad_analytics", "big_analytics_unified"):
        for table, builder in WIDE_SOURCED_DIM_BUILDERS.items():
            rows[table] = builder(client)
    else:
        for table in WIDE_SOURCED_DIM_BUILDERS:
            rows[table] = _preserve_existing_dim(client, table)
    details = ", ".join(f"{key}={value:,}" for key, value in rows.items())
    log.info("build_star_extensions завершён за %.1f сек: %s", time.perf_counter() - t0, details)
    return {"rows": sum(rows.values()), "details": details}


if __name__ == "__main__":
    print(run())
