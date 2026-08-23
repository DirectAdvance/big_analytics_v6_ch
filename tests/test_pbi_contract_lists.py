import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _literal_list_from_file(path: Path, name: str):
    module = ast.parse(path.read_text(encoding="utf-8"))
    for node in module.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in {path}")


def _function_names(path: Path) -> set[str]:
    module = ast.parse(path.read_text(encoding="utf-8"))
    return {node.name for node in module.body if isinstance(node, ast.FunctionDef)}


def test_stale_analytics_report_objects_are_not_in_pbi_contract_lists():
    pbi_source_objects = _literal_list_from_file(
        ROOT / "star_refactor" / "build_pbi_compat.py",
        "PBI_SOURCE_OBJECTS",
    )
    all_tables = _literal_list_from_file(ROOT / "refresh_powerbi.py", "_ALL_TABLES")
    verify_required_tables = _literal_list_from_file(
        ROOT / "data_check" / "verify_big_analytics.py",
        "REQUIRED_TABLES",
    )
    verify_pbi_source_objects = _literal_list_from_file(
        ROOT / "data_check" / "verify_big_analytics.py",
        "PBI_SOURCE_OBJECTS",
    )
    verify_pbi_compat_objects = _literal_list_from_file(
        ROOT / "data_check" / "verify_big_analytics.py",
        "PBI_COMPAT_OBJECTS",
    )
    stale_compat_objects = {"arp_fact", "arc_fact", "arf_fact", "Dim_Criterion"}
    stale_refresh_tables = {
        "analytics_report_placement",
        "analytics_report_criterion",
        "analytics_report_feed",
    }

    assert stale_compat_objects.isdisjoint(pbi_source_objects)
    assert stale_refresh_tables.isdisjoint(all_tables)
    assert {"arp_fact", "arc_fact", "arf_fact"}.isdisjoint(verify_required_tables)
    assert stale_compat_objects.isdisjoint(verify_pbi_source_objects)
    assert {"arp_fact", "arc_fact", "arf_fact"}.isdisjoint(verify_pbi_compat_objects)


def test_removed_feed_and_criterion_legacy_builders_are_not_callable():
    builders = _function_names(ROOT / "star_refactor" / "build_pbi_compat.py")

    assert {"build_arf_fact", "build_arc_fact"}.isdisjoint(builders)


def test_pbi_empty_whitelist_is_empty():
    verify_path = ROOT / "data_check" / "verify_big_analytics.py"
    allowed = _literal_list_from_file(verify_path, "PBI_EMPTY_ALLOWED")
    by_design = _literal_list_from_file(verify_path, "PBI_EMPTY_BY_DESIGN")

    assert allowed == set()
    assert by_design == allowed


def test_live_arp_and_search_query_replace_raw_new_snapshots():
    """ARP_LIVE_2026-08-23: обе PBI-таблицы обязаны быть в контракте, иначе пустая `bi_*` не упадёт."""
    live_objects = {"analytics_report_placement", "yandex_direct_search_query_report_master"}
    pbi_source_objects = _literal_list_from_file(
        ROOT / "star_refactor" / "build_pbi_compat.py",
        "PBI_SOURCE_OBJECTS",
    )
    verify_pbi_source_objects = _literal_list_from_file(
        ROOT / "data_check" / "verify_big_analytics.py",
        "PBI_SOURCE_OBJECTS",
    )

    assert live_objects <= set(pbi_source_objects)
    assert live_objects <= set(verify_pbi_source_objects)


def test_live_pbi_sql_never_reads_raw_new_snapshots():
    """Контракт BA6: живые `bi_*` читают только raw_data/reference_data/живые ad_analytics-объекты."""
    import sys

    sys.path.insert(0, str(ROOT))
    from star_refactor import build_pbi_compat

    for name in ("analytics_report_placement", "yandex_direct_search_query_report_master"):
        sql = build_pbi_compat.PBI_VIEW_SQL_BUILDERS[name]()
        assert "raw_new_" not in sql, name
