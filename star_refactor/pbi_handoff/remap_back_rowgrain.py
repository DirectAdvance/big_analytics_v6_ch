#!/usr/bin/env python3
"""remap_back_rowgrain.py — ОБРАТНЫЙ remap (2026-06-07).

Прошлый прогон (remap_field_refs.py) перенёс ссылки 14 row-grain полей с факта
(big_analytics_full) на Dim_Site. Это была ошибка: поля row-grain (не зависят от domain),
их нельзя держать в Dim_Site (mode-свёртка к 1 значению/домен искажает срезы). Колонки
ВОЗВРАЩЕНЫ на star.fact_big_analytics. Теперь возвращаем и ссылки в PBIP обратно:
  Entity Dim_Site -> big_analytics_full  для 14 полей
  Property "статус_сайта" -> "статус"     (прошлый прогон переименовал)

ОБЛАСТЬ: definition/pages/ + definition/bookmarks/ (бэкапы *backup*/bak_/root_orig НЕ трогаются).
НЕ ТРОГАЕТ: Dim_Campaign/Dim_AdGroup/Dim_Date поля, меры, ключи, 10 orphan-колонок,
            поля что остаются на факте.

Зеркалит безопасную alias-логику прямого скрипта: alias (From) перенацеливается на факт
ТОЛЬКО если все поля под ним едут на факт и под alias нет полей, что ОСТАЮТСЯ на Dim_Site
(в Dim_Site после ревизии остаётся лишь ключ domain — поэтому конфликтов быть почти не должно).

Запуск:
    python3 remap_back_rowgrain.py "<...>.Report"            # dry-run
    python3 remap_back_rowgrain.py "<...>.Report" --apply
"""
from __future__ import annotations
import argparse, json, os, sys, tempfile
from collections import Counter


def atomic_write_json(fp, data):
    """Атомарная запись: tmp в том же каталоге + os.replace. Работает даже если сам файл
    root-owned read-only — запись зависит от прав КАТАЛОГА (writable), не файла. Бонус:
    новый файл принадлежит текущему пользователю (снимает root-owned блок на будущее)."""
    d = os.path.dirname(fp)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".__remap_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, fp)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise

SITE = "Dim_Site"
FACT = "big_analytics_full"

# 14 row-grain полей, возвращаемых на факт. Значение = новое имя Property на факте
# (None = имя то же). "статус_сайта" в Dim_Site -> "статус" на факте.
ROWGRAIN = ["направление", "site_quiz", "марки авто", "специалист", "тип_сайта",
            "салон", "шаблон", "id_салона", "город", "регион", "проджект",
            "менеджер", "Название crm"]
# Поле-источник в Dim_Site -> (новое имя на факте)
FIELD_MAP: dict[str, str | None] = {f: None for f in ROWGRAIN}
FIELD_MAP["статус_сайта"] = "статус"   # переименование назад
FIELD_MAP["статус"] = None             # если где-то осталось имя "статус" на Dim_Site

SITE_ENTITIES = {SITE}


def collect_aliases(query_obj):
    aliases = {}
    def walk(o):
        if isinstance(o, dict):
            if isinstance(o.get("From"), list):
                for fr in o["From"]:
                    if isinstance(fr, dict) and "Name" in fr and "Entity" in fr:
                        aliases[fr["Name"]] = fr["Entity"]
            for v in o.values(): walk(v)
        elif isinstance(o, list):
            for v in o: walk(v)
    walk(query_obj)
    return aliases


def remap_node(node, stats):
    """Прямой Entity Dim_Site -> big_analytics_full + переименование Property. True если изменено."""
    expr = node.get("Expression", {})
    sref = expr.get("SourceRef", {}) if isinstance(expr, dict) else {}
    prop = node.get("Property")
    if prop not in FIELD_MAP:
        return False
    if "Entity" in sref and sref["Entity"] in SITE_ENTITIES:
        sref["Entity"] = FACT
        new_p = FIELD_MAP[prop]
        if new_p:
            node["Property"] = new_p
        stats[prop] += 1
        return True
    return False


def alias_uses_site_kept(query_obj, alias):
    """True если под alias есть поле, что ОСТАЁТСЯ на Dim_Site (не в FIELD_MAP, напр. ключ domain)."""
    found = [False]
    def walk(o):
        if isinstance(o, dict):
            for kind in ("Column", "Measure", "HierarchyLevel"):
                node = o.get(kind)
                if isinstance(node, dict):
                    expr = node.get("Expression", {})
                    sref = expr.get("SourceRef", {}) if isinstance(expr, dict) else {}
                    if sref.get("Source") == alias:
                        p = node.get("Property")
                        if kind == "Measure" or (p and p not in FIELD_MAP):
                            found[0] = True
            for v in o.values(): walk(v)
        elif isinstance(o, list):
            for v in o: walk(v)
    walk(query_obj)
    return found[0]


def remap_direct_entities(data, stats):
    """Глобальный проход по ВСЕМУ файлу: прямые SourceRef.Entity = Dim_Site -> big_analytics_full
    для 14 полей + переименование статус_сайта->статус. Прямые Entity не зависят от scope."""
    def walk(o):
        if isinstance(o, dict):
            for kind in ("Column", "Measure", "HierarchyLevel"):
                node = o.get(kind)
                if isinstance(node, dict):
                    remap_node(node, stats)
            for v in o.values(): walk(v)
        elif isinstance(o, list):
            for v in o: walk(v)
    walk(data)


def process_scope(query_obj, stats, alias_conflicts):
    """Обрабатывает ОДИН изолированный From-scope (визуал query / отдельный filter).
    Перенацеливает alias-ссылки (Source) Dim_Site -> big_analytics_full, но ТОЛЬКО если
    под этим alias в ДАННОМ scope нет полей, остающихся на Dim_Site (ключ domain)."""
    aliases = collect_aliases(query_obj)
    src_alias_targets: dict[str, bool] = {}

    def walk(o):
        if isinstance(o, dict):
            for kind in ("Column", "Measure", "HierarchyLevel"):
                node = o.get(kind)
                if isinstance(node, dict):
                    expr = node.get("Expression", {})
                    sref = expr.get("SourceRef", {}) if isinstance(expr, dict) else {}
                    alias = sref.get("Source")
                    p = node.get("Property")
                    if alias and aliases.get(alias) in SITE_ENTITIES and p in FIELD_MAP:
                        src_alias_targets[alias] = True
            for v in o.values(): walk(v)
        elif isinstance(o, list):
            for v in o: walk(v)
    walk(query_obj)

    # безопасные alias: под alias в ЭТОМ scope только rowgrain-поля (нет полей на Dim_Site)
    safe = {}
    for alias in src_alias_targets:
        if not alias_uses_site_kept(query_obj, alias):
            safe[alias] = FACT
        else:
            alias_conflicts.append((alias,))

    # применить From.Entity Dim_Site->FACT + переименование Property под safe alias
    if safe:
        def walk2(o):
            if isinstance(o, dict):
                if isinstance(o.get("From"), list):
                    for fr in o["From"]:
                        if isinstance(fr, dict) and fr.get("Name") in safe \
                           and fr.get("Entity") in SITE_ENTITIES:
                            fr["Entity"] = FACT
                for kind in ("Column", "HierarchyLevel"):
                    node = o.get(kind)
                    if isinstance(node, dict):
                        expr = node.get("Expression", {})
                        sref = expr.get("SourceRef", {}) if isinstance(expr, dict) else {}
                        alias = sref.get("Source")
                        p = node.get("Property")
                        if alias in safe and p in FIELD_MAP and FIELD_MAP[p]:
                            node["Property"] = FIELD_MAP[p]
                for v in o.values(): walk2(v)
            elif isinstance(o, list):
                for v in o: walk2(v)
        walk2(query_obj)
        for a in safe:
            stats[f"<alias {a}->{FACT}>"] += 1


def iter_query_scopes(data):
    """Возвращает список изолированных query-объектов: каждый объект, у которого есть
    СОБСТВЕННЫЙ ключ 'From' (визуал query / каждый отдельный filterConfig.filters[i].filter).
    Это правильная гранулярность: в одном page.json несколько filter-объектов, у каждого
    свой From[{Name:'b'}] на РАЗНЫЕ Entity (b->big_analytics_full в одном, b->Dim_Site в
    другом). Обработка всего файла как единого query ложно склеивает alias 'b' из разных
    filter -> ложные конфликты. Обрабатываем каждый From-scope отдельно.
    Если ни одного 'From' нет — возвращаем сам объект (поля с прямым Entity)."""
    scopes = []
    def walk(o):
        if isinstance(o, dict):
            if isinstance(o.get("From"), list):
                scopes.append(o)
                # внутрь scope не спускаемся для поиска новых From — вложенные From
                # (подзапросы) редки; если есть, walk их обработает как часть scope.
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(data)
    if not scopes:
        scopes = [data]
    return scopes


def is_excluded(path: str) -> bool:
    for p in path.lower().split(os.sep):
        if "backup" in p: return True
        if p.startswith("pages.bak") or p.startswith("pages.root_orig"): return True
        if p.startswith("pages_backup"): return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("report_dir")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    roots = [os.path.join(args.report_dir, "definition", "pages"),
             os.path.join(args.report_dir, "definition", "bookmarks")]
    stats = Counter(); alias_conflicts = []; changed = 0; scanned = 0

    for rootdir in roots:
        if not os.path.isdir(rootdir): continue
        for root, _d, files in os.walk(rootdir):
            if is_excluded(root): continue
            for fn in files:
                if not fn.endswith(".json"): continue
                fp = os.path.join(root, fn)
                if is_excluded(fp): continue
                try:
                    with open(fp, encoding="utf-8") as fh:
                        data = json.load(fh)
                except Exception as e:
                    print(f"  ! пропуск {fp}: {e}", file=sys.stderr); continue
                scanned += 1
                before = json.dumps(data, ensure_ascii=False, sort_keys=True)
                # 1) прямые Entity — глобально по файлу
                remap_direct_entities(data, stats)
                # 2) alias-ссылки — изолированно по каждому From-scope (visual/filter)
                for scope in iter_query_scopes(data):
                    process_scope(scope, stats, alias_conflicts)
                after = json.dumps(data, ensure_ascii=False, sort_keys=True)
                if before != after:
                    changed += 1
                    if args.apply:
                        atomic_write_json(fp, data)

    print("=" * 70)
    print(f"СКАН pages/+bookmarks/: {scanned} json | изменено: {changed} (apply={args.apply})")
    print("Переписано Dim_Site -> big_analytics_full по полям/alias:")
    for k, n in stats.most_common():
        print(f"  {n:4}  {k}")
    direct = sum(v for k, v in stats.items() if not k.startswith("<alias"))
    aliasn = sum(v for k, v in stats.items() if k.startswith("<alias"))
    print(f"ИТОГО прямых полей: {direct}; alias-перенацеливаний: {aliasn}")
    if alias_conflicts:
        print(f"\n⚠ ALIAS-КОНФЛИКТЫ (под alias есть поле остающееся на Dim_Site): {len(alias_conflicts)}")
        for (a,) in set(alias_conflicts):
            print(f"  alias '{a}'")
    if not args.apply:
        print("\nDRY-RUN. Для записи добавь --apply")


if __name__ == "__main__":
    main()
