"""Build ClickHouse star/Power BI tables for v6_ch."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_utils import column_names, count_rows, month_ranges_from_table, q, swap_shadow, table_exists

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("build_star")


def _create_fact_empty(client, target: str, select_sql: str) -> None:
    client.command(f"DROP TABLE IF EXISTS {target} SYNC")
    client.command(
        f"""
        CREATE TABLE {target}
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(ifNull(`Date`, toDate('2026-01-01')))
        ORDER BY (ifNull(`Date`, toDate('2026-01-01')), `атрибуция`, ifNull(domain, ''))
        AS SELECT {select_sql}
        FROM ad_analytics.big_analytics_unified
        WHERE 0
        """
    )


def build_fact(client) -> int:
    cols = column_names(client, "ad_analytics", "big_analytics_unified")
    alias_exprs = []
    if "status" not in cols:
        alias_exprs.append("`статус` AS status")
    if "project_manager" not in cols:
        alias_exprs.append("`проджект` AS project_manager")
    target_cols = cols + [expr.rsplit(" AS ", 1)[1] for expr in alias_exprs]
    select_sql = ", ".join([q(col) for col in cols] + alias_exprs)
    cols_sql = ", ".join(q(col) for col in target_cols)
    shadow = "ad_analytics.fact_big_analytics_new"
    _create_fact_empty(client, shadow, select_sql)
    ranges = month_ranges_from_table(
        client,
        "ad_analytics.big_analytics_unified",
        "`Date`",
        "`Date` IS NOT NULL",
    )
    for idx, (lo, hi) in enumerate(ranges, start=1):
        before = count_rows(client, shadow)
        client.command(
            f"""
            INSERT INTO {shadow} ({cols_sql})
            SELECT {select_sql}
            FROM ad_analytics.big_analytics_unified
            WHERE `Date` >= toDate('{lo}') AND `Date` < toDate('{hi}')
            """
        )
        after = count_rows(client, shadow)
        log.info("  fact batch %d/%d: +%d rows", idx, len(ranges), after - before)
    swap_shadow(client, "ad_analytics.fact_big_analytics", shadow)
    return count_rows(client, "ad_analytics.fact_big_analytics")


def build_dims(client) -> dict[str, int]:
    ddl = {
        "Dim_Date": """
            CREATE TABLE ad_analytics.Dim_Date_new
            ENGINE = MergeTree
            ORDER BY Date
            AS
            SELECT DISTINCT
                `Date`,
                `День недели`,
                week_start,
                toYear(`Date`) AS year,
                toMonth(`Date`) AS month,
                toYYYYMM(`Date`) AS month_key,
                formatDateTime(`Date`, '%Y-%m') AS year_month,
                toDayOfMonth(`Date`) AS day
            FROM ad_analytics.big_analytics_unified
            WHERE `Date` IS NOT NULL
        """,
        "Dim_Site": """
            CREATE TABLE ad_analytics.Dim_Site_new
            ENGINE = MergeTree
            ORDER BY (ifNull(domain, ''), ifNull(`салон`, ''))
            AS
            SELECT
                domain,
                salon AS `салон`,
                city AS `город`,
                region AS `регион`,
                site_type AS `тип_сайта`,
                template AS `шаблон`,
                direction AS `направление`,
                site_status AS `статус`,
                site_status AS status,
                specialist AS `специалист`,
                project AS `проджект`,
                project AS project_manager,
                salon_id AS `id_салона`,
                manager AS `менеджер`,
                crm_name AS `Название crm`
            FROM
            (
                SELECT
                    domain,
                    anyLast(`салон`) AS salon,
                    anyLast(`город`) AS city,
                    anyLast(`регион`) AS region,
                    anyLast(`тип_сайта`) AS site_type,
                    anyLast(`шаблон`) AS template,
                    anyLast(`направление`) AS direction,
                    anyLast(`статус`) AS site_status,
                    anyLast(`специалист`) AS specialist,
                    anyLast(`проджект`) AS project,
                    anyLast(`id_салона`) AS salon_id,
                    anyLast(`менеджер`) AS manager,
                    anyLast(`Название crm`) AS crm_name
                FROM ad_analytics.big_analytics_unified
                WHERE ifNull(domain, '') != ''
                GROUP BY domain
            )
        """,
        "Dim_Campaign": """
            CREATE TABLE ad_analytics.Dim_Campaign_new
            ENGINE = MergeTree
            ORDER BY ifNull(CampaignId, 0)
            AS
            SELECT
                `CampaignId`,
                anyLast(`CampaignName`) AS `CampaignName`,
                anyLast(account_login) AS account_login,
                anyLast(campaign_code) AS campaign_code,
                anyLast(tp) AS tp,
                anyLast(cpc_cpa) AS cpc_cpa,
                anyLast(site_quiz) AS site_quiz,
                anyLast(campaign_status) AS campaign_status,
                anyLast(payment_model) AS payment_model,
                anyLast(`номер кампании | название кампании`) AS `номер кампании | название кампании`
            FROM ad_analytics.big_analytics_unified
            WHERE `CampaignId` IS NOT NULL
            GROUP BY `CampaignId`
        """,
        "Dim_AdGroup": """
            CREATE TABLE ad_analytics.Dim_AdGroup_new
            ENGINE = MergeTree
            ORDER BY ifNull(AdGroupId, 0)
            AS
            SELECT
                `AdGroupId`,
                anyLast(`AdGroupName`) AS `AdGroupName`,
                anyLast(adgroup_code) AS adgroup_code,
                anyLast(ag_part1) AS ag_part1,
                anyLast(ag_part2) AS ag_part2,
                anyLast(ag_part3) AS ag_part3,
                anyLast(ag_part4) AS ag_part4,
                anyLast(ag_part5) AS ag_part5,
                anyLast(ag_part6) AS ag_part6,
                anyLast(ag_part7) AS ag_part7,
                anyLast(`номер группы | название группы`) AS `номер группы | название группы`
            FROM ad_analytics.big_analytics_unified
            WHERE `AdGroupId` IS NOT NULL
            GROUP BY `AdGroupId`
        """,
    }
    rows: dict[str, int] = {}
    for table, sql in ddl.items():
        shadow = f"ad_analytics.{table}_new"
        client.command(f"DROP TABLE IF EXISTS {shadow} SYNC")
        client.command(sql)
        swap_shadow(client, f"ad_analytics.{table}", shadow)
        rows[table] = count_rows(client, f"ad_analytics.{table}")
        log.info("  %s=%d", table, rows[table])
    return rows


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    log.info("build_star v6_ch: ClickHouse star tables")
    client = get_client()
    t0 = time.perf_counter()
    if not table_exists(client, "ad_analytics", "big_analytics_unified"):
        raise RuntimeError("ad_analytics.big_analytics_unified отсутствует")
    fact_rows = build_fact(client)
    dim_rows = build_dims(client)
    parts = [f"fact_big_analytics={fact_rows:,}", *[f"{k}={v:,}" for k, v in dim_rows.items()]]
    details = ", ".join(parts)
    log.info("build_star v6_ch завершён за %.1f сек: %s", time.perf_counter() - t0, details)
    return {"rows": fact_rows, "details": details}


def main() -> None:
    run()


if __name__ == "__main__":
    main()
