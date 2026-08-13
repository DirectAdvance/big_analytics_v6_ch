from config.status_sql import _group_by_category, _status_reach


def test_sale_status_contributes_to_approved_status_side():
    by_cat = _group_by_category([
        ("Продажа", "sale", "", "", "status"),
    ])

    assert "Продажа" in by_cat["approved"][("", "", "status")]


def test_sale_reach_includes_approved_credit_visit_qualified_correct():
    assert {"sale", "approved", "credit", "visit", "qualified", "correct"} <= _status_reach("sale")
