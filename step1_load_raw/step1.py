"""Step 1 for v6_ch: build RAW tables in ClickHouse from raw_data.

This replaces the v5 Postgres path (`local_*` -> UNLOGGED raw_*). The v6 raw
tables are fully rebuilt in `ad_analytics` from the existing `raw_data` schema.
No CRM data is imported here.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_settings import DATE_FROM, EXCLUDED_DOMAIN_IDS, RAW_TARGET_TABLES
from config.ch_utils import SAFE_QUERY_SETTINGS, count_rows, day_ranges, swap_shadow

logger = logging.getLogger("pipeline.step1")

DIRECT_TOTAL_COST_FACTOR_OVERRIDES = (
    {
        "account_login": "porg-kkhtgf2u",
        "date_from": "2026-01-01",
        "date_to": "2026-01-28",
        "multiplier": "10",
        "divisor": "7",
    },
    {
        "account_login": "e-20086619",
        "domain": "samara-buavto.ru",
        "date_from": "2026-03-02",
        "date_to": "2026-03-03",
        "factor": "1.4022552",
        "require_equal_cost": False,
    },
)


def _excluded_domain_list() -> str:
    return ", ".join(str(i) for i in EXCLUDED_DOMAIN_IDS)


def _drop_and_create(client, table: str, create_sql: str) -> None:
    client.command(f"DROP TABLE IF EXISTS {table} SYNC")
    client.command(create_sql, settings=SAFE_QUERY_SETTINGS)


def _total_cost_expr() -> str:
    """Source-level overrides for raw_data rows where corrected total_cost is missing."""
    branches: list[str] = []
    for rule in DIRECT_TOTAL_COST_FACTOR_OVERRIDES:
        domain_condition = ""
        if rule.get("domain"):
            domain_condition = f" AND lower(ifNull(domain, '')) = '{rule['domain']}'"
        source_value = (
            f"toFloat64(ifNull(total_cost, 0)) * {rule['factor']}"
            if rule.get("factor")
            else f"toDecimal64(ifNull(cost, 0), 9) * {rule['multiplier']} / {rule['divisor']}"
        )
        branches.extend(
            [
                (
                    f"client_login = '{rule['account_login']}' "
                    f"AND toDate(day) >= toDate('{rule['date_from']}') "
                    f"AND toDate(day) < toDate('{rule['date_to']}') "
                    f"{domain_condition} "
                    + (
                        "AND ifNull(total_cost, 0) = ifNull(cost, 0)"
                        if rule.get("require_equal_cost", True)
                        else "AND ifNull(total_cost, 0) != 0"
                    )
                ),
                f"toDecimal64({source_value}, 9)",
            ]
        )
    branches.append("toDecimal64(ifNull(total_cost, 0), 9)")
    return f"multiIf({', '.join(branches)})"


def _raw_yandex_sql(raw_date_filter: str = "") -> str:
    table = RAW_TARGET_TABLES["raw_yandex"]
    total_cost_expr = _total_cost_expr()
    return f"""
CREATE TABLE {table}
ENGINE = MergeTree
PARTITION BY toYYYYMM("Date")
ORDER BY ("Date", "CampaignId", key3)
AS
WITH parsed_src AS
(
    SELECT
        cityHash64(row_key) AS id,
        toDate(day) AS "Date",
        campaign_id AS "CampaignId",
        campaign_name AS "CampaignName",
        ifNull(ad_group_id, 0) AS "AdGroupId",
        ad_group_name AS "AdGroupName",
        ad_network_type AS "AdNetworkType",
        device AS "Device",
        toInt64OrZero(ifNull(rl_adjustment_id, '')) AS "RlAdjustmentId",
        ifNull(impressions, 0) AS "Impressions",
        ifNull(clicks, 0) AS "Clicks",
        {total_cost_expr} AS total_cost,
        client_login AS account_login,
        manager_login AS manager_login,
        nullIf(extract(
            splitByString(' — ', ifNull(ad_group_name, ''))[1],
            '(ct\\\\d+_(?:aoff|aon)_n\\\\d+_r\\\\d+_ct\\\\d+_ag\\\\d+_g\\\\d+)'
        ), '') AS adgroup_code,
        nullIf(extract(
            replaceRegexpAll(replaceAll(ifNull(campaign_name, ''), 'с', 'c'), '__+', '_'),
            '(?i)(tp\\\\d+_(?:cpc|cpa)_(?:site|kviz|quiz))'
        ), '') AS campaign_match,
        match(ifNull(campaign_name, ''), '(?i)tp[67]_') AS tp67_check,
        toStartOfWeek(toDate(day), 1) AS week_start
    FROM raw_data.yandex_direct_report_rows
    WHERE toDate(day) >= toDate('{DATE_FROM}')
      AND campaign_id != 0
      {raw_date_filter}
)
SELECT
    id,
    "Date",
    "CampaignId",
    "CampaignName",
    "AdGroupId",
    "AdGroupName",
    "AdNetworkType",
    "Device",
    "RlAdjustmentId",
    "Impressions",
    "Clicks",
    total_cost,
    account_login,
    manager_login,
    adgroup_code,
    campaign_match AS campaign_code,
    lower(splitByChar('_', ifNull(campaign_match, ''))[1]) AS tp,
    lower(splitByChar('_', ifNull(campaign_match, ''))[2]) AS cpc_cpa,
    replaceAll(lower(splitByChar('_', ifNull(campaign_match, ''))[3]), 'kviz', 'quiz') AS site_quiz,
    week_start,
    lower(concat(
        toString("Date"), '|',
        toString("CampaignId"),
        if(tp67_check, '|0', concat('|', toString("AdGroupId"))), '|',
        multiIf(
            lower(ifNull("Device", '')) = 'mobile', 'mobile',
            lower(ifNull("Device", '')) = 'desktop', 'desktop',
            lower(ifNull("Device", '')) = 'tablet', 'tablet',
            lower(ifNull("Device", '')) = 'smart_tv', 'smart_tv',
            '0'
        ), '|',
        toString("RlAdjustmentId")
    )) AS key3
FROM parsed_src
"""


def _raw_leads_select_sql(source: str = "raw_data.leads_all") -> str:
    excluded = _excluded_domain_list()
    return f"""
SELECT
    l.id,
    l.created_date,
    l.arrival_date,
    l.domain_id,
    d.domain AS domain,
    l.deal_type,
    l.status,
    l.source_type,
    l.campaign_id,
    l.group_id,
    l.correction_id,
    l.utm_source,
    l.utm_medium,
    l.utm_campaign,
    l.utm_content,
    l.utm_term,
    l.phone,
    l.yclid,
    l.is_copy_for_removal,
    l.reason,
    l.salon,
    nullIf(extract(ifNull(l.utm_content, ''), 'fid:([^|]+)'), '') AS fid,
    lower(concat(
        ifNull(toString(l.created_date), ''), '|',
        toString(ifNull(l.campaign_id, 0)),
        if(match(ifNull(l.utm_campaign, ''), '(?i)tp[67]'), '|0', concat('|', toString(ifNull(l.group_id, 0)))), '|',
        multiIf(
            position(ifNull(l.utm_content, ''), 'dev:mobile') > 0, 'mobile',
            position(ifNull(l.utm_content, ''), 'dev:desktop') > 0, 'desktop',
            position(ifNull(l.utm_content, ''), 'dev:tablet') > 0, 'tablet',
            position(ifNull(l.utm_content, ''), 'dev:smart_tv') > 0, 'smart_tv',
            '0'
        ), '|',
        toString(ifNull(l.correction_id, 0))
    )) AS key3,
    if(l.arrival_date IS NULL, NULL, lower(concat(
        ifNull(toString(l.arrival_date), ''), '|',
        toString(ifNull(l.campaign_id, 0)),
        if(match(ifNull(l.utm_campaign, ''), '(?i)tp[67]'), '|0', concat('|', toString(ifNull(l.group_id, 0)))), '|',
        multiIf(
            position(ifNull(l.utm_content, ''), 'dev:mobile') > 0, 'mobile',
            position(ifNull(l.utm_content, ''), 'dev:desktop') > 0, 'desktop',
            position(ifNull(l.utm_content, ''), 'dev:tablet') > 0, 'tablet',
            position(ifNull(l.utm_content, ''), 'dev:smart_tv') > 0, 'smart_tv',
            '0'
        ), '|',
        toString(ifNull(l.correction_id, 0))
    ))) AS key3_arrival_date
FROM {source} AS l
LEFT JOIN raw_data.domains AS d ON d.id = l.domain_id
WHERE l.deal_type != 'Звонок'
  AND (l.domain_id NOT IN ({excluded}) OR l.domain_id IS NULL)
"""


def _raw_leads_sql(source_filter: str = "") -> str:
    table = RAW_TARGET_TABLES["raw_leads"]
    return f"""
CREATE TABLE {table}
ENGINE = MergeTree
PARTITION BY toYYYYMM(ifNull(created_date, toDate('{DATE_FROM}')))
ORDER BY (ifNull(created_date, toDate('{DATE_FROM}')), ifNull(domain_id, 0), key3)
AS
SELECT * FROM ({_raw_leads_select_sql()})
WHERE 1 = 1
  {source_filter}
"""


def _raw_calls_sql(source_filter: str = "") -> str:
    table = RAW_TARGET_TABLES["raw_calls"]
    return f"""
CREATE TABLE {table}
ENGINE = MergeTree
PARTITION BY toYYYYMM(ifNull(created_date, toDate('{DATE_FROM}')))
ORDER BY (ifNull(created_date, toDate('{DATE_FROM}')), ifNull(domain_id, 0), id)
AS
SELECT
    l.id,
    l.created_date,
    l.domain_id,
    d.domain AS domain,
    l.deal_type,
    l.status,
    l.reason,
    l.source_type,
    l.phone,
    l.utm_source,
    l.utm_medium,
    l.utm_campaign
FROM raw_data.leads_all AS l
LEFT JOIN raw_data.domains AS d ON d.id = l.domain_id
WHERE l.deal_type = 'Звонок'
  AND l.domain_id IS NOT NULL
  {source_filter}
"""


def _raw_domains_sql() -> str:
    table = RAW_TARGET_TABLES["raw_domains"]
    return f"""
CREATE TABLE {table}
ENGINE = MergeTree
ORDER BY id
AS
SELECT
    id,
    domain AS name,
    domain,
    niche,
    counter_id,
    counter_name,
    metrika_synced_at,
    created_at
FROM raw_data.domains
"""


def _raw_perform_leads_sql() -> str:
    table = RAW_TARGET_TABLES["raw_perform_leads"]
    return f"""
CREATE TABLE {table}
ENGINE = MergeTree
PARTITION BY toYYYYMM(ifNull(created_date, toDate('{DATE_FROM}')))
ORDER BY (ifNull(created_date, toDate('{DATE_FROM}')), ifNull(domain_id, 0), key3)
AS
SELECT *
FROM ({_raw_leads_select_sql()})
WHERE 0
"""


def _select_from_create(create_sql: str) -> str:
    marker = "\nAS\n"
    if marker not in create_sql:
        raise ValueError("CREATE TABLE SQL does not contain expected AS marker")
    return create_sql.split(marker, 1)[1].strip()


def _rebuild_batched(client, logical_name: str, empty_sql: str, batch_selects: list[str]) -> int:
    table = RAW_TARGET_TABLES[logical_name]
    shadow = f"{table}_new"
    client.command(f"DROP TABLE IF EXISTS {shadow} SYNC")
    client.command(empty_sql.replace(table, shadow, 1), settings=SAFE_QUERY_SETTINGS)
    for idx, select_sql in enumerate(batch_selects, start=1):
        client.command(f"INSERT INTO {shadow}\n{select_sql}", settings=SAFE_QUERY_SETTINGS)
        logger.info("    %s daily batch %d/%d inserted", logical_name, idx, len(batch_selects))
    swap_shadow(client, table, shadow)
    return count_rows(client, table)


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    logger.info("Шаг 1 v6_ch: пересборка RAW таблиц в ClickHouse")
    client = get_client()
    t0 = time.perf_counter()

    details_parts: list[str] = []
    total = 0

    t_table = time.perf_counter()
    yandex_ranges = day_ranges(DATE_FROM)
    yandex_batches = [
        _select_from_create(_raw_yandex_sql(f"AND toDate(day) >= toDate('{lo}') AND toDate(day) < toDate('{hi}')"))
        for lo, hi in yandex_ranges
    ]
    logger.info("  rebuild %s (%d daily batches)", RAW_TARGET_TABLES["raw_yandex"], len(yandex_batches))
    rows = _rebuild_batched(client, "raw_yandex", _raw_yandex_sql("AND 0"), yandex_batches)
    total += rows
    details_parts.append(f"raw_yandex={rows:,}")
    logger.info("  raw_yandex: %d строк за %.1f сек", rows, time.perf_counter() - t_table)

    lead_ranges = day_ranges(DATE_FROM)
    t_table = time.perf_counter()
    lead_batches = [
        _select_from_create(_raw_leads_sql(f"AND created_date >= toDate('{lo}') AND created_date < toDate('{hi}')"))
        for lo, hi in lead_ranges
    ]
    logger.info("  rebuild %s (%d daily batches)", RAW_TARGET_TABLES["raw_leads"], len(lead_batches))
    rows = _rebuild_batched(client, "raw_leads", _raw_leads_sql("AND 0"), lead_batches)
    total += rows
    details_parts.append(f"raw_leads={rows:,}")
    logger.info("  raw_leads: %d строк за %.1f сек", rows, time.perf_counter() - t_table)

    t_table = time.perf_counter()
    call_batches = [
        _select_from_create(_raw_calls_sql(f"AND created_date >= toDate('{lo}') AND created_date < toDate('{hi}')"))
        for lo, hi in lead_ranges
    ]
    logger.info("  rebuild %s (%d daily batches)", RAW_TARGET_TABLES["raw_calls"], len(call_batches))
    rows = _rebuild_batched(client, "raw_calls", _raw_calls_sql("AND 0"), call_batches)
    total += rows
    details_parts.append(f"raw_calls={rows:,}")
    logger.info("  raw_calls: %d строк за %.1f сек", rows, time.perf_counter() - t_table)

    for logical_name, sql in [
        ("raw_domains", _raw_domains_sql()),
        ("raw_perform_leads", _raw_perform_leads_sql()),
    ]:
        table = RAW_TARGET_TABLES[logical_name]
        t_table = time.perf_counter()
        logger.info("  rebuild %s", table)
        _drop_and_create(client, table, sql)
        rows = count_rows(client, table)
        total += rows
        details_parts.append(f"{logical_name}={rows:,}")
        logger.info("  %s: %d строк за %.1f сек", logical_name, rows, time.perf_counter() - t_table)

    cost_sum = client.query(
        f"SELECT sum(total_cost) FROM {RAW_TARGET_TABLES['raw_yandex']}",
        settings=SAFE_QUERY_SETTINGS,
    ).result_rows[0][0]
    if not cost_sum:
        raise RuntimeError(
            f"RAW_YANDEX_COST_GUARD: {RAW_TARGET_TABLES['raw_yandex']}.total_cost=0 после загрузки"
        )
    logger.info("RAW_YANDEX_COST_GUARD: SUM(total_cost)=%s — OK", cost_sum)

    details = ", ".join(details_parts)
    logger.info("Шаг 1 v6_ch завершён за %.1f сек: %s", time.perf_counter() - t0, details)
    return {"rows": total, "details": details}


def get_explain_sql(conn=None) -> str:  # noqa: ARG001
    return f"SELECT count() FROM {RAW_TARGET_TABLES['raw_yandex']}"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())
