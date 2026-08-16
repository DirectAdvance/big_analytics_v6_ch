#!/usr/bin/env python3
"""RAW_NEW_2026-08-16 — перенос источников БА5 из PostgreSQL в ClickHouse `ad_analytics`.

Восемь вкладок БА6 не собирались, потому что их источники живут только в PostgreSQL и в
`raw_data` их никто не грузит. До появления штатной загрузки (docs/DIRECT_RAW_HANDOVER.md,
поток 4) кладём разовые копии рядом с рабочими таблицами под префиксом `raw_new_`.

Префикс — не косметика: он отделяет «привезли руками, обновляться само не будет» от того,
что наполняет пайплайн. Как только источник появится в `raw_data`, копия удаляется.

Запускать НА VICTORY: там PostgreSQL локальный, а гнать 15 млн строк через мак — это
лишний интернет-хоп в обе стороны.

    ssh victory 'cd ~/big_analytics_v6_ch && nohup ~/venv-v6/bin/python3 \
        tools/copy_pg_to_raw_new.py > /tmp/raw_new_copy.log 2>&1 &'
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
for _p in BASE.parents:
    if (_p / ".secret" / "loader.py").exists():
        sys.path.insert(0, str(_p / ".secret"))
        break

import psycopg2  # noqa: E402
from loader import load_db  # noqa: E402

from config.ch_db import get_client  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("raw_new")

TARGET_DB = "ad_analytics"
PREFIX = "raw_new_"
BATCH = 50_000

# (схема PG, объект PG, имя в ClickHouse без префикса) — берём именно те объекты, на которые
# ссылается модель БА5: это могут быть view, тогда переносится их результат.
SOURCES = [
    ("public", "arp_fact", "arp_fact"),
    ("yandex_direct_raw", "yandex_direct_ads_texts_master_pbi", "ads_texts_master_pbi"),
    ("yandex_direct_raw", "yandex_direct_type_placement_report_master", "type_placement_report_master"),
    ("yandex_direct_raw", "yandex_direct_type_placement_types", "type_placement_types"),
    ("yandex_direct_raw", "yandex_direct_search_query_report_master_pbi", "search_query_report_master_pbi"),
    ("yandex_direct_raw", "yandex_direct_tp_placement_links", "tp_placement_links"),
    ("victoryads_direct_automation", "yandex_direct_accounts_human_cyborgs", "human_cyborgs"),
]

# Деньги и доли держим в Decimal: приводить дробную атрибуцию к float запрещено правилами проекта.
TYPES = {
    "bigint": "Int64", "integer": "Int32", "smallint": "Int16",
    "numeric": "Decimal(38, 9)", "money": "Decimal(38, 9)",
    "double precision": "Float64", "real": "Float32",
    "boolean": "Bool",
    "date": "Date32",                       # Date в CH обрезан 1970-2149, Date32 шире
    "timestamp without time zone": "DateTime64(6)",
    "timestamp with time zone": "DateTime64(6, 'UTC')",
}


def ch_type(pg_type: str, nullable: bool) -> str:
    base = TYPES.get(pg_type, "String")
    return f"Nullable({base})" if nullable else base


def columns(cur, schema: str, obj: str):
    cur.execute(
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
        (schema, obj),
    )
    return [(n, t, yn == "YES") for n, t, yn in cur.fetchall()]


def copy_one(pg, ch, schema: str, obj: str, short: str) -> int:
    target = f"{PREFIX}{short}"
    fq = f"{TARGET_DB}.`{target}`"
    with pg.cursor() as cur:
        cols = columns(cur, schema, obj)
        cur.execute(f'SELECT count(*) FROM "{schema}"."{obj}"')
        total = cur.fetchone()[0]
    if not cols:
        raise RuntimeError(f"{schema}.{obj}: не нашёл колонок")

    ddl_cols = ",\n  ".join(f"`{n}` {ch_type(t, nul)}" for n, t, nul in cols)
    ch.command(f"DROP TABLE IF EXISTS {fq}")
    ch.command(f"CREATE TABLE {fq} (\n  {ddl_cols}\n) ENGINE = MergeTree ORDER BY tuple()")
    log.info("%s → %s: %s колонок, ожидаем %s строк", obj, target, len(cols), f"{total:,}")

    names = [n for n, _t, _nul in cols]
    sent = 0
    started = time.time()
    # Серверный курсор: 5-7 млн строк целиком в память Victory не тянем.
    with pg.cursor(name=f"cp_{short}") as cur:
        cur.itersize = BATCH
        cur.execute(f'SELECT {", ".join(chr(34) + n + chr(34) for n in names)} FROM "{schema}"."{obj}"')
        while True:
            rows = cur.fetchmany(BATCH)
            if not rows:
                break
            ch.insert(target, [list(r) for r in rows], column_names=names, database=TARGET_DB)
            sent += len(rows)
            log.info("  %s: %s / %s (%.0f%%)", target, f"{sent:,}", f"{total:,}", 100 * sent / max(total, 1))

    got = ch.query(f"SELECT count() FROM {fq}").result_rows[0][0]
    log.info("%s: готово за %.0f сек, в ClickHouse %s строк", target, time.time() - started, f"{got:,}")
    if got != total:
        raise RuntimeError(f"{target}: перенесено {got}, а в PostgreSQL {total}")
    return got


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="перенести только один объект (короткое имя)")
    args = ap.parse_args()

    db = load_db("victory")
    pg = psycopg2.connect(host=db["host"], port=db["port"], dbname=db["database"],
                          user=db["user"], password=db["password"])
    ch = get_client()

    todo = [s for s in SOURCES if not args.only or s[2] == args.only]
    failed = []
    for schema, obj, short in todo:
        try:
            copy_one(pg, ch, schema, obj, short)
        except Exception as exc:  # noqa: BLE001
            log.exception("%s: ПРОВАЛ", obj)
            failed.append(f"{obj}: {exc}")
    pg.close()

    log.info("=" * 60)
    for schema, obj, short in todo:
        try:
            n = ch.query(f"SELECT count() FROM {TARGET_DB}.`{PREFIX}{short}`").result_rows[0][0]
            log.info("  %s%-34s %s строк", PREFIX, short, f"{n:,}")
        except Exception:  # noqa: BLE001
            log.info("  %s%-34s НЕТ", PREFIX, short)
    if failed:
        for f in failed:
            log.error("ПРОВАЛ %s", f)
        return 1
    log.info("все объекты перенесены")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
