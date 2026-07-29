"""
sales_attribution/build.py

Создаёт public.analytics_sales_attribution для Power BI.

Гранулярность:
  row_type='sale'                 — 1 строка = 1 продажа из sale_xlsx
  row_type='cost_unattributed'    — (CampaignId, Date) с расходом без продажи в эту дату
  row_type='cost_no_campaign_id'  — расходы без CampaignId (calls/seo/crop/...)

Фильтры:
  sale_xlsx: utm_medium IS NOT NULL/'' AND utm_source NOT IN ('victory-corp','leadgen')
  big_analytics_full: "Название crm" = 'Фаиг'

Аллокация: cost_camp_date / N_sales_camp_date по точной паре (camp_id, date).
Сумма cost_alloc по таблице = SUM(big_analytics_full.total_cost) для Фаиг.

Маржа: 200 000 ₽ за каждую продажу (row_type='sale'). Для cost_*-строк маржа = 0.

Запуск:
  cd big_analytics_v5 && ~/venv/bin/python3 sales_attribution/build.py
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2
from config.settings import DB_DST

TARGET_TABLE    = 'analytics_sales_attribution'
MARZHA_PER_SALE = 200000


DDL = f"""
DROP TABLE IF EXISTS public.{TARGET_TABLE};
CREATE TABLE public.{TARGET_TABLE} (
    row_type             TEXT NOT NULL,
    sozdana_at           TIMESTAMP,
    sozdana_date         DATE,
    sale_month           DATE,
    week_start           DATE,
    visit_date           DATE,
    sale_status          TEXT,
    deal_type            TEXT,
    marka                TEXT,
    model                TEXT,
    state                TEXT,
    fio                  TEXT,
    phone_mask           TEXT,
    yclid                TEXT,
    phone_region         TEXT,
    utm_source           TEXT,
    utm_medium           TEXT,
    utm_campaign         TEXT,
    utm_term             TEXT,
    utm_content          TEXT,
    is_organic           BOOLEAN,
    camp_id              BIGINT,
    adgroup_id           BIGINT,
    camp_date            DATE,
    camp_name            TEXT,
    domain               TEXT,
    salon                TEXT,
    specialist           TEXT,
    city                 TEXT,
    template             TEXT,
    region               TEXT,
    tp                   TEXT,
    ag_part1             TEXT,
    source_table         TEXT,
    cost_camp_date_total NUMERIC,
    zayavok_camp_date    BIGINT,
    korr_camp_date       BIGINT,
    priezd_camp_date     BIGINT,
    prodazhi_camp_date   BIGINT,
    n_sales_camp_date    INTEGER,
    cost_alloc           NUMERIC,
    marzha               INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX ON public.{TARGET_TABLE} (sozdana_date);
CREATE INDEX ON public.{TARGET_TABLE} (camp_date);
CREATE INDEX ON public.{TARGET_TABLE} (camp_id);
CREATE INDEX ON public.{TARGET_TABLE} (salon);
CREATE INDEX ON public.{TARGET_TABLE} (row_type);
CREATE INDEX ON public.{TARGET_TABLE} (marka);
"""


INSERT_SQL = f"""
WITH sale AS (
    SELECT
        NULLIF(TRIM(s."Создана"), '')::timestamp                        AS sozdana_at,
        NULLIF(TRIM(s."Создана"), '')::timestamp::date                  AS sozdana_date,
        NULLIF(TRIM(s.visit_date), '')::timestamp::date                 AS visit_date,
        NULLIF(TRIM(s."Статус"), '')                                    AS sale_status,
        NULLIF(TRIM(s."Тип сделки"), '')                                AS deal_type,
        COALESCE(NULLIF(TRIM(s."Марка (купил)"), ''),    'авто не указан') AS marka,
        COALESCE(NULLIF(TRIM(s."Модель (купил)"), ''),   'авто не указан') AS model,
        COALESCE(NULLIF(TRIM(s."Состояние (купил)"), ''),'авто не указан') AS state,
        NULLIF(TRIM(s."ФИО"), '')                                       AS fio,
        NULLIF(TRIM(s.телефон_маска), '')                               AS phone_mask,
        NULLIF(TRIM(s.yclid), '')                                       AS yclid,
        NULLIF(TRIM(s."Регион по номеру телефона"), '')                 AS phone_region,
        s.utm_source, s.utm_medium, s.utm_campaign, s.utm_term, s.utm_content,
        (s.utm_source = 'seo' AND s.utm_medium = 'organic')             AS is_organic,
        CASE WHEN s.utm_campaign ~ '^[0-9]+\\|'
             THEN SPLIT_PART(s.utm_campaign, '|', 1)::BIGINT
        END                                                              AS camp_id,
        CASE WHEN s.utm_content ~ 'g:[0-9]+'
             THEN (REGEXP_MATCH(s.utm_content, 'g:([0-9]+)'))[1]::BIGINT
        END                                                              AS adgroup_id
    FROM public.sale_xlsx s
    WHERE s.utm_medium IS NOT NULL AND s.utm_medium <> ''
      AND COALESCE(s.utm_source, '') NOT IN ('victory-corp', 'leadgen')
),
camp_date_agg AS (
    SELECT
        "CampaignId"                AS camp_id,
        "Date"::date                AS camp_date,
        MAX(domain)                 AS domain,
        MAX("салон")                AS salon,
        MAX("специалист")           AS specialist,
        MAX("город")                AS city,
        MAX("шаблон")               AS template,
        MAX("регион")               AS region,
        MAX(tp)                     AS tp,
        MAX(ag_part1)               AS ag_part1,
        MAX(_source_table)          AS source_table,
        MAX("CampaignName")         AS camp_name,
        SUM(total_cost)             AS cost_total,
        SUM(kol_vo_zayavok)         AS zayavok,
        SUM(korr)                   AS korr,
        SUM(priezd)                 AS priezd,
        SUM(prodazhi)               AS prodazhi
    FROM public.big_analytics_full
    WHERE "Название crm" = 'Фаиг'
      AND "CampaignId" IS NOT NULL
    GROUP BY "CampaignId", "Date"::date
),
camp_date_no_id AS (
    SELECT
        "Date"::date                AS camp_date,
        domain,
        _source_table               AS source_table,
        MAX("салон")                AS salon,
        MAX("специалист")           AS specialist,
        MAX("город")                AS city,
        MAX("шаблон")               AS template,
        MAX("регион")               AS region,
        SUM(total_cost)             AS cost_total,
        SUM(kol_vo_zayavok)         AS zayavok,
        SUM(korr)                   AS korr,
        SUM(priezd)                 AS priezd,
        SUM(prodazhi)               AS prodazhi
    FROM public.big_analytics_full
    WHERE "Название crm" = 'Фаиг'
      AND "CampaignId" IS NULL
    GROUP BY "Date"::date, domain, _source_table
),
sales_per_camp_date AS (
    SELECT camp_id, sozdana_date, COUNT(*) AS n_sales
    FROM sale
    WHERE camp_id IS NOT NULL
    GROUP BY 1, 2
)
INSERT INTO public.{TARGET_TABLE}

-- ─── 1: SALE ROWS (1 строка = 1 продажа) ───
SELECT
    'sale'::TEXT,
    s.sozdana_at, s.sozdana_date,
    DATE_TRUNC('month', s.sozdana_date)::date,
    DATE_TRUNC('week',  s.sozdana_date)::date,
    s.visit_date, s.sale_status, s.deal_type,
    s.marka, s.model, s.state,
    s.fio, s.phone_mask, s.yclid, s.phone_region,
    s.utm_source, s.utm_medium, s.utm_campaign, s.utm_term, s.utm_content,
    s.is_organic,
    s.camp_id, s.adgroup_id,
    cm.camp_date, cm.camp_name,
    cm.domain, cm.salon, cm.specialist, cm.city, cm.template, cm.region,
    cm.tp, cm.ag_part1, cm.source_table,
    cm.cost_total, cm.zayavok, cm.korr, cm.priezd, cm.prodazhi,
    COALESCE(spc.n_sales, 0),
    CASE WHEN spc.n_sales > 0 AND cm.cost_total IS NOT NULL
         THEN cm.cost_total / spc.n_sales
         ELSE 0
    END,
    {MARZHA_PER_SALE}::INTEGER
FROM sale s
LEFT JOIN camp_date_agg        cm  ON cm.camp_id      = s.camp_id
                                  AND cm.camp_date    = s.sozdana_date
LEFT JOIN sales_per_camp_date  spc ON spc.camp_id     = s.camp_id
                                  AND spc.sozdana_date = s.sozdana_date

UNION ALL

-- ─── 2: COST_UNATTRIBUTED — (CampaignId, Date) с расходом, но без продажи в эту дату ───
SELECT
    'cost_unattributed'::TEXT,
    NULL::TIMESTAMP, NULL::DATE,
    DATE_TRUNC('month', cm.camp_date)::date,
    DATE_TRUNC('week',  cm.camp_date)::date,
    NULL::DATE, NULL, NULL,
    NULL, NULL, NULL,
    NULL, NULL, NULL, NULL,
    NULL, NULL, NULL, NULL, NULL,
    FALSE,
    cm.camp_id, NULL::BIGINT,
    cm.camp_date, cm.camp_name,
    cm.domain, cm.salon, cm.specialist, cm.city, cm.template, cm.region,
    cm.tp, cm.ag_part1, cm.source_table,
    cm.cost_total, cm.zayavok, cm.korr, cm.priezd, cm.prodazhi,
    0,
    cm.cost_total,
    0::INTEGER
FROM camp_date_agg cm
WHERE NOT EXISTS (
    SELECT 1 FROM sale s
    WHERE s.camp_id = cm.camp_id AND s.sozdana_date = cm.camp_date
)

UNION ALL

-- ─── 3: COST_NO_CAMPAIGN_ID — расходы без CampaignId (calls, seo, crop, ...) ───
SELECT
    'cost_no_campaign_id'::TEXT,
    NULL::TIMESTAMP, NULL::DATE,
    DATE_TRUNC('month', cm.camp_date)::date,
    DATE_TRUNC('week',  cm.camp_date)::date,
    NULL::DATE, NULL, NULL,
    NULL, NULL, NULL,
    NULL, NULL, NULL, NULL,
    NULL, NULL, NULL, NULL, NULL,
    FALSE,
    NULL::BIGINT, NULL::BIGINT,
    cm.camp_date, NULL,
    cm.domain, cm.salon, cm.specialist, cm.city, cm.template, cm.region,
    NULL, NULL, cm.source_table,
    cm.cost_total, cm.zayavok, cm.korr, cm.priezd, cm.prodazhi,
    0,
    cm.cost_total,
    0::INTEGER
FROM camp_date_no_id cm;
"""


def main() -> None:
    conn = psycopg2.connect(**DB_DST)
    try:
        t0 = time.perf_counter()
        with conn.cursor() as cur:
            print(f'1/3  Создаём таблицу public.{TARGET_TABLE} ...')
            cur.execute(DDL)

            print('2/3  Заполняем (это может занять минуту) ...')
            cur.execute(INSERT_SQL)

            cur.execute(f'SELECT COUNT(*) FROM public.{TARGET_TABLE}')
            n_total = cur.fetchone()[0]

            cur.execute(f"""
                SELECT row_type,
                       COUNT(*),
                       ROUND(COALESCE(SUM(cost_alloc), 0), 2),
                       COALESCE(SUM(marzha), 0)
                FROM public.{TARGET_TABLE}
                GROUP BY row_type
                ORDER BY 2 DESC
            """)
            stats = cur.fetchall()
        conn.commit()

        elapsed = time.perf_counter() - t0
        print(f'3/3  Готово: {n_total:,} строк за {elapsed:.1f} сек')
        print()
        print('  row_type             |     count |       cost_alloc |        marzha')
        print('  ' + '─' * 70)
        for r in stats:
            print(f'  {r[0]:<20} | {r[1]:>9,} | {r[2]:>16,.2f} | {r[3]:>13,}')
    finally:
        conn.close()


if __name__ == '__main__':
    main()
