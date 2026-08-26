from spec_fallback import _fallback_expr


def test_fallback_expr_does_not_classify_calls_as_specialist():
    expr = _fallback_expr()

    assert "'Звонки'" not in expr
    assert "'Без специалиста'" in expr
