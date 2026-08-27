from step1_load_raw import step1
from step13_arrival import step13


def test_step1_excludes_copy_leads_from_raw_outputs() -> None:
    leads_sql = step1._raw_leads_select_sql()
    calls_sql = step1._raw_calls_sql()
    perform_sql = step1._raw_perform_leads_sql()

    assert "AND ifNull(l.is_copy_for_removal, 0) = 0" in leads_sql
    assert "AND ifNull(l.is_copy_for_removal, 0) = 0" in calls_sql
    assert "AND ifNull(la.is_copy_for_removal, 0) = 0" in perform_sql
    assert perform_sql.count("AND ifNull(l.is_copy_for_removal, 0) = 0") >= 3


def test_step1_raw_yandex_keeps_domain_for_spend_slices() -> None:
    assert "`domain` LowCardinality(Nullable(String))" in step1._raw_yandex_ddl()
    assert "domain AS domain" in step1._raw_yandex_select()
    assert "\n    domain,\n    \"RlAdjustmentId\"" in step1._raw_yandex_select()


def test_step1_resolves_leads_all_salon_code_to_name() -> None:
    leads_sql = step1._raw_leads_select_sql()
    perform_sql = step1._raw_perform_leads_sql()

    assert "raw_data.gsheet_autosalony_clients AS salon_client" in leads_sql
    assert "nullIf(lowerUTF8(extract(ifNull(l.salon, ''), '^([A-Za-z]+_[0-9]+)')), '')" in leads_sql
    assert "extract(ifNull(l.salon, ''), '^([A-Za-z]+_[0-9]+)')" in leads_sql
    assert "coalesce(nullIf(salon_client.salon, ''), l.salon) AS salon" in leads_sql
    assert "coalesce(nullIf(salon_client.salon, ''), l.salon) AS salon" in perform_sql


def test_step13_direct_leads_all_reads_ignore_copy_rows() -> None:
    assert "AND ifNull(is_copy_for_removal, 0) = 0" in step13._marcar_arrivals_cte()
    assert "AND ifNull(is_copy_for_removal, 0) = 0" in step13._calls_branch_sql("2026-01-01")
    assert "AND ifNull(is_copy_for_removal, 0) = 0" in step13._marcar_orphan_branch_sql("2026-01-01")


def test_step13_pixel_metrics_resolve_leads_all_salon_code_to_name() -> None:
    sql = step13._pixel_branch_sql("2026-01-01")

    assert "raw_data.gsheet_autosalony_clients AS salon_client" in sql
    assert "coalesce(nullIf(salon_client.salon, ''), l.salon)" in sql
