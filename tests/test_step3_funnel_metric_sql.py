from step3_build_sources import step3
from step6_build_full import step6


def test_sale_forces_credit_and_approval_funnel_steps():
    sql = step3._metric_expr("status", "reason", "source_type", "salon")

    assert "AS dohod_do_kredita" in sql
    assert "AS dobro" in sql
    assert "OR " in sql


def test_calls_use_effective_site_overlay():
    sql = step6._calls_select("2026-01-01", "2026-01-02")

    assert "ad_analytics.gsheet_sites_effective" in sql
    assert "LEFT JOIN reference_data.gsheet_sites gs" not in sql
