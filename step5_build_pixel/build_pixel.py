"""
pixel/build_pixel.py — построение pixel_leads + pixel_leads_check + INSERT в big_analytics_pixel.

Шаг 5 пайплайна (после step3, до step6_build_full).

Источник: local_leads_all (синхронизируется в step0 из leads_all).

Таблицы:
  pixel_leads — только с валидными доменами (есть в local_gsheet_sites)
  pixel_leads_check — остальное (для проверки, в pipeline не используется)
  big_analytics_pixel — агрегированные данные из pixel_leads

domain = utm_source (домен сайта для пикселей).

total_cost = kol_vo_zayavok * COALESCE(cost_per_lead, cost_total, 0)
  Если cost_per_lead заполнен — используем его, иначе cost_total, иначе 0.
"""

import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config.db as db_module
from config.status_sql import load_status_sql
from config.settings import T_PIXEL, T_GSHEET_SITES, T_GSHEET_AUTOSALONY

logger = logging.getLogger('pipeline.step5')

T_PIXEL_LEADS = 'pixel_leads'
T_PIXEL_LEADS_CHECK = 'pixel_leads_check'
T_PIXEL_CONFIG = 'local_pixel_config'
T_PIXEL_PRICE_HISTORY = 'local_pixel_price_history'
T_LEADS_ALL    = 'local_leads_all'
T_GSHEET_SITES = 'local_gsheet_sites'
T_GSHEET_AUTOSALONY = 'local_gsheet_autosalony_clients'

# PIXEL_PR_SALON_OVERRIDES_2026-07-10: жёсткий override салона для конкретных pixel_pr доменов.
# Значения — ЭТАЛОН из справочника (вкладка «Проекты пиксель», согласовано с Семёном 2026-07-10):
# написание 1:1 из вкладки (регистр/дефисы/пробелы не менять). Эталон побеждает per-lead salon из
# local_leads_all (там народные имена менеджеров, размазаны по салонам → per-lead MAX() ненадёжен).
# Домены без override (напр. генерик victory_pixel_pr — смешанные салоны) → обычный per-lead salon.
PIXEL_PR_SALON_OVERRIDES: dict = {
    'victory_mavto_pixel_pr':    'МАвто',
    'victory_urbancar_pixel_pr': 'СКА моторс',
    'victory_premier_pixel_pr':  'Премьер',
    'victory_uralauto_pixel_pr': 'Урал Авто',
    'victory_vershina_pixel_pr': 'Авиньон',
    'victory_carcity_pixel_pr':  'Кар сити',
}


def _pixel_pr_salon_case() -> str:
    """SQL CASE для поля салон в pixel_pr строках.

    Приоритет:
      1. Явный override из PIXEL_PR_SALON_OVERRIDES (побеждает per-lead данные).
      2. Реальный салон из local_leads_all (pixel_salon_raw = MAX(NULLIF(salon,''))).
      3. Фолбэк 'Перформ РФ' если salon пуст.
      4. Для не-pixel_pr доменов — gs.salon (как раньше).
    """
    override_branches = ''.join(
        f"\n    WHEN agg.domain = '{domain}' THEN '{salon}'"
        for domain, salon in PIXEL_PR_SALON_OVERRIDES.items()
    )
    return (
        "CASE"
        + override_branches
        + "\n    WHEN agg.domain LIKE '%pixel\\_pr' ESCAPE '\\'"
        + "\n         THEN COALESCE(NULLIF(agg.pixel_salon_raw, ''), 'Перформ РФ')"
        + "\n    ELSE gs.salon"
        + "\n    END"
    )


def _build_pixel_leads_sql() -> str:
    return f"""
-- ── Промежуточная таблица: все пиксели + домены ──────────────────────────
DROP TABLE IF EXISTS _pixel_leads_raw;
CREATE UNLOGGED TABLE _pixel_leads_raw AS
SELECT
    l.id,
    l.created_date::date          AS created_date,
    l.status,
    COALESCE(l.reason, '')        AS reason,
    COALESCE(l.source_type, '')   AS source_type,
    l.salon,
    pc.pixel_name,
    LOWER(TRIM(l.utm_source))     AS domain,
    -- Дата-эффективная цена: override из history по дате лида замещает baseline-пару целиком,
    -- иначе берём baseline из local_pixel_config (как раньше).
    CASE WHEN ov.pixel_name IS NOT NULL THEN ov.cost_total    ELSE pc.cost_total    END AS cost_total,
    CASE WHEN ov.pixel_name IS NOT NULL THEN ov.cost_per_lead ELSE pc.cost_per_lead END AS cost_per_lead
FROM {T_LEADS_ALL} l
JOIN {T_PIXEL_CONFIG} pc ON (
    l.source_name = pc.pixel_name
    OR LOWER(l.utm_source) = LOWER(pc.pixel_name)
)
LEFT JOIN LATERAL (
    SELECT h.pixel_name, h.cost_per_lead, h.cost_total
    FROM {T_PIXEL_PRICE_HISTORY} h
    WHERE h.pixel_name = pc.pixel_name
      AND h.valid_from <= l.created_date::date
      AND (h.valid_to IS NULL OR l.created_date::date <= h.valid_to)
    ORDER BY h.valid_from DESC
    LIMIT 1
) ov ON TRUE
WHERE l.is_copy_for_removal IS NOT TRUE;

CREATE INDEX ON _pixel_leads_raw (domain);

-- ── Валидные домены из gsheet (для быстрого поиска) ───────────────────────
DROP TABLE IF EXISTS _valid_domains;
CREATE UNLOGGED TABLE _valid_domains AS
SELECT DISTINCT LOWER(TRIM("domain")) AS domain
FROM {T_GSHEET_SITES}
WHERE "domain" IS NOT NULL AND TRIM("domain") != '';

CREATE INDEX ON _valid_domains (domain);

-- ── pixel_leads: только с валидными доменами ───────────────────────────────
DROP TABLE IF EXISTS {T_PIXEL_LEADS};
CREATE UNLOGGED TABLE {T_PIXEL_LEADS} AS
SELECT
    raw.id, raw.created_date, raw.status, raw.reason, raw.source_type,
    raw.salon, raw.pixel_name, raw.domain, raw.cost_total, raw.cost_per_lead
FROM _pixel_leads_raw raw
WHERE raw.domain IS NOT NULL AND raw.domain != ''
  AND raw.domain IN (SELECT domain FROM _valid_domains);

CREATE INDEX ON {T_PIXEL_LEADS} (domain, created_date);
ANALYZE {T_PIXEL_LEADS};

-- ── pixel_leads_check: остальное (для проверки) ───────────────────────────
DROP TABLE IF EXISTS {T_PIXEL_LEADS_CHECK};
CREATE UNLOGGED TABLE {T_PIXEL_LEADS_CHECK} AS
SELECT
    raw.id, raw.created_date, raw.status, raw.reason, raw.source_type,
    raw.salon, raw.pixel_name, raw.domain, raw.cost_total, raw.cost_per_lead
FROM _pixel_leads_raw raw
WHERE raw.domain IS NULL OR raw.domain = ''
   OR raw.domain NOT IN (SELECT domain FROM _valid_domains);

CREATE INDEX ON {T_PIXEL_LEADS_CHECK} (domain, created_date);
ANALYZE {T_PIXEL_LEADS_CHECK};

DROP TABLE _pixel_leads_raw;
DROP TABLE _valid_domains;
"""


def _build_insert_sql(leads_agg_cases: str) -> str:
    salon_case = _pixel_pr_salon_case()  # PIXEL_PR_SALON_FIX_2026-07-10
    return f"""
TRUNCATE TABLE {T_PIXEL};

INSERT INTO {T_PIXEL} (
    key3, "Date", "День недели", week_start,
    "CampaignId", "CampaignName", "AdGroupId", "AdGroupName",
    "AdNetworkType", "Device", "Impressions", "Clicks",
    total_cost, domain,
    "RlAdjustmentId", "RlAdjustmentId_total",
    campaign_code, tp, cpc_cpa, site_quiz, adgroup_code,
    account_login, manager_login,
    ag_part1, ag_part2, ag_part3, ag_part4, ag_part5, ag_part6, ag_part7,
    "марки авто", "Название crm", тип_заявки,
    kol_vo_zayavok, korr, kval, priezd, prodazhi,
    nekorr, ne_otvechaet, filtr, nedozvon, priedet,
    dohod_do_kredita, dobro,
    статус, специалист, тип_сайта, шаблон, салон, город, регион,
    direction, неверный_кодер_new, fid, проджект, id_салона,
    менеджер, источник, направление,
    "номер кампании | название кампании",
    "номер группы | название группы",
    "План заявки", "План приезда",
    "аккаунт|сайт",
    priezd_arrival_date, prodazhi_arrival_date,
    поставщик, _source_table
)
SELECT
    NULL::TEXT                                  AS key3,
    agg.created_date                            AS "Date",
    CASE EXTRACT(ISODOW FROM agg.created_date)
        WHEN 1 THEN '1_Понедельник' WHEN 2 THEN '2_Вторник' WHEN 3 THEN '3_Среда'
        WHEN 4 THEN '4_Четверг'    WHEN 5 THEN '5_Пятница'  WHEN 6 THEN '6_Суббота'
        WHEN 7 THEN '7_Воскресенье'
    END                                         AS "День недели",
    DATE_TRUNC('week', agg.created_date)::date  AS week_start,
    NULL::BIGINT                                AS "CampaignId",
    agg.pixel_name                              AS "CampaignName",
    NULL::BIGINT                                AS "AdGroupId",
    agg.pixel_name                              AS "AdGroupName",
    NULL::TEXT   AS "AdNetworkType",
    NULL::TEXT   AS "Device",
    NULL::BIGINT AS "Impressions",
    NULL::BIGINT AS "Clicks",
    agg.kol_vo_zayavok * COALESCE(agg.cost_per_lead, agg.cost_total, 0) AS total_cost,
    agg.domain,
    NULL::BIGINT AS "RlAdjustmentId",
    NULL::TEXT   AS "RlAdjustmentId_total",
    NULL::TEXT   AS campaign_code,
    NULL::TEXT   AS tp,
    NULL::TEXT   AS cpc_cpa,
    NULL::TEXT   AS site_quiz,
    NULL::TEXT   AS adgroup_code,
    'пиксель'::TEXT AS account_login,
    'пиксель'::TEXT AS manager_login,
    NULL::TEXT AS ag_part1, NULL::TEXT AS ag_part2, NULL::TEXT AS ag_part3,
    NULL::TEXT AS ag_part4, NULL::TEXT AS ag_part5, NULL::TEXT AS ag_part6, NULL::TEXT AS ag_part7,
    ''::TEXT        AS "марки авто",
    CASE agg.source_type
        WHEN 'crmf_excel'       THEN 'Фаиг'
        WHEN 'plex_excel'       THEN 'Плекс'
        WHEN 'mega_crm_excel'   THEN 'Мега'
        WHEN 'marcar_crm_excel' THEN 'Маркар'
        WHEN 'redauto_excel'    THEN 'Ред Авто'  -- CRM_MAPPING_PIXEL_2026-07-10
        WHEN 'genzes_excel'     THEN 'Генезис'   -- CRM_MAPPING_PIXEL_2026-07-10
        WHEN 'mauto_excel'      THEN 'МаАвто'    -- CRM_MAPPING_PIXEL_2026-07-10
        ELSE NULLIF(agg.source_type, '')
    END                                         AS "Название crm",
    'Заявки'::TEXT  AS тип_заявки,
    agg.kol_vo_zayavok, agg.korr, agg.kval, agg.priezd, agg.prodazhi,
    agg.nekorr, agg.ne_otvechaet, agg.filtr, agg.nedozvon, agg.priedet,
    agg.dohod_do_kredita, agg.dobro,
    NULL::TEXT              AS статус,
    gs.directologist        AS специалист,
    gs.site_type            AS тип_сайта,
    gs.template             AS шаблон,
    -- PIXEL_PR_SALON_FIX_2026-07-10: override dict → per-lead salon → 'Перформ РФ' → gs.salon.
    -- Перформ Директ (_source_table='direct'/tp8/tp9) — НЕ затронут (отдельная ветка в build_star).
    {salon_case}                     AS салон,
    gs.city                 AS город,
    gs.region               AS регион,
    gs.direction            AS direction,
    NULL::TEXT              AS неверный_кодер_new,
    NULL::TEXT              AS fid,
    NULLIF(TRIM(gs.project_manager), '')        AS проджект,
    gs.client_id                                AS id_салона,
    COALESCE(NULLIF(TRIM(gs.sales_manager),''),
             NULLIF(TRIM(auto.менеджер),''))    AS менеджер,
    'Пиксель'::TEXT AS источник,          -- KOMPLEKS_REFACTOR_REDO_2026-07-09
    -- PIXEL_PR_2026-07-09: pixel_pr домены Перформа → направление='Перформ'.
    -- corrections.py::apply() работает до step5 (COMPONENT_TABLES), pixel данных ещё нет,
    -- поэтому направление задаётся здесь напрямую.
    -- ESCAPE '\\': экранируем _ в pixel_pr — в SQL LIKE underscore = любой символ без ESCAPE.
    CASE WHEN agg.domain LIKE '%pixel\\_pr' ESCAPE '\\'
         THEN 'Перформ'
         ELSE 'Пиксель'
    END AS направление,
    NULL::TEXT      AS "номер кампании | название кампании",
    NULL::TEXT      AS "номер группы | название группы",
    NULL::INTEGER   AS "План заявки",
    NULL::INTEGER   AS "План приезда",
    gs.login_key || '|' || agg.domain  AS "аккаунт|сайт",
    NULL::INTEGER   AS priezd_arrival_date,
    NULL::INTEGER   AS prodazhi_arrival_date,
    'Victory'::TEXT AS поставщик,
    'pixel'::TEXT   AS _source_table
FROM (
    SELECT
        domain,
        created_date,
        pixel_name,
        MAX(cost_total)     AS cost_total,
        MAX(cost_per_lead)  AS cost_per_lead,
        MAX(NULLIF(source_type, '')) AS source_type,
        -- PIXEL_PR_SALON_FIX_2026-07-10: per-lead salon из local_leads_all для pixel_pr доменов.
        -- Для специфичных доменов (victory_mavto_pixel_pr и т.д.) salon однороден → MAX безопасен.
        -- Для общего victory_pixel_pr (25 лидов, несколько салонов) MAX даёт доминирующий салон
        -- за (domain, date, pixel_name). Не затрагивает non-pixel_pr домены.
        MAX(NULLIF(salon, '')) AS pixel_salon_raw,
{leads_agg_cases}
    FROM {T_PIXEL_LEADS}
    GROUP BY domain, created_date, pixel_name
) agg
LEFT JOIN {T_GSHEET_SITES} gs
    ON agg.domain = LOWER(TRIM(gs."domain"))
LEFT JOIN {T_GSHEET_AUTOSALONY} auto
    ON gs.client_id = auto.id_салона AND gs.client_id IS NOT NULL
;
"""


def _check_table_exists(conn, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT EXISTS(SELECT 1 FROM information_schema.tables "
            "WHERE table_name = %s AND table_schema = 'public')",
            (table,),
        )
        return cur.fetchone()[0]


def run(conn=None, run_id=None, **kwargs) -> dict:
    own_conn = conn is None
    if own_conn:
        db_module.init_pool()
        conn = db_module.get_conn()

    t0 = time.perf_counter()
    try:
        # Проверка local_leads_all
        if not _check_table_exists(conn, T_LEADS_ALL):
            logger.warning('%s не существует — пропускаем build_pixel (нужен step0)', T_LEADS_ALL)
            return {'rows': 0, 'details': f'{T_LEADS_ALL} missing'}

        # Проверка local_pixel_config
        if not _check_table_exists(conn, T_PIXEL_CONFIG):
            logger.warning('%s не существует — пропускаем build_pixel (нужен sync_pixel_config)', T_PIXEL_CONFIG)
            return {'rows': 0, 'details': f'{T_PIXEL_CONFIG} missing'}

        sql_parts = load_status_sql(conn)
        leads_agg = sql_parts['leads_agg_cases']

        # Шаг 1: создать pixel_leads
        logger.info('Создаём %s...', T_PIXEL_LEADS)
        with conn.cursor() as cur:
            cur.execute(_build_pixel_leads_sql())
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(f'SELECT COUNT(*) FROM {T_PIXEL_LEADS}')
            valid_rows = cur.fetchone()[0]
            cur.execute(f'SELECT COUNT(*) FROM {T_PIXEL_LEADS_CHECK}')
            check_rows = cur.fetchone()[0]
        logger.info('%s: %d строк (валидных)', T_PIXEL_LEADS, valid_rows)
        logger.info('%s: %d строк (для проверки)', T_PIXEL_LEADS_CHECK, check_rows)

        if valid_rows == 0:
            logger.warning('pixel_leads пустой — нет совпадений с gsheet доменами')

        # Шаг 2: TRUNCATE + INSERT в big_analytics_pixel
        logger.info('INSERT в %s...', T_PIXEL)
        with conn.cursor() as cur:
            cur.execute(_build_insert_sql(leads_agg))
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(f'SELECT COUNT(*) FROM {T_PIXEL}')
            pixel_rows = cur.fetchone()[0]
        logger.info('%s: %d строк итого', T_PIXEL, pixel_rows)

        elapsed = time.perf_counter() - t0
        logger.info('build_pixel завершён за %.1f сек', elapsed)

        return {
            'rows': pixel_rows,
            'details': f'valid={valid_rows:,}, check={check_rows:,}, pixel={pixel_rows:,}',
        }
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        if own_conn:
            db_module.put_conn(conn)
            db_module.close_pool()


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%H:%M:%S',
    )
    result = run()
    print(result)
