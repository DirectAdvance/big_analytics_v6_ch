import inspect

from step3_build_sources import step3
from step6_build_full import step6
from step11_pixel_score import step11
from step13_arrival import step13


def test_direct_rows_use_complex_direction() -> None:
    assert step3._direct_napravlenie_expr("yd.") == "'Комплекс'"


def test_calls_rows_use_complex_direction() -> None:
    step13_leads_sql = step13._leads_branch_sql("2026-01-01")
    step6_calls_sql = step6._calls_select("2026-01-01", "2026-01-02", crop=False)
    step13_calls_columns = step13._calls_branch_columns()
    step13_marcar_orphan_columns = step13._marcar_orphan_branch_columns()

    assert "'Комплекс'\n        ) AS `направление`" in step13_leads_sql
    assert "'Контекст'\n        ) AS `направление`" not in step13_leads_sql
    assert "'Комплекс' AS `направление`" in step6_calls_sql
    assert step13_calls_columns["направление"] == "'Комплекс'"
    assert step13_marcar_orphan_columns["направление"] == "'Комплекс'"


def test_step11_does_not_copy_raw_pixel_into_full() -> None:
    source = inspect.getsource(step11._rebuild_full_with_pixel)

    assert "ad_analytics.big_analytics_pixel_score" in source
    assert "s._source_table = 'pixel'" not in source
