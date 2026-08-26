from pathlib import Path


def test_verify_feed_funnel_checks_use_bi_view_with_credit_columns():
    source = Path("data_check/verify_big_analytics.py").read_text(encoding="utf-8")
    checks_block = source.split('"feed_funnel_dohod_do_kredita_gt_priezd"', 1)[1]
    checks_block = checks_block.split('"ml_korrektirovki_funnel_dohod_do_kredita_gt_priezd"', 1)[0]

    assert "ad_analytics.bi_fact_direct_feed_funnel" in checks_block
    assert "ad_analytics.fact_direct_feed_funnel WHERE dohod_do_kredita" not in checks_block
    assert "`Приезд_feed`" not in checks_block
    assert "`Продажи_feed`" not in checks_block
