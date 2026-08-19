import inspect

from step_cron_night import metrika_raw_builders


def test_direct_tracking_class_uses_v5_template_parts():
    sql = metrika_raw_builders._direct_tracking_class_sql("tracking_params")

    assert "utm_source=s:{source}" in sql
    assert "utm_campaign={campaign_id}|{campaign_name}" in sql
    assert "utm_content=g:{gbid}" in sql
    assert "utm_term={keyword}" in sql
    assert "utm_term={phrase}" in sql
    assert "'НЕТ_UTM'" in sql
    assert "'ДРУГОЙ_UTM'" in sql
    assert "'OK'" in sql


def test_check_utm_builds_direct_group_audit_not_utm_visit_dump():
    source = inspect.getsource(metrika_raw_builders.build_check_utm)

    assert "reference_data.direct_adgroups" in source
    assert "reference_data.direct_campaigns" in source
    assert "raw_data.yandex_direct_report_rows" in source
    assert "raw_data.metrika_yandex_utm_daily" not in source
