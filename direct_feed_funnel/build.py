"""
Build Direct feed funnel tables.

Sources:
  - public.yandex_direct_feeds_report: feed spend/clicks/impressions
  - public.leads_all: CRM funnel, linked through utm_content token "fid:<feed>"

The builder keeps this funnel separate from big_analytics_full because feed is a
different grain. This avoids multiplying the main director fact by feed rows.

Join key, agreed with the director report grain:
  - Date|CampaignId|AdGroupId|feed_key
  - for tp6/tp7 campaigns: Date|CampaignId|feed_key

Date is yandex_direct_feeds_report.date on the spend side and
leads_all.created_date on the lead side. feed_key is parsed from
leads_all.utm_content fid:<...> and normalized from feed_name on the spend side.
"""

from __future__ import annotations

import logging
from textwrap import dedent

from config.db import close_pool, get_conn, init_pool, put_conn
from config.settings import DATE_FROM
from config.status_sql import load_status_sql

logger = logging.getLogger("pipeline.direct_feed_funnel")

T_ATTR = "direct_feed_lead_attribution"
T_FACT = "fact_direct_feed_funnel"
T_QUALITY = "fact_direct_feed_funnel_quality"
T_ALIAS = "direct_feed_key_aliases"
V_FACT = "v_direct_feed_funnel"
V_QUALITY = "v_direct_feed_funnel_quality"


def _exec(cur, sql: str) -> None:
    cur.execute(dedent(sql))


def _ddl(cur) -> None:
    _exec(
        cur,
        f"""
        CREATE TABLE IF NOT EXISTS public.{T_ALIAS} (
            fid_key     TEXT NOT NULL,
            feed_key    TEXT NOT NULL,
            note        TEXT,
            created_at  TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (fid_key, feed_key)
        );

        DROP VIEW IF EXISTS public.{V_QUALITY};
        DROP VIEW IF EXISTS public.{V_FACT};
        DROP TABLE IF EXISTS public.{T_QUALITY};
        DROP TABLE IF EXISTS public.{T_FACT};
        DROP TABLE IF EXISTS public.{T_ATTR};

        CREATE TABLE public.{T_ATTR} (
            lead_id          INTEGER PRIMARY KEY,
            lead_date        DATE NOT NULL,
            parsed_fid_key   TEXT NOT NULL,
            match_method     TEXT NOT NULL,
            match_count      INTEGER NOT NULL DEFAULT 0,
            is_ambiguous     BOOLEAN NOT NULL DEFAULT FALSE,
            domain           TEXT,
            campaign_id      BIGINT,
            campaign_name    TEXT,
            adgroup_id       BIGINT,
            adgroup_name     TEXT,
            feed_id          BIGINT,
            feed_name        TEXT,
            feed_key         TEXT,
            feed_join_key    TEXT,
            source_type      TEXT,
            salon            TEXT,
            status           TEXT,
            reason           TEXT,
            utm_content      TEXT,
            kol_vo_zayavok   NUMERIC,
            korr             NUMERIC,
            kval             NUMERIC,
            priezd           NUMERIC,
            prodazhi         NUMERIC,
            nekorr           NUMERIC,
            ne_otvechaet     NUMERIC,
            filtr            NUMERIC,
            nedozvon         NUMERIC,
            priedet          NUMERIC,
            dohod_do_kredita NUMERIC,
            dobro            NUMERIC,
            generated_at     TIMESTAMPTZ DEFAULT NOW()
        );

        CREATE INDEX idx_{T_ATTR}_feed
            ON public.{T_ATTR} (feed_id, lead_date)
            WHERE feed_id IS NOT NULL AND NOT is_ambiguous;
        CREATE INDEX idx_{T_ATTR}_method
            ON public.{T_ATTR} (match_method, is_ambiguous);
        CREATE INDEX idx_{T_ATTR}_fid
            ON public.{T_ATTR} (parsed_fid_key);

        CREATE TABLE public.{T_FACT} (
            date                  DATE NOT NULL,
            login_key             TEXT,
            domain                TEXT,
            campaign_id           BIGINT,
            campaign_name         TEXT,
            adgroup_id            BIGINT,
            adgroup_name          TEXT,
            feed_id               BIGINT,
            feed_name             TEXT,
            feed_key              TEXT,
            impressions           BIGINT DEFAULT 0,
            clicks                BIGINT DEFAULT 0,
            total_cost            NUMERIC DEFAULT 0,
            goal_all_forms        NUMERIC DEFAULT 0,
            goal_crm_order_created NUMERIC DEFAULT 0,
            goal_crm_order_paid   NUMERIC DEFAULT 0,
            goal_crm_spam_order   NUMERIC DEFAULT 0,
            goal_crm_order_canceled NUMERIC DEFAULT 0,
            kol_vo_zayavok        NUMERIC DEFAULT 0,
            korr                  NUMERIC DEFAULT 0,
            kval                  NUMERIC DEFAULT 0,
            priezd                NUMERIC DEFAULT 0,
            prodazhi              NUMERIC DEFAULT 0,
            nekorr                NUMERIC DEFAULT 0,
            ne_otvechaet          NUMERIC DEFAULT 0,
            filtr                 NUMERIC DEFAULT 0,
            nedozvon              NUMERIC DEFAULT 0,
            priedet               NUMERIC DEFAULT 0,
            dohod_do_kredita      NUMERIC,
            dobro                 NUMERIC,
            attributed_leads      INTEGER DEFAULT 0,
            generated_at          TIMESTAMPTZ DEFAULT NOW()
        );

        CREATE INDEX idx_{T_FACT}_date ON public.{T_FACT} (date);
        CREATE INDEX idx_{T_FACT}_feed ON public.{T_FACT} (feed_id, date);
        CREATE INDEX idx_{T_FACT}_campaign_group
            ON public.{T_FACT} (campaign_id, adgroup_id, date);

        CREATE TABLE public.{T_QUALITY} (
            date                DATE NOT NULL,
            parsed_fid_key      TEXT NOT NULL,
            total_fid_leads     INTEGER DEFAULT 0,
            exact_fid_leads     INTEGER DEFAULT 0,
            unique_group_leads  INTEGER DEFAULT 0,
            ambiguous_leads     INTEGER DEFAULT 0,
            unmatched_fid_leads INTEGER DEFAULT 0,
            generated_at        TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (date, parsed_fid_key)
        );

        CREATE INDEX idx_{T_QUALITY}_fid ON public.{T_QUALITY} (parsed_fid_key);
        """,
    )


def _build_temp_feed_tables(cur) -> None:
    _exec(
        cur,
        f"""
        CREATE TEMP TABLE _feed_report_agg ON COMMIT DROP AS
        SELECT
            date,
            login_key,
            domain,
            campaign_id,
            campaign_name,
            adgroup_id,
            adgroup_name,
            feed_id,
            feed_name,
            regexp_replace(
                regexp_replace(
                    regexp_replace(
                        trim(lower(regexp_replace(coalesce(feed_name, ''), '^.*/', ''))),
                        '\\\\.xml$',
                        ''
                    ),
                    '^фид\\\\s+',
                    ''
                ),
                '^new\\\\s+',
                ''
            ) AS feed_key,
            SUM(COALESCE(impressions, 0)) AS impressions,
            SUM(COALESCE(clicks, 0)) AS clicks,
            SUM(COALESCE(cost, 0)) AS total_cost,
            SUM(COALESCE(goal_all_forms, 0)) AS goal_all_forms,
            SUM(COALESCE(goal_crm_order_created, 0)) AS goal_crm_order_created,
            SUM(COALESCE(goal_crm_order_paid, 0)) AS goal_crm_order_paid,
            SUM(COALESCE(goal_crm_spam_order, 0)) AS goal_crm_spam_order,
            SUM(COALESCE(goal_crm_order_canceled, 0)) AS goal_crm_order_canceled,
            (
                REGEXP_MATCH(REPLACE(COALESCE(campaign_name, ''), chr(1089), 'c'), '(?i)(tp[67]_)')
            )[1] IS NOT NULL AS is_tp67
        FROM public.yandex_direct_feeds_report
        WHERE date >= DATE '{DATE_FROM}'
        GROUP BY 1,2,3,4,5,6,7,8,9,10,19;

        CREATE INDEX ON _feed_report_agg (feed_key, campaign_id, adgroup_id, domain);
        CREATE INDEX ON _feed_report_agg (feed_id, date);
        CREATE INDEX ON _feed_report_agg (date, campaign_id, adgroup_id, feed_key);

        CREATE TEMP TABLE _feed_keys ON COMMIT DROP AS
        SELECT
            date,
            domain,
            campaign_id,
            MIN(campaign_name) AS campaign_name,
            adgroup_id,
            MIN(adgroup_name) AS adgroup_name,
            feed_id,
            MIN(feed_name) AS feed_name,
            feed_key,
            BOOL_OR(is_tp67) AS is_tp67,
            MIN(date) AS min_date,
            MAX(date) AS max_date,
            LOWER(
                date::text || '|' ||
                campaign_id::text || '|' ||
                CASE WHEN BOOL_OR(is_tp67) THEN '' ELSE COALESCE(adgroup_id::text, '0') || '|' END ||
                feed_key
            ) AS feed_join_key
        FROM _feed_report_agg
        GROUP BY 1,2,3,5,7,9;

        CREATE INDEX ON _feed_keys (feed_key, campaign_id, adgroup_id, domain);
        CREATE INDEX ON _feed_keys (campaign_id, adgroup_id, domain);
        CREATE INDEX ON _feed_keys (feed_join_key);

        CREATE TEMP TABLE _tp67_campaigns ON COMMIT DROP AS
        SELECT DISTINCT campaign_id
        FROM _feed_keys
        WHERE is_tp67;

        CREATE INDEX ON _tp67_campaigns (campaign_id);

        CREATE TEMP TABLE _feed_key_map ON COMMIT DROP AS
        SELECT DISTINCT feed_key AS fid_key, feed_key
        FROM _feed_keys
        WHERE feed_key IS NOT NULL AND feed_key <> ''
        UNION
        SELECT trim(lower(fid_key)) AS fid_key, trim(lower(feed_key)) AS feed_key
        FROM public.{T_ALIAS};

        CREATE INDEX ON _feed_key_map (fid_key, feed_key);

        """,
    )


def _build_attribution(cur, status_cases: str) -> None:
    _exec(
        cur,
        f"""
        CREATE TEMP TABLE _lead_fids ON COMMIT DROP AS
        WITH lead_candidates AS (
            SELECT
                l.id,
                l.created_date,
                d.name AS domain,
                l.campaign_id,
                l.group_id,
                l.source_type,
                l.salon,
                l.status,
                l.reason,
                l.utm_content,
                (regexp_match(coalesce(l.utm_content, ''), 'fid:([^| ]+)'))[1] AS fid_raw
            FROM public.leads_all l
            LEFT JOIN public.local_domains d ON d.id = l.domain_id
            WHERE l.created_date >= DATE '{DATE_FROM}'
              AND lower(coalesce(l.utm_content, '')) LIKE '%fid:%'
        ),
        normalized AS (
            SELECT
                lc.*,
                regexp_replace(
                    regexp_replace(trim(lower(lc.fid_raw)), '\\\\.xml$', ''),
                    '^фид\\\\s+',
                    ''
                ) AS parsed_fid_key
            FROM lead_candidates lc
            WHERE lc.fid_raw IS NOT NULL
        )
        SELECT
            l.id AS lead_id,
            l.created_date::date AS lead_date,
            l.domain,
            l.campaign_id,
            l.group_id,
            l.source_type,
            l.salon,
            l.status,
            l.reason,
            l.utm_content,
            l.parsed_fid_key,
            LOWER(
                l.created_date::date::text || '|' ||
                COALESCE(l.campaign_id::text, '0') || '|' ||
                CASE WHEN t67.campaign_id IS NOT NULL THEN ''
                     ELSE COALESCE(l.group_id::text, '0') || '|'
                END ||
                l.parsed_fid_key
            ) AS feed_join_key,
            {status_cases}
        FROM normalized l
        LEFT JOIN _tp67_campaigns t67 ON t67.campaign_id = l.campaign_id;

        CREATE INDEX ON _lead_fids (feed_join_key);
        CREATE INDEX ON _lead_fids (parsed_fid_key, campaign_id, group_id, lead_date);
        CREATE INDEX ON _lead_fids (lead_id);

        CREATE TEMP TABLE _lead_match_candidates ON COMMIT DROP AS
        SELECT
            l.lead_id,
            1 AS priority,
            'feed_key3'::text AS match_method,
            f.domain,
            f.campaign_id,
            f.campaign_name,
            f.adgroup_id,
            f.adgroup_name,
            f.feed_id,
            f.feed_name,
            f.feed_key,
            f.feed_join_key
        FROM _lead_fids l
        JOIN _feed_key_map m ON m.fid_key = l.parsed_fid_key
        JOIN _feed_keys f ON f.feed_key = m.feed_key
            AND f.feed_join_key = LOWER(
                l.lead_date::text || '|' ||
                COALESCE(l.campaign_id::text, '0') || '|' ||
                CASE WHEN f.is_tp67 THEN ''
                     ELSE COALESCE(l.group_id::text, '0') || '|'
                END ||
                m.feed_key
            );

        CREATE INDEX ON _lead_match_candidates (lead_id, priority);

        WITH ranked AS (
            SELECT
                c.*,
                MIN(priority) OVER (PARTITION BY lead_id) AS best_priority
            FROM _lead_match_candidates c
        ),
        best AS (
            SELECT
                r.*,
                COUNT(*) OVER (PARTITION BY lead_id, best_priority) AS best_match_count,
                ROW_NUMBER() OVER (PARTITION BY lead_id ORDER BY priority, feed_id) AS rn
            FROM ranked r
            WHERE priority = best_priority
        )
        INSERT INTO public.{T_ATTR} (
            lead_id, lead_date, parsed_fid_key, match_method, match_count, is_ambiguous,
            domain, campaign_id, campaign_name, adgroup_id, adgroup_name,
            feed_id, feed_name, feed_key, feed_join_key,
            source_type, salon, status, reason, utm_content,
            kol_vo_zayavok, korr, kval, priezd, prodazhi, nekorr,
            ne_otvechaet, filtr, nedozvon, priedet, dohod_do_kredita, dobro
        )
        SELECT
            l.lead_id,
            l.lead_date,
            l.parsed_fid_key,
            CASE
                WHEN b.lead_id IS NULL THEN 'unmatched'
                WHEN b.best_match_count > 1 THEN b.match_method || '_ambiguous'
                ELSE b.match_method
            END AS match_method,
            COALESCE(b.best_match_count, 0) AS match_count,
            COALESCE(b.best_match_count > 1, FALSE) AS is_ambiguous,
            CASE WHEN b.best_match_count = 1 THEN b.domain ELSE NULL END,
            CASE WHEN b.best_match_count = 1 THEN b.campaign_id ELSE l.campaign_id END,
            CASE WHEN b.best_match_count = 1 THEN b.campaign_name ELSE NULL END,
            CASE WHEN b.best_match_count = 1 THEN b.adgroup_id ELSE l.group_id END,
            CASE WHEN b.best_match_count = 1 THEN b.adgroup_name ELSE NULL END,
            CASE WHEN b.best_match_count = 1 THEN b.feed_id ELSE NULL END,
            CASE WHEN b.best_match_count = 1 THEN b.feed_name ELSE NULL END,
            CASE WHEN b.best_match_count = 1 THEN b.feed_key ELSE NULL END,
            l.feed_join_key,
            l.source_type,
            l.salon,
            l.status,
            l.reason,
            l.utm_content,
            l.kol_vo_zayavok,
            l.korr,
            l.kval,
            l.priezd,
            l.prodazhi,
            l.nekorr,
            l.ne_otvechaet,
            l.filtr,
            l.nedozvon,
            l.priedet,
            l.dohod_do_kredita,
            l.dobro
        FROM _lead_fids l
        LEFT JOIN best b ON b.lead_id = l.lead_id AND b.rn = 1;
        """,
    )


def _build_fact(cur) -> None:
    _exec(
        cur,
        f"""
        CREATE TEMP TABLE _feed_leads_agg ON COMMIT DROP AS
        SELECT
            lead_date AS date,
            domain,
            campaign_id,
            campaign_name,
            adgroup_id,
            adgroup_name,
            feed_id,
            feed_name,
            feed_key,
            SUM(kol_vo_zayavok) AS kol_vo_zayavok,
            SUM(korr) AS korr,
            SUM(kval) AS kval,
            SUM(priezd) AS priezd,
            SUM(prodazhi) AS prodazhi,
            SUM(nekorr) AS nekorr,
            SUM(ne_otvechaet) AS ne_otvechaet,
            SUM(filtr) AS filtr,
            SUM(nedozvon) AS nedozvon,
            SUM(priedet) AS priedet,
            SUM(dohod_do_kredita) AS dohod_do_kredita,
            SUM(dobro) AS dobro,
            COUNT(*)::integer AS attributed_leads
        FROM public.{T_ATTR}
        WHERE feed_id IS NOT NULL
          AND NOT is_ambiguous
        GROUP BY 1,2,3,4,5,6,7,8,9;

        CREATE INDEX ON _feed_leads_agg (feed_id, date);

        INSERT INTO public.{T_QUALITY} (
            date, parsed_fid_key, total_fid_leads, exact_fid_leads, unique_group_leads,
            ambiguous_leads, unmatched_fid_leads
        )
        SELECT
            lead_date AS date,
            parsed_fid_key,
            COUNT(*)::integer AS total_fid_leads,
            COUNT(*) FILTER (WHERE match_method = 'feed_key3')::integer AS exact_fid_leads,
            0::integer AS unique_group_leads,
            COUNT(*) FILTER (WHERE is_ambiguous)::integer AS ambiguous_leads,
            COUNT(*) FILTER (WHERE match_method = 'unmatched')::integer AS unmatched_fid_leads
        FROM public.{T_ATTR}
        GROUP BY 1,2;

        INSERT INTO public.{T_FACT} (
            date, login_key, domain, campaign_id, campaign_name, adgroup_id, adgroup_name,
            feed_id, feed_name, feed_key,
            impressions, clicks, total_cost,
            goal_all_forms, goal_crm_order_created, goal_crm_order_paid,
            goal_crm_spam_order, goal_crm_order_canceled,
            kol_vo_zayavok, korr, kval, priezd, prodazhi, nekorr,
            ne_otvechaet, filtr, nedozvon, priedet, dohod_do_kredita, dobro,
            attributed_leads
        )
        SELECT
            COALESCE(s.date, l.date) AS date,
            s.login_key,
            COALESCE(s.domain, l.domain) AS domain,
            COALESCE(s.campaign_id, l.campaign_id) AS campaign_id,
            COALESCE(s.campaign_name, l.campaign_name) AS campaign_name,
            COALESCE(s.adgroup_id, l.adgroup_id) AS adgroup_id,
            COALESCE(s.adgroup_name, l.adgroup_name) AS adgroup_name,
            COALESCE(s.feed_id, l.feed_id) AS feed_id,
            COALESCE(s.feed_name, l.feed_name) AS feed_name,
            COALESCE(s.feed_key, l.feed_key) AS feed_key,
            COALESCE(s.impressions, 0) AS impressions,
            COALESCE(s.clicks, 0) AS clicks,
            COALESCE(s.total_cost, 0) AS total_cost,
            COALESCE(s.goal_all_forms, 0) AS goal_all_forms,
            COALESCE(s.goal_crm_order_created, 0) AS goal_crm_order_created,
            COALESCE(s.goal_crm_order_paid, 0) AS goal_crm_order_paid,
            COALESCE(s.goal_crm_spam_order, 0) AS goal_crm_spam_order,
            COALESCE(s.goal_crm_order_canceled, 0) AS goal_crm_order_canceled,
            COALESCE(l.kol_vo_zayavok, 0) AS kol_vo_zayavok,
            COALESCE(l.korr, 0) AS korr,
            COALESCE(l.kval, 0) AS kval,
            COALESCE(l.priezd, 0) AS priezd,
            COALESCE(l.prodazhi, 0) AS prodazhi,
            COALESCE(l.nekorr, 0) AS nekorr,
            COALESCE(l.ne_otvechaet, 0) AS ne_otvechaet,
            COALESCE(l.filtr, 0) AS filtr,
            COALESCE(l.nedozvon, 0) AS nedozvon,
            COALESCE(l.priedet, 0) AS priedet,
            l.dohod_do_kredita,
            l.dobro,
            COALESCE(l.attributed_leads, 0) AS attributed_leads
        FROM _feed_report_agg s
        FULL JOIN _feed_leads_agg l
          ON l.date = s.date
         AND l.feed_id = s.feed_id
         AND l.campaign_id = s.campaign_id
         AND l.adgroup_id = s.adgroup_id
         AND COALESCE(l.domain, '') = COALESCE(s.domain, '')
        ;

        CREATE VIEW public.{V_FACT} AS
        SELECT
            date,
            login_key,
            domain,
            campaign_id,
            campaign_name,
            adgroup_id,
            adgroup_name,
            feed_id,
            feed_name,
            feed_key,
            impressions,
            clicks,
            total_cost,
            goal_all_forms AS metrika_forms,
            kol_vo_zayavok AS obrashcheniya,
            korr AS zayavki,
            kval,
            priezd AS vizity,
            prodazhi,
            nekorr,
            ne_otvechaet,
            filtr,
            nedozvon,
            priedet,
            dohod_do_kredita,
            dobro,
            attributed_leads,
            CASE WHEN kol_vo_zayavok > 0 THEN total_cost / kol_vo_zayavok END AS cpl_obrashcheniya,
            CASE WHEN korr > 0 THEN total_cost / korr END AS cpl_zayavki,
            CASE WHEN kval > 0 THEN total_cost / kval END AS cpl_kval,
            CASE WHEN priezd > 0 THEN total_cost / priezd END AS cpl_vizity,
            CASE WHEN prodazhi > 0 THEN total_cost / prodazhi END AS cpl_prodazhi
        FROM public.{T_FACT};

        CREATE VIEW public.{V_QUALITY} AS
        SELECT
            date,
            parsed_fid_key AS feed_key,
            total_fid_leads,
            exact_fid_leads,
            unique_group_leads,
            ambiguous_leads,
            unmatched_fid_leads,
            CASE WHEN total_fid_leads > 0
                 THEN 100.0 * (exact_fid_leads + unique_group_leads) / total_fid_leads
            END AS matched_pct
        FROM public.{T_QUALITY};
        """,
    )


def _log_stats(cur) -> None:
    cur.execute(
        f"""
        SELECT match_method, COUNT(*)
        FROM public.{T_ATTR}
        GROUP BY match_method
        ORDER BY COUNT(*) DESC
        """
    )
    logger.info("direct feed attribution: %s", cur.fetchall())

    cur.execute(
        f"""
        SELECT
            COUNT(*) AS fact_rows,
            COALESCE(SUM(total_cost), 0) AS total_cost,
            COALESCE(SUM(kol_vo_zayavok), 0) AS obrashcheniya,
            COALESCE(SUM(korr), 0) AS zayavki,
            COALESCE(SUM(kval), 0) AS kval,
            COALESCE(SUM(priezd), 0) AS vizity,
            COALESCE(SUM(prodazhi), 0) AS prodazhi,
            COALESCE(SUM(attributed_leads), 0) AS attributed_leads
        FROM public.{T_FACT}
        """
    )
    logger.info("direct feed funnel fact: %s", cur.fetchone())


def build() -> None:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SET work_mem = '256MB'")
            status_cases = load_status_sql(conn)["status_cases"]
            _ddl(cur)
            _build_temp_feed_tables(cur)
            _build_attribution(cur, status_cases)
            _build_fact(cur)
            _log_stats(cur)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    init_pool()
    try:
        build()
    finally:
        close_pool()


if __name__ == "__main__":
    main()
