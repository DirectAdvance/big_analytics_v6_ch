"""
big analytics_v5/crop_targeting/load_api_leads.py

Читает crop_targeting_api_telegain (сырые данные), применяет трансформации,
добавляет метрики лидов из local_leads_all.

Трансформации:
  - utm_content (DDMMYYYY) → "Date" DATE
  - user_price * 1.22 * 1.30 → total_cost NUMERIC
  - channel_link → "CampaignName"
  - domain → lookup салон/город из local_gsheet_sites
  - channel_link URL pattern → источник (telegram/instagram/VK/TikTok)
  - убрать _tp8 из источника
  - lookup "специалист"/"статус"/"тип_сайта"/"шаблон"/"регион"/direction из local_gsheet_sites по domain

Фильтр: только записи где utm_content = 8 цифр (DDMMYYYY).
Матчинг лидов (2026-06-05): ключ = ПЯТЬ полей, все жёсткие —
    домен + utm_campaign + lpad(btrim(utm_content),8,'0') + utm_source + utm_medium.
Домен — ЖЁСТКИЙ ключ джойна (WHERE la.lead_domain = LOWER(TRIM(a.domain))).
utm_source/utm_medium нормализуются LOWER(BTRIM(..)) одинаково на обеих сторонах
(la.lead_utm_source = LOWER(BTRIM(a.utm_source)), та же для medium). Пятёрка =
полный GROUP BY-ключ агрегата лидов → матч уникален, тай-брейкер не нужен.

Создаёт crop_targeting_api_telegain_lead.

Запуск (из папки big analytics_v5/):
  python3 crop_targeting/load_api_leads.py
"""

import os
import sys

import psycopg2
from psycopg2.extras import execute_values

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import DB_DST

API_TABLE     = 'crop_targeting_api_telegain'
LEADS_TABLE   = 'local_leads_all'
SITES_TABLE   = 'local_gsheet_sites'
OUTPUT_TABLE  = 'crop_targeting_api_telegain_lead'


def _connect():
    return psycopg2.connect(**DB_DST)


def ensure_output_table(conn):
    sql = f"""
DROP TABLE IF EXISTS public.{OUTPUT_TABLE};
CREATE TABLE public.{OUTPUT_TABLE} (
    id               SERIAL PRIMARY KEY,
    "Date"           DATE,
    total_cost       NUMERIC,
    "CampaignName"   TEXT,
    domain           TEXT,
    "салон"          TEXT,
    "город"          TEXT,
    источник         TEXT,
    поставщик        TEXT,
    "специалист"     TEXT,
    "статус"         TEXT,
    "тип_сайта"      TEXT,
    "шаблон"         TEXT,
    "регион"         TEXT,
    direction        TEXT,
    kol_vo_zayavok   INTEGER,
    korr             INTEGER,
    kval             INTEGER,
    priezd           INTEGER,
    prodazhi         INTEGER,
    nekorr           INTEGER,
    ne_otvechaet     INTEGER,
    filtr            INTEGER,
    nedozvon         INTEGER,
    priedet          INTEGER,
    dohod_do_kredita INTEGER,
    dobro            INTEGER
);
"""
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


LEADS_AGG_TMP = '_tmp_leads_agg_apitelegain'


def _materialize_leads_agg(conn):
    """Материализует агрегат лидов в UNLOGGED-таблицу с индексом по ключу джойна.

    Зачем: leads_agg как CTE не индексируется, LATERAL ниже сканирует его по разу
    на каждую API-строку — на больших объёмах это seqscan агрегата всех лидов.
    Паттерн как _tmp_salon_aggs (step6) / _tmp_ag_parts_lookup (corrections):
    DROP IF EXISTS → CREATE UNLOGGED AS (агрегат) → CREATE INDEX
    (utm_campaign, lead_utm_content, lead_domain, lead_utm_source,
    lead_utm_medium) → ANALYZE. После запроса дропается (_drop_leads_agg).
    Ключ связки = домен + utm_campaign + lpad(utm_content,8,'0') + utm_source
    + utm_medium (ПЯТЬ полей, все ЖЁСТКИЕ — равенство в WHERE LATERAL).
    utm_source/utm_medium нормализуются LOWER(BTRIM(..)) на обеих сторонах.
    Пятёрка совпадает с полным GROUP BY-ключом агрегата → матч уникален.
    """
    sql = f"""
DROP TABLE IF EXISTS {LEADS_AGG_TMP};
CREATE UNLOGGED TABLE {LEADS_AGG_TMP} AS
SELECT
        l.utm_campaign,
        lpad(btrim(l.utm_content), 8, '0') AS lead_utm_content,
        LOWER(TRIM(ld.name)) AS lead_domain,
        LOWER(BTRIM(l.utm_source)) AS lead_utm_source,
        LOWER(BTRIM(l.utm_medium)) AS lead_utm_medium,
        SUM(CASE WHEN l.status IS NOT NULL AND l.status != '' THEN 1 ELSE 0 END) AS kol_vo_zayavok,
        SUM(CASE WHEN l.status IN (
            'Новый','В салоне','Купил','На рассмотрении','В салоне не отмечен','Отказ',
            'Не отвечает','Приедет','В работе','Фильтр','Недозвон','Приехал',
            'Уточнить по дате','Перезвонить','Отказ клиента','Продажа за наличные',
            'Продажа в кредит','Соскок','Консультация','Отказ по банкам','Одобрен банк',
            'Одобрено банк','Новая','Заполнить','Новая: Не отвечает','Т. Кредит',
            'А. Кредит','Одобрить','Одобрен','Отложенный','В работе - odobrit',
            'Перезвонить срочно','Одобренные','Оформленные','Одобрение','Дошел в КО'
        ) THEN 1 ELSE 0 END) AS korr,
        SUM(CASE WHEN l.status IN (
            'В салоне','В салоне не отмечен','Купил','Приехал','Соскок','Консультация',
            'Отказ по банкам','Одобрен банк','Продажа за наличные','Продажа в кредит',
            'Одобрить','Одобрен','На рассмотрении','Т. Кредит','А. Кредит',
            'Одобренные','Оформленные','Одобрение','Дошел в КО'
        ) THEN 1 ELSE 0 END) AS priezd,
        SUM(CASE WHEN l.status IN (
            'Купил','Продажа за наличные','Продажа в кредит',
            'Т. Кредит','А. Кредит','Оформленные','COMPLETED','Продажа'
        ) THEN 1 ELSE 0 END) AS prodazhi,
        SUM(CASE WHEN l.status IN (
            'Некорректные данные','Корзина','Повтор','Нет данных','Дубль',
            '***','Спам','Хлам','Отбракованные','Общие вопросы'
        ) THEN 1 ELSE 0 END) AS nekorr,
        SUM(CASE WHEN l.status IN ('Не отвечает','Новая: Не отвечает') THEN 1 ELSE 0 END) AS ne_otvechaet,
        SUM(CASE WHEN l.status = 'Фильтр'   THEN 1 ELSE 0 END) AS filtr,
        SUM(CASE WHEN l.status = 'Недозвон' THEN 1 ELSE 0 END) AS nedozvon,
        SUM(CASE WHEN l.status = 'Приедет'  THEN 1 ELSE 0 END) AS priedet
    FROM public.{LEADS_TABLE} l
    JOIN public.local_domains ld ON ld.id = l.domain_id
    WHERE l.utm_campaign IS NOT NULL AND l.utm_campaign != ''
    GROUP BY l.utm_campaign, lpad(btrim(l.utm_content), 8, '0'), LOWER(TRIM(ld.name)),
             LOWER(BTRIM(l.utm_source)), LOWER(BTRIM(l.utm_medium));
CREATE INDEX ON {LEADS_AGG_TMP}
    (utm_campaign, lead_utm_content, lead_domain, lead_utm_source, lead_utm_medium);
ANALYZE {LEADS_AGG_TMP};
"""
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


def _drop_leads_agg(conn):
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {LEADS_AGG_TMP}")
    conn.commit()


def run_query(conn):
    # leads_agg вынесен в индексированную UNLOGGED-таблицу (см. _materialize_leads_agg)
    # — иначе LATERAL сканировал бы неиндексируемый CTE на каждую строку.
    _materialize_leads_agg(conn)
    sql = f"""
WITH api_dedup AS (
    -- API иногда отдаёт одинаковые размещения с разными id. Для воронки это одна
    -- и та же бизнес-строка: одинаковые UTM/домен/канал/стоимость, значит лиды
    -- и продажи должны матчиться к ней один раз.
    SELECT DISTINCT
        a.utm_content,
        a.user_price,
        a.channel_link,
        a.domain,
        a.utm_source,
        a.utm_medium,
        a.utm_campaign
    FROM public.{API_TABLE} a
    WHERE a.utm_content ~ '^[0-9]{{8}}$'
      AND a.utm_campaign IS NOT NULL
)
SELECT
    TO_DATE(a.utm_content, 'DDMMYYYY')                         AS "Date",
    ROUND(a.user_price * 1.22 * 1.30, 2)                       AS total_cost,
    a.channel_link                                             AS "CampaignName",
    LOWER(TRIM(a.domain))                                      AS domain,
    gs.salon                                                   AS "салон",
    gs.city                                                    AS "город",
    RTRIM(
        CASE
            WHEN a.channel_link LIKE 't.me/%' THEN 'telegram'
            WHEN a.channel_link LIKE 'instagram.com/%' THEN 'instagram'
            WHEN a.channel_link LIKE 'vk.com/%' THEN 'VK'
            WHEN a.channel_link LIKE 'tiktok.com/%' THEN 'TikTok'
            ELSE CASE WHEN LOWER(a.utm_source) = 'max' THEN 'Max' ELSE a.utm_source END
        END,
        '_tp8'
    )                                                          AS источник,
    'Telega IN'::TEXT                                          AS поставщик,
    gs.directologist                                           AS "специалист",
    gs.status                                                  AS "статус",
    gs.site_type                                               AS "тип_сайта",
    gs.template                                                AS "шаблон",
    gs.region                                                  AS "регион",
    'Авто'::TEXT                                               AS direction,
    COALESCE(l.kol_vo_zayavok, 0)                             AS kol_vo_zayavok,
    COALESCE(l.korr, 0)                                       AS korr,
    COALESCE(l.korr, 0)
        - COALESCE(l.ne_otvechaet, 0)
        - COALESCE(l.filtr, 0)
        - COALESCE(l.nedozvon, 0)                             AS kval,
    COALESCE(l.priezd, 0)                                     AS priezd,
    COALESCE(l.prodazhi, 0)                                   AS prodazhi,
    COALESCE(l.nekorr, 0)                                     AS nekorr,
    COALESCE(l.ne_otvechaet, 0)                              AS ne_otvechaet,
    COALESCE(l.filtr, 0)                                      AS filtr,
    COALESCE(l.nedozvon, 0)                                   AS nedozvon,
    COALESCE(l.priedet, 0)                                    AS priedet,
    NULL::INTEGER                                              AS dohod_do_kredita,
    NULL::INTEGER                                              AS dobro
FROM api_dedup a
LEFT JOIN public.{SITES_TABLE} gs
    ON LOWER(TRIM(a.domain)) = LOWER(TRIM(gs.domain))
-- Ключ связки заказ<->лиды = домен + utm_campaign + lpad(utm_content,8,'0')
-- + utm_source + utm_medium (ПЯТЬ полей, все ЖЁСТКИЕ).
-- Домен — равенство lead_domain = LOWER(TRIM(a.domain)).
-- utm_source/utm_medium нормализуются LOWER(BTRIM(..)) на обеих сторонах.
-- Пятёрка = полный GROUP BY-ключ агрегата → матч уникален.
LEFT JOIN LATERAL (
    SELECT la.*
    FROM {LEADS_AGG_TMP} la
    WHERE la.utm_campaign     = a.utm_campaign
      AND la.lead_utm_content = lpad(btrim(a.utm_content), 8, '0')
      AND la.lead_domain      = LOWER(TRIM(a.domain))
      AND la.lead_utm_source  = LOWER(BTRIM(a.utm_source))
      AND la.lead_utm_medium  = LOWER(BTRIM(a.utm_medium))
    LIMIT 1
) l ON TRUE
"""
    with conn.cursor() as cur:
        cur.execute(sql)
        rows      = cur.fetchall()
        col_names = [desc[0] for desc in cur.description]
    _drop_leads_agg(conn)
    return rows, col_names


def main():
    conn = _connect()
    try:
        print(f'Читаем {API_TABLE}...')
        rows, col_names = run_query(conn)
        print(f'  Строк API: {len(rows)}')

        print(f'Создаём таблицу {OUTPUT_TABLE}...')
        ensure_output_table(conn)

        print('Вставляем данные...')
        insert_sql = f"""
            INSERT INTO public.{OUTPUT_TABLE} (
                "Date", total_cost, "CampaignName", domain, "салон",
                "город", источник, поставщик, "специалист", "статус",
                "тип_сайта", "шаблон", "регион", direction,
                kol_vo_zayavok, korr, kval, priezd, prodazhi,
                nekorr, ne_otvechaet, filtr, nedozvon, priedet,
                dohod_do_kredita, dobro
            ) VALUES %s
        """
        with conn.cursor() as cur:
            execute_values(cur, insert_sql, rows)
        conn.commit()
        print(f'OK: {len(rows)} строк загружено в {OUTPUT_TABLE}')
    finally:
        conn.close()


if __name__ == '__main__':
    main()
