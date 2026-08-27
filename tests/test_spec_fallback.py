from spec_fallback import _fallback_expr


def test_fallback_expr_classifies_unmapped_calls_as_calls_specialist():
    expr = _fallback_expr()

    assert "ifNull(s.campaign_code, '') = 'звонки'" in expr
    assert "s.`_source_table` = 'calls'" in expr
    assert "ifNull(s.`источник`, '') = 'Посевы_Звонки'" in expr
    assert "'Звонки'" in expr
    assert "'Без специалиста'" in expr
