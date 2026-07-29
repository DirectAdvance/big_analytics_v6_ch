"""
step4_build_full/step4.py — сборка big_analytics_full

UNION ALL всех источников + звонки как отдельные строки.
Строится как UNLOGGED → SET LOGGED в шаге 5.

Важно: все ветки UNION ALL перечисляют колонки явно — SELECT * запрещён.
"""

import logging
import os
import time

from config.settings import (
    WORK_MEM,
    T_RAW_CALLS,
    T_GSHEET_SITES,
    T_GSHEET_PLAN_FAKT, T_GSHEET_AUTOSALONY,
    T_DIRECT, T_SEO, T_CROP, T_REVIEWS,
    T_FULL,
)
from config.status_sql import load_status_sql

logger = logging.getLogger('pipeline.step4')

# EXPLAIN_CAPTURE_REAL_v2 — маркер патча для grep-проверки доезда на Victory
_EXPLAIN_CAPTURE: bool = os.environ.get('EXPLAIN_CAPTURE', '').strip() == '1'

# Список колонок в точном порядке — должен совпадать с шагом 3
COLS = """
    key3, "Date", "День недели", week_start,
    "CampaignId", "CampaignName", "AdGroupId", "AdGroupName",
    "AdNetworkType", "Device", "Impressions", "Clicks", total_cost, domain,
    "RlAdjustmentId", "RlAdjustmentId_total",
    campaign_code, tp, cpc_cpa, site_quiz, adgroup_code,
    account_login, manager_login,
    ag_part1, ag_part2, ag_part3, ag_part4, ag_part5, ag_part6, ag_part7,
    "марки авто", "Название crm", тип_заявки,
    kol_vo_zayavok, korr, kval, priezd, prodazhi, nekorr, ne_otvechaet, filtr, nedozvon, priedet,
    dohod_do_kredita, dobro,
    "статус", "специалист", "тип_сайта", "шаблон", "салон", "город", "регион",
    direction,
    "неверный_кодер_new", fid,
    проджект, id_салона, менеджер,
    источник, направление,
    "номер кампании | название кампании",
    "номер группы | название группы",
    "План заявки", "План приезда",
    "аккаунт|сайт",
    priezd_arrival_date, prodazhi_arrival_date,
    поставщик, _source_table
"""

# Композитный ключ для связи с analytics_pixel_score в Power BI.
# Формат: "Date|domain|источник|CampaignId" (NULL → '').
# Только для big_analytics_full — в компонентных таблицах не нужен.
KEY_PIXEL_SCORE_EXPR = (
    """COALESCE("Date"::TEXT, '') || '|' || """
    """COALESCE(domain, '') || '|' || """
    """COALESCE(источник, '') || '|' || """
    """COALESCE("CampaignId"::TEXT, '')"""
)

# Inner UNION ALL: rename campaign col to avoid duplicate in outer wrapper SELECT
_INNER_COLS = COLS.replace(
    '"номер кампании | название кампании"',
    '"номер кампании | название кампании" AS _raw_cam_name',
)


def _make_outer_cols() -> str:
    """Outer SELECT column list: inner_t. prefix + emoji-prefix CASE for campaign name + cs.campaign_status."""
    _TARGET = '"номер кампании | название кампании"'
    _CASE = (
        "CASE\n"
        "        WHEN cs.campaign_status = 'Активна'     AND inner_t._raw_cam_name IS NOT NULL\n"
        "            THEN '🟢 ' || inner_t._raw_cam_name\n"
        "        WHEN cs.campaign_status = 'Остановлена' AND inner_t._raw_cam_name IS NOT NULL\n"
        "            THEN '🟡 ' || inner_t._raw_cam_name\n"
        "        WHEN inner_t._raw_cam_name IS NOT NULL\n"
        "            THEN '⚪ ' || inner_t._raw_cam_name\n"
        "        ELSE NULL\n"
        f"    END AS {_TARGET}"
    )
    parts = []
    for col in COLS.replace('\n', ' ').split(','):
        col = col.strip()
        if not col:
            continue
        if col == _TARGET:
            parts.append(_CASE)
        else:
            parts.append(f'inner_t.{col}')
    parts.append('inner_t.key_pixel_score')
    parts.append('cs.campaign_status')
    parts.append('cs.payment_model')
    return ',\n    '.join(parts)


def _build_full_select_sql(calls_agg_cases: str) -> str:
    """Чистый SELECT для big_analytics_full — без CREATE/INSERT обёртки.

    Используется в двух местах:
      1. CREATE UNLOGGED TABLE ... AS (...) WHERE FALSE — создание пустой схемы (если таблицы нет)
      2. INSERT INTO ... (...) — основная загрузка после TRUNCATE
    """
    outer_cols = _make_outer_cols()
    return f"""
SELECT {outer_cols}
FROM (

-- ── Директ ────────────────────────────────────────────────────────────────
SELECT {_INNER_COLS}, {KEY_PIXEL_SCORE_EXPR} AS key_pixel_score
FROM {T_DIRECT} WHERE direction = 'Авто'

UNION ALL

-- ── Посевы ────────────────────────────────────────────────────────────────
SELECT {_INNER_COLS}, {KEY_PIXEL_SCORE_EXPR} AS key_pixel_score
FROM {T_CROP} WHERE direction = 'Авто'

UNION ALL

-- ── SEO ───────────────────────────────────────────────────────────────────
SELECT {_INNER_COLS}, {KEY_PIXEL_SCORE_EXPR} AS key_pixel_score
FROM {T_SEO} WHERE direction = 'Авто'

UNION ALL

-- ── Отзывы (кампании отзывов из yandex_direct_reports_reviews) ────────────
SELECT {_INNER_COLS}, {KEY_PIXEL_SCORE_EXPR} AS key_pixel_score
FROM {T_REVIEWS} WHERE direction = 'Авто'

-- Пиксели (T_PIXEL) больше не вливаются сюда напрямую — атрибуция в step11
-- вставляет big_analytics_pixel_score → big_analytics_full с _source_table='пиксель_атрибуц'.

UNION ALL

-- ── Звонки (inline — своей таблицы нет) ──────────────────────────────────
SELECT
    NULL::TEXT      AS key3,
    c.created_date::date AS "Date",
    CASE EXTRACT(ISODOW FROM c.created_date::date)
        WHEN 1 THEN '1_Понедельник' WHEN 2 THEN '2_Вторник'  WHEN 3 THEN '3_Среда'
        WHEN 4 THEN '4_Четверг'    WHEN 5 THEN '5_Пятница'   WHEN 6 THEN '6_Суббота'
        WHEN 7 THEN '7_Воскресенье'
    END             AS "День недели",
    DATE_TRUNC('week', c.created_date::date)::date AS week_start,
    NULL::BIGINT AS "CampaignId",   'звонки'::TEXT AS "CampaignName",
    NULL::BIGINT AS "AdGroupId",    'звонки'::TEXT AS "AdGroupName",
    'Звонки'::TEXT AS "AdNetworkType",
    'звонки'::TEXT AS "Device",
    NULL::BIGINT AS "Impressions",  NULL::BIGINT AS "Clicks",
    NULL::NUMERIC AS total_cost,
    LOWER(TRIM(c.domain)) AS domain,
    NULL::BIGINT AS "RlAdjustmentId",
    NULL::TEXT AS "RlAdjustmentId_total",
    'Звонки'::TEXT AS campaign_code, 'звонки'::TEXT AS tp,
    'звонки'::TEXT AS cpc_cpa,       'звонки'::TEXT AS site_quiz,
    'звонки'::TEXT AS adgroup_code,
    gs.login_key    AS account_login,
    amm.manager_login,
    'звонки'::TEXT AS ag_part1, 'звонки'::TEXT AS ag_part2,
    'звонки'::TEXT AS ag_part3, 'звонки'::TEXT AS ag_part4,
    'звонки'::TEXT AS ag_part5, 'звонки'::TEXT AS ag_part6,
    'звонки'::TEXT AS ag_part7,
    ''::TEXT AS "марки авто",
    dst.leads_source_type   AS "Название crm",
    'Звонки'::TEXT          AS тип_заявки,
    -- агрегаты звонков (динамически из local_crm_statuses)
    {calls_agg_cases},
    gs."status"  AS "статус", gs."directologist" AS "специалист", gs."site_type" AS "тип_сайта",
    gs."template" AS "шаблон", gs."salon" AS "салон", gs."city" AS "город", gs."region" AS "регион",
    gs."direction" AS direction,
    NULL::TEXT AS "неверный_кодер_new",
    NULL::TEXT AS fid,
    NULLIF(TRIM(gs.project_manager), '') AS проджект,
    gs.client_id AS id_салона,
    COALESCE(NULLIF(TRIM(gs.sales_manager),''), NULLIF(TRIM(auto.менеджер),'')) AS менеджер,
    'Звонки'::TEXT AS источник,
    NULL::TEXT AS направление,
    NULL::TEXT AS _raw_cam_name,
    NULL::TEXT AS "номер группы | название группы",
    CASE WHEN DATE_TRUNC('month', c.created_date::date) = DATE_TRUNC('month', CURRENT_DATE)
         THEN NULLIF(REPLACE(REPLACE(REPLACE(pf."цена_заявки", chr(160), ''), ' ', ''), ',', '.'), '-')::NUMERIC::INTEGER
         ELSE NULL END AS "План заявки",
    CASE WHEN DATE_TRUNC('month', c.created_date::date) = DATE_TRUNC('month', CURRENT_DATE)
         THEN NULLIF(REPLACE(REPLACE(REPLACE(pf."цена_приезда", chr(160), ''), ' ', ''), ',', '.'), '-')::NUMERIC::INTEGER
         ELSE NULL END AS "План приезда",
    gs.login_key || '|' || LOWER(TRIM(c.domain)) AS "аккаунт|сайт",
    NULL::INTEGER AS priezd_arrival_date,
    NULL::INTEGER AS prodazhi_arrival_date,
    'звонки'::TEXT AS поставщик,
    'calls'::TEXT  AS _source_table,
    COALESCE(c.created_date::date::TEXT, '') || '|' ||
    COALESCE(LOWER(TRIM(c.domain)), '') || '|' ||
    'звонки' || '|' || ''                          AS key_pixel_score

FROM {T_RAW_CALLS} c
LEFT JOIN {T_GSHEET_SITES}       gs    ON LOWER(TRIM(c.domain)) = LOWER(TRIM(gs."domain"))
LEFT JOIN (
    -- Приоритет CRM по домену: Маркар=1 > Мега=2 > Фаиг=3 > Плекс=4
    -- (MAX по Unicode давал неверный результат: 'Плекс' > 'Маркар')
    SELECT domain_name,
           (ARRAY_AGG(leads_source_type ORDER BY crm_priority))[1] AS leads_source_type
    FROM (
        SELECT LOWER(TRIM(domain)) AS domain_name,
               CASE source_type
                   WHEN 'marcar_crm_excel' THEN 'Маркар'
                   WHEN 'mega_crm_excel'   THEN 'Мега'
                   WHEN 'crmf_excel'       THEN 'Фаиг'
                   WHEN 'plex_excel'       THEN 'Плекс'
                   WHEN 'redauto_excel'    THEN 'Ред Авто'
                   WHEN 'genzes_excel'     THEN 'Генезис'
                   WHEN 'mauto_excel'      THEN 'МаАвто'
                   ELSE source_type
               END AS leads_source_type,
               CASE source_type
                   WHEN 'marcar_crm_excel' THEN 1
                   WHEN 'mega_crm_excel'   THEN 2
                   WHEN 'crmf_excel'       THEN 3
                   WHEN 'plex_excel'       THEN 4
                   WHEN 'redauto_excel'    THEN 5
                   WHEN 'genzes_excel'     THEN 6
                   WHEN 'mauto_excel'      THEN 7
                   ELSE 9
               END AS crm_priority
        FROM {T_RAW_CALLS}
        WHERE domain IS NOT NULL AND domain != ''
    ) u GROUP BY domain_name
) dst ON LOWER(TRIM(c.domain)) = dst.domain_name
LEFT JOIN (
    SELECT account_login, MAX(manager_login) AS manager_login
    FROM {T_DIRECT} WHERE manager_login IS NOT NULL GROUP BY account_login
) amm ON gs.login_key = amm.account_login
LEFT JOIN {T_GSHEET_AUTOSALONY}  auto   ON gs.client_id = auto.id_салона AND gs.client_id IS NOT NULL
LEFT JOIN {T_GSHEET_PLAN_FAKT}   pf     ON LOWER(TRIM(gs."salon"))     = LOWER(TRIM(pf."салон"))
                                        AND LOWER(TRIM(gs."site_type")) = LOWER(TRIM(pf."тип"))
WHERE gs."domain" IS NOT NULL
  AND gs."direction" = 'Авто'
  AND LOWER(TRIM(c.domain)) NOT IN (
    SELECT DISTINCT LOWER(TRIM("Сайт"))
    FROM public.gsheets_crop_targeting_account
    WHERE "Сайт" IS NOT NULL AND TRIM("Сайт") != ''
  )
GROUP BY
    c.created_date::date, LOWER(TRIM(c.domain)),
    gs.login_key, gs."status", gs."directologist", gs."site_type",
    gs."template", gs."salon", gs."city", gs."region",
    gs."direction",
    gs.project_manager, gs.client_id,
    gs.sales_manager, auto.менеджер,
    amm.manager_login, dst.leads_source_type,
    pf."цена_заявки", pf."цена_приезда"

) inner_t
LEFT JOIN campaign_status cs ON inner_t."CampaignId" = cs."CampaignId"
"""


def _patch_telega_io_specialist(conn) -> int:
    """Патч специалиста/салона для crop_targeting строк с domain='telega.io'.

    Telega IN (Max-каналы) не имеют домена сайта — JOIN к gsheet_sites по domain
    не срабатывает при сборке big_analytics_full. Маппинг идёт через
    local_telega_in_orders.order_project_name (первое слово = домен сайта).

    Условие domain='telega.io' строгое — не затрагивает другие crop_targeting строки.
    """
    sql = f"""
        WITH tio_all_domains AS (
            SELECT DISTINCT
                t.channel_link,
                TRIM(SPLIT_PART(t.order_project_name, ' ', 1)) AS extracted_domain
            FROM public.local_telega_in_orders t
            WHERE t.order_project_name IS NOT NULL
              AND LENGTH(TRIM(SPLIT_PART(t.order_project_name, ' ', 1))) > 4
        ),
        matched AS (
            SELECT DISTINCT ON (m.channel_link)
                m.channel_link,
                gs.directologist,
                gs.salon,
                gs.city,
                gs.region
            FROM tio_all_domains m
            JOIN {T_GSHEET_SITES} gs
                ON LOWER(TRIM(gs.domain)) = LOWER(m.extracted_domain)
            ORDER BY m.channel_link, m.extracted_domain
        )
        UPDATE {T_FULL} bf
        SET
            "специалист" = m.directologist,
            "салон"      = COALESCE(bf."салон", m.salon),
            "город"      = COALESCE(bf."город", m.city),
            "регион"     = COALESCE(bf."регион", m.region)
        FROM matched m
        WHERE bf."CampaignName" = m.channel_link
          AND bf._source_table = 'crop_targeting'
          AND bf.domain = 'telega.io'
          AND (bf."специалист" IS NULL OR bf."специалист" = '')
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        n = cur.rowcount
    conn.commit()
    return n


def _patch_kuderko_calls_specialist(conn) -> int:
    # FIX-KUDERKO-CALLS-SALES-2026-06-11
    """Переназначает специалиста ЗВОНКОВ на 'Кудерко Семен' для его аккаунтов до 10.04.

    Корень бага golden (продажи 42 vs эталон 47): corrections._rule1_kudерко
    переназначает РАСХОД и продажи-лиды Кудерко по account_login только в
    big_analytics_direct (direct/tp8). Но ЗВОНКИ (_source_table='calls')
    собираются inline в step6 ПОСЛЕ corrections и получают специалиста из
    gsheet_sites.directologist по домену. Для доменов, чьи аккаунты переданы
    Кудерко (login_key ∈ _KUDЕРКО_LOGINS), gsheet показывает НОВОГО владельца
    (Терехов/Тумашенко) → звонки-продажи уходили мимо Кудерко (−5 продаж).

    Фикс согласован с rule1: тот же account-based scope (_KUDЕРКО_LOGINS) и тот
    же барьер (_KUDЕРКО_DATE). SEO НЕ трогаем — органика следует за gsheet-
    директологом домена (эталон оставляет 2 seo-продажи за Тереховым/Тумашенко;
    проверено эмуляцией на стабильной звезде: только звонки+direct+tp8 дают
    РОВНО 47, добавление seo → 49 перелёт).
    """
    import corrections as corr_mod
    logins = list(getattr(corr_mod, '_KUDЕРКО_LOGINS'))
    name = getattr(corr_mod, '_KUDЕРКО_NAME')
    date_barrier = getattr(corr_mod, '_KUDЕРКО_DATE')
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {T_FULL}
            SET    "специалист" = %s
            WHERE  _source_table = 'calls'
              AND  "Date" < %s
              AND  account_login = ANY(%s)
            """,
            (name, date_barrier, logins),
        )
        n = cur.rowcount
    conn.commit()
    return n


def run(conn, run_id: str) -> dict:
    logger.info('Шаг 6: сборка big_analytics_full')
    t0 = time.perf_counter()

    st = load_status_sql(conn)

    select_sql = _build_full_select_sql(st['calls_agg_cases'])

    # Безопасная пересборка без двойного пика диска (раньше: DROP + CTAS — пик ~17.6 GB).
    # Новая логика:
    #   1) Если таблица существует — TRUNCATE (мгновенно, без MVCC bloat).
    #   2) Если нет — CREATE UNLOGGED TABLE ... AS (SELECT ... WHERE FALSE).
    #   3) ALTER TABLE SET UNLOGGED (на случай если кто-то перевёл в LOGGED).
    #   4) INSERT INTO ... <select_sql> — единственный пик размером в одну таблицу.
    # При смене схемы между релизами (новая колонка) — выполнить DROP TABLE big_analytics_full вручную.
    # Транзакция 1: TRUNCATE (или CREATE пустой) + commit.
    # Физически освобождает файл таблицы ДО INSERT, иначе пик диска ~2× (~18 GB → DiskFull при free 5.5 GB).
    with conn.cursor() as cur:
        cur.execute(f"SET work_mem = '{WORK_MEM}'")
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE c.relname=%s AND n.nspname='public')",
            (T_FULL,)
        )
        table_exists = cur.fetchone()[0]
        if table_exists:
            cur.execute(f"ALTER TABLE {T_FULL} SET UNLOGGED")
            cur.execute(f"TRUNCATE TABLE {T_FULL}")
            logger.info('Шаг 6: %s существует, TRUNCATE выполнен', T_FULL)
        else:
            create_empty = f"CREATE UNLOGGED TABLE {T_FULL} AS\n{select_sql}\nWHERE FALSE"
            # WHERE FALSE добавляется к ВНЕШНЕМУ SELECT (после LEFT JOIN campaign_status)
            cur.execute(create_empty)
            logger.info('Шаг 6: %s не существовало, создана пустая UNLOGGED таблица', T_FULL)
    conn.commit()
    logger.info('Шаг 6: TRUNCATE/CREATE завершён, файл таблицы физически освобождён')

    # Транзакция 2: INSERT — теперь только один пик ~9 GB (без старых данных).
    with conn.cursor() as cur:
        cur.execute(f"SET work_mem = '{WORK_MEM}'")
        cur.execute(f"INSERT INTO {T_FULL}\n{select_sql}")
    conn.commit()
    logger.info('Шаг 6: INSERT завершён за %.1f сек', time.perf_counter() - t0)

    # ── Индексы перед UPDATE'ами (seq scan x8 → index lookup) ────────────────
    # campaign_status включён в CTAS через LEFT JOIN — ALTER/UPDATE не нужны
    t_idx = time.perf_counter()
    with conn.cursor() as cur:
        cur.execute(f'CREATE INDEX IF NOT EXISTS idx_full_tmp_domain ON {T_FULL} (domain)')
        cur.execute(f'CREATE INDEX IF NOT EXISTS idx_full_tmp_salon ON {T_FULL} ("салон")')
        cur.execute(f'CREATE INDEX IF NOT EXISTS idx_full_tmp_src ON {T_FULL} (_source_table)')
        cur.execute(f'ANALYZE {T_FULL}')
    conn.commit()
    logger.info('Шаг 6: индексы перед UPDATE за %.1f сек', time.perf_counter() - t_idx)

    # ── 1. campaign_status='Активна' для звонков активных доменов ────────────
    with conn.cursor() as cur:
        cur.execute(f"""
            UPDATE {T_FULL} f
            SET campaign_status = 'Активна'
            WHERE f._source_table = 'calls'
              AND f.тип_заявки = 'Звонки'
              AND f.campaign_status IS DISTINCT FROM 'Активна'
              AND EXISTS (
                  SELECT 1 FROM {T_FULL} f2
                  WHERE f2._source_table = 'direct'
                    AND f2.domain = f.domain
                    AND f2.campaign_status = 'Активна'
              )
        """)
        n_calls = cur.rowcount
    conn.commit()
    logger.info('Шаг 6: campaign_status звонки → Активна: %d строк', n_calls)

    # ── 3. направление='Комплекс' + источник по gs.status для звонков ───────────────
    # KOMPLEKS_REFACTOR_REDO_2026-07-09:
    # направление всегда 'Комплекс'; источник = SEO Flow / SEO / Контекст (детализация).
    # Обычные звонки (Контекст) → источник='Контекст', посевные звонки обработаны в 3c.
    with conn.cursor() as cur:
        cur.execute(f"""
            UPDATE {T_FULL} f
            SET направление = 'Комплекс',
                источник = CASE gs.status
                    WHEN 'SEO Flow'        THEN 'SEO Flow'
                    WHEN 'SEO'             THEN 'SEO'
                    ELSE 'Контекст'
                END
            FROM {T_GSHEET_SITES} gs
            WHERE f._source_table = 'calls'
              AND f.направление IS NULL
              AND LOWER(TRIM(gs.domain)) = f.domain
              AND gs.direction = 'Авто'
        """)
        n_dir = cur.rowcount
    conn.commit()
    logger.info('Шаг 6: направление по gs.status для звонков: %d строк', n_dir)

    # ── 3b. направление='Комплекс' для direct_zero / direct_unmatched ───────
    # KOMPLEKS_REFACTOR_REDO_2026-07-09
    with conn.cursor() as cur:
        cur.execute(f"""
            UPDATE {T_FULL} f
            SET направление = 'Комплекс'
            WHERE f._source_table IN ('direct_zero', 'direct_unmatched')
              AND f.направление IS NULL
              AND EXISTS (
                  SELECT 1 FROM {T_GSHEET_SITES} gs
                  WHERE LOWER(TRIM(gs.domain)) = f.domain
              )
        """)
        n_dir_zero = cur.rowcount
    conn.commit()
    logger.info('Шаг 6: направление=Контекст для direct_zero/unmatched: %d строк', n_dir_zero)

    # ── 3c. направление для звонков на доменах с посевами ────────────────────
    # FIX4-REWORK (POSEV_INFECTION_UPDATE3C_DIRMAIN): звонок красится в 'посевы'
    # ТОЛЬКО если домен РЕАЛЬНО посевной по справочнику сайтов
    # (local_gsheet_sites.direction_main = 'Посевы') И засеян В ТОТ ЖЕ МЕСЯЦ.
    #
    # Корень бага (диагностика anton_sql, 2026-06-08): прошлый предикат (только
    # crop.domain + месяц + kol_vo>0) красил в посевы 279 звонко-продаж, из них
    # 257 = ЗАРАЖЕНИЕ: домены с direction_main='Контекст', которые в тот же месяц
    # крутят tp8-посев, поэтому проходили crop-условие. Реально посевных доменов
    # (direction_main='Посевы') среди них — только 22 продажи.
    #
    # Добавлен предикат gs.direction_main = 'Посевы' через JOIN на local_gsheet_sites:
    # теперь крашение в 'посевы' возможно лишь для доменов, у которых в справочнике
    # сайтов основное направление = Посевы. Привязка к месяцу+kol_vo>0 (crop)
    # сохранена — звонок остаётся посевным только если домен засеян в ЭТОТ месяц.
    #
    # ПОРЯДОК: этот UPDATE 3c идёт ПОСЛЕ UPDATE 3 (направление по gs.status:
    # SEO Flow/SEO/иначе Контекст). 257 освобождённых звонков уже покрашены
    # UPDATE 3 в Контекст (домены 'Авто' в local_gsheet_sites) и НЕ перекрашиваются
    # здесь обратно в посевы.
    #
    # golden 47 НЕ двигается (меняем только направление, не _source_table/
    # специалист/продажу). Эффект на данных: calls-посевы продажи 279→~22.
    # KOMPLEKS_REFACTOR_REDO_2026-07-09: SET направление='Комплекс', источник='Посевы_Звонки'.
    # EXISTS фильтр переключён с ct.направление='посевы' на ct._source_table='crop_targeting'
    # (после рефактора crop-строки имеют направление='Комплекс', не 'посевы').
    with conn.cursor() as cur:
        cur.execute(f"""
            UPDATE {T_FULL} f
            SET направление = 'Комплекс',
                источник    = 'Посевы_Звонки'
            FROM {T_GSHEET_SITES} gs
            WHERE f._source_table = 'calls'
              AND LOWER(TRIM(gs.domain)) = f.domain
              AND gs.direction_main = 'Посевы'
              AND EXISTS (
                  SELECT 1 FROM {T_CROP} ct
                  WHERE LOWER(TRIM(ct.domain)) = f.domain
                    AND ct._source_table = 'crop_targeting'
                    AND DATE_TRUNC('month', ct."Date") = DATE_TRUNC('month', f."Date")
                    AND COALESCE(ct.kol_vo_zayavok, 0) > 0
              )
              -- VK_CALLS_2026-07-10: ЯВНЫЙ приоритет VK > посевы (решение Семёна).
              -- Звонок на VK-Авто-домене НЕ красится в Посевы_Звонки — он уйдёт в
              -- VK Ads (UPDATE 3d ниже). Выражено ТЕМ ЖЕ приёмом, что «контекст>посевы»:
              -- посевная краска бьёт только по direction_main='Посевы', а контекстные
              -- домены исключены самим этим предикатом. Здесь VK-Авто-домен исключается
              -- явным NOT EXISTS, чтобы приоритет VK>посевы держался УСЛОВИЕМ, а не
              -- только порядком блоков 3c→3d. Признак VK-домена — зеркало vk_sites (step3 :2184-2186).
              AND NOT EXISTS (
                  SELECT 1 FROM {T_GSHEET_SITES} gsv
                  WHERE LOWER(TRIM(gsv.domain)) = f.domain
                    AND NULLIF(TRIM(gsv.vk_client_id), '') IS NOT NULL
                    AND gsv.direction = 'Авто'
              )
        """)
        n_calls_posev = cur.rowcount
    conn.commit()
    logger.info('Шаг 6: направление=Комплекс/источник=Посевы_Звонки для звонков (посевные домены): %d строк', n_calls_posev)

    # ── 3d. Звонки на VK-Авто-доменах → канал VK Ads ─────────────────────────
    # VK_CALLS_2026-07-10: бизнес-правило Семёна «один домен = один платный источник».
    # Домен, у которого в local_gsheet_sites заполнен vk_client_id И direction='Авто',
    # — это VK-Авто-домен (тот же признак, по которому VK-блок step3
    # `_add_vk_ads_to_crop_sql` забирает безметочные web-заявки в vk_sites, :2184-2186).
    # Его ЗВОНКИ тоже должны идти в VK-воронку, а не в общий «Звонки»/«Контекст».
    #
    # Это ПЕРЕКРАСКА той же строки звонка (_source_table='calls', тип_заявки='Звонки'),
    # НЕ вставка новой строки → двойного учёта нет. Аналог исключения безметочных
    # web-заявок VK_ADS_LEADS_EXCLUSION_2026-07-07, но для calls-оси: строка была
    # покрашена UPDATE 3 в источник='Контекст', здесь перекрашивается в источник='VK Ads'
    # → одновременно ИСЧЕЗАЕТ из «Контекст»/«Звонки» и ПОЯВЛЯЕТСЯ в VK-воронке.
    #
    # Расход не трогаем: у звонка total_cost=NULL (строки расхода VK идут отдельно
    # из local_vk_ads_stats_day в step3, _source_table='vk_ads') → строки расхода не
    # размножаются. Воронка (korr/kval/priezd/prodazhi) звонка уже посчитана
    # calls_agg_cases и остаётся на строке — звонок входит в VK-воронку как есть.
    #
    # ПОРЯДОК/ПРИОРИТЕТ: приоритет VK > посевы выражен ЯВНО в UPDATE 3c
    # (NOT EXISTS(vk-site) исключает VK-Авто-домены из Посевы_Звонки) — по образцу
    # того как «контекст>посевы» держится предикатом direction_main='Посевы'.
    # Поэтому VK-Авто-домен приходит сюда с источником='Контекст' (от UPDATE 3),
    # НЕ 'Посевы_Звонки', и здесь финально перекрашивается в VK Ads. Приоритет
    # держится УСЛОВИЕМ, а не только порядком блоков 3c→3d (решение Семёна 2026-07-10).
    #
    # Идентификация домена — точное зеркало vk_sites (step3 :2184-2186):
    #   NULLIF(TRIM(vk_client_id),'') IS NOT NULL AND direction='Авто'.
    # golden Кудерко НЕ двигается: звонки входят в golden по _source_table='calls'
    # (не по источнику), а VK-Авто-домены не принадлежат Кудерко. На текущих данных
    # VK-доменов со звонками 0 → регрессия нулевая, правка структурная.
    with conn.cursor() as cur:
        cur.execute(f"""
            UPDATE {T_FULL} f
            SET направление = 'Комплекс',
                источник    = 'VK Ads',
                поставщик   = 'ВК Реклама'
            WHERE f._source_table = 'calls'
              AND EXISTS (
                  SELECT 1 FROM {T_GSHEET_SITES} gs
                  WHERE LOWER(TRIM(gs.domain)) = f.domain
                    AND NULLIF(TRIM(gs.vk_client_id), '') IS NOT NULL
                    AND gs.direction = 'Авто'
              )
        """)
        n_calls_vk = cur.rowcount
    conn.commit()
    logger.info('Шаг 6: источник=VK Ads/поставщик=ВК Реклама для звонков (VK-Авто-домены): %d строк', n_calls_vk)

    # ── 4. Объединённый UPDATE по салону: "Название crm" + manager_login + проджект ──
    # Один проход вместо трёх — вычисляем все три агрегата сразу.
    with conn.cursor() as cur:
        cur.execute(f"""
            DROP TABLE IF EXISTS _tmp_salon_aggs;
            CREATE UNLOGGED TABLE _tmp_salon_aggs AS
            SELECT
                "салон",
                MAX("Название crm") FILTER (
                    WHERE "Название crm" IS NOT NULL
                      AND "Название crm" NOT IN ('отзывы', 'посевы')
                ) AS crm_name,
                MAX(manager_login) FILTER (
                    WHERE manager_login IS NOT NULL
                      AND manager_login NOT IN ('отзывы', 'посевы', 'пиксель')
                ) AS mgr_real,
                MAX(проджект) FILTER (
                    WHERE проджект IS NOT NULL
                ) AS proj
            FROM {T_FULL}
            WHERE "салон" IS NOT NULL
            GROUP BY "салон";
            CREATE INDEX ON _tmp_salon_aggs ("салон");
            ANALYZE _tmp_salon_aggs;
        """)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(f"""
            UPDATE {T_FULL} f
            SET
                "Название crm" = COALESCE(f."Название crm", s.crm_name),
                manager_login  = CASE
                    WHEN (f.manager_login IS NULL OR f.manager_login IN ('отзывы', 'посевы'))
                     AND NOT (f._source_table = 'crop_targeting' AND COALESCE(f.поставщик, '') != 'Яндекс')
                        THEN COALESCE(s.mgr_real, f.manager_login)
                    ELSE f.manager_login
                END,
                проджект       = COALESCE(f.проджект, s.proj)
            FROM _tmp_salon_aggs s
            WHERE f."салон" = s."салон"
              AND f."салон" IS NOT NULL
              AND (
                   (f."Название crm" IS NULL AND s.crm_name IS NOT NULL)
                OR (f.проджект IS NULL AND s.proj IS NOT NULL)
                OR ((f.manager_login IS NULL OR f.manager_login IN ('отзывы', 'посевы'))
                    AND NOT (f._source_table = 'crop_targeting' AND COALESCE(f.поставщик, '') != 'Яндекс')
                    AND s.mgr_real IS NOT NULL)
              )
        """)
        n_salon = cur.rowcount
        cur.execute('DROP TABLE IF EXISTS _tmp_salon_aggs')
    conn.commit()
    logger.info('Шаг 6: salon-агрегаты (CRM + mgr + проджект): %d строк', n_salon)

    # ── 4b. Fallback "Название crm" через local_gsheet_sites.crm ─────────────
    # Когда у домена нет лидов (raw_leads пуст) — source_type неизвестен,
    # CRM остаётся NULL даже после salon-агрегата (нечего наследовать внутри салона).
    # Берём CRM из sites по названию салона (DISTINCT — sites имеет много строк на салон).
    with conn.cursor() as cur:
        cur.execute(f"""
            UPDATE {T_FULL} f
            SET "Название crm" = CASE gs.crm
                WHEN 'PLEX'         THEN 'Плекс'
                WHEN 'MEGA CRM'     THEN 'Мега'
                WHEN 'MarCar CRM'   THEN 'Маркар'
                WHEN 'mauto_excel'  THEN 'МаАвто'    -- CRM_NAME_MAPPING_2026-07-10
                WHEN 'genzes_excel' THEN 'Генезис'   -- CRM_NAME_MAPPING_2026-07-10
                WHEN 'redauto_excel' THEN 'Ред Авто' -- CRM_NAME_MAPPING_2026-07-10
                WHEN ''             THEN 'Не указана' -- CRM_NAME_MAPPING_2026-07-10
                ELSE NULLIF(TRIM(gs.crm), '')
            END
            FROM (
                SELECT DISTINCT salon, crm FROM public.local_gsheet_sites
                WHERE salon IS NOT NULL AND crm IS NOT NULL
            ) gs
            WHERE f."Название crm" IS NULL
              AND f."салон" IS NOT NULL
              AND LOWER(TRIM(f."салон")) = LOWER(TRIM(gs.salon))
        """)
        n_reestr = cur.rowcount
    conn.commit()
    logger.info('Шаг 6: CRM из sites (домены без лидов): %d строк', n_reestr)

    # ── 4c. ПЕРЕНЕСЕНО (CRM_NAME_MAPPING_2026-07-10) ──────────────────────────
    # Финальная заливка "Название crm" NULL/пустая → 'Не указана' перенесена в
    # build_unified.py (после доливок load_reviews/load_crop/step11 обеих осей).
    # step6 отрабатывает ДО доливок → их пустые строки обходили бы заливку здесь.

    # ── 5. manager_login по домену (отдельный проход — ключ другой) ─────────
    with conn.cursor() as cur:
        cur.execute(f"""
            UPDATE {T_FULL} f
            SET manager_login = src.mgr
            FROM (
                SELECT domain, MAX(manager_login) AS mgr
                FROM {T_FULL}
                WHERE manager_login IS NOT NULL
                  AND manager_login NOT IN ('отзывы', 'посевы', 'пиксель')
                  AND domain IS NOT NULL
                GROUP BY domain
            ) src
            WHERE f.manager_login IS NULL
              AND f.domain = src.domain
              AND f.domain IS NOT NULL
        """)
        n_mgr = cur.rowcount
    conn.commit()
    logger.info('Шаг 6: manager_login заполнен по домену: %d строк', n_mgr)

    # ── 5b. Патч специалиста для telega.io (Telega IN / Max-каналы) ──────────
    # Max-каналы имеют domain='telega.io', JOIN к gsheet_sites по domain не работает.
    # Маппинг через local_telega_in_orders.order_project_name → домен → gsheet_sites.
    n_tio = _patch_telega_io_specialist(conn)
    logger.info('Шаг 6: специалист telega.io (Max-каналы): %d строк', n_tio)

    # ── 6. источник по директологу + SEO-звонки (один UPDATE) ────────────────
    with conn.cursor() as cur:
        cur.execute(f"""
            UPDATE {T_FULL} f
            SET источник = CASE
                WHEN gs.status IN ('SEO', 'SEO Flow') THEN 'SEO'
                ELSE 'Контекст'
            END
            FROM {T_GSHEET_SITES} gs
            WHERE LOWER(TRIM(gs.domain)) = f.domain
              AND gs.directologist IS NOT NULL AND gs.directologist != ''
              AND f."специалист" IS NOT NULL AND f."специалист" != ''
              AND (f.источник IS NULL OR f.источник = '')
        """)
        n_src = cur.rowcount
    conn.commit()
    logger.info('Шаг 6: источник заполнен по директологу: %d строк', n_src)

    with conn.cursor() as cur:
        cur.execute(f"""
            UPDATE {T_FULL} f
            SET источник = 'SEO'
            FROM {T_GSHEET_SITES} gs
            WHERE LOWER(TRIM(gs.domain)) = f.domain
              AND gs.status IN ('SEO', 'SEO Flow')
              AND f._source_table = 'calls'
              AND (f.источник IS NULL OR f.источник = '')
        """)
        n_seo = cur.rowcount
    conn.commit()
    logger.info('Шаг 6: источник=SEO для звонков: %d строк', n_seo)

    # ── 6b. fallback: источник='Контекст' для direct_zero/unmatched без источника ──
    # KOMPLEKS_REFACTOR_REDO_2026-07-09: раньше было источник=f.направление, но после
    # рефактора f.направление='Комплекс' — не подходит как источник. Явно 'Контекст'.
    with conn.cursor() as cur:
        cur.execute(f"""
            UPDATE {T_FULL} f
            SET источник = 'Контекст'
            WHERE f._source_table IN ('direct_zero', 'direct_unmatched')
              AND (f.источник IS NULL OR f.источник = '')
              AND f.направление IS NOT NULL
        """)
        n_src_fallback = cur.rowcount
    conn.commit()
    logger.info('Шаг 6: источник fallback direct_zero/unmatched: %d строк', n_src_fallback)

    # ── 6c. Кудерко: переназначение специалиста ЗВОНКОВ ──────────────────────
    # FIX-KUDERKO-CALLS-SALES-2026-06-11
    # Звонки собираются inline ВЫШЕ (после corrections.rule1) и получают
    # специалиста из gsheet по домену; для переданных Кудерко аккаунтов это
    # давало рассинхрон со спендом (см. _patch_kuderko_calls_specialist).
    n_kud_calls = _patch_kuderko_calls_specialist(conn)
    logger.info('Шаг 6: специалист звонков Кудерко (account-based до 10.04): %d строк', n_kud_calls)

    # ── Снести временные индексы (step7 создаст финальные с правильными именами) ─
    with conn.cursor() as cur:
        cur.execute(f'DROP INDEX IF EXISTS idx_full_tmp_domain')
        cur.execute(f'DROP INDEX IF EXISTS idx_full_tmp_salon')
        cur.execute(f'DROP INDEX IF EXISTS idx_full_tmp_src')
    conn.commit()

    # ── LZ4_FULL_2026-06-17: lz4-сжатие TEXT-колонок big_analytics_full ────────
    # big_analytics_full создаётся каждый прогон через DROP+CREATE UNLOGGED CTAS
    # (step6), поэтому SET COMPRESSION навешивается здесь, после каждого CREATE.
    # Эффект сразу: CTAS уже наполнил таблицу, но эти данные не перепишутся
    # (только новые INSERT после TRUNCATE следующего прогона). Реальная экономия
    # — начиная со СЛЕДУЮЩЕГО прогона. step7 SET LOGGED не перезаписывает данные
    # (меняет только persistence-флаг + WAL overhead) → sжатие сохраняется.
    # На big_analytics_full ~5 GB ожидается -40-50% TEXT-хранилища.
    # Downstream (step7/step8/step11/step13) только читают через SELECT — lz4 прозрачен.
    _TEXT_COLS_FULL = [
        'key3', '"День недели"', '"CampaignName"', '"AdGroupName"',
        '"AdNetworkType"', '"Device"', '"RlAdjustmentId_total"',
        'campaign_code', 'tp', 'cpc_cpa', 'site_quiz', 'adgroup_code',
        'account_login', 'manager_login',
        'ag_part1', 'ag_part2', 'ag_part3', 'ag_part4',
        'ag_part5', 'ag_part6', 'ag_part7',
        '"марки авто"', '"Название crm"', 'тип_заявки',
        '"статус"', '"специалист"', '"тип_сайта"', '"шаблон"',
        '"салон"', '"город"', '"регион"', 'domain',
        'direction', '"неверный_кодер_new"', 'fid',
        'проджект', 'id_салона', 'менеджер',
        'источник', 'направление', 'key_pixel_score',
        '"номер кампании | название кампании"',
        '"номер группы | название группы"',
        '"аккаунт|сайт"', 'поставщик', '_source_table',
        'campaign_status', 'payment_model',
    ]
    try:
        with conn.cursor() as cur:
            for col in _TEXT_COLS_FULL:
                cur.execute(
                    f'ALTER TABLE {T_FULL} '
                    f'ALTER COLUMN {col} SET COMPRESSION lz4'
                )
        conn.commit()
        logger.info('  LZ4: сжатие lz4 навешено на %d TEXT-колонок %s', len(_TEXT_COLS_FULL), T_FULL)
    except Exception as _lz4_err:
        logger.warning('  LZ4: не удалось навесить lz4 на %s: %s (продолжаем)', T_FULL, _lz4_err)
        try:
            conn.rollback()
        except Exception:
            pass

    # ── EARLY_TRUNCATE_RAW_CALLS_2026-06-17: raw_calls больше не нужен ─────────
    # raw_calls читается ТОЛЬКО в step6 (звонки inline в UNION ALL — строки 199+).
    # К этой точке звонки уже материализованы в big_analytics_full.
    # step7/step8/step11/step13 raw_calls НЕ читают. Ранний TRUNCATE освобождает
    # место ДО step7 SET LOGGED (самый дисковый момент прогона — WAL overhead).
    # raw_calls пересоздаётся step1 (DROP+CREATE UNLOGGED) при следующем прогоне.
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_total_relation_size('public.raw_calls')")
            _calls_bytes = cur.fetchone()[0] or 0
        with conn.cursor() as cur:
            cur.execute(f'TRUNCATE TABLE {T_RAW_CALLS}')
        conn.commit()
        _calls_mb = _calls_bytes / 1024 / 1024
        logger.info('  EARLY TRUNCATE raw_calls: освобождено ~%.1f MB', _calls_mb)
    except Exception as _trunc_err:
        logger.warning('  EARLY TRUNCATE raw_calls: не удалось: %s (продолжаем)', _trunc_err)
        try:
            conn.rollback()
        except Exception:
            pass

    with conn.cursor() as cur:
        cur.execute(f'SELECT COUNT(*) FROM {T_FULL}')
        rows = cur.fetchone()[0]

    elapsed = time.perf_counter() - t0
    logger.info('Шаг 6 завершён: %d строк за %.1f сек', rows, elapsed)
    return {'rows': rows, 'details': f'{T_FULL}: {rows:,} строк'}


def get_explain_sql(conn) -> str:
    """SELECT-эквивалент для EXPLAIN ANALYZE тяжёлого UNION ALL step6.

    Используется explain_capture при EXPLAIN_CAPTURE=1. Таблица big_analytics_full
    уже собрана к моменту вызова (после run()). Запрос анализирует полный скан
    результирующей таблицы — покрывает стоимость чтения всего big_analytics_full.
    Для плана INSERT INTO SELECT нужна бы была оригинальная SELECT-часть, но она
    динамически генерируется через _build_full_select_sql(). Здесь снимаем профиль
    физического доступа к таблице post-factum.
    """
    return f"""
        SELECT
            _source_table,
            COUNT(*)            AS rows,
            SUM(total_cost)     AS total_cost,
            SUM(kol_vo_zayavok) AS kol_vo,
            SUM(prodazhi)       AS prodazhi
        FROM {T_FULL}
        GROUP BY _source_table
        ORDER BY rows DESC
    """
