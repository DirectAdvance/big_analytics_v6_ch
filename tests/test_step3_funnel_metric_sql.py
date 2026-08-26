from step3_build_sources import step3


def test_sale_forces_credit_and_approval_funnel_steps():
    sql = step3._metric_expr("status", "reason", "source_type", "salon")

    assert "AS dohod_do_kredita" in sql
    assert "AS dobro" in sql
    assert "OR " in sql
