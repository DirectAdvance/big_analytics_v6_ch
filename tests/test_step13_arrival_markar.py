from step13_arrival import step13


def test_marcar_arrivals_cte_exports_sheet_domain():
    sql = step13._marcar_arrivals_cte()

    assert "sheet_domain" in sql
    assert "lower(trim(ifNull(source, '')))" in sql
    assert "argMax(sheet_domain" in sql


def test_leads_and_calls_use_marcar_sheet_domain_when_it_matches_auto_site():
    leads_sql = step13._leads_branch_sql("2026-01-01")
    calls_sql = step13._calls_branch_sql("2026-01-01")

    for sql in (leads_sql, calls_sql):
        assert "gs_ma.domain_key = ma.sheet_domain" in sql
        assert "gs_ma.direction = 'Авто'" in sql
        assert "coalesce(nullIf(gs_ma.domain, '')" in sql
        assert "_auto_domains_filter(\"coalesce(nullIf(gs_ma.domain, '')," not in sql


def test_build_branches_contains_marcar_orphans_before_pixel():
    branch_names = [name for name, _columns, _sql in step13.build_branches("2026-01-01")]

    assert branch_names == ["leads", "calls", "marcar_orphans", "perform_posevy_proxy", "pixel"]
    assert branch_names.index("marcar_orphans") < branch_names.index("pixel")


def test_marcar_orphans_branch_adds_unmatched_gsheet_rows_by_source_domain():
    sql = step13._marcar_orphan_branch_sql("2026-01-01")

    assert "MARCAR_GSHEET_ORPHANS_2026-08-13" in sql
    assert "reference_data.gsheet_priezdi_marcar" in sql
    assert "raw_data.leads_all" in sql
    assert "source_type = 'marcar_crm_excel'" in sql
    assert "gs.domain_key = ms.sheet_domain" in sql
    assert "if(lead_status = 'Продажа', 1, 0)" in sql
