"""Step 1 for v6_ch: build RAW tables in ClickHouse from raw_data.

This replaces the v5 Postgres path (`local_*` -> UNLOGGED raw_*). The v6 raw
tables are rebuilt in `ad_analytics` from the existing `raw_data` schema. Direct
and CRM sources can be overridden by PG-backed local copies when raw_data is
known to be behind or lossy for historical periods.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_settings import DATE_FROM, EXCLUDED_DOMAIN_IDS, RAW_TARGET_TABLES
from config.ch_utils import SAFE_QUERY_SETTINGS, count_rows, day_ranges, replace_view, swap_shadow, table_exists

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


# ══════════════════════════════════════════════════════════════════════════════
# MARCAR_GSHEET_STATUS_2026-08-05 — патч статусов Маркара по гугл-таблице приездов
# ------------------------------------------------------------------------------
# Порт v5 `step0_sync_local/step0.py::_patch_marcar_statuses()` (v5:1228, вызов v5:1741).
# В v5 патч был UPDATE по локальной копии `local_leads_all`; в v6 источник
# `raw_data.leads_all` — реплика CRM, писать в неё нельзя, поэтому патч сдвинут на
# один шаг вниз: он применяется ВЫРАЖЕНИЕМ при сборке `raw_leads` / `raw_calls`.
# Результат для всех последующих шагов идентичен v5 — status у лида Маркара уже
# патченый ещё до step3 (`_step3_leads_deduped`), до дедупликации и до воронки.
#
# Механика: в `raw_data.gsheet_priezdi_marcar` лежит ссылка на карточку CRM
# (`.../crm.marcar.ru/.../<id>`); хвост после последнего `/` == `leads_all.source_record_id`.
# Патч НЕ перезаписывает статус «вниз» по воронке: применяется, только если
# приоритет gsheet-статуса СТРОГО выше приоритета текущего CRM-статуса
# (0 = самый высокий). Статус вне списка = приоритет 9999, т.е. любой из четырёх
# gsheet-статусов его перебивает — ровно как в v5.
#
# ⚠️ Парная половина правки — КАТЕГОРИИ этих статусов. В CH-справочнике нет
# general-ветки (v5 `crm_name='default'`), а прав на запись в `raw_data.*` нет,
# поэтому категории заданы кодом: `step3_build_sources/step3.py::CODE_STATUS_CATEGORY`
# (маркер CODE_STATUS_CATEGORY_2026-08-06). Добавили сюда пятый статус — обязаны
# добавить его туда же, иначе `check_code_status_categories()` уронит шаг 3.
# ══════════════════════════════════════════════════════════════════════════════
MARCAR_SOURCE_TYPE = "marcar_crm_excel"

# Иерархия статусов Маркара (0 = высший). Копия v5 `_MARCAR_STATUS_PRIORITY` (v5 step0.py:1145).
MARCAR_STATUS_PRIORITY: dict[str, int] = {
    "Продажа": 0,
    "Дошел в КО": 1,
    "Одобрение": 2,
    "Приехал": 3,
}


def _marcar_priority_expr(status_expr: str) -> str:
    branches = "".join(f"{status_expr} = '{status}', {prio}, " for status, prio in MARCAR_STATUS_PRIORITY.items())
    return f"multiIf({branches}9999)"


def _marcar_gsheet_subquery() -> str:
    """id карточки Маркара -> лучший (самый глубокий) статус из гугл-таблицы приездов."""
    statuses_in = ", ".join(f"'{status}'" for status in MARCAR_STATUS_PRIORITY)
    return f"""
SELECT
    lead_record_id,
    argMin(status, prio) AS status
FROM
(
    SELECT
        replaceRegexpOne(ifNull(link, ''), '^.+/', '') AS lead_record_id,
        ifNull(status, '') AS status,
        {_marcar_priority_expr("ifNull(status, '')")} AS prio
    FROM raw_data.gsheet_priezdi_marcar
    WHERE ifNull(link, '') LIKE '%crm.marcar.ru%'
      AND match(ifNull(link, ''), '^https?://.+/[0-9]+$')
      AND ifNull(status, '') IN ({statuses_in})
)
GROUP BY lead_record_id
"""


def _marcar_join_sql(lead_alias: str, join_alias: str = "mp") -> str:
    return f"""
LEFT JOIN ({_marcar_gsheet_subquery()}) AS {join_alias}
  ON {join_alias}.lead_record_id = ifNull({lead_alias}.source_record_id, '')
"""


def _marcar_patched_status_expr(lead_alias: str, join_alias: str = "mp") -> str:
    """status лида с применённым патчем Маркара (только «вверх» по воронке)."""
    current = f"ifNull({lead_alias}.status, '')"
    patched = f"ifNull({join_alias}.status, '')"
    return f"""
    if(
        {lead_alias}.source_type = '{MARCAR_SOURCE_TYPE}'
        AND {patched} != ''
        AND {_marcar_priority_expr(patched)} < {_marcar_priority_expr(current)},
        CAST({join_alias}.status, 'Nullable(String)'),
        {lead_alias}.status
    )
"""


def _drop_and_create(client, table: str, create_sql: str) -> None:
    client.command(f"DROP TABLE IF EXISTS {table} SYNC")
    client.command(create_sql, settings=SAFE_QUERY_SETTINGS)


def _command_with_retry(client, sql: str, *, label: str, settings=None, attempts: int = 3):
    for attempt in range(1, attempts + 1):
        try:
            client.command(sql, settings=settings)
            return client
        except Exception as exc:
            if attempt == attempts:
                raise
            logger.warning(
                "    %s failed (%d/%d): %s; reconnecting",
                label,
                attempt,
                attempts,
                exc,
            )
            time.sleep(attempt * 2)
            client = get_client()
    return client


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


def _raw_yandex_sql(raw_date_filter: str = "", source: str = "raw_data.yandex_direct_report_rows") -> str:
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
    FROM {source}
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


def _direct_source_table(client) -> str:
    primary = "raw_data.yandex_direct_report_rows"
    if table_exists(client, "raw_data", "yandex_direct_report_rows"):
        rows = count_rows(client, primary)
        if rows > 0:
            logger.info("  raw Direct source: %s (%d rows)", primary, rows)
            return primary
    fallback = "ad_analytics.local_yandex_pg"
    if table_exists(client, "ad_analytics", "local_yandex_pg"):
        rows = count_rows(client, fallback)
        if rows > 0:
            logger.warning("  raw Direct source fallback: %s (%d rows)", fallback, rows)
            return fallback
    return primary


def _raw_leads_select_sql(source: str = "raw_data.leads_all") -> str:
    excluded = _excluded_domain_list()
    return f"""
SELECT
    -- явный алиас обязателен: с третьим JOIN (mp) анализатор CH называет колонку `l.id`,
    -- и `raw_leads.id` переименовывается — step3 (`ORDER BY … id`) падает
    l.id AS id,
    l.created_date,
    l.arrival_date,
    l.domain_id,
    d.domain AS domain,
    l.deal_type,
    -- MARCAR_GSHEET_STATUS_2026-08-05
    {_marcar_patched_status_expr("l")} AS status,
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
{_marcar_join_sql("l")}
WHERE l.deal_type != 'Звонок'
  AND (l.domain_id NOT IN ({excluded}) OR l.domain_id IS NULL)
"""


def _leads_source_table(client) -> str:
    primary = "raw_data.leads_all"
    if table_exists(client, "raw_data", "leads_all"):
        rows = count_rows(client, primary)
        if rows > 0:
            logger.info("  raw CRM source: %s (%d rows)", primary, rows)
            return primary
    fallback = "ad_analytics.local_leads_all_pg"
    if table_exists(client, "ad_analytics", "local_leads_all_pg"):
        rows = count_rows(client, fallback)
        if rows > 0:
            logger.warning("  raw CRM source fallback: %s (%d rows)", fallback, rows)
            return fallback
    return primary


def _raw_leads_sql(source_filter: str = "", source: str = "raw_data.leads_all") -> str:
    table = RAW_TARGET_TABLES["raw_leads"]
    return f"""
CREATE TABLE {table}
ENGINE = MergeTree
PARTITION BY toYYYYMM(ifNull(created_date, toDate('{DATE_FROM}')))
ORDER BY (ifNull(created_date, toDate('{DATE_FROM}')), ifNull(domain_id, 0), key3)
AS
SELECT * FROM ({_raw_leads_select_sql(source)})
WHERE 1 = 1
  {source_filter}
"""


def _raw_calls_sql(source_filter: str = "", source: str = "raw_data.leads_all") -> str:
    table = RAW_TARGET_TABLES["raw_calls"]
    return f"""
CREATE TABLE {table}
ENGINE = MergeTree
PARTITION BY toYYYYMM(ifNull(created_date, toDate('{DATE_FROM}')))
ORDER BY (ifNull(created_date, toDate('{DATE_FROM}')), ifNull(domain_id, 0), id)
AS
SELECT
    l.id AS id,  -- см. комментарий в _raw_leads_select_sql: алиас держит имя колонки
    l.created_date,
    l.domain_id,
    d.domain AS domain,
    l.deal_type,
    -- MARCAR_GSHEET_STATUS_2026-08-05: в v5 патч был UPDATE по local_leads_all,
    -- т.е. накрывал и звонки (deal_type='Звонок') — здесь то же выражение.
    {_marcar_patched_status_expr("l")} AS status,
    l.reason,
    l.source_type,
    l.phone,
    l.utm_source,
    l.utm_medium,
    l.utm_campaign
FROM {source} AS l
LEFT JOIN raw_data.domains AS d ON d.id = l.domain_id
{_marcar_join_sql("l")}
WHERE l.deal_type = 'Звонок'
  AND l.domain_id IS NOT NULL
  {source_filter}
"""


def _raw_domains_select_sql() -> str:
    return """
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


def _replace_raw_domains_view(client) -> int:
    replace_view(client, RAW_TARGET_TABLES["raw_domains"], _raw_domains_select_sql())
    return count_rows(client, RAW_TARGET_TABLES["raw_domains"])


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


def _rebuild_batched(client, logical_name: str, empty_sql: str, batch_selects: list[str]):
    table = RAW_TARGET_TABLES[logical_name]
    shadow = f"{table}_new"
    client = _command_with_retry(client, f"DROP TABLE IF EXISTS {shadow} SYNC", label=f"{logical_name} shadow drop")
    client = _command_with_retry(
        client,
        empty_sql.replace(table, shadow, 1),
        label=f"{logical_name} shadow create",
        settings=SAFE_QUERY_SETTINGS,
    )
    for idx, select_sql in enumerate(batch_selects, start=1):
        client = _command_with_retry(
            client,
            f"INSERT INTO {shadow}\n{select_sql}",
            label=f"{logical_name} daily batch {idx}/{len(batch_selects)}",
            settings=SAFE_QUERY_SETTINGS,
        )
        logger.info("    %s daily batch %d/%d inserted", logical_name, idx, len(batch_selects))
    swap_shadow(client, table, shadow)
    return client, count_rows(client, table)


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    logger.info("Шаг 1 v6_ch: пересборка RAW таблиц в ClickHouse")
    client = get_client()
    t0 = time.perf_counter()

    details_parts: list[str] = []
    total = 0
    direct_source = _direct_source_table(client)
    leads_source = _leads_source_table(client)

    t_table = time.perf_counter()
    yandex_ranges = day_ranges(DATE_FROM)
    yandex_batches = [
        _select_from_create(
            _raw_yandex_sql(
                f"AND toDate(day) >= toDate('{lo}') AND toDate(day) < toDate('{hi}')",
                source=direct_source,
            )
        )
        for lo, hi in yandex_ranges
    ]
    logger.info("  rebuild %s (%d daily batches)", RAW_TARGET_TABLES["raw_yandex"], len(yandex_batches))
    client, rows = _rebuild_batched(client, "raw_yandex", _raw_yandex_sql("AND 0", source=direct_source), yandex_batches)
    total += rows
    details_parts.append(f"raw_yandex={rows:,}")
    logger.info("  raw_yandex: %d строк за %.1f сек", rows, time.perf_counter() - t_table)

    lead_ranges = day_ranges(DATE_FROM)
    t_table = time.perf_counter()
    lead_batches = [
        _select_from_create(
            _raw_leads_sql(
                f"AND created_date >= toDate('{lo}') AND created_date < toDate('{hi}')",
                source=leads_source,
            )
        )
        for lo, hi in lead_ranges
    ]
    logger.info("  rebuild %s (%d daily batches)", RAW_TARGET_TABLES["raw_leads"], len(lead_batches))
    client, rows = _rebuild_batched(client, "raw_leads", _raw_leads_sql("AND 0", source=leads_source), lead_batches)
    total += rows
    details_parts.append(f"raw_leads={rows:,}")
    logger.info("  raw_leads: %d строк за %.1f сек", rows, time.perf_counter() - t_table)

    t_table = time.perf_counter()
    call_batches = [
        _select_from_create(
            _raw_calls_sql(
                f"AND created_date >= toDate('{lo}') AND created_date < toDate('{hi}')",
                source=leads_source,
            )
        )
        for lo, hi in lead_ranges
    ]
    logger.info("  rebuild %s (%d daily batches)", RAW_TARGET_TABLES["raw_calls"], len(call_batches))
    client, rows = _rebuild_batched(client, "raw_calls", _raw_calls_sql("AND 0", source=leads_source), call_batches)
    total += rows
    details_parts.append(f"raw_calls={rows:,}")
    logger.info("  raw_calls: %d строк за %.1f сек", rows, time.perf_counter() - t_table)

    t_table = time.perf_counter()
    logger.info("  replace %s as VIEW", RAW_TARGET_TABLES["raw_domains"])
    rows = _replace_raw_domains_view(client)
    total += rows
    details_parts.append(f"raw_domains={rows:,}")
    logger.info("  raw_domains view: %d строк за %.1f сек", rows, time.perf_counter() - t_table)

    for logical_name, sql in [
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
