"""Build analytics_report_feed — denormalised report table for Power BI "Фиды" page.

ANALYTICS_REPORT_FEED_2026-06-29

Full rebuild strategy: DROP TABLE IF EXISTS + CREATE TABLE AS SELECT.
Source: fact_direct_feed_funnel LEFT JOIN local_gsheet_sites + Dim_Campaign + Dim_AdGroup.

Note: adgroup_id is 100 % NULL in fact_direct_feed_funnel (by-design: grain is
date|domain|feed_key, not adgroup). Dim_AdGroup join is included for future use
when/if adgroup granularity is added.
"""

from __future__ import annotations

import logging
import time

from config.db import get_conn, put_conn

logger = logging.getLogger("pipeline.direct_feed_funnel.build_report_feed")

_CREATE_SQL = """
CREATE TABLE public.analytics_report_feed AS  -- ANALYTICS_REPORT_FEED_2026-06-29
-- FEED_NEW_FIELDS_2026-06-29: tp, campaign_code, AdNetworkType, тип_заявки,
--   Название crm, статус, источник, домен (кириллица).
-- leads_meta агрегирует direct_feed_leads_keyed по dk2 (date|domain|feed_key):
--   название_crm = MAX(source_type) — аналогично ARP этап B (MAX(source_type) из лидов);
--   статус_заявки = MAX(status) — CRM-статус заявки (Корзина / Дубль / Корректный и т.д.)
-- FEED_SPECIALIST_FALLBACK_2026-07-24:
--   приоритет метаданных сайта:
--   1) точный матч по login_key;
--   2) fallback по domain, если login_key пустой/не сматчился.
WITH leads_meta AS (
    SELECT
        dk2,
        MAX(source_type) AS название_crm,
        MAX(status)      AS статус_заявки
    FROM public.direct_feed_leads_keyed
    GROUP BY dk2
),
site_by_login AS (
    SELECT DISTINCT ON (login_key)
        login_key,
        site_type,
        region,
        salon,
        city,
        template,
        directologist,
        agency_account,
        direction,
        niche,
        channel
    FROM public.local_gsheet_sites
    WHERE login_key IS NOT NULL
      AND btrim(login_key) <> ''
    ORDER BY
        login_key,
        CASE status
            WHEN 'Контекст активно' THEN 0
            WHEN 'На модерации' THEN 1
            WHEN 'В работе' THEN 2
            WHEN 'Стоп' THEN 3
            WHEN 'Удален' THEN 4
            ELSE 5
        END,
        domain
),
site_by_domain AS (
    SELECT DISTINCT ON (domain)
        domain,
        site_type,
        region,
        salon,
        city,
        template,
        directologist,
        agency_account,
        direction,
        niche,
        channel
    FROM public.local_gsheet_sites
    WHERE domain IS NOT NULL
      AND btrim(domain) <> ''
    ORDER BY
        domain,
        CASE status
            WHEN 'Контекст активно' THEN 0
            WHEN 'На модерации' THEN 1
            WHEN 'В работе' THEN 2
            WHEN 'Стоп' THEN 3
            WHEN 'Удален' THEN 4
            ELSE 5
        END,
        login_key
)
SELECT
    f.date,
    f.feed_id,
    f.feed_name,
    f.feed_url,
    f.feed_key,
    f.feed_url_key,
    f.campaign_id,
    f.campaign_name,
    f.adgroup_id,
    f.adgroup_name,
    f.domain,
    f.login_key,
    f.dk2,
    f.is_tp67,
    f.total_cost,
    f.clicks,
    f.impressions,
    f.kol_vo_zayavok,
    f.korr,
    f.nekorr,
    f.kval,
    f.priezd,
    f.prodazhi,
    f.dobro,
    f.dohod_do_kredita,
    f.filtr,
    f.attributed_leads,
    f.ne_otvechaet,
    f.nedozvon,
    f.priedet,
    f.goal_all_forms,
    f.goal_crm_order_created,
    f.goal_crm_order_paid,
    f.goal_crm_order_canceled,
    f.goal_crm_spam_order,

    -- из local_gsheet_sites: сначала по login_key, затем fallback по domain
    coalesce(sl.site_type,      sd.site_type)      AS "тип_сайта",
    coalesce(sl.region,         sd.region)         AS "регион",
    coalesce(sl.salon,          sd.salon)          AS "салон",
    coalesce(sl.city,           sd.city)           AS "город",
    coalesce(sl.template,       sd.template)       AS "шаблон",
    coalesce(sl.directologist,  sd.directologist)  AS "специалист",
    coalesce(sl.agency_account, sd.agency_account) AS "аккаунт|сайт",
    coalesce(sl.direction,      sd.direction)      AS "направление",
    coalesce(sl.niche,          sd.niche)          AS "ниша",
    coalesce(sl.channel,        sd.channel)        AS "канал",

    -- из Dim_Campaign по campaign_id
    dc.campaign_status                        AS "статус_кампании",
    dc.payment_model                          AS "тип_оплаты",
    dc."номер кампании | название кампании"   AS "номер кампании | название кампании",

    -- из Dim_AdGroup по adgroup_id (сейчас 100 % NULL — by-design, см. модуль-docstring)
    da."номер группы | название группы"       AS "номер группы | название группы",

    -- FEED_NEW_FIELDS_2026-06-29 -----------------------------------------------
    -- tp: тип размещения (tp1, tp2, tp6, tp7, ...).
    --   Парсится из campaign_name по паттерну tpN_(cpc|cpa)_(site|quiz),
    --   аналогично parse_campaign_code() в step1_fetch_direct.py.
    --   Пример: 'tp6_cpc_site — Каталог Авто' → 'tp6'.
    (regexp_match(
        split_part(f.campaign_name, ' — ', 1),
        '(tp\\d+)_(?:cpc|cpa)_(?:site|quiz)'
    ))[1]                                     AS tp,

    -- campaign_code: полный код кампании вида tp6_cpc_site.
    --   Пример: 'tp6_cpc_site — Каталог Авто' → 'tp6_cpc_site'.
    (regexp_match(
        split_part(f.campaign_name, ' — ', 1),
        'tp\\d+_(?:cpc|cpa)_(?:site|quiz)'
    ))[1]                                     AS campaign_code,

    -- AdNetworkType: тип сети (SEARCH/NETWORK/MIXED).
    --   NULL — поле отсутствует в yandex_direct_feeds_report (источник фидовых данных).
    --   Фидовые кампании работают только в сети (РСЯ/Каталог), но тип явно не передаётся.
    NULL::text                                AS "AdNetworkType",

    -- тип_заявки: 'заявки' если есть атрибутированные лиды, иначе NULL.
    --   Аналогично ARP step2 (тип_заявки = 'заявки' при UPDATE из leads_agg).
    CASE WHEN f.kol_vo_zayavok > 0
         THEN 'заявки'::text
         ELSE NULL
    END                                       AS "тип_заявки",

    -- Название crm: MAX(source_type) из direct_feed_leads_keyed по dk2.
    --   Пример значений: 'marcar_crm_excel', 'crmf_excel'.
    lk.название_crm                           AS "Название crm",

    -- статус: CRM-статус заявки, MAX(status) по dk2 из direct_feed_leads_keyed.
    --   Пример значений: 'Корзина', 'Корректный', 'Некорректные данные', 'Дубль'.
    --   При нескольких разных статусах по dk2 берётся лексикографически наибольший.
    lk.статус_заявки                          AS "статус",

    -- источник: константа для всех строк фидов — всегда Яндекс.Директ.
    'Я.Директ'::text                          AS "источник",

    -- домен (кириллица): алиас domain в кириллическом именовании для PBI.
    f.domain                                  AS "домен"
    -- -------------------------------------------------------------------

FROM public.fact_direct_feed_funnel f
LEFT JOIN site_by_login sl
    ON f.login_key = sl.login_key
LEFT JOIN site_by_domain sd
    ON f.domain = sd.domain
LEFT JOIN public."Dim_Campaign" dc
    ON f.campaign_id = dc."CampaignId"
LEFT JOIN public."Dim_AdGroup" da
    ON f.adgroup_id = da."AdGroupId"
LEFT JOIN leads_meta lk  -- FEED_NEW_FIELDS_2026-06-29
    ON f.dk2 = lk.dk2
WHERE f.domain <> 'victory-crm.ru'  -- VICTORY_CRM_DOMAIN_EXCLUDE_2026-07-26: internal/test domain, not a real client
"""

_STATS_SQL = """
SELECT
    COUNT(*)::bigint                         AS total_rows,
    MIN(date)                                AS min_date,
    MAX(date)                                AS max_date,
    COUNT("специалист")::bigint              AS specialist_filled,
    COUNT("тип_сайта")::bigint               AS site_type_filled,
    SUM(f.total_cost)                        AS total_cost,
    SUM(f.kol_vo_zayavok)::bigint            AS kol_vo_zayavok
FROM public.analytics_report_feed f
"""


def build() -> dict:
    """Full rebuild of analytics_report_feed.  Returns a stats dict."""
    # PostgreSQL поддерживает транзакционный DDL — DROP + CREATE AS SELECT
    # выполняются в одной транзакции, что безопаснее (откат при ошибке).
    # Не используем autocommit — избегаем psycopg2.ProgrammingError:
    # "set_session cannot be used inside a transaction" (KNOWN_ISSUES #14).
    conn = get_conn()
    try:
        t0 = time.monotonic()

        with conn.cursor() as cur:
            # ARF_CASCADE_2026-06-30: arf_fact VIEW зависит от analytics_report_feed,
            # поэтому DROP нужен с CASCADE. После пересоздания таблицы VIEW воссоздаётся
            # ниже. Без CASCADE: DependentObjectsStillExist (psycopg2.errors).
            cur.execute("DROP TABLE IF EXISTS public.analytics_report_feed CASCADE")
            logger.info("DROP TABLE analytics_report_feed CASCADE: OK")

            cur.execute(_CREATE_SQL)
            logger.info("CREATE TABLE analytics_report_feed: OK")

        conn.commit()
        elapsed_build = time.monotonic() - t0

        # Индексы (после INSERT — быстрее чем до)
        with conn.cursor() as cur:
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_arf_date "
                "ON public.analytics_report_feed (date)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_arf_login_key "
                "ON public.analytics_report_feed (login_key)"
            )
        conn.commit()
        logger.info("Индексы idx_arf_date / idx_arf_login_key: OK")

        # ARF_CASCADE_2026-06-30: пересоздаём arf_fact VIEW (был дропнут CASCADE выше).
        # arf_fact = passthrough SELECT * FROM analytics_report_feed (по образцу arp_fact).
        # CREATE OR REPLACE безопасен: если VIEW уже есть — обновляет, нет — создаёт.
        with conn.cursor() as cur:
            cur.execute(
                "CREATE OR REPLACE VIEW public.arf_fact AS "
                "SELECT * FROM public.analytics_report_feed"
            )
        conn.commit()
        logger.info("VIEW arf_fact: OK (recreated after CASCADE)")

        # Статистика
        with conn.cursor() as cur:
            cur.execute(_STATS_SQL)
            row = cur.fetchone()
        conn.rollback()  # закрываем read-транзакцию

        total_rows, min_date, max_date, spec_filled, st_filled, total_cost, kol_vo = row
        pct_spec = 100 * spec_filled / total_rows if total_rows else 0
        pct_st = 100 * st_filled / total_rows if total_rows else 0

        logger.info(
            "analytics_report_feed: rows=%s date=%s..%s "
            "специалист=%s(%.0f%%) тип_сайта=%s(%.0f%%) "
            "total_cost=%.2f kol_vo_zayavok=%s elapsed=%.1fs",
            total_rows, min_date, max_date,
            spec_filled, pct_spec,
            st_filled, pct_st,
            total_cost or 0, kol_vo or 0,
            elapsed_build,
        )

        return {
            "rows": total_rows,
            "min_date": str(min_date),
            "max_date": str(max_date),
            "specialist_filled": spec_filled,
            "specialist_pct": round(pct_spec, 1),
            "site_type_filled": st_filled,
            "site_type_pct": round(pct_st, 1),
            "total_cost": float(total_cost or 0),
            "kol_vo_zayavok": int(kol_vo or 0),
            "elapsed": round(elapsed_build, 1),
        }

    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)
