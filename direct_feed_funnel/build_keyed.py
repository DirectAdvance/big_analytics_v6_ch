"""
Build Direct feed funnel by the agreed physical key.

Patches:
  SOURCE_TYPE_SITE_GUARD_2026-07-25 — shadow_orders load: +source_type='site' defensive filter
  PHONE_CRM_GUARD_2026-07-25 — key2 phone guard attempted but reverted: phones differ between
    shadow_orders and leads_all for all key-2 matches; hard/soft guards both killed all 3956
    key-2 matches causing -3471 attributed regression. Needs cross-CRM crm_id resolution first.
  DOMAIN_GUARD_KEY2_2026-07-25 — attempted but REVERTED: order_domain from entry_point URL has
    subdomains (auto.dealer.ru) while lb.domain from local_domains is root domain (dealer.ru);
    even NULL-safe equality killed 2163/2206 non-empty-domain key-2 matches. Cannot use domain
    for cross-CRM collision guard without subdomain normalization.
  FEED_CAMPAIGN_ADGROUP_GUARD_2026-07-26 — fallback_order_match (key-2): added EXISTS guard
    against yandex_direct_feeds_report so only truly feed campaigns/adgroups are attributed.
    Root cause: 47.4% of key-2 matches (10,368/21,889) had campaign_id absent from feeds_report
    (fid was a random URL param, not a real feed); 77 more had campaign present but adgroup_id
    absent. Guard: campaign guard (campaign_id must be in feeds_report) + adgroup guard
    (adgroup_id must be in feeds_report for same campaign, applied only when group_id IS NOT NULL).

Match key (FEED_DOMAIN_MATCH_2026-06-29):
    dk2 = Date|domain|feed_key

Old spend key (feed_key3, kept in T_SPEND for reference):
    Date|CampaignId|AdGroupId|feed_key  (tp6/tp7: Date|CampaignId|feed_key)

Leads are matched to spend by (date, domain, feed_key) — campaign_id and
adgroup_id are deliberately dropped from the match key because the campaign that
generated the click in the feed report is never the same as the campaign
attributed to the lead (autotarget routing). The result is built through
physical keyed tables and a FULL OUTER JOIN so that:
  - leads with no matching spend row are preserved (funnel metrics visible);
  - spend with no matching leads are preserved (cost visible, funnel = 0).
"""

from __future__ import annotations

import logging
import re
from textwrap import dedent
from urllib.parse import parse_qs, unquote, urlsplit

import psycopg2
from psycopg2.extras import execute_values

from config.db import close_pool, get_conn, init_pool, put_conn
from config.settings import DATE_FROM
from config.status_sql import load_status_sql
from loader import load_db

logger = logging.getLogger("pipeline.direct_feed_funnel_keyed")

T_SPEND = "direct_feed_spend_keyed"
T_LEADS = "direct_feed_leads_keyed"
T_FACT = "fact_direct_feed_funnel"
T_QUALITY = "fact_direct_feed_funnel_quality"
SHADOW_BATCH = 10_000


def _normalize_feed_key(raw: str | None) -> str | None:
    if not raw:
        return None
    v = raw.strip().lower()
    if not v:
        return None
    v = re.sub(r"^.*/", "", v)
    v = re.sub(r"[.]xml$", "", v)
    v = re.sub(r"^фид\s+", "", v)
    v = re.sub(r"^new\s+", "", v)
    return v or None


def _resolve_campaign_id(campaign_id: object, utm_campaign: str | None) -> str | None:
    if campaign_id is not None and str(campaign_id).strip():
        return str(campaign_id).strip()
    if utm_campaign:
        m = re.match(r"^\s*([0-9]+)\|", utm_campaign)
        if m:
            return m.group(1)
    return None


def _extract_order_meta(entry_point: str | None) -> tuple[str | None, str | None]:
    if not entry_point:
        return None, None
    raw = entry_point.strip()
    if not raw:
        return None, None
    try:
        split = urlsplit(raw)
    except Exception:
        split = None
    host = None
    fid = None
    if split:
        host = (split.hostname or "").lower() or None
        if host and host.startswith("www."):
            host = host[4:]
        params = parse_qs(split.query, keep_blank_values=True)
        fid_vals = params.get("fid") or params.get("FID")
        if fid_vals:
            fid = unquote(fid_vals[0]).strip()
    if fid is None:
        m = re.search(r"(?i)(?:[?&]fid=)([^&#]+)", raw)
        if m:
            fid = unquote(m.group(1)).strip()
    return host, _normalize_feed_key(fid)


def _digits10(phone: str | None) -> str | None:
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    if len(digits) < 10:
        return None
    return digits[-10:]


def _load_shadow_orders_temp(conn: psycopg2.extensions.connection) -> int:
    """Load only orders that can enrich fid into a temp table inside DST session."""
    shadow = load_db("shadow_orders")
    inserted = 0
    with conn.cursor() as cur:
        _exec(
            cur,
            """
            CREATE TEMP TABLE _shadow_orders_fid (
                order_id              BIGINT,
                external_id_crm       TEXT,
                order_date            DATE,
                order_domain          TEXT,
                yclid                 TEXT,
                phone_last10          TEXT,
                campaign_id_resolved  TEXT,
                fid_key               TEXT,
                utm_content           TEXT
            ) ON COMMIT DROP;
            """,
        )
    src = psycopg2.connect(
        host=shadow["host"],
        port=shadow["port"],
        dbname=shadow["database"],
        user=shadow["user"],
        password=shadow["password"],
        connect_timeout=30,
        sslmode="require",
        options="-c statement_timeout=0",
        keepalives=1,
        keepalives_idle=10,
        keepalives_interval=5,
        keepalives_count=10,
    )
    try:
        with src.cursor(name="shadow_orders_feed_fid") as s_cur:
            s_cur.itersize = SHADOW_BATCH
            s_cur.execute(
                """
                SELECT
                    id,
                    NULLIF(BTRIM(external_id_crm), '') AS external_id_crm,
                    created_at::date                   AS order_date,
                    entry_point,
                    NULLIF(BTRIM(yclid), '')           AS yclid,
                    NULLIF(BTRIM(phone_normalized), '') AS phone_normalized,
                    campaign_id,
                    utm_campaign,
                    NULLIF(BTRIM(utm_content), '')     AS utm_content
                FROM public.orders
                WHERE created_at::date >= %s
                  AND source_type = 'site'
                  AND entry_point IS NOT NULL
                  AND BTRIM(entry_point) <> ''
                  AND (
                        lower(entry_point) LIKE '%%?fid=%%'
                     OR lower(entry_point) LIKE '%%&fid=%%'
                  )
                  AND (
                        NULLIF(BTRIM(external_id_crm), '') IS NOT NULL
                     OR (
                            NULLIF(BTRIM(yclid), '') IS NOT NULL
                        AND NULLIF(BTRIM(phone_normalized), '') IS NOT NULL
                        AND (
                                campaign_id IS NOT NULL
                             OR utm_campaign ~ '^[0-9]+\\|'
                            )
                        )
                  )
                """,
                (DATE_FROM,),
            )
            insert_sql = """
                INSERT INTO _shadow_orders_fid (
                    order_id, external_id_crm, order_date, order_domain,
                    yclid, phone_last10, campaign_id_resolved, fid_key, utm_content
                ) VALUES %s
            """
            while True:
                rows = s_cur.fetchmany(SHADOW_BATCH)
                if not rows:
                    break
                payload: list[tuple[object, ...]] = []
                for row in rows:
                    order_id, external_id_crm, order_date, entry_point, yclid, phone_normalized, campaign_id, utm_campaign, utm_content = row
                    order_domain, fid_key = _extract_order_meta(entry_point)
                    campaign_id_resolved = _resolve_campaign_id(campaign_id, utm_campaign)
                    phone_last10 = _digits10(phone_normalized)
                    if not fid_key:
                        continue
                    if not external_id_crm and not (
                        order_domain and yclid and phone_last10 and campaign_id_resolved
                    ):
                        continue
                    payload.append(
                        (
                            order_id,
                            external_id_crm,
                            order_date,
                            order_domain,
                            yclid,
                            phone_last10,
                            campaign_id_resolved,
                            fid_key,
                            utm_content,
                        )
                    )
                if payload:
                    with conn.cursor() as dst_cur:
                        execute_values(dst_cur, insert_sql, payload, page_size=2000)
                    inserted += len(payload)
    finally:
        src.close()
    with conn.cursor() as cur:
        _exec(
            cur,
            """
            CREATE INDEX ON _shadow_orders_fid (external_id_crm);
            CREATE INDEX ON _shadow_orders_fid (order_date, order_domain, yclid, phone_last10, campaign_id_resolved);
            CREATE INDEX ON _shadow_orders_fid (fid_key);
            """,
        )
    logger.info("shadow orders feed-fid staged: rows=%s", inserted)
    return inserted


def _exec(cur, sql: str) -> None:
    cur.execute(dedent(sql))


def build() -> None:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SET work_mem = '256MB'")
            status_cases = load_status_sql(conn)["status_cases"]

            _exec(
                cur,
                f"""
                DROP VIEW IF EXISTS public.v_direct_feed_funnel_quality;
                DROP VIEW IF EXISTS public.v_direct_feed_funnel;
                DROP TABLE IF EXISTS public.{T_FACT};
                DROP TABLE IF EXISTS public.{T_QUALITY};
                DROP TABLE IF EXISTS public.{T_LEADS};
                DROP TABLE IF EXISTS public.{T_SPEND};

                CREATE TABLE public.{T_SPEND} AS
                WITH src AS (
                    SELECT
                        r.date,
                        r.login_key,
                        r.domain,
                        r.campaign_id,
                        r.campaign_name,
                        r.adgroup_id,
                        r.adgroup_name,
                        r.feed_id,
                        r.feed_name,
                        r.feed_url,
                        coalesce(
                            nullif(r.feed_url_key, ''),
                            CASE lower(trim(coalesce(r.feed_name, '')))
                                WHEN 'y' THEN 'yandex.xml'
                                WHEN 'каталог-модель' THEN 'yandex-catalog-model.xml'
                                WHEN 'кастом-нейм' THEN 'yandex-catalog-model-design-custom-name.xml'
                                ELSE NULL
                            END,
                            CASE
                                WHEN lower(coalesce(r.feed_name, '')) ~ '[.]xml$'
                                THEN lower(regexp_replace(regexp_replace(r.feed_name, '^.*/', ''), '^.* — ', ''))
                                ELSE NULL
                            END,
                            rule.feed_url_key
                        ) AS feed_url_key,
                        regexp_replace(
                            regexp_replace(
                                regexp_replace(
                                    trim(lower(regexp_replace(coalesce(
                                        nullif(r.feed_url_key, ''),
                                        CASE lower(trim(coalesce(r.feed_name, '')))
                                            WHEN 'y' THEN 'yandex.xml'
                                            WHEN 'каталог-модель' THEN 'yandex-catalog-model.xml'
                                            WHEN 'кастом-нейм' THEN 'yandex-catalog-model-design-custom-name.xml'
                                            ELSE NULL
                                        END,
                                        CASE
                                            WHEN lower(coalesce(r.feed_name, '')) ~ '[.]xml$'
                                            THEN lower(regexp_replace(regexp_replace(r.feed_name, '^.*/', ''), '^.* — ', ''))
                                            ELSE NULL
                                        END,
                                        rule.feed_url_key,
                                        r.feed_name,
                                        ''
                                    ), '^.*/', ''))),
                                    '[.]xml$',
                                    ''
                                ),
                                '^фид\\\\s+',
                                ''
                            ),
                            '^new\\\\s+',
                            ''
                        ) AS feed_key,
                        (
                            regexp_match(
                                replace(coalesce(campaign_name, ''), chr(1089), 'c'),
                                '(?i)(tp[67]_)'
                            )
                        )[1] IS NOT NULL AS is_tp67,
                        r.impressions,
                        r.clicks,
                        r.cost,
                        r.goal_all_forms,
                        r.goal_crm_order_created,
                        r.goal_crm_order_paid,
                        r.goal_crm_spam_order,
                        r.goal_crm_order_canceled
                    FROM public.yandex_direct_feeds_report r
                    LEFT JOIN LATERAL (
                        SELECT d.feed_key AS feed_url_key
                        FROM public.direct_global_feed_rules d
                        WHERE lower(coalesce(r.feed_name, '')) LIKE '%' || lower(regexp_replace(d.feed_key, '[.]xml$', '')) || '%'
                        ORDER BY length(regexp_replace(d.feed_key, '[.]xml$', '')) DESC, d.feed_key
                        LIMIT 1
                    ) rule ON true
                    WHERE r.date >= DATE '{DATE_FROM}'
                ),
                keyed AS (
                    SELECT
                        *,
                        CASE WHEN is_tp67 THEN NULL ELSE adgroup_id END AS key_adgroup_id
                    FROM src
                )
                SELECT
                    lower(
                        date::text || '|' ||
                        campaign_id::text || '|' ||
                        CASE WHEN bool_or(is_tp67)
                             THEN ''
                             ELSE coalesce(key_adgroup_id::text, '0') || '|'
                        END ||
                        feed_key
                    ) AS feed_key3,
                    date,
                    login_key,
                    domain,
                    campaign_id,
                    min(campaign_name) AS campaign_name,
                    key_adgroup_id AS adgroup_id,
                    CASE WHEN key_adgroup_id IS NULL THEN NULL ELSE min(adgroup_name) END AS adgroup_name,
                    feed_id,
                    min(feed_name) AS feed_name,
                    min(feed_url) AS feed_url,
                    min(feed_url_key) AS feed_url_key,
                    feed_key,
                    -- FEED_DOMAIN_MATCH_2026-06-29: ключ матча лид↔расход по (date,domain,feed_key)
                    lower(
                        date::text || '|' ||
                        lower(regexp_replace(coalesce(domain, ''), '^www[.]', '')) || '|' ||
                        feed_key
                    ) AS dk2,
                    bool_or(is_tp67) AS is_tp67,
                    sum(coalesce(impressions, 0)) AS impressions,
                    sum(coalesce(clicks, 0)) AS clicks,
                    sum(coalesce(cost, 0)) AS total_cost,
                    sum(coalesce(goal_all_forms, 0)) AS goal_all_forms,
                    sum(coalesce(goal_crm_order_created, 0)) AS goal_crm_order_created,
                    sum(coalesce(goal_crm_order_paid, 0)) AS goal_crm_order_paid,
                    sum(coalesce(goal_crm_spam_order, 0)) AS goal_crm_spam_order,
                    sum(coalesce(goal_crm_order_canceled, 0)) AS goal_crm_order_canceled,
                    now() AS generated_at
                FROM keyed
                GROUP BY
                    date, login_key, domain, campaign_id, key_adgroup_id, feed_id, feed_key;

                CREATE INDEX idx_{T_SPEND}_key ON public.{T_SPEND} (feed_key3);
                CREATE INDEX idx_{T_SPEND}_date ON public.{T_SPEND} (date);
                CREATE INDEX idx_{T_SPEND}_dk2 ON public.{T_SPEND} (dk2);
                -- INDEX_AUDIT_2026-06-27: удалён мёртвый idx_{{T_SPEND}}_feed (idx_scan=0).

                CREATE TEMP TABLE _tp67_campaigns ON COMMIT DROP AS
                SELECT DISTINCT campaign_id
                FROM public.{T_SPEND}
                WHERE is_tp67;

                CREATE INDEX ON _tp67_campaigns (campaign_id);
                """,
            )
            _load_shadow_orders_temp(conn)

            _exec(
                cur,
                f"""
                CREATE TABLE public.{T_LEADS} AS
                WITH lead_base AS (
                    SELECT
                        l.id,
                        l.created_date::date AS date,
                        regexp_replace(lower(coalesce(d.name, '')), '^www[.]', '') AS domain,
                        l.campaign_id,
                        l.group_id,
                        l.source_type,
                        l.salon,
                        l.status,
                        l.reason,
                        l.utm_content,
                        nullif(btrim(l.source_record_id), '') AS source_record_id,
                        nullif(btrim(l.yclid), '') AS yclid,
                        nullif(right(regexp_replace(coalesce(l.phone, ''), '\\D', '', 'g'), 10), '') AS phone_last10,
                        (regexp_match(coalesce(l.utm_content, ''), 'fid:([^| ]+)'))[1] AS fid_raw_utm
                    FROM public.leads_all l
                    LEFT JOIN public.local_domains d ON d.id = l.domain_id
                    WHERE l.created_date >= DATE '{DATE_FROM}'
                ),
                direct_order_match AS (
                    SELECT
                        lb.id AS lead_id,
                        min(s.fid_key) AS fid_key,
                        count(*)::integer AS match_rows,
                        count(DISTINCT s.fid_key)::integer AS fid_variants
                    FROM lead_base lb
                    JOIN _shadow_orders_fid s
                      ON s.external_id_crm = lb.source_record_id
                    WHERE lb.fid_raw_utm IS NULL
                    GROUP BY lb.id
                ),
                fallback_order_match AS (
                    SELECT
                        lb.id AS lead_id,
                        min(s.fid_key) AS fid_key,
                        count(*)::integer AS match_rows,
                        count(DISTINCT s.fid_key)::integer AS fid_variants
                    FROM lead_base lb
                    JOIN _shadow_orders_fid s
                      ON s.order_date = lb.date
                     AND s.order_domain = lb.domain
                     AND s.yclid = lb.yclid
                     AND s.phone_last10 = lb.phone_last10
                     AND s.campaign_id_resolved = lb.campaign_id::text
                    LEFT JOIN direct_order_match dm ON dm.lead_id = lb.id
                    WHERE lb.fid_raw_utm IS NULL
                      AND dm.lead_id IS NULL
                      -- FEED_CAMPAIGN_ADGROUP_GUARD_2026-07-26: campaign must exist in feeds_report
                      -- (campaign guard); if lead group_id is known — adgroup must also be present
                      -- in feeds_report for that same campaign (adgroup guard). Prevents attributing
                      -- fid from random URL params to non-feed campaigns (was 47.4% of key-2 matches).
                      AND EXISTS (
                          SELECT 1
                          FROM public.yandex_direct_feeds_report fr
                          WHERE fr.campaign_id = lb.campaign_id
                            AND (lb.group_id IS NULL OR fr.adgroup_id = lb.group_id)
                      )
                      -- UTM_CONTENT_ADGROUP_FALLBACK_2026-07-27: when group_id IS NULL
                      -- (adgroup unknown in leads_all), verify via utm_content equality —
                      -- both tables encode g:adgroup_id|geoname:... identically, so a match
                      -- proves the same adgroup without needing group_id to be populated.
                      AND (lb.group_id IS NOT NULL OR s.utm_content = lb.utm_content)
                    GROUP BY lb.id
                ),
                lead_candidates AS (
                    SELECT
                        lb.id,
                        lb.date,
                        lb.domain,
                        lb.campaign_id,
                        lb.group_id,
                        lb.source_type,
                        lb.salon,
                        lb.status,
                        lb.reason,
                        lb.utm_content,
                        CASE
                            WHEN lb.fid_raw_utm IS NOT NULL THEN lb.fid_raw_utm
                            WHEN dm.fid_variants = 1 THEN dm.fid_key  -- CROSS_CRM_AMBIGUITY_2026-07-25: 61 external_id_crm (0.13% of ~3,974 key-2) share same fid across 2 CRMs — not caught by this guard; accepted as permissible margin error, not a bug
                            WHEN fm.fid_variants = 1 THEN fm.fid_key
                            ELSE NULL
                        END AS fid_raw,
                        CASE
                            WHEN lb.fid_raw_utm IS NOT NULL THEN 'utm_content'
                            WHEN dm.fid_variants = 1 THEN 'orders_external_id_crm'
                            WHEN fm.fid_variants = 1 THEN 'orders_composite'
                            ELSE NULL
                        END AS fid_source
                    FROM lead_base lb
                    LEFT JOIN direct_order_match dm ON dm.lead_id = lb.id
                    LEFT JOIN fallback_order_match fm ON fm.lead_id = lb.id
                ),
                normalized AS (
                    SELECT
                        lc.*,
                        -- FEED_DOMAIN_MATCH_2026-06-29: нормализация идентична расходной стороне
                        -- (срез пути ^.*/, .xml$, ^фид\s+, ^new\s+)
                        regexp_replace(
                            regexp_replace(
                                regexp_replace(
                                    trim(lower(regexp_replace(lc.fid_raw, '^.*/', ''))),
                                    '[.]xml$', ''
                                ),
                                '^фид\\\\s+',
                                ''
                            ),
                            '^new\\\\s+',
                            ''
                        ) AS feed_key
                    FROM lead_candidates lc
                    WHERE lc.fid_raw IS NOT NULL
                )
                SELECT
                    lower(
                        n.date::text || '|' ||
                        coalesce(n.campaign_id::text, '0') || '|' ||
                        CASE WHEN t67.campaign_id IS NOT NULL
                             THEN ''
                             ELSE coalesce(n.group_id::text, '0') || '|'
                        END ||
                        n.feed_key
                    ) AS feed_key3,
                    n.id AS lead_id,
                    n.date,
                    n.domain,
                    n.campaign_id,
                    CASE WHEN t67.campaign_id IS NOT NULL THEN NULL ELSE n.group_id END AS adgroup_id,
                    n.feed_key,
                    lower(
                        n.date::text || '|' ||
                        lower(regexp_replace(coalesce(n.domain, ''), '^www[.]', '')) || '|' ||
                        n.feed_key
                    ) AS dk2,
                    n.source_type,
                    n.fid_source,
                    n.salon,
                    n.status,
                    n.reason,
                    n.utm_content,
                    {status_cases},
                    now() AS generated_at
                FROM normalized n
                LEFT JOIN _tp67_campaigns t67 ON t67.campaign_id = n.campaign_id;

                CREATE INDEX idx_{T_LEADS}_key ON public.{T_LEADS} (feed_key3);
                CREATE INDEX idx_{T_LEADS}_lead ON public.{T_LEADS} (lead_id);
                CREATE INDEX idx_{T_LEADS}_date ON public.{T_LEADS} (date);
                CREATE INDEX idx_{T_LEADS}_dk2 ON public.{T_LEADS} (dk2);
                """,
            )

            _exec(
                cur,
                f"""
                CREATE TABLE public.{T_QUALITY} AS
                SELECT
                    l.date,
                    l.feed_key,
                    count(*)::integer AS total_fid_leads,
                    count(*) FILTER (WHERE s.dk2 IS NOT NULL)::integer AS matched_leads,
                    count(*) FILTER (WHERE s.dk2 IS NULL)::integer AS unmatched_fid_leads,
                    now() AS generated_at
                FROM public.{T_LEADS} l
                LEFT JOIN (
                    SELECT DISTINCT dk2
                    FROM public.{T_SPEND}
                ) s ON s.dk2 = l.dk2
                GROUP BY 1,2;

                CREATE INDEX idx_{T_QUALITY}_date_feed ON public.{T_QUALITY} (date, feed_key);

                -- FEED_DOMAIN_MATCH_2026-06-29: T_FACT перестроена на ключ dk2 (date|domain|feed_key).
                -- spend_agg агрегирует расход, leads_agg — всю воронку без EXISTS-фильтра,
                -- FULL OUTER JOIN сохраняет и лиды без расхода, и расход без лидов.
                -- Грейн T_FACT: (date, domain, feed_key). campaign_id/adgroup_id убраны
                -- (не однозначны при агрегации по dk2; детали остались в T_SPEND).
                --
                -- FEED_META_LOOKUP_2026-06-29: для lead-only строк (spend IS NULL) поля
                -- feed_name/feed_url/feed_url_key/feed_id заполняются через lookup из T_SPEND
                -- по приоритету: 1) (domain, feed_key) — строго доменный; 2) (feed_key) —
                -- fallback если у домена нет расхода вообще. campaign_id/campaign_name/
                -- login_key/is_tp67 намеренно оставляем NULL: dk2 может иметь несколько
                -- campaign_id (разные кампании одного фида), min() был бы произвольным.
                CREATE TABLE public.{T_FACT} AS
                WITH spend_agg AS (
                    SELECT
                        dk2,
                        min(date)           AS date,
                        min(login_key)      AS login_key,
                        min(domain)         AS domain,
                        min(feed_key)       AS feed_key,
                        min(campaign_id)    AS campaign_id,
                        min(campaign_name)  AS campaign_name,
                        min(feed_id)        AS feed_id,
                        min(feed_name)      AS feed_name,
                        min(nullif(feed_url, ''))       AS feed_url,
                        min(nullif(feed_url_key, ''))   AS feed_url_key,
                        bool_or(is_tp67)    AS is_tp67,
                        sum(impressions)    AS impressions,
                        sum(clicks)         AS clicks,
                        sum(total_cost)     AS total_cost,
                        sum(goal_all_forms)          AS goal_all_forms,
                        sum(goal_crm_order_created)  AS goal_crm_order_created,
                        sum(goal_crm_order_paid)     AS goal_crm_order_paid,
                        sum(goal_crm_spam_order)     AS goal_crm_spam_order,
                        sum(goal_crm_order_canceled) AS goal_crm_order_canceled
                    FROM public.{T_SPEND}
                    GROUP BY dk2
                ),
                leads_agg AS (
                    SELECT
                        dk2,
                        min(date)           AS date,
                        min(domain)         AS domain,
                        min(feed_key)       AS feed_key,
                        count(*)::integer   AS attributed_leads,
                        sum(kol_vo_zayavok) AS kol_vo_zayavok,
                        sum(korr)           AS korr,
                        sum(kval)           AS kval,
                        sum(priezd)         AS priezd,
                        sum(prodazhi)       AS prodazhi,
                        sum(nekorr)         AS nekorr,
                        sum(ne_otvechaet)   AS ne_otvechaet,
                        sum(filtr)          AS filtr,
                        sum(nedozvon)       AS nedozvon,
                        sum(priedet)        AS priedet,
                        sum(dohod_do_kredita) AS dohod_do_kredita,
                        sum(dobro)          AS dobro
                    FROM public.{T_LEADS}
                    GROUP BY dk2
                ),
                meta_by_domain_feed AS (
                    -- FEED_META_LOOKUP_2026-06-29: per (domain, feed_key) lookup
                    -- Приоритет 1: метаданные фида строго по домену лида
                    SELECT
                        domain,
                        feed_key,
                        min(feed_name)    AS feed_name,
                        min(nullif(feed_url, ''))     AS feed_url,
                        min(nullif(feed_url_key, '')) AS feed_url_key,
                        min(feed_id)      AS feed_id
                    FROM public.{T_SPEND}
                    WHERE feed_name IS NOT NULL
                    GROUP BY domain, feed_key
                ),
                meta_by_feed AS (
                    -- FEED_META_LOOKUP_2026-06-29: per feed_key fallback
                    -- Приоритет 2: только если домен лида вообще не имеет расхода в T_SPEND
                    SELECT
                        feed_key,
                        min(feed_name)    AS feed_name,
                        min(nullif(feed_url, ''))     AS feed_url,
                        min(nullif(feed_url_key, '')) AS feed_url_key,
                        min(feed_id)      AS feed_id
                    FROM public.{T_SPEND}
                    WHERE feed_name IS NOT NULL
                    GROUP BY feed_key
                )
                SELECT
                    coalesce(s.dk2, l.dk2)             AS dk2,
                    coalesce(s.date, l.date)            AS date,
                    s.login_key,
                    coalesce(s.domain, l.domain)        AS domain,
                    s.campaign_id,
                    s.campaign_name,
                    -- FEED_ADGROUP_NULLCOL_2026-06-29: grein date|domain|feed_key не несёт
                    -- adgroup-грануляции; колонки восстановлены как NULL для PBI-совместимости
                    -- (старый build.py содержал adgroup_id BIGINT + adgroup_name TEXT)
                    NULL::bigint AS adgroup_id,
                    NULL::text   AS adgroup_name,
                    coalesce(s.feed_id,      mdf.feed_id,      mf.feed_id)      AS feed_id,
                    coalesce(s.feed_name,    mdf.feed_name,    mf.feed_name)    AS feed_name,
                    coalesce(s.feed_url,     mdf.feed_url,     mf.feed_url)     AS feed_url,
                    coalesce(s.feed_url_key, mdf.feed_url_key, mf.feed_url_key) AS feed_url_key,
                    coalesce(s.feed_key, l.feed_key)    AS feed_key,
                    s.is_tp67,
                    coalesce(s.impressions, 0)          AS impressions,
                    coalesce(s.clicks, 0)               AS clicks,
                    coalesce(s.total_cost, 0)           AS total_cost,
                    coalesce(s.goal_all_forms, 0)           AS goal_all_forms,
                    coalesce(s.goal_crm_order_created, 0)   AS goal_crm_order_created,
                    coalesce(s.goal_crm_order_paid, 0)      AS goal_crm_order_paid,
                    coalesce(s.goal_crm_spam_order, 0)      AS goal_crm_spam_order,
                    coalesce(s.goal_crm_order_canceled, 0)  AS goal_crm_order_canceled,
                    coalesce(l.attributed_leads, 0)     AS attributed_leads,
                    coalesce(l.kol_vo_zayavok, 0)       AS kol_vo_zayavok,
                    coalesce(l.korr, 0)                 AS korr,
                    coalesce(l.kval, 0)                 AS kval,
                    coalesce(l.priezd, 0)               AS priezd,
                    coalesce(l.prodazhi, 0)             AS prodazhi,
                    coalesce(l.nekorr, 0)               AS nekorr,
                    coalesce(l.ne_otvechaet, 0)         AS ne_otvechaet,
                    coalesce(l.filtr, 0)                AS filtr,
                    coalesce(l.nedozvon, 0)             AS nedozvon,
                    coalesce(l.priedet, 0)              AS priedet,
                    l.dohod_do_kredita,
                    l.dobro,
                    now() AS generated_at
                FROM spend_agg s
                FULL OUTER JOIN leads_agg l ON l.dk2 = s.dk2
                LEFT JOIN meta_by_domain_feed mdf
                       ON mdf.domain   = coalesce(s.domain,   l.domain)
                      AND mdf.feed_key = coalesce(s.feed_key, l.feed_key)
                LEFT JOIN meta_by_feed mf
                       ON mf.feed_key  = coalesce(s.feed_key, l.feed_key);

                -- INDEX_AUDIT_2026-06-27: удалены мёртвые (idx_scan=0): idx_{{T_FACT}}_key, idx_{{T_FACT}}_feed.
                CREATE INDEX idx_{T_FACT}_date ON public.{T_FACT} (date);
                """,
            )

            cur.execute(
                f"""
                SELECT
                    (SELECT count(*) FROM public.{T_SPEND}) AS spend_rows,
                    (SELECT count(*) FROM public.{T_LEADS}) AS lead_rows,
                    (SELECT count(*) FROM public.{T_FACT}) AS fact_rows,
                    (SELECT coalesce(sum(attributed_leads), 0) FROM public.{T_FACT}) AS attributed_leads,
                    (SELECT coalesce(sum(unmatched_fid_leads), 0) FROM public.{T_QUALITY}) AS unmatched_fid_leads
                """
            )
            logger.info("direct feed keyed funnel: %s", cur.fetchone())

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
