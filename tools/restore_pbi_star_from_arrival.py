#!/usr/bin/env python3
"""
Restore the local Power BI star-facing objects from big_analytics_full_arrival.

This is a narrow recovery tool. It does not run the pipeline and does not fetch
new data; it only rebuilds the objects used by the Power BI model from the
already materialized durable arrival table.
"""

from __future__ import annotations

import os
import sys
import argparse
from datetime import datetime

import psycopg2

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from config.settings import DB_DST  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--desktop-local",
        action="store_true",
        help="Use the local Power BI Desktop PostgreSQL source on 10.211.55.2/ad_analytics_bi.",
    )
    return parser.parse_args()


def db_params(args: argparse.Namespace) -> dict:
    if args.desktop_local:
        return {
            "host": "10.211.55.2",
            "database": "ad_analytics_bi",
            "user": "postgres",
            "password": "",
        }
    return DB_DST


def qcount(cur, rel: str) -> int:
    if rel.startswith("Dim_"):
        cur.execute(f'select count(*) from public."{rel}"')
    else:
        cur.execute(f"select count(*) from public.{rel}")
    return int(cur.fetchone()[0])


def main() -> int:
    args = parse_args()
    db = db_params(args)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    conn = psycopg2.connect(**db, connect_timeout=10)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        arrival_rows = qcount(cur, "big_analytics_full_arrival")
        if arrival_rows == 0:
            raise RuntimeError("public.big_analytics_full_arrival is empty; refusing to restore from empty source")

        for rel in ("fact_big_analytics", "Dim_Date", "Dim_AdGroup", "Dim_Adjustment"):
            bak = f"{rel}_bak_pbi_restore_{stamp}"
            cur.execute(f'DROP TABLE IF EXISTS public."{bak}"')
            cur.execute(f'CREATE TABLE public."{bak}" AS TABLE public."{rel}"')

        cur.execute("TRUNCATE TABLE public.fact_big_analytics")
        cur.execute('TRUNCATE TABLE public."Dim_Date" CASCADE')
        cur.execute('TRUNCATE TABLE public."Dim_AdGroup" CASCADE')
        cur.execute('TRUNCATE TABLE public."Dim_Adjustment" CASCADE')

        cur.execute("""
            INSERT INTO public."Dim_Date" ("Date", week_start, "День недели", year, month, year_month, day)
            SELECT d::date AS "Date",
                   date_trunc('week', d)::date AS week_start,
                   CASE extract(isodow from d)::int
                       WHEN 1 THEN 'Понедельник' WHEN 2 THEN 'Вторник' WHEN 3 THEN 'Среда'
                       WHEN 4 THEN 'Четверг' WHEN 5 THEN 'Пятница' WHEN 6 THEN 'Суббота'
                       WHEN 7 THEN 'Воскресенье' END AS "День недели",
                   extract(year from d)::smallint AS year,
                   extract(month from d)::smallint AS month,
                   to_char(d, 'YYYY-MM') AS year_month,
                   extract(day from d)::smallint AS day
            FROM (
                SELECT generate_series(min("Date"), max("Date"), interval '1 day')::date AS d
                FROM public.big_analytics_full_arrival
                WHERE "Date" IS NOT NULL
            ) s
        """)

        cur.execute("""
            INSERT INTO public."Dim_AdGroup" (
                "AdGroupId", "AdGroupName", adgroup_code, "номер группы | название группы",
                ag_part1, ag_part2, ag_part3, ag_part4, ag_part5, ag_part6, ag_part7,
                ag_part1_name, "неверный_кодер_new", "parent_CampaignId"
            )
            SELECT DISTINCT ON ("AdGroupId")
                "AdGroupId", "AdGroupName", adgroup_code, "номер группы | название группы",
                ag_part1, ag_part2, ag_part3, ag_part4, ag_part5, ag_part6, ag_part7,
                ag_part1_name, "неверный_кодер_new", "CampaignId"
            FROM public.big_analytics_full_arrival
            WHERE "AdGroupId" IS NOT NULL
            ORDER BY "AdGroupId", "Date" DESC NULLS LAST
        """)

        cur.execute("""
            INSERT INTO public."Dim_AdGroup" (
                "AdGroupId", "AdGroupName", adgroup_code, "номер группы | название группы",
                ag_part1, ag_part2, ag_part3, ag_part4, ag_part5, ag_part6, ag_part7,
                ag_part1_name, "неверный_кодер_new", "parent_CampaignId"
            )
            SELECT DISTINCT ON (s.ad_group_id)
                s.ad_group_id,
                NULLIF(s.adgroup_brand, '') AS "AdGroupName",
                s.adgroup_code,
                CASE
                    WHEN s.ad_group_id IS NULL THEN NULL
                    ELSE concat_ws(' | ', s.ad_group_id::text, NULLIF(s.adgroup_brand, ''))
                END AS "номер группы | название группы",
                NULL::text, NULL::text, NULL::text, NULL::text, NULL::text, NULL::text, NULL::text,
                NULLIF(s.adgroup_brand, '') AS ag_part1_name,
                NULL::text,
                s.campaign_id
            FROM public.fact_adformat_spend s
            LEFT JOIN public."Dim_AdGroup" da ON da."AdGroupId" = s.ad_group_id
            WHERE s.ad_group_id IS NOT NULL AND da."AdGroupId" IS NULL
            ORDER BY s.ad_group_id, s.date DESC NULLS LAST
        """)

        cur.execute("""
            INSERT INTO public."Dim_Campaign" (
                "CampaignId", "CampaignName", account_login, "статус_кампании",
                "специалист", manager_login, campaign_status, payment_model,
                "номер кампании | название кампании"
            )
            SELECT DISTINCT ON (s.campaign_id)
                s.campaign_id,
                s.campaign_name,
                s."логин",
                s."статус",
                s."директолог",
                s."директолог",
                s."статус",
                NULL::text,
                CASE
                    WHEN s.campaign_id IS NULL THEN NULL
                    ELSE concat_ws(' | ', s.campaign_id::text, NULLIF(s.campaign_name, ''))
                END AS "номер кампании | название кампании"
            FROM public.fact_adformat_spend s
            WHERE s.campaign_id IS NOT NULL
            ORDER BY s.campaign_id, s.date DESC NULLS LAST
            ON CONFLICT ("CampaignId") DO NOTHING
        """)

        cur.execute("""
            INSERT INTO public."Dim_Adjustment" ("RlAdjustmentId", "RlAdjustmentId_total")
            SELECT DISTINCT ON ("RlAdjustmentId")
                "RlAdjustmentId", COALESCE("RlAdjustmentId_total", '')
            FROM public.big_analytics_full_arrival
            WHERE "RlAdjustmentId" IS NOT NULL
            ORDER BY "RlAdjustmentId", "Date" DESC NULLS LAST
        """)

        cur.execute("""
            INSERT INTO public.fact_big_analytics (
                "CampaignId", "AdGroupId", "RlAdjustmentId", priezd_arrival_date, prodazhi_arrival_date,
                dohod_do_kredita, dobro, total_cost, kol_vo_zayavok, korr, kval, priezd, prodazhi,
                "Clicks", "Impressions", nekorr, ne_otvechaet, nedozvon, filtr, priedet,
                "План заявки", "План приезда", "Date", domain, "атрибуция", _source_table,
                tp, "источник", "AdNetworkType", "аккаунт|сайт", campaign_code, "поставщик",
                "Device", fid, cpc_cpa, "направление", site_quiz, "марки авто", "специалист",
                "тип_сайта", "статус", "салон", "шаблон", "id_салона", "город", "регион",
                "проджект", "менеджер", "Название crm", "тип_заявки", manager_login
            )
            SELECT
                "CampaignId", "AdGroupId", "RlAdjustmentId",
                COALESCE(priezd_arrival_date, 0), COALESCE(prodazhi_arrival_date, 0),
                COALESCE(dohod_do_kredita, 0), COALESCE(dobro, 0), COALESCE(total_cost, 0),
                COALESCE(kol_vo_zayavok, 0), COALESCE(korr, 0), COALESCE(kval, 0),
                COALESCE(priezd, 0), COALESCE(prodazhi, 0),
                COALESCE("Clicks", 0)::integer, COALESCE("Impressions", 0)::integer,
                COALESCE(nekorr, 0)::integer, COALESCE(ne_otvechaet, 0)::integer,
                COALESCE(nedozvon, 0)::integer, COALESCE(filtr, 0)::integer, COALESCE(priedet, 0)::integer,
                "План заявки", "План приезда", "Date", domain, 'По дате заявки'::text, _source_table,
                tp, "источник", "AdNetworkType", "аккаунт|сайт", campaign_code, "поставщик",
                "Device", fid, cpc_cpa, "направление", site_quiz, "марки авто", "специалист",
                "тип_сайта", "статус", "салон", "шаблон", "id_салона", "город", "регион",
                "проджект", "менеджер", "Название crm", "тип_заявки", manager_login
            FROM public.big_analytics_full_arrival
        """)

        cur.execute("""
            INSERT INTO public.fact_big_analytics (
                "CampaignId", "AdGroupId", "RlAdjustmentId", priezd_arrival_date, prodazhi_arrival_date,
                dohod_do_kredita, dobro, total_cost, kol_vo_zayavok, korr, kval, priezd, prodazhi,
                "Clicks", "Impressions", nekorr, ne_otvechaet, nedozvon, filtr, priedet,
                "План заявки", "План приезда", "Date", domain, "атрибуция", _source_table,
                tp, "источник", "AdNetworkType", "аккаунт|сайт", campaign_code, "поставщик",
                "Device", fid, cpc_cpa, "направление", site_quiz, "марки авто", "специалист",
                "тип_сайта", "статус", "салон", "шаблон", "id_салона", "город", "регион",
                "проджект", "менеджер", "Название crm", "тип_заявки", manager_login
            )
            SELECT
                s.campaign_id, s.ad_group_id, NULL::bigint,
                0::bigint, 0::bigint,
                0::bigint, 0::bigint,
                COALESCE(s.cost, 0),
                0::numeric, 0::numeric, 0::numeric, 0::numeric, 0::numeric,
                COALESCE(s.clicks, 0)::integer,
                COALESCE(s.impressions, 0)::integer,
                0::integer, 0::integer, 0::integer, 0::integer, 0::integer,
                NULL::integer, NULL::integer,
                s.date,
                s.domain,
                'По дате заявки'::text,
                'fact_adformat_spend'::text,
                s.tp,
                'Я.Директ'::text,
                s.ad_network_type,
                s."логин",
                s.campaign_code,
                'Яндекс.Директ'::text,
                NULL::text,
                NULL::text,
                s.cpc_cpa,
                s."направление",
                s.site_quiz,
                NULL::text,
                s."директолог",
                s."тип_сайта",
                s."статус",
                s."салон",
                s."шаблон",
                NULL::text,
                s."город",
                s."регион",
                NULL::text,
                s.project_manager,
                NULL::text,
                NULL::text,
                s."директолог"
            FROM public.fact_adformat_spend s
            WHERE s.date IS NOT NULL
        """)

        cur.execute("DROP VIEW IF EXISTS public.pbi_big_analytics_full")
        cur.execute("""
            CREATE VIEW public.pbi_big_analytics_full AS
            SELECT
                f."Date", f."CampaignId", f."AdNetworkType", f."Device", f.total_cost,
                f.campaign_code, f.tp, f.cpc_cpa, f.kol_vo_zayavok AS "Обращения",
                f.korr, f.kval, f.priezd, f.prodazhi, f.nekorr, f.ne_otvechaet,
                f.filtr, f.nedozvon, f."RlAdjustmentId",
                COALESCE(adj."RlAdjustmentId_total", '') AS "RlAdjustmentId_total",
                f.fid, f."Clicks", f."источник", f."План заявки", f."План приезда",
                f."аккаунт|сайт", f."поставщик", f.domain AS "домен", f.priedet,
                f.dohod_do_kredita, f.dobro, f."атрибуция", f."AdGroupId",
                f."направление", f.site_quiz, f."марки авто", f."специалист",
                f."тип_сайта", f."статус", f."салон", f."шаблон", f."id_салона",
                f."город", f."регион", f."проджект", f."менеджер", f."Название crm",
                f."тип_заявки", f.manager_login,
                COALESCE(dc."CampaignName", '') AS "CampaignName",
                COALESCE(da."AdGroupName", '') AS "AdGroupName",
                COALESCE(dc."account_login", '') AS "account_login",
                COALESCE(da."adgroup_code", '') AS "adgroup_code",
                COALESCE(da."ag_part1", '') AS "ag_part1",
                COALESCE(da."ag_part2", '') AS "ag_part2",
                COALESCE(da."ag_part3", '') AS "ag_part3",
                COALESCE(da."ag_part4", '') AS "ag_part4",
                COALESCE(da."ag_part5", '') AS "ag_part5",
                COALESCE(da."ag_part6", '') AS "ag_part6",
                COALESCE(da."ag_part7", '') AS "ag_part7",
                COALESCE(dc."campaign_status", '') AS "campaign_status",
                COALESCE(dc."payment_model", '') AS "payment_model",
                COALESCE(da."неверный_кодер_new", '') AS "неверный_кодер_new",
                dd.week_start,
                COALESCE(dd."День недели", '') AS "День недели",
                COALESCE(da."номер группы | название группы", '') AS "номер группы | название группы",
                COALESCE(dc."номер кампании | название кампании", '') AS "номер кампании | название кампании"
            FROM public.fact_big_analytics f
            LEFT JOIN public."Dim_Campaign" dc ON f."CampaignId" = dc."CampaignId"
            LEFT JOIN public."Dim_AdGroup" da ON f."AdGroupId" = da."AdGroupId"
            LEFT JOIN public."Dim_Date" dd ON f."Date" = dd."Date"
            LEFT JOIN public."Dim_Adjustment" adj ON f."RlAdjustmentId" = adj."RlAdjustmentId"
        """)

        cur.execute("""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bi_analytic') THEN
                    GRANT SELECT ON public.pbi_big_analytics_full TO bi_analytic;
                END IF;
            END $$;
        """)

        for rel in ('public.fact_big_analytics', 'public."Dim_Date"', 'public."Dim_AdGroup"',
                    'public."Dim_Adjustment"'):
            cur.execute(f"ANALYZE {rel}")

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()

    conn = psycopg2.connect(**db, connect_timeout=10)
    cur = conn.cursor()
    for rel in ("big_analytics_full_arrival", "fact_big_analytics", "pbi_big_analytics_full",
                "Dim_Date", "Dim_Campaign", "Dim_AdGroup", "Dim_Adjustment"):
        print(f"{rel}\t{qcount(cur, rel)}")
    cur.execute('select min("Date"), max("Date") from public.pbi_big_analytics_full')
    print("pbi_date_range\t%s\t%s" % cur.fetchone())
    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
