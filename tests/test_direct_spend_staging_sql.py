from pathlib import Path


def test_direct_spend_staging_does_not_read_removed_raw_crm_columns():
    sql = Path("spend/build_direct_spend_staging.py").read_text(encoding="utf-8")
    insert_sql = sql.split("INSERT INTO {target}", 1)[1]

    assert "ifNull(crm_spam_order" not in insert_sql
    assert "ifNull(crm_order_canceled" not in insert_sql
    assert "toDecimal128(0, 9) AS crm_spam_order" in insert_sql
    assert "toDecimal128(0, 9) AS crm_order_canceled" in insert_sql
