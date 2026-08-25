import inspect

import pipeline
from star_refactor.cleanup_wide_intermediates import SOURCE_VIEWS
from step10_crop_targeting import step10
from step10_crop_targeting.step10 import CROP_TYPES_SQL
from step13_arrival.step13 import _calls_branch_columns, _calls_branch_sql, _leads_branch_sql
from step3_build_sources.step3 import (
    CROP_SOURCE_TYPES,
    _build_crop_sql_batched,
    _build_direct_sql,
    _build_direct_zero_sql,
    _build_seo_sql,
)
from step3_build_sources.step3 import _leads_deduped_cte
from step5_build_pixel.build_pixel import _build_pixel_insert_sql
from step6_build_full.step6 import _calls_select


def test_regular_calls_use_ba5_context_or_seo_source():
    sql = _calls_select("2026-01-01", "2026-01-02", crop=False)

    assert "gs.status = 'SEO Flow', 'SEO Flow'" in sql
    assert "gs.status = 'SEO', 'SEO'" in sql
    assert "FROM ad_analytics.gsheets_crop_targeting_account" in sql
    assert "ifNull(gs.direction_main, '') = 'Посевы'" in sql
    assert "FROM ad_analytics.big_analytics_sources" not in sql
    assert "FROM ad_analytics.big_analytics_crop_targeting" not in sql
    assert "CAST('звонки', 'Nullable(String)') AS campaign_code" in sql


def test_crop_calls_include_ba5_crop_account_domains():
    sql = _calls_select("2026-01-01", "2026-01-02", crop=True)

    assert "'Посевы_Звонки'" in sql
    assert "FROM ad_analytics.gsheets_crop_targeting_account" in sql
    assert "OR" in sql
    assert "gs.vk_client_id" in sql


def test_arrival_calls_reuse_ba5_call_source_labels():
    columns = _calls_branch_columns()
    sql = _calls_branch_sql("2026-01-01")

    assert columns["источник"] == "g.source_label"
    assert "'Посевы_Звонки'" in sql
    assert "'SEO Flow'" in sql
    assert "'SEO'" in sql
    assert "'Контекст'" in sql
    assert "FROM ad_analytics.big_analytics_calls" in sql
    assert "pcm.dom IS NOT NULL" in sql
    assert '"источник": "\'Звонки\'"' not in repr(columns)


def test_arrival_calls_drop_stale_specialist_after_account_cutoff():
    columns = _calls_branch_columns()
    sql = _calls_branch_sql("2026-01-01")

    assert "g.account_specialist_raw" in columns["специалист"]
    assert "account_specialist_raw" in sql
    assert "LEFT JOIN gs_account ga" in sql
    assert "2026-04-10" in columns["специалист"]


def test_arrival_leads_keep_posev_channel_sources():
    sql = _leads_branch_sql("2026-01-01")

    assert "lowerUTF8(ifNull(l.utm_source, '')) = 'max', 'Посевы_Max'" in sql
    assert "lowerUTF8(ifNull(l.utm_source, '')) IN ('vk', 'vk_groups', 'vk_storis'), 'Посевы_VK'" in sql
    assert "'Посевы_SEO'" in sql
    assert "'SEO Flow'" in sql


def test_lead_claim_type_preserves_ba5_cdr_bucket():
    direct_sql = _build_direct_sql("tmp_direct")
    crop_sql = _build_crop_sql_batched("AND l.created_date >= toDate('2026-01-01')")
    arrival_sql = _leads_branch_sql("2026-01-01")

    assert "GROUP BY key3, zvonki_cdr" in direct_sql
    assert "anyLast(zvonki_cdr)" not in direct_sql
    for sql in (direct_sql, crop_sql, arrival_sql):
        assert "'(^|[^a-z0-9])cdr([^a-z0-9]|$)'" in sql
        assert "'Звонки_CDR'" in sql


def test_seo_keeps_crop_seo_and_seo_flow_sources():
    sql = _build_seo_sql("AND l.created_date >= toDate('2026-01-01')")

    assert "'Посевы_SEO'" in sql
    assert "ifNull(gs.direction_main, '') = 'Посевы', 'Посевы_SEO'" in sql
    assert "gs.direction_main," in sql
    assert "gs.status = 'SEO Flow', 'SEO Flow'" in sql
    assert "AND lowerUTF8(trim(ifNull(domain, ''))) NOT IN" not in sql


def test_posev_repaint_domains_do_not_depend_on_active_crop_gate():
    sql = _build_direct_zero_sql("2026-01-01", "2026-01-02")

    assert "posev_repaint_domains AS" in sql
    assert "gs.direction_main = 'Посевы'" in sql
    assert "posev_active_domains" not in sql
    assert "match(ifNull(utm_campaign, ''), '(?i)tp(8|9|10)_(cpc|cpa)_')" not in sql


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
    assert "concat('Посевы_'" not in sql


def test_telega_api_unknown_source_does_not_create_domain_source():
    assert "concat('Посевы_'" not in step10._API_SOURCE
    assert "'Посевы_Telegram'" in step10._API_SOURCE


def test_lead_sources_fill_crm_from_source_type_not_domain_lookup():
    sql = _build_crop_sql_batched("AND l.created_date >= toDate('2026-01-01')")

    assert "l.source_type = 'crmf_excel', 'Фаиг'" in sql
    assert "l.source_type = 'rivendell_excel', 'Ривендел'" in sql
    assert "l.source_type = 'perform_api', 'Ривендел'" in sql
    assert "AS `Название crm`" in sql
    assert "crm.crm_name AS `Название crm`" not in sql


def test_crop_cost_overlays_fill_crm_from_domain_source_type():
    source = inspect.getsource(step10)

    assert "def _crm_by_domain_cte" in source
    assert "ifNull(nullIf(crm.crm_name, ''), 'Не указана') AS `Название crm`" in source
    assert "'' AS `Название crm`" not in source


def test_pixel_uses_hybrid_source_and_raw_status_funnel():
    sql = _build_pixel_insert_sql("ad_analytics.big_analytics_sources_new")

    assert "FROM (SELECT * FROM reference_data.victory_answers FINAL) AS v" in sql
    assert "v.product = 'пиксель'" in sql
    assert "2026-06-03" in sql
    assert "raw_data.leads_all AS l" in sql
    assert "raw_data.gsheet_autosalony_clients AS answer_salon_client" in sql
    assert "raw_data.gsheet_autosalony_clients AS legacy_salon_client" in sql
    assert "raw_data.gsheet_autosalony_clients AS matched_salon_client" in sql
    assert "extract(ifNull(v.salon, ''), '^([A-Za-z]+_[0-9]+)')" in sql
    assert "extract(ifNull(l.salon, ''), '^([A-Za-z]+_[0-9]+)')" in sql
    assert "ad_analytics.local_pixel_config AS pc" in sql
    assert "ad_analytics.local_pixel_price_history AS h" in sql
    assert "row_number() OVER" in sql
    assert "right(replaceRegexpAll(ifNull(l.phone, ''), '[^0-9]', ''), 10) = v.phone" in sql
    assert "toYYYYMM(l.created_date) = toYYYYMM(v.answer_date)" in sql
    assert "v.cost AS total_cost" in sql
    assert "toDecimal64(if(ifNull(m.status, '') != '', 1, 0), 6) AS kol_vo_zayavok" in sql
    assert "toDecimal64(0, 6) AS prodazhi" not in sql


def test_crop_source_views_use_the_same_ba5_source_types():
    for source_type in CROP_SOURCE_TYPES:
        assert f"'{source_type}'" in CROP_TYPES_SQL
    assert SOURCE_VIEWS["big_analytics_crop_targeting"] == CROP_SOURCE_TYPES


def test_step3_does_not_apply_lider_crmf_special_dedup():
    sql = _leads_deduped_cte()

    assert "lider_mauto_phones" not in sql
    assert "source_type = 'crmf_excel'" not in sql
    assert "salon = 'Лидер'" not in sql


def test_step10a_rebuilds_crop_gate_without_full_overlay():
    source = inspect.getsource(step10.run_crop_phase)

    assert "_rebuild_cost_overlays" in source
    assert "_overlay_full" not in source


def test_pipeline_runs_step10a_before_step6():
    source = inspect.getsource(pipeline.main)

    assert "step_num == 6" in source
    assert "run_step10a_before_step6" in source
