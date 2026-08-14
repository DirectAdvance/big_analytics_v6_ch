"""
direct_account_reviews/load_reviews_to_big_analytics.py

Добавляет данные из public.yandex_direct_reports_reviews в public.big_analytics_full.

JOIN с public.direct_account_reviews по login = аккаунт.
Перед вставкой удаляет строки с направление = 'отзывы'.

ВАЖНО: запускать только после полного завершения пайплайна (все шаги 0–7).

Запуск:
    python load_reviews_to_big_analytics.py
"""

import psycopg2
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from config.settings import DB_DST

SOURCE_TABLE  = 'yandex_direct_reports_reviews'
REVIEWS_TABLE = 'yandex_direct_account_reviews'
TARGET_TABLE  = 'big_analytics_full'
DIREKTOLOG    = 'Караваев Михаил'


def _connect():
    return psycopg2.connect(**DB_DST)


DELETE_SQL = f"DELETE FROM public.{TARGET_TABLE} WHERE направление = 'Отзывы'"  # KOMPLEKS_REFACTOR_REDO_2026-07-09

INSERT_SQL = rf"""
INSERT INTO public.{TARGET_TABLE} (
    key3,
    "Date",
    "День недели",
    week_start,
    "CampaignId",
    "CampaignName",
    "AdGroupId",
    "AdGroupName",
    "AdNetworkType",
    "Device",
    "Impressions",
    "Clicks",
    total_cost,
    domain,
    "RlAdjustmentId",
    "RlAdjustmentId_total",
    campaign_code,
    tp,
    cpc_cpa,
    site_quiz,
    adgroup_code,
    account_login,
    manager_login,
    ag_part1, ag_part2, ag_part3, ag_part4, ag_part5, ag_part6, ag_part7,
    "марки авто",
    "Название crm",
    тип_заявки,
    kol_vo_zayavok,
    korr,
    kval,
    priezd,
    prodazhi,
    nekorr,
    ne_otvechaet,
    filtr,
    nedozvon,
    priedet,
    dohod_do_kredita,
    dobro,
    "статус",
    "специалист",
    "тип_сайта",
    "шаблон",
    "салон",
    "город",
    "регион",
    direction,
    "неверный_кодер_new",
    fid,
    проджект,
    id_салона,
    менеджер,
    источник,
    направление,
    "номер кампании | название кампании",
    "номер группы | название группы",
    "План заявки",
    "План приезда",
    "аккаунт|сайт",
    priezd_arrival_date,
    prodazhi_arrival_date,
    поставщик,
    _source_table
)
SELECT
    NULL::TEXT,                                                            -- key3
    r."Date",                                                              -- Date
    CASE EXTRACT(ISODOW FROM r."Date")
        WHEN 1 THEN '1_Понедельник' WHEN 2 THEN '2_Вторник'  WHEN 3 THEN '3_Среда'
        WHEN 4 THEN '4_Четверг'    WHEN 5 THEN '5_Пятница'   WHEN 6 THEN '6_Суббота'
        WHEN 7 THEN '7_Воскресенье'
    END,                                                                   -- День недели
    DATE_TRUNC('week', r."Date")::date,                                    -- week_start
    r."CampaignId",
    r."CampaignName",
    r."AdGroupId",
    r."AdGroupName",
    -- REVIEWS_CAPITALIZE_2026-07-10: 'SEARCH' (API Direct) → 'Отзывы'
    CASE r."AdNetworkType" WHEN 'SEARCH' THEN 'Отзывы' ELSE r."AdNetworkType" END,
    r."Device",
    r."Impressions",
    r."Clicks",
    r."Cost",                                                              -- total_cost
    d.сайт,                                                                -- домен
    r."RlAdjustmentId",
    NULL::TEXT,                                                            -- RlAdjustmentId_total
    'Отзывы'::TEXT,                                                        -- campaign_code
    NULL::TEXT,                                                            -- tp
    NULL::TEXT,                                                            -- cpc_cpa
    NULL::TEXT,                                                            -- site_quiz
    NULL::TEXT,                                                            -- adgroup_code
    r.login,                                                               -- account_login
    'отзывы'::TEXT,                                                        -- manager_login
    NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT,
    NULL::TEXT, NULL::TEXT, NULL::TEXT,                                    -- ag_part1..7
    NULL::TEXT,                                                            -- марки авто
    'отзывы'::TEXT,                                                        -- Название crm
    'Отзывы'::TEXT,                                                        -- тип_заявки -- REVIEWS_CAPITALIZE_2026-07-10
    NULL::BIGINT,                                                          -- kol_vo_zayavok
    NULL::BIGINT,                                                          -- korr
    NULL::BIGINT,                                                          -- kval
    NULL::BIGINT,                                                          -- priezd
    NULL::BIGINT,                                                          -- prodazhi
    NULL::BIGINT,                                                          -- nekorr
    NULL::BIGINT,                                                          -- ne_otvechaet
    NULL::BIGINT,                                                          -- filtr
    NULL::BIGINT,                                                          -- nedozvon
    NULL::BIGINT,                                                          -- priedet
    NULL::BIGINT,                                                          -- dohod_do_kredita
    NULL::BIGINT,                                                          -- dobro
    NULL::TEXT,                                                            -- статус
    '{DIREKTOLOG}'::TEXT,                                                  -- директолог
    gs."site_type",                                                        -- тип_сайта (вариант B: из справочника local_gsheet_sites по домену; нет домена → NULL/Неопределено)
    'отзывы'::TEXT,                                                        -- шаблон
    d.салон,                                                               -- салон
    d.город,                                                               -- город
    gs."region",                                                           -- регион
    'Авто'::TEXT,                                                          -- direction (хардкод: отзывы всегда Авто)
    NULL::TEXT,                                                            -- неверный_кодер_new
    NULL::TEXT,                                                            -- fid
    NULLIF(TRIM(COALESCE(NULLIF(TRIM(gs.project_manager), ''), NULLIF(TRIM(c.project_manager), ''))), ''), -- проджект (gs по домену → c по салону)
    gs.client_id,                                                          -- id_салона
    COALESCE(NULLIF(TRIM(gs.sales_manager), ''), NULLIF(TRIM(c.sales_manager), '')), -- менеджер (gs по домену → c по салону)
    'Контекст'::TEXT,                                                      -- источник
    'Отзывы'::TEXT,                                                        -- направление  KOMPLEKS_REFACTOR_REDO_2026-07-09
    r."CampaignId"::TEXT || '|' || COALESCE(r."CampaignName", ''),        -- номер кампании | название кампании
    r."AdGroupId"::TEXT  || '|' || COALESCE(r."AdGroupName", ''),         -- номер группы | название группы
    NULL::INTEGER,                                                         -- План заявки
    NULL::INTEGER,                                                         -- План приезда
    r.login || '|' || COALESCE(d.сайт, ''),                               -- аккаунт|сайт
    NULL::INTEGER,                                                         -- priezd_arrival_date
    NULL::INTEGER,                                                         -- prodazhi_arrival_date
    'Яндекс'::TEXT,                                                        -- поставщик
    'direct_account_reviews'::TEXT                                         -- _source_table
FROM public.{SOURCE_TABLE} r
LEFT JOIN public.{REVIEWS_TABLE} d ON d.аккаунт = r.login
-- сайт→проджект/менеджер: нормализуем домен (срезаем http(s)://, www., завершающий /)
LEFT JOIN public.local_gsheet_sites gs
    ON LOWER(TRIM(regexp_replace(d.сайт,   '^https?://(www\.)?|/$', '', 'g')))
     = LOWER(TRIM(regexp_replace(gs.domain, '^https?://(www\.)?|/$', '', 'g')))
-- салон→проджект/менеджер (fallback когда домен не сматчился). ВНИМАНИЕ: ключ — ЛАТИНСКАЯ
-- колонка c.salon (заполнена 262/287), кириллические salon/id_салона — пустой legacy.
-- WORD-SORT КЛЮЧ: устойчив к ПЕРЕСТАНОВКЕ СЛОВ в названии салона (частая ошибка данных).
--   Нормализация: lower → trim → пунктуация в пробел → схлопнуть пробелы → разбить на слова →
--   ОТСОРТИРОВАТЬ слова по алфавиту → склеить. Тогда 'Центр Авто Казань' и 'Казань Центр Авто'
--   дают ОДИН ключ 'авто казань центр'. Детерминированно (НЕ fuzzy/Левенштейн). Коллизии на
--   справочнике проверены = 0 (ни один разный c.salon не схлопывается в один ключ, 2026-06-10).
LEFT JOIN public.local_gsheet_autosalony_clients c
    ON array_to_string(ARRAY(
         SELECT t.w FROM unnest(string_to_array(
           regexp_replace(regexp_replace(LOWER(TRIM(c.salon)),  '[^[:alnum:][:space:]]', ' ', 'g'), '\s+', ' ', 'g'), ' '
         )) AS t(w) WHERE t.w <> '' ORDER BY t.w), ' ')
     = array_to_string(ARRAY(
         SELECT t.w FROM unnest(string_to_array(
           regexp_replace(regexp_replace(LOWER(TRIM(d.салон)), '[^[:alnum:][:space:]]', ' ', 'g'), '\s+', ' ', 'g'), ' '
         )) AS t(w) WHERE t.w <> '' ORDER BY t.w), ' ')
   AND NULLIF(TRIM(d.салон), '') IS NOT NULL
"""


def main():
    conn = _connect()
    try:
        with conn.cursor() as cur:
            print(f'Удаляем строки направление=отзывы из {TARGET_TABLE}...')
            cur.execute(DELETE_SQL)
            deleted = cur.rowcount
            print(f'  Удалено: {deleted}')

            print(f'Вставляем из {SOURCE_TABLE}...')
            cur.execute(INSERT_SQL)
            inserted = cur.rowcount
            print(f'  Вставлено: {inserted}')

        conn.commit()
        print(f'OK: {inserted} строк загружено в {TARGET_TABLE}')
    finally:
        conn.close()


if __name__ == '__main__':
    main()
