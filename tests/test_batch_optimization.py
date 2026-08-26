import inspect

from step10_crop_targeting import step10


def test_crop_overlay_full_uses_global_pipeline_batches():
    source = inspect.getsource(step10._overlay_full)

    assert "day_ranges(DATE_FROM)" in source
    assert "range_batches(DATE_FROM, days=1)" not in source


def test_telega_api_metrics_use_ba5_kval_formula():
    sql = step10._telega_api_metric_expr("status")

    assert "AS korr" in sql
    assert "- toInt64(ifNull(status, '') IN ('Не отвечает', 'Новая: Не отвечает'))" in sql
    assert "- toInt64(ifNull(status, '') = 'Фильтр')" in sql
    assert "- toInt64(ifNull(status, '') = 'Недозвон') AS kval" in sql
    assert "AS dohod_do_kredita" in sql
    assert "AS dobro" in sql


def test_crop_overlay_removes_telega_covered_raw_posev_rows():
    source = inspect.getsource(step10._overlay_full)
    covered = step10._telega_covered_raw_keys("2026-05-01", "2026-05-02")

    assert "_source_table IN ('social_посевы', 'telegram')" in source
    assert "key3 IN ({_telega_covered_raw_keys(lo, hi)})" in source
    assert "FROM ad_analytics.local_telega_in_orders o" in covered
    assert "FROM ad_analytics.raw_leads l" in covered


def test_crop_overlays_keep_sales_inside_approval_funnel():
    source = inspect.getsource(step10._insert_crop_gsheet_costs) + inspect.getsource(step10._insert_crop_api_costs)

    assert '_gs_metric_int("prodazhi")' in source
    assert 'greatest(ifNull(t.dohod_do_kredita, 0), {_api_metric_int("prodazhi")})' in source
    assert 'greatest(ifNull(t.dobro, 0), {_api_metric_int("prodazhi")})' in source
