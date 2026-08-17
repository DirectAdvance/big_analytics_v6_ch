"""Step 6 for v6_ch: build `big_analytics_full` in ClickHouse.

The v5 step used PostgreSQL CTAS. This v6 implementation keeps the same
pipeline contract, but writes to ClickHouse with shadow tables and daily
INSERT batches so large source tables are never materialized in one statement.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_settings import DATE_FROM
from config.ch_utils import SAFE_QUERY_SETTINGS, column_names, count_rows, day_ranges, q, swap_shadow
from corrections import specialist_correction_expr
from step3_build_sources.step3 import SOURCE_STORE, _domain_specialist_expr, _gs_account_cte, _metric_expr, _weekday_expr

logger = logging.getLogger("pipeline.step6")

SOURCE_TABLES = [
    SOURCE_STORE,
    "big_analytics_calls",
]


def _source_key_expr(alias: str = "s") -> str:
    return (
        f"concat(ifNull(toString({alias}.`Date`), ''), '|', "
        f"ifNull({alias}.domain, ''), '|', ifNull({alias}.`источник`, ''), '|', "
        f"ifNull(toString({alias}.`CampaignId`), ''))"
    )


def _create_full_shadow(client, shadow: str, source_cols: list[str]) -> None:
    cols_sql = ", ".join(q(col) for col in source_cols)
    client.command(f"DROP TABLE IF EXISTS {shadow} SYNC")
    client.command(
        f"""
        CREATE TABLE {shadow}
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(ifNull(`Date`, toDate('2026-01-01')))
        ORDER BY (ifNull(`Date`, toDate('2026-01-01')), ifNull(domain, ''), ifNull(_source_table, ''))
        AS
        SELECT
            {cols_sql},
            CAST(NULL, 'Nullable(String)') AS key_pixel_score
        FROM ad_analytics.{SOURCE_STORE}
        WHERE 0
        """,
        settings=SAFE_QUERY_SETTINGS,
    )


def _create_calls_shadow(client, shadow: str) -> None:
    client.command(f"DROP TABLE IF EXISTS {shadow} SYNC")
    client.command(
        f"""
        CREATE TABLE {shadow}
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(ifNull(`Date`, toDate('2026-01-01')))
        ORDER BY (ifNull(`Date`, toDate('2026-01-01')), ifNull(domain, ''), ifNull(key3, ''))
        AS SELECT * FROM ad_analytics.{SOURCE_STORE} WHERE 0
        """,
        settings=SAFE_QUERY_SETTINGS,
    )


# CROP_CALLS_PARITY_2026-08-06: домены посевного справочника (~19-21 сайтов) — порт v5
# step3.py:2415-2419 `_CROP_DOMAIN_SUBQUERY`. Используется ДВАЖДЫ в `_calls_select`:
#   * crop=False — NOT IN (эти звонки не должны попадать в обычный бакет 'Звонки'/'Контекст');
#   * crop=True  — IN (посевные звонки репейнтятся в 'Посевы_Звонки'/'Комплекс', мирроря v5
#     `_add_crop_calls_sql`, step3.py:2422-2525), а не исчезают из big_analytics_full.
_CROP_DOMAIN_SUBQUERY = """
    SELECT DISTINCT lower(trim(ifNull(`Сайт`, '')))
    FROM ad_analytics.gsheets_crop_targeting_account
    WHERE ifNull(`Сайт`, '') != ''
"""


def _calls_select(lo: str, hi: str, *, crop: bool = False) -> str:
    """Calls branch of `big_analytics_full` for the [lo, hi) day window.

    `crop=False` (обычные звонки) — домен ОБЯЗАН матчиться в `raw_data.gsheet_sites` со
    строгим `direction='Авто'` (BUG1_CALLS_DOMAIN_MATCH_2026-08-06: было `ifNull(gs.direction,
    'Авто')='Авто'` — звонок БЕЗ матча в справочнике проходил фильтр, мирроря-инвертируя v5
    step6.py:239-240 `gs."domain" IS NOT NULL AND gs."direction"='Авто'`) И домен НЕ входит в
    посевной справочник (BUG2_CALLS_CROP_EXCLUDE_2026-08-06, порт v5 step3.py:2512 `NOT IN`).
    `crop=True` (посевные звонки) — домен ОБЯЗАН входить в посевной справочник, без гейта по
    `gs.direction` (v5 `_add_crop_calls_sql` его тоже не ставит — посевной домен главнее).
    """
    metrics = _metric_expr("c.status", "c.reason", "c.source_type", "gs.salon")
    specialist_expr = specialist_correction_expr("c.created_date", "gs.login_key", _domain_specialist_expr("gs"))
    if crop:
        domain_filter = f"lower(trim(ifNull(c.domain, ''))) IN ({_CROP_DOMAIN_SUBQUERY})"
        istochnik_sql = "'Посевы_Звонки'"
        napravlenie_sql = "'Комплекс'"
    else:
        domain_filter = (
            "gs.direction = 'Авто'\n"
            f"  AND lower(trim(ifNull(c.domain, ''))) NOT IN ({_CROP_DOMAIN_SUBQUERY})"
        )
        istochnik_sql = "multiIf(gs.status IN ('SEO', 'SEO Flow'), 'SEO', 'Контекст')"
        napravlenie_sql = "'Комплекс'"
    return f"""
WITH
{_gs_account_cte()},
gs_domain_best AS
(
    SELECT *
    FROM
    (
        SELECT
            cd.domain_key AS domain_key,
            cd.date_val AS match_date,
            gs.domain,
            gs.status,
            gs.directologist,
            gs.site_type,
            gs.template,
            gs.salon,
            gs.city,
            gs.region,
            gs.direction,
            gs.project_manager,
            gs.client_id,
            gs.sales_manager,
            gs.login_key,
            multiIf(
                ifNull(gs.domain, '') = '', 99,
                ifNull(trim(gs.launch_date), '') = '' AND ifNull(trim(gs.block_date), '') = '', 2,
                (ifNull(trim(gs.launch_date), '') = '' OR cd.date_val >= toDate(parseDateTimeBestEffortOrNull(gs.launch_date)))
                    AND (ifNull(trim(gs.block_date), '') = '' OR cd.date_val < toDate(parseDateTimeBestEffortOrNull(gs.block_date))),
                1,
                3
            ) AS match_priority,
            row_number() OVER (
                PARTITION BY cd.domain_key, cd.date_val
                ORDER BY
                    match_priority ASC,
                    ifNull(toDate(parseDateTimeBestEffortOrNull(gs.launch_date)), toDate('1900-01-01')) DESC,
                    ifNull(gs.domain, '') ASC
            ) AS rn
        FROM
        (
            SELECT DISTINCT lower(trim(ifNull(domain, ''))) AS domain_key, created_date AS date_val
            FROM ad_analytics.raw_calls
            WHERE created_date >= toDate('{lo}')
              AND created_date < toDate('{hi}')
              AND lower(trim(ifNull(domain, ''))) != ''
        ) cd
        LEFT JOIN raw_data.gsheet_sites gs
          ON lower(trim(ifNull(gs.domain, ''))) = cd.domain_key
    )
    WHERE rn = 1
)
SELECT
    concat('call|', toString(c.id)) AS key3,
    c.created_date AS `Date`,
    {_weekday_expr("c.created_date")} AS `День недели`,
    toStartOfWeek(c.created_date, 1) AS week_start,
    toInt64(0) AS `CampaignId`,
    CAST('звонки', 'Nullable(String)') AS `CampaignName`,
    toInt64(0) AS `AdGroupId`,
    CAST('звонки', 'Nullable(String)') AS `AdGroupName`,
    CAST('Звонки', 'Nullable(String)') AS `AdNetworkType`,
    CAST('звонки', 'Nullable(String)') AS `Device`,
    toDecimal64(0, 6) AS `Impressions`,
    toDecimal64(0, 6) AS `Clicks`,
    toDecimal64(0, 6) AS total_cost,
    c.domain AS domain,
    toInt64(0) AS `RlAdjustmentId`,
    '' AS `RlAdjustmentId_total`,
    CAST('звонки', 'Nullable(String)') AS campaign_code,
    'звонки' AS tp,
    'звонки' AS cpc_cpa,
    'звонки' AS site_quiz,
    CAST(NULL, 'Nullable(String)') AS adgroup_code,
    ifNull(gs.login_key, '') AS account_login,
    gs.directologist AS manager_login,
    '' AS ag_part1,
    '' AS ag_part2,
    '' AS ag_part3,
    '' AS ag_part4,
    '' AS ag_part5,
    '' AS ag_part6,
    '' AS ag_part7,
    '' AS `марки авто`,
    crm.crm_name AS `Название crm`,
    CAST('Звонки', 'Nullable(String)') AS `тип_заявки`,
    {metrics},
    gs.status AS `статус`,
    {specialist_expr} AS `специалист`,
    gs.site_type AS `тип_сайта`,
    gs.template AS `шаблон`,
    gs.salon AS `салон`,
    gs.city AS `город`,
    gs.region AS `регион`,
    gs.direction AS direction,
    CAST(NULL, 'Nullable(String)') AS `неверный_кодер_new`,
    CAST(NULL, 'Nullable(String)') AS fid,
    gs.project_manager AS `проджект`,
    gs.client_id AS `id_салона`,
    gs.sales_manager AS `менеджер`,
    {istochnik_sql} AS `источник`,
    {napravlenie_sql} AS `направление`,
    '' AS `номер кампании | название кампании`,
    '' AS `номер группы | название группы`,
    CAST(NULL, 'Nullable(Int32)') AS `План заявки`,
    CAST(NULL, 'Nullable(Int32)') AS `План приезда`,
    concat(ifNull(gs.login_key, ''), '|', ifNull(c.domain, '')) AS `аккаунт|сайт`,
    CAST(NULL, 'Nullable(Int64)') AS priezd_arrival_date,
    CAST(NULL, 'Nullable(Int64)') AS prodazhi_arrival_date,
    'звонки' AS `поставщик`,
    'calls' AS _source_table,
    CAST(NULL, 'Nullable(String)') AS cascade_level,
    CAST(NULL, 'Nullable(String)') AS campaign_status,
    CAST(NULL, 'Nullable(String)') AS payment_model
FROM ad_analytics.raw_calls c
LEFT JOIN gs_domain_best gs ON gs.domain_key = lower(trim(ifNull(c.domain, ''))) AND gs.match_date = c.created_date
LEFT JOIN crm_by_domain crm ON crm.domain_key = lower(trim(ifNull(c.domain, '')))
WHERE c.created_date >= toDate('{lo}')
  AND c.created_date < toDate('{hi}')
  AND ({domain_filter})
"""


def _rebuild_calls(client) -> int:
    shadow = "ad_analytics.big_analytics_calls_new"
    _create_calls_shadow(client, shadow)
    ranges = day_ranges(DATE_FROM)
    for idx, (lo, hi) in enumerate(ranges, start=1):
        client.command(f"INSERT INTO {shadow}\n{_calls_select(lo, hi, crop=False)}", settings=SAFE_QUERY_SETTINGS)
        # CROP_CALLS_PARITY_2026-08-06: посевные звонки — отдельный проход тем же батчем,
        # чтобы не пропасть из big_analytics_full (см. docstring _calls_select).
        client.command(f"INSERT INTO {shadow}\n{_calls_select(lo, hi, crop=True)}", settings=SAFE_QUERY_SETTINGS)
        logger.info("  calls daily batch %d/%d inserted: %s -> %s", idx, len(ranges), lo, hi)
    swap_shadow(client, "ad_analytics.big_analytics_calls", shadow)
    return count_rows(client, "ad_analytics.big_analytics_calls")


def _insert_source_batches(client, shadow: str, source_table: str, base_cols: list[str], extra_where: str = "1 = 1") -> int:
    ranges = day_ranges(DATE_FROM)
    target_cols = ", ".join(q(col) for col in [*base_cols, "key_pixel_score"])
    select_cols = ", ".join(f"s.{q(col)}" for col in base_cols)
    for idx, (lo, hi) in enumerate(ranges, start=1):
        client.command(
            f"""
            INSERT INTO {shadow} ({target_cols})
            SELECT
                {select_cols},
                {_source_key_expr("s")} AS key_pixel_score
            FROM ad_analytics.{source_table} AS s
            WHERE s.`Date` >= toDate('{lo}') AND s.`Date` < toDate('{hi}')
              AND {extra_where}
            """,
            settings=SAFE_QUERY_SETTINGS,
        )
        logger.info("  full %s daily batch %d/%d inserted: %s -> %s", source_table, idx, len(ranges), lo, hi)
    return 0


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    logger.info("Шаг 6 v6_ch: сборка big_analytics_full ClickHouse батчами")
    client = get_client()
    t0 = time.perf_counter()

    calls_rows = _rebuild_calls(client)
    base_cols = column_names(client, "ad_analytics", SOURCE_STORE)
    shadow = "ad_analytics.big_analytics_full_new"
    _create_full_shadow(client, shadow, base_cols)

    for source_table in SOURCE_TABLES:
        extra_where = "s._source_table != 'pixel'" if source_table == SOURCE_STORE else "1 = 1"
        _insert_source_batches(client, shadow, source_table, base_cols, extra_where)

    swap_shadow(client, "ad_analytics.big_analytics_full", shadow)
    total = count_rows(client, "ad_analytics.big_analytics_full")
    details = f"big_analytics_full={total:,}, calls={calls_rows:,}"
    logger.info("Шаг 6 v6_ch завершён за %.1f сек: %s", time.perf_counter() - t0, details)
    return {"rows": total, "details": details}


def get_explain_sql(conn=None) -> str:  # noqa: ARG001
    return "SELECT count() FROM ad_analytics.big_analytics_full"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())
