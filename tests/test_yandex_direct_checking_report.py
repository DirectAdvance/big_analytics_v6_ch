from pathlib import Path


def test_checking_report_uses_raw_data_total_cost_not_direct_api():
    source = Path("yandex_direct_checking_report/report.py").read_text()

    assert "raw_data.yandex_direct_report_rows" in source
    assert "raw_data.gsheet_sites" in source
    assert "sum(rr.total_cost)" in source
    assert "api.direct.yandex.com/json/v5/reports" not in source
    assert "OAUTH_TOKEN_" not in source
