#!/usr/bin/env python3
"""
remap_field_refs.py — переписывает ссылки полей факта big_analytics_full на dim-таблицы
в PBIP-отчёте (star-распайка, Этап 5).

ЧТО ДЕЛАЕТ:
  Для каждого визуала/закладки/фильтра, где поле ссылается на сущность
  big_analytics_full (напрямую Entity ИЛИ через alias Source + From-клаузу) и это поле
  переехало в dim — меняет целевую сущность (Entity/From.Entity) на dim и при необходимости
  переименовывает Property (статус -> статус_сайта).

ОБЛАСТЬ: только definition/pages/ и definition/bookmarks/ (активные). Бэкап-папки
  (pages.bak_*, pages.root_orig_*, *backup*) НЕ трогаются.

НЕ ТРОГАЕТ: меры, claim, ключи, 10 орфан-колонок, поля что остаются на факте.

Запуск:
    python3 remap_field_refs.py "<...>.Report"            # dry-run (только отчёт)
    python3 remap_field_refs.py "<...>.Report" --apply    # записать изменения
"""
from __future__ import annotations
import argparse, json, os, sys
from collections import Counter

# ── Карта факт-поле -> (целевая dim, новое имя поля в dim) ─────────────────────
# новое имя = None означает "имя то же".
SITE = "Dim_Site"; CAMP = "Dim_Campaign"; AG = "Dim_AdGroup"; DATE = "Dim_Date"
FIELD_MAP: dict[str, tuple[str, str | None]] = {}
for f in ["салон","город","регион","тип_сайта","id_салона","направление","шаблон",
          "site_quiz","проджект","менеджер","специалист","Название crm","марки авто"]:
    FIELD_MAP[f] = (SITE, None)
FIELD_MAP["статус"] = (SITE, "статус_сайта")      # переименование в dim
FIELD_MAP["статус_сайта"] = (SITE, None)
for f in ["CampaignName","номер кампании | название кампании","campaign_status",
          "payment_model","account_login","статус_кампании"]:
    FIELD_MAP[f] = (CAMP, None)
for f in ["AdGroupName","adgroup_code","номер группы | название группы","ag_part1",
          "ag_part2","ag_part3","ag_part4","ag_part5","ag_part6","ag_part7",
          "ag_part1_name","неверный_кодер_new"]:
    FIELD_MAP[f] = (AG, None)
for f in ["День недели","week_start"]:
    FIELD_MAP[f] = (DATE, None)

FACT_ENTITIES = {"big_analytics_full", "big_analytics_unified"}


def collect_aliases(query_obj):
    """Внутри объекта запроса собрать alias->Entity из всех 'From' клауз.
    Возвращает dict alias_name -> entity, ограниченный alias-ами, что указывают на факт."""
    aliases = {}
    def walk(o):
        if isinstance(o, dict):
            if "From" in o and isinstance(o["From"], list):
                for fr in o["From"]:
                    if isinstance(fr, dict) and "Name" in fr and "Entity" in fr:
                        aliases[fr["Name"]] = fr["Entity"]
            for v in o.values(): walk(v)
        elif isinstance(o, list):
            for v in o: walk(v)
    walk(query_obj)
    return aliases


def remap_node(node, aliases, stats, src_alias_targets):
    """node = dict под ключом Column/Measure/HierarchyLevel с Expression.SourceRef.
    Меняет Entity/Property на dim, если применимо. Возвращает True если изменено.
    Для alias-ссылок (Source) — собирает какие alias надо перенацелить (через From)."""
    expr = node.get("Expression", {})
    sref = expr.get("SourceRef", {}) if isinstance(expr, dict) else {}
    prop = node.get("Property")
    if not prop or prop not in FIELD_MAP:
        return False
    target_dim, new_prop = FIELD_MAP[prop]

    # прямой Entity
    if "Entity" in sref:
        if sref["Entity"] in FACT_ENTITIES:
            sref["Entity"] = target_dim
            if new_prop: node["Property"] = new_prop
            stats[prop] += 1
            return True
        return False
    # alias (Source) — резолвим через aliases
    if "Source" in sref:
        alias = sref["Source"]
        ent = aliases.get(alias)
        if ent in FACT_ENTITIES:
            # пометить, что этот alias в данном запросе указывает на факт + цель dim.
            # Property и Entity НЕ трогаем здесь — только после решения по alias (шаг 3),
            # чтобы при alias-конфликте (alias остаётся на факте) не сломать Property.
            src_alias_targets.setdefault(alias, set()).add(target_dim)
            return False  # фактическое изменение — позже, в process_query
    return False


def process_query(query_obj, stats, alias_conflicts):
    """Обрабатывает один объект запроса визуала/фильтра целиком (с его From-клаузами)."""
    aliases = collect_aliases(query_obj)
    src_alias_targets: dict[str, set] = {}

    # 1) пройтись по всем Column/Measure узлам
    def walk(o):
        if isinstance(o, dict):
            for kind in ("Column","Measure","HierarchyLevel"):
                if isinstance(o.get(kind), dict):
                    remap_node(o[kind], aliases, stats, src_alias_targets)
            for v in o.values(): walk(v)
        elif isinstance(o, list):
            for v in o: walk(v)
    walk(query_obj)

    # 2) решить по alias: если все поля под alias едут в одну dim -> перенацелить From.Entity
    #    если в разные dim -> КОНФЛИКТ (alias используется и для факт-мер, и для dim-поля)
    #    -> alias оставляем на факте (не трогаем Entity), но это значит dim-поле сломается.
    #    Такие случаи логируем для ручной правки.
    safe_realias = {}
    for alias, dims in src_alias_targets.items():
        # есть ли под этим alias также поля, что ОСТАЮТСЯ на факте (меры/ключи)?
        stays = alias_uses_fact_kept(query_obj, alias)
        if len(dims) == 1 and not stays:
            safe_realias[alias] = next(iter(dims))
        else:
            alias_conflicts.append((alias, sorted(dims), stays))

    # 3) применить безопасные перенацеливания From.Entity + Property-переименования
    if safe_realias:
        def walk2(o):
            if isinstance(o, dict):
                if "From" in o and isinstance(o["From"], list):
                    for fr in o["From"]:
                        if isinstance(fr, dict) and fr.get("Name") in safe_realias \
                           and fr.get("Entity") in FACT_ENTITIES:
                            fr["Entity"] = safe_realias[fr["Name"]]
                # переименовать Property у Column/HierarchyLevel под безопасным alias
                for kind in ("Column", "HierarchyLevel"):
                    node = o.get(kind)
                    if isinstance(node, dict):
                        expr = node.get("Expression", {})
                        sref = expr.get("SourceRef", {}) if isinstance(expr, dict) else {}
                        alias = sref.get("Source")
                        p = node.get("Property")
                        if alias in safe_realias and p in FIELD_MAP:
                            _, new_p = FIELD_MAP[p]
                            if new_p:
                                node["Property"] = new_p
                for v in o.values(): walk2(v)
            elif isinstance(o, list):
                for v in o: walk2(v)
        walk2(query_obj)
        for a, d in safe_realias.items():
            stats[f"<alias {a}->{d}>"] += 1


def alias_uses_fact_kept(query_obj, alias):
    """True если под данным alias есть поле, что ОСТАЁТСЯ на факте (не в FIELD_MAP)."""
    found = [False]
    def walk(o):
        if isinstance(o, dict):
            for kind in ("Column","Measure","HierarchyLevel"):
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


def is_excluded(path: str) -> bool:
    low = path.lower()
    parts = low.split(os.sep)
    for p in parts:
        if "backup" in p: return True
        if p.startswith("pages.bak") or p.startswith("pages.root_orig"): return True
        if p.startswith("pages_backup"): return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("report_dir")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    base = args.report_dir
    targets_roots = [os.path.join(base, "definition", "pages"),
                     os.path.join(base, "definition", "bookmarks")]

    stats = Counter(); alias_conflicts = []
    changed_files = 0; scanned = 0

    for rootdir in targets_roots:
        if not os.path.isdir(rootdir): continue
        for root, _dirs, files in os.walk(rootdir):
            if is_excluded(root): continue
            for fn in files:
                if not fn.endswith(".json"): continue
                fp = os.path.join(root, fn)
                if is_excluded(fp): continue
                try:
                    with open(fp, encoding="utf-8") as fh:
                        text = fh.read()
                    data = json.loads(text)
                except Exception as e:
                    print(f"  ! пропуск {fp}: {e}", file=sys.stderr); continue
                scanned += 1
                before = json.dumps(data, ensure_ascii=False, sort_keys=True)
                process_query(data, stats, alias_conflicts)
                after = json.dumps(data, ensure_ascii=False, sort_keys=True)
                if before != after:
                    changed_files += 1
                    if args.apply:
                        with open(fp, "w", encoding="utf-8") as fh:
                            json.dump(data, fh, ensure_ascii=False, indent=2)

    print("=" * 70)
    print(f"СКАН (только pages/ + bookmarks/): {scanned} json")
    print(f"Файлов изменено: {changed_files}  (apply={args.apply})")
    print("Переписано по полям/alias:")
    for k, n in stats.most_common():
        print(f"  {n:4}  {k}")
    total = sum(v for k, v in stats.items() if not k.startswith("<alias"))
    aliasn = sum(v for k, v in stats.items() if k.startswith("<alias"))
    print(f"ИТОГО прямых полей: {total}; alias-перенацеливаний: {aliasn}")
    if alias_conflicts:
        print(f"\n⚠ ALIAS-КОНФЛИКТЫ (оставлены на факте, нужна ручная правка в UI): {len(alias_conflicts)}")
        seen = set()
        for a, dims, stays in alias_conflicts:
            key = (a, tuple(dims), stays)
            if key in seen: continue
            seen.add(key)
            print(f"  alias '{a}' -> {dims} (используется_и_мерами/ключами={stays})")
    if not args.apply:
        print("\nDRY-RUN. Для записи добавь --apply")


if __name__ == "__main__":
    main()
