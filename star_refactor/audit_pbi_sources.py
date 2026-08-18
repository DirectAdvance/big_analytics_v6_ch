"""Metadata-only audit of Power BI source objects in ClickHouse.

The script intentionally avoids `SELECT count() FROM <object>` for PBI objects.
For physical tables it reads row/byte counts from `system.parts`; for views it
reports the engine/query shape and flags them as refresh-risk candidates.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.ch_db import get_client


ROOT = Path(__file__).resolve().parents[1]

PBI_NAME_ALIASES = {
    "big_analytics_full": ["pbi_big_analytics_full", "bi_pbi_big_analytics_full"],
    "analytics_report_placement": ["arp_fact", "bi_arp_fact"],
    "analytics_report_feed": ["arf_fact", "bi_arf_fact", "pbi_import_fact_direct_feed_funnel"],
    "analytics_report_criterion": ["arc_fact", "bi_arc_fact"],
    "direct_history": ["yandex_direct_history", "bi_yandex_direct_history"],
}

STAR_CANDIDATE_ATTRS = {
    "campaign_name",
    "ad_group_name",
    "CampaignName",
    "AdGroupName",
    "номер кампании | название кампании",
    "номер группы | название группы",
    "салон",
    "город",
    "регион",
    "тип_сайта",
    "шаблон",
    "статус",
    "специалист",
    "location",
    "Область",
    "GeoRegionType",
    "distance_km",
    "feed_name",
    "feed_url",
    "placement",
    "criterion",
    "criterion_raw",
}


@dataclass(frozen=True)
class ObjectMeta:
    name: str
    engine: str | None
    rows: int | None
    bytes_on_disk: int | None
    columns: int
    attr_columns: int
    create_query: str
    partition_key: str
    sorting_key: str


def _read_refresh_tables() -> list[str]:
    tree = ast.parse((ROOT / "refresh_powerbi.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "_ALL_TABLES":
                    return [str(v) for v in ast.literal_eval(node.value)]
    raise RuntimeError("_ALL_TABLES not found in refresh_powerbi.py")


def _candidate_names(pbi_name: str) -> list[str]:
    names = []
    names.extend(PBI_NAME_ALIASES.get(pbi_name, []))
    names.append(f"bi_{pbi_name}")
    names.append(pbi_name)
    return list(dict.fromkeys(names))


def _fetch_meta(client) -> dict[str, ObjectMeta]:
    tables = {
        row[0]: (row[1], row[2] or "", row[3] or "", row[4] or "")
        for row in client.query(
            """
            SELECT name, engine, create_table_query, partition_key, sorting_key
            FROM system.tables
            WHERE database = 'ad_analytics'
            """
        ).result_rows
    }
    parts = {
        row[0]: (int(row[1] or 0), int(row[2] or 0))
        for row in client.query(
            """
            SELECT table, sum(rows), sum(bytes_on_disk)
            FROM system.parts
            WHERE database = 'ad_analytics' AND active
            GROUP BY table
            """
        ).result_rows
    }
    columns: dict[str, tuple[int, int]] = {}
    for table, col_count, attr_count in client.query(
        """
        SELECT
            table,
            count() AS col_count,
            countIf(name IN {attrs:Array(String)}) AS attr_count
        FROM system.columns
        WHERE database = 'ad_analytics'
        GROUP BY table
        """,
        parameters={"attrs": sorted(STAR_CANDIDATE_ATTRS)},
    ).result_rows:
        columns[table] = (int(col_count), int(attr_count))

    out: dict[str, ObjectMeta] = {}
    for name, (engine, create_query, partition_key, sorting_key) in tables.items():
        rows, bytes_on_disk = parts.get(name, (None, None))
        col_count, attr_count = columns.get(name, (0, 0))
        out[name] = ObjectMeta(
            name=name,
            engine=engine,
            rows=rows,
            bytes_on_disk=bytes_on_disk,
            columns=col_count,
            attr_columns=attr_count,
            create_query=create_query,
            partition_key=partition_key,
            sorting_key=sorting_key,
        )
    return out


def _fmt_int(value: int | None) -> str:
    return "view" if value is None else f"{value:,}"


def _fmt_bytes(value: int | None) -> str:
    if value is None:
        return "view"
    units = ["B", "KB", "MB", "GB", "TB"]
    x = float(value)
    unit = units[0]
    for unit in units:
        if x < 1024 or unit == units[-1]:
            break
        x /= 1024
    return f"{x:.1f} {unit}"


def _view_source(create_query: str) -> str:
    m = re.search(r"FROM\s+ad_analytics\.(`?)([A-Za-z0-9_]+)\1", create_query, flags=re.IGNORECASE)
    return m.group(2) if m else ""


def _recommendation(meta: ObjectMeta) -> str:
    source = _view_source(meta.create_query)
    if meta.engine == "View":
        if source.startswith("pbi_import_"):
            return "OK: view over physical import"
        if source:
            if meta.columns >= 30 or meta.attr_columns >= 5:
                return f"star/materialize candidate: source {source}"
            return f"OK: projection view over {source}"
        return "review: complex view may recalc on refresh"
    if (meta.rows or 0) >= 1_000_000 and meta.attr_columns >= 5:
        return "star candidate: move text attrs to Dim_*"
    if (meta.rows or 0) >= 1_000_000 and meta.columns >= 30:
        return "narrow fact candidate: keep keys+metrics only"
    if meta.attr_columns >= 5:
        return "dim candidate: repeated descriptive columns"
    return "OK/low priority"


def _index_note(meta: ObjectMeta) -> str:
    if meta.engine == "View":
        source = _view_source(meta.create_query)
        return f"view source={source or '?'}"
    if not meta.sorting_key:
        return "missing sorting_key"
    rows = meta.rows or 0
    has_date_col = "date" in meta.sorting_key.lower() or "`date`" in meta.sorting_key.lower()
    if rows >= 1_000_000 and "date" in {c.lower() for c in ["date"]} and not has_date_col:
        return f"review sort={meta.sorting_key}"
    return f"partition={meta.partition_key or '-'}; sort={meta.sorting_key}"


def build_report() -> str:
    client = get_client()
    meta = _fetch_meta(client)
    pbi_tables = _read_refresh_tables()

    lines = [
        "# PBI Source Audit",
        "",
        "| PBI table | CH object | engine | rows | disk | cols | attr cols | key/index note | recommendation |",
        "|---|---|---|---:|---:|---:|---:|---|---|",
    ]
    missing: list[str] = []
    for pbi_name in pbi_tables:
        found = None
        for candidate in _candidate_names(pbi_name):
            if candidate in meta:
                found = meta[candidate]
                break
        if found is None:
            missing.append(pbi_name)
            lines.append(f"| `{pbi_name}` | missing | missing |  |  |  |  | missing | create/port builder |")
            continue
        lines.append(
            "| `{pbi}` | `{obj}` | {engine} | {rows} | {disk} | {cols} | {attrs} | {idx} | {rec} |".format(
                pbi=pbi_name,
                obj=found.name,
                engine=found.engine or "missing",
                rows=_fmt_int(found.rows),
                disk=_fmt_bytes(found.bytes_on_disk),
                cols=found.columns,
                attrs=found.attr_columns,
                idx=_index_note(found).replace("|", "\\|"),
                rec=_recommendation(found),
            )
        )

    lines.extend(["", "## Missing", ""])
    if missing:
        lines.extend(f"- `{name}`" for name in missing)
    else:
        lines.append("- none")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="star_refactor/pbi_source_audit.md")
    args = parser.parse_args()
    report = build_report()
    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report)
    print(report)


if __name__ == "__main__":
    main()
