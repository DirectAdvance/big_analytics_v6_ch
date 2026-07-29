#!/usr/bin/env python3
"""
find_field_refs.py — сканер ссылок на поля факта, которые переезжают в dim.

Назначение: пройти по PBIP-отчёту (папка *.Report, файлы visual.json / page.json),
найти ВСЕ места, где визуалы/фильтры ссылаются на справочные поля таблицы
big_analytics_full (салон, CampaignName, AdGroupName, …), и выдать карту замен
факт→dim. Это «глаза» для ручной распайки в Power BI Desktop (Этап 5 чек-листа).

РЕЖИМ: read-only. Ничего не правит — только сканирует и печатает/пишет CSV.
(Авто-правка JSON визуалов возможна, но рискованна вслепую → оставлена пользователю
в открытом Power BI; см. 02_checklist_raspaika.md.)

Запуск:
    python3 find_field_refs.py "/path/to/Большая аналитика_v00.Report"
    python3 find_field_refs.py "<...>.Report" --csv field_refs_report.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter

# ── Карта факт→dim. Имя поля в факте  ->  целевая dim-таблица ──────────────────
FIELD_TO_DIM: dict[str, str] = {}
for f in ["салон", "город", "регион", "тип_сайта", "id_салона", "направление",
          "шаблон", "site_quiz", "проджект", "менеджер", "специалист",
          "Название crm", "марки авто", "статус", "статус_сайта"]:
    FIELD_TO_DIM[f] = "Dim_Site"
for f in ["CampaignName", "номер кампании | название кампании", "campaign_status",
          "payment_model", "account_login"]:
    FIELD_TO_DIM[f] = "Dim_Campaign"
for f in ["AdGroupName", "adgroup_code", "номер группы | название группы",
          "ag_part1", "ag_part2", "ag_part3", "ag_part4", "ag_part5",
          "ag_part6", "ag_part7", "ag_part1_name", "неверный_кодер_new"]:
    FIELD_TO_DIM[f] = "Dim_AdGroup"
for f in ["День недели", "week_start"]:
    FIELD_TO_DIM[f] = "Dim_Date"

# Таблица-факт в модели PBI (источник, который переедет на star.fact_big_analytics).
FACT_TABLE_NAMES = {"big_analytics_full", "big_analytics_unified"}

# Поля, которые ОСТАЮТСЯ на факте (не трогать) — для контроля ложных срабатываний.
KEEP_ON_FACT = {
    "атрибуция", "_source_table", "total_cost", "Clicks", "Impressions",
    "kol_vo_zayavok", "korr", "kval", "priezd", "prodazhi", "nekorr",
    "ne_otvechaet", "nedozvon", "filtr", "priedet", "RlAdjustmentId",
    "План заявки", "План приезда", "priezd_arrival_date", "prodazhi_arrival_date",
    "dohod_do_kredita", "dobro", "CampaignId", "AdGroupId", "domain", "Date",
}


def _walk_json(obj, path, hits):
    """Рекурсивно ищет конструкции вида {Column:{Expression:{SourceRef:{Entity}},Property}}."""
    if isinstance(obj, dict):
        # типовой PBI-узел поля: Column / Measure / HierarchyLevel
        for kind in ("Column", "Measure"):
            node = obj.get(kind)
            if isinstance(node, dict):
                prop = node.get("Property")
                entity = None
                expr = node.get("Expression", {})
                sref = expr.get("SourceRef", {}) if isinstance(expr, dict) else {}
                entity = sref.get("Entity") or sref.get("Source")
                if prop and entity:
                    hits.append((entity, prop, kind, path))
        for k, v in obj.items():
            _walk_json(v, f"{path}/{k}", hits)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _walk_json(v, f"{path}[{i}]", hits)


def scan_report(report_dir: str):
    hits = []
    for root, _dirs, files in os.walk(report_dir):
        for fn in files:
            if not fn.endswith(".json"):
                continue
            fp = os.path.join(root, fn)
            try:
                with open(fp, encoding="utf-8") as fh:
                    data = json.load(fh)
            except Exception as e:
                print(f"  ! пропуск {fp}: {e}", file=sys.stderr)
                continue
            rel = os.path.relpath(fp, report_dir)
            _walk_json(data, rel, hits)
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("report_dir", help="путь к папке *.Report (PBIP)")
    ap.add_argument("--csv", default=None, help="файл для CSV-отчёта")
    args = ap.parse_args()

    if not os.path.isdir(args.report_dir):
        sys.exit(f"Не папка: {args.report_dir}")

    hits = scan_report(args.report_dir)

    # фильтруем только ссылки на факт + поле, которое переезжает в dim
    rows = []
    field_counter = Counter()
    for entity, prop, kind, path in hits:
        if entity in FACT_TABLE_NAMES and prop in FIELD_TO_DIM:
            target = FIELD_TO_DIM[prop]
            rows.append((entity, prop, target, kind, path))
            field_counter[prop] += 1

    # отдельно — поля факта, которые ОСТАЮТСЯ (для контроля)
    keep_refs = sum(1 for e, p, k, _ in hits if e in FACT_TABLE_NAMES and p in KEEP_ON_FACT)

    print("=" * 70)
    print(f"СКАН: {args.report_dir}")
    print(f"Всего ссылок-полей найдено: {len(hits)}")
    print(f"Ссылок на ФАКТ-поля для ПЕРЕЕЗДА в dim: {len(rows)}")
    print(f"Ссылок на поля факта, что ОСТАЮТСЯ (меры/ключи): {keep_refs}")
    print("=" * 70)
    print("\nПо полям (сколько ссылок переназначить):")
    for prop, n in field_counter.most_common():
        print(f"  {n:4}  {prop:35} -> {FIELD_TO_DIM[prop]}")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["fact_entity", "field", "target_dim", "kind", "file_path"])
            w.writerows(rows)
        print(f"\nCSV записан: {args.csv} ({len(rows)} строк)")


if __name__ == "__main__":
    main()
