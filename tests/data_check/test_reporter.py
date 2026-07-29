import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from data_check.reporter import format_report


def test_report_shows_critical_count():
    results = {
        'projects': {'only_in_sheets': ['site1.ru', 'site2.ru'], 'only_in_db': [], 'no_analytics_data': []},
        'fields': {'null_directologist': [], 'null_manager_login': [], 'null_login_key': []},
        'spending': {'spend_no_leads_7d': [], 'no_spend_7d': [], 'per_project': []},
        'funnel': {'invariant_violations': [], 'zero_funnel_active': [], 'per_project': []},
        'freshness': {'last_pipeline_run': '2026-05-18 10:00:00', 'hours_ago': 2, 'stale': False},
    }
    report = format_report(results)
    assert 'site1.ru' in report
    assert 'site2.ru' in report


def test_report_shows_stale_warning():
    results = {
        'projects': {'only_in_sheets': [], 'only_in_db': [], 'no_analytics_data': []},
        'fields': {'null_directologist': [], 'null_manager_login': [], 'null_login_key': []},
        'spending': {'spend_no_leads_7d': [], 'no_spend_7d': [], 'per_project': []},
        'funnel': {'invariant_violations': [], 'zero_funnel_active': [], 'per_project': []},
        'freshness': {'last_pipeline_run': '2026-05-17 10:00:00', 'hours_ago': 26, 'stale': True},
    }
    report = format_report(results)
    assert '26' in report or 'устарел' in report.lower() or 'PBI' in report
