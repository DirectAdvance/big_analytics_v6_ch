"""
step3_build_sources/step3.py — сборка таблиц по источникам

Создаёт 5 таблиц UNLOGGED (финализируются в шаге 6 через SET LOGGED):
  big_analytics_direct       — Яндекс Директ + tp8 + лиды директ
  big_analytics_seo          — лиды без UTM (не звонки)
  big_analytics_pixel        — лиды с utm_source LIKE 'victory_%' (пустая на старте)
  big_analytics_crop_targeting — посевы + лиды посевов + telegram-посевы
  big_analytics_reviews      — кампании отзывов (из yandex_direct_reports_reviews)
"""

import logging
import threading
import time

from config.settings import (
    WORK_MEM,
    CDR_PATTERN,  # CDR_SPLIT_2026-07-27: единая константа POSIX-паттерна CDR
    T_RAW_YANDEX, T_RAW_LEADS, T_RAW_CALLS, T_RAW_DOMAINS,
    T_RAW_PERFORM_LEADS,  # PERFORM_LEADS_2026-07-01
    T_GSHEET_SITES, T_GSHEET_NAMING,
    T_GSHEET_PLAN_FAKT, T_GSHEET_AUTOSALONY,
    T_GSHEET_PRIEZDI, T_LEADS_CROP_ATTR,
    T_DIRECT, T_SEO, T_PIXEL, T_CROP, T_REVIEWS,
    T_LOCAL_VK_ADS,  # VK_ADS_INTEGRATION_2026-07-06
)
from config.status_sql import load_status_sql

logger = logging.getLogger('pipeline.step3')

# STEP3_DIRECT_WORKMEM_2026-07-11: целевой work_mem ТОЛЬКО для big_analytics_direct CTAS.
# 4 GB (vs общий WORK_MEM=1999MB) — уменьшает hash/sort-спилл direct'а в pgsql_tmp, чтобы
# при ~29 GB free step3 попытался влезть без ENOSPC. Применяется СТРОГО в паре с
# max_parallel_workers_per_gather=0 (serial). Почему serial обязателен: на Victory
# hash_mem_multiplier=2 и max_parallel_workers_per_gather=2 → параллельный 4GB дал бы до
# 4GB × 2(hash_mem_multiplier) × 3(leader+2 worker) = 24 GB на ОДИН hash-узел; при Swap=0
# это OOM SIGKILL (а OOM-kill — главный источник ОСИРОТЕВШЕГО pgsql_tmp, который без root
# не вычистить). Serial бюджет памяти direct-CTAS ограничен ~work_mem×hash_mem_multiplier
# на узел (≈8 GB), с запасом под 30 GB avail. Цена — direct-CTAS медленнее (нет parallel),
# осознанный компромисс: безопасность памяти при Swap=0 важнее скорости одного шага.
_WM_DIRECT = '4096MB'  # CDR_OOM_FIX_2026-07-27: восстановлено — window-fn убраны, память не спиллит

# ── Общие CTE (переиспользуются в каждом источнике) ──────────────────────────

def _build_common_ctes(priezd_sql: str) -> str:
    """
    Вернуть блок общих CTE.
    priezd_sql — IN-список статусов visit+sale из local_crm_statuses
                 (используется для дедупликации дублей по phone+yclid).

    MATONCE_ACCOUNT_MANAGER_MAP_2026-06-18: account_manager_map и domain_source_type
    материализуются ОДИН РАЗ в run() как TEMP TABLE (_account_manager_map,
    _domain_source_type) до всех 5 CTAS. Здесь CTE просто читают из TEMP TABLE —
    никакого повторного скана raw_yandex/raw_leads/raw_calls.
    Результат идентичен: тот же MAX(manager_login)/ARRAY_AGG — вычислен однократно.
    TEMP TABLE дропаются автоматически при закрытии сессии (не плодим постоянные таблицы).
    """
    return f"""
-- ══════════════════════════════════════════════════════════
-- ПЛАН/ФАКТ (один раз для всех источников)
-- ══════════════════════════════════════════════════════════
plan_fakt_cte AS MATERIALIZED (
    SELECT "салон" AS pf_salon, "тип" AS pf_tip, "цена_заявки", "цена_приезда"
    FROM {T_GSHEET_PLAN_FAKT}
),

-- ══════════════════════════════════════════════════════════
-- МЕНЕДЖЕРЫ ПО АККАУНТУ — читаем из TEMP TABLE (материализована 1 раз в run())
-- MATONCE_ACCOUNT_MANAGER_MAP_2026-06-18: was 5× GROUP BY 19M raw_yandex rows
-- ══════════════════════════════════════════════════════════
account_manager_map AS MATERIALIZED (
    SELECT account_login, manager_login
    FROM _account_manager_map
),

-- ══════════════════════════════════════════════════════════
-- PERFORM-ДОМЕНЫ (PERFORM_LEADS_2026-07-01)
-- Домены perform-аккаунтов (client_id='avto_0415') — их лиды идут из raw_perform_leads,
-- а не из raw_leads, чтобы избежать двойного учёта.
-- ══════════════════════════════════════════════════════════
perform_domains AS MATERIALIZED (
    SELECT LOWER(TRIM(domain)) AS domain
    FROM {T_GSHEET_SITES}
    WHERE client_id = 'avto_0415'
),

-- ══════════════════════════════════════════════════════════
-- ДЕДУПЛИКАЦИЯ ЛИДОВ ПО phone + yclid
-- При дублях: оставляем "приездные/продажные" статусы (из local_crm_statuses)
-- PERFORM_LEADS_2026-07-01: источник = raw_leads (исключая perform-домены)
--   UNION ALL raw_perform_leads (только perform-домены)
-- ══════════════════════════════════════════════════════════
leads_deduped AS (
    SELECT id, key3, key3_arrival_date, source_type, status, reason, salon, campaign_id, group_id,
           correction_id, created_date, utm_content, utm_campaign, utm_source,
           utm_medium, deal_type, domain_id, domain, fid, phone, yclid
    FROM (
        SELECT l.*, 0::INT AS _hp, 1::INT AS _rn
        FROM (
            SELECT * FROM {T_RAW_LEADS}
            WHERE LOWER(TRIM(domain)) NOT IN (SELECT domain FROM perform_domains)
            UNION ALL
            SELECT * FROM {T_RAW_PERFORM_LEADS}
        ) l
        WHERE l.phone IS NULL OR l.phone = '' OR l.yclid IS NULL OR l.yclid = ''
        UNION ALL
        SELECT * FROM (
            SELECT l.*,
                MAX(CASE WHEN l.status IN ({priezd_sql}) THEN 1 ELSE 0 END)
                OVER (PARTITION BY l.phone, l.yclid) AS _hp,
                ROW_NUMBER() OVER (
                    PARTITION BY l.phone, l.yclid
                    ORDER BY
                        CASE WHEN l.status IN ({priezd_sql}) THEN 0 ELSE 1 END,
                        created_date
                ) AS _rn
            FROM (
                SELECT * FROM {T_RAW_LEADS}
                WHERE LOWER(TRIM(domain)) NOT IN (SELECT domain FROM perform_domains)
                UNION ALL
                SELECT * FROM {T_RAW_PERFORM_LEADS}
            ) l
            WHERE l.phone IS NOT NULL AND l.phone != ''
              AND l.yclid IS NOT NULL AND l.yclid != ''
        ) t
        WHERE (_hp = 1 AND status IN ({priezd_sql})) OR (_hp = 0 AND _rn = 1)
    ) dedup
),

-- ══════════════════════════════════════════════════════════
-- ТИП CRM ПО ДОМЕНУ — читаем из TEMP TABLE (материализована 1 раз в run())
-- MATONCE_DOMAIN_SOURCE_TYPE_2026-06-18: was 5× UNION raw_leads+raw_calls scans
-- Приоритет: Маркар=1 > Мега=2 > Фаиг=3 > Плекс=4 > прочие=9
-- ══════════════════════════════════════════════════════════
domain_source_type AS (
    SELECT domain_name, leads_source_type
    FROM _domain_source_type
)
"""

# ── ag_parts JOIN (нейминг групп) ─────────────────────────────────────────────

AG_PARTS_JOINS = f"""
LEFT JOIN {T_GSHEET_NAMING} n_ag1 ON n_ag1.type='ag_part1' AND LOWER(SPLIT_PART(adgroup_code,'_',1))=n_ag1.code
LEFT JOIN {T_GSHEET_NAMING} n_ag2 ON n_ag2.type='ag_part2' AND LOWER(SPLIT_PART(adgroup_code,'_',2))=n_ag2.code
LEFT JOIN {T_GSHEET_NAMING} n_ag3 ON n_ag3.type='ag_part3' AND LOWER(SPLIT_PART(adgroup_code,'_',3))=n_ag3.code
LEFT JOIN {T_GSHEET_NAMING} n_ag4 ON n_ag4.type='ag_part4' AND LOWER(SPLIT_PART(adgroup_code,'_',4))=n_ag4.code
LEFT JOIN {T_GSHEET_NAMING} n_ag5 ON n_ag5.type='ag_part5' AND LOWER(SPLIT_PART(adgroup_code,'_',5))=n_ag5.code
LEFT JOIN {T_GSHEET_NAMING} n_ag6 ON n_ag6.type='ag_part6' AND LOWER(SPLIT_PART(adgroup_code,'_',6))=n_ag6.code
LEFT JOIN {T_GSHEET_NAMING} n_ag7 ON n_ag7.type='ag_part7' AND LOWER(SPLIT_PART(adgroup_code,'_',7))=n_ag7.code
"""

# ── Колонки результирующих таблиц ─────────────────────────────────────────────
# Должны быть одинаковыми во всех источниках для UNION ALL в шаге 4

RESULT_COLUMNS = """
    key3, "Date", "День недели", week_start,
    "CampaignId", "CampaignName", "AdGroupId", "AdGroupName",
    "AdNetworkType", "Device", "Impressions", "Clicks", total_cost, domain,
    "RlAdjustmentId", "RlAdjustmentId_total",
    campaign_code, tp, cpc_cpa, site_quiz, adgroup_code,
    account_login, manager_login,
    ag_part1, ag_part2, ag_part3, ag_part4, ag_part5, ag_part6, ag_part7,
    "марки авто", "Название crm", тип_заявки,
    kol_vo_zayavok, korr, kval, priezd, prodazhi, nekorr, ne_otvechaet, filtr, nedozvon, priedet,
    dohod_do_kredita, dobro,
    "статус", "специалист", "тип_сайта", "шаблон", "салон", "город", "регион",
    direction,
    "неверный_кодер_new", fid,
    проджект, id_салона, менеджер,
    источник, направление,
    "номер кампании | название кампании",
    "номер группы | название группы",
    "План заявки", "План приезда",
    "аккаунт|сайт",
    priezd_arrival_date, prodazhi_arrival_date,
    поставщик, _source_table
"""


# ══════════════════════════════════════════════════════════════════════════════
# DDL для big_analytics_direct (явная схема, используется для IF NOT EXISTS)
# Типы взяты из information_schema на 2026-05-28.
# campaign_status и payment_model добавляются step4 — но включаем в DDL чтобы
# TRUNCATE+INSERT работал без ALTER TABLE при первом запуске.
# ══════════════════════════════════════════════════════════════════════════════

_DDL_DIRECT = f"""
CREATE UNLOGGED TABLE IF NOT EXISTS {T_DIRECT} (
    key3                                   TEXT,
    "Date"                                 DATE,
    "День недели"                          TEXT,
    week_start                             DATE,
    "CampaignId"                           BIGINT,
    "CampaignName"                         TEXT,
    "AdGroupId"                            BIGINT,
    "AdGroupName"                          TEXT,
    "AdNetworkType"                        TEXT,
    "Device"                               TEXT,
    "Impressions"                          NUMERIC,
    "Clicks"                               NUMERIC,
    total_cost                             NUMERIC,
    domain                                 TEXT,
    "RlAdjustmentId"                       BIGINT,
    "RlAdjustmentId_total"                 TEXT,
    campaign_code                          TEXT,
    tp                                     TEXT,
    cpc_cpa                                TEXT,
    site_quiz                              TEXT,
    adgroup_code                           TEXT,
    account_login                          TEXT,
    manager_login                          TEXT,
    ag_part1                               TEXT,
    ag_part2                               TEXT,
    ag_part3                               TEXT,
    ag_part4                               TEXT,
    ag_part5                               TEXT,
    ag_part6                               TEXT,
    ag_part7                               TEXT,
    "марки авто"                           TEXT,
    "Название crm"                         TEXT,
    тип_заявки                             TEXT,
    kol_vo_zayavok                         BIGINT,
    korr                                   BIGINT,
    kval                                   BIGINT,
    priezd                                 BIGINT,
    prodazhi                               BIGINT,
    nekorr                                 BIGINT,
    ne_otvechaet                           BIGINT,
    filtr                                  BIGINT,
    nedozvon                               BIGINT,
    priedet                                BIGINT,
    dohod_do_kredita                       BIGINT,
    dobro                                  BIGINT,
    "статус"                               TEXT,
    "специалист"                           TEXT,
    "тип_сайта"                            TEXT,
    "шаблон"                               TEXT,
    "салон"                                TEXT,
    "город"                                TEXT,
    "регион"                               TEXT,
    direction                              TEXT,
    "неверный_кодер_new"                   TEXT,
    fid                                    TEXT,
    проджект                               TEXT,
    id_салона                              TEXT,
    менеджер                               TEXT,
    источник                               TEXT,
    направление                            TEXT,
    "номер кампании | название кампании"   TEXT,
    "номер группы | название группы"       TEXT,
    "План заявки"                          INTEGER,
    "План приезда"                         INTEGER,
    "аккаунт|сайт"                         TEXT,
    priezd_arrival_date                    BIGINT,
    prodazhi_arrival_date                  BIGINT,
    поставщик                              TEXT,
    _source_table                          TEXT,
    -- CASCADE_MATCH_2026-07-03: уровень каскадного матчинга (4/3/2 или NULL = строгий/нет)
    cascade_level                          TEXT,
    campaign_status                        TEXT,
    payment_model                          TEXT
);
-- CASCADE_MATCH_2026-07-03: миграция существующей таблицы (IF NOT EXISTS пропускает DDL)
ALTER TABLE {T_DIRECT} ADD COLUMN IF NOT EXISTS cascade_level TEXT;
"""

# ══════════════════════════════════════════════════════════════════════════════
# big_analytics_direct
# ══════════════════════════════════════════════════════════════════════════════

def _build_direct_sql(brand_case_sql: str, status_cases: str, priezd_sql: str) -> str:
    """SQL сборки big_analytics_direct."""
    common_ctes = _build_common_ctes(priezd_sql)
    return f"""
{_DDL_DIRECT}
ALTER TABLE {T_DIRECT} SET UNLOGGED;
TRUNCATE {T_DIRECT};
INSERT INTO {T_DIRECT} (
    key3, "Date", "День недели", week_start,
    "CampaignId", "CampaignName", "AdGroupId", "AdGroupName",
    "AdNetworkType", "Device", "Impressions", "Clicks", total_cost, domain,
    "RlAdjustmentId", "RlAdjustmentId_total",
    campaign_code, tp, cpc_cpa, site_quiz, adgroup_code,
    account_login, manager_login,
    ag_part1, ag_part2, ag_part3, ag_part4, ag_part5, ag_part6, ag_part7,
    "марки авто", "Название crm", тип_заявки,
    kol_vo_zayavok, korr, kval, priezd, prodazhi, nekorr, ne_otvechaet, filtr, nedozvon, priedet,
    dohod_do_kredita, dobro,
    "статус", "специалист", "тип_сайта", "шаблон", "салон", "город", "регион",
    direction,
    "неверный_кодер_new", fid,
    проджект, id_салона, менеджер,
    источник, направление,
    "номер кампании | название кампании",
    "номер группы | название группы",
    "План заявки", "План приезда",
    "аккаунт|сайт",
    priezd_arrival_date, prodazhi_arrival_date,
    поставщик, _source_table,
    cascade_level
)
WITH
{common_ctes},

-- Лиды директ: исключаем посевы, SEO, pixel, telegram
leads_direct AS (
    SELECT l.*
    FROM leads_deduped l
    LEFT JOIN {T_LEADS_CROP_ATTR} lca ON l.id = lca.lead_id
    WHERE (lca.lead_id IS NULL OR lca.is_crop = FALSE)           -- не посевы
      AND NOT (l.utm_source IS NULL OR l.utm_source = '')         -- не SEO
      AND NOT (l.utm_source = 'seo' AND l.utm_medium = 'organic') -- не SEO с utm-тегом
      AND l.utm_source NOT LIKE 'victory_%'                       -- не pixel
      AND NOT (l.utm_source IN ('telegram','stories_tg')
               AND l.utm_medium = 'posev')                        -- не telegram-посевы
      AND NOT (l.utm_source IN ('vk_storis','telegram_storis')
               AND l.utm_medium = 'posev')                        -- не vk/tg storis-посевы
      AND NOT (l.utm_source IN ('max','vk','vk_groups')
               AND l.utm_medium IN ('posev','paid_social'))        -- не max/vk/vk_groups-посевы
      AND l.utm_source != 'vkads'                                  -- VK_ADS_LEADS_EXCLUSION_2026-07-07: не задваиваем в Контекст
),

leads_parsed AS (
    SELECT
        key3, key3_arrival_date, source_type, status, reason, salon,
        LOWER(TRIM(domain)) AS domain_name,
        created_date::date AS created_date,
        fid, utm_source, utm_medium,
        -- CDR_SPLIT_2026-07-27: расширенный паттерн (subsource:cdr + cdr_<tel>).
        -- CDR_PATTERN = case-insensitive POSIX с границами токена (config/settings.py).
        (COALESCE(utm_content, '') ~* '{CDR_PATTERN}') AS zvonki_cdr,
        {status_cases}
    FROM leads_direct
),

leads_agg AS (
    -- CDR_SPLIT_2026-07-27: GROUP BY key3, zvonki_cdr вместо BOOL_OR.
    -- Смешанная группа → 2 строки (CDR/не-CDR); однотипная → 1 строка (как раньше).
    SELECT
        key3,
        zvonki_cdr,
        MAX(domain_name)    AS lead_domain,
        MAX(fid)            AS fid,
        MAX(created_date)   AS created_date,
        MAX(utm_source)     AS utm_source,
        MAX(utm_medium)     AS utm_medium,
        SUM(kol_vo_zayavok)       AS kol_vo_zayavok,
        SUM(korr)                 AS korr,
        SUM(priezd)               AS priezd,
        SUM(prodazhi)             AS prodazhi,
        SUM(nekorr)               AS nekorr,
        SUM(ne_otvechaet)         AS ne_otvechaet,
        SUM(filtr)                AS filtr,
        SUM(nedozvon)             AS nedozvon,
        SUM(priedet)              AS priedet,
        SUM(dohod_do_kredita)     AS dohod_do_kredita,
        SUM(dobro)                AS dobro,
        SUM(kval)                 AS kval
    FROM leads_parsed
    WHERE key3 NOT LIKE '%|0|0|0|0'
    GROUP BY key3, zvonki_cdr
),

leads_arrival_agg AS (
    -- CDR_ARRIVAL_FIX_2026-07-27: zvonki_cdr убран из GROUP BY — arrival агрегируется
    -- целиком per key3_arrival_date (визитная грань). Equality-JOIN по CDR-флагу убран,
    -- т.к. заявочная и визитная грани имеют РАЗНЫЙ CDR-состав → equality JOIN терял строки.
    -- Защита от задвоения при двух la-строках (CDR/не-CDR) — в ЧАСТИ 1: arrival назначается
    -- только строке с rn_arrival=1 (ROW_NUMBER OVER PARTITION BY key3).
    SELECT
        key3_arrival_date,
        SUM(priezd)   AS priezd_arrival_date,
        SUM(prodazhi) AS prodazhi_arrival_date
    FROM leads_parsed
    WHERE key3_arrival_date IS NOT NULL
    GROUP BY key3_arrival_date
),

leads_agg_total AS (
    -- CDR_SPLIT_2026-07-27: суммарный kol_vo_zayavok по key3 (все CDR + не-CDR).
    -- CDR_OOM_FIX_2026-07-27: добавлены row_count и has_non_cdr — заменяют
    -- window-функции COUNT(*) OVER и ROW_NUMBER в base_join/ЧАСТИ 1.
    -- Это предотвращает принудительную материализацию base_join (CTE с window-fn
    -- не инлайнируется PG → сортировка миллионов строк → disk спилл при WM < sort).
    SELECT key3,
           SUM(kol_vo_zayavok) AS total_kol,
           COUNT(*) AS row_count,
           MAX(CASE WHEN NOT COALESCE(zvonki_cdr, FALSE) THEN 1 ELSE 0 END) AS has_non_cdr
    FROM leads_agg
    GROUP BY key3
),

-- Лиды без пары в Яндексе (key3 есть, но нет в raw_yandex)
leads_unmatched AS (
    SELECT la.*
    FROM leads_agg la
    WHERE la.key3 NOT IN (SELECT key3 FROM {T_RAW_YANDEX} WHERE key3 IS NOT NULL)
      AND la.key3 NOT LIKE '%|0|0|0|0'
),

-- Лиды с нулевым campaign_id (группируются по домену+дата)
leads_zero_agg AS (
    -- CDR_SPLIT_2026-07-27: zvonki_cdr в GROUP BY (по лиду, не BOOL_OR).
    SELECT
        domain_name AS lead_domain,
        created_date,
        zvonki_cdr,
        MAX(fid)            AS fid,
        MAX(utm_source)     AS utm_source,
        MAX(utm_medium)     AS utm_medium,
        SUM(kol_vo_zayavok)       AS kol_vo_zayavok,
        SUM(korr)                 AS korr,
        SUM(priezd)               AS priezd,
        SUM(prodazhi)             AS prodazhi,
        SUM(nekorr)               AS nekorr,
        SUM(ne_otvechaet)         AS ne_otvechaet,
        SUM(filtr)                AS filtr,
        SUM(nedozvon)             AS nedozvon,
        SUM(priedet)              AS priedet,
        SUM(dohod_do_kredita)     AS dohod_do_kredita,
        SUM(dobro)                AS dobro,
        SUM(kval)                 AS kval
    FROM leads_parsed
    WHERE key3 LIKE '%|0|0|0|0'
      -- исключаем посевные лиды для посевных доменов — они идут через telegram/social/seo crop
      AND NOT (
          domain_name IN (SELECT DISTINCT LOWER(TRIM("Сайт"))
                          FROM public.gsheets_crop_targeting_account
                          WHERE "Сайт" IS NOT NULL AND TRIM("Сайт") != '')
          AND (
              (utm_source IN ('telegram','stories_tg','max','vk','vk_groups',
                              'vk_storis','telegram_storis')
               AND utm_medium IN ('posev','paid_social'))
              OR (utm_source IS NULL OR utm_source = '')
              OR (utm_source = 'seo' AND utm_medium = 'organic')
          )
      )
    GROUP BY domain_name, created_date, zvonki_cdr
),

-- Яндекс: нормализация campaign_code + агрегация по key3
yd_agg AS (
    SELECT
        key3,
        SUM(total_cost)     AS total_cost,
        SUM("Impressions")  AS "Impressions",
        SUM("Clicks")       AS "Clicks",
        MAX("Date")         AS "Date",
        MAX("CampaignId")   AS "CampaignId",
        MAX("CampaignName") AS "CampaignName",
        MAX("AdGroupId")    AS "AdGroupId",
        MAX("AdGroupName")  AS "AdGroupName",
        MAX("AdNetworkType") AS "AdNetworkType",
        MAX("Device")       AS "Device",
        MAX("RlAdjustmentId") AS "RlAdjustmentId",
        MAX(week_start)     AS week_start,
        MAX(LOWER(TRIM(account_login))) AS account_login,
        MAX(manager_login)  AS manager_login,
        MAX(campaign_code)  AS campaign_code,
        MAX(tp)             AS tp,
        MAX(cpc_cpa)        AS cpc_cpa,
        MAX(REPLACE(REPLACE(site_quiz,'kviz','quiz'),'Kviz','Quiz')) AS site_quiz,
        MAX(adgroup_code)   AS adgroup_code
    FROM {T_RAW_YANDEX}
    GROUP BY key3
),

-- ══════════════════════════════════════════════════════════════════════════════
-- CASCADE_MATCH_2026-07-03: каскадный матчинг unmatched лидов к расходам Директа
-- Порядок: level4 (−correction_id) → level3 (−device) → level2 (−group_id)
-- Каждый лид матчится ровно на ОДНОМ уровне. Расход НЕ дублируется.
-- При множестве кандидатов — выбирается строка с MAX total_cost.
-- ══════════════════════════════════════════════════════════════════════════════
yd_keys_cascade AS MATERIALIZED (
    SELECT
        key3,
        total_cost,
        "CampaignId", "CampaignName", "AdGroupId", "AdGroupName",
        "AdNetworkType", "Device", "RlAdjustmentId", week_start,
        account_login, manager_login, campaign_code, tp, cpc_cpa, site_quiz, adgroup_code,
        SPLIT_PART(key3,'|',1)||'|'||SPLIT_PART(key3,'|',2)||'|'||SPLIT_PART(key3,'|',3)||'|'||SPLIT_PART(key3,'|',4) AS k4,
        SPLIT_PART(key3,'|',1)||'|'||SPLIT_PART(key3,'|',2)||'|'||SPLIT_PART(key3,'|',3) AS k3a,
        SPLIT_PART(key3,'|',1)||'|'||SPLIT_PART(key3,'|',2) AS k2
    FROM yd_agg
),
leads_unmatched_k AS MATERIALIZED (
    SELECT
        lu.*,
        SPLIT_PART(lu.key3,'|',1)||'|'||SPLIT_PART(lu.key3,'|',2)||'|'||SPLIT_PART(lu.key3,'|',3)||'|'||SPLIT_PART(lu.key3,'|',4) AS k4,
        SPLIT_PART(lu.key3,'|',1)||'|'||SPLIT_PART(lu.key3,'|',2)||'|'||SPLIT_PART(lu.key3,'|',3) AS k3a,
        SPLIT_PART(lu.key3,'|',1)||'|'||SPLIT_PART(lu.key3,'|',2) AS k2
    FROM leads_unmatched lu
),
-- Level 4: отбрасываем correction_id/RlAdjustmentId (H3_RlAdj — CRM не парсит r:)
cascade_lvl4 AS MATERIALIZED (
    SELECT DISTINCT ON (lu.key3)
        lu.key3 AS lead_key3, '4'::TEXT AS cascade_level,
        yk."CampaignId", yk."CampaignName", yk."AdGroupId", yk."AdGroupName",
        yk."AdNetworkType", yk."Device", yk."RlAdjustmentId", yk.week_start,
        yk.account_login, yk.manager_login, yk.campaign_code, yk.tp, yk.cpc_cpa,
        yk.site_quiz, yk.adgroup_code
    FROM leads_unmatched_k lu
    JOIN yd_keys_cascade yk ON lu.k4 = yk.k4
    ORDER BY lu.key3, yk.total_cost DESC NULLS LAST
),
-- Level 3: отбрасываем device + correction_id (H3_RlAdj + device-расхождение)
cascade_lvl3 AS MATERIALIZED (
    SELECT DISTINCT ON (lu.key3)
        lu.key3 AS lead_key3, '3'::TEXT AS cascade_level,
        yk."CampaignId", yk."CampaignName", yk."AdGroupId", yk."AdGroupName",
        yk."AdNetworkType", yk."Device", yk."RlAdjustmentId", yk.week_start,
        yk.account_login, yk.manager_login, yk.campaign_code, yk.tp, yk.cpc_cpa,
        yk.site_quiz, yk.adgroup_code
    FROM leads_unmatched_k lu
    JOIN yd_keys_cascade yk ON lu.k3a = yk.k3a
    WHERE lu.key3 NOT IN (SELECT lead_key3 FROM cascade_lvl4)
    ORDER BY lu.key3, yk.total_cost DESC NULLS LAST
),
-- Level 2: отбрасываем group_id + device + correction_id (H3 — group_id=NULL в UTM)
cascade_lvl2 AS (
    SELECT DISTINCT ON (lu.key3)
        lu.key3 AS lead_key3, '2'::TEXT AS cascade_level,
        yk."CampaignId", yk."CampaignName", yk."AdGroupId", yk."AdGroupName",
        yk."AdNetworkType", yk."Device", yk."RlAdjustmentId", yk.week_start,
        yk.account_login, yk.manager_login, yk.campaign_code, yk.tp, yk.cpc_cpa,
        yk.site_quiz, yk.adgroup_code
    FROM leads_unmatched_k lu
    JOIN yd_keys_cascade yk ON lu.k2 = yk.k2
    WHERE lu.key3 NOT IN (SELECT lead_key3 FROM cascade_lvl4)
      AND lu.key3 NOT IN (SELECT lead_key3 FROM cascade_lvl3)
    ORDER BY lu.key3, yk.total_cost DESC NULLS LAST
),
-- Все cascade-матчи
cascade_all AS MATERIALIZED (
    SELECT lead_key3, cascade_level,
        "CampaignId", "CampaignName", "AdGroupId", "AdGroupName",
        "AdNetworkType", "Device", "RlAdjustmentId", week_start,
        account_login, manager_login, campaign_code, tp, cpc_cpa, site_quiz, adgroup_code
    FROM cascade_lvl4
    UNION ALL
    SELECT lead_key3, cascade_level,
        "CampaignId", "CampaignName", "AdGroupId", "AdGroupName",
        "AdNetworkType", "Device", "RlAdjustmentId", week_start,
        account_login, manager_login, campaign_code, tp, cpc_cpa, site_quiz, adgroup_code
    FROM cascade_lvl3
    UNION ALL
    SELECT lead_key3, cascade_level,
        "CampaignId", "CampaignName", "AdGroupId", "AdGroupName",
        "AdNetworkType", "Device", "RlAdjustmentId", week_start,
        account_login, manager_login, campaign_code, tp, cpc_cpa, site_quiz, adgroup_code
    FROM cascade_lvl2
),
-- Лиды, не нашедшие пары ни на одном уровне каскада (остаются direct_unmatched)
leads_truly_unmatched AS (
    SELECT lu.* FROM leads_unmatched lu
    WHERE lu.key3 NOT IN (SELECT lead_key3 FROM cascade_all)
),

-- GS_BEST_2026-07-06: детерминированный выбор строки gsheet_sites по (login_key, дата расхода).
-- Заменяет LATERAL JOIN (тот менял план всего CTE → x6 замедление step3 из-за материализации).
-- Алгоритм: DISTINCT ON (login_key, date) × ~200K уникальных пар из raw_yandex →
-- hash JOIN в base_join = O(N), идентичная скорость исходному LEFT JOIN.
-- Приоритет: 1=дата в [launch_date,block_date), 2=без дат (fallback), 3=вне диапазона.
-- Для login_key с одним доменом — поведение идентично старому LEFT JOIN.
gs_best AS (
    SELECT DISTINCT ON (ud.login_key, ud.date_val)
        ud.login_key  AS match_login_key,
        ud.date_val   AS match_date,
        gs.*
    FROM (
        SELECT DISTINCT LOWER(TRIM(account_login)) AS login_key, "Date" AS date_val
        FROM {T_RAW_YANDEX}
    ) ud
    LEFT JOIN {T_GSHEET_SITES} gs ON gs.login_key = ud.login_key
    ORDER BY
        ud.login_key,
        ud.date_val,
        CASE
            WHEN gs.login_key IS NULL THEN 99
            WHEN NULLIF(TRIM(gs.launch_date), '') IS NULL
             AND NULLIF(TRIM(gs.block_date),  '') IS NULL
            THEN 2
            WHEN (NULLIF(TRIM(gs.launch_date), '') IS NULL
                   OR ud.date_val >= TO_DATE(NULLIF(TRIM(gs.launch_date), ''), 'DD.MM.YYYY'))
             AND (NULLIF(TRIM(gs.block_date),  '') IS NULL
                   OR ud.date_val <  TO_DATE(NULLIF(TRIM(gs.block_date),  ''), 'DD.MM.YYYY'))
            THEN 1
            ELSE 3
        END,
        CASE WHEN NULLIF(TRIM(gs.launch_date), '') IS NOT NULL
             THEN TO_DATE(NULLIF(TRIM(gs.launch_date), ''), 'DD.MM.YYYY')
             ELSE '1900-01-01'::date
        END DESC,
        gs.domain
),

base_join AS (
    SELECT
        yd.key3, yd."Date", yd."CampaignId", yd."CampaignName",
        yd."AdGroupId", yd."AdGroupName", yd."AdNetworkType", yd."Device",
        yd."Impressions", yd."Clicks", yd.total_cost, yd."RlAdjustmentId", yd.week_start,
        LOWER(TRIM(yd.account_login))                           AS account_login,
        COALESCE(yd.manager_login, amm.manager_login)           AS manager_login,
        COALESCE(yd.campaign_code, 'неверный кодер')            AS campaign_code,
        CASE WHEN COALESCE(yd.campaign_code, 'неверный кодер') = 'неверный кодер'
             THEN 'неверный кодер'
             ELSE COALESCE(NULLIF(yd.tp,      ''), 'неверный кодер') END AS tp,
        CASE WHEN COALESCE(yd.campaign_code, 'неверный кодер') = 'неверный кодер'
             THEN 'неверный кодер'
             ELSE COALESCE(NULLIF(yd.cpc_cpa, ''), 'неверный кодер') END AS cpc_cpa,
        CASE WHEN yd.site_quiz IN ('site','quiz')
             THEN yd.site_quiz ELSE 'неверный кодер' END        AS site_quiz,
        yd.adgroup_code,
        -- домен из Яндекса (для строк без лида)
        gs."domain"                                              AS yd_domain,
        la.kol_vo_zayavok, la.korr, la.kval, la.priezd, la.prodazhi,
        la.nekorr, la.ne_otvechaet, la.filtr, la.nedozvon, la.priedet,
        la.dohod_do_kredita, la.dobro,
        la.lead_domain, la.fid, la.utm_source, la.utm_medium,
        la.zvonki_cdr,  -- CDR_ZVONKI_2026-07-09: пробрасываем через base_join для PART 1
        lat.total_kol,  -- CDR_SPLIT_2026-07-27: всего лидов по key3 для деления расхода
        lat.row_count,  -- CDR_OOM_FIX_2026-07-27: число CDR/не-CDR строк per key3 (заменяет COUNT OVER)
        -- CDR_OOM_FIX_2026-07-27: заменяем ROW_NUMBER OVER (требовал sort всего base_join → disk спилл)
        -- на детерминированный CASE без window-fn. PG может инлайнить base_join без материализации.
        -- Arrival назначается: не-CDR строке (zvonki_cdr=FALSE), или CDR строке если нет не-CDR (has_non_cdr=0).
        CASE WHEN NOT COALESCE(la.zvonki_cdr, FALSE) THEN TRUE
             WHEN COALESCE(la.zvonki_cdr, FALSE) AND COALESCE(lat.has_non_cdr, 0) = 0 THEN TRUE
             ELSE FALSE END AS is_arrival_row,
        laa.priezd_arrival_date, laa.prodazhi_arrival_date,
        dst_lead.leads_source_type AS dst_lead_type,
        dst_yd.leads_source_type   AS dst_yd_type,
        -- DOMAIN_PRIORITY_2026-07-06: при cross-domain лиде (lead_domain != домен аккаунта gs)
        -- gs_dir (матч по домену лида) получает приоритет над gs (матч по account_login).
        -- Предотвращает утечку salon/direction когда Перформ-аккаунт рекламирует чужой домен.
        CASE WHEN la.lead_domain IS NOT NULL AND la.lead_domain IS DISTINCT FROM LOWER(TRIM(gs."domain"))
             THEN COALESCE(gs_dir."status",        gs."status")
             ELSE COALESCE(gs."status",            gs_dir."status")        END AS "статус",
        CASE WHEN la.lead_domain IS NOT NULL AND la.lead_domain IS DISTINCT FROM LOWER(TRIM(gs."domain"))
             THEN COALESCE(gs_dir."directologist", gs."directologist")
             ELSE COALESCE(gs."directologist",     gs_dir."directologist") END AS "специалист",
        CASE WHEN la.lead_domain IS NOT NULL AND la.lead_domain IS DISTINCT FROM LOWER(TRIM(gs."domain"))
             THEN COALESCE(gs_dir."site_type",     gs."site_type")
             ELSE COALESCE(gs."site_type",         gs_dir."site_type")     END AS "тип_сайта",
        CASE WHEN la.lead_domain IS NOT NULL AND la.lead_domain IS DISTINCT FROM LOWER(TRIM(gs."domain"))
             THEN COALESCE(gs_dir."template",      gs."template")
             ELSE COALESCE(gs."template",          gs_dir."template")      END AS "шаблон",
        CASE WHEN la.lead_domain IS NOT NULL AND la.lead_domain IS DISTINCT FROM LOWER(TRIM(gs."domain"))
             THEN COALESCE(gs_dir."salon",         gs."salon")
             ELSE COALESCE(gs."salon",             gs_dir."salon")         END AS "салон",
        CASE WHEN la.lead_domain IS NOT NULL AND la.lead_domain IS DISTINCT FROM LOWER(TRIM(gs."domain"))
             THEN COALESCE(gs_dir."city",          gs."city")
             ELSE COALESCE(gs."city",              gs_dir."city")          END AS "город",
        CASE WHEN la.lead_domain IS NOT NULL AND la.lead_domain IS DISTINCT FROM LOWER(TRIM(gs."domain"))
             THEN COALESCE(gs_dir."region",        gs."region")
             ELSE COALESCE(gs."region",            gs_dir."region")        END AS "регион",
        COALESCE(gs_dir."direction", gs."direction")                           AS direction,
        gs.login_key                                            AS gs_login,
        NULLIF(TRIM(gs.project_manager), '')                     AS проджект,
        gs.client_id,
        COALESCE(NULLIF(TRIM(gs.sales_manager),''),
                 NULLIF(TRIM(auto.менеджер),''))                AS менеджер
    FROM yd_agg yd
    -- GS_BEST_2026-07-06: hash JOIN по предвычисленному маппингу (login_key, date) → домен.
    -- Детерминированный выбор без LATERAL — скорость идентична исходному LEFT JOIN.
    LEFT JOIN gs_best                gs ON gs.match_login_key = LOWER(TRIM(yd.account_login))
                                       AND gs.match_date      = yd."Date"
    LEFT JOIN leads_agg             la      ON yd.key3        = la.key3
    -- CDR_ARRIVAL_FIX_2026-07-27: arrival джойнится только по key3 (без CDR-равенства).
    -- Equality по CDR убран: заявочная и визитная грани имеют разный CDR-состав →
    -- la.zvonki_cdr != laa.zvonki_cdr терял arrival-строки без пары в la.
    -- Задвоение при 2 la-строках предотвращено через rn_arrival в ЧАСТИ 1.
    LEFT JOIN leads_arrival_agg     laa     ON yd.key3        = laa.key3_arrival_date
    LEFT JOIN leads_agg_total       lat     ON yd.key3        = lat.key3
    LEFT JOIN account_manager_map   amm     ON yd.account_login = amm.account_login
    LEFT JOIN domain_source_type    dst_lead ON la.lead_domain = dst_lead.domain_name
    LEFT JOIN domain_source_type    dst_yd   ON LOWER(TRIM(gs."domain")) = dst_yd.domain_name
    LEFT JOIN {T_GSHEET_AUTOSALONY}  auto   ON gs.client_id = auto.id_салона
                                              AND gs.client_id IS NOT NULL
    -- GS_DIR_DEDUP_2026-07-27: 25 дублей доменов в не-Авто нишах → fan-out расхода.
    -- DISTINCT ON детерминированно оставляет одну строку на домен.
    LEFT JOIN (
        SELECT DISTINCT ON (LOWER(TRIM("domain")))
            "domain", "status", "directologist", "site_type", "template",
            "salon", "city", "region", "direction"
        FROM {T_GSHEET_SITES}
        ORDER BY LOWER(TRIM("domain")), "status" NULLS LAST
    )                                gs_dir ON COALESCE(la.lead_domain, LOWER(TRIM(gs."domain"))) = LOWER(TRIM(gs_dir."domain"))
    WHERE LOWER(TRIM(gs."domain")) != 'victory-crm.ru' OR gs."domain" IS NULL
)

-- ── ЧАСТЬ 1: Строки с данными Яндекса (основные) ──────────────────────────
-- CDR_SPLIT_2026-07-27: убран DISTINCT ON (key3) — теперь 1-2 строки per key3 (CDR/не-CDR).
-- Смешанная группа → 2 строки из leads_agg → 2 строки здесь; расход делится пропорционально.
-- CDR_ARRIVAL_FIX_2026-07-27: arrival назначается только строке rn_arrival=1 (детерминировано);
-- gs_dir дедуплицирован по domain (DISTINCT ON) — защита от fan-out при дублях в не-Авто нишах.
SELECT
    key3,
    "Date",
    CASE EXTRACT(ISODOW FROM "Date")
        WHEN 1 THEN '1_Понедельник' WHEN 2 THEN '2_Вторник'  WHEN 3 THEN '3_Среда'
        WHEN 4 THEN '4_Четверг'    WHEN 5 THEN '5_Пятница'   WHEN 6 THEN '6_Суббота'
        WHEN 7 THEN '7_Воскресенье'
    END                                                         AS "День недели",
    week_start,
    "CampaignId", "CampaignName", "AdGroupId", "AdGroupName",
    "AdNetworkType", "Device", "Impressions", "Clicks",
    -- CDR_SPLIT_2026-07-27: расход пропорционально лидам CDR/не-CDR в группе.
    -- Однотипная группа (total_kol = kol_vo_zayavok) → множитель 1.0 → расход не меняется.
    -- Группа без лидов (kol_vo_zayavok IS NULL) → ELSE → полный расход.
    -- Доля NUMERIC — без int-каста строчно (инвариант проекта).
    -- CDR_ARRIVAL_FIX_2026-07-27: при total_kol=0 (все лиды имеют пустой source_type →
    -- kol_vo_zayavok=0) и 2 la-строках ELSE давал полный расход ОБЕИМ → задвоение.
    -- Фикс: делить расход поровну по числу строк в key3-группе.
    CASE WHEN kol_vo_zayavok IS NOT NULL AND total_kol > 0
         THEN total_cost * (kol_vo_zayavok::NUMERIC / total_kol)
         WHEN kol_vo_zayavok IS NOT NULL AND total_kol = 0
         THEN total_cost / NULLIF(row_count, 0)  -- CDR_OOM_FIX_2026-07-27: скалярное значение из lat
         ELSE total_cost
    END                                                         AS total_cost,
    COALESCE(lead_domain, LOWER(TRIM(yd_domain)))               AS domain,
    "RlAdjustmentId",
    CASE WHEN "RlAdjustmentId" IS NULL THEN NULL
         WHEN "RlAdjustmentId" > 0     THEN 'Есть корректировка'
         ELSE 'Нет корректировки'
    END                                                         AS "RlAdjustmentId_total",
    campaign_code, tp, cpc_cpa, site_quiz, adgroup_code,
    COALESCE(account_login, gs_login)                           AS account_login,
    manager_login,
    -- ag_parts из нейминга
    CASE WHEN tp IN ('tp6','tp7') THEN 'MK/TK'
         WHEN adgroup_code ~ '^ct[0-9]+_(aoff|aon)_n[0-9]+_r[0-9]+_ct[0-9]+_ag[0-9]+_g[0-9]+$'
         THEN COALESCE(SPLIT_PART(adgroup_code,'_',1)||' - '||n_ag1.name, SPLIT_PART(adgroup_code,'_',1))
         ELSE 'неверный кодер' END                              AS ag_part1,
    CASE WHEN tp IN ('tp6','tp7') THEN 'MK/TK'
         WHEN adgroup_code ~ '^ct[0-9]+_(aoff|aon)_n[0-9]+_r[0-9]+_ct[0-9]+_ag[0-9]+_g[0-9]+$'
         THEN COALESCE(SPLIT_PART(adgroup_code,'_',2)||' - '||n_ag2.name, SPLIT_PART(adgroup_code,'_',2))
         ELSE 'неверный кодер' END                              AS ag_part2,
    CASE WHEN tp IN ('tp6','tp7') THEN 'MK/TK'
         WHEN adgroup_code ~ '^ct[0-9]+_(aoff|aon)_n[0-9]+_r[0-9]+_ct[0-9]+_ag[0-9]+_g[0-9]+$'
         THEN COALESCE(SPLIT_PART(adgroup_code,'_',3)||' - '||n_ag3.name, SPLIT_PART(adgroup_code,'_',3))
         ELSE 'неверный кодер' END                              AS ag_part3,
    CASE WHEN tp IN ('tp6','tp7') THEN 'MK/TK'
         WHEN adgroup_code ~ '^ct[0-9]+_(aoff|aon)_n[0-9]+_r[0-9]+_ct[0-9]+_ag[0-9]+_g[0-9]+$'
         THEN COALESCE(SPLIT_PART(adgroup_code,'_',4)||' - '||n_ag4.name, SPLIT_PART(adgroup_code,'_',4))
         ELSE 'неверный кодер' END                              AS ag_part4,
    CASE WHEN tp IN ('tp6','tp7') THEN 'MK/TK'
         WHEN adgroup_code ~ '^ct[0-9]+_(aoff|aon)_n[0-9]+_r[0-9]+_ct[0-9]{{3}}_ag[0-9]+_g[0-9]+$'
         THEN COALESCE(SPLIT_PART(adgroup_code,'_',5)||' - '||n_ag5.name, SPLIT_PART(adgroup_code,'_',5))
         ELSE 'неверный кодер' END                              AS ag_part5,
    CASE WHEN tp IN ('tp6','tp7') THEN 'MK/TK'
         WHEN adgroup_code ~ '^ct[0-9]+_(aoff|aon)_n[0-9]+_r[0-9]+_ct[0-9]+_ag[0-9]+_g[0-9]+$'
         THEN COALESCE(SPLIT_PART(adgroup_code,'_',6)||' - '||n_ag6.name, SPLIT_PART(adgroup_code,'_',6))
         ELSE 'неверный кодер' END                              AS ag_part6,
    CASE WHEN tp IN ('tp6','tp7') THEN 'MK/TK'
         WHEN adgroup_code ~ '^ct[0-9]+_(aoff|aon)_n[0-9]+_r[0-9]+_ct[0-9]+_ag[0-9]+_g[0-9]+$'
         THEN COALESCE(SPLIT_PART(adgroup_code,'_',7)||' - '||n_ag7.name, SPLIT_PART(adgroup_code,'_',7))
         ELSE 'неверный кодер' END                              AS ag_part7,
    {brand_case_sql}                                            AS "марки авто",
    COALESCE(dst_lead_type, dst_yd_type)                        AS "Название crm",
    -- CDR_ZVONKI_2026-07-09: CDR-лиды маркируются 'Звонки_CDR', остальные 'заявки'
    -- zvonki_cdr пробрасывается из base_join (la.zvonki_cdr → base_join.zvonki_cdr), la здесь не в scope
    CASE WHEN COALESCE(zvonki_cdr, FALSE) THEN 'Звонки_CDR' ELSE 'Заявки' END AS тип_заявки,
    COALESCE(kol_vo_zayavok, 0) AS kol_vo_zayavok,
    COALESCE(korr,           0) AS korr,
    COALESCE(kval,           0) AS kval,
    COALESCE(priezd,         0) AS priezd,
    COALESCE(prodazhi,       0) AS prodazhi,
    COALESCE(nekorr,         0) AS nekorr,
    COALESCE(ne_otvechaet,   0) AS ne_otvechaet,
    COALESCE(filtr,          0) AS filtr,
    COALESCE(nedozvon,       0) AS nedozvon,
    COALESCE(priedet,        0) AS priedet,
    dohod_do_kredita,
    dobro,
    "статус", "специалист", "тип_сайта", "шаблон", "салон", "город", "регион",
    direction,
    CASE WHEN tp IN ('tp6','tp7') THEN NULL
         WHEN adgroup_code !~ '^ct[0-9]+_(aoff|aon)_n[0-9]+_r[0-9]+_ct[0-9]+_ag[0-9]+_g[0-9]+$'
              THEN 'неверный кодер'
         WHEN n_ag1.name IS NOT NULL AND n_ag2.name IS NOT NULL AND n_ag3.name IS NOT NULL
              AND n_ag4.name IS NOT NULL AND n_ag5.name IS NOT NULL
              AND n_ag6.name IS NOT NULL AND n_ag7.name IS NOT NULL
              THEN 'верный кодер'
         ELSE 'неверный кодер'
    END                                                         AS "неверный_кодер_new",
    fid,
    проджект,
    client_id AS id_салона,
    менеджер,
    -- KOMPLEKS_REFACTOR_REDO_2026-07-09: источник хранит SEO/SEO Flow/Контекст, направление → 'Комплекс'
    CASE WHEN tp = 'tp8'                    THEN 'Посевы_Telegram'
         WHEN tp = 'tp9'                    THEN 'Посевы_Max'
         WHEN tp = 'tp10'                   THEN 'Посевы_Telegram+Max'
         WHEN manager_login IS NOT NULL     THEN 'Контекст'
         WHEN "статус" = 'Контекст активно' THEN 'Контекст'
         WHEN "статус" = 'SEO'              THEN 'SEO'
         WHEN "статус" = 'SEO Flow'         THEN 'SEO Flow'
         ELSE NULL
    END                                                         AS источник,
    CASE WHEN tp = 'tp8'                    THEN 'директ (tp8)'
         WHEN tp = 'tp9'                    THEN 'директ (tp9)'
         WHEN tp = 'tp10'                   THEN 'директ (tp10)'
         WHEN manager_login IS NOT NULL     THEN 'Комплекс'
         WHEN "статус" = 'Контекст активно' THEN 'Комплекс'
         WHEN "статус" = 'SEO'              THEN 'Комплекс'
         WHEN "статус" = 'SEO Flow'         THEN 'Комплекс'
         ELSE NULL
    END                                                         AS направление,
    -- NULL_NAME_FALLBACK_2026-07-06: если CampaignName отсутствует (новая кампания,
    -- имя ещё не подтянулось из API) — возвращаем хотя бы CampaignId, чтобы расход
    -- кампании не терялся в «без кампании» в Power BI
    CASE WHEN "CampaignId" IS NULL THEN NULL
         WHEN NULLIF(TRIM("CampaignName"), '') IS NULL
              THEN "CampaignId"::TEXT
         ELSE "CampaignId"::TEXT || '|' ||
              COALESCE(NULLIF(SUBSTRING("CampaignName"
                  FROM POSITION(' — ' IN "CampaignName") + 3), ''), "CampaignName")
    END                                                         AS "номер кампании | название кампании",
    CASE WHEN "AdGroupId" IS NULL THEN NULL
         ELSE "AdGroupId"::TEXT || '|' ||
              COALESCE(NULLIF(SPLIT_PART("AdGroupName", ' — ', 2), ''), "AdGroupName")
    END                                                         AS "номер группы | название группы",
    CASE WHEN DATE_TRUNC('month', "Date") = DATE_TRUNC('month', CURRENT_DATE)
         THEN NULLIF(REPLACE(REPLACE(REPLACE(pf."цена_заявки", chr(160), ''), ' ', ''), ',', '.'), '-')::NUMERIC::INTEGER
         ELSE NULL END                                          AS "План заявки",
    CASE WHEN DATE_TRUNC('month', "Date") = DATE_TRUNC('month', CURRENT_DATE)
         THEN NULLIF(REPLACE(REPLACE(REPLACE(pf."цена_приезда", chr(160), ''), ' ', ''), ',', '.'), '-')::NUMERIC::INTEGER
         ELSE NULL END                                          AS "План приезда",
    COALESCE(account_login, gs_login) || '|' ||
        COALESCE(lead_domain, LOWER(TRIM(yd_domain)))           AS "аккаунт|сайт",
    -- CDR_OOM_FIX_2026-07-27: is_arrival_row (bool) заменяет rn_arrival=1.
    -- При двух la-строках (CDR/не-CDR): не-CDR получает arrival, CDR строка получает 0.
    CASE WHEN is_arrival_row THEN priezd_arrival_date   ELSE 0 END AS priezd_arrival_date,
    CASE WHEN is_arrival_row THEN prodazhi_arrival_date ELSE 0 END AS prodazhi_arrival_date,
    'Яндекс'::TEXT                                              AS поставщик,
    'direct'::TEXT                                              AS _source_table,
    NULL::TEXT                                                  AS cascade_level
FROM base_join
{AG_PARTS_JOINS}
LEFT JOIN plan_fakt_cte pf
    ON LOWER(TRIM(base_join."салон"))     = LOWER(TRIM(pf.pf_salon))
   AND LOWER(TRIM(base_join."тип_сайта")) = LOWER(TRIM(pf.pf_tip))

UNION ALL

-- ── ЧАСТЬ 2: Лиды без пары в Яндексе (строгий матч не найден, каскад не нашёл) ─
SELECT
    lu.key3,
    lu.created_date::date                                       AS "Date",
    CASE EXTRACT(ISODOW FROM lu.created_date::date)
        WHEN 1 THEN '1_Понедельник' WHEN 2 THEN '2_Вторник'  WHEN 3 THEN '3_Среда'
        WHEN 4 THEN '4_Четверг'    WHEN 5 THEN '5_Пятница'   WHEN 6 THEN '6_Суббота'
        WHEN 7 THEN '7_Воскресенье'
    END                                                         AS "День недели",
    DATE_TRUNC('week', lu.created_date::date)::date             AS week_start,
    NULL::BIGINT AS "CampaignId",   NULL::TEXT AS "CampaignName",
    NULL::BIGINT AS "AdGroupId",    NULL::TEXT AS "AdGroupName",
    NULL::TEXT AS "AdNetworkType",  NULL::TEXT AS "Device",
    NULL::BIGINT AS "Impressions",  NULL::BIGINT AS "Clicks",
    NULL::NUMERIC AS total_cost,
    lu.lead_domain                                              AS domain,
    NULL::BIGINT AS "RlAdjustmentId",
    NULL::TEXT AS "RlAdjustmentId_total",
    'неверный кодер'::TEXT AS campaign_code, 'неверный кодер'::TEXT AS tp,
    'неверный кодер'::TEXT AS cpc_cpa, 'неверный кодер'::TEXT AS site_quiz,
    NULL::TEXT AS adgroup_code,
    gs.login_key                                                AS account_login,
    amm.manager_login                                           AS manager_login,
    'неверный кодер'::TEXT AS ag_part1, 'неверный кодер'::TEXT AS ag_part2,
    'неверный кодер'::TEXT AS ag_part3, 'неверный кодер'::TEXT AS ag_part4,
    'неверный кодер'::TEXT AS ag_part5, 'неверный кодер'::TEXT AS ag_part6,
    'неверный кодер'::TEXT AS ag_part7,
    ''::TEXT AS "марки авто",
    dst.leads_source_type                                       AS "Название crm",
    -- CDR_ZVONKI_2026-07-09: CDR-лиды маркируются 'Звонки_CDR', остальные 'заявки'
    CASE WHEN COALESCE(lu.zvonki_cdr, FALSE) THEN 'Звонки_CDR' ELSE 'Заявки' END AS тип_заявки,
    lu.kol_vo_zayavok, lu.korr, lu.kval, lu.priezd, lu.prodazhi,
    lu.nekorr, lu.ne_otvechaet, lu.filtr, lu.nedozvon, lu.priedet,
    lu.dohod_do_kredita, lu.dobro,
    gs."status"  AS "статус", gs."directologist" AS "специалист", gs."site_type" AS "тип_сайта", gs."template" AS "шаблон",
    gs."salon" AS "салон", gs."city" AS "город", gs."region" AS "регион",
    gs."direction" AS direction,
    NULL::TEXT AS "неверный_кодер_new",
    lu.fid,
    NULLIF(TRIM(gs.project_manager), '')                         AS проджект,
    gs.client_id,
    COALESCE(NULLIF(TRIM(gs.sales_manager),''),
             NULLIF(TRIM(auto.менеджер),''))                    AS менеджер,
    -- KOMPLEKS_REFACTOR_REDO_2026-07-09: источник хранит SEO/SEO Flow/Контекст, направление → 'Комплекс'
    CASE WHEN gs."status" = 'Контекст активно' THEN 'Контекст'
         WHEN gs."status" = 'SEO'              THEN 'SEO'
         WHEN gs."status" = 'SEO Flow'         THEN 'SEO Flow'
         ELSE NULL END                                          AS источник,
    CASE WHEN gs."status" = 'Контекст активно' THEN 'Комплекс'
         WHEN gs."status" = 'SEO'              THEN 'Комплекс'
         WHEN gs."status" = 'SEO Flow'         THEN 'Комплекс'
         ELSE NULL END                                          AS направление,
    NULL::TEXT AS "номер кампании | название кампании",
    NULL::TEXT AS "номер группы | название группы",
    CASE WHEN DATE_TRUNC('month', lu.created_date::date) = DATE_TRUNC('month', CURRENT_DATE)
         THEN NULLIF(REPLACE(REPLACE(REPLACE(pf."цена_заявки", chr(160), ''), ' ', ''), ',', '.'), '-')::NUMERIC::INTEGER
         ELSE NULL END                                          AS "План заявки",
    CASE WHEN DATE_TRUNC('month', lu.created_date::date) = DATE_TRUNC('month', CURRENT_DATE)
         THEN NULLIF(REPLACE(REPLACE(REPLACE(pf."цена_приезда", chr(160), ''), ' ', ''), ',', '.'), '-')::NUMERIC::INTEGER
         ELSE NULL END                                          AS "План приезда",
    gs.login_key || '|' || lu.lead_domain                       AS "аккаунт|сайт",
    NULL::INTEGER AS priezd_arrival_date,
    NULL::INTEGER AS prodazhi_arrival_date,
    'Яндекс'::TEXT                                              AS поставщик,
    'direct_unmatched'::TEXT                                    AS _source_table,
    NULL::TEXT                                                  AS cascade_level
FROM leads_truly_unmatched lu
LEFT JOIN {T_GSHEET_SITES}       gs     ON lu.lead_domain = LOWER(TRIM(gs."domain"))
LEFT JOIN domain_source_type     dst    ON lu.lead_domain = dst.domain_name
LEFT JOIN account_manager_map    amm    ON gs.login_key   = amm.account_login
LEFT JOIN {T_GSHEET_AUTOSALONY}  auto   ON gs.client_id = auto.id_салона AND gs.client_id IS NOT NULL
LEFT JOIN plan_fakt_cte pf
    ON LOWER(TRIM(gs."salon"))     = LOWER(TRIM(pf.pf_salon))
   AND LOWER(TRIM(gs."site_type")) = LOWER(TRIM(pf.pf_tip))

UNION ALL

-- ── ЧАСТЬ 2b: Лиды с каскадным матчем (CASCADE_MATCH_2026-07-03) ───────────
-- total_cost=NULL: расход не дублируется (spend-строка уже в ЧАСТИ 1).
-- _source_table='direct': лид теперь атрибутирован к кампании.
-- cascade_level: '4'/'3'/'2' — уровень каскада для диагностики.
SELECT
    lu.key3,
    lu.created_date::date                                       AS "Date",
    CASE EXTRACT(ISODOW FROM lu.created_date::date)
        WHEN 1 THEN '1_Понедельник' WHEN 2 THEN '2_Вторник'  WHEN 3 THEN '3_Среда'
        WHEN 4 THEN '4_Четверг'    WHEN 5 THEN '5_Пятница'   WHEN 6 THEN '6_Суббота'
        WHEN 7 THEN '7_Воскресенье'
    END                                                         AS "День недели",
    DATE_TRUNC('week', lu.created_date::date)::date             AS week_start,
    ca."CampaignId", ca."CampaignName", ca."AdGroupId", ca."AdGroupName",
    ca."AdNetworkType", ca."Device",
    NULL::BIGINT  AS "Impressions",
    NULL::BIGINT  AS "Clicks",
    NULL::NUMERIC AS total_cost,
    lu.lead_domain                                              AS domain,
    ca."RlAdjustmentId",
    CASE WHEN ca."RlAdjustmentId" IS NULL THEN NULL
         WHEN ca."RlAdjustmentId" > 0     THEN 'Есть корректировка'
         ELSE 'Нет корректировки'
    END                                                         AS "RlAdjustmentId_total",
    COALESCE(ca.campaign_code, 'неверный кодер')               AS campaign_code,
    CASE WHEN COALESCE(ca.campaign_code, 'неверный кодер') = 'неверный кодер'
         THEN 'неверный кодер'
         ELSE COALESCE(NULLIF(ca.tp,      ''), 'неверный кодер') END AS tp,
    CASE WHEN COALESCE(ca.campaign_code, 'неверный кодер') = 'неверный кодер'
         THEN 'неверный кодер'
         ELSE COALESCE(NULLIF(ca.cpc_cpa, ''), 'неверный кодер') END AS cpc_cpa,
    CASE WHEN ca.site_quiz IN ('site','quiz')
         THEN ca.site_quiz ELSE 'неверный кодер' END            AS site_quiz,
    ca.adgroup_code,
    COALESCE(ca.account_login, gs.login_key)                   AS account_login,
    COALESCE(ca.manager_login, amm.manager_login)              AS manager_login,
    'неверный кодер'::TEXT AS ag_part1, 'неверный кодер'::TEXT AS ag_part2,
    'неверный кодер'::TEXT AS ag_part3, 'неверный кодер'::TEXT AS ag_part4,
    'неверный кодер'::TEXT AS ag_part5, 'неверный кодер'::TEXT AS ag_part6,
    'неверный кодер'::TEXT AS ag_part7,
    ''::TEXT AS "марки авто",
    dst.leads_source_type                                       AS "Название crm",
    -- CDR_ZVONKI_2026-07-09: CDR-лиды маркируются 'Звонки_CDR', остальные 'заявки'
    CASE WHEN COALESCE(lu.zvonki_cdr, FALSE) THEN 'Звонки_CDR' ELSE 'Заявки' END AS тип_заявки,
    lu.kol_vo_zayavok, lu.korr, lu.kval, lu.priezd, lu.prodazhi,
    lu.nekorr, lu.ne_otvechaet, lu.filtr, lu.nedozvon, lu.priedet,
    lu.dohod_do_kredita, lu.dobro,
    gs."status"  AS "статус", gs."directologist" AS "специалист",
    gs."site_type" AS "тип_сайта", gs."template" AS "шаблон",
    gs."salon" AS "салон", gs."city" AS "город", gs."region" AS "регион",
    gs."direction" AS direction,
    NULL::TEXT AS "неверный_кодер_new",
    lu.fid,
    NULLIF(TRIM(gs.project_manager), '')                        AS проджект,
    gs.client_id,
    COALESCE(NULLIF(TRIM(gs.sales_manager),''),
             NULLIF(TRIM(auto.менеджер),''))                   AS менеджер,
    -- KOMPLEKS_REFACTOR_REDO_2026-07-09: cascade matched — источник=SEO/SEO Flow/Контекст, направление → 'Комплекс'
    CASE WHEN ca.tp = 'tp8'                    THEN 'Посевы_Telegram'
         WHEN ca.tp = 'tp9'                    THEN 'Посевы_Max'
         WHEN ca.tp = 'tp10'                   THEN 'Посевы_Telegram+Max'
         WHEN ca.manager_login IS NOT NULL      THEN 'Контекст'
         WHEN gs."status" = 'Контекст активно' THEN 'Контекст'
         WHEN gs."status" = 'SEO'              THEN 'SEO'
         WHEN gs."status" = 'SEO Flow'         THEN 'SEO Flow'
         ELSE NULL
    END                                                         AS источник,
    CASE WHEN ca.tp = 'tp8'                    THEN 'директ (tp8)'
         WHEN ca.tp = 'tp9'                    THEN 'директ (tp9)'
         WHEN ca.tp = 'tp10'                   THEN 'директ (tp10)'
         WHEN ca.manager_login IS NOT NULL      THEN 'Комплекс'
         WHEN gs."status" = 'Контекст активно' THEN 'Комплекс'
         WHEN gs."status" = 'SEO'              THEN 'Комплекс'
         WHEN gs."status" = 'SEO Flow'         THEN 'Комплекс'
         ELSE NULL
    END                                                         AS направление,
    -- NULL_NAME_FALLBACK_2026-07-06: cascade-matched лиды — тот же guard на NULL CampaignName
    CASE WHEN ca."CampaignId" IS NULL THEN NULL
         WHEN NULLIF(TRIM(ca."CampaignName"), '') IS NULL
              THEN ca."CampaignId"::TEXT
         ELSE ca."CampaignId"::TEXT || '|' ||
              COALESCE(NULLIF(SUBSTRING(ca."CampaignName"
                  FROM POSITION(' — ' IN ca."CampaignName") + 3), ''), ca."CampaignName")
    END                                                         AS "номер кампании | название кампании",
    CASE WHEN ca."AdGroupId" IS NULL THEN NULL
         ELSE ca."AdGroupId"::TEXT || '|' ||
              COALESCE(NULLIF(SPLIT_PART(ca."AdGroupName", ' — ', 2), ''), ca."AdGroupName")
    END                                                         AS "номер группы | название группы",
    CASE WHEN DATE_TRUNC('month', lu.created_date::date) = DATE_TRUNC('month', CURRENT_DATE)
         THEN NULLIF(REPLACE(REPLACE(REPLACE(pf."цена_заявки", chr(160), ''), ' ', ''), ',', '.'), '-')::NUMERIC::INTEGER
         ELSE NULL END                                          AS "План заявки",
    CASE WHEN DATE_TRUNC('month', lu.created_date::date) = DATE_TRUNC('month', CURRENT_DATE)
         THEN NULLIF(REPLACE(REPLACE(REPLACE(pf."цена_приезда", chr(160), ''), ' ', ''), ',', '.'), '-')::NUMERIC::INTEGER
         ELSE NULL END                                          AS "План приезда",
    COALESCE(ca.account_login, gs.login_key) || '|' || lu.lead_domain AS "аккаунт|сайт",
    NULL::INTEGER AS priezd_arrival_date,
    NULL::INTEGER AS prodazhi_arrival_date,
    'Яндекс'::TEXT                                              AS поставщик,
    'direct'::TEXT                                              AS _source_table,
    ca.cascade_level                                            AS cascade_level
FROM leads_unmatched lu
JOIN cascade_all ca ON lu.key3 = ca.lead_key3
LEFT JOIN {T_GSHEET_SITES}       gs    ON lu.lead_domain = LOWER(TRIM(gs."domain"))
LEFT JOIN domain_source_type     dst   ON lu.lead_domain = dst.domain_name
LEFT JOIN account_manager_map    amm   ON COALESCE(ca.account_login, gs.login_key) = amm.account_login
LEFT JOIN {T_GSHEET_AUTOSALONY}  auto  ON gs.client_id = auto.id_салона AND gs.client_id IS NOT NULL
LEFT JOIN plan_fakt_cte pf
    ON LOWER(TRIM(gs."salon"))     = LOWER(TRIM(pf.pf_salon))
   AND LOWER(TRIM(gs."site_type")) = LOWER(TRIM(pf.pf_tip))

UNION ALL

-- ── ЧАСТЬ 3: Лиды без campaign_id (группируются по домену+дата) ───────────
SELECT
    NULL::TEXT AS key3,
    lz.created_date::date                                       AS "Date",
    CASE EXTRACT(ISODOW FROM lz.created_date::date)
        WHEN 1 THEN '1_Понедельник' WHEN 2 THEN '2_Вторник'  WHEN 3 THEN '3_Среда'
        WHEN 4 THEN '4_Четверг'    WHEN 5 THEN '5_Пятница'   WHEN 6 THEN '6_Суббота'
        WHEN 7 THEN '7_Воскресенье'
    END                                                         AS "День недели",
    DATE_TRUNC('week', lz.created_date::date)::date             AS week_start,
    NULL::BIGINT AS "CampaignId",   'нет campaign'::TEXT AS "CampaignName",
    NULL::BIGINT AS "AdGroupId",    'нет campaign'::TEXT AS "AdGroupName",
    NULL::TEXT AS "AdNetworkType",  NULL::TEXT AS "Device",
    NULL::BIGINT AS "Impressions",  NULL::BIGINT AS "Clicks",
    NULL::NUMERIC AS total_cost,
    lz.lead_domain                                              AS domain,
    NULL::BIGINT AS "RlAdjustmentId",
    NULL::TEXT AS "RlAdjustmentId_total",
    NULL::TEXT AS campaign_code, NULL::TEXT AS tp,
    NULL::TEXT AS cpc_cpa, NULL::TEXT AS site_quiz,
    NULL::TEXT AS adgroup_code,
    gs.login_key                                                AS account_login,
    amm.manager_login                                           AS manager_login,
    NULL::TEXT AS ag_part1, NULL::TEXT AS ag_part2, NULL::TEXT AS ag_part3,
    NULL::TEXT AS ag_part4, NULL::TEXT AS ag_part5, NULL::TEXT AS ag_part6, NULL::TEXT AS ag_part7,
    ''::TEXT AS "марки авто",
    dst.leads_source_type                                       AS "Название crm",
    -- CDR_ZVONKI_2026-07-09: CDR-лиды маркируются 'Звонки_CDR', остальные 'заявки'
    CASE WHEN COALESCE(lz.zvonki_cdr, FALSE) THEN 'Звонки_CDR' ELSE 'Заявки' END AS тип_заявки,
    lz.kol_vo_zayavok, lz.korr, lz.kval, lz.priezd, lz.prodazhi,
    lz.nekorr, lz.ne_otvechaet, lz.filtr, lz.nedozvon, lz.priedet,
    lz.dohod_do_kredita, lz.dobro,
    gs."status"  AS "статус", gs."directologist" AS "специалист", gs."site_type" AS "тип_сайта", gs."template" AS "шаблон",
    gs."salon" AS "салон", gs."city" AS "город", gs."region" AS "регион",
    gs."direction" AS direction,
    NULL::TEXT AS "неверный_кодер_new",
    lz.fid,
    NULLIF(TRIM(gs.project_manager), '')                         AS проджект,
    gs.client_id,
    COALESCE(NULLIF(TRIM(gs.sales_manager),''),
             NULLIF(TRIM(auto.менеджер),''))                    AS менеджер,
    NULL::TEXT AS источник,
    NULL::TEXT AS направление,
    NULL::TEXT AS "номер кампании | название кампании",
    NULL::TEXT AS "номер группы | название группы",
    NULL::INTEGER AS "План заявки",
    NULL::INTEGER AS "План приезда",
    gs.login_key || '|' || lz.lead_domain                       AS "аккаунт|сайт",
    NULL::INTEGER AS priezd_arrival_date,
    NULL::INTEGER AS prodazhi_arrival_date,
    'Яндекс'::TEXT                                              AS поставщик,
    'direct_zero'::TEXT                                         AS _source_table,
    NULL::TEXT                                                  AS cascade_level
FROM leads_zero_agg lz
LEFT JOIN {T_GSHEET_SITES}       gs     ON lz.lead_domain = LOWER(TRIM(gs."domain"))
LEFT JOIN domain_source_type     dst    ON lz.lead_domain = dst.domain_name
LEFT JOIN account_manager_map    amm    ON gs.login_key   = amm.account_login
LEFT JOIN {T_GSHEET_AUTOSALONY}  auto   ON gs.client_id = auto.id_салона AND gs.client_id IS NOT NULL;
"""


# ══════════════════════════════════════════════════════════════════════════════
# big_analytics_seo
# ══════════════════════════════════════════════════════════════════════════════

def _build_seo_sql(status_cases: str, priezd_sql: str) -> str:
    common_ctes = _build_common_ctes(priezd_sql)
    return f"""
CREATE UNLOGGED TABLE IF NOT EXISTS {T_SEO} (LIKE {T_DIRECT} INCLUDING ALL);
ALTER TABLE {T_SEO} SET UNLOGGED;
TRUNCATE {T_SEO};
INSERT INTO {T_SEO} (
    key3, "Date", "День недели", week_start,
    "CampaignId", "CampaignName", "AdGroupId", "AdGroupName",
    "AdNetworkType", "Device", "Impressions", "Clicks", total_cost, domain,
    "RlAdjustmentId", "RlAdjustmentId_total",
    campaign_code, tp, cpc_cpa, site_quiz, adgroup_code,
    account_login, manager_login,
    ag_part1, ag_part2, ag_part3, ag_part4, ag_part5, ag_part6, ag_part7,
    "марки авто", "Название crm", тип_заявки,
    kol_vo_zayavok, korr, kval, priezd, prodazhi, nekorr, ne_otvechaet, filtr, nedozvon, priedet,
    dohod_do_kredita, dobro,
    "статус", "специалист", "тип_сайта", "шаблон", "салон", "город", "регион",
    direction,
    "неверный_кодер_new", fid,
    проджект, id_салона, менеджер,
    источник, направление,
    "номер кампании | название кампании",
    "номер группы | название группы",
    "План заявки", "План приезда",
    "аккаунт|сайт",
    priezd_arrival_date, prodazhi_arrival_date,
    поставщик, _source_table
)
WITH
{common_ctes},

leads_seo AS (
    SELECT
        id, created_date::date AS created_date, domain_id, domain,
        status, source_type, fid,
        -- CDR_SPLIT_2026-07-27: расширенный паттерн (CDR_PATTERN из config/settings.py)
        (COALESCE(utm_content, '') ~* '{CDR_PATTERN}') AS zvonki_cdr,
        {status_cases}
    FROM leads_deduped
    WHERE (
        (utm_source IS NULL OR utm_source = '')
        OR (utm_source = 'seo' AND utm_medium = 'organic')
    )
    AND LOWER(TRIM(domain)) NOT IN (
        SELECT DISTINCT LOWER(TRIM("Сайт"))
        FROM public.gsheets_crop_targeting_account
        WHERE "Сайт" IS NOT NULL AND TRIM("Сайт") != ''
    )
    -- VK_ADS_LEADS_EXCLUSION_2026-07-07: лиды с NULL utm_source на VK Ads доменах (direction='Авто')
    -- матчатся в _add_vk_ads_to_crop_sql по ключу domain+date+utm_campaign; в SEO не идут
    AND NOT (
        (utm_source IS NULL OR utm_source = '')
        AND LOWER(TRIM(domain)) IN (
            SELECT DISTINCT LOWER(TRIM(gs."domain"))
            FROM {T_GSHEET_SITES} gs
            WHERE TRIM(gs.vk_client_id) != ''
              AND gs.vk_client_id IS NOT NULL
              AND gs.direction = 'Авто'
        )
    )
),

leads_seo_agg AS (
    -- CDR_SPLIT_2026-07-27: zvonki_cdr в GROUP BY (по лиду, не BOOL_OR)
    SELECT
        LOWER(TRIM(domain))     AS domain,
        created_date,
        zvonki_cdr,
        MAX(fid)                AS fid,
        SUM(kol_vo_zayavok)     AS kol_vo_zayavok,
        SUM(korr)               AS korr,
        SUM(priezd)             AS priezd,
        SUM(prodazhi)           AS prodazhi,
        SUM(nekorr)             AS nekorr,
        SUM(ne_otvechaet)       AS ne_otvechaet,
        SUM(filtr)              AS filtr,
        SUM(nedozvon)           AS nedozvon,
        SUM(priedet)            AS priedet,
        SUM(dohod_do_kredita)   AS dohod_do_kredita,
        SUM(dobro)              AS dobro,
        SUM(kval)               AS kval
    FROM leads_seo
    GROUP BY LOWER(TRIM(domain)), created_date, zvonki_cdr
)

SELECT
    NULL::TEXT      AS key3,
    la.created_date AS "Date",
    CASE EXTRACT(ISODOW FROM la.created_date)
        WHEN 1 THEN '1_Понедельник' WHEN 2 THEN '2_Вторник' WHEN 3 THEN '3_Среда'
        WHEN 4 THEN '4_Четверг'    WHEN 5 THEN '5_Пятница'  WHEN 6 THEN '6_Суббота'
        WHEN 7 THEN '7_Воскресенье'
    END             AS "День недели",
    DATE_TRUNC('week', la.created_date)::date AS week_start,
    NULL::BIGINT AS "CampaignId",   'seo'::TEXT AS "CampaignName",
    NULL::BIGINT AS "AdGroupId",    'seo'::TEXT AS "AdGroupName",
    NULL::TEXT AS "AdNetworkType",  NULL::TEXT AS "Device",
    NULL::BIGINT AS "Impressions",  NULL::BIGINT AS "Clicks",
    NULL::NUMERIC AS total_cost,
    la.domain,
    NULL::BIGINT AS "RlAdjustmentId", NULL::TEXT AS "RlAdjustmentId_total",
    NULL::TEXT AS campaign_code,    NULL::TEXT AS tp,
    NULL::TEXT AS cpc_cpa,          NULL::TEXT AS site_quiz,
    NULL::TEXT AS adgroup_code,
    gs.login_key AS account_login,  amm.manager_login,
    NULL::TEXT AS ag_part1, NULL::TEXT AS ag_part2, NULL::TEXT AS ag_part3,
    NULL::TEXT AS ag_part4, NULL::TEXT AS ag_part5, NULL::TEXT AS ag_part6, NULL::TEXT AS ag_part7,
    ''::TEXT AS "марки авто",
    dst.leads_source_type       AS "Название crm",
    -- CDR_ZVONKI_2026-07-09: CDR-лиды маркируются 'Звонки_CDR', остальные 'заявки'
    CASE WHEN COALESCE(la.zvonki_cdr, FALSE) THEN 'Звонки_CDR' ELSE 'Заявки' END AS тип_заявки,
    la.kol_vo_zayavok, la.korr, la.kval, la.priezd, la.prodazhi,
    la.nekorr, la.ne_otvechaet, la.filtr, la.nedozvon, la.priedet,
    la.dohod_do_kredita, la.dobro,
    gs."status"  AS "статус", gs."directologist" AS "специалист", gs."site_type" AS "тип_сайта", gs."template" AS "шаблон",
    gs."salon" AS "салон", gs."city" AS "город", gs."region" AS "регион",
    gs."direction" AS direction,
    NULL::TEXT AS "неверный_кодер_new",
    la.fid,
    NULLIF(TRIM(gs.project_manager), '') AS проджект,
    gs.client_id AS id_салона,
    COALESCE(NULLIF(TRIM(gs.sales_manager),''), NULLIF(TRIM(auto.менеджер),'')) AS менеджер,
    -- KOMPLEKS_REFACTOR_REDO_2026-07-09: источник различает SEO / SEO Flow, направление = 'Комплекс'
    CASE WHEN gs."status" = 'SEO Flow' THEN 'SEO Flow' ELSE 'SEO' END AS источник,
    'Комплекс'::TEXT AS направление,
    NULL::TEXT AS "номер кампании | название кампании",
    NULL::TEXT AS "номер группы | название группы",
    NULL::INTEGER AS "План заявки",
    NULL::INTEGER AS "План приезда",
    gs.login_key || '|' || la.domain AS "аккаунт|сайт",
    NULL::INTEGER AS priezd_arrival_date,
    NULL::INTEGER AS prodazhi_arrival_date,
    'SEO'::TEXT AS поставщик,
    'seo'::TEXT     AS _source_table
FROM leads_seo_agg la
LEFT JOIN {T_GSHEET_SITES}       gs    ON la.domain = LOWER(TRIM(gs."domain"))
LEFT JOIN domain_source_type     dst   ON la.domain = dst.domain_name
LEFT JOIN account_manager_map    amm   ON gs.login_key = amm.account_login
LEFT JOIN {T_GSHEET_AUTOSALONY}  auto  ON gs.client_id = auto.id_салона AND gs.client_id IS NOT NULL
WHERE gs."domain" IS NOT NULL;
"""


# ══════════════════════════════════════════════════════════════════════════════
# big_analytics_pixel (пустая таблица, DDL создаётся всегда)
# ══════════════════════════════════════════════════════════════════════════════

def _build_pixel_sql(status_cases: str, priezd_sql: str) -> str:
    # big_analytics_pixel намеренно остаётся пустой после step3:
    # данные заполняются в step5 (step5_build_pixel/build_pixel.py).
    # step3 только создаёт DDL (через LIKE {T_DIRECT}) и очищает таблицу.
    # Аргументы status_cases/priezd_sql сохраняем в сигнатуре для обратной совместимости.
    return f"""
CREATE UNLOGGED TABLE IF NOT EXISTS {T_PIXEL} (LIKE {T_DIRECT} INCLUDING ALL);
ALTER TABLE {T_PIXEL} SET UNLOGGED;
TRUNCATE {T_PIXEL};
"""


def _build_pixel_sql_unused(status_cases: str, priezd_sql: str) -> str:
    """Не используется. Сохранён как справочник SELECT-логики пикселя."""
    common_ctes = _build_common_ctes(priezd_sql)
    return f"""
-- (справочник, не выполняется)
WITH
{common_ctes},
leads_pixel AS (
    SELECT
        id, created_date::date AS created_date, domain,
        status, source_type, fid, utm_source,
        {status_cases}
    FROM leads_deduped
    WHERE utm_source LIKE 'victory_%'
),
leads_pixel_agg AS (
    SELECT
        LOWER(TRIM(domain)) AS domain,
        created_date,
        MAX(fid) AS fid,
        MAX(utm_source) AS utm_source,
        SUM(kol_vo_zayavok) AS kol_vo_zayavok,
        SUM(korr) AS korr, SUM(priezd) AS priezd, SUM(prodazhi) AS prodazhi,
        SUM(nekorr) AS nekorr, SUM(ne_otvechaet) AS ne_otvechaet,
        SUM(filtr) AS filtr, SUM(nedozvon) AS nedozvon, SUM(priedet) AS priedet,
        SUM(dohod_do_kredita) AS dohod_do_kredita, SUM(dobro) AS dobro,
        SUM(kval)                 AS kval
    FROM leads_pixel GROUP BY LOWER(TRIM(domain)), created_date
)
SELECT
    NULL::TEXT AS key3, la.created_date AS "Date",
    CASE EXTRACT(ISODOW FROM la.created_date)
        WHEN 1 THEN '1_Понедельник' WHEN 2 THEN '2_Вторник' WHEN 3 THEN '3_Среда'
        WHEN 4 THEN '4_Четверг'    WHEN 5 THEN '5_Пятница'  WHEN 6 THEN '6_Суббота'
        WHEN 7 THEN '7_Воскресенье'
    END AS "День недели",
    DATE_TRUNC('week', la.created_date)::date AS week_start,
    NULL::BIGINT AS "CampaignId", 'pixel'::TEXT AS "CampaignName",
    NULL::BIGINT AS "AdGroupId",  'pixel'::TEXT AS "AdGroupName",
    NULL::TEXT AS "AdNetworkType", NULL::TEXT AS "Device",
    NULL::BIGINT AS "Impressions", NULL::BIGINT AS "Clicks",
    NULL::NUMERIC AS total_cost,
    la.domain,
    NULL::BIGINT AS "RlAdjustmentId", NULL::TEXT AS "RlAdjustmentId_total",
    NULL::TEXT AS campaign_code, NULL::TEXT AS tp, NULL::TEXT AS cpc_cpa, NULL::TEXT AS site_quiz,
    NULL::TEXT AS adgroup_code,
    gs.login_key AS account_login, NULL::TEXT AS manager_login,
    NULL::TEXT AS ag_part1, NULL::TEXT AS ag_part2, NULL::TEXT AS ag_part3,
    NULL::TEXT AS ag_part4, NULL::TEXT AS ag_part5, NULL::TEXT AS ag_part6, NULL::TEXT AS ag_part7,
    ''::TEXT AS "марки авто",
    dst.leads_source_type AS "Название crm", 'Заявки'::TEXT AS тип_заявки,
    la.kol_vo_zayavok, la.korr, la.kval, la.priezd, la.prodazhi,
    la.nekorr, la.ne_otvechaet, la.filtr, la.nedozvon, la.priedet,
    la.dohod_do_kredita, la.dobro,
    gs."status"  AS "статус", gs."directologist" AS "специалист", gs."site_type" AS "тип_сайта", gs."template" AS "шаблон",
    gs."salon" AS "салон", gs."city" AS "город", gs."region" AS "регион",
    gs."direction" AS direction,
    NULL::TEXT AS "неверный_кодер_new", la.fid,
    NULL::TEXT AS проджект, NULL::TEXT AS id_салона, NULL::TEXT AS менеджер,
    'Pixel'::TEXT AS источник, 'Pixel'::TEXT AS направление,
    NULL::TEXT AS "номер кампании | название кампании",
    NULL::TEXT AS "номер группы | название группы",
    NULL::INTEGER AS "План заявки", NULL::INTEGER AS "План приезда",
    gs.login_key || '|' || la.domain AS "аккаунт|сайт",
    NULL::INTEGER AS priezd_arrival_date, NULL::INTEGER AS prodazhi_arrival_date,
    'Victory'::TEXT AS поставщик,
    'pixel'::TEXT   AS _source_table
FROM leads_pixel_agg la
LEFT JOIN {T_GSHEET_SITES}   gs  ON la.domain = LOWER(TRIM(gs."domain"))
LEFT JOIN domain_source_type dst ON la.domain = dst.domain_name
WHERE 1=0;
"""


def _telegain_orders_current_cte() -> str:
    """CTE `telegain_orders_current` — проекция ТЕКУЩИХ Telega.in-заказов прогона.

    ORDERING_RACE_FIX_2026-07-15: дедуп-гарды посевов (NOT EXISTS «Путь 2 / API»)
    раньше читали `public.crop_targeting_api_telegain_lead` — таблицу, которую
    step10 (`load_telega_in_orders.py`) DROP+CREATE-пересобирает ПОСЛЕ step3.
    В pipeline.py/fast_pipeline step3 идёт раньше step10 → гард видел заказы
    ПРОШЛОГО прогона → для новой посевной кампании заказа ещё нет → лид оставался
    в social/telegram_посевы, а позже step10 доливал ту же продажу в crop_targeting
    → ЗАДВОЕНИЕ (bug BUG_posev_double_count_ordering_2026-07-15).

    Фикс: гард читает ТОТ ЖЕ источник прогона, что и step10 —
    `local_telega_in_orders` (FDW, синкается в step0, доступен step3). Логика
    effective_date / effective_domain / status='complete' воспроизводит tio_raw
    и tio_dated из `step10_crop_targeting/load_telega_in_orders.py::run_query`
    (образец там же). Проекция (utm_campaign, domain, "Date") семантически
    ИДЕНТИЧНА проекции `crop_targeting_api_telegain_lead`, но всегда свежая →
    гонка порядка исчезает без переноса шагов и без двойной сборки.

    Не f-string: содержит regex `^[0-9]{8}$` — фигурные скобки должны попасть
    в SQL как есть (значение подставляется в внешний f-string уже после разбора).
    """
    return """
telegain_orders_current AS (
    SELECT
        a.utm_campaign,
        CASE
            WHEN NULLIF(LOWER(TRIM(CASE
                    WHEN a.post_links IS NOT NULL AND a.post_links LIKE '[%'
                        THEN SUBSTRING((a.post_links::jsonb->>0) FROM '://([^/?]+)')
                    ELSE SUBSTRING(COALESCE(a.post_link, '') FROM '://([^/?]+)')
                END)), '') IS NOT NULL
                 AND LOWER(TRIM(CASE
                    WHEN a.post_links IS NOT NULL AND a.post_links LIKE '[%'
                        THEN SUBSTRING((a.post_links::jsonb->>0) FROM '://([^/?]+)')
                    ELSE SUBSTRING(COALESCE(a.post_link, '') FROM '://([^/?]+)')
                END)) NOT IN ('telega.io', 'max.ru', 't.me')
            THEN LOWER(TRIM(CASE
                    WHEN a.post_links IS NOT NULL AND a.post_links LIKE '[%'
                        THEN SUBSTRING((a.post_links::jsonb->>0) FROM '://([^/?]+)')
                    ELSE SUBSTRING(COALESCE(a.post_link, '') FROM '://([^/?]+)')
                END))
            ELSE LOWER(TRIM(SPLIT_PART(a.order_project_name, ' ', 1)))
        END AS domain,
        CASE
            WHEN a.utm_content ~ '^[0-9]{8}$' THEN TO_DATE(a.utm_content, 'DDMMYYYY')
            ELSE COALESCE(a.completed_at::date, a.done_at::date, a.created_at::date)
        END AS "Date"
    FROM public.local_telega_in_orders a
    WHERE a.status = 'complete'
)"""


def _add_telegram_to_crop_sql(status_cases: str, priezd_sql: str) -> str:
    """INSERT telegram-посевов (utm_medium='posev') в big_analytics_crop_targeting."""
    common_ctes = _build_common_ctes(priezd_sql)
    telegain_cte = _telegain_orders_current_cte()
    return f"""
WITH
{common_ctes},

{telegain_cte},

leads_tg AS (
    -- POSEVDEDUP2_2026-06-19: дедуп по lead_id (leads_deduped.id).
    -- Исключаем лид ТОЛЬКО если ЭТОТ КОНКРЕТНЫЙ id уже учтён в gsheets-ветке
    -- (crop_targeting) через load_crop_targeting_leads.py::posev_leads_attributed:
    --   лид матчится → его utm_campaign идёт через pravilo_utm (utm_effective)
    --   → находится nearest-prior placement в gsheets_crop_targeting_account
    --     на ТОМ ЖЕ домене в 90-дневном окне.
    -- Без этой проверки: лид с тем же (domain,date) но ДРУГИМ utm_campaign
    -- (не в pravilo) или без prior placement — НЕ вошёл в gsheets-ветку → он уникален
    -- → НЕ должен исключаться. Грубый (domain,date) резал таких уникальных ошибочно.
    -- Совпадение с прямой логикой posev_leads_attributed: JOIN utm_effective + gsheets.
    --
    -- POSEVDEDUP3_2026-06-19: добавлен второй путь — API (crop_targeting_api_telegain_lead).
    -- Майско-июньские лиды (с мая 2026) учтены через Telega.in API, а не gsheets.
    -- API-таблица не несёт lead_id → матч по (utm_campaign + domain + месяц±1):
    -- "Date" заказа попадает в диапазон [месяц лида - 1 мес, месяц лида + 1 мес].
    -- Ключ из load_crop_targeting_leads.py L243-253. Лид исключается из social если
    -- учтён в crop ЛЮБЫМ путём: gsheets ИЛИ API.
    SELECT
        id, created_date::date AS created_date, domain, utm_source, utm_campaign,
        status, source_type, fid,
        {status_cases}
    FROM leads_deduped
    WHERE utm_source IN ('telegram', 'stories_tg') AND utm_medium = 'posev'
      AND NOT EXISTS (
          -- Путь 1 (gsheets): лид учтён в gsheets-ветке crop_targeting если:
          -- 1. его utm_campaign матчится через pravilo_utm (utm_effective)
          -- 2. И существует gsheets-размещение на том же домене в окне 90 дней
          SELECT 1
          FROM public.gsheets_crop_targeting_account_pravilo_utm pr
          WHERE (
              LOWER(TRIM(CASE
                  WHEN pr."UTM" IS NOT NULL AND TRIM(pr."UTM") != '' THEN pr."UTM"
                  ELSE pr."utm утвержденная"
              END)) = LOWER(TRIM(leads_deduped.utm_campaign))
              OR leads_deduped.utm_campaign IS NULL
          )
          AND COALESCE(TRIM(pr."utm утвержденная"), '') != '-'
          AND COALESCE(TRIM(pr."UTM"), '') != '-'
          AND EXISTS (
              SELECT 1
              FROM public.gsheets_crop_targeting_account ga
              WHERE ga."utm утвержденная" = pr."utm утвержденная"
                AND LOWER(TRIM(ga."Сайт")) = LOWER(TRIM(leads_deduped.domain))
                AND NULLIF(TRIM(ga."Дата"), '') IS NOT NULL
                AND TO_DATE(NULLIF(TRIM(ga."Дата"), ''), 'FMDD.FMMM.YYYY')
                    <= leads_deduped.created_date::date
                AND TO_DATE(NULLIF(TRIM(ga."Дата"), ''), 'FMDD.FMMM.YYYY')
                    >= (leads_deduped.created_date::date - INTERVAL '90 days')::date
                AND TO_DATE(NULLIF(TRIM(ga."Дата"), ''), 'FMDD.FMMM.YYYY')
                    >= '2026-01-01'
          )
      )
      AND NOT EXISTS (
          -- Путь 2 (API): лид учтён в Telega.in-заказе (telegain_orders_current,
          -- ORDERING_RACE_FIX_2026-07-15 — свежий источник прогона, не step10-производная) если:
          -- utm_campaign совпадает + тот же домен + "Date" заказа в окне месяц±1 от лида.
          -- Образец: load_crop_targeting_leads.py L243-253.
          -- POSEVDEDUP3_2026-06-19
          -- POSEVDEDUP4_2026-06-19: date-гейт >= '2026-05-01' — API-дедуп только для май+.
          -- До мая crop льётся только из gsheets; API-заказов jan-apr в витрине нет.
          -- Без гейта jan-apr лиды могут ложно резаться если API-таблица дополнится ретро-данными.
          SELECT 1
          FROM telegain_orders_current t
          WHERE t.utm_campaign = leads_deduped.utm_campaign
            AND LOWER(TRIM(t.domain)) = LOWER(TRIM(leads_deduped.domain))
            AND DATE_TRUNC('month', t."Date")::date BETWEEN
                    (DATE_TRUNC('month', leads_deduped.created_date::date) - INTERVAL '1 month')::date
                AND (DATE_TRUNC('month', leads_deduped.created_date::date) + INTERVAL '1 month')::date
            AND t."Date" >= '2026-05-01'
            AND leads_deduped.created_date >= '2026-05-01'
      )
),

leads_tg_agg AS (
    SELECT
        LOWER(TRIM(domain)) AS domain,
        created_date,
        MAX(utm_source) AS utm_source,
        MAX(utm_campaign) AS utm_campaign,
        MAX(fid) AS fid,
        SUM(kol_vo_zayavok) AS kol_vo_zayavok,
        SUM(korr) AS korr, SUM(priezd) AS priezd, SUM(prodazhi) AS prodazhi,
        SUM(nekorr) AS nekorr, SUM(ne_otvechaet) AS ne_otvechaet,
        SUM(filtr) AS filtr, SUM(nedozvon) AS nedozvon, SUM(priedet) AS priedet,
        SUM(dohod_do_kredita) AS dohod_do_kredita, SUM(dobro) AS dobro,
        SUM(kval)                 AS kval
    FROM leads_tg GROUP BY LOWER(TRIM(domain)), created_date
),

-- L1: точный матч по (Сайт, utm утвержденная) — Telegram/Max/VK каналы
tg_tip_exact AS (
    SELECT
        LOWER(TRIM("Сайт"))         AS domain,
        TRIM("utm утвержденная")    AS utm_campaign,
        MAX(TRIM("Тип закупа"))     AS tip_zakupa
    FROM public.gsheets_crop_targeting_account
    WHERE "Источник" IN ('Telegram', 'Max', 'VK')
      AND TRIM("utm утвержденная") != ''
      AND TRIM("Сайт") != ''
    GROUP BY LOWER(TRIM("Сайт")), TRIM("utm утвержденная")
),

-- L2: доминирующий Тип закупа по домену из gsheets (Telegram/Max/VK)
tg_tip_domain AS (
    SELECT
        LOWER(TRIM("Сайт")) AS domain,
        TRIM("Тип закупа")  AS tip_zakupa,
        COUNT(*)             AS cnt,
        ROW_NUMBER() OVER (
            PARTITION BY LOWER(TRIM("Сайт"))
            ORDER BY COUNT(*) DESC, TRIM("Тип закупа")
        ) AS rn
    FROM public.gsheets_crop_targeting_account
    WHERE "Источник" IN ('Telegram', 'Max', 'VK')
      AND TRIM("Сайт") != ''
      AND TRIM("Тип закупа") != ''
    GROUP BY LOWER(TRIM("Сайт")), TRIM("Тип закупа")
),

-- L3: utm_campaign лида есть в local_telega_in_orders → Telega IN
tg_tip_telega AS (
    SELECT DISTINCT utm_campaign
    FROM public.local_telega_in_orders
    WHERE utm_source IN ('telegram', 'stories_tg')
      AND utm_campaign IS NOT NULL
      AND utm_campaign != ''
)

INSERT INTO {T_CROP} ({RESULT_COLUMNS})
SELECT
    NULL::TEXT,
    la.created_date,
    CASE EXTRACT(ISODOW FROM la.created_date)
        WHEN 1 THEN '1_Понедельник' WHEN 2 THEN '2_Вторник' WHEN 3 THEN '3_Среда'
        WHEN 4 THEN '4_Четверг'    WHEN 5 THEN '5_Пятница'  WHEN 6 THEN '6_Суббота'
        WHEN 7 THEN '7_Воскресенье'
    END,
    DATE_TRUNC('week', la.created_date)::date,
    NULL::BIGINT, 'telegram_посевы'::TEXT,
    NULL::BIGINT, 'telegram_посевы'::TEXT,
    NULL::TEXT, NULL::TEXT,
    NULL::BIGINT, NULL::BIGINT,
    tgl.total_cost,
    la.domain,
    NULL::BIGINT, NULL::TEXT,
    NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT,
    gs.login_key, NULL::TEXT,
    NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT,
    NULL::TEXT, NULL::TEXT, NULL::TEXT,
    ''::TEXT,
    dst.leads_source_type,
    'Заявки'::TEXT,
    la.kol_vo_zayavok, la.korr, la.kval, la.priezd, la.prodazhi,
    la.nekorr, la.ne_otvechaet, la.filtr, la.nedozvon, la.priedet,
    la.dohod_do_kredita, la.dobro,
    gs."status", gs."directologist", gs."site_type", gs."template",
    gs."salon", gs."city", gs."region",
    gs."direction",
    NULL::TEXT,
    la.fid,
    NULLIF(TRIM(gs.project_manager), ''),
    gs.client_id,
    COALESCE(NULLIF(TRIM(gs.sales_manager),''), NULLIF(TRIM(auto.менеджер),'')),
    'Посевы_Telegram'::TEXT,  -- KOMPLEKS_REFACTOR_REDO_2026-07-09
    'Комплекс'::TEXT,
    NULL::TEXT,
    NULL::TEXT,
    NULL::INTEGER,
    NULL::INTEGER,
    gs.login_key || '|' || la.domain,
    NULL::INTEGER,
    NULL::INTEGER,
    COALESCE(
        -- L1: точный матч (Сайт, utm утвержденная)
        NULLIF(tte.tip_zakupa, ''),
        -- L2: доминирующий тип по домену из gsheets
        NULLIF(ttd.tip_zakupa, ''),
        -- L3: utm_campaign есть в Telega.in заказах → Telega IN
        CASE WHEN tio.utm_campaign IS NOT NULL THEN 'Telega IN' END,
        -- L4: fallback
        'Прямой закуп'
    )::TEXT,
    'telegram'::TEXT
FROM leads_tg_agg la
LEFT JOIN {T_GSHEET_SITES}       gs    ON la.domain = LOWER(TRIM(gs."domain"))
LEFT JOIN domain_source_type     dst   ON la.domain = dst.domain_name
LEFT JOIN {T_GSHEET_AUTOSALONY}  auto  ON gs.client_id = auto.id_салона AND gs.client_id IS NOT NULL
LEFT JOIN tg_tip_exact           tte   ON la.domain = tte.domain
                                      AND TRIM(COALESCE(la.utm_campaign, '')) = tte.utm_campaign
LEFT JOIN tg_tip_domain          ttd   ON la.domain = ttd.domain
                                      AND ttd.rn = 1
LEFT JOIN tg_tip_telega          tio   ON TRIM(COALESCE(la.utm_campaign, '')) = tio.utm_campaign
LEFT JOIN public.crop_targeting_api_telegain_lead tgl
                                      ON la.domain = tgl.domain
                                     AND la.created_date = tgl."Date"
WHERE gs."domain" IS NOT NULL;
"""



def _add_social_posev_to_crop_sql(status_cases: str, priezd_sql: str) -> str:
    """INSERT социальных посевов (Max/VK/vk_storis/telegram_storis, utm_medium='posev')
    в big_analytics_crop_targeting."""
    common_ctes = _build_common_ctes(priezd_sql)
    telegain_cte = _telegain_orders_current_cte()
    return f"""
WITH
{common_ctes},

{telegain_cte},

leads_social AS (
    -- POSEVDEDUP2_2026-06-19: дедуп по lead_id (leads_deduped.id).
    -- Та же логика что в leads_tg: исключаем лид ТОЛЬКО если ЭТОТ id вошёл
    -- в gsheets-ветку (posev_leads_attributed в load_crop_targeting_leads.py).
    -- Критерий входа: utm_campaign → pravilo_utm → nearest-prior placement
    --   на том же домене в 90-дневном окне в gsheets_crop_targeting_account.
    -- «Уникальные» лиды (другой utm или нет prior placement) — НЕ исключаются.
    --
    -- POSEVDEDUP3_2026-06-19: добавлен второй путь — API (crop_targeting_api_telegain_lead).
    -- Майско-июньские Max/VK-лиды учтены через Telega.in API, а не gsheets.
    -- API-таблица не несёт lead_id → матч по (utm_campaign + domain + месяц±1).
    -- Образец: load_crop_targeting_leads.py L243-253. Лид исключается если
    -- учтён в crop ЛЮБЫМ путём: gsheets ИЛИ API.
    SELECT
        id, created_date::date AS created_date, domain, utm_source, utm_campaign,
        status, source_type, fid,
        {status_cases}
    FROM leads_deduped
    WHERE utm_source IN ('max', 'vk', 'vk_groups', 'vk_storis', 'telegram_storis')
      AND (utm_medium = 'posev' OR (utm_source = 'vk_groups' AND utm_medium = 'paid_social'))
      AND NOT EXISTS (
          -- Путь 1 (gsheets): лид учтён в gsheets-ветке crop_targeting если:
          -- 1. его utm_campaign матчится через pravilo_utm (utm_effective)
          -- 2. И существует gsheets-размещение на том же домене в окне 90 дней
          SELECT 1
          FROM public.gsheets_crop_targeting_account_pravilo_utm pr
          WHERE (
              LOWER(TRIM(CASE
                  WHEN pr."UTM" IS NOT NULL AND TRIM(pr."UTM") != '' THEN pr."UTM"
                  ELSE pr."utm утвержденная"
              END)) = LOWER(TRIM(leads_deduped.utm_campaign))
              OR leads_deduped.utm_campaign IS NULL
          )
          AND COALESCE(TRIM(pr."utm утвержденная"), '') != '-'
          AND COALESCE(TRIM(pr."UTM"), '') != '-'
          AND EXISTS (
              SELECT 1
              FROM public.gsheets_crop_targeting_account ga
              WHERE ga."utm утвержденная" = pr."utm утвержденная"
                AND LOWER(TRIM(ga."Сайт")) = LOWER(TRIM(leads_deduped.domain))
                AND NULLIF(TRIM(ga."Дата"), '') IS NOT NULL
                AND TO_DATE(NULLIF(TRIM(ga."Дата"), ''), 'FMDD.FMMM.YYYY')
                    <= leads_deduped.created_date::date
                AND TO_DATE(NULLIF(TRIM(ga."Дата"), ''), 'FMDD.FMMM.YYYY')
                    >= (leads_deduped.created_date::date - INTERVAL '90 days')::date
                AND TO_DATE(NULLIF(TRIM(ga."Дата"), ''), 'FMDD.FMMM.YYYY')
                    >= '2026-01-01'
          )
      )
      AND NOT EXISTS (
          -- Путь 2 (API): лид учтён в Telega.in-заказе (telegain_orders_current,
          -- ORDERING_RACE_FIX_2026-07-15 — свежий источник прогона, не step10-производная) если:
          -- utm_campaign совпадает + тот же домен + "Date" заказа в окне месяц±1 от лида.
          -- Образец: load_crop_targeting_leads.py L243-253.
          -- POSEVDEDUP3_2026-06-19
          -- POSEVDEDUP4_2026-06-19: date-гейт >= '2026-05-01' — API-дедуп только для май+.
          -- До мая crop льётся только из gsheets; API-заказов jan-apr в витрине нет.
          -- Без гейта jan-apr лиды могут ложно резаться если API-таблица дополнится ретро-данными.
          SELECT 1
          FROM telegain_orders_current t
          WHERE t.utm_campaign = leads_deduped.utm_campaign
            AND LOWER(TRIM(t.domain)) = LOWER(TRIM(leads_deduped.domain))
            AND DATE_TRUNC('month', t."Date")::date BETWEEN
                    (DATE_TRUNC('month', leads_deduped.created_date::date) - INTERVAL '1 month')::date
                AND (DATE_TRUNC('month', leads_deduped.created_date::date) + INTERVAL '1 month')::date
            AND t."Date" >= '2026-05-01'
            AND leads_deduped.created_date >= '2026-05-01'
      )
      AND NOT (
          -- Путь 3 (FIX_A_DEDUP_2026-07-27): исключаем лид из social_посевы если он
          -- попадает в INSERT_LOST_LEADS_SQL (FIX A, load_crop_to_big_analytics.py,
          -- CTE posev/lost) — т.е. уже будет учтён через crop_targeting.
          --
          -- Проблема: когда Telega.in отдаёт utm_campaign=NULL у заказа (сбой июль 2026),
          -- 5-польный ключ FIX A (camp+content+dom+src+med) не матчится (NULL='X'→NULL) →
          -- лид становится "потерянным" и захватывается FIX A в crop_targeting.
          -- Путь 2 тоже не срабатывал (NULL='X'→NULL в telegain_orders_current) →
          -- один лид попадал в ОБА источника (crop_targeting + social_посевы) → дубль.
          --
          -- Воспроизводим критерии posev+lost CTE FIX A ровно:
          --   utm_medium='posev' — уже в WHERE; utm_source NOT LIKE 'victory_%' — уже в WHERE;
          --   created_date >= '2026-05-01', utm_campaign IS NOT NULL, NOT LeadV,
          --   НЕТ complete-заказа по 5-польному ключу в local_telega_in_orders.
          -- Семантика NOT(A AND NOT EXISTS B) = NOT A OR EXISTS B:
          --   если матч-заказ ЕСТЬ → условие FALSE → включаем (FIX A не захватит;
          --     Путь 2 обработает если utm_campaign совпадает);
          --   если матч-заказа НЕТ → условие TRUE → исключаем (FIX A захватит
          --     через crop_targeting — ровно один раз, без дубля).
          -- Гранулярность: 5-польный ключ КОНКРЕТНОГО лида, НЕ домен целиком.
          leads_deduped.created_date >= '2026-05-01'
          AND leads_deduped.utm_campaign IS NOT NULL
          AND TRIM(leads_deduped.utm_campaign) <> ''
          AND NOT EXISTS (
              -- NOT LeadV: воспроизводим COALESCE(source_name,'') NOT ILIKE '%LeadV%' из FIX A.
              -- source_name нет в leads_deduped → подзапрос по PK (индексный, быстрый).
              SELECT 1 FROM public.local_leads_all la
              WHERE la.id = leads_deduped.id
                AND la.source_name ILIKE '%LeadV%'
          )
          AND NOT EXISTS (
              -- 5-польный ключ: camp + content + dom + src + med.
              -- Домен заказа: та же effective_domain логика что в _telegain_orders_current_cte
              -- и ord CTE FIX A (post_links jsonb → host, fallback order_project_name word 1).
              SELECT 1 FROM public.local_telega_in_orders o
              WHERE o.status = 'complete'
                AND LOWER(BTRIM(o.utm_campaign)) = LOWER(BTRIM(leads_deduped.utm_campaign))
                AND lpad(BTRIM(o.utm_content), 8, '0')
                      = lpad(BTRIM(leads_deduped.utm_content), 8, '0')
                AND LOWER(BTRIM(o.utm_source)) = LOWER(BTRIM(leads_deduped.utm_source))
                AND LOWER(BTRIM(o.utm_medium)) = LOWER(BTRIM(leads_deduped.utm_medium))
                AND CASE
                      WHEN NULLIF(LOWER(TRIM(CASE
                               WHEN o.post_links IS NOT NULL AND o.post_links LIKE '[%'
                                   THEN SUBSTRING((o.post_links::jsonb->>0) FROM '://([^/?]+)')
                               ELSE SUBSTRING(COALESCE(o.post_link, '') FROM '://([^/?]+)')
                           END)), '') IS NOT NULL
                           AND LOWER(TRIM(CASE
                               WHEN o.post_links IS NOT NULL AND o.post_links LIKE '[%'
                                   THEN SUBSTRING((o.post_links::jsonb->>0) FROM '://([^/?]+)')
                               ELSE SUBSTRING(COALESCE(o.post_link, '') FROM '://([^/?]+)')
                           END)) NOT IN ('telega.io', 'max.ru', 't.me')
                      THEN LOWER(TRIM(CASE
                               WHEN o.post_links IS NOT NULL AND o.post_links LIKE '[%'
                                   THEN SUBSTRING((o.post_links::jsonb->>0) FROM '://([^/?]+)')
                               ELSE SUBSTRING(COALESCE(o.post_link, '') FROM '://([^/?]+)')
                           END))
                      ELSE LOWER(TRIM(SPLIT_PART(o.order_project_name, ' ', 1)))
                    END = LOWER(TRIM(leads_deduped.domain))
          )
      )
),

leads_social_agg AS (
    SELECT
        LOWER(TRIM(domain)) AS domain,
        created_date,
        MAX(utm_source)      AS utm_source,
        utm_campaign,
        MAX(fid)             AS fid,
        SUM(kol_vo_zayavok) AS kol_vo_zayavok,
        SUM(korr) AS korr, SUM(priezd) AS priezd, SUM(prodazhi) AS prodazhi,
        SUM(nekorr) AS nekorr, SUM(ne_otvechaet) AS ne_otvechaet,
        SUM(filtr) AS filtr, SUM(nedozvon) AS nedozvon, SUM(priedet) AS priedet,
        SUM(dohod_do_kredita) AS dohod_do_kredita, SUM(dobro) AS dobro,
        SUM(kval) AS kval
    FROM leads_social GROUP BY LOWER(TRIM(domain)), created_date, utm_campaign
),

-- Тип закупа для Max: ищем по (Сайт, utm утвержденная). При конфликте берём MAX.
max_tip_zakupa AS (
    SELECT
        LOWER(TRIM("Сайт"))         AS domain,
        TRIM("utm утвержденная")    AS utm_campaign,
        MAX(TRIM("Тип закупа"))     AS tip_zakupa
    FROM public.gsheets_crop_targeting_account
    WHERE "Источник" = 'Max'
      AND TRIM("utm утвержденная") != ''
      AND TRIM("Сайт") != ''
    GROUP BY LOWER(TRIM("Сайт")), TRIM("utm утвержденная")
),

-- Fallback 1: доминирующий тип закупа для Max по домену (без учёта utm_campaign).
-- Используется когда utm_campaign лида не совпадает ни с одной "utm утвержденная" в gsheets.
max_tip_zakupa_domain AS (
    SELECT
        LOWER(TRIM("Сайт")) AS domain,
        TRIM("Тип закупа")  AS tip_zakupa,
        COUNT(*)             AS cnt,
        ROW_NUMBER() OVER (
            PARTITION BY LOWER(TRIM("Сайт"))
            ORDER BY COUNT(*) DESC
        ) AS rn
    FROM public.gsheets_crop_targeting_account
    WHERE "Источник" = 'Max'
      AND TRIM("Сайт") != ''
      AND TRIM("Тип закупа") != ''
    GROUP BY LOWER(TRIM("Сайт")), TRIM("Тип закупа")
),

-- Fallback 2: если домен не покрыт gsheets — проверяем local_telega_in_orders.
-- Матчинг: utm_campaign лида совпадает с utm_campaign заказа Telega.in (источник Max).
max_tip_zakupa_telega AS (
    SELECT DISTINCT utm_campaign
    FROM public.local_telega_in_orders
    WHERE utm_source = 'max'
      AND utm_campaign IS NOT NULL
      AND utm_campaign != ''
),

-- Тип закупа для VK/vk_storis: доминирующий тип по домену из gsheets (Источник='VK').
vk_tip_domain AS (
    SELECT
        LOWER(TRIM("Сайт")) AS domain,
        TRIM("Тип закупа")  AS tip_zakupa,
        COUNT(*)             AS cnt,
        ROW_NUMBER() OVER (
            PARTITION BY LOWER(TRIM("Сайт"))
            ORDER BY COUNT(*) DESC, TRIM("Тип закупа")
        ) AS rn
    FROM public.gsheets_crop_targeting_account
    WHERE "Источник" = 'VK'
      AND TRIM("Сайт") != ''
      AND TRIM("Тип закупа") != ''
    GROUP BY LOWER(TRIM("Сайт")), TRIM("Тип закупа")
),

-- Тип закупа для telegram_storis: доминирующий тип по домену из gsheets (Источник='Telegram').
tg_storis_tip_domain AS (
    SELECT
        LOWER(TRIM("Сайт")) AS domain,
        TRIM("Тип закупа")  AS tip_zakupa,
        COUNT(*)             AS cnt,
        ROW_NUMBER() OVER (
            PARTITION BY LOWER(TRIM("Сайт"))
            ORDER BY COUNT(*) DESC, TRIM("Тип закупа")
        ) AS rn
    FROM public.gsheets_crop_targeting_account
    WHERE "Источник" = 'Telegram'
      AND TRIM("Сайт") != ''
      AND TRIM("Тип закупа") != ''
    GROUP BY LOWER(TRIM("Сайт")), TRIM("Тип закупа")
),

-- Fallback VK storis через Telega.in (utm_source='stories_tg' и схожие).
tg_storis_tip_telega AS (
    SELECT DISTINCT utm_campaign
    FROM public.local_telega_in_orders
    WHERE utm_source IN ('telegram', 'stories_tg')
      AND utm_campaign IS NOT NULL
      AND utm_campaign != ''
)

INSERT INTO {T_CROP} ({RESULT_COLUMNS})
SELECT
    NULL::TEXT,
    la.created_date,
    CASE EXTRACT(ISODOW FROM la.created_date)
        WHEN 1 THEN '1_Понедельник' WHEN 2 THEN '2_Вторник' WHEN 3 THEN '3_Среда'
        WHEN 4 THEN '4_Четверг'    WHEN 5 THEN '5_Пятница'  WHEN 6 THEN '6_Суббота'
        WHEN 7 THEN '7_Воскресенье'
    END,
    DATE_TRUNC('week', la.created_date)::date,
    NULL::BIGINT, 'social_посевы'::TEXT,
    NULL::BIGINT, 'social_посевы'::TEXT,
    NULL::TEXT, NULL::TEXT,
    NULL::BIGINT, NULL::BIGINT,
    NULL::NUMERIC,
    la.domain,
    NULL::BIGINT, NULL::TEXT,
    NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT,
    gs.login_key, NULL::TEXT,
    NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT,
    NULL::TEXT, NULL::TEXT, NULL::TEXT,
    ''::TEXT,
    dst.leads_source_type,
    'Заявки'::TEXT,
    la.kol_vo_zayavok, la.korr, la.kval, la.priezd, la.prodazhi,
    la.nekorr, la.ne_otvechaet, la.filtr, la.nedozvon, la.priedet,
    la.dohod_do_kredita, la.dobro,
    gs."status", gs."directologist", gs."site_type", gs."template",
    gs."salon", gs."city", gs."region",
    gs."direction",
    NULL::TEXT,
    la.fid,
    NULLIF(TRIM(gs.project_manager), ''),
    gs.client_id,
    COALESCE(NULLIF(TRIM(gs.sales_manager),''), NULLIF(TRIM(auto.менеджер),'')),
    -- KOMPLEKS_REFACTOR_REDO_2026-07-09: социальные посевы — источник с префиксом 'Посевы_'
    CASE la.utm_source
    WHEN 'max'             THEN 'Посевы_Max'
    WHEN 'vk'              THEN 'Посевы_VK'
    WHEN 'vk_groups'       THEN 'Посевы_VK'
    WHEN 'vk_storis'       THEN 'Посевы_VK'
    WHEN 'telegram_storis' THEN 'Посевы_Telegram'
    ELSE la.utm_source
END::TEXT,
    'Комплекс'::TEXT,
    NULL::TEXT,
    NULL::TEXT,
    NULL::INTEGER,
    NULL::INTEGER,
    gs.login_key || '|' || la.domain,
    NULL::INTEGER,
    NULL::INTEGER,
    CASE la.utm_source
    WHEN 'max'             THEN COALESCE(
                                    -- 1. точный матч (Сайт, utm утвержденная)
                                    NULLIF(mtz.tip_zakupa, ''),
                                    -- 2. доминирующий тип по домену из gsheets
                                    NULLIF(mtd.tip_zakupa, ''),
                                    -- 3. utm_campaign есть в Telega.in заказах → Telega IN
                                    CASE WHEN tio.utm_campaign IS NOT NULL THEN 'Telega IN' END,
                                    -- 4. последний резерв
                                    'Max'
                                )
    WHEN 'vk'             THEN COALESCE(
                                    -- доминирующий тип по домену из gsheets VK
                                    NULLIF(vkd.tip_zakupa, ''),
                                    -- fallback
                                    'Прямой закуп'
                                )
    WHEN 'vk_groups'      THEN COALESCE(
                                    -- доминирующий тип по домену из gsheets VK
                                    NULLIF(vkd.tip_zakupa, ''),
                                    -- fallback
                                    'Прямой закуп'
                                )
    WHEN 'vk_storis'      THEN COALESCE(
                                    -- доминирующий тип по домену из gsheets VK
                                    NULLIF(vkd.tip_zakupa, ''),
                                    -- fallback
                                    'Прямой закуп'
                                )
    WHEN 'telegram_storis' THEN COALESCE(
                                    -- доминирующий тип по домену из gsheets Telegram
                                    NULLIF(tgsd.tip_zakupa, ''),
                                    -- utm_campaign есть в Telega.in → Telega IN
                                    CASE WHEN tgstio.utm_campaign IS NOT NULL THEN 'Telega IN' END,
                                    -- fallback
                                    'Прямой закуп'
                                )
    ELSE la.utm_source
END::TEXT,
    'social_посевы'::TEXT
FROM leads_social_agg la
LEFT JOIN {T_GSHEET_SITES}       gs    ON la.domain = LOWER(TRIM(gs."domain"))
LEFT JOIN domain_source_type       dst   ON la.domain = dst.domain_name
LEFT JOIN {T_GSHEET_AUTOSALONY}  auto  ON gs.client_id = auto.id_салона AND gs.client_id IS NOT NULL
LEFT JOIN max_tip_zakupa           mtz   ON la.utm_source = 'max'
                                        AND la.domain = mtz.domain
                                        AND TRIM(COALESCE(la.utm_campaign, '')) = mtz.utm_campaign
LEFT JOIN max_tip_zakupa_domain    mtd   ON la.utm_source = 'max'
                                        AND la.domain = mtd.domain
                                        AND mtd.rn = 1
LEFT JOIN max_tip_zakupa_telega    tio   ON la.utm_source = 'max'
                                        AND TRIM(COALESCE(la.utm_campaign, '')) = tio.utm_campaign
LEFT JOIN vk_tip_domain            vkd   ON la.utm_source IN ('vk', 'vk_groups', 'vk_storis')
                                        AND la.domain = vkd.domain
                                        AND vkd.rn = 1
LEFT JOIN tg_storis_tip_domain     tgsd  ON la.utm_source = 'telegram_storis'
                                        AND la.domain = tgsd.domain
                                        AND tgsd.rn = 1
LEFT JOIN tg_storis_tip_telega     tgstio ON la.utm_source = 'telegram_storis'
                                         AND TRIM(COALESCE(la.utm_campaign, '')) = tgstio.utm_campaign
WHERE gs."domain" IS NOT NULL;
"""


# ══════════════════════════════════════════════════════════════════════════════
# big_analytics_crop_targeting (из gsheets_crop_targeting_account_leads)
# ══════════════════════════════════════════════════════════════════════════════

def _build_crop_sql() -> str:
    """
    Создаёт big_analytics_crop_targeting из gsheets_crop_targeting_account_leads.
    Если таблица-источник не существует — создаётся пустая (LIKE big_analytics_direct).
    Данные обновляются через crop_targeting/pipeline.py.
    """
    return f"""
CREATE UNLOGGED TABLE IF NOT EXISTS {T_CROP} (LIKE {T_DIRECT} INCLUDING ALL);
ALTER TABLE {T_CROP} SET UNLOGGED;
-- CASCADE_MATCH_2026-07-03: миграция (IF NOT EXISTS пропускает DDL → явный ALTER для старых таблиц)
ALTER TABLE {T_CROP} ADD COLUMN IF NOT EXISTS cascade_level TEXT;
TRUNCATE {T_CROP};

DO $$
BEGIN
    IF EXISTS (
        SELECT FROM pg_tables
        WHERE schemaname = 'public'
          AND tablename  = 'gsheets_crop_targeting_account_leads'
    ) THEN
        INSERT INTO {T_CROP} (
            key3, "Date", "День недели", week_start,
            "CampaignId", "CampaignName", "AdGroupId", "AdGroupName",
            "AdNetworkType", "Device", "Impressions", "Clicks", total_cost, domain,
            "RlAdjustmentId", "RlAdjustmentId_total",
            campaign_code, tp, cpc_cpa, site_quiz, adgroup_code,
            account_login, manager_login,
            ag_part1, ag_part2, ag_part3, ag_part4, ag_part5, ag_part6, ag_part7,
            "марки авто", "Название crm", тип_заявки,
            kol_vo_zayavok, korr, kval, priezd, prodazhi, nekorr, ne_otvechaet, filtr, nedozvon, priedet,
            dohod_do_kredita, dobro,
            "статус", "специалист", "тип_сайта", "шаблон", "салон", "город", "регион",
            direction,
            "неверный_кодер_new", fid,
            проджект, id_салона, менеджер,
            источник, направление,
            "номер кампании | название кампании",
            "номер группы | название группы",
            "План заявки", "План приезда",
            "аккаунт|сайт",
            priezd_arrival_date, prodazhi_arrival_date,
            поставщик, _source_table
        )
        SELECT
            NULL::TEXT,
            TO_DATE(NULLIF(TRIM("Дата"), ''), 'DD.MM.YYYY'),
            CASE EXTRACT(ISODOW FROM TO_DATE(NULLIF(TRIM("Дата"), ''), 'DD.MM.YYYY'))
                WHEN 1 THEN '1_Понедельник' WHEN 2 THEN '2_Вторник' WHEN 3 THEN '3_Среда'
                WHEN 4 THEN '4_Четверг'    WHEN 5 THEN '5_Пятница'  WHEN 6 THEN '6_Суббота'
                WHEN 7 THEN '7_Воскресенье'
            END,
            DATE_TRUNC('week', TO_DATE(NULLIF(TRIM("Дата"), ''), 'DD.MM.YYYY'))::DATE,
            NULL::BIGINT,
            "Канал",
            NULL::BIGINT,
            NULL::TEXT,
            NULL::TEXT,
            NULL::TEXT,
            NULL::BIGINT,
            NULL::BIGINT,
            NULLIF(TRIM("total_cost"), '')::NUMERIC,
            "Сайт",
            NULL::BIGINT,
            NULL::TEXT,
            NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT,
            NULL::TEXT,
            'посевы'::TEXT,
            NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT,
            NULL::TEXT, NULL::TEXT, NULL::TEXT,
            NULL::TEXT,
            dst.leads_source_type,
            'Заявки'::TEXT,
            kol_vo_zayavok,
            korr, kval, priezd, prodazhi,
            nekorr, ne_otvechaet, filtr, nedozvon, priedet,
            NULL::BIGINT, NULL::BIGINT,
            NULL::TEXT,
            "Специалист",
            NULL::TEXT,
            NULL::TEXT,
            REPLACE(REPLACE(REPLACE("Гео",
                'АЦ на Жукова',   'Автоцентр на Жукова'),
                'АвтоПарк Южный', 'Автопарк Южный'),
                'М-Авто',         'М-авто'),
            "Гео2",
            NULL::TEXT,
            'Авто'::TEXT,
            NULL::TEXT,
            NULL::TEXT,
            NULLIF(TRIM(gs.project_manager), ''),
            gs.client_id,
            COALESCE(NULLIF(TRIM(gs.sales_manager),''), NULLIF(TRIM(auto.менеджер),'')),
            -- KOMPLEKS_REFACTOR_REDO_2026-07-09: gsheets источник с префиксом 'Посевы_'
            CASE
                WHEN TRIM("Источник") = 'Telegram' THEN 'Посевы_Telegram'
                WHEN TRIM("Источник") = 'VK'       THEN 'Посевы_VK'
                WHEN TRIM("Источник") = 'Max'      THEN 'Посевы_Max'
                ELSE NULLIF(TRIM("Источник"), '')
            END,
            'Комплекс'::TEXT,
            NULL::TEXT,
            NULL::TEXT,
            NULL::INTEGER,
            NULL::INTEGER,
            NULL::TEXT,
            NULL::INTEGER,
            NULL::INTEGER,
            NULLIF(TRIM("Тип закупа"), ''),
            'crop_targeting'::TEXT
        FROM public.gsheets_crop_targeting_account_leads
        LEFT JOIN (
            SELECT domain_name,
                   (ARRAY_AGG(leads_source_type ORDER BY crm_priority))[1] AS leads_source_type
            FROM (
                SELECT LOWER(TRIM(domain)) AS domain_name,
                       CASE source_type
                           WHEN 'marcar_crm_excel' THEN 'Маркар'
                           WHEN 'mega_crm_excel'   THEN 'Мега'
                           WHEN 'crmf_excel'       THEN 'Фаиг'
                           WHEN 'plex_excel'       THEN 'Плекс'
                           WHEN 'redauto_excel'    THEN 'Ред Авто'
                           WHEN 'genzes_excel'     THEN 'Генезис'
                           WHEN 'mauto_excel'      THEN 'МаАвто'
                           ELSE source_type
                       END AS leads_source_type,
                       CASE source_type
                           WHEN 'marcar_crm_excel' THEN 1
                           WHEN 'mega_crm_excel'   THEN 2
                           WHEN 'crmf_excel'       THEN 3
                           WHEN 'plex_excel'       THEN 4
                           WHEN 'redauto_excel'    THEN 5
                           WHEN 'genzes_excel'     THEN 6
                           WHEN 'mauto_excel'      THEN 7
                           ELSE 9
                       END AS crm_priority
                FROM {T_RAW_LEADS}
                WHERE domain IS NOT NULL AND domain != ''
            ) u GROUP BY domain_name
        ) dst ON LOWER(TRIM("Сайт")) = dst.domain_name
        LEFT JOIN (
            SELECT DISTINCT salon, client_id, project_manager, sales_manager, crm
            FROM {T_GSHEET_SITES} WHERE salon IS NOT NULL
        ) gs ON LOWER(TRIM(
            REPLACE("Гео", 'АЦ на Жукова', 'Автоцентр на Жукова')
        )) = LOWER(TRIM(gs.salon))
        LEFT JOIN {T_GSHEET_AUTOSALONY} auto ON gs.client_id = auto.id_салона AND gs.client_id IS NOT NULL;
    END IF;
END
$$;
"""


_CROP_DOMAIN_SUBQUERY = """
    SELECT DISTINCT LOWER(TRIM("Сайт"))
    FROM public.gsheets_crop_targeting_account
    WHERE "Сайт" IS NOT NULL AND TRIM("Сайт") != ''
"""


def _add_crop_calls_sql(calls_agg_cases: str) -> str:
    """INSERT звонков для не-Яндекс (посевы) доменов в big_analytics_crop_targeting."""
    return f"""
INSERT INTO {T_CROP} ({RESULT_COLUMNS})
SELECT
    NULL::TEXT,
    c.created_date::date,
    CASE EXTRACT(ISODOW FROM c.created_date::date)
        WHEN 1 THEN '1_Понедельник' WHEN 2 THEN '2_Вторник'  WHEN 3 THEN '3_Среда'
        WHEN 4 THEN '4_Четверг'    WHEN 5 THEN '5_Пятница'   WHEN 6 THEN '6_Суббота'
        WHEN 7 THEN '7_Воскресенье'
    END,
    DATE_TRUNC('week', c.created_date::date)::date,
    NULL::BIGINT, 'звонки'::TEXT,
    NULL::BIGINT, 'звонки'::TEXT,
    'Звонки'::TEXT, 'звонки'::TEXT,
    NULL::BIGINT,  NULL::BIGINT,
    NULL::NUMERIC,
    LOWER(TRIM(c.domain)),
    NULL::BIGINT,
    NULL::TEXT,
    'звонки'::TEXT, 'звонки'::TEXT, 'звонки'::TEXT, 'звонки'::TEXT, 'звонки'::TEXT,
    gs.login_key, amm.manager_login,
    'звонки'::TEXT, 'звонки'::TEXT, 'звонки'::TEXT, 'звонки'::TEXT,
    'звонки'::TEXT, 'звонки'::TEXT, 'звонки'::TEXT,
    ''::TEXT,
    dst.leads_source_type,
    'Звонки'::TEXT,
    {calls_agg_cases},
    gs."status", gs."directologist", gs."site_type", gs."template",
    gs."salon", gs."city", gs."region",
    'Авто'::TEXT,
    NULL::TEXT,
    NULL::TEXT,
    NULLIF(TRIM(gs.project_manager), ''),
    gs.client_id,
    COALESCE(NULLIF(TRIM(gs.sales_manager),''), NULLIF(TRIM(auto.менеджер),'')),
    'Посевы_Звонки'::TEXT,  -- KOMPLEKS_REFACTOR_REDO_2026-07-09
    'Комплекс'::TEXT,
    NULL::TEXT,
    NULL::TEXT,
    CASE WHEN DATE_TRUNC('month', c.created_date::date) = DATE_TRUNC('month', CURRENT_DATE)
         THEN NULLIF(REPLACE(REPLACE(REPLACE(pf."цена_заявки", chr(160), ''), ' ', ''), ',', '.'), '-')::NUMERIC::INTEGER
         ELSE NULL END,
    CASE WHEN DATE_TRUNC('month', c.created_date::date) = DATE_TRUNC('month', CURRENT_DATE)
         THEN NULLIF(REPLACE(REPLACE(REPLACE(pf."цена_приезда", chr(160), ''), ' ', ''), ',', '.'), '-')::NUMERIC::INTEGER
         ELSE NULL END,
    gs.login_key || '|' || LOWER(TRIM(c.domain)),
    NULL::INTEGER,
    NULL::INTEGER,
    'звонки'::TEXT,
    'calls'::TEXT
FROM {T_RAW_CALLS} c
LEFT JOIN {T_GSHEET_SITES}       gs    ON LOWER(TRIM(c.domain)) = LOWER(TRIM(gs."domain"))
LEFT JOIN (
    SELECT domain_name,
           (ARRAY_AGG(leads_source_type ORDER BY crm_priority))[1] AS leads_source_type
    FROM (
        SELECT LOWER(TRIM(domain)) AS domain_name,
               CASE source_type
                   WHEN 'marcar_crm_excel' THEN 'Маркар'
                   WHEN 'mega_crm_excel'   THEN 'Мега'
                   WHEN 'crmf_excel'       THEN 'Фаиг'
                   WHEN 'plex_excel'       THEN 'Плекс'
                   WHEN 'redauto_excel'    THEN 'Ред Авто'
                   WHEN 'genzes_excel'     THEN 'Генезис'
                   WHEN 'mauto_excel'      THEN 'МаАвто'
                   ELSE source_type
               END AS leads_source_type,
               CASE source_type
                   WHEN 'marcar_crm_excel' THEN 1
                   WHEN 'mega_crm_excel'   THEN 2
                   WHEN 'crmf_excel'       THEN 3
                   WHEN 'plex_excel'       THEN 4
                   WHEN 'redauto_excel'    THEN 5
                   WHEN 'genzes_excel'     THEN 6
                   WHEN 'mauto_excel'      THEN 7
                   ELSE 9
               END AS crm_priority
        FROM {T_RAW_CALLS}
        WHERE domain IS NOT NULL AND domain != ''
    ) u GROUP BY domain_name
) dst ON LOWER(TRIM(c.domain)) = dst.domain_name
LEFT JOIN (
    SELECT account_login, MAX(manager_login) AS manager_login
    FROM {T_DIRECT} WHERE manager_login IS NOT NULL GROUP BY account_login
) amm ON gs.login_key = amm.account_login
LEFT JOIN {T_GSHEET_AUTOSALONY}  auto   ON gs.client_id = auto.id_салона AND gs.client_id IS NOT NULL
LEFT JOIN {T_GSHEET_PLAN_FAKT}   pf     ON LOWER(TRIM(gs."salon"))     = LOWER(TRIM(pf."салон"))
                                        AND LOWER(TRIM(gs."site_type")) = LOWER(TRIM(pf."тип"))
WHERE LOWER(TRIM(c.domain)) IN ({_CROP_DOMAIN_SUBQUERY})
  -- NOT EXISTS убран: step6 уже исключает crop-targeting домены из звонков big_analytics_full
  -- (step6 строки 227-231: NOT IN gsheets_crop_targeting_account).
  -- Старый NOT EXISTS блокировал звонки для crop-доменов с реальным login_key (не 'Нет'),
  -- что приводило к потере ~1,324 звонков. Дублирования нет — step6 и step3 взаимоисключают.
GROUP BY
    c.created_date::date, LOWER(TRIM(c.domain)),
    gs.login_key, gs."status", gs."directologist", gs."site_type",
    gs."template", gs."salon", gs."city", gs."region", gs."direction",
    gs.project_manager, gs.client_id,
    gs.sales_manager, auto.менеджер,
    amm.manager_login, dst.leads_source_type,
    pf."цена_заявки", pf."цена_приезда";
"""


def _add_crop_seo_sql(status_cases: str, priezd_sql: str) -> str:
    """INSERT SEO-лидов для не-Яндекс (посевы) доменов в big_analytics_crop_targeting."""
    common_ctes = _build_common_ctes(priezd_sql)
    return f"""
WITH
{common_ctes},

leads_crop_seo AS (
    SELECT
        id, created_date::date AS created_date, domain_id, domain,
        status, source_type, fid,
        -- CDR_SPLIT_2026-07-27: расширенный паттерн (CDR_PATTERN из config/settings.py)
        (COALESCE(utm_content, '') ~* '{CDR_PATTERN}') AS zvonki_cdr,
        {status_cases}
    FROM leads_deduped
    WHERE (
        (utm_source IS NULL OR utm_source = '')
        OR (utm_source = 'seo' AND utm_medium = 'organic')
    )
    AND LOWER(TRIM(domain)) IN ({_CROP_DOMAIN_SUBQUERY})
),

leads_crop_seo_agg AS (
    -- CDR_SPLIT_2026-07-27: zvonki_cdr в GROUP BY (по лиду, не BOOL_OR)
    SELECT
        LOWER(TRIM(domain))     AS domain,
        created_date,
        zvonki_cdr,
        MAX(fid)                AS fid,
        SUM(kol_vo_zayavok)     AS kol_vo_zayavok,
        SUM(korr)               AS korr,
        SUM(priezd)             AS priezd,
        SUM(prodazhi)           AS prodazhi,
        SUM(nekorr)             AS nekorr,
        SUM(ne_otvechaet)       AS ne_otvechaet,
        SUM(filtr)              AS filtr,
        SUM(nedozvon)           AS nedozvon,
        SUM(priedet)            AS priedet,
        SUM(dohod_do_kredita)   AS dohod_do_kredita,
        SUM(dobro)              AS dobro,
        SUM(kval)               AS kval
    FROM leads_crop_seo
    GROUP BY LOWER(TRIM(domain)), created_date, zvonki_cdr
)

INSERT INTO {T_CROP} ({RESULT_COLUMNS})
SELECT
    NULL::TEXT,
    la.created_date,
    CASE EXTRACT(ISODOW FROM la.created_date)
        WHEN 1 THEN '1_Понедельник' WHEN 2 THEN '2_Вторник' WHEN 3 THEN '3_Среда'
        WHEN 4 THEN '4_Четверг'    WHEN 5 THEN '5_Пятница'  WHEN 6 THEN '6_Суббота'
        WHEN 7 THEN '7_Воскресенье'
    END,
    DATE_TRUNC('week', la.created_date)::date,
    NULL::BIGINT, 'seo'::TEXT,
    NULL::BIGINT, 'seo'::TEXT,
    NULL::TEXT,   NULL::TEXT,
    NULL::BIGINT, NULL::BIGINT,
    NULL::NUMERIC,
    la.domain,
    NULL::BIGINT, NULL::TEXT,
    NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT,
    gs.login_key, amm.manager_login,
    NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT,
    NULL::TEXT, NULL::TEXT, NULL::TEXT,
    ''::TEXT,
    dst.leads_source_type,
    -- CDR_ZVONKI_2026-07-09: CDR-лиды посевных доменов маркируются 'Звонки_CDR'
    CASE WHEN COALESCE(la.zvonki_cdr, FALSE) THEN 'Звонки_CDR' ELSE 'Заявки' END,
    la.kol_vo_zayavok, la.korr, la.kval, la.priezd, la.prodazhi,
    la.nekorr, la.ne_otvechaet, la.filtr, la.nedozvon, la.priedet,
    la.dohod_do_kredita, la.dobro,
    gs."status", gs."directologist", gs."site_type", gs."template",
    gs."salon", gs."city", gs."region",
    'Авто'::TEXT,
    NULL::TEXT,
    la.fid,
    NULLIF(TRIM(gs.project_manager), ''),
    gs.client_id,
    COALESCE(NULLIF(TRIM(gs.sales_manager),''), NULLIF(TRIM(auto.менеджер),'')),
    'Посевы_SEO'::TEXT,  -- KOMPLEKS_REFACTOR_REDO_2026-07-09
    'Комплекс'::TEXT,
    NULL::TEXT,
    NULL::TEXT,
    NULL::INTEGER,
    NULL::INTEGER,
    gs.login_key || '|' || la.domain,
    NULL::INTEGER,
    NULL::INTEGER,
    'SEO'::TEXT,
    'seo'::TEXT
FROM leads_crop_seo_agg la
LEFT JOIN {T_GSHEET_SITES}       gs    ON la.domain = LOWER(TRIM(gs."domain"))
LEFT JOIN domain_source_type     dst   ON la.domain = dst.domain_name
LEFT JOIN account_manager_map    amm   ON gs.login_key = amm.account_login
LEFT JOIN {T_GSHEET_AUTOSALONY}  auto  ON gs.client_id = auto.id_салона AND gs.client_id IS NOT NULL
WHERE gs."domain" IS NOT NULL;
"""


def _add_vk_ads_to_crop_sql(status_cases: str, priezd_sql: str) -> str:
    """INSERT расхода ВК Реклама + orphan-лидов в big_analytics_crop_targeting.

    Только аккаунты с direction='Авто' в local_gsheet_sites:
      - vk_client_id=1090694302 → autopro-116.site (Казань Центр Авто)
      - vk_client_id=1090694347 → autocenter-152.site (Нижний Центр Авто)
      - vk_client_id=1090694251 → autodrive-102.site (Уфа Центр Авто)

    Два блока (UNION ALL):
    1. vk_ads  — строки расхода из local_vk_ads_stats_day, воронка через LEFT JOIN leads_vk_agg.
                 _source_table='vk_ads', total_cost=spent.
    2. vk_zero — лиды на VK-доменах без числового utm_campaign ("зазор" — нигде не учтены).
                 utm_source='' / NULL (исключены из SEO по VK_ADS_LEADS_EXCLUSION_2026-07-07)
                 utm_source='vkads' с нечисловым campaign (исключены из leads_direct строка ~307).
                 _source_table='vk_zero', total_cost=NULL.

    VK_ADS_FUNNEL_2026-07-07: реальная воронка через leads_vk / leads_vk_agg.
    Ключ матчинга лида (vk_ads): domain + created_date + utm_campaign (= ad_plan_id).
    VK_ADS_ZERO_LEADS_2026-07-07: лиды без числового utm_campaign → vk_zero.
    utm_source='vkads' исключён из leads_direct (правка 1); NULL-utm на VK-доменах — из leads_seo (правка 2).
    VK_ADS_INTEGRATION_2026-07-06
    """
    common_ctes = _build_common_ctes(priezd_sql)
    return f"""
-- VK_ADS_INTEGRATION_2026-07-06 | VK_ADS_FUNNEL_2026-07-07 | VK_ADS_ZERO_LEADS_2026-07-07
WITH
{common_ctes},

vk_sites AS MATERIALIZED (
    -- DISTINCT ON (vk_client_id): защита от фанаута (несколько login_key на один vk_client_id).
    -- Фильтр direction='Авто' сужает до 2 аккаунтов Авто-направления.
    -- Тай-брейкер ORDER BY domain (алфавит) — детерминированный выбор.
    SELECT DISTINCT ON (gs.vk_client_id)
        -- VK_ADS_BIGINT_FIX_2026-07-07: vk_client_id — TEXT в gsheet_sites, пустая строка ≠ NULL.
        -- NULLIF + TRIM чтобы '' → NULL (не падало с "invalid input syntax for type bigint: ''").
        NULLIF(TRIM(gs.vk_client_id), '')::bigint  AS vk_client_id,
        LOWER(TRIM(gs."domain"))        AS domain,
        gs."status"                     AS gs_status,
        gs."directologist"              AS directologist,
        gs."salon"                      AS salon,
        gs."city"                       AS city,
        gs."region"                     AS region,
        gs.direction                    AS direction,
        gs.client_id                    AS client_id,
        gs.project_manager              AS project_manager,
        gs.sales_manager                AS sales_manager,
        gs.login_key                    AS login_key
    FROM {T_GSHEET_SITES} gs
    WHERE TRIM(gs.vk_client_id) != ''  -- VK_ADS_BIGINT_FIX_2026-07-07: IS NOT NULL не фильтрует ''
      AND gs.vk_client_id IS NOT NULL
      AND gs.direction = 'Авто'
    ORDER BY gs.vk_client_id, gs."domain"
),

-- VK_ADS_BANNER_GRAIN_2026-07-10: local_vk_ads_stats_day теперь banner×date (детальная
-- грань для датамарта fact_vk_ads). Ветка vk_ads (расход Комплекс-VK) строит строки на
-- уровне (date, account_id, ad_plan_id) — ре-агрегируем сюда SUM(spent) по офферу.
-- Совместимость: сумма расхода по плану идентична прежней (плановой) грануле → расход
-- VK Ads Комплекса (~531k) не меняется; key3 account_id|date|ad_plan_id остаётся
-- уникальным (GROUP BY схлопывает баннеры) → двойного счёта расхода/воронки нет.
vk_ads_by_plan AS (
    SELECT
        vk.date              AS date,
        vk.account_id        AS account_id,
        vk.ad_plan_id        AS ad_plan_id,
        MAX(vk.ad_plan_name) AS ad_plan_name,
        SUM(vk.spent)        AS spent
    FROM public.{T_LOCAL_VK_ADS} vk
    GROUP BY vk.date, vk.account_id, vk.ad_plan_id
),

-- VK_ADS_FUNNEL_2026-07-07: лиды с доменов VK Ads (direction='Авто')
-- Ключ матчинга: domain + created_date + utm_campaign (числовой ad_plan_id).
-- Guard ~ '^[0-9]+$': защита от нечисловых utm_campaign (не падаем с cast error).
leads_vk AS (
    SELECT
        LOWER(TRIM(l.domain))        AS domain,
        l.created_date::date         AS created_date,
        l.utm_campaign::bigint       AS ad_plan_id,
        {status_cases}
    FROM leads_deduped l
    WHERE l.utm_campaign IS NOT NULL
      AND l.utm_campaign ~ '^[0-9]+$'
      AND LOWER(TRIM(l.domain)) IN (SELECT domain FROM vk_sites)
),

leads_vk_agg AS (
    -- VK_ADS_FUNNEL_2026-07-07: агрегат лидов по domain + date + ad_plan_id
    SELECT
        domain,
        created_date,
        ad_plan_id,
        SUM(kol_vo_zayavok)     AS kol_vo_zayavok,
        SUM(korr)               AS korr,
        SUM(kval)               AS kval,
        SUM(priezd)             AS priezd,
        SUM(prodazhi)           AS prodazhi,
        SUM(nekorr)             AS nekorr,
        SUM(ne_otvechaet)       AS ne_otvechaet,
        SUM(filtr)              AS filtr,
        SUM(nedozvon)           AS nedozvon,
        SUM(priedet)            AS priedet,
        SUM(dohod_do_kredita)   AS dohod_do_kredita,
        SUM(dobro)              AS dobro
    FROM leads_vk
    GROUP BY domain, created_date, ad_plan_id
),

-- VK_ADS_ZERO_LEADS_2026-07-07: лиды на VK-доменах без числового utm_campaign.
-- Покрывает "зазор" — лиды которые нигде не учтены:
--   utm_source='' / NULL → исключены из SEO (VK_ADS_LEADS_EXCLUSION_2026-07-07) и из leads_direct (строка ~298)
--   utm_source='vkads' с нечисловым campaign → исключены из leads_direct (строка ~307)
-- НЕ пересекается с leads_vk_agg: условия utm_campaign взаимоисключающие
--   (leads_vk: IS NOT NULL + ~ '^[0-9]+$'; leads_vk_zero: IS NULL OR !~ '^[0-9]+$').
-- НЕ пересекается с leads_direct: только utm_source='' или 'vkads' — они не входят в leads_direct.
-- НЕ дублирует direct_zero: лиды utm_source='direct' на VK-доменах идут в leads_direct, здесь исключены.
leads_vk_zero AS (
    SELECT
        LOWER(TRIM(l.domain))   AS domain,
        l.created_date::date    AS created_date,
        {status_cases}
    FROM leads_deduped l
    WHERE LOWER(TRIM(l.domain)) IN (SELECT domain FROM vk_sites)
      AND (l.utm_campaign IS NULL OR l.utm_campaign !~ '^[0-9]+$')
      AND (l.utm_source IS NULL OR l.utm_source = '' OR l.utm_source = 'vkads')
),

leads_vk_zero_agg AS (
    -- Агрегат по domain + date (нет ad_plan_id — нет числового utm_campaign)
    SELECT
        domain,
        created_date,
        SUM(kol_vo_zayavok)     AS kol_vo_zayavok,
        SUM(korr)               AS korr,
        SUM(kval)               AS kval,
        SUM(priezd)             AS priezd,
        SUM(prodazhi)           AS prodazhi,
        SUM(nekorr)             AS nekorr,
        SUM(ne_otvechaet)       AS ne_otvechaet,
        SUM(filtr)              AS filtr,
        SUM(nedozvon)           AS nedozvon,
        SUM(priedet)            AS priedet,
        SUM(dohod_do_kredita)   AS dohod_do_kredita,
        SUM(dobro)              AS dobro
    FROM leads_vk_zero
    GROUP BY domain, created_date
),

-- ══════════════════════════════════════════════════════════════════════════════
-- VK_PERFORM_LEADS_2026-07-10: потерянные vkads-заявки ПЕРФОРМА (Вариант B).
-- Заявки source_name 'LeadVDL Perform …', utm_source='vkads', utm_campaign='victory',
-- source_type='crmf_excel', domain_id=NULL — выпадали из витрины НАСОВСЕМ:
--   • в raw_leads они попадают (step1 _excluded_domains_sql: "OR domain_id IS NULL"),
--   • но leads_deduped их исключает: raw_leads-ветка фильтрует
--     `LOWER(TRIM(domain)) NOT IN perform_domains`; при domain=NULL это NULL→строка выпадает,
--   • поэтому они не доходят ни до leads_direct (L307 vkads-глушилка тут уже неактуальна —
--     их там просто нет), ни до leads_vk_zero (нет domain для vk_sites).
-- Ловим их ОТДЕЛЬНОЙ веткой прямо из raw_leads. Уникальный признак Перформ-vkads —
-- utm_campaign='victory' (Комплекс-vkads имеют numeric/пустой campaign + реальный domain →
-- их ловят leads_vk / leads_vk_zero). Источник/поставщик/тип_заявки — как у Комплекс-VK.
-- direction='Перформ', id_салона='avto_0415', total_cost=NULL, _source_table='vk_perform'.
-- ══════════════════════════════════════════════════════════════════════════════
leads_perform_vk AS (
    SELECT
        COALESCE(NULLIF(TRIM(l.salon), ''), 'Перформ РФ') AS salon,
        l.created_date::date AS created_date,
        {status_cases}
    FROM {T_RAW_LEADS} l
    WHERE l.utm_source = 'vkads'
      AND l.utm_campaign = 'victory'
      -- страховка от коллизии с Комплекс-VK веткой (у Перформ-vkads domain=NULL)
      AND (l.domain IS NULL OR LOWER(TRIM(l.domain)) NOT IN (SELECT domain FROM vk_sites))
      -- защита от двойного учёта: телефон не должен уже присутствовать в perform-direct пути
      -- (дизъюнктность подтверждена: 0 пересечения; guard на будущее)
      AND NOT EXISTS (
          SELECT 1 FROM {T_RAW_PERFORM_LEADS} rpl WHERE rpl.phone = l.phone
      )
),

leads_perform_vk_agg AS (
    -- Агрегат по реальному салону лида + дате. domain=NULL (нет в лидах), id_салона='avto_0415'.
    SELECT
        salon,
        created_date,
        SUM(kol_vo_zayavok)     AS kol_vo_zayavok,
        SUM(korr)               AS korr,
        SUM(kval)               AS kval,
        SUM(priezd)             AS priezd,
        SUM(prodazhi)           AS prodazhi,
        SUM(nekorr)             AS nekorr,
        SUM(ne_otvechaet)       AS ne_otvechaet,
        SUM(filtr)              AS filtr,
        SUM(nedozvon)           AS nedozvon,
        SUM(priedet)            AS priedet,
        SUM(dohod_do_kredita)   AS dohod_do_kredita,
        SUM(dobro)              AS dobro
    FROM leads_perform_vk
    GROUP BY salon, created_date
),

perform_vk_site AS (
    -- Представительский Авто-домен Перформа (client_id='avto_0415', niche='Авто') —
    -- ТЕХНИЧЕСКИЙ носитель domain, чтобы строки прошли build_star.FACT_AUTO_WHERE
    -- (domain лидов = NULL → иначе выпали бы из fact_big_analytics, как было с pixel_pr).
    -- Бизнес-идентичность строки несут салон (реальный) + id_салона='avto_0415' + направление='Перформ'.
    SELECT
        LOWER(TRIM(gs."domain")) AS domain,
        gs.login_key             AS login_key
    FROM {T_GSHEET_SITES} gs
    WHERE gs.client_id = 'avto_0415'
      AND gs.niche = 'Авто'
      AND gs."domain" IS NOT NULL
      AND TRIM(gs."domain") <> ''
    ORDER BY LOWER(TRIM(gs."domain"))
    LIMIT 1
)

INSERT INTO {T_CROP} ({RESULT_COLUMNS})
-- Часть 1: строки расхода ВК Реклама (vk_ads) — данные из local_vk_ads_stats_day
SELECT
    -- key3: account_id|date|ad_plan_id (уникальный ключ строки)
    vk.account_id::text || '|' || vk.date::text || '|' || vk.ad_plan_id::text,
    -- "Date"
    vk.date,
    -- "День недели"
    CASE EXTRACT(ISODOW FROM vk.date)
        WHEN 1 THEN '1_Понедельник' WHEN 2 THEN '2_Вторник' WHEN 3 THEN '3_Среда'
        WHEN 4 THEN '4_Четверг'    WHEN 5 THEN '5_Пятница'  WHEN 6 THEN '6_Суббота'
        WHEN 7 THEN '7_Воскресенье'
    END,
    -- week_start
    DATE_TRUNC('week', vk.date)::date,
    -- "CampaignId": ad_plan_id как суррогат CampaignId
    vk.ad_plan_id,
    -- "CampaignName": ad_plan_id|ad_plan_name (формат «номер|имя», как в Директе)
    vk.ad_plan_id::text || '|' || COALESCE(vk.ad_plan_name, vk.ad_plan_id::text),
    -- "AdGroupId", "AdGroupName"
    NULL::BIGINT, NULL::TEXT,
    -- "AdNetworkType", "Device"
    NULL::TEXT, NULL::TEXT,
    -- "Impressions", "Clicks"
    NULL::BIGINT, NULL::BIGINT,
    -- total_cost
    vk.spent,
    -- domain
    vs.domain,
    -- "RlAdjustmentId", "RlAdjustmentId_total"
    NULL::BIGINT, NULL::TEXT,
    -- campaign_code, tp, cpc_cpa, site_quiz, adgroup_code
    NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT,
    -- account_login, manager_login
    vs.login_key, NULL::TEXT,
    -- ag_part1 .. ag_part7
    NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT,
    -- "марки авто", "Название crm", тип_заявки  -- VK_ADS_INTEGRATION_2026-07-06: тип_заявки='Заявки'
    ''::TEXT, NULL::TEXT, 'Заявки'::TEXT,
    -- VK_ADS_FUNNEL_2026-07-07: реальная воронка из leads_vk_agg (LEFT JOIN по domain+date+ad_plan_id)
    -- kol_vo_zayavok, korr, kval, priezd, prodazhi, nekorr, ne_otvechaet, filtr, nedozvon, priedet
    COALESCE(lva.kol_vo_zayavok, 0)::BIGINT, COALESCE(lva.korr, 0)::BIGINT,
    COALESCE(lva.kval, 0)::BIGINT, COALESCE(lva.priezd, 0)::BIGINT,
    COALESCE(lva.prodazhi, 0)::BIGINT,
    COALESCE(lva.nekorr, 0)::BIGINT, COALESCE(lva.ne_otvechaet, 0)::BIGINT,
    COALESCE(lva.filtr, 0)::BIGINT, COALESCE(lva.nedozvon, 0)::BIGINT,
    COALESCE(lva.priedet, 0)::BIGINT,
    -- dohod_do_kredita, dobro
    COALESCE(lva.dohod_do_kredita, 0)::BIGINT, COALESCE(lva.dobro, 0)::BIGINT,
    -- "статус", "специалист", "тип_сайта", "шаблон", "салон", "город", "регион"
    vs.gs_status, vs.directologist, NULL::TEXT, NULL::TEXT, vs.salon, vs.city, vs.region,
    -- direction (из gsheet_sites, = 'Авто' для всех VK Авто-аккаунтов)
    vs.direction,
    -- "неверный_кодер_new", fid
    NULL::TEXT, NULL::TEXT,
    -- проджект, id_салона, менеджер
    NULLIF(TRIM(vs.project_manager), ''), vs.client_id, NULLIF(TRIM(vs.sales_manager), ''),
    -- источник, направление  -- KOMPLEKS_REFACTOR_REDO_2026-07-09
    'VK Ads'::TEXT, 'Комплекс'::TEXT,
    -- "номер кампании | название кампании"
    vk.ad_plan_id::text || '|' || COALESCE(vk.ad_plan_name, vk.ad_plan_id::text),
    -- "номер группы | название группы"
    NULL::TEXT,
    -- "План заявки", "План приезда"
    NULL::INTEGER, NULL::INTEGER,
    -- "аккаунт|сайт"
    vs.login_key || '|' || vs.domain,
    -- priezd_arrival_date, prodazhi_arrival_date
    NULL::BIGINT, NULL::BIGINT,
    -- поставщик
    'ВК Реклама'::TEXT,
    -- _source_table
    'vk_ads'::TEXT
-- VK_ADS_BANNER_GRAIN_2026-07-10: читаем ре-агрегат по офферу (не детальную banner-grain
-- таблицу напрямую) — иначе key3 account_id|date|ad_plan_id дублировался бы по баннерам
-- → двойной учёт расхода/воронки Комплекс-VK.
FROM vk_ads_by_plan vk
JOIN vk_sites vs ON vs.vk_client_id = vk.account_id
-- VK_ADS_FUNNEL_2026-07-07: матчинг лидов по domain + date + ad_plan_id
LEFT JOIN leads_vk_agg lva ON lva.domain = vs.domain
    AND lva.created_date = vk.date
    AND lva.ad_plan_id = vk.ad_plan_id

UNION ALL

-- VK_ADS_ZERO_LEADS_2026-07-07: Часть 2 — лиды без числового utm_campaign (vk_zero).
-- Строки для лидов с неизвестным/нечисловым utm_campaign на VK-доменах.
-- total_cost = NULL (нет расходных данных), _source_table = 'vk_zero'.
SELECT
    -- key3: domain|date|vk_zero (синтетический ключ — нет ad_plan_id)
    lvz.domain || '|' || lvz.created_date::text || '|vk_zero',
    -- "Date"
    lvz.created_date,
    -- "День недели"
    CASE EXTRACT(ISODOW FROM lvz.created_date)
        WHEN 1 THEN '1_Понедельник' WHEN 2 THEN '2_Вторник' WHEN 3 THEN '3_Среда'
        WHEN 4 THEN '4_Четверг'    WHEN 5 THEN '5_Пятница'  WHEN 6 THEN '6_Суббота'
        WHEN 7 THEN '7_Воскресенье'
    END,
    -- week_start
    DATE_TRUNC('week', lvz.created_date)::date,
    -- "CampaignId", "CampaignName"
    NULL::BIGINT, 'нет campaign'::TEXT,
    -- "AdGroupId", "AdGroupName"
    NULL::BIGINT, NULL::TEXT,
    -- "AdNetworkType", "Device"
    NULL::TEXT, NULL::TEXT,
    -- "Impressions", "Clicks"
    NULL::BIGINT, NULL::BIGINT,
    -- total_cost: NULL (нет расхода — лид не матчится к stats по utm_campaign)
    NULL::NUMERIC,
    -- domain
    lvz.domain,
    -- "RlAdjustmentId", "RlAdjustmentId_total"
    NULL::BIGINT, NULL::TEXT,
    -- campaign_code, tp, cpc_cpa, site_quiz, adgroup_code
    NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT,
    -- account_login, manager_login
    vs.login_key, NULL::TEXT,
    -- ag_part1 .. ag_part7
    NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT,
    -- "марки авто", "Название crm", тип_заявки  -- VK_ADS_INTEGRATION_2026-07-06: тип_заявки='Заявки'
    ''::TEXT, NULL::TEXT, 'Заявки'::TEXT,
    -- Воронка из leads_vk_zero_agg
    -- kol_vo_zayavok, korr, kval, priezd, prodazhi, nekorr, ne_otvechaet, filtr, nedozvon, priedet
    lvz.kol_vo_zayavok::BIGINT, lvz.korr::BIGINT,
    lvz.kval::BIGINT, lvz.priezd::BIGINT,
    lvz.prodazhi::BIGINT,
    lvz.nekorr::BIGINT, lvz.ne_otvechaet::BIGINT, lvz.filtr::BIGINT,
    lvz.nedozvon::BIGINT, lvz.priedet::BIGINT,
    -- dohod_do_kredita, dobro
    lvz.dohod_do_kredita::BIGINT, lvz.dobro::BIGINT,
    -- "статус", "специалист", "тип_сайта", "шаблон", "салон", "город", "регион"
    vs.gs_status, vs.directologist, NULL::TEXT, NULL::TEXT, vs.salon, vs.city, vs.region,
    -- direction (= 'Авто' из vk_sites)
    vs.direction,
    -- "неверный_кодер_new", fid
    NULL::TEXT, NULL::TEXT,
    -- проджект, id_салона, менеджер
    NULLIF(TRIM(vs.project_manager), ''), vs.client_id, NULLIF(TRIM(vs.sales_manager), ''),
    -- источник, направление  -- KOMPLEKS_REFACTOR_REDO_2026-07-09
    'VK Ads'::TEXT, 'Комплекс'::TEXT,
    -- "номер кампании | название кампании"
    'нет campaign'::TEXT,
    -- "номер группы | название группы"
    NULL::TEXT,
    -- "План заявки", "План приезда"
    NULL::INTEGER, NULL::INTEGER,
    -- "аккаунт|сайт"
    vs.login_key || '|' || lvz.domain,
    -- priezd_arrival_date, prodazhi_arrival_date
    NULL::BIGINT, NULL::BIGINT,
    -- поставщик
    'ВК Реклама'::TEXT,
    -- _source_table
    'vk_zero'::TEXT
FROM leads_vk_zero_agg lvz
JOIN vk_sites vs ON vs.domain = lvz.domain

UNION ALL

-- VK_PERFORM_LEADS_2026-07-10: Часть 3 — потерянные vkads-заявки Перформа.
-- направление='Перформ', источник='VK Ads', поставщик='ВК Реклама', тип_заявки='Заявки',
-- total_cost=NULL, _source_table='vk_perform', id_салона='avto_0415', салон=реальный из лида.
-- domain=представительский Авто-домен Перформа (носитель для FACT_AUTO_WHERE).
SELECT
    -- key3: vk_perform|salon|date (синтетический — нет campaign/ad_plan)
    'vk_perform|' || pvk.salon || '|' || pvk.created_date::text,
    -- "Date"
    pvk.created_date,
    -- "День недели"
    CASE EXTRACT(ISODOW FROM pvk.created_date)
        WHEN 1 THEN '1_Понедельник' WHEN 2 THEN '2_Вторник' WHEN 3 THEN '3_Среда'
        WHEN 4 THEN '4_Четверг'    WHEN 5 THEN '5_Пятница'  WHEN 6 THEN '6_Суббота'
        WHEN 7 THEN '7_Воскресенье'
    END,
    -- week_start
    DATE_TRUNC('week', pvk.created_date)::date,
    -- "CampaignId", "CampaignName"
    NULL::BIGINT, 'victory'::TEXT,
    -- "AdGroupId", "AdGroupName"
    NULL::BIGINT, NULL::TEXT,
    -- "AdNetworkType", "Device"
    NULL::TEXT, NULL::TEXT,
    -- "Impressions", "Clicks"
    NULL::BIGINT, NULL::BIGINT,
    -- total_cost: NULL (расхода нет)
    NULL::NUMERIC,
    -- domain: представительский Авто-домен Перформа (FACT_AUTO_WHERE)
    pvs.domain,
    -- "RlAdjustmentId", "RlAdjustmentId_total"
    NULL::BIGINT, NULL::TEXT,
    -- campaign_code, tp, cpc_cpa, site_quiz, adgroup_code
    NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT,
    -- account_login, manager_login
    pvs.login_key, NULL::TEXT,
    -- ag_part1 .. ag_part7
    NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT,
    -- "марки авто", "Название crm", тип_заявки
    ''::TEXT, NULL::TEXT, 'Заявки'::TEXT,
    -- Воронка из leads_perform_vk_agg
    -- kol_vo_zayavok, korr, kval, priezd, prodazhi, nekorr, ne_otvechaet, filtr, nedozvon, priedet
    pvk.kol_vo_zayavok::BIGINT, pvk.korr::BIGINT,
    pvk.kval::BIGINT, pvk.priezd::BIGINT,
    pvk.prodazhi::BIGINT,
    pvk.nekorr::BIGINT, pvk.ne_otvechaet::BIGINT, pvk.filtr::BIGINT,
    pvk.nedozvon::BIGINT, pvk.priedet::BIGINT,
    -- dohod_do_kredita, dobro
    pvk.dohod_do_kredita::BIGINT, pvk.dobro::BIGINT,
    -- "статус", "специалист", "тип_сайта", "шаблон", "салон", "город", "регион"
    NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT, pvk.salon, NULL::TEXT, NULL::TEXT,
    -- direction (gsheet-направление Перформ-доменов = 'Авто')
    'Авто'::TEXT,
    -- "неверный_кодер_new", fid
    NULL::TEXT, NULL::TEXT,
    -- проджект, id_салона, менеджер
    NULL::TEXT, 'avto_0415'::TEXT, NULL::TEXT,
    -- источник, направление
    'VK Ads'::TEXT, 'Перформ'::TEXT,
    -- "номер кампании | название кампании"
    'victory'::TEXT,
    -- "номер группы | название группы"
    NULL::TEXT,
    -- "План заявки", "План приезда"
    NULL::INTEGER, NULL::INTEGER,
    -- "аккаунт|сайт"
    pvs.login_key || '|' || pvs.domain,
    -- priezd_arrival_date, prodazhi_arrival_date
    NULL::BIGINT, NULL::BIGINT,
    -- поставщик
    'ВК Реклама'::TEXT,
    -- _source_table
    'vk_perform'::TEXT
FROM leads_perform_vk_agg pvk
CROSS JOIN perform_vk_site pvs;
"""


def _move_tp8_to_crop(conn) -> int:
    """
    Переносит tp8/tp9/tp10-строки из big_analytics_direct в big_analytics_crop_targeting.
    tp8 — МК/ТК Telegram; tp9 — МК/ТК Max; tp10 — МК/ТК Telegram+Max.
    Все три логически относятся к посевам, не к Директу.
    _source_table сохраняет различимость по типу ('tp8'/'tp9'/'tp10'),
    что критично: load_crop_to_big_analytics.py удаляет только _source_table='crop_targeting'.
    TP9_TP10_POSEV_MOVE_2026-06-22
    """
    with conn.cursor() as cur:
        # Перенос всех трёх типов одним INSERT
        cur.execute(f"""
            INSERT INTO {T_CROP}
            SELECT * FROM {T_DIRECT} WHERE tp IN ('tp8', 'tp9', 'tp10')
        """)
        moved = cur.rowcount
        # tp8 → Посевы_Telegram / tp8  -- KOMPLEKS_REFACTOR_REDO_2026-07-09
        cur.execute(f"""
            UPDATE {T_CROP}
            SET направление = 'Комплекс', _source_table = 'tp8',
                источник = 'Посевы_Telegram'
            WHERE tp = 'tp8'
        """)
        # tp9 → Посевы_Max / tp9
        cur.execute(f"""
            UPDATE {T_CROP}
            SET направление = 'Комплекс', _source_table = 'tp9',
                источник = 'Посевы_Max'
            WHERE tp = 'tp9'
        """)
        # tp10 → Посевы_Telegram+Max / tp10
        cur.execute(f"""
            UPDATE {T_CROP}
            SET направление = 'Комплекс', _source_table = 'tp10',
                источник = 'Посевы_Telegram+Max'
            WHERE tp = 'tp10'
        """)
        cur.execute(f"DELETE FROM {T_DIRECT} WHERE tp IN ('tp8', 'tp9', 'tp10')")
    conn.commit()
    return moved


# ══════════════════════════════════════════════════════════════════════════════
# big_analytics_reviews (из yandex_direct_reports_reviews + direct_account_reviews)
# ══════════════════════════════════════════════════════════════════════════════

def _build_reviews_sql() -> str:
    """
    Создаёт big_analytics_reviews из yandex_direct_reports_reviews.
    Если таблица-источник не существует — создаётся пустая (LIKE big_analytics_direct).
    Данные обновляются вручную через direct_account_reviews/fetch_direct_stats.py.
    """
    return f"""
CREATE UNLOGGED TABLE IF NOT EXISTS {T_REVIEWS} (LIKE {T_DIRECT} INCLUDING ALL);
ALTER TABLE {T_REVIEWS} SET UNLOGGED;
TRUNCATE {T_REVIEWS};

DO $$
BEGIN
    IF EXISTS (
        SELECT FROM pg_tables
        WHERE schemaname = 'public'
          AND tablename  = 'yandex_direct_reports_reviews'
    ) THEN
        INSERT INTO {T_REVIEWS} (
            key3, "Date", "День недели", week_start,
            "CampaignId", "CampaignName", "AdGroupId", "AdGroupName",
            "AdNetworkType", "Device", "Impressions", "Clicks", total_cost, domain,
            "RlAdjustmentId", "RlAdjustmentId_total",
            campaign_code, tp, cpc_cpa, site_quiz, adgroup_code,
            account_login, manager_login,
            ag_part1, ag_part2, ag_part3, ag_part4, ag_part5, ag_part6, ag_part7,
            "марки авто", "Название crm", тип_заявки,
            kol_vo_zayavok, korr, kval, priezd, prodazhi, nekorr, ne_otvechaet, filtr, nedozvon, priedet,
            dohod_do_kredita, dobro,
            "статус", "специалист", "тип_сайта", "шаблон", "салон", "город", "регион",
            direction,
            "неверный_кодер_new", fid,
            проджект, id_салона, менеджер,
            источник, направление,
            "номер кампании | название кампании",
            "номер группы | название группы",
            "План заявки", "План приезда",
            "аккаунт|сайт",
            priezd_arrival_date, prodazhi_arrival_date,
            поставщик, _source_table
        )
        SELECT
            NULL::TEXT,
            r."Date",
            NULL::TEXT,
            NULL::DATE,
            r."CampaignId",
            r."CampaignName",
            r."AdGroupId",
            r."AdGroupName",
            -- REVIEWS_CAPITALIZE_2026-07-10: 'SEARCH' (API Direct) → 'Отзывы' (бизнес-смысл, _source_table='direct_account_reviews')
            CASE r."AdNetworkType" WHEN 'SEARCH' THEN 'Отзывы' ELSE r."AdNetworkType" END,
            r."Device",
            r."Impressions",
            r."Clicks",
            r."Cost",
            d.сайт,
            r."RlAdjustmentId",
            'отзывы'::TEXT,
            NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT,
            r.login,
            'отзывы'::TEXT,
            NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT,
            NULL::TEXT, NULL::TEXT, NULL::TEXT,
            NULL::TEXT,
            'отзывы'::TEXT,
            'Отзывы'::TEXT,
            NULL::BIGINT,
            NULL::BIGINT, NULL::BIGINT, NULL::BIGINT, NULL::BIGINT,
            NULL::BIGINT, NULL::BIGINT, NULL::BIGINT, NULL::BIGINT,
            NULL::BIGINT,
            NULL::BIGINT, NULL::BIGINT,
            NULL::TEXT,
            'Караваев Михаил'::TEXT,
            'отзывы'::TEXT,
            'отзывы'::TEXT,
            d.салон,
            d.город,
            NULL::TEXT,
            'Авто'::TEXT,
            NULL::TEXT,
            NULL::TEXT,
            NULLIF(TRIM(gs.project_manager), ''),
            gs.client_id,
            COALESCE(NULLIF(TRIM(gs.sales_manager),''), NULLIF(TRIM(auto.менеджер),'')),
            'Контекст'::TEXT,
            'Отзывы'::TEXT,            -- KOMPLEKS_REFACTOR_REDO_2026-07-09
            r."CampaignId"::TEXT || '|' || COALESCE(r."CampaignName", ''),
            r."AdGroupId"::TEXT  || '|' || COALESCE(r."AdGroupName",  ''),
            NULL::INTEGER,
            NULL::INTEGER,
            r.login || '|' || COALESCE(d.сайт, ''),
            NULL::INTEGER,
            NULL::INTEGER,
            'Яндекс'::TEXT,
            'direct_account_reviews'::TEXT
        FROM public.yandex_direct_reports_reviews r
        LEFT JOIN public.yandex_direct_account_reviews d ON d.аккаунт = r.login
        LEFT JOIN (
            SELECT DISTINCT salon, client_id, project_manager, sales_manager, crm
            FROM {T_GSHEET_SITES} WHERE salon IS NOT NULL
        ) gs ON LOWER(TRIM(d.салон)) = LOWER(TRIM(gs.salon))
        LEFT JOIN {T_GSHEET_AUTOSALONY} auto ON gs.client_id = auto.id_салона AND gs.client_id IS NOT NULL;
    END IF;
END
$$;
"""


# ── Подсчёт строк ─────────────────────────────────────────────────────────────

def _count(conn, table: str) -> int:
    try:
        with conn.cursor() as cur:
            cur.execute(f'SELECT COUNT(*) FROM {table}')
            return cur.fetchone()[0]
    except Exception:
        return -1


# ── Точка входа шага ─────────────────────────────────────────────────────────

def _ensure_crop_attribution(conn) -> None:
    """Создать leads_crop_attribution если не существует (заполняется внешним скриптом)."""
    with conn.cursor() as cur:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {T_LEADS_CROP_ATTR} (
                lead_id      BIGINT  PRIMARY KEY,
                is_crop      BOOLEAN NOT NULL DEFAULT FALSE,
                crop_source  TEXT,
                attributed_by TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_lca_is_crop ON {T_LEADS_CROP_ATTR} (is_crop);
        """)
    conn.commit()


def run(conn, run_id: str) -> dict:
    from config.brand_map import build_brand_case_sql
    logger.info('Шаг 3: сборка таблиц по источникам')

    _ensure_crop_attribution(conn)

    st = load_status_sql(conn)
    sc = st['status_cases']
    ps = st['priezd_statuses_sql']
    brand_sql = build_brand_case_sql()

    # ── STEP3_TEMP_GUARD_2026-07-11 (rev2): диск-защита step3 ─────────────────
    # ⚠️ ЧЕСТНО про temp_file_limit: это SUSET-параметр. Роль bi_analytic НЕ superuser
    # (проверено на Victory: SET temp_file_limit → "permission denied to set parameter";
    # глобально temp_file_limit = -1). Значит per-session temp_file_limit под этой ролью
    # НЕ применяется и НЕ может быть «чистым падением» (как в spend/build_spend_staging.py,
    # где тот же SET глушится SAVEPOINT'ом = no-op). Оставляем ПОПЫТКУ SET (на случай, если
    # роли когда-нибудь выдадут GRANT SET ON PARAMETER temp_file_limit), но:
    #   - оборачиваем в SAVEPOINT+try/except, чтобы permission denied НЕ уронил step3;
    #   - ЧЕСТНО логируем, применился ли лимит фактически (SHOW после SET).
    # РЕАЛЬНАЯ защита от ENOSPC/забивания диска в 0 — disk-watchdog ниже (pg_cancel_backend
    # СВОЕГО backend'а до нуля; PG чистит свой pgsql_tmp при abort) + бюджет памяти
    # (work_mem serial для direct-CTAS против OOM при Swap=0 → orphaned temp).
    #
    # Формула лимита (если бы работал): free − резерв под саму big_analytics_direct
    # (~6 GB, растёт ПАРАЛЛЕЛЬНО temp) − 1 GB буфер (чтобы temp не съел диск в 0).
    # При входе ~19 GB free → лимит ~12 GB — ОСОЗНАННО МЕНЬШЕ штатного пика temp (~15 GB):
    # упёрлись бы в лимит ДО ENOSPC = чистый abort. Не ALTER SYSTEM — только сессия.
    import shutil as _s3_shutil
    _s3_free_gb = _s3_shutil.disk_usage('/').free / (1024 ** 3)
    _S3_TABLE_RESERVE_GB = 6   # big_analytics_direct пишется параллельно temp
    _S3_TEMP_BUFFER_GB = 1     # запас, чтобы temp не упёрся в абсолютный 0
    _s3_temp_limit_gb = max(
        4, int(_s3_free_gb) - _S3_TABLE_RESERVE_GB - _S3_TEMP_BUFFER_GB
    )
    with conn.cursor() as _s3_cur:
        _s3_cur.execute("SAVEPOINT _s3_tfl")
        try:
            _s3_cur.execute(f"SET temp_file_limit = '{_s3_temp_limit_gb}GB'")
            _s3_cur.execute("RELEASE SAVEPOINT _s3_tfl")
            _s3_cur.execute("SHOW temp_file_limit")
            _s3_eff = _s3_cur.fetchone()[0]
            _s3_tfl_ok = _s3_eff not in ('-1', '0')
        except Exception as _s3_tfl_e:  # noqa: F841
            _s3_cur.execute("ROLLBACK TO SAVEPOINT _s3_tfl")
            _s3_eff, _s3_tfl_ok = 'permission denied', False
    conn.commit()
    if _s3_tfl_ok:
        logger.info(
            '  STEP3_TEMP_GUARD: free=%.1f GB → temp_file_limit=%d GB ПРИМЕНЁН (%s)',
            _s3_free_gb, _s3_temp_limit_gb, _s3_eff
        )
    else:
        logger.warning(
            '  STEP3_TEMP_GUARD: temp_file_limit НЕ применён (%s) — роль без superuser. '
            'Чистое падение обеспечивают disk-watchdog + work_mem serial, НЕ temp_file_limit',
            _s3_eff
        )

    # ── STEP3_RAW_YANDEX_PREFLIGHT_2026-07-20 ────────────────────────────────
    # Лёгкий fail-fast ДО MATONCE и CTAS: raw_yandex обязана быть непустой.
    # На retry после PG crash recovery сервер TRUNCATE-ит все UNLOGGED-таблицы
    # (raw_yandex, raw_leads, raw_calls, big_analytics_direct). MATONCE за 0.0 сек
    # и INSERT big_analytics_direct = 0 строк → STEP3_ZERO_ROW_GUARD тратит время.
    # Этот guard выходит раньше — до тяжёлых DDL/DML.
    # Второй рубеж (STEP3_ZERO_ROW_GUARD после INSERT) оставлен как second line of defense.
    with conn.cursor() as _ry_cur:
        _ry_cur.execute(f'SELECT EXISTS(SELECT 1 FROM {T_RAW_YANDEX} LIMIT 1)')
        _ry_populated = _ry_cur.fetchone()[0]
    if not _ry_populated:
        raise RuntimeError(
            'STEP3_RAW_YANDEX_PREFLIGHT: raw_yandex пуста до начала step3 — '
            'вероятно PG crash recovery обнулил UNLOGGED-таблицы между step1 и step3. '
            'Требуется полный перезапуск pipeline с шага 0 для пересборки raw_yandex.'
        )
    logger.info('  STEP3_RAW_YANDEX_PREFLIGHT: raw_yandex непустая — OK')

    # ── MATONCE_ACCOUNT_MANAGER_MAP_2026-06-18 ────────────────────────────────
    # Материализуем account_manager_map ОДИН РАЗ перед всеми 5 CTAS.
    # Было: каждый _build_*_sql() вызывал _build_common_ctes() → CTE агрегировал
    # raw_yandex (19M строк) inline 5 раз = 5 полных GROUP BY-сканов.
    # Стало: один CREATE TEMP TABLE + индекс (~небольшая таблица уникальных аккаунтов),
    # CTE в каждом SQL читает из TEMP — тривиальный seq scan. Результат идентичен:
    # тот же MAX(manager_login) GROUP BY account_login.
    # TEMP TABLE дропается автоматически при закрытии сессии (транзиентная).
    t0_mat = time.perf_counter()
    with conn.cursor() as cur:
        cur.execute(f"SET work_mem = '{WORK_MEM}'")
        cur.execute("DROP TABLE IF EXISTS _account_manager_map")
        cur.execute(f"""
            CREATE TEMP TABLE _account_manager_map AS
            SELECT account_login, MAX(manager_login) AS manager_login
            FROM {T_RAW_YANDEX}
            WHERE manager_login IS NOT NULL AND manager_login != ''
            GROUP BY account_login
        """)
        cur.execute("CREATE INDEX ON _account_manager_map(account_login)")
    conn.commit()
    logger.info('  MATONCE: _account_manager_map создан за %.1f сек', time.perf_counter() - t0_mat)

    # ── MATONCE_DOMAIN_SOURCE_TYPE_2026-06-18 ────────────────────────────────
    # Материализуем domain_source_type ОДИН РАЗ перед всеми 5 CTAS.
    # Было: 5 × (UNION raw_leads + raw_calls) с ARRAY_AGG GROUP BY внутри CTE.
    # Стало: один скан raw_leads + raw_calls, TEMP TABLE. Результат идентичен:
    # тот же приоритетный ARRAY_AGG ORDER BY crm_priority.
    # TEMP TABLE дропается автоматически при закрытии сессии (транзиентная).
    t0_dst = time.perf_counter()
    with conn.cursor() as cur:
        cur.execute(f"SET work_mem = '{WORK_MEM}'")
        cur.execute("DROP TABLE IF EXISTS _domain_source_type")
        cur.execute(f"""
            CREATE TEMP TABLE _domain_source_type AS
            SELECT domain_name,
                (ARRAY_AGG(leads_source_type ORDER BY crm_priority))[1] AS leads_source_type
            FROM (
                SELECT
                    LOWER(TRIM(domain)) AS domain_name,
                    CASE source_type
                        WHEN 'marcar_crm_excel' THEN 'Маркар'
                        WHEN 'mega_crm_excel'   THEN 'Мега'
                        WHEN 'crmf_excel'       THEN 'Фаиг'
                        WHEN 'plex_excel'       THEN 'Плекс'
                        WHEN 'redauto_excel'    THEN 'Ред Авто'
                        WHEN 'genzes_excel'     THEN 'Генезис'
                        WHEN 'mauto_excel'      THEN 'МаАвто'
                        ELSE source_type
                    END AS leads_source_type,
                    CASE source_type
                        WHEN 'marcar_crm_excel' THEN 1
                        WHEN 'mega_crm_excel'   THEN 2
                        WHEN 'crmf_excel'       THEN 3
                        WHEN 'plex_excel'       THEN 4
                        WHEN 'redauto_excel'    THEN 5
                        WHEN 'genzes_excel'     THEN 6
                        WHEN 'mauto_excel'      THEN 7
                        ELSE 9
                    END AS crm_priority
                FROM {T_RAW_LEADS}
                WHERE domain IS NOT NULL AND domain != ''
                UNION ALL
                SELECT
                    LOWER(TRIM(domain)) AS domain_name,
                    CASE source_type
                        WHEN 'marcar_crm_excel' THEN 'Маркар'
                        WHEN 'mega_crm_excel'   THEN 'Мега'
                        WHEN 'crmf_excel'       THEN 'Фаиг'
                        WHEN 'plex_excel'       THEN 'Плекс'
                        WHEN 'redauto_excel'    THEN 'Ред Авто'
                        WHEN 'genzes_excel'     THEN 'Генезис'
                        WHEN 'mauto_excel'      THEN 'МаАвто'
                        ELSE source_type
                    END AS leads_source_type,
                    CASE source_type
                        WHEN 'marcar_crm_excel' THEN 1
                        WHEN 'mega_crm_excel'   THEN 2
                        WHEN 'crmf_excel'       THEN 3
                        WHEN 'plex_excel'       THEN 4
                        WHEN 'redauto_excel'    THEN 5
                        WHEN 'genzes_excel'     THEN 6
                        WHEN 'mauto_excel'      THEN 7
                        ELSE 9
                    END AS crm_priority
                FROM {T_RAW_CALLS}
                WHERE domain IS NOT NULL AND domain != ''
            ) combined
            GROUP BY domain_name
        """)
        cur.execute("CREATE INDEX ON _domain_source_type(domain_name)")
    conn.commit()
    logger.info('  MATONCE: _domain_source_type создан за %.1f сек', time.perf_counter() - t0_dst)

    tasks = [
        ('big_analytics_direct',           _build_direct_sql(brand_sql, sc, ps), T_DIRECT),
        ('big_analytics_seo',              _build_seo_sql(sc, ps),               T_SEO),
        ('big_analytics_pixel',            _build_pixel_sql(sc, ps),             T_PIXEL),
        ('big_analytics_crop_targeting',   _build_crop_sql(),                    T_CROP),
        ('big_analytics_reviews',          _build_reviews_sql(),                 T_REVIEWS),
    ]

    total_rows = 0
    details_parts = []

    # ── STEP3_DISK_WATCHDOG_2026-07-11: реальная защита от забивания диска в 0 ──
    # temp_file_limit под bi_analytic не работает (см. выше). Поэтому НАСТОЯЩИЙ
    # «чистый abort до ENOSPC» делает фоновый поток: раз в 2 сек проверяет free диска
    # и при free < 2 GB отменяет СВОЙ backend (pg_cancel_backend(pid) — разрешено:
    # своя роль, своя сессия). Отмена → PG аварийно завершает запрос → чистит СВОЙ
    # pgsql_tmp → диск восстанавливается, backend жив. Так direct-CTAS падает ЧИСТО,
    # НЕ доводя диск до нуля и НЕ оставляя осиротевший temp. Watcher — отдельное
    # соединение (основной conn занят CTAS); daemon; при любой своей ошибке молча
    # выходит, НИКОГДА не вредит основному запросу. Работает вокруг 5 CTAS (там пик).
    _S3_DISK_FLOOR_GB = 2.0
    with conn.cursor() as _pcur:
        _pcur.execute("SELECT pg_backend_pid()")
        _s3_pid = _pcur.fetchone()[0]
    conn.commit()
    _s3_stop = threading.Event()

    def _s3_disk_watchdog():
        import psycopg2 as _wpg
        from config.settings import DB_DST as _WDB
        _wc = None
        try:
            _wc = _wpg.connect(**_WDB)
            _wc.autocommit = True
            while not _s3_stop.wait(2.0):
                _free = _s3_shutil.disk_usage('/').free / (1024 ** 3)
                if _free < _S3_DISK_FLOOR_GB:
                    with _wc.cursor() as _wcur:
                        _wcur.execute("SELECT pg_cancel_backend(%s)", (_s3_pid,))
                    logger.error(
                        '  STEP3_DISK_WATCHDOG: free=%.2f GB < %.1f GB → '
                        'pg_cancel_backend(%s) — чистый abort ДО ENOSPC',
                        _free, _S3_DISK_FLOOR_GB, _s3_pid
                    )
                    break
        except Exception as _we:  # noqa: F841
            logger.warning('  STEP3_DISK_WATCHDOG: watcher-ошибка (не критично): %s', _we)
        finally:
            if _wc is not None:
                try:
                    _wc.close()
                except Exception:
                    pass

    _s3_wt = threading.Thread(
        target=_s3_disk_watchdog, name='step3-disk-watchdog', daemon=True
    )
    _s3_wt.start()

    try:
        for name, sql, table_const in tasks:
            t0 = time.perf_counter()
            # STEP3_DIRECT_WORKMEM_2026-07-11: только direct-CTAS получает 4 GB work_mem
            # СТРОГО в паре с serial (max_parallel_workers_per_gather=0) — см. _WM_DIRECT.
            _is_direct = (name == 'big_analytics_direct')
            _wm = _WM_DIRECT if _is_direct else WORK_MEM
            _pgather = 0 if _is_direct else 2  # 2 = дефолт Victory (восстанавливаем для прочих)
            try:
                # Split DDL (CREATE+ALTER+TRUNCATE) from DML (INSERT) into separate
                # transactions: TRUNCATE-freed pages only become reusable after commit,
                # so INSERT in the same transaction would allocate new pages → 2× disk peak.
                dml_marker = f'\nINSERT INTO {table_const}'
                dml_start = sql.find(dml_marker)
                if dml_start != -1:
                    ddl_sql = sql[:dml_start]
                    dml_sql = sql[dml_start + 1:]  # skip leading \n
                else:
                    ddl_sql = sql
                    dml_sql = None

                with conn.cursor() as cur:
                    cur.execute(f"SET work_mem = '{_wm}'")
                    cur.execute(f"SET max_parallel_workers_per_gather = {_pgather}")
                    cur.execute(ddl_sql)
                conn.commit()  # TRUNCATE commits → freed pages available for INSERT

                if dml_sql:
                    with conn.cursor() as cur:
                        cur.execute(f"SET work_mem = '{_wm}'")
                        cur.execute(f"SET max_parallel_workers_per_gather = {_pgather}")
                        cur.execute(dml_sql)
                    conn.commit()

                rows = _count(conn, table_const)

                # STEP3_ZERO_ROW_GUARD_2026-07-16: big_analytics_direct никогда не должен
                # быть пустым после INSERT (нормальный объём ~4.3M строк). Ноль строк =
                # INSERT не выполнился (ENOSPC / OperationalError на предыдущей попытке →
                # retry отработал без INSERT). RuntimeError НЕ является _CONN_ERRORS →
                # pipeline.py не уйдёт на повторную попытку, пометит шаг failed и пропустит
                # build_star / downstream шаги вместо того чтобы строить star из 71K строк.
                if name == 'big_analytics_direct' and rows == 0:
                    raw_rows = _count(conn, T_RAW_YANDEX)
                    raise RuntimeError(
                        f'STEP3_ZERO_ROW_GUARD: big_analytics_direct пуст (0 строк) после '
                        f'INSERT — raw_yandex содержит {raw_rows:,} строк. '
                        f'Вероятная причина: ENOSPC/OperationalError при INSERT, retry '
                        f'отработал без DML. Шаг помечен failed — build_star пропущен.'
                    )

                elapsed = time.perf_counter() - t0
                total_rows += max(rows, 0)
                details_parts.append(f'{name}={rows:,}')
                logger.info('  %s: %d строк за %.1f сек', name, rows, elapsed)
            except Exception as e:
                conn.rollback()
                logger.error('  ОШИБКА %s: %s', name, e)
                raise
    finally:
        _s3_stop.set()
        _s3_wt.join(timeout=5)

    details = ', '.join(details_parts)

    # ── LZ4_DIRECT_FULL_UNIFIED_2026-06-17: lz4-сжатие TEXT-колонок big_analytics_direct ──
    # T_DIRECT — UNLOGGED TRUNCATE+INSERT (18M+ строк ~19 GB несжатый).
    # lz4 навешивается ПОСЛЕ INSERT (данные уже записаны — экономия с ЭТОГО прогона
    # для новых INSERT при следующем; но главное — компрессия атрибута-колонок, которые
    # занимают до ~70% TOAST-пространства). step7 SET LOGGED не перезаписывает данные →
    # сжатие сохраняется. Downstream (step6/step8) только читают через SELECT — lz4 прозрачен.
    # Ожидаемый эффект: direct 19 GB → ~6 GB (−60-70% TEXT-хранилища через TOAST).
    _TEXT_COLS_DIRECT = [
        'key3', '"День недели"', '"CampaignName"', '"AdGroupName"',
        '"AdNetworkType"', '"Device"', 'domain',
        '"RlAdjustmentId_total"', 'campaign_code', 'tp', 'cpc_cpa', 'site_quiz',
        'adgroup_code', 'account_login', 'manager_login',
        'ag_part1', 'ag_part2', 'ag_part3', 'ag_part4',
        'ag_part5', 'ag_part6', 'ag_part7',
        '"марки авто"', '"Название crm"', 'тип_заявки',
        '"статус"', '"специалист"', '"тип_сайта"', '"шаблон"',
        '"салон"', '"город"', '"регион"', 'direction',
        '"неверный_кодер_new"', 'fid', 'проджект', 'id_салона', 'менеджер',
        'источник', 'направление',
        '"номер кампании | название кампании"',
        '"номер группы | название группы"',
        '"аккаунт|сайт"', 'поставщик', '_source_table',
        'campaign_status', 'payment_model',
    ]
    try:
        with conn.cursor() as cur:
            for col in _TEXT_COLS_DIRECT:
                cur.execute(
                    f'ALTER TABLE {T_DIRECT} '
                    f'ALTER COLUMN {col} SET COMPRESSION lz4'
                )
        conn.commit()
        logger.info('  LZ4: сжатие lz4 навешено на %d TEXT-колонок %s',
                    len(_TEXT_COLS_DIRECT), T_DIRECT)
    except Exception as _lz4_err:
        logger.warning('  LZ4: не удалось навесить lz4 на %s: %s (продолжаем)', T_DIRECT, _lz4_err)
        try:
            conn.rollback()
        except Exception:
            pass

    t0 = time.perf_counter()
    moved = _move_tp8_to_crop(conn)
    logger.info('  tp8/tp9/tp10 → big_analytics_crop_targeting: %d строк за %.1f сек', moved, time.perf_counter() - t0)

    t0 = time.perf_counter()
    with conn.cursor() as cur:
        cur.execute(f"SET work_mem = '{WORK_MEM}'")
        cur.execute(_add_crop_calls_sql(st['calls_agg_cases']))
        n_crop_calls = cur.rowcount
    conn.commit()
    logger.info('  звонки → big_analytics_crop_targeting: %d строк за %.1f сек', n_crop_calls, time.perf_counter() - t0)

    t0 = time.perf_counter()
    with conn.cursor() as cur:
        cur.execute(f"SET work_mem = '{WORK_MEM}'")
        cur.execute(_add_crop_seo_sql(sc, ps))
        n_crop_seo = cur.rowcount
    conn.commit()
    logger.info('  SEO → big_analytics_crop_targeting: %d строк за %.1f сек', n_crop_seo, time.perf_counter() - t0)

    t0 = time.perf_counter()
    with conn.cursor() as cur:
        cur.execute(f"SET work_mem = '{WORK_MEM}'")
        cur.execute(_add_telegram_to_crop_sql(sc, ps))
        n_tg = cur.rowcount
    conn.commit()
    logger.info('  Telegram посевы → big_analytics_crop_targeting: %d строк за %.1f сек', n_tg, time.perf_counter() - t0)

    t0 = time.perf_counter()
    with conn.cursor() as cur:
        cur.execute(f"SET work_mem = '{WORK_MEM}'")
        cur.execute(_add_social_posev_to_crop_sql(sc, ps))
        n_social = cur.rowcount
    conn.commit()
    logger.info('  Social посевы (Max/VK/storis) → big_analytics_crop_targeting: %d строк за %.1f сек', n_social, time.perf_counter() - t0)

    # VK_ADS_INTEGRATION_2026-07-06: расход ВК Реклама (direction='Авто', 2 аккаунта, воронка=0)
    t0 = time.perf_counter()
    with conn.cursor() as cur:
        cur.execute(f"SET work_mem = '{WORK_MEM}'")
        cur.execute(_add_vk_ads_to_crop_sql(sc, ps))
        n_vk_ads = cur.rowcount
    conn.commit()
    logger.info('  ВК Реклама (VK ads) → big_analytics_crop_targeting: %d строк за %.1f сек', n_vk_ads, time.perf_counter() - t0)

    logger.info('Шаг 3 завершён: %s', details)
    return {'rows': total_rows, 'details': details}


# ── EXPLAIN_BASELINE_2026-06-17: SELECT-эквивалент для EXPLAIN ANALYZE ──────
# Используется explain_capture при EXPLAIN_CAPTURE=1 (post-run, данные уже записаны).
# Показывает план тяжёлого SELECT из big_analytics_direct (18M+ строк).
# Data path не изменяется — это SELECT-only по уже записанной таблице.

def get_explain_sql(conn) -> str:  # noqa: ARG001
    """SELECT-эквивалент для EXPLAIN ANALYZE по big_analytics_direct (step3, самая тяжёлая таблица)."""
    return (
        "SELECT COUNT(*), _source_table "
        "FROM public.big_analytics_direct "
        "GROUP BY _source_table"
    )
