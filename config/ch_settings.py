"""ClickHouse-specific constants for big_analytics_v6_ch."""

from __future__ import annotations

DATE_FROM = "2026-01-01"

CH_RAW_DB = "raw_data"
CH_REF_DB = "reference_data"
CH_WORK_DB = "ad_analytics"
GSHEET_SITES_EFFECTIVE = f"{CH_WORK_DB}.gsheet_sites_effective"

RAW_SOURCE_TABLES = {
    "yandex": f"{CH_RAW_DB}.yandex_direct_report_rows",
    "leads": f"{CH_RAW_DB}.leads_all",
    "domains": f"{CH_REF_DB}.domains",
    "crm_statuses": f"{CH_REF_DB}.crm_status_mapping",
}

RAW_TARGET_TABLES = {
    "raw_yandex": f"{CH_WORK_DB}.raw_yandex",
    "raw_leads": f"{CH_WORK_DB}.raw_leads",
    "raw_calls": f"{CH_WORK_DB}.raw_calls",
    "raw_domains": f"{CH_WORK_DB}.raw_domains",
    "raw_perform_leads": f"{CH_WORK_DB}.raw_perform_leads",
}

# EXCLUDED_DOMAIN_NAMES_2026-08-06: домены-«мусор», которые нужно выкинуть из raw_leads /
# raw_perform_leads. ⚠️ ИСТОРИЯ БАГА: здесь раньше стояло `EXCLUDED_DOMAIN_IDS = (1645, 883)`,
# буквально скопированное из v5 (`work/big_analytics_v5/config/settings.py:41`). Числовой
# `domain_id` НЕ переносится между PostgreSQL (v5) и ClickHouse (v6) — своя нумерация в каждой
# системе:
#   id=883  в v5  → victory-crm.ru      (тестовый, ДОЛЖЕН исключаться)
#   id=883  в v6  → multiautos-23.ru    (реальный клиент — ошибочно исключался)
#   id=1645 в v6  → rt-avtomarket-geely.ru (реальный клиент — ошибочно исключался)
#   id=17478 в v6 → victory-crm.ru      (реальный «мусор» — НЕ исключался вообще)
# Итог замера 2026-08-06: в raw_leads молча тёк тестовый домен (170 287 лидов за 2026, из
# них 151 961 plex_excel/Заявка), а 2 живых клиента молча выбрасывались. Правило теперь —
# фильтровать по ИМЕНИ (case-insensitive), не по id: step1_load_raw/step1.py матчит по
# `d.domain` через уже существующий JOIN на reference_data.domains, число id в сравнении не участвует.
EXCLUDED_DOMAIN_NAMES = ("victory-crm.ru",)

# ОТКРЫТЫЙ ВОПРОС (не переносить вслепую!): в v5-комментарии `1645` значился как
# «priezd shared key3» (домен, деливший key3 с другим доменом и дублировавший лиды в Директе).
# В текущей v5 БД id=1645 не существует (диапазон id 1322..3953 отсутствует целиком — старое
# массовое удаление доменов, не специфичное для 1645), git-история v5 начинается с одного
# init-коммита (без более ранних правок этой константы), а исходники v3/v4 на диске не найдены —
# восстановить ИМЯ домена не удалось. Поэтому он НЕ перенесён в EXCLUDED_DOMAIN_NAMES выше.
# См. KNOWN_ISSUES.md #33.


MINUS_SNAPSHOT_BLOCKS = ["tp2", "tp4"]

# VK_AUTO_ACCOUNT_SCOPE_2026-08-05
# `raw_data.vk_ads_stats_day` — весь агентский кабинет ВК Рекламы (100 account_id, все ниши:
# медцентры, недвижимость, юристы…). В витрины «Авто» должны попадать ТОЛЬКО свои Авто-клиенты.
#
# В v5 скоуп задавался на шаге 0: `local_vk_ads_stats_day` наполнялся с фильтром
# `account_id IN (SELECT vk_client_id FROM local_gsheet_sites WHERE niche='Авто')`
# (`work/big_analytics_v5/step0_sync_local/step0.py:699-720`), и все потребители читали уже
# суженную таблицу. В v6_ch step0 собирает `ad_analytics.gsheet_sites_effective`
# из CH-справочника + PG overlay для доменов, которых нет/которые без directologist.
#
# Замер 2026-08-05: скоуп даёт 4 аккаунта с расходом (1090518071 autostock.ru,
# 1090694251 autodrive-102.site, 1090694302 autopro-116.site, 1090694347 autocenter-152.site)
# и 98 уникальных banner_id — ровно как в v5 (`public.fact_vk_ads`: 4 / 98).
VK_AUTO_ACCOUNTS_SQL = f"""
    SELECT DISTINCT toInt64OrNull(vk_client_id) AS account_id
    FROM {GSHEET_SITES_EFFECTIVE}
    WHERE niche = 'Авто'
      AND toInt64OrNull(vk_client_id) IS NOT NULL
"""
