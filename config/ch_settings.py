"""ClickHouse-specific constants for big_analytics_v6_ch."""

from __future__ import annotations

DATE_FROM = "2026-01-01"

CH_RAW_DB = "raw_data"
CH_WORK_DB = "ad_analytics"

RAW_SOURCE_TABLES = {
    "yandex": f"{CH_RAW_DB}.yandex_direct_report_rows",
    "leads": f"{CH_RAW_DB}.leads_all",
    "domains": f"{CH_RAW_DB}.domains",
    "crm_statuses": f"{CH_RAW_DB}.crm_status_mapping",
}

RAW_TARGET_TABLES = {
    "raw_yandex": f"{CH_WORK_DB}.raw_yandex",
    "raw_leads": f"{CH_WORK_DB}.raw_leads",
    "raw_calls": f"{CH_WORK_DB}.raw_calls",
    "raw_domains": f"{CH_WORK_DB}.raw_domains",
    "raw_perform_leads": f"{CH_WORK_DB}.raw_perform_leads",
}

EXCLUDED_DOMAIN_IDS = (1645, 883)

MINUS_SNAPSHOT_BLOCKS = ["tp2", "tp4"]
