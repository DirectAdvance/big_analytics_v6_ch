"""Step 0 for v6_ch: validate raw_data and sync small v5 cost tables.

Most source data already lives in ClickHouse `raw_data`. A few business-cost
tables are still maintained by the v5 PostgreSQL pipeline, so v6 copies them
into ClickHouse before downstream steps calculate pixel/crop/VK costs.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client
from config.ch_settings import CH_RAW_DB, CH_WORK_DB, RAW_SOURCE_TABLES
from config.ch_utils import SAFE_QUERY_SETTINGS
from config.settings import DB_DST

logger = logging.getLogger("pipeline.step0")

COST_DB = CH_WORK_DB


COST_TABLES = {
    "local_pixel_config": {
        "columns": ["id", "salon", "project", "cost_per_lead", "cost_total", "pixel_name"],
        "ddl": """
            (
                id Int32,
                salon Nullable(String),
                project Nullable(String),
                cost_per_lead Nullable(Decimal(18, 6)),
                cost_total Nullable(Decimal(18, 6)),
                pixel_name String
            )
            ENGINE = MergeTree
            ORDER BY pixel_name
        """,
    },
    "local_pixel_price_history": {
        "columns": [
            "id",
            "pixel_name",
            "salon",
            "project",
            "cost_per_lead",
            "cost_total",
            "valid_from",
            "valid_to",
            "changed_at",
            "changed_by",
            "note",
        ],
        "ddl": """
            (
                id Int32,
                pixel_name String,
                salon Nullable(String),
                project Nullable(String),
                cost_per_lead Nullable(Decimal(18, 6)),
                cost_total Nullable(Decimal(18, 6)),
                valid_from Date,
                valid_to Nullable(Date),
                changed_at Nullable(DateTime),
                changed_by Nullable(String),
                note Nullable(String)
            )
            ENGINE = MergeTree
            ORDER BY (pixel_name, valid_from)
        """,
    },
    "gsheets_crop_targeting_account_leads": {
        "columns": [
            "id",
            "Специалист",
            "Дата",
            "Гео",
            "Гео2",
            "Сайт",
            "Тип закупа",
            "utm утвержденная",
            "Источник",
            "Канал",
            "сумма входящего ндс",
            "Цена продажи клиенту с НДС, руб.",
            "total_cost",
            "kol_vo_zayavok",
            "korr",
            "kval",
            "priezd",
            "prodazhi",
            "nekorr",
            "ne_otvechaet",
            "filtr",
            "nedozvon",
            "priedet",
            "dohod_do_kredita",
            "dobro",
        ],
        "ddl": """
            (
                id Int32,
                `Специалист` Nullable(String),
                `Дата` Nullable(String),
                `Гео` Nullable(String),
                `Гео2` Nullable(String),
                `Сайт` Nullable(String),
                `Тип закупа` Nullable(String),
                `utm утвержденная` Nullable(String),
                `Источник` Nullable(String),
                `Канал` Nullable(String),
                `сумма входящего ндс` Nullable(String),
                `Цена продажи клиенту с НДС, руб.` Nullable(String),
                total_cost Nullable(String),
                kol_vo_zayavok Nullable(Int64),
                korr Nullable(Int64),
                kval Nullable(Int64),
                priezd Nullable(Int64),
                prodazhi Nullable(Int64),
                nekorr Nullable(Int64),
                ne_otvechaet Nullable(Int64),
                filtr Nullable(Int64),
                nedozvon Nullable(Int64),
                priedet Nullable(Int64),
                dohod_do_kredita Nullable(Int64),
                dobro Nullable(Int64)
            )
            ENGINE = MergeTree
            ORDER BY id
        """,
    },
    "gsheets_crop_targeting_account": {
        "columns": [
            "id",
            "Специалист",
            "Дата",
            "Гео",
            "Гео2",
            "Сайт",
            "Тип закупа",
            "utm утвержденная",
            "Источник",
            "Канал",
            "НДС",
            "Цена закупа без ндс",
            "сумма входящего ндс",
            "Проценты ак",
            "Цена продажи клиенту с НДС, руб.",
            "Наша комиссия с НДС, руб.",
            "Наша чистая комиссия (без затрат н",
            "total_cost",
        ],
        "ddl": """
            (
                id Int32,
                `Специалист` Nullable(String),
                `Дата` Nullable(String),
                `Гео` Nullable(String),
                `Гео2` Nullable(String),
                `Сайт` Nullable(String),
                `Тип закупа` Nullable(String),
                `utm утвержденная` Nullable(String),
                `Источник` Nullable(String),
                `Канал` Nullable(String),
                `НДС` Nullable(String),
                `Цена закупа без ндс` Nullable(String),
                `сумма входящего ндс` Nullable(String),
                `Проценты ак` Nullable(String),
                `Цена продажи клиенту с НДС, руб.` Nullable(String),
                `Наша комиссия с НДС, руб.` Nullable(String),
                `Наша чистая комиссия (без затрат н` Nullable(String),
                total_cost Nullable(String)
            )
            ENGINE = MergeTree
            ORDER BY id
        """,
    },
    "gsheets_crop_targeting_account_pravilo_utm": {
        "columns": ["id", "UTM", "utm утвержденная"],
        "ddl": """
            (
                id Int32,
                UTM Nullable(String),
                `utm утвержденная` Nullable(String)
            )
            ENGINE = MergeTree
            ORDER BY id
        """,
    },
    "crop_targeting_api_telegain_lead": {
        "columns": [
            "id",
            "Date",
            "total_cost",
            "CampaignName",
            "domain",
            "салон",
            "город",
            "источник",
            "поставщик",
            "специалист",
            "статус",
            "тип_сайта",
            "шаблон",
            "регион",
            "direction",
            "kol_vo_zayavok",
            "korr",
            "kval",
            "priezd",
            "prodazhi",
            "nekorr",
            "ne_otvechaet",
            "filtr",
            "nedozvon",
            "priedet",
            "dohod_do_kredita",
            "dobro",
            "utm_campaign",
        ],
        "ddl": """
            (
                id Int32,
                `Date` Nullable(Date),
                total_cost Nullable(Decimal(18, 6)),
                CampaignName Nullable(String),
                domain Nullable(String),
                `салон` Nullable(String),
                `город` Nullable(String),
                `источник` Nullable(String),
                `поставщик` Nullable(String),
                `специалист` Nullable(String),
                `статус` Nullable(String),
                `тип_сайта` Nullable(String),
                `шаблон` Nullable(String),
                `регион` Nullable(String),
                direction Nullable(String),
                kol_vo_zayavok Nullable(Int64),
                korr Nullable(Int64),
                kval Nullable(Int64),
                priezd Nullable(Int64),
                prodazhi Nullable(Int64),
                nekorr Nullable(Int64),
                ne_otvechaet Nullable(Int64),
                filtr Nullable(Int64),
                nedozvon Nullable(Int64),
                priedet Nullable(Int64),
                dohod_do_kredita Nullable(Int64),
                dobro Nullable(Int64),
                utm_campaign Nullable(String)
            )
            ENGINE = MergeTree
            ORDER BY id
        """,
    },
    "local_telega_in_orders": {
        "columns": [
            "id",
            "uid",
            "order_id",
            "order_project_name",
            "order_comment",
            "channel_id",
            "channel_name",
            "channel_link",
            "post_link",
            "placement_format",
            "status",
            "cancel_comment",
            "price",
            "total_price",
            "total_views",
            "clicks",
            "post_links",
            "utm_source",
            "utm_medium",
            "utm_campaign",
            "utm_content",
            "utm_term",
            "created_at",
            "completed_at",
            "done_at",
            "run_at",
            "raw",
            "updated_at",
        ],
        "select_exprs": {"raw": '"raw"::text'},
        "ddl": """
            (
                id Int64,
                uid Nullable(String),
                order_id Nullable(Int64),
                order_project_name Nullable(String),
                order_comment Nullable(String),
                channel_id Nullable(Int64),
                channel_name Nullable(String),
                channel_link Nullable(String),
                post_link Nullable(String),
                placement_format Nullable(String),
                status Nullable(String),
                cancel_comment Nullable(String),
                price Nullable(Decimal(18, 6)),
                total_price Nullable(Decimal(18, 6)),
                total_views Nullable(Int64),
                clicks Nullable(Int64),
                post_links Nullable(String),
                utm_source Nullable(String),
                utm_medium Nullable(String),
                utm_campaign Nullable(String),
                utm_content Nullable(String),
                utm_term Nullable(String),
                created_at Nullable(DateTime),
                completed_at Nullable(DateTime),
                done_at Nullable(DateTime),
                run_at Nullable(DateTime),
                raw Nullable(String),
                updated_at Nullable(DateTime)
            )
            ENGINE = MergeTree
            ORDER BY id
        """,
    },
    "local_telega_in_orders_errors": {
        "columns": [
            "id",
            "order_id",
            "order_project_name",
            "post_links",
            "status",
            "utm_source",
            "utm_medium",
            "utm_campaign",
            "utm_content",
            "utm_content_norm",
            "effective_domain",
            "site_status",
            "directologist",
            "salon",
            "city",
            "region",
            "total_price",
            "created_at",
            "error_type",
            "error_detail",
            "checked_at",
        ],
        "ddl": """
            (
                id Nullable(Int64),
                order_id Nullable(Int64),
                order_project_name Nullable(String),
                post_links Nullable(String),
                status Nullable(String),
                utm_source Nullable(String),
                utm_medium Nullable(String),
                utm_campaign Nullable(String),
                utm_content Nullable(String),
                utm_content_norm Nullable(String),
                effective_domain Nullable(String),
                site_status Nullable(String),
                directologist Nullable(String),
                salon Nullable(String),
                city Nullable(String),
                region Nullable(String),
                total_price Nullable(Decimal(18, 6)),
                created_at Nullable(DateTime),
                error_type Nullable(String),
                error_detail Nullable(String),
                checked_at Nullable(DateTime)
            )
            ENGINE = MergeTree
            ORDER BY (ifNull(id, 0), ifNull(order_id, 0), ifNull(error_type, ''))
        """,
    },
    "yandex_direct_cookie_analytics_website_pages": {
        "columns": [
            "id",
            "login_key",
            "domain",
            "banner_href",
            "date_from",
            "date_to",
            "sum",
            "clicks",
            "agoalnum",
            "aconv",
            "agoalcost",
            "goal_all_forms",
            "goal_crm_order_created",
            "goal_crm_order_paid",
            "final_url",
            "directologist",
            "template",
            "salon",
            "city",
            "region",
            "site_type",
            "page_type",
            "loaded_at",
        ],
        "select_exprs": {"agoalnum": '"agoalnum"::text'},
        "ddl": """
            (
                id Int64,
                login_key String,
                domain Nullable(String),
                banner_href String,
                date_from Date,
                date_to Date,
                `sum` Nullable(Decimal(18, 6)),
                clicks Nullable(Decimal(18, 6)),
                agoalnum Nullable(String),
                aconv Nullable(Decimal(18, 6)),
                agoalcost Nullable(Decimal(18, 6)),
                goal_all_forms Nullable(Decimal(18, 6)),
                goal_crm_order_created Nullable(Decimal(18, 6)),
                goal_crm_order_paid Nullable(Decimal(18, 6)),
                final_url Nullable(String),
                directologist Nullable(String),
                template Nullable(String),
                salon Nullable(String),
                city Nullable(String),
                region Nullable(String),
                site_type Nullable(String),
                page_type Nullable(String),
                loaded_at Nullable(DateTime)
            )
            ENGINE = MergeTree
            ORDER BY (login_key, date_from, date_to, id)
        """,
    },
    "local_vk_ads_stats_day": {
        "columns": [
            "date",
            "account_id",
            "account_name",
            "ad_plan_id",
            "ad_plan_name",
            "spent",
            "ad_group_id",
            "ad_group_name",
            "banner_id",
            "banner_name",
            "shows",
            "clicks",
            "ctr",
            "cpm",
        ],
        "ddl": """
            (
                date Date,
                account_id Int64,
                account_name Nullable(String),
                ad_plan_id Int64,
                ad_plan_name Nullable(String),
                spent Decimal(18, 6),
                ad_group_id Nullable(Int64),
                ad_group_name Nullable(String),
                banner_id Nullable(Int64),
                banner_name Nullable(String),
                shows Nullable(Int64),
                clicks Nullable(Int64),
                ctr Nullable(Decimal(18, 6)),
                cpm Nullable(Decimal(18, 6))
            )
            ENGINE = MergeTree
            ORDER BY (date, account_id, ad_plan_id, ifNull(ad_group_id, 0), ifNull(banner_id, 0))
        """,
    },
}


def _count_table(client, table: str) -> int:
    return int(client.query(f"SELECT count() FROM {table}", settings=SAFE_QUERY_SETTINGS).result_rows[0][0])


def _pg_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _pg_select_expr(col: str, spec: dict) -> str:
    expr = spec.get("select_exprs", {}).get(col, _pg_ident(col))
    return f"{expr} AS {_pg_ident(col)}"


def _ch_target_engine(ch_client, table: str) -> str | None:
    rows = ch_client.query(
        """
        SELECT engine
        FROM system.tables
        WHERE database={db:String} AND name={table:String}
        """,
        parameters={"db": COST_DB, "table": table},
        settings=SAFE_QUERY_SETTINGS,
    ).result_rows
    return rows[0][0] if rows else None


def _sync_cost_table(ch_client, pg_conn, table: str, spec: dict) -> int:
    columns = spec["columns"]
    shadow = f"{COST_DB}.{table}__new"
    target = f"{COST_DB}.{table}"

    ch_client.command(f"DROP TABLE IF EXISTS {shadow} SYNC", settings=SAFE_QUERY_SETTINGS)
    ch_client.command(f"CREATE TABLE {shadow} {spec['ddl']}", settings=SAFE_QUERY_SETTINGS)

    pg_cols = ", ".join(_pg_select_expr(col, spec) for col in columns)
    select_sql = f"SELECT {pg_cols} FROM public.{_pg_ident(table)} ORDER BY 1"
    rows_inserted = 0

    with pg_conn.cursor(name=f"ch_sync_{table}") as cur:
        cur.itersize = 20_000
        cur.execute(select_sql)
        while True:
            batch = cur.fetchmany(20_000)
            if not batch:
                break
            ch_client.insert(shadow, batch, column_names=columns)
            rows_inserted += len(batch)

    engine = _ch_target_engine(ch_client, table)
    if engine and engine != "View":
        ch_client.command(f"EXCHANGE TABLES {target} AND {shadow}", settings=SAFE_QUERY_SETTINGS)
        ch_client.command(f"DROP TABLE IF EXISTS {shadow} SYNC", settings=SAFE_QUERY_SETTINGS)
    elif engine:
        ch_client.command(f"DROP TABLE IF EXISTS {target} SYNC", settings=SAFE_QUERY_SETTINGS)
        ch_client.command(f"RENAME TABLE {shadow} TO {target}", settings=SAFE_QUERY_SETTINGS)
    else:
        ch_client.command(f"RENAME TABLE {shadow} TO {target}", settings=SAFE_QUERY_SETTINGS)

    return rows_inserted


def _sync_cost_tables(ch_client) -> dict[str, int]:
    counts: dict[str, int] = {}
    with psycopg2.connect(**DB_DST) as pg_conn:
        for table, spec in COST_TABLES.items():
            logger.info("  sync public.%s -> %s.%s", table, COST_DB, table)
            counts[table] = _sync_cost_table(ch_client, pg_conn, table, spec)
    return counts


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    """Verify required raw_data tables and sync small cost tables."""
    logger.info("Шаг 0 v6_ch: проверка raw_data и sync cost-таблиц в ClickHouse")
    client = get_client(CH_RAW_DB)

    counts: dict[str, int] = {}
    missing: list[str] = []
    empty: list[str] = []

    existing = {
        row[0]
        for row in client.query(
            "SELECT name FROM system.tables WHERE database = {db:String}",
            parameters={"db": CH_RAW_DB},
        ).result_rows
    }

    for logical_name, qualified in RAW_SOURCE_TABLES.items():
        table_name = qualified.split(".", 1)[1]
        if table_name not in existing:
            missing.append(qualified)
            continue
        rows = _count_table(client, qualified)
        counts[logical_name] = rows
        if rows == 0:
            empty.append(qualified)

    if missing or empty:
        problems = []
        if missing:
            problems.append(f"missing={', '.join(missing)}")
        if empty:
            problems.append(f"empty={', '.join(empty)}")
        raise RuntimeError("raw_data preflight failed: " + "; ".join(problems))

    cost_counts = _sync_cost_tables(client)
    counts.update(cost_counts)

    details = ", ".join(f"{name}={rows:,}" for name, rows in sorted(counts.items()))
    logger.info("Шаг 0 v6_ch завершён: %s", details)
    return {"rows": sum(counts.values()), "details": details}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())
