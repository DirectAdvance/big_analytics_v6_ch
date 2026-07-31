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
    return int(client.query(f"SELECT count() FROM {table}").result_rows[0][0])


def _pg_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _sync_cost_table(ch_client, pg_conn, table: str, spec: dict) -> int:
    columns = spec["columns"]
    shadow = f"{COST_DB}.{table}__new"
    target = f"{COST_DB}.{table}"

    ch_client.command(f"DROP TABLE IF EXISTS {shadow} SYNC")
    ch_client.command(f"CREATE TABLE {shadow} {spec['ddl']}")

    pg_cols = ", ".join(_pg_ident(col) for col in columns)
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

    exists = bool(
        ch_client.query(
            """
            SELECT count()
            FROM system.tables
            WHERE database={db:String} AND name={table:String}
            """,
            parameters={"db": COST_DB, "table": table},
        ).result_rows[0][0]
    )
    if exists:
        ch_client.command(f"EXCHANGE TABLES {target} AND {shadow}")
        ch_client.command(f"DROP TABLE IF EXISTS {shadow} SYNC")
    else:
        ch_client.command(f"RENAME TABLE {shadow} TO {target}")

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
