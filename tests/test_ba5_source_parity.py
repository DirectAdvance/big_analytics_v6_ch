from star_refactor.cleanup_wide_intermediates import SOURCE_VIEWS
from step10_crop_targeting.step10 import CROP_TYPES_SQL
from step13_arrival.step13 import _calls_branch_columns, _calls_branch_sql
from step3_build_sources.step3 import CROP_SOURCE_TYPES, _build_crop_sql_batched
from step6_build_full.step6 import _calls_select


def test_regular_calls_use_ba5_context_or_seo_source():
    sql = _calls_select("2026-01-01", "2026-01-02", crop=False)

    assert "multiIf(gs.status IN ('SEO', 'SEO Flow'), 'SEO', 'Контекст') AS `источник`" in sql
    assert "CAST('звонки', 'Nullable(String)') AS campaign_code" in sql


def test_arrival_calls_reuse_ba5_call_source_labels():
    columns = _calls_branch_columns()
    sql = _calls_branch_sql("2026-01-01")

    assert columns["источник"] == "g.source_label"
    assert "'Посевы_Звонки'" in sql
    assert "'SEO'" in sql
    assert "'Контекст'" in sql
    assert '"источник": "\'Звонки\'"' not in repr(columns)


def test_crop_leads_keep_channel_source_for_cost_overlay_aggregation():
    sql = _build_crop_sql_batched("AND l.created_date >= toDate('2026-01-01')")

    assert "lowerUTF8(ifNull(l.utm_source, '')) = 'max', 'Посевы_Max'" in sql
    assert "lowerUTF8(ifNull(l.utm_source, '')) IN ('vk', 'vk_groups', 'vk_storis'), 'Посевы_VK'" in sql
    assert "lowerUTF8(ifNull(l.utm_source, '')) = 'vkads', 'VK Ads'" in sql
    assert (
        "lowerUTF8(ifNull(l.utm_source, '')) IN ('telegram', 'stories_tg', 'telegram_storis', 'instagram'), "
        "'Посевы_Telegram'"
    ) in sql
    assert "ifNull(l.utm_source, '') IN ('telegram', 'stories_tg'), 'telegram'" in sql
    assert "'social_посевы'" in sql
    assert "'vk_ads'" in sql
    assert "'vk_zero'" in sql
    assert "'Посевы' AS `источник`" not in sql


def test_crop_source_views_use_the_same_ba5_source_types():
    for source_type in CROP_SOURCE_TYPES:
        assert f"'{source_type}'" in CROP_TYPES_SQL
    assert SOURCE_VIEWS["big_analytics_crop_targeting"] == CROP_SOURCE_TYPES
