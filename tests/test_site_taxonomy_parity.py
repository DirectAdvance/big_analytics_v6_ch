import inspect

import corrections
from step3_build_sources import step3
from step6_build_full import step6
from step11_pixel_score import step11
from step13_arrival import step13


def test_direct_rows_use_complex_direction() -> None:
    assert step3._direct_napravlenie_expr("yd.") == "'Комплекс'"


def test_direct_tp_rows_use_posevy_sources() -> None:
    assert "tp8" in step3._direct_source_expr("yd.")
    assert "'Посевы_Telegram'" in step3._direct_source_expr("yd.")
    assert "'Посевы_Max'" in step3._direct_source_expr("yd.")
    assert "Посевы_Telegram+Max" in step3._direct_source_expr("yd.")

    direct_sql = step3._build_direct_sql("ad_analytics.big_analytics_direct")
    cascade_sql = step3._build_direct_cascade_sql("2026-01-01", "2026-01-02")
    assert "Посевы_Max" in direct_sql
    assert "Посевы_Max" in cascade_sql


def test_corrections_keep_perform_direction() -> None:
    labels_sql = corrections._stage6_labels()

    assert "id_салона`, '') = 'avto_0415'" in labels_sql
    assert "s.`салон`, '') = 'Перформ РФ'" in labels_sql
    assert "'Перформ'" in labels_sql


def test_perform_vk_has_dedicated_source_branch() -> None:
    crop_sql = step3._build_crop_sql_batched()
    vk_sql = step3._perform_vk_insert_sql("ad_analytics.big_analytics_sources_new")

    assert "utm_source, '') = 'vkads'" in step3._perform_vk_filter()
    assert "utm_campaign, '') = 'victory'" in step3._perform_vk_filter()
    assert "AND NOT" in crop_sql
    assert "vk_perform" in vk_sql
    assert "'VK Ads'" in vk_sql
    assert "'Перформ'" in vk_sql
    assert "'avto_0415'" in vk_sql


def test_reviews_without_site_get_stable_domain() -> None:
    source = inspect.getsource(step3._fetch_reviews_rows_from_postgres)

    assert "AS review_domain" in source
    assert "'reviews-' || REPLACE(LOWER(TRIM(COALESCE(r.login, 'unknown'))), '_', '-') || '.local'" in source
    assert "COALESCE(r.login, '') || '|' || COALESCE(r.review_domain, '')" in source


def test_perform_api_uses_live_crm_status_mapping() -> None:
    coverage_source = inspect.getsource(step3.check_crm_mapping_coverage)

    assert step3.CRM_BY_SOURCE_TYPE["perform_api"] == "rivendell"
    assert "ad_analytics.raw_perform_leads" in coverage_source
    assert step3.CODE_SOURCE_STATUS_CATEGORY[("perform_api", "Приехал")] == "visit"
    assert step3.CODE_SOURCE_STATUS_CATEGORY[("perform_api", "Продажа в кредит")] == "sale"
    assert step3.CODE_SOURCE_STATUS_CATEGORY[("perform_api", "Отказ клиента")] == "qualified"


def test_step13_adds_perform_posevy_visit_proxy() -> None:
    branches = {name: sql for name, _columns, sql in step13.build_branches("2026-01-01")}
    columns = step13._perform_posevy_proxy_columns()
    leads_sql = step13._leads_branch_sql("2026-01-01")

    assert "perform_posevy_proxy" in branches
    assert "`направление` = 'Перформ'" in branches["perform_posevy_proxy"]
    assert "'Посевы_Telegram'" in branches["perform_posevy_proxy"]
    assert "FROM ad_analytics.big_analytics_full" in leads_sql
    assert "ifNull(`направление`, '') = 'Перформ'" in leads_sql
    assert columns["kol_vo_zayavok"] == "toDecimal64(0, 6)"
    assert columns["priezd"] == "g.priezd"


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


def test_step11_copies_raw_pixel_into_full() -> None:
    """PIXEL_DEDUP_2026-08-15: only the raw 'pixel' copy (block 3) lands in
    big_analytics_full now. The attributed `пиксель_атрибуц` copy (old block 2,
    reading from `ad_analytics.big_analytics_pixel_score`) duplicated the same
    leads/cost as the raw copy and was removed — `big_analytics_pixel_score`
    stays a standalone table read directly by step13 and PBI, not re-merged
    into big_analytics_full."""
    source = inspect.getsource(step11._rebuild_full_with_pixel)

    assert "ad_analytics.big_analytics_pixel_score" not in source
    assert "s._source_table = 'pixel'" in source
