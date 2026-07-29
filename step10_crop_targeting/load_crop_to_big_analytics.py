"""
big analytics_v5/crop_targeting/load_crop_to_big_analytics.py

Обновляет big_analytics_crop_targeting и big_analytics_full данными посевов:
  - до мая 2026: gsheets_crop_targeting_account_leads
  - с мая 2026:  crop_targeting_api_telegain_lead

Шаг 1: DELETE из big_analytics_crop_targeting WHERE _source_table='crop_targeting'
Шаг 2: INSERT gsheets_leads (< 2026-05-01) → big_analytics_crop_targeting
Шаг 3: INSERT api_leads    (>= 2026-05-01) → big_analytics_crop_targeting
Шаг 4: DELETE из big_analytics_full WHERE _source_table='crop_targeting'
Шаг 5: INSERT big_analytics_crop_targeting WHERE _source_table='crop_targeting' → big_analytics_full

ВАЖНО: запускать только после завершения полного пайплайна big_analytics_v5 (шаги 0–7).

Запуск (из папки big analytics_v5/):
  python3 crop_targeting/load_crop_to_big_analytics.py
"""

import os
import sys

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import DB_DST

GSHEETS_TABLE = 'gsheets_crop_targeting_account_leads'
API_TABLE     = 'crop_targeting_api_telegain_lead'
CROP_TABLE    = 'big_analytics_crop_targeting'
TARGET_TABLE  = 'big_analytics_full'

_COLS = (
    'key3, "Date", "День недели", week_start,'
    ' "CampaignId", "CampaignName", "AdGroupId", "AdGroupName",'
    ' "AdNetworkType", "Device", "Impressions", "Clicks", total_cost, domain,'
    ' "RlAdjustmentId", "RlAdjustmentId_total",'
    ' campaign_code, tp, cpc_cpa, site_quiz, adgroup_code,'
    ' account_login, manager_login,'
    ' ag_part1, ag_part2, ag_part3, ag_part4, ag_part5, ag_part6, ag_part7,'
    ' "марки авто", "Название crm", тип_заявки,'
    ' kol_vo_zayavok, korr, kval, priezd, prodazhi,'
    ' nekorr, ne_otvechaet, filtr, nedozvon, priedet,'
    ' dohod_do_kredita, dobro,'
    ' "статус", "специалист", "тип_сайта", "шаблон", "салон", "город", "регион",'
    ' direction, "неверный_кодер_new", fid,'
    ' проджект, id_салона, менеджер,'
    ' источник, направление,'
    ' "номер кампании | название кампании",'
    ' "номер группы | название группы",'
    ' "План заявки", "План приезда",'
    ' "аккаунт|сайт", priezd_arrival_date, prodazhi_arrival_date,'
    ' поставщик, _source_table'
)

# ── gsheets JOIN (использует колонки "Сайт" и "Гео") ─────────────────────────

_GSHEETS_REESTR_JOIN = """
LEFT JOIN (
    SELECT LOWER(TRIM(d.name)) AS domain_name,
           MAX(CASE l.source_type
               WHEN 'crmf_excel'       THEN 'Фаиг'
               WHEN 'plex_excel'       THEN 'Плекс'
               WHEN 'mega_crm_excel'   THEN 'Мега'
               WHEN 'marcar_crm_excel' THEN 'Маркар'
               ELSE l.source_type
           END) AS leads_source_type
    FROM public.local_leads_all l
    JOIN public.local_domains d ON d.id = l.domain_id
    WHERE d.name IS NOT NULL AND d.name != ''
    GROUP BY LOWER(TRIM(d.name))
) dst ON LOWER(TRIM("Сайт")) = dst.domain_name
LEFT JOIN (
    SELECT DISTINCT salon, client_id, project_manager, sales_manager, crm
    FROM public.local_gsheet_sites WHERE salon IS NOT NULL
) gs ON LOWER(TRIM(REPLACE("Гео", 'АЦ на Жукова', 'Автоцентр на Жукова'))) = LOWER(TRIM(gs.salon))
LEFT JOIN public.local_gsheet_autosalony_clients auto
    ON gs.client_id = auto.id_салона AND gs.client_id IS NOT NULL
-- тип_сайта (вариант B): из справочника local_gsheet_sites по ДОМЕНУ "Сайт"
-- (как API/Telega-путь). Нет домена в справочнике → site_type NULL/Неопределено.
LEFT JOIN LATERAL (
    SELECT gs_site.site_type
    FROM public.local_gsheet_sites gs_site
    WHERE LOWER(TRIM(gs_site.domain)) = LOWER(TRIM("Сайт"))
    LIMIT 1
) gs_st ON TRUE
-- Справочник «utm утвержденная» → доминирующий источник.
-- Лиды посевов часто приходят с пустым "Источник", но в справочнике аккаунтов
-- (gsheets_crop_targeting_account) у той же utm источник заполнен. Берём моду
-- (самый частый источник на utm), Telegram нормализуем в 'telegram'.
LEFT JOIN (
    SELECT utm, src FROM (
        SELECT TRIM("utm утвержденная") AS utm,
               -- KOMPLEKS_REFACTOR_REDO_2026-07-09: src_dir возвращает уже нормализованный 'Посевы_*'
               CASE WHEN "Источник" = 'Telegram' THEN 'Посевы_Telegram'
                    WHEN "Источник" = 'VK'       THEN 'Посевы_VK'
                    WHEN "Источник" = 'Max'      THEN 'Посевы_Max'
                    ELSE "Источник" END AS src,
               ROW_NUMBER() OVER (
                   PARTITION BY TRIM("utm утвержденная")
                   ORDER BY COUNT(*) DESC, "Источник"
               ) AS rn
        FROM public.gsheets_crop_targeting_account
        WHERE "Источник" IS NOT NULL AND TRIM("Источник") <> ''
        GROUP BY TRIM("utm утвержденная"), "Источник"
    ) m WHERE rn = 1
) src_dir ON TRIM("utm утвержденная") = src_dir.utm
-- SUPPLIER_CASCADE_FIX_2026-06-16: каскад восстановления «Тип закупа» (поставщик)
-- из справочника закупов (gsheets_crop_targeting_account).
-- Аналог каскада из step3._build_crop_sql, но БЕЗ дата-гарда (максимальный охват).
-- cn_site10: агрегат по (utm + Сайт); n_tip = кол-во distinct непустых «Тип закупа».
-- Восстанавливаем ТОЛЬКО при n_tip=1 (однозначность — инвариант пользователя).
-- При n_tip≥2 — конфликт, поставщик остаётся NULL.
LEFT JOIN (
    SELECT
        TRIM("utm утвержденная")                        AS utm,
        LOWER(TRIM("Сайт"))                             AS site,
        COUNT(DISTINCT NULLIF(TRIM("Тип закупа"), ''))  AS n_tip,
        MAX(NULLIF(TRIM("Тип закупа"), ''))             AS tip
    FROM public.gsheets_crop_targeting_account
    WHERE COALESCE(TRIM("utm утвержденная"), '') <> ''
    GROUP BY TRIM("utm утвержденная"), LOWER(TRIM("Сайт"))
) cn_site10 ON TRIM("utm утвержденная") = cn_site10.utm
           AND LOWER(TRIM("Сайт")) = cn_site10.site
           AND cn_site10.n_tip = 1
-- cn_utm10: агрегат только по utm (фоллбэк когда site не совпал или n_tip_site!=1).
LEFT JOIN (
    SELECT
        TRIM("utm утвержденная")                        AS utm,
        COUNT(DISTINCT NULLIF(TRIM("Тип закупа"), ''))  AS n_tip,
        MAX(NULLIF(TRIM("Тип закупа"), ''))             AS tip
    FROM public.gsheets_crop_targeting_account
    WHERE COALESCE(TRIM("utm утвержденная"), '') <> ''
    GROUP BY TRIM("utm утвержденная")
) cn_utm10 ON TRIM("utm утвержденная") = cn_utm10.utm
          AND cn_utm10.n_tip = 1
"""

# ── API JOIN (использует колонки domain и "салон", alias t для lead-таблицы) ──

_API_REESTR_JOIN = """
LEFT JOIN (
    SELECT DISTINCT salon, client_id, project_manager, sales_manager, crm
    FROM public.local_gsheet_sites WHERE salon IS NOT NULL
) gs ON LOWER(TRIM(t."салон")) = LOWER(TRIM(gs.salon))
LEFT JOIN public.local_gsheet_autosalony_clients auto
    ON gs.client_id = auto.id_салона AND gs.client_id IS NOT NULL
"""

# ── gsheets SELECT body (начиная с "День недели") ────────────────────────────

_GSHEETS_SELECT_BODY = """
    CASE EXTRACT(ISODOW FROM TO_DATE(NULLIF(TRIM("Дата"), ''), 'DD.MM.YYYY'))
        WHEN 1 THEN '1_Понедельник' WHEN 2 THEN '2_Вторник' WHEN 3 THEN '3_Среда'
        WHEN 4 THEN '4_Четверг'    WHEN 5 THEN '5_Пятница'  WHEN 6 THEN '6_Суббота'
        WHEN 7 THEN '7_Воскресенье'
    END,
    DATE_TRUNC('week', TO_DATE(NULLIF(TRIM("Дата"), ''), 'DD.MM.YYYY'))::DATE,
    NULL::BIGINT, "Канал", NULL::BIGINT, NULL::TEXT,
    NULL::TEXT, NULL::TEXT, NULL::BIGINT, NULL::BIGINT,
    NULLIF(TRIM(total_cost), '')::NUMERIC,
    "Сайт",
    NULL::BIGINT, NULL::TEXT,
    -- campaign_code='Посевы', tp/adgroup_code=NULL, cpc_cpa/site_quiz='посевы'
    'Посевы'::TEXT, NULL::TEXT, 'посевы'::TEXT, 'посевы'::TEXT, NULL::TEXT,
    NULL::TEXT, 'посевы'::TEXT,
    NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT,
    NULL::TEXT, NULL::TEXT, NULL::TEXT,
    NULL::TEXT,
    dst.leads_source_type,
    'Заявки'::TEXT,
    kol_vo_zayavok, korr, kval, priezd, prodazhi,
    nekorr, ne_otvechaet, filtr, nedozvon, priedet,
    NULL::BIGINT, NULL::BIGINT,
    NULL::TEXT,
    "Специалист",
    gs_st.site_type,
    NULL::TEXT,
    REPLACE(REPLACE(REPLACE("Гео",
        'АЦ на Жукова',   'Автоцентр на Жукова'),
        'АвтоПарк Южный', 'Автопарк Южный'),
        'М-Авто',         'М-авто'),
    "Гео2",
    NULL::TEXT, 'Авто'::TEXT, NULL::TEXT, NULL::TEXT,
    NULLIF(TRIM(gs.project_manager), ''),
    gs.client_id,
    COALESCE(NULLIF(TRIM(gs.sales_manager),''), NULLIF(TRIM(auto.менеджер),'')),
    -- источник никогда не NULL: 1) явное значение из лида,
    -- 2) мода из справочника аккаунтов по utm, 3) суффикс utm (_vk/_max),
    -- 4) дефолт 'telegram' (посевы — преимущественно Telegram).
    -- KOMPLEKS_REFACTOR_REDO_2026-07-09: gsheets источник → 'Посевы_*', направление → 'Комплекс'
    COALESCE(
        CASE WHEN TRIM("Источник") = 'Telegram' THEN 'Посевы_Telegram'
             WHEN TRIM("Источник") = 'VK'       THEN 'Посевы_VK'
             WHEN TRIM("Источник") = 'Max'      THEN 'Посевы_Max'
             ELSE NULLIF(TRIM("Источник"), '') END,
        src_dir.src,
        CASE WHEN LOWER("utm утвержденная") ~ '(_|^)vk($|_|[0-9])'  THEN 'Посевы_VK'
             WHEN LOWER("utm утвержденная") ~ '(_|^)max($|_|[0-9])' THEN 'Посевы_Max'
             ELSE 'Посевы_Telegram' END
    ),
    'Комплекс'::TEXT,
    NULL::TEXT, NULL::TEXT,
    NULL::INTEGER, NULL::INTEGER,
    NULL::TEXT, NULL::INTEGER, NULL::INTEGER,
    -- SUPPLIER_CASCADE_FIX_2026-06-16: каскад восстановления поставщика («Тип закупа»).
    -- 1) Берём явное значение из листа лидов (если есть).
    -- 2) Фоллбэк: cn_site10 (по utm+Сайт, n_tip=1 — однозначно, без дата-гарда).
    -- 3) Фоллбэк: cn_utm10 (только по utm, n_tip=1 — однозначно, без дата-гарда).
    -- Строки с уже заполненным поставщиком — COALESCE вернёт его первым (нет изменений).
    -- Строки с конфликтом n_tip≥2 (1777_stvrp, kazan_da) — оба JOIN не сматчатся → NULL.
    COALESCE(
        NULLIF(TRIM("Тип закупа"), ''),
        cn_site10.tip,
        cn_utm10.tip
    ),
    'crop_targeting'::TEXT
"""

# ── API SELECT body (начиная с "День недели", колонки из api lead-таблицы) ───
# Все колонки из lead-таблицы квалифицированы через alias t

_API_SELECT_BODY = """
    CASE EXTRACT(ISODOW FROM t."Date")
        WHEN 1 THEN '1_Понедельник' WHEN 2 THEN '2_Вторник' WHEN 3 THEN '3_Среда'
        WHEN 4 THEN '4_Четверг'    WHEN 5 THEN '5_Пятница'  WHEN 6 THEN '6_Суббота'
        WHEN 7 THEN '7_Воскресенье'
    END,
    DATE_TRUNC('week', t."Date")::DATE,
    NULL::BIGINT, t."CampaignName", NULL::BIGINT, NULL::TEXT,
    NULL::TEXT, NULL::TEXT, NULL::BIGINT, NULL::BIGINT,
    t.total_cost,
    t.domain,
    NULL::BIGINT, NULL::TEXT,
    -- campaign_code='Посевы', tp/adgroup_code=NULL, cpc_cpa/site_quiz='посевы'
    'Посевы'::TEXT, NULL::TEXT, 'посевы'::TEXT, 'посевы'::TEXT, NULL::TEXT,
    NULL::TEXT, 'посевы'::TEXT,
    NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT,
    NULL::TEXT, NULL::TEXT, NULL::TEXT,
    NULL::TEXT,
    NULL::TEXT,
    'Заявки'::TEXT,
    t.kol_vo_zayavok, t.korr, t.kval, t.priezd, t.prodazhi,
    t.nekorr, t.ne_otvechaet, t.filtr, t.nedozvon, t.priedet,
    NULL::BIGINT, NULL::BIGINT,
    t."статус",
    t."специалист",
    t."тип_сайта",
    t."шаблон",
    t."салон",
    t."город",
    t."регион",
    'Авто'::TEXT,
    NULL::TEXT, NULL::TEXT,
    NULLIF(TRIM(gs.project_manager), ''),
    gs.client_id,
    COALESCE(NULLIF(TRIM(gs.sales_manager),''), NULLIF(TRIM(auto.менеджер),'')),
    -- KOMPLEKS_REFACTOR_REDO_2026-07-09: API источник → 'Посевы_*', направление → 'Комплекс'
    CASE t.источник
        WHEN 'telegram'  THEN 'Посевы_Telegram'
        WHEN 'VK'        THEN 'Посевы_VK'
        WHEN 'Max'       THEN 'Посевы_Max'
        WHEN 'instagram' THEN 'Посевы_Telegram'
        ELSE COALESCE('Посевы_' || NULLIF(t.источник, ''), 'Посевы_Telegram')
    END,
    'Комплекс'::TEXT,
    NULL::TEXT, NULL::TEXT,
    NULL::INTEGER, NULL::INTEGER,
    NULL::TEXT, NULL::INTEGER, NULL::INTEGER,
    t.поставщик,
    'crop_targeting'::TEXT
"""

DELETE_CROP_SQL = "DELETE FROM public." + CROP_TABLE + " WHERE _source_table = 'crop_targeting'"

INSERT_GSHEETS_SQL = (
    "INSERT INTO public." + CROP_TABLE + " (" + _COLS + ")\n"
    "SELECT\n"
    "    NULL::TEXT,\n"
    "    TO_DATE(NULLIF(TRIM(\"Дата\"), ''), 'DD.MM.YYYY'),"
    + _GSHEETS_SELECT_BODY
    + "\nFROM public." + GSHEETS_TABLE
    + _GSHEETS_REESTR_JOIN
    + "\nWHERE TO_DATE(NULLIF(TRIM(\"Дата\"), ''), 'DD.MM.YYYY') >= '2026-01-01'"
    + "\n  AND TO_DATE(NULLIF(TRIM(\"Дата\"), ''), 'DD.MM.YYYY') < '2026-05-01'"
)

INSERT_API_SQL = (
    "INSERT INTO public." + CROP_TABLE + " (" + _COLS + ")\n"
    "SELECT\n"
    "    NULL::TEXT,\n"
    "    t.\"Date\","
    + _API_SELECT_BODY
    + "\nFROM public." + API_TABLE + " t"
    + _API_REESTR_JOIN
    + "\nWHERE t.\"Date\" >= '2026-05-01'"
)

# ── FIX A: лид-онли строки для посев-заявок ПОСЛЕ мая без сматченного заказа ──
# (POSEV_LOST_LEADS_AFTER_MAY, plan §4 Фикс A)
#
# ПРОБЛЕМА (доказано read-only, I0 = 556/500/56): витрина посевов после мая
# строится ОТ ЗАКАЗА telega (load_telega_in_orders: 1 строка = 1 заказ, лид
# только ПРИКЛЕИВАЕТСЯ по 5-полевому ключу). Посевная заявка, по которой НЕТ
# заказа на тот же 5-полевой ключ, строки НЕ порождает ВООБЩЕ (даже cost=0) →
# полная потеря из воронки. На 2026-06-08: 56 валидных посев-лидов / 14 меток /
# 1 продажа / 6 визитов теряются.
#
# ФИКС: достроить витрину лид-онли строками (total_cost=0, kol_vo>0) ровно для
# этих потерянных заявок — агрегируя по метке (utm_campaign + домен), funnel по
# статусам (тот же CASE-набор, что в load_telega_in_orders _materialize_leads_agg).
#
# АНТИ-ЗАДВОЕНИЕ (критично, plan §4 риск):
#   1) created_date >= '2026-05-01' — до-майные gsheets-лиды (которые УЖЕ в
#      воронке через INSERT_GSHEETS_SQL, < 2026-05-01) НЕ затрагиваются.
#   2) strict NOT EXISTS заказа по ПОЛНОМУ 5-полевому ключу (domain + utm_campaign
#      + lpad(utm_content,8,'0') + utm_source + utm_medium) в local_telega_in_orders
#      (status='complete'). Сматченные лиды (= УЖЕ в витрине через INSERT_API_SQL)
#      по построению исключены → ровно множество "56 потерянных", пересечения с
#      order-строками витрины нет. Домен заказа = effective_domain (post_links
#      jsonb->>0 host, fallback order_project_name) — та же логика, что в
#      load_telega_in_orders.
#   3) фильтры posev_leads_raw: utm_medium='posev', utm_campaign непуст,
#      НЕ LeadV (source_name), НЕ pixel (utm_source LIKE 'victory_%').
#
# golden: total_cost=0 → расход 25 422 774.03 НЕ двигается (это телеграм-посевы,
# не Кудерко-Авто; расход уже учтён orphan-стороной). Продажи 47 = Авто/директ
# Кудерко; +1 посевная продажа уходит в воронку посевов, не в Кудерко-Авто.
# I3 (korr>=kval>=priezd>=prodazhi) — funnel агрегируется по статусам, инвариант
# сохраняется (на данных: korr=43 >= priezd=6 >= prodazhi=1).
_KORR_STATUSES = (
    "'Новый','В салоне','Купил','На рассмотрении','В салоне не отмечен','Отказ',"
    "'Не отвечает','Приедет','В работе','Фильтр','Недозвон','Приехал',"
    "'Уточнить по дате','Перезвонить','Отказ клиента','Продажа за наличные',"
    "'Продажа в кредит','Соскок','Консультация','Отказ по банкам','Одобрен банк',"
    "'Одобрено банк','Новая','Заполнить','Новая: Не отвечает','Т. Кредит',"
    "'А. Кредит','Одобрить','Одобрен','Отложенный','В работе - odobrit',"
    "'Перезвонить срочно','Одобренные','Оформленные','Одобрение','Дошел в КО'"
)
_PRIEZD_STATUSES = (
    "'В салоне','В салоне не отмечен','Купил','Приехал','Соскок','Консультация',"
    "'Отказ по банкам','Одобрен банк','Продажа за наличные','Продажа в кредит',"
    "'Одобрить','Одобрен','На рассмотрении','Т. Кредит','А. Кредит',"
    "'Одобренные','Оформленные','Одобрение','Дошел в КО'"
)
_PRODAZHI_STATUSES = (
    "'Купил','Продажа за наличные','Продажа в кредит',"
    "'Т. Кредит','А. Кредит','Оформленные','COMPLETED','Продажа'"
)

INSERT_LOST_LEADS_SQL = (
    "INSERT INTO public." + CROP_TABLE + " (" + _COLS + ")\n"
    + """
WITH posev AS (
    SELECT
        l.id, l.status, l.created_date,
        LOWER(BTRIM(l.utm_campaign))          AS camp,
        lpad(btrim(l.utm_content), 8, '0')    AS content,
        LOWER(TRIM(ld.name))                  AS dom,
        LOWER(BTRIM(l.utm_source))            AS src,
        LOWER(BTRIM(l.utm_medium))            AS med,
        l.utm_source                          AS raw_src,
        l.utm_campaign                        AS raw_camp
    FROM public.local_leads_all l
    JOIN public.local_domains ld ON ld.id = l.domain_id
    WHERE l.utm_medium = 'posev'
      AND l.created_date >= '2026-05-01'
      AND l.utm_campaign IS NOT NULL AND TRIM(l.utm_campaign) <> ''
      AND COALESCE(l.source_name, '') NOT ILIKE '%LeadV%'
      AND l.utm_source NOT LIKE 'victory_%'
),
ord AS (
    SELECT
        LOWER(BTRIM(o.utm_campaign))       AS camp,
        lpad(btrim(o.utm_content), 8, '0') AS content,
        LOWER(BTRIM(o.utm_source))         AS src,
        LOWER(BTRIM(o.utm_medium))         AS med,
        CASE
            WHEN NULLIF(LOWER(TRIM(
                CASE WHEN o.post_links IS NOT NULL AND o.post_links LIKE '[%'
                     THEN SUBSTRING((o.post_links::jsonb->>0) FROM '://([^/?]+)')
                     ELSE SUBSTRING(COALESCE(o.post_link, '') FROM '://([^/?]+)') END
            )), '') IS NOT NULL
             AND LOWER(TRIM(
                CASE WHEN o.post_links IS NOT NULL AND o.post_links LIKE '[%'
                     THEN SUBSTRING((o.post_links::jsonb->>0) FROM '://([^/?]+)')
                     ELSE SUBSTRING(COALESCE(o.post_link, '') FROM '://([^/?]+)') END
            )) NOT IN ('telega.io', 'max.ru', 't.me')
            THEN LOWER(TRIM(
                CASE WHEN o.post_links IS NOT NULL AND o.post_links LIKE '[%'
                     THEN SUBSTRING((o.post_links::jsonb->>0) FROM '://([^/?]+)')
                     ELSE SUBSTRING(COALESCE(o.post_link, '') FROM '://([^/?]+)') END
            ))
            ELSE LOWER(TRIM(SPLIT_PART(o.order_project_name, ' ', 1)))
        END AS dom
    FROM public.local_telega_in_orders o
    WHERE o.status = 'complete'
),
lost AS (
    SELECT p.*
    FROM posev p
    WHERE NOT EXISTS (
        SELECT 1 FROM ord o
        WHERE o.camp = p.camp AND o.content = p.content AND o.dom = p.dom
          AND o.src = p.src AND o.med = p.med
    )
),
agg AS (
    SELECT
        dom, raw_camp,
        MIN(created_date)                              AS dt,
        COUNT(*)                                       AS kol_vo_zayavok,
        SUM(CASE WHEN status IN (""" + _KORR_STATUSES + """) THEN 1 ELSE 0 END)     AS korr,
        SUM(CASE WHEN status IN (""" + _PRIEZD_STATUSES + """) THEN 1 ELSE 0 END)   AS priezd,
        SUM(CASE WHEN status IN (""" + _PRODAZHI_STATUSES + """) THEN 1 ELSE 0 END) AS prodazhi,
        SUM(CASE WHEN status IN ('Некорректные данные','Корзина','Повтор','Нет данных','Дубль','***','Спам','Хлам','Отбракованные','Общие вопросы') THEN 1 ELSE 0 END) AS nekorr,
        SUM(CASE WHEN status IN ('Не отвечает','Новая: Не отвечает') THEN 1 ELSE 0 END) AS ne_otvechaet,
        SUM(CASE WHEN status = 'Фильтр'   THEN 1 ELSE 0 END) AS filtr,
        SUM(CASE WHEN status = 'Недозвон' THEN 1 ELSE 0 END) AS nedozvon,
        SUM(CASE WHEN status = 'Приедет'  THEN 1 ELSE 0 END) AS priedet,
        MAX(raw_src)                                   AS raw_src
    FROM lost
    GROUP BY dom, raw_camp
)
SELECT
    NULL::TEXT,
    a.dt,
    CASE EXTRACT(ISODOW FROM a.dt)
        WHEN 1 THEN '1_Понедельник' WHEN 2 THEN '2_Вторник' WHEN 3 THEN '3_Среда'
        WHEN 4 THEN '4_Четверг'    WHEN 5 THEN '5_Пятница'  WHEN 6 THEN '6_Суббота'
        WHEN 7 THEN '7_Воскресенье'
    END,
    DATE_TRUNC('week', a.dt)::DATE,
    NULL::BIGINT, a.raw_camp, NULL::BIGINT, NULL::TEXT,
    NULL::TEXT, NULL::TEXT, NULL::BIGINT, NULL::BIGINT,
    0::NUMERIC,                                       -- total_cost = 0 (лид-онли)
    a.dom,
    NULL::BIGINT, NULL::TEXT,
    -- campaign_code='Посевы', tp/adgroup_code=NULL, cpc_cpa/site_quiz='посевы'
    'Посевы'::TEXT, NULL::TEXT, 'посевы'::TEXT, 'посевы'::TEXT, NULL::TEXT,
    NULL::TEXT, 'посевы'::TEXT,
    NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT,
    NULL::TEXT, NULL::TEXT, NULL::TEXT,
    NULL::TEXT,
    NULL::TEXT,
    'Заявки'::TEXT,
    a.kol_vo_zayavok,
    a.korr,
    a.korr - a.ne_otvechaet - a.filtr - a.nedozvon,  -- kval
    a.priezd, a.prodazhi,
    a.nekorr, a.ne_otvechaet, a.filtr, a.nedozvon, a.priedet,
    NULL::BIGINT, NULL::BIGINT,
    gs.status, gs.directologist, gs.site_type, gs.template, gs.salon, gs.city, gs.region,
    'Авто'::TEXT,
    NULL::TEXT, NULL::TEXT,
    NULL::TEXT, NULL::TEXT, NULL::TEXT,
    -- KOMPLEKS_REFACTOR_REDO_2026-07-09: _FIX_A источник → 'Посевы_*', направление → 'Комплекс'
    CASE
        WHEN LOWER(a.raw_src) = 'max'      THEN 'Посевы_Max'
        WHEN LOWER(a.raw_src) = 'vk'       THEN 'Посевы_VK'
        WHEN LOWER(a.raw_camp) ~ '(_|^)vk($|_|[0-9])'  THEN 'Посевы_VK'
        WHEN LOWER(a.raw_camp) ~ '(_|^)max($|_|[0-9])' THEN 'Посевы_Max'
        WHEN LOWER(a.raw_src) = 'telegram' THEN 'Посевы_Telegram'
        ELSE COALESCE(NULLIF('Посевы_' || a.raw_src, 'Посевы_'), 'Посевы_Telegram')
    END,
    'Комплекс'::TEXT,
    NULL::TEXT, NULL::TEXT,
    NULL::INTEGER, NULL::INTEGER,
    NULL::TEXT, NULL::INTEGER, NULL::INTEGER,
    'Telega IN'::TEXT,
    'crop_targeting'::TEXT
FROM agg a
LEFT JOIN LATERAL (
    SELECT gs2.status, gs2.directologist, gs2.site_type, gs2.template,
           gs2.salon, gs2.city, gs2.region
    FROM public.local_gsheet_sites gs2
    WHERE LOWER(TRIM(gs2.domain)) = a.dom
    LIMIT 1
) gs ON TRUE
"""
)

DELETE_FULL_SQL = "DELETE FROM public." + TARGET_TABLE + " WHERE _source_table = 'crop_targeting'"

INSERT_FULL_SQL = (
    "INSERT INTO public." + TARGET_TABLE + " (" + _COLS + ")\n"
    "SELECT " + _COLS + "\n"
    "FROM public." + CROP_TABLE + "\n"
    "WHERE _source_table = 'crop_targeting'"
)

BACKFILL_CRM_SQL = """
UPDATE public.big_analytics_full f
SET "Название crm" = src.crm_name
FROM (
    SELECT "салон", MAX("Название crm") AS crm_name
    FROM public.big_analytics_full
    WHERE "Название crm" IS NOT NULL
      AND "Название crm" NOT IN ('отзывы', 'посевы')
    GROUP BY "салон"
) src
WHERE f."Название crm" IS NULL
  AND f."салон" = src."салон"
"""

# Fallback backfill по домену: когда "салон" IS NULL и в других строках того же домена CRM известна.
BACKFILL_CRM_BY_DOMAIN_SQL = """
UPDATE public.big_analytics_full f
SET "Название crm" = src.crm_name
FROM (
    SELECT domain, MAX("Название crm") AS crm_name
    FROM public.big_analytics_full
    WHERE _source_table != 'crop_targeting'
      AND "Название crm" IS NOT NULL
      AND "Название crm" NOT IN ('отзывы', 'посевы')
      AND domain IS NOT NULL
    GROUP BY domain
) src
WHERE f._source_table = 'crop_targeting'
  AND f."Название crm" IS NULL
  AND f.domain = src.domain
"""


def _connect():
    return psycopg2.connect(**DB_DST)


def main():
    conn = _connect()
    try:
        with conn.cursor() as cur:
            print('Удаляем crop_targeting из ' + CROP_TABLE + '...')
            cur.execute(DELETE_CROP_SQL)
            print('  Удалено: ' + str(cur.rowcount))

            print('Вставляем gsheets (до мая) → ' + CROP_TABLE + '...')
            cur.execute(INSERT_GSHEETS_SQL)
            n_gsheets = cur.rowcount
            print('  Вставлено gsheets: ' + str(n_gsheets))

            print('Вставляем api (с мая) → ' + CROP_TABLE + '...')
            cur.execute(INSERT_API_SQL)
            n_api = cur.rowcount
            print('  Вставлено API: ' + str(n_api))

            # FIX A: лид-онли строки для посев-заявок после мая БЕЗ заказа telega
            # (cost=0, kol_vo>0). strict 5-полевой NOT EXISTS → задвоения нет.
            print('Вставляем lost-leads (после мая, без заказа, cost=0) → ' + CROP_TABLE + '...')
            cur.execute(INSERT_LOST_LEADS_SQL)
            n_lost = cur.rowcount
            print('  Вставлено lost-leads: ' + str(n_lost))

        conn.commit()

        with conn.cursor() as cur:
            print('Удаляем crop_targeting из ' + TARGET_TABLE + '...')
            cur.execute(DELETE_FULL_SQL)
            print('  Удалено: ' + str(cur.rowcount))

            print('Вставляем ' + CROP_TABLE + ' → ' + TARGET_TABLE + '...')
            cur.execute(INSERT_FULL_SQL)
            inserted = cur.rowcount
            print('  Вставлено: ' + str(inserted))

            print('Заполняем NULL "Название crm" по салону...')
            cur.execute(BACKFILL_CRM_SQL)
            print('  Заполнено CRM: ' + str(cur.rowcount))

            print('Заполняем NULL "Название crm" по домену (fallback)...')
            cur.execute(BACKFILL_CRM_BY_DOMAIN_SQL)
            print('  Заполнено CRM по домену: ' + str(cur.rowcount))

        conn.commit()

        try:
            import sys, os
            sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            import corrections as corr_mod
            n_fix = corr_mod._fix_crop_missing_utms(conn)
            print('  Backfill UTM (салон/город): ' + str(n_fix))
        except Exception as e:
            print('  WARNING: _fix_crop_missing_utms пропущен: ' + str(e))

        print(
            'OK: gsheets=' + str(n_gsheets) + ', api=' + str(n_api)
            + ', lost_leads=' + str(n_lost)
            + ' → ' + CROP_TABLE
            + '; ' + str(inserted) + ' → ' + TARGET_TABLE
        )
    finally:
        conn.close()


if __name__ == '__main__':
    main()
