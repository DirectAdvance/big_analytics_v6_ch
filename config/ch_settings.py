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

# VK_AUTO_ACCOUNT_SCOPE_2026-08-05
# `raw_data.vk_ads_stats_day` — весь агентский кабинет ВК Рекламы (100 account_id, все ниши:
# медцентры, недвижимость, юристы…). В витрины «Авто» должны попадать ТОЛЬКО свои Авто-клиенты.
#
# В v5 скоуп задавался на шаге 0: `local_vk_ads_stats_day` наполнялся с фильтром
# `account_id IN (SELECT vk_client_id FROM local_gsheet_sites WHERE niche='Авто')`
# (`work/big_analytics_v5/step0_sync_local/step0.py:699-720`), и все потребители читали уже
# суженную таблицу. В v6_ch шага 0-синка нет, а в `raw_data.gsheet_sites` колонки `vk_client_id`
# ПРОСТО НЕТ — поэтому связка «VK-аккаунт → домен» берётся из реестра агентских клиентов
# `raw_data.vk_ads_agency_clients` (account_id → domain), а ниша — из `raw_data.gsheet_sites`.
#
# Замер 2026-08-05: скоуп даёт 4 аккаунта с расходом (1090518071 autostock.ru,
# 1090694251 autodrive-102.site, 1090694302 autopro-116.site, 1090694347 autocenter-152.site)
# и 98 уникальных banner_id — ровно как в v5 (`public.fact_vk_ads`: 4 / 98).
VK_AUTO_ACCOUNTS_SQL = f"""
    SELECT DISTINCT a.account_id
    FROM {CH_RAW_DB}.vk_ads_agency_clients AS a
    WHERE lowerUTF8(trim(ifNull(a.domain, ''))) IN (
        SELECT lowerUTF8(trim(ifNull(domain, '')))
        FROM {CH_RAW_DB}.gsheet_sites
        WHERE niche = 'Авто' AND ifNull(domain, '') != ''
    )
"""
