"""
big analytics_v5/crop_targeting/load_crop_targeting_leads.py

Обогащает gsheets_crop_targeting_account метриками лидов из local_leads_all.

Логика привязки (окно 90 дней, nearest-prior placement):
  - placements: каждая уникальная (utm утвержденная + дата размещения) входит
    в итоговую таблицу РОВНО ОДИН РАЗ. Расход суммируется по дню если в реестре
    несколько строк с одним utm+дата. Никакого копирования расхода в другие месяцы.
  - Лид привязывается к размещению того же канала с MAX(placement_date) <= created_date,
    при условии (created_date - placement_date) <= 90 дней (nearest-prior).
  - DISTINCT ON (lead_id) гарантирует что один лид матчится ровно с одним размещением.
  - Лиды-сироты (нет prior placement в окне 90 дней) → отдельные строки с total_cost=NULL.

Убрана CTE gsheets_nearest — она использовалась для сирот и копировала расход
ближайшего размещения, что раздувало SUM(total_cost) выше фактического реестра.

БД: ad_analytics_bi

Запуск (из папки big analytics_v5/):
  python3 crop_targeting/load_crop_targeting_leads.py
"""

import os
import sys

import psycopg2
from psycopg2.extras import execute_values

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import DB_DST

# ─── Настройки ────────────────────────────────────────────────────────────────

SOURCE_TABLE = 'gsheets_crop_targeting_account'
UTM_TABLE    = 'gsheets_crop_targeting_account_pravilo_utm'
LEADS_TABLE  = 'local_leads_all'
OUTPUT_TABLE = 'gsheets_crop_targeting_account_leads'

METRICS = [
    'kol_vo_zayavok', 'korr', 'kval', 'priezd', 'prodazhi',
    'nekorr', 'ne_otvechaet', 'filtr', 'nedozvon', 'priedet',
    'dohod_do_kredita', 'dobro',
]

# Колонки из SOURCE_TABLE, которые не нужны в выходной таблице
EXCLUDE_COLS = {
    'НДС',
    'Цена закупа без ндс',
    'процент входящего ндс',
    'Проценты ак',
    'Наша комиссия с НДС, руб.',
    'Наша чистая комиссия (без затрат н',   # имя обрезано в БД
}


def _connect():
    return psycopg2.connect(**DB_DST)


def _q(name):
    return '"' + name.replace('"', '""') + '"'


# ─── PostgreSQL helpers ────────────────────────────────────────────────────────

def get_source_columns(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
              AND column_name != 'id'
            ORDER BY ordinal_position
        """, (SOURCE_TABLE,))
        return [row[0] for row in cur.fetchall() if row[0] not in EXCLUDE_COLS]


def ensure_output_table(conn, source_cols):
    src_ddl     = ',\n    '.join(f'{_q(c)} TEXT' for c in source_cols)
    metrics_ddl = ',\n    '.join(f'{m} INTEGER' for m in METRICS)
    sql = f"""
DROP TABLE IF EXISTS public.{OUTPUT_TABLE};
CREATE TABLE public.{OUTPUT_TABLE} (
    id   SERIAL PRIMARY KEY,
    {src_ddl},
    {metrics_ddl}
);
"""
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


# ─── CTE-запрос ───────────────────────────────────────────────────────────────

def _placement_agg_col(c: str) -> str:
    """Агрегация колонок реестра при GROUP BY (utm + дата + сайт).
    Баг-фикс 2026-06-05: ключ расширен с (utm+дата) до (utm+дата+Сайт),
    чтобы одно размещение на нескольких доменах не схлапывалось в один домен.
    """
    if c == 'total_cost':
        return f"SUM(NULLIF(TRIM({_q(c)}), '')::numeric)::text AS {_q(c)}"
    if c == 'utm утвержденная':
        return _q(c)
    if c == 'Сайт':
        # Сайт входит в GROUP BY — возвращаем без агрегации
        return _q(c)
    # Дата и все текстовые поля — берём MIN (все строки с одним utm+дата+сайт идентичны)
    return f'MIN({_q(c)}) AS {_q(c)}'


def _orphan_sel(source_cols: list) -> str:
    """SELECT-список для строк-сирот: utm утвержденная + Сайт (домен лида) + дата из
    created_date, остальное NULL.
    Баг-фикс 2026-06-05 v2: колонка "Сайт" теперь = домен лида (oa.orphan_site), а не
    NULL — orphan-лиды видны на своём домене в big_analytics_full (резолв домена идёт
    через "Сайт" в load_crop_to_big_analytics.py)."""
    parts = []
    for c in source_cols:
        if c == 'utm утвержденная':
            parts.append('oa."utm утвержденная"')
        elif c == 'Сайт':
            parts.append('oa.orphan_site')
        elif c == 'Дата':
            # Дата сироты = первая дата лида в данном месяце (для ориентира)
            parts.append("TO_CHAR(oa.min_created_date, 'DD.MM.YYYY')")
        else:
            parts.append('NULL::TEXT')
    return ',\n        '.join(parts)


def run_query(conn, source_cols):
    # SELECT-список из placements (строки с реальным расходом)
    p_sel = ',\n        '.join(f'p.{_q(c)}' for c in source_cols)

    # Агрегация placements: SUM(total_cost) по дню, MIN остальных полей
    agg_cols = ',\n        '.join(_placement_agg_col(c) for c in source_cols)

    # SELECT-список для сирот (total_cost = NULL, дата из лида)
    orphan_sel = _orphan_sel(source_cols)

    # Статусные условия для каждой метрики воронки (одинаковые в обеих ветках)
    korr_cond = """status IN (
            'Новый','В салоне','Купил','На рассмотрении','В салоне не отмечен','Отказ',
            'Не отвечает','Приедет','В работе','Фильтр','Недозвон','Приехал',
            'Уточнить по дате','Перезвонить','Отказ клиента','Продажа за наличные',
            'Продажа в кредит','Соскок','Консультация','Отказ по банкам','Одобрен банк',
            'Одобрено банк','Новая','Заполнить','Новая: Не отвечает','Т. Кредит',
            'А. Кредит','Одобрить','Одобрен','Отложенный','В работе - odobrit',
            'Перезвонить срочно','Одобренные','Оформленные','Одобрение','Дошел в КО'
        )"""
    priezd_cond = """status IN (
            'В салоне','В салоне не отмечен','Купил','Приехал','Соскок','Консультация',
            'Отказ по банкам','Одобрен банк','Продажа за наличные','Продажа в кредит',
            'Одобрить','Одобрен','На рассмотрении','Т. Кредит','А. Кредит',
            'Одобренные','Оформленные','Одобрение','Дошел в КО'
        )"""
    prodazhi_cond = """status IN (
            'Купил','Продажа за наличные','Продажа в кредит',
            'Т. Кредит','А. Кредит','Оформленные','COMPLETED','Продажа'
        )"""
    nekorr_cond = """status IN (
            'Некорректные данные','Корзина','Повтор','Нет данных','Дубль',
            '***','Спам','Хлам','Отбракованные','Общие вопросы'
        )"""

    sql = f"""
WITH

-- Эффективный UTM: pravilo_utm задаёт соответствие lead.utm_campaign → "utm утвержденная"
utm_effective AS (
    SELECT DISTINCT
        "utm утвержденная",
        CASE
            WHEN "UTM" IS NOT NULL AND "UTM" != '' THEN "UTM"
            ELSE "utm утвержденная"
        END AS effective_utm
    FROM public.{UTM_TABLE}
    WHERE COALESCE("utm утвержденная", '') != '-'
      AND COALESCE("UTM", '') != '-'
),

-- placements: каждая уникальная (utm + дата + Сайт) из реестра ровно один раз.
-- Баг-фикс 2026-06-05: ключ расширен с (utm+дата) до (utm+дата+Сайт).
-- Одно размещение с двумя доменами (driveavto + ladaauto) теперь даёт 2 строки.
-- Расход: каждая строка-домен берёт свой cost (SUM внутри utm+дата+Сайт) — не задваивается.
placements AS (
    SELECT
        {agg_cols}
    FROM public.{SOURCE_TABLE}
    WHERE NULLIF(TRIM("Дата"), '') IS NOT NULL
      AND TO_DATE(NULLIF(TRIM("Дата"), ''), 'FMDD.FMMM.YYYY') >= '2026-01-01'  -- данные до 2026 не нужны
    GROUP BY "utm утвержденная", TO_DATE(NULLIF(TRIM("Дата"), ''), 'FMDD.FMMM.YYYY'), "Сайт"
),

-- Все лиды с utm_medium='posev', у которых есть соответствие в utm_effective.
-- Вариант A: убираем майские лиды, уже учтённые telega-путём, чтобы не задваивать;
-- 26 уникальных VK/Max без telega (avtoworld-kuban.ru и др.) остаются.
-- До-майские (created_date < '2026-05-01') не затрагиваются — у них своя привязка.
-- LEFT JOIN local_domains даёт домен лида (ld.name) для точного матча с telega ниже.
-- LEFT (не INNER): лиды без domain_id не теряются — для них telega-матч не сработает,
-- они останутся (telega их тоже не считает, т.к. матчит по домену).
posev_leads_raw AS (
    SELECT
        l.id              AS lead_id,
        ue."utm утвержденная",
        l.created_date,
        l.status,
        ld.name           AS lead_domain  -- домен лида для приоритетной привязки (баг-фикс 2026-06-05)
    FROM public.{LEADS_TABLE} l
    LEFT JOIN public.local_domains ld ON ld.id = l.domain_id
    JOIN utm_effective ue ON l.utm_campaign = ue.effective_utm
    WHERE l.utm_campaign IS NOT NULL AND l.utm_campaign != ''
      AND l.utm_medium = 'posev'
      AND l.created_date IS NOT NULL
      AND l.created_date >= '2026-01-01'  -- данные до 2026 не нужны
      -- Фикс 2026-06-08 (LeadV-протечка): внешний поставщик лидов LeadV ошибочно
      -- попадал в посевы. У LeadV-posev source_name LIKE 'LeadV%', utm_source=ДОМЕН,
      -- но utm_medium='posev'+utm_campaign совпадает с VK/региональным справочником →
      -- лид «протекал» сюда. По решению: лиды, сконфигурированные как ПИКСЕЛЬ
      -- (source_name есть в local_pixel_config), должны учитываться пиксельной веткой
      -- (step5 build_pixel ловит их по source_name=pixel_name), а LeadV вне конфига —
      -- полностью исключаются. Условие отсекает ОБА класса:
      --   (а) любой источник, заведённый в local_pixel_config (=пиксель, не посев);
      --   (б) любой LeadV-вендор буквально (вкл. 'LeadV SEO ...', которого нет в конфиге).
      -- Фильтр строго НА УРОВНЕ ЛИДА (source_name), НЕ по домену — реальный VK-расход
      -- crop_targeting тех же доменов (driveavto-kazan.ru и др.) НЕ затрагивается.
      -- Настоящие соц-посевы (source_name IS NULL, utm_source=канал) под условие не
      -- подпадают и остаются в посевах без изменений.
      AND NOT (
          l.source_name LIKE 'LeadV%'
          OR EXISTS (
              SELECT 1 FROM public.local_pixel_config pc
              WHERE l.source_name = pc.pixel_name
          )
      )
      -- Вариант A: исключаем майские лиды, УЖЕ учтённые telega-путём по ТОЧНОМУ ключу
      -- telega (utm_campaign + домен + месяц±1), чтобы не задваивать. Лиды, которые
      -- telega по факту НЕ считает (другой домен/месяц), и 26 уникальных VK/Max без
      -- telega (avtoworld-kuban.ru и др.) — ОСТАЮТСЯ. До-майские не затрагиваются.
      -- crop_targeting_api_telegain_lead строится РАНЬШЕ (порядок шагов в pipeline.py изменён).
      AND NOT (
          l.created_date >= '2026-05-01'
          AND EXISTS (
              SELECT 1 FROM public.crop_targeting_api_telegain_lead t
              WHERE t.utm_campaign = l.utm_campaign
                AND LOWER(TRIM(t.domain)) = LOWER(TRIM(ld.name))
                AND DATE_TRUNC('month', t."Date")::date BETWEEN
                        (DATE_TRUNC('month', l.created_date) - INTERVAL '1 month')::date
                    AND (DATE_TRUNC('month', l.created_date) + INTERVAL '1 month')::date
          )
      )
),

-- Привязка лида к размещению: nearest-PRIOR placement СТРОГО на СВОЁМ домене лида.
-- Баг-фикс 2026-06-05 (v2, межсайтовое задвоение): домен добавлен в само УСЛОВИЕ JOIN
-- (LOWER(TRIM(p."Сайт")) = LOWER(TRIM(lr.lead_domain))), а не только в ORDER BY.
-- Раньше JOIN шёл лишь по "utm утвержденная" → лиды одного домена приклеивались к
-- единственному размещению с этой utm на ДРУГОМ домене (пример: 18 autopark + 26
-- autostorage лидов tvoy_stvrp приклеивались к единственному placement tvoy_stvrp =
-- havalcar 15.01 → havalcar получал kol_vo_zayavok=114). Теперь лид матчится только
-- с размещением СВОЕГО домена. Лиды без размещения на своём домене (или без домена,
-- lead_domain IS NULL) НЕ матчатся здесь → попадают в orphan_leads (total_cost=NULL)
-- на своём домене, а не задваиваются на чужом.
-- ORDER BY оставлен только по дате (домен уже гарантирован JOIN'ом).
-- DISTINCT ON (lead_id) → один лид матчится ровно с одним размещением.
posev_leads_attributed AS (
    SELECT DISTINCT ON (lr.lead_id)
        lr.lead_id,
        lr."utm утвержденная",
        lr.created_date,
        lr.status,
        TO_DATE(NULLIF(TRIM(p."Дата"), ''), 'FMDD.FMMM.YYYY') AS placement_date,
        p."Сайт"                                               AS placement_site
    FROM posev_leads_raw lr
    JOIN public.{SOURCE_TABLE} p
      ON p."utm утвержденная" = lr."utm утвержденная"
     -- Баг-фикс 2026-06-05 v2: домен лида ДОЛЖЕН совпадать с доменом размещения.
     AND LOWER(TRIM(p."Сайт")) = LOWER(TRIM(lr.lead_domain))
     AND NULLIF(TRIM(p."Дата"), '') IS NOT NULL
     AND TO_DATE(NULLIF(TRIM(p."Дата"), ''), 'FMDD.FMMM.YYYY') <= lr.created_date
     AND TO_DATE(NULLIF(TRIM(p."Дата"), ''), 'FMDD.FMMM.YYYY') >= '2026-01-01'  -- данные до 2026 не нужны
     AND (lr.created_date - TO_DATE(NULLIF(TRIM(p."Дата"), ''), 'FMDD.FMMM.YYYY')) <= 90
    ORDER BY lr.lead_id,
             -- Ближайшее предшествующее размещение (домен уже гарантирован JOIN'ом)
             TO_DATE(NULLIF(TRIM(p."Дата"), ''), 'FMDD.FMMM.YYYY') DESC
),

-- Лиды-сироты: нет размещения НА СВОЁМ домене лида в окне 90 дней (или нет домена,
-- или пришли раньше первого размещения своего домена). Для них total_cost = NULL.
-- Баг-фикс 2026-06-05 v2: NOT EXISTS зеркалит JOIN в posev_leads_attributed — то же
-- доменное условие (LOWER(TRIM(p2."Сайт")) = LOWER(TRIM(lr.lead_domain))). Иначе лид,
-- у которого нет своего placement, но ЕСТЬ чужой по той же utm, не попал бы ни в
-- attributed (домен не совпал), ни в orphan (NOT EXISTS ложно из-за чужого placement)
-- → лид бы потерялся. Теперь такие лиды гарантированно становятся orphan на своём
-- домене (lead_domain), а NULL-домен → тоже orphan (LOWER(TRIM(NULL)) = ... → NULL,
-- NOT EXISTS истинно). Расход чужого размещения к ним больше не приклеивается.
orphan_leads AS (
    SELECT lr.lead_id, lr."utm утвержденная", lr.created_date, lr.status, lr.lead_domain
    FROM posev_leads_raw lr
    WHERE NOT EXISTS (
        SELECT 1 FROM public.{SOURCE_TABLE} p2
        WHERE p2."utm утвержденная" = lr."utm утвержденная"
          AND LOWER(TRIM(p2."Сайт")) = LOWER(TRIM(lr.lead_domain))
          AND NULLIF(TRIM(p2."Дата"), '') IS NOT NULL
          AND TO_DATE(NULLIF(TRIM(p2."Дата"), ''), 'FMDD.FMMM.YYYY') <= lr.created_date
          AND TO_DATE(NULLIF(TRIM(p2."Дата"), ''), 'FMDD.FMMM.YYYY') >= '2026-01-01'  -- данные до 2026 не нужны
          AND (lr.created_date - TO_DATE(NULLIF(TRIM(p2."Дата"), ''), 'FMDD.FMMM.YYYY')) <= 90
    )
),

-- Агрегация привязанных лидов по (utm + дата размещения + домен размещения).
-- Баг-фикс 2026-06-05: добавлен placement_site в ключ GROUP BY.
-- Лиды ladaauto и driveavto считаются в отдельных строках.
leads_per_placement AS (
    SELECT
        la.placement_date,
        la."utm утвержденная",
        la.placement_site,
        COUNT(*)                                                    AS kol_vo_zayavok,
        SUM(CASE WHEN {korr_cond}     THEN 1 ELSE 0 END)           AS korr,
        SUM(CASE WHEN {priezd_cond}   THEN 1 ELSE 0 END)           AS priezd,
        SUM(CASE WHEN {prodazhi_cond} THEN 1 ELSE 0 END)           AS prodazhi,
        SUM(CASE WHEN {nekorr_cond}   THEN 1 ELSE 0 END)           AS nekorr,
        SUM(CASE WHEN status IN ('Не отвечает','Новая: Не отвечает')
            THEN 1 ELSE 0 END)                                      AS ne_otvechaet,
        SUM(CASE WHEN status = 'Фильтр'   THEN 1 ELSE 0 END)       AS filtr,
        SUM(CASE WHEN status = 'Недозвон' THEN 1 ELSE 0 END)       AS nedozvon,
        SUM(CASE WHEN status = 'Приедет'  THEN 1 ELSE 0 END)       AS priedet
    FROM posev_leads_attributed la
    GROUP BY la.placement_date, la."utm утвержденная", la.placement_site
),

-- Агрегация сирот по (utm + домен лида + месяц появления лида).
-- Баг-фикс 2026-06-05 v2: добавлен lead_domain в ключ GROUP BY → orphan-строки
-- ложатся на СВОЙ домен (попадают в "Сайт"), а не схлопываются в одну строку без
-- домена. Лиды autopark/autostorage tvoy_stvrp (нет своего placement) видны на
-- autopark-26.ru / autostorage-stavropol.ru. Строки видны в итоге, total_cost = NULL.
orphan_agg AS (
    SELECT
        o."utm утвержденная",
        o.lead_domain                                    AS orphan_site,
        DATE_TRUNC('month', o.created_date)::date        AS orphan_month,
        MIN(o.created_date)                              AS min_created_date,
        COUNT(*)                                         AS kol_vo_zayavok,
        SUM(CASE WHEN {korr_cond}     THEN 1 ELSE 0 END) AS korr,
        SUM(CASE WHEN {priezd_cond}   THEN 1 ELSE 0 END) AS priezd,
        SUM(CASE WHEN {prodazhi_cond} THEN 1 ELSE 0 END) AS prodazhi,
        SUM(CASE WHEN {nekorr_cond}   THEN 1 ELSE 0 END) AS nekorr,
        SUM(CASE WHEN status IN ('Не отвечает','Новая: Не отвечает')
            THEN 1 ELSE 0 END)                           AS ne_otvechaet,
        SUM(CASE WHEN status = 'Фильтр'   THEN 1 ELSE 0 END) AS filtr,
        SUM(CASE WHEN status = 'Недозвон' THEN 1 ELSE 0 END) AS nedozvon,
        SUM(CASE WHEN status = 'Приедет'  THEN 1 ELSE 0 END) AS priedet
    FROM orphan_leads o
    GROUP BY o."utm утвержденная", o.lead_domain, DATE_TRUNC('month', o.created_date)::date
)

-- ЧАСТЬ 1: размещения (строки реестра) + привязанные лиды (LEFT JOIN).
-- Каждое размещение входит ровно один раз со своим реальным расходом.
-- Строки без лидов получают нули (не NULL) в метриках воронки.
SELECT
    {p_sel},
    COALESCE(lpp.kol_vo_zayavok, 0)   AS kol_vo_zayavok,
    COALESCE(lpp.korr, 0)             AS korr,
    COALESCE(lpp.korr, 0)
        - COALESCE(lpp.ne_otvechaet, 0)
        - COALESCE(lpp.filtr, 0)
        - COALESCE(lpp.nedozvon, 0)   AS kval,
    COALESCE(lpp.priezd, 0)           AS priezd,
    COALESCE(lpp.prodazhi, 0)         AS prodazhi,
    COALESCE(lpp.nekorr, 0)           AS nekorr,
    COALESCE(lpp.ne_otvechaet, 0)     AS ne_otvechaet,
    COALESCE(lpp.filtr, 0)            AS filtr,
    COALESCE(lpp.nedozvon, 0)         AS nedozvon,
    COALESCE(lpp.priedet, 0)          AS priedet,
    NULL::INTEGER                     AS dohod_do_kredita,
    NULL::INTEGER                     AS dobro
FROM placements p
LEFT JOIN leads_per_placement lpp
    ON  lpp."utm утвержденная" = p."utm утвержденная"
    AND lpp.placement_date     = TO_DATE(NULLIF(TRIM(p."Дата"), ''), 'FMDD.FMMM.YYYY')
    AND (lpp.placement_site = p."Сайт" OR (lpp.placement_site IS NULL AND p."Сайт" IS NULL))

UNION ALL

-- ЧАСТЬ 2: лиды-сироты (total_cost = NULL).
-- Пришли раньше любого размещения канала в окне 90 дней.
-- gsheets-поля = NULL, utm утвержденная и дата (из лида) — заполнены.
SELECT
    {orphan_sel},
    oa.kol_vo_zayavok,
    oa.korr,
    oa.korr - oa.ne_otvechaet - oa.filtr - oa.nedozvon   AS kval,
    oa.priezd,
    oa.prodazhi,
    oa.nekorr,
    oa.ne_otvechaet,
    oa.filtr,
    oa.nedozvon,
    oa.priedet,
    NULL::INTEGER AS dohod_do_kredita,
    NULL::INTEGER AS dobro
FROM orphan_agg oa
"""
    with conn.cursor() as cur:
        cur.execute(sql)
        rows      = cur.fetchall()
        col_names = [desc[0] for desc in cur.description]
    return rows, col_names


# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    conn = _connect()
    try:
        print(f'Читаем структуру {SOURCE_TABLE}...')
        source_cols = get_source_columns(conn)
        print(f'  Колонок: {len(source_cols)}')

        print('Выполняем CTE-запрос...')
        rows, col_names = run_query(conn, source_cols)

        # Различаем размещения (с расходом) и сирот (total_cost = NULL)
        cost_idx  = source_cols.index('total_cost') if 'total_cost' in source_cols else None
        if cost_idx is not None:
            placements_cnt = sum(1 for r in rows if r[cost_idx] is not None)
            orphans_cnt    = len(rows) - placements_cnt
        else:
            placements_cnt = len(rows)
            orphans_cnt    = 0
        print(f'  Размещений (с расходом): {placements_cnt}')
        print(f'  Сирот (total_cost=NULL): {orphans_cnt}')
        print(f'  Итого:                   {len(rows)}')

        print(f'Создаём таблицу {OUTPUT_TABLE}...')
        ensure_output_table(conn, source_cols)

        print('Вставляем данные...')
        insert_sql = f"""
            INSERT INTO public.{OUTPUT_TABLE}
            ({', '.join(_q(c) for c in col_names)})
            VALUES %s
        """
        with conn.cursor() as cur:
            execute_values(cur, insert_sql, rows)
        conn.commit()
        print(f'OK: {len(rows)} строк загружено в {OUTPUT_TABLE}')

    finally:
        conn.close()


if __name__ == '__main__':
    main()
