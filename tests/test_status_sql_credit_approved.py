from config.status_sql import _group_by_category, _status_reach
from step3_build_sources import step3


def test_sale_status_contributes_to_approved_status_side():
    by_cat = _group_by_category([
        ("Продажа", "sale", "", "", "status"),
    ])

    assert "Продажа" in by_cat["approved"][("", "", "status")]


def test_sale_reach_includes_approved_credit_visit_qualified_correct():
    assert {"sale", "approved", "credit", "visit", "qualified", "correct"} <= _status_reach("sale")


def test_reason_metrics_lowercase_cyrillic_with_lower_utf8():
    sql = step3._metric_expr("status", "reason", "source_type", "salon")

    assert "lowerUTF8(ifNull(reason, ''))" in sql
    assert "lower(ifNull(reason, ''))" not in sql
    assert "'был в ксо'" in sql
