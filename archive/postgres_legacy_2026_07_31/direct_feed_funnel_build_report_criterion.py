"""Build analytics_report_criterion — denormalised report table for Power BI «Критерий».

BUILD_REPORT_CRITERION_2026-06-29

Full rebuild strategy: DROP TABLE IF EXISTS + CREATE TABLE AS SELECT.
Source: public.local_leads_all (воронка) FULL OUTER JOIN public.fact_criterion_spend (расход)
LEFT JOIN public.local_gsheet_sites (мета-атрибуты сайта).

Grain: date × domain × tp.

Note: mirrors build_report_feed.py structure. Uses config.status_sql.build_leads_agg_sql(conn)
for funnel aggregation expressions — same as build_criterion_zayavki.py.
"""

from __future__ import annotations

import logging
import time

from config.db import get_conn, put_conn
from config.status_sql import build_leads_agg_sql  # BUILD_REPORT_CRITERION_2026-06-29

logger = logging.getLogger("pipeline.direct_feed_funnel.build_report_criterion")

# SQL-шаблон: {leads_agg_sql} заменяется на CASE-выражения из build_leads_agg_sql(conn)
_SQL_TEMPLATE = """\
CREATE TABLE public.analytics_report_criterion AS  -- BUILD_REPORT_CRITERION_2026-06-29
WITH auto_domains AS (
  SELECT DISTINCT lower(trim(domain)) AS d FROM public.local_gsheet_sites
  WHERE domain IS NOT NULL AND trim(domain)<>''
),
leads AS (
  SELECT l.created_date AS date,
         lower(trim(dm.name)) AS domain_norm, dm.name AS domain,
         (regexp_match(l.utm_campaign,'[|](tp[0-9]+)_'))[1] AS tp,
         l.status, l.reason, l.source_type, l.salon
  FROM public.local_leads_all l
  JOIN public.local_domains dm ON dm.id = l.domain_id
  WHERE l.created_date >= DATE '2026-01-01'
    AND COALESCE(l.deal_type,'') <> 'Звонок'
    AND COALESCE(l.is_copy_for_removal,false) = false
    AND lower(trim(dm.name)) IN (SELECT d FROM auto_domains)
),
leads_agg AS (
  SELECT date, domain_norm, min(domain) AS domain, tp,
         {leads_agg_sql}
  FROM leads GROUP BY date, domain_norm, tp
),
spend_agg AS (
  SELECT date, lower(trim(domain)) AS domain_norm, min(domain) AS domain, tp,
         sum(cost) AS cost, sum(clicks) AS clicks, sum(impressions) AS impressions,
         sum("Все формы") AS "Все формы",
         sum("CRM: Заказ создан") AS "CRM: Заказ создан",
         sum("CRM: Заказ оплачен") AS "CRM: Заказ оплачен",
         sum("CRM: Заказ отменен") AS "CRM: Заказ отменен",
         sum("CRM: Спам заказ") AS "CRM: Спам заказ"
  FROM public.fact_criterion_spend
  GROUP BY date, lower(trim(domain)), tp
),
sites AS (
  SELECT DISTINCT ON (lower(trim(domain))) lower(trim(domain)) AS domain_norm,
         template AS "шаблон", salon AS "салон", city AS "город", region AS "регион",
         site_type AS "тип_сайта", directologist AS "специалист"
  FROM public.local_gsheet_sites WHERE domain IS NOT NULL AND trim(domain)<>''
  ORDER BY lower(trim(domain)), row_hash
)
SELECT coalesce(s.date, l.date) AS date,
       coalesce(s.domain, l.domain) AS domain,
       coalesce(s.domain, l.domain) AS "домен",
       st."шаблон", st."салон", st."город", st."регион", st."тип_сайта", st."специалист",
       coalesce(s.tp, l.tp) AS tp,
       CASE WHEN coalesce(s.tp,l.tp) = 'tp1' THEN 'Сети'
            WHEN coalesce(s.tp,l.tp) IN ('tp2','tp3','tp4','tp5') THEN 'Поиск'
            WHEN coalesce(s.tp,l.tp) IN ('tp6','tp7','tp8','tp9','tp10') THEN coalesce(s.tp,l.tp)
            ELSE NULL END AS ad_network_type,
       coalesce(l.kol_vo_zayavok, 0) AS kol_vo_zayavok,
       coalesce(l.korr, 0) AS korr,
       coalesce(l.nekorr, 0) AS nekorr,
       coalesce(l.kval, 0) AS kval,
       coalesce(l.priezd, 0) AS priezd,
       coalesce(l.prodazhi, 0) AS prodazhi,
       coalesce(s.cost, 0) AS cost,
       coalesce(s.clicks, 0) AS clicks,
       coalesce(s.impressions, 0) AS impressions,
       coalesce(s."Все формы", 0) AS "Все формы",
       coalesce(s."CRM: Заказ создан", 0) AS "CRM: Заказ создан",
       coalesce(s."CRM: Заказ оплачен", 0) AS "CRM: Заказ оплачен",
       coalesce(s."CRM: Заказ отменен", 0) AS "CRM: Заказ отменен",
       coalesce(s."CRM: Спам заказ", 0) AS "CRM: Спам заказ",
       NULL::text AS "номер кампании | название кампании"
FROM leads_agg l
FULL OUTER JOIN spend_agg s
  ON s.date = l.date
 AND s.domain_norm = l.domain_norm
 AND s.tp IS NOT DISTINCT FROM l.tp
LEFT JOIN sites st ON st.domain_norm = coalesce(s.domain_norm, l.domain_norm)
"""

_STATS_SQL = """\
SELECT
    COUNT(*)::bigint                 AS total_rows,
    MIN(date)                        AS min_date,
    MAX(date)                        AS max_date,
    SUM(cost)                        AS total_cost,
    SUM(kol_vo_zayavok)::bigint      AS kol_vo_zayavok,
    SUM(korr)::bigint                AS korr,
    SUM(kval)::bigint                AS kval,
    SUM(priezd)::bigint              AS priezd,
    SUM(prodazhi)::bigint            AS prodazhi
FROM public.analytics_report_criterion
"""


def build(date_from: str | None = None) -> dict:
    """Full rebuild of analytics_report_criterion. Returns a stats dict.

    DROP TABLE IF EXISTS + CREATE TABLE AS SELECT in one transaction (no autocommit),
    consistent with build_report_feed.py and KNOWN_ISSUES #14 (no _interim_vacuum).
    """
    conn = get_conn()
    try:
        t0 = time.monotonic()

        # Получаем CASE-выражения воронки из local_crm_statuses (авторитетный источник)
        # Результат — строка с kol_vo_zayavok, korr, kval, priezd, prodazhi, nekorr и т.д.
        agg_cases = build_leads_agg_sql(conn)

        create_sql = _SQL_TEMPLATE.format(leads_agg_sql=agg_cases)

        with conn.cursor() as cur:
            # ARF_CASCADE_2026-06-30: arc_fact VIEW зависит от analytics_report_criterion,
            # поэтому DROP нужен с CASCADE. VIEW воссоздаётся ниже после CTAS.
            cur.execute("DROP TABLE IF EXISTS public.analytics_report_criterion CASCADE")
            logger.info("DROP TABLE analytics_report_criterion CASCADE: OK")

            cur.execute(create_sql)
            logger.info("CREATE TABLE analytics_report_criterion: OK")

        conn.commit()
        elapsed_build = time.monotonic() - t0

        # Индексы после CTAS (быстрее чем до вставки)
        with conn.cursor() as cur:
            cur.execute(
                "CREATE INDEX idx_arc_date "
                "ON public.analytics_report_criterion (date)"
            )
            cur.execute(
                "CREATE INDEX idx_arc_domain "
                "ON public.analytics_report_criterion (domain)"
            )
        conn.commit()
        logger.info("Индексы idx_arc_date / idx_arc_domain: OK")

        # ARF_CASCADE_2026-06-30: пересоздаём arc_fact VIEW (был дропнут CASCADE выше).
        # arc_fact = passthrough SELECT * FROM analytics_report_criterion (по образцу arf_fact/arp_fact).
        with conn.cursor() as cur:
            cur.execute(
                "CREATE OR REPLACE VIEW public.arc_fact AS "
                "SELECT * FROM public.analytics_report_criterion"
            )
        conn.commit()
        logger.info("VIEW arc_fact: OK (recreated after CASCADE)")

        # Статистика
        with conn.cursor() as cur:
            cur.execute(_STATS_SQL)
            row = cur.fetchone()
        conn.rollback()  # закрываем read-транзакцию

        total_rows, min_date, max_date, total_cost, kol_vo, korr, kval, priezd, prodazhi = row

        logger.info(
            "analytics_report_criterion: rows=%s date=%s..%s "
            "cost=%.2f kol_vo=%s korr=%s kval=%s priezd=%s prodazhi=%s elapsed=%.1fs",
            total_rows, min_date, max_date,
            float(total_cost or 0),
            int(kol_vo or 0), int(korr or 0), int(kval or 0),
            int(priezd or 0), int(prodazhi or 0),
            elapsed_build,
        )

        return {
            "rows": int(total_rows or 0),
            "min_date": str(min_date),
            "max_date": str(max_date),
            "total_cost": float(total_cost or 0),
            "kol_vo_zayavok": int(kol_vo or 0),
            "korr": int(korr or 0),
            "kval": int(kval or 0),
            "priezd": int(priezd or 0),
            "prodazhi": int(prodazhi or 0),
            "elapsed": round(elapsed_build, 1),
        }

    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


if __name__ == "__main__":
    import logging as _logging

    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    from config.db import close_pool, init_pool

    init_pool()
    try:
        result = build()
        print(result)
    finally:
        close_pool()
