import inspect

import pipeline
import refresh_powerbi
from data_check import verify_big_analytics
from star_refactor import build_pbi_compat, build_star, build_star_extensions, cleanup_wide_intermediates
from direct_feed_funnel import build as direct_feed_build
from step10_crop_targeting import step10


def test_build_fact_materializes_site_key():
    select_sql, target_cols = build_star.build_fact_projection(
        [
            "Date",
            "CampaignId",
            "AdGroupId",
            "domain",
            "total_cost",
            "AdNetworkType",
            "Device",
            "источник",
            "manager_login",
        ]
    )

    assert "site_key" in target_cols
    assert "cityHash64(lowerUTF8(trim(BOTH ' ' FROM ifNull(domain, ''))))" in select_sql
    assert "toUInt64(0)) AS site_key" in select_sql


def test_build_fact_projects_new_dimension_keys_and_removes_duplicate_text_attrs():
    select_sql, target_cols = build_star.build_fact_projection(
        [
            "Date",
            "CampaignId",
            "AdGroupId",
            "domain",
            "account_login",
            "Название crm",
            "тип_заявки",
            "статус",
            "cascade_level",
            "салон",
            "город",
            "регион",
            "тип_сайта",
            "шаблон",
            "специалист",
            "проджект",
            "менеджер",
            "id_салона",
            "направление",
            "total_cost",
        ]
    )

    assert "account_key" in target_cols
    assert "crm_status_key" in target_cols
    assert "salon_key" in target_cols
    for column in [
        "account_login",
        "Название crm",
        "тип_заявки",
        "статус",
        "cascade_level",
        "салон",
        "город",
        "регион",
        "тип_сайта",
        "шаблон",
        "специалист",
        "проджект",
        "менеджер",
        "id_салона",
        "направление",
    ]:
        assert column not in target_cols
    assert "AS account_key" in select_sql
    assert "AS crm_status_key" in select_sql
    assert "AS salon_key" in select_sql


def test_dim_build_can_target_one_dimension():
    assert "Dim_Site" in build_star.DIM_DDL
    assert "Dim_AdGroup" in build_star.DIM_DDL
    assert "Dim_Account" in build_star.DIM_DDL
    assert "Dim_CRMStatus" in build_star.DIM_DDL
    assert "Dim_Salon" in build_star.DIM_DDL


def test_dim_date_year_month_is_russian_month_name():
    sql = build_star.DIM_DDL["Dim_Date"]

    assert "formatDateTime(`Date`, '%Y-%m')" not in sql
    assert "'Январь'" in sql
    assert "'Декабрь'" in sql
    assert "toMonth(`Date`)" in sql
    assert "month_key" in sql


def test_dim_adgroup_uses_narrow_raw_source_before_fact_fallback():
    sql = build_star.DIM_DDL["Dim_AdGroup"]

    assert "raw_data.direct_adgroups" in sql
    assert "ad_analytics.big_analytics_unified" in sql
    assert sql.index("raw_data.direct_adgroups") < sql.index("ad_analytics.big_analytics_unified")


def test_dim_site_uses_ba5_empty_crm_label():
    sql = build_star.DIM_DDL["Dim_Site"]

    assert "'Не указана'" in sql
    assert "CAST(ifNull(crm_name, ''), 'String') AS `Название crm`" not in sql


def test_dim_campaign_normalizes_kviz_to_quiz():
    ddl_sql = build_star.DIM_DDL["Dim_Campaign"]
    build_source = inspect.getsource(build_star.build_dim_campaign)

    assert "replaceAll(anyLast(campaign_code), 'kviz', 'quiz') AS campaign_code" in ddl_sql
    assert "replaceAll(anyLast(campaign_code), 'kviz', 'quiz') AS campaign_code" in build_source


def test_campaign_and_adgroup_default_to_single_merge_bucket():
    signature = inspect.signature(build_star.build_dim_campaign)
    source = inspect.getsource(build_star.build_dim_adgroup)

    assert signature.parameters["bucket_count"].default == 1
    assert "bucket_count = 1" in source


def test_direct_feed_fact_materializes_site_key():
    create_sql = direct_feed_build.fact_direct_feed_funnel_create_sql("target")
    insert_sql = direct_feed_build.fact_direct_feed_funnel_insert_sql("target", "2026-01-01", "2026-01-02")

    # Схема задана явно (FACT_WEIGHT_2026-08-14), а не выведена из CTAS-заглушки: контракт —
    # обе колонки существуют в факте физически и остаются UInt64.
    assert "`site_key` UInt64" in create_sql
    assert "`placement_feed_key_hash` UInt64" in create_sql
    assert "AS site_key" in insert_sql
    assert "cityHash64(placement_feed_key) AS placement_feed_key_hash" in insert_sql
    assert "GROUP BY date, campaign_id, ad_group_id, placement_feed_key_hash, account_login, site_key" in insert_sql


def test_direct_feed_fact_view_restores_placement_key_from_dimension():
    sql = direct_feed_build.fact_direct_feed_funnel_view_sql("source_table")

    assert "FROM source_table f" in sql
    assert "FROM ad_analytics.Dim_PlacementFeed" in sql
    assert "ifNull(pf.placement_feed_key_value, '') AS placement_feed_key" in sql
    assert "LEFT JOIN placement_feed pf ON pf.placement_feed_key_hash = f.placement_feed_key_hash" in sql


def test_direct_feed_builds_placement_dimension_before_compat_view():
    source = inspect.getsource(direct_feed_build.run)

    assert source.index("build_dim_placement_feed(client)") < source.index("replace_view(")


def test_direct_feed_pbi_view_joins_dim_site_by_materialized_site_key():
    sql = build_pbi_compat._feed_funnel_pbi_sql()

    assert "LEFT JOIN ad_analytics.Dim_Site ds ON ds.site_key = f.site_key" in sql
    assert "_site_key_expr(\"f\")" not in sql


def test_direct_feed_star_import_keeps_only_keys_and_metrics():
    sql = build_pbi_compat._feed_funnel_star_sql()
    dim_sql = build_pbi_compat._dim_placement_feed_pbi_sql()

    assert "FROM ad_analytics.fact_direct_feed_funnel_light f" in sql
    assert "p.placement_feed_id" in sql
    assert "site_key" in sql
    assert "LEFT JOIN placement_feed_ids p ON p.placement_feed_key_hash = f.placement_feed_key_hash" in sql
    assert "f.placement_feed_key AS placement_feed_key" not in sql
    assert "coalesce(nullIf(f.domain" not in sql
    assert "toInt64(0)" not in sql
    assert "toUInt32(row_number() OVER (ORDER BY placement_feed_key)) AS placement_feed_id" in dim_sql
    assert "cityHash64(placement_feed_key) AS placement_feed_key_hash" in dim_sql
    assert "fact_direct_feed_funnel_star" in build_pbi_compat.PBI_SOURCE_OBJECTS
    assert "fact_direct_feed_funnel_star" in build_pbi_compat.PBI_VIEW_SQL_BUILDERS


def test_feed_funnel_import_uses_global_pipeline_batches():
    source = inspect.getsource(build_pbi_compat.build_pbi_import_direct_feed_funnel)

    assert "day_ranges(DATE_FROM)" in source
    assert "range_batches(DATE_FROM, days=1)" not in source


def test_heavy_direct_ads_texts_is_not_in_selective_powerbi_refresh():
    assert "yandex_direct_ads_texts" not in refresh_powerbi._ALL_TABLES


def test_vk_ads_filters_use_raw_date_sort_key():
    star_source = inspect.getsource(build_star.build_vk_ads_fact)
    step10_source = inspect.getsource(step10._insert_vk_ads_costs)

    assert "toDateOrNull(s.date) >=" not in star_source
    assert "s.date >= " in star_source
    assert "WHERE toDateOrNull(date)" not in step10_source
    assert "WHERE date >=" in step10_source


def test_direct_cookie_sources_have_pbi_views():
    expected = {
        "yandex_direct_ads_texts",
        "yandex_direct_type_placement_report_master",
    }

    assert expected <= set(build_pbi_compat.PBI_SOURCE_OBJECTS)
    assert expected <= set(build_pbi_compat.PBI_VIEW_SQL_BUILDERS)
    assert "raw_data.direct_cookie_ads_texts_master" in build_pbi_compat._pbi_view_select_sql("yandex_direct_ads_texts")
    assert "raw_data.direct_cookie_type_placement_master" in build_pbi_compat._pbi_view_select_sql(
        "yandex_direct_type_placement_report_master"
    )


def test_pbi_full_restores_duplicate_text_attrs_from_new_dimensions():
    sql = build_pbi_compat._pbi_full_sql()

    assert "LEFT JOIN ad_analytics.Dim_Account da ON da.account_key = f.account_key" in sql
    assert "LEFT JOIN ad_analytics.Dim_CRMStatus dcs ON dcs.crm_status_key = f.crm_status_key" in sql
    assert "LEFT JOIN ad_analytics.Dim_Salon dsl ON dsl.salon_key = f.salon_key" in sql
    assert "concat(ifNull(da.account_login, ''), '|', ifNull(f.domain, '')) AS `аккаунт|сайт`" in sql
    assert "dcs.`Название crm`" in sql
    assert "dsl.`салон`" in sql
    assert "f.`салон`" not in sql
    assert "f.`специалист`" not in sql


def test_wide_compat_views_restore_duplicate_text_attrs_from_new_dimensions():
    sql = cleanup_wide_intermediates._wide_fact_sql("WHERE 1 = 1")

    assert "LEFT JOIN ad_analytics.Dim_Account dac ON dac.account_key = f.account_key" in sql
    assert "LEFT JOIN ad_analytics.Dim_CRMStatus dcs ON dcs.crm_status_key = f.crm_status_key" in sql
    assert "LEFT JOIN ad_analytics.Dim_Salon dsl ON dsl.salon_key = f.salon_key" in sql
    assert "concat(ifNull(dac.account_login, ''), '|', ifNull(f.domain, '')) AS `аккаунт|сайт`" in sql
    assert "dcs.`Название crm`" in sql
    assert "dsl.`салон`" in sql
    assert "f.account_login" not in sql
    assert "f.`салон`" not in sql
    assert "f.cascade_level" not in sql


def test_drop_fact_compat_objects_drops_by_actual_engine(monkeypatch):
    engines = {
        "view_object": "View",
        "table_object": "MergeTree",
    }
    commands = []

    class FakeClient:
        def command(self, sql, settings=None):  # noqa: ARG002
            commands.append(sql)

    monkeypatch.setattr(build_star, "FACT_SWAP_COMPAT_OBJECTS", ["view_object", "table_object", "missing_object"])
    monkeypatch.setattr(
        build_star,
        "table_engine",
        lambda client, database, table: engines.get(table),
    )

    build_star.drop_fact_compat_objects(FakeClient())

    assert commands == [
        "DROP VIEW IF EXISTS ad_analytics.`view_object` SYNC",
        "DROP TABLE IF EXISTS ad_analytics.`table_object` SYNC",
    ]


def test_build_star_runs_extension_dimensions_before_fact_swap(monkeypatch):
    calls = []

    class FakeClient:
        pass

    monkeypatch.setattr(build_star, "get_client", lambda: FakeClient())
    monkeypatch.setattr(build_star, "table_exists", lambda client, database, table: True)
    monkeypatch.setattr(build_star, "build_dims", lambda client: calls.append("dims") or {"Dim_Date": 1})
    monkeypatch.setattr(
        build_star,
        "build_extension_dims",
        lambda client: calls.append("extension_dims") or {"Dim_Source": 1},
    )
    monkeypatch.setattr(build_star, "build_vk_ads_fact", lambda client: calls.append("vk_fact") or 1)
    monkeypatch.setattr(build_star, "build_vk_dims", lambda client: calls.append("vk_dims") or {"Dim_VkAdPlan": 1})
    monkeypatch.setattr(build_star, "build_ml_korrektirovki_fact", lambda client: calls.append("ml_fact") or 1)
    monkeypatch.setattr(build_star, "build_fact", lambda client: calls.append("fact") or 1)

    build_star.run()

    assert calls.index("extension_dims") < calls.index("fact")


def test_build_star_extensions_preserves_wide_sourced_dims_when_unified_is_absent(monkeypatch):
    calls = []
    existing_rows = {
        "Dim_AdNetworkType": 3,
        "Dim_Device": 5,
        "Dim_Source": 11,
    }

    class FakeClient:
        pass

    monkeypatch.setattr(build_star_extensions, "get_client", lambda: FakeClient())
    monkeypatch.setattr(
        build_star_extensions,
        "table_exists",
        lambda client, database, table: table in existing_rows,
    )
    monkeypatch.setattr(
        build_star_extensions,
        "count_rows",
        lambda client, table: existing_rows[table.split(".")[-1].strip("`")],
    )
    monkeypatch.setattr(build_star_extensions, "build_dim_adformat", lambda client: calls.append("adformat") or 5)

    result = build_star_extensions.run()

    assert calls == ["adformat"]
    assert "Dim_AdNetworkType=3" in result["details"]
    assert "Dim_Device=5" in result["details"]
    assert "Dim_Source=11" in result["details"]


def test_pipeline_rebuilds_wide_compat_views_before_pbi_compat():
    step_numbers = [step[0] for step in pipeline.STEPS]

    assert step_numbers.index(145) < step_numbers.index(1451)
    assert step_numbers.index(1451) < step_numbers.index(148)
    assert step_numbers.index(148) < step_numbers.index(146)


def test_golden_kuderko_reads_specialist_from_dim_salon():
    sql = verify_big_analytics._golden_kuderko_sql("'direct'")

    assert "FROM ad_analytics.fact_big_analytics f" in sql
    assert "LEFT JOIN ad_analytics.Dim_Salon dsl ON dsl.salon_key = f.salon_key" in sql
    assert "dsl.`специалист` = {specialist:String}" in sql
    assert "f.`специалист`" not in sql
