from spec_fallback import _fallback_expr


def test_fallback_expr_classifies_calls_by_source_table_not_only_campaign_code():
    expr = _fallback_expr()

    assert "s.`_source_table` = 'calls'" in expr
    assert "s.campaign_code, '') = 'звонки'" in expr
