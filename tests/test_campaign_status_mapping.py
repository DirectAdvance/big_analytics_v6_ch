from pathlib import Path


def test_campaign_status_state_precedes_status():
    sql = Path("step4_campaign_status/step4.py").read_text(encoding="utf-8")

    archive_pos = sql.index("upper(ifNull(dc.state, '')) = 'ARCHIVED'")
    stopped_pos = sql.index("upper(ifNull(dc.state, '')) IN ('OFF', 'SUSPENDED')")
    active_pos = sql.index("upper(ifNull(dc.status, '')) IN ('ACCEPTED', 'ACTIVE') AND upper(ifNull(dc.state, '')) = 'ON'")

    assert archive_pos < active_pos
    assert stopped_pos < active_pos


def test_campaign_active_requires_active_auto_account():
    sql = Path("step4_campaign_status/step4.py").read_text(encoding="utf-8")

    assert "active_auto_accounts" in sql
    assert "ifNull(niche, '') = 'Авто'" in sql
    assert "ifNull(status, '') = 'Контекст активно'" in sql
    assert "aa.account_login IS NULL" in sql
    assert "OR upper(ifNull(dc.state, '')) = 'ON', 'Активна'" not in sql
