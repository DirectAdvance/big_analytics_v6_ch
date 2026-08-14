"""
big analytics_v5/crop_targeting/load_telega_in_orders.py

Читает telega_in_orders (внешние raw данные Telega.in), применяет трансформации,
добавляет метрики лидов из local_leads_all.

Изменения 2026-05-19:
  - Фильтр: только status='complete' (исключаем cancel/progress/not_paid)
  - Расход: total_price (не price*1.22*1.30)
  - Записи без utm_campaign или нестандартной utm_content ТОЖЕ берутся (расходы не теряются)
    Дата: DDMMYYYY из utm_content, fallback -> completed_at/done_at/created_at
    Направление/источник: domain -> local_gsheet_sites lookup (как обычно)

Изменения 2026-05-19 (фикс):
  - Убран фильтр created_at >= '2026-05-01' — брались только майские заказы (92% потери)
  - leads_agg GROUP BY добавлен домен через JOIN local_domains — исключает задвоение лидов
    когда один utm_campaign куплен для нескольких доменов в одном месяце
  - JOIN leads_agg дополнен условием по домену

Изменения 2026-06-04 (ключ джойна):
  - Ключ связки заказ<->лиды = utm_campaign + lpad(btrim(utm_content),8,'0') + домен
    (utm_content = дата посева DDMMYYYY; то же значение есть в лидах).
    Раньше джойнили по utm_campaign + домен + месяц-окно ±1 — терялись пары
    из-за смещения по месяцу-окну.
  - Домен — ЖЁСТКИЙ ключ джойна (WHERE la.lead_domain = d.effective_domain).
    Домен заказа берётся из post_links (jsonb->>0 URL host) как effective_domain.
  - Сбор дефектов заказа в public.local_telega_in_orders_errors (collect_errors).

Изменения 2026-06-05 (ключ джойна = 5 полей):
  - Ключ связки заказ<->лиды РАСШИРЕН до ПЯТИ полей (все жёсткие, в WHERE LATERAL):
      domain (effective_domain из post_links)
      + utm_campaign
      + lpad(btrim(utm_content),8,'0')
      + utm_source
      + utm_medium
    Нормализация LOWER(BTRIM(...)) применяется к utm_source/utm_medium с ОБЕИХ
    сторон одинаково. Пятёрка = полный GROUP BY-ключ агрегата лидов
    (_tmp_leads_agg_telegain), поэтому матч уникален — тай-брейкер не нужен.
  - Замер на боевых (214 заказов status=complete, created_at>=2026-05-01):
    5-полевой ключ даёт 102 матча — РОВНО столько же, сколько 3-полевой (102).
    Расширение source/medium НЕ ухудшило матчинг: когда совпали
    domain+campaign+content, source/medium совпадают всегда (потеря = 0).

Создаёт crop_targeting_api_telegain_lead.
"""

import os
import sys

import psycopg2
from psycopg2.extras import execute_values

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import DB_DST

API_TABLE     = 'local_telega_in_orders'
LEADS_TABLE   = 'local_leads_all'
SITES_TABLE   = 'local_gsheet_sites'
OUTPUT_TABLE  = 'crop_targeting_api_telegain_lead'
ERRORS_TABLE  = 'local_telega_in_orders_errors'


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
    dobro            INTEGER,
    utm_campaign     TEXT          -- нужен для дедупа в load_crop_targeting_leads (Вариант A)
);
"""
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


LEADS_AGG_TMP = '_tmp_leads_agg_telegain'


def _materialize_leads_agg(conn):
    """Материализует агрегат лидов в UNLOGGED-таблицу с индексом по ключу джойна.

    Зачем: leads_agg как CTE не индексируется, а LATERAL ниже сканирует его по
    одному разу на КАЖДЫЙ заказ (1318 раз) — на проде это давало ~4.8 мин
    (seqscan агрегата всех лидов). Паттерн материализации как у _tmp_salon_aggs
    (step6) / _tmp_ag_parts_lookup (corrections): DROP IF EXISTS → CREATE UNLOGGED
    AS (агрегат) → CREATE INDEX (utm_campaign, lead_utm_content, lead_domain,
    lead_utm_source, lead_utm_medium) → ANALYZE. После основного запроса таблица
    дропается (_drop_leads_agg).

    Ключ связки заказ<->лиды = домен + utm_campaign + lpad(utm_content,8,'0')
    + utm_source + utm_medium (ПЯТЬ полей, все ЖЁСТКИЕ — равенство в WHERE
    LATERAL, не тай-брейкер). Пятёрка совпадает с полным GROUP BY-ключом
    агрегата — матч уникален. utm_source/utm_medium нормализуются LOWER(BTRIM(..))
    одинаково на обеих сторонах джойна.
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
    # leads_agg вынесен в индексированную UNLOGGED-таблицу _tmp_leads_agg_telegain
    # (см. _materialize_leads_agg) — иначе LATERAL сканировал бы неиндексируемый CTE
    # один раз на каждый заказ (~4.8 мин на проде).
    _materialize_leads_agg(conn)
    sql = f"""
WITH
-- Все complete-записи с resolved датой (utm_content DDMMYYYY или fallback на completed_at)
tio_raw AS (
    SELECT
        a.*,
        CASE
            WHEN a.utm_content ~ '^[0-9]{{8}}$' THEN TO_DATE(a.utm_content, 'DDMMYYYY')
            ELSE COALESCE(a.completed_at::date, a.done_at::date, a.created_at::date)
        END AS effective_date,
        LOWER(TRIM(
            CASE
                WHEN a.post_links IS NOT NULL AND a.post_links LIKE '[%'
                    THEN SUBSTRING((a.post_links::jsonb->>0) FROM '://([^/?]+)')
                ELSE SUBSTRING(COALESCE(a.post_link, '') FROM '://([^/?]+)')
            END
        )) AS raw_domain
    FROM public.{API_TABLE} a
    WHERE a.status = 'complete'
),
-- fallback: если raw_domain пустой или редиректор — берём домен из order_project_name
tio_dated AS (
    SELECT
        *,
        CASE
            WHEN NULLIF(raw_domain, '') IS NOT NULL
                 AND raw_domain NOT IN ('telega.io', 'max.ru', 't.me')
            THEN raw_domain
            ELSE LOWER(TRIM(SPLIT_PART(order_project_name, ' ', 1)))
        END AS effective_domain
    FROM tio_raw
),
-- TELEGAIN_DEDUP_2026-07-23: API может прислать один и тот же посев дважды с
-- разными id/order_project_name. Для воронки это одна бизнес-строка, поэтому
-- схлопываем дубли по ключу матчинга + каналу + расходу и оставляем самый
-- поздний экземпляр.
tio_dedup AS (
    SELECT DISTINCT ON (
        effective_domain,
        utm_campaign,
        lpad(btrim(utm_content), 8, '0'),
        LOWER(BTRIM(utm_source)),
        LOWER(BTRIM(utm_medium)),
        channel_link,
        total_price,
        effective_date
    )
        *
    FROM tio_dated
    ORDER BY
        effective_domain,
        utm_campaign,
        lpad(btrim(utm_content), 8, '0'),
        LOWER(BTRIM(utm_source)),
        LOWER(BTRIM(utm_medium)),
        channel_link,
        total_price,
        effective_date,
        COALESCE(completed_at, done_at, created_at) DESC,
        id DESC
)
SELECT
    d.effective_date                                           AS "Date",
    d.total_price                                              AS total_cost,
    d.channel_link                                             AS "CampaignName",
    d.effective_domain                                         AS domain,
    gs.salon                                                   AS "салон",
    gs.city                                                    AS "город",
    RTRIM(
        CASE
            WHEN d.channel_link LIKE '%t.me/%'
              OR d.channel_link LIKE '%telegram.me/%' THEN 'telegram'
            WHEN d.channel_link LIKE '%instagram.com/%' THEN 'instagram'
            WHEN d.channel_link LIKE '%vk.com/%'        THEN 'VK'
            WHEN d.channel_link LIKE '%tiktok.com/%'    THEN 'TikTok'
            WHEN d.channel_link LIKE '%max.ru/%'        THEN 'Max'
            ELSE COALESCE(
                CASE WHEN LOWER(d.utm_source) = 'max'      THEN 'Max'      END,
                CASE WHEN LOWER(d.utm_source) = 'telegram' THEN 'telegram' END,
                d.utm_source
            )
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
    NULL::INTEGER                                              AS dobro,
    d.utm_campaign                                             AS utm_campaign
FROM tio_dedup d
LEFT JOIN public.{SITES_TABLE} gs
    ON d.effective_domain = LOWER(TRIM(gs.domain))
-- Матч лидов по ключу связки = домен + utm_campaign + lpad(utm_content,8,'0')
-- + utm_source + utm_medium (ПЯТЬ полей, все ЖЁСТКИЕ).
-- utm_content заказа = дата посева DDMMYYYY; то же значение проставлено в лидах.
-- Домен заказа берётся из post_links (jsonb->>0 URL host) как effective_domain.
-- utm_source/utm_medium нормализуются LOWER(BTRIM(..)) одинаково на обеих сторонах.
-- Пятёрка = полный GROUP BY-ключ агрегата лидов, поэтому в _tmp_leads_agg_telegain
-- ей соответствует РОВНО ОДНА строка — тай-брейкер ORDER BY не нужен.
-- LATERAL + LIMIT 1 защищает от случайных дублей.
LEFT JOIN LATERAL (
    SELECT la.*
    FROM {LEADS_AGG_TMP} la
    WHERE la.utm_campaign     = d.utm_campaign
      AND la.lead_utm_content = lpad(btrim(d.utm_content), 8, '0')
      AND la.lead_domain      = d.effective_domain
      AND la.lead_utm_source  = LOWER(BTRIM(d.utm_source))
      AND la.lead_utm_medium  = LOWER(BTRIM(d.utm_medium))
    LIMIT 1
) l ON TRUE
"""
    with conn.cursor() as cur:
        cur.execute(sql)
        rows      = cur.fetchall()
        col_names = [desc[0] for desc in cur.description]
    _drop_leads_agg(conn)
    return rows, col_names


def collect_errors(conn):
    """Собирает дефекты заказа по ЛЮБОМУ из 5 ключей матчинга в
    public.local_telega_in_orders_errors.

    Ошибка = дефект любого поля ключа связки заказ<->лиды (5 полей). Значения
    error_type/error_detail — НА РУССКОМ (названия столбцов остаются английскими).

    error_type:
      - 'неверный utm_content'   — utm_content NULL/пусто либо после lpad не ^\\d{8}$
      - 'пустой utm_campaign'    — utm_campaign NULL/пусто
      - 'пустой utm_source'      — utm_source NULL/пусто
      - 'пустой utm_medium'      — utm_medium NULL/пусто
      - 'домен не извлекается'    — effective_domain NULL/пусто
            (host из post_links jsonb->>0, fallback SPLIT_PART(order_project_name,' ',1))

    Пишет ТОЛЬКО дефекты ключа самого заказа (НЕ orphan'ы — заказы с валидным
    ключом, но без подходящего лида сюда НЕ попадают).

    Охват: status='complete' AND created_at >= '2026-05-01'.
    Режим: DROP TABLE + CREATE + INSERT (полный пересбор каждый прогон; DROP+CREATE
    нужен чтобы задать порядок колонок — post_links сразу после order_project_name,
    добавлен status). РОВНО ОДНА строка
    на заказ: все дефекты заказа склеиваются через string_agg('; ') в одно
    значение error_type и одно значение error_detail. Порядок склейки
    детерминирован (ORDER BY фиксированного порядка типов: content -> campaign
    -> source -> medium -> domain). В таблицу попадают только заказы с хотя бы
    одним дефектом.
    """
    # DROP+CREATE (вместо CREATE IF NOT EXISTS + ALTER + TRUNCATE) — единственный
    # способ задать НУЖНЫЙ порядок колонок в PostgreSQL (ALTER не переупорядочивает).
    # Новые/перемещённые колонки: post_links идёт сразу после order_project_name,
    # добавлен status TEXT (значение из local_telega_in_orders.status).
    ddl = f"""
DROP TABLE IF EXISTS public.{ERRORS_TABLE};
CREATE TABLE public.{ERRORS_TABLE} (
    id               BIGINT,
    order_id         BIGINT,
    order_project_name TEXT,
    post_links       TEXT,
    status           TEXT,
    utm_source       TEXT,
    utm_medium       TEXT,
    utm_campaign     TEXT,
    utm_content      TEXT,
    utm_content_norm TEXT,
    effective_domain TEXT,
    site_status      TEXT,
    directologist    TEXT,
    salon            TEXT,
    city             TEXT,
    region           TEXT,
    total_price      NUMERIC,
    created_at       TIMESTAMP,
    error_type       TEXT,
    error_detail     TEXT,
    checked_at       TIMESTAMP DEFAULT now()
);
"""
    # effective_domain воспроизводит ту же логику, что tio_raw/tio_dated в run_query:
    # host из post_links (jsonb->>0 URL) ИЛИ post_link, fallback —
    # первое слово order_project_name. Редиректоры (telega.io/max.ru/t.me) → fallback.
    # Один заказ -> РОВНО ОДНА строка. Дефекты собираются через
    # CROSS JOIN LATERAL (VALUES ...) (по строке на дефект, как раньше), затем
    # ВНЕШНИЙ GROUP BY o.id агрегирует их в одно значение:
    #   error_type   = string_agg(v.error_type,   '; ')
    #   error_detail = string_agg(v.error_detail, '; ')
    # Порядок склейки ДЕТЕРМИНИРОВАННЫЙ — ORDER BY v.ord (фиксированный порядок
    # пяти типов дефектов: content -> campaign -> source -> medium -> domain).
    # В таблицу попадают ТОЛЬКО заказы, у которых есть хотя бы один дефект.
    sql = f"""
INSERT INTO public.{ERRORS_TABLE}
    (id, order_id, order_project_name, post_links, status, utm_source, utm_medium,
     utm_campaign, utm_content, utm_content_norm, effective_domain,
     site_status, directologist, salon, city, region,
     total_price, created_at,
     error_type, error_detail)
SELECT o.id, o.order_id, o.order_project_name, o.post_links, o.status, o.utm_source, o.utm_medium,
       o.utm_campaign, o.utm_content, lpad(btrim(o.utm_content), 8, '0'), o.effective_domain,
       gs.status AS site_status, gs.directologist, gs.salon, gs.city, gs.region,
       o.total_price, o.created_at,
       string_agg(v.error_type,   '; ' ORDER BY v.ord) AS error_type,
       string_agg(v.error_detail, '; ' ORDER BY v.ord) AS error_detail
FROM (
    SELECT a.*,
        CASE
            WHEN NULLIF(LOWER(TRIM(
                    CASE
                        WHEN a.post_links IS NOT NULL AND a.post_links LIKE '[%'
                            THEN SUBSTRING((a.post_links::jsonb->>0) FROM '://([^/?]+)')
                        ELSE SUBSTRING(COALESCE(a.post_link, '') FROM '://([^/?]+)')
                    END
                 )), '') IS NOT NULL
                 AND LOWER(TRIM(
                    CASE
                        WHEN a.post_links IS NOT NULL AND a.post_links LIKE '[%'
                            THEN SUBSTRING((a.post_links::jsonb->>0) FROM '://([^/?]+)')
                        ELSE SUBSTRING(COALESCE(a.post_link, '') FROM '://([^/?]+)')
                    END
                 )) NOT IN ('telega.io', 'max.ru', 't.me')
            THEN LOWER(TRIM(
                    CASE
                        WHEN a.post_links IS NOT NULL AND a.post_links LIKE '[%'
                            THEN SUBSTRING((a.post_links::jsonb->>0) FROM '://([^/?]+)')
                        ELSE SUBSTRING(COALESCE(a.post_link, '') FROM '://([^/?]+)')
                    END
                 ))
            ELSE LOWER(TRIM(SPLIT_PART(a.order_project_name, ' ', 1)))
        END AS effective_domain
    FROM public.{API_TABLE} a
    WHERE a.status = 'complete' AND a.created_at >= '2026-05-01'
) o
LEFT JOIN LATERAL (
    SELECT gs2.status, gs2.directologist, gs2.salon, gs2.city, gs2.region
    FROM public.{SITES_TABLE} gs2
    WHERE LOWER(TRIM(gs2.domain)) = LOWER(TRIM(o.effective_domain))
    LIMIT 1
) gs ON TRUE
CROSS JOIN LATERAL (VALUES
    (1, 'неверный utm_content',
       CASE WHEN o.utm_content IS NULL OR btrim(o.utm_content) = '' THEN 'utm_content пустой/NULL'
            WHEN lpad(btrim(o.utm_content), 8, '0') !~ '^[0-9]{{8}}$'
              THEN 'после lpad не 8 цифр: ' || lpad(btrim(o.utm_content), 8, '0') END),
    (2, 'пустой utm_campaign', CASE WHEN o.utm_campaign IS NULL OR btrim(o.utm_campaign) = '' THEN 'utm_campaign пустой/NULL' END),
    (3, 'пустой utm_source',   CASE WHEN o.utm_source   IS NULL OR btrim(o.utm_source)   = '' THEN 'utm_source пустой/NULL'   END),
    (4, 'пустой utm_medium',   CASE WHEN o.utm_medium   IS NULL OR btrim(o.utm_medium)   = '' THEN 'utm_medium пустой/NULL'   END),
    (5, 'домен не извлекается', CASE WHEN o.effective_domain IS NULL OR btrim(o.effective_domain) = '' THEN 'effective_domain пустой/NULL (нет host в post_links и пустой order_project_name)' END)
) AS v(ord, error_type, error_detail)
WHERE v.error_detail IS NOT NULL
GROUP BY o.id, o.order_id, o.order_project_name, o.post_links, o.status, o.utm_source, o.utm_medium,
         o.utm_campaign, o.utm_content, o.effective_domain,
         gs.status, gs.directologist, gs.salon, gs.city, gs.region,
         o.total_price, o.created_at;
"""
    with conn.cursor() as cur:
        cur.execute(ddl)
        cur.execute(sql)
        cur.execute(f"SELECT count(*) FROM public.{ERRORS_TABLE}")
        n = cur.fetchone()[0]
    conn.commit()
    print(f'OK: {n} дефектов заказов в {ERRORS_TABLE}')
    return n


def main():
    conn = _connect()
    try:
        print(f'Читаем {API_TABLE} (status=complete)...')
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
                dohod_do_kredita, dobro, utm_campaign
            ) VALUES %s
        """
        with conn.cursor() as cur:
            execute_values(cur, insert_sql, rows)
        conn.commit()
        print(f'OK: {len(rows)} строк загружено в {OUTPUT_TABLE}')

        print(f'Собираем дефекты заказов в {ERRORS_TABLE}...')
        collect_errors(conn)
    finally:
        conn.close()


if __name__ == '__main__':
    main()
