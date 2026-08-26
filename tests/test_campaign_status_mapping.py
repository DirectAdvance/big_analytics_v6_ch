from pathlib import Path


def test_campaign_status_state_precedes_status():
    sql = Path("step4_campaign_status/step4.py").read_text(encoding="utf-8")

    archive_pos = sql.index("upper(ifNull(state, '')) = 'ARCHIVED'")
    stopped_pos = sql.index("upper(ifNull(state, '')) IN ('OFF', 'SUSPENDED')")
    active_pos = sql.index("upper(ifNull(status, '')) IN ('ACCEPTED', 'ACTIVE')")

    assert archive_pos < active_pos
    assert stopped_pos < active_pos
