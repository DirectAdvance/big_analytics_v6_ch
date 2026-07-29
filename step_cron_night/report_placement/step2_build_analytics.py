# step_cron_night/report_placement/step2_build_analytics.py
# Что делает:
#   1. Строит динамический SQL из local_crm_statuses (через build_leads_agg_sql)
#   2. Обогащает public.analytics_report_placement лидами из raw_leads ИНКРЕМЕНТАЛЬНО
#      (окно MAX(date) - 61 день). Direct-строки уже залиты step1 напрямую в ARP.
#      Этап A: сбросить лид-данные Direct-строк за окно
#      Этап B: обогатить Direct-строки данными из raw_leads (UPDATE по key2)
#      Этап C: удалить старые leads-only строки за окно (логин IS NULL)
#      Этап D: вставить новые leads-only строки за окно (площадки raw_leads без Direct)
#
# Запуск: cd ~/big_analytics_v5 && ~/venv/bin/python3 step_cron_night/report_placement/step2_build_analytics.py

import logging
import os
import sys
from datetime import datetime

import psycopg2
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from config.settings import DB_DST
from config.tokens import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_PROXY, TELEGRAM_PROXY_VARIANTS
from config.status_sql import build_leads_agg_sql

_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'step2_build_analytics.log')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(_LOG_FILE, encoding='utf-8', mode='a'),
    ]
)
logger = logging.getLogger(__name__)

TARGET_TABLE = 'analytics_report_placement'
SOURCE_TABLE = 'analytics_report_placement'
# LEADSRC_2026-06-20: переключение источника лидов с raw_leads на local_leads_all.
# raw_leads — UNLOGGED таблица, пересоздаётся каждый прогон pipeline.py (step1_load_raw).
# При запуске step2 в субботу (cron) raw_leads может быть пустой (pipeline не запускался).
# local_leads_all — LOGGED постоянная таблица, обновляется step0 (синк из ad_analytics).
# Эквивалентность доказана (LEADSRC_NULL_FIX_2026-06-20): фильтр domain_id приведён в
# паритет со step1.py L63: (domain_id NOT IN (1645, 883) OR domain_id IS NULL).
# NULL-domain легитимен — лиды с неразрешённым FK domain_id сохраняются в raw_leads
# через LEFT JOIN (step1_load_raw). Без OR IS NULL они молча терялись (SQL NULL NOT IN = NULL).
LEADS_TABLE  = 'local_leads_all'

DDL = f"""
CREATE TABLE IF NOT EXISTS public.{TARGET_TABLE} (
    row_hash         TEXT PRIMARY KEY,
    date             DATE,
    domain           TEXT,
    логин            TEXT,
    ad_network_type  TEXT,
    placement        TEXT,
    placement_key    TEXT,
    clicks           BIGINT,
    cost             NUMERIC,
    "Все формы"           BIGINT,
    "CRM: Заказ создан"   BIGINT,
    "CRM: Заказ оплачен"  BIGINT,
    "CRM: Спам заказ"     BIGINT,
    "CRM: Заказ отменен"  BIGINT,
    campaign_id      BIGINT,
    campaign_name    TEXT,
    campaign_code    TEXT,
    tp               TEXT,
    cpc_cpa          TEXT,
    site_quiz        TEXT,
    ad_group_id      BIGINT,
    key              TEXT,
    key2             TEXT,
    директолог       TEXT,
    город            TEXT,
    регион           TEXT,
    салон            TEXT,
    шаблон           TEXT,
    тип_сайта        TEXT,
    статус           TEXT,
    направление      TEXT,
    "номер кампании|название кампании" TEXT,
    updated_at       TIMESTAMP,
    "Название crm"   TEXT,
    тип_заявки       TEXT,
    kol_vo_zayavok   BIGINT,
    korr             BIGINT,
    kval             BIGINT,
    priezd           BIGINT,
    prodazhi         BIGINT,
    nekorr           BIGINT,
    ne_otvechaet     BIGINT,
    filtr            BIGINT,
    nedozvon         BIGINT,
    priedet          BIGINT,
    dohod_do_kredita BIGINT,
    dobro            BIGINT
)
"""

_PLACEMENT_BASE = (
    "LOWER(REGEXP_REPLACE("
    "COALESCE((regexp_match(utm_source, '(?:^|[^a-z])s:(.+)$'))[1], utm_source, ''), "
    "'^(www\\.|m\\.)', ''))"
)

_PLACEMENT_EXPR = (
    f"CASE WHEN COALESCE(campaign_id::TEXT, '') NOT IN ('', '0')"
    f" AND {_PLACEMENT_BASE} = 'none'"
    f" THEN 'yandex' ELSE {_PLACEMENT_BASE} END"
)

_KEY2_EXPR = (
    "created_date::text"
    " || '|' || COALESCE(campaign_id::text, '0')"
    " || '|' || CASE WHEN utm_campaign ~* 'tp[67]' THEN '0'"
    "                ELSE COALESCE(group_id::text, '0') END"
    f" || '|' || {_PLACEMENT_EXPR}"
)

_LEADS_FILTER = (
    "COALESCE(deal_type, '') != 'Звонок'"
    " AND COALESCE(is_copy_for_removal, false) = false"
    # LEADSRC_2026-06-20: исключаем те же domain_id что step1_load_raw исключает
    # при сборке raw_leads (EXCLUDED_DOMAIN_IDS = (1645, 883) из config/settings.py):
    #   1645 = priezd shared key3 (общий ключ искажает статистику)
    #   883  = victory-crm.ru (не клиент)
    # LEADSRC_NULL_FIX_2026-06-20: NULL-domain легитимен — step1_load_raw сохраняет
    # лиды с domain_id IS NULL (LEFT JOIN local_domains, L63 step1.py).
    # SQL: NULL NOT IN (1645,883) = NULL → строка отбрасывается без OR IS NULL.
    # Паритет со step1.py L63: AND (l.domain_id NOT IN (1645,883) OR l.domain_id IS NULL)
    " AND (domain_id NOT IN (1645, 883) OR domain_id IS NULL)"
)


# ----- Этап A: сбросить лид-данные Direct-строк за инкрементальное окно -----
RESET_DIRECT_LEADS_SQL = f"""
UPDATE public.{TARGET_TABLE}
SET "Название crm" = NULL,
    тип_заявки = NULL,
    kol_vo_zayavok = 0, korr = 0, kval = 0,
    priezd = 0, prodazhi = 0, nekorr = 0,
    ne_otvechaet = 0, filtr = 0, nedozvon = 0,
    priedet = 0, dohod_do_kredita = NULL, dobro = NULL
WHERE date >= %(date_from)s AND логин IS NOT NULL
"""


# ----- Этап B: обогатить Direct-строки данными из raw_leads по key2 -----
def build_enrich_direct_sql(agg_cases: str) -> str:
    return f"""
WITH leads_agg AS (
    SELECT
        key2,
        MAX(source_type) AS название_crm,
{agg_cases}
    FROM (
        SELECT
            {_KEY2_EXPR} AS key2,
            status,
            reason,
            source_type,
            salon,
            deal_type,
            is_copy_for_removal
        FROM public.{LEADS_TABLE}
        WHERE {_LEADS_FILTER}
          AND campaign_id IS NOT NULL
          AND created_date >= %(date_from)s
    ) sub
    GROUP BY key2
)
UPDATE public.{TARGET_TABLE} d
SET "Название crm" = la.название_crm,
    тип_заявки = 'Заявки',  -- CAPITALIZE_FIX_2026-07-10
    kol_vo_zayavok = COALESCE(la.kol_vo_zayavok, 0),
    korr = COALESCE(la.korr, 0),
    kval = COALESCE(la.kval, 0),
    priezd = COALESCE(la.priezd, 0),
    prodazhi = COALESCE(la.prodazhi, 0),
    nekorr = COALESCE(la.nekorr, 0),
    ne_otvechaet = COALESCE(la.ne_otvechaet, 0),
    filtr = COALESCE(la.filtr, 0),
    nedozvon = COALESCE(la.nedozvon, 0),
    priedet = COALESCE(la.priedet, 0),
    dohod_do_kredita = la.dohod_do_kredita,
    dobro = la.dobro
FROM leads_agg la
WHERE d.key2 = la.key2
  AND d.date >= %(date_from)s
  AND d.логин IS NOT NULL
"""


# ----- Этап C: удалить старые leads-only строки за окно (логин IS NULL) -----
DELETE_LEADS_ONLY_SQL = f"""
DELETE FROM public.{TARGET_TABLE}
WHERE логин IS NULL AND date >= %(date_from)s
"""


# ----- Этап D: вставить новые leads-only строки за окно -----
def build_insert_leads_only_sql(agg_cases: str) -> str:
    return f"""
WITH
direct_placements AS (
    SELECT DISTINCT placement_key
    FROM public.{TARGET_TABLE}
    WHERE placement_key IS NOT NULL AND placement_key != ''
      AND логин IS NOT NULL
),
leads_only_agg AS (
    SELECT
        placement,
        created_date,
        MAX(source_type) AS название_crm,
{agg_cases}
    FROM (
        SELECT
            {_PLACEMENT_EXPR} AS placement,
            created_date,
            status,
            reason,
            source_type,
            salon,
            deal_type,
            is_copy_for_removal,
            utm_source
        FROM public.{LEADS_TABLE}
        WHERE {_LEADS_FILTER}
          AND created_date >= %(date_from)s
          AND utm_source IS NOT NULL AND utm_source != ''
          AND {_PLACEMENT_EXPR} NOT IN (SELECT placement_key FROM direct_placements)
          AND NOT (utm_source ~ 'victory_' OR utm_source = 'victory')
          AND NOT (utm_source = 'seo' OR utm_source ~ '_vdl'
                   OR utm_source = 'vk_groups' OR utm_source = 'vk')
          AND NOT utm_source ~ '_pixel'
    ) sub2
    GROUP BY placement, created_date
)
INSERT INTO public.{TARGET_TABLE} (
    row_hash, date, domain, логин,
    ad_network_type, placement, placement_key, clicks, cost,
    "Все формы", "CRM: Заказ создан", "CRM: Заказ оплачен",
    "CRM: Спам заказ", "CRM: Заказ отменен",
    campaign_id, campaign_name, campaign_code, tp, cpc_cpa, site_quiz,
    ad_group_id, key, key2,
    директолог, город, регион, салон, шаблон, тип_сайта, статус, направление,
    "номер кампании|название кампании",
    updated_at,
    "Название crm", тип_заявки,
    kol_vo_zayavok, korr, kval, priezd, prodazhi,
    nekorr, ne_otvechaet, filtr, nedozvon, priedet,
    dohod_do_kredita, dobro
)
SELECT
    md5('__leads_only__' || lo.placement || '|' || lo.created_date::text),
    lo.created_date,
    NULL, NULL,
    NULL,
    lo.placement,
    lo.placement,
    NULL, NULL,
    NULL, NULL, NULL, NULL, NULL,
    NULL, NULL, NULL,
    CASE WHEN lo.placement ~ '^(t\\.me|telegram|tg_)' THEN 'tp8' ELSE NULL END,
    NULL, NULL,
    NULL, NULL, NULL,
    NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
    NULL::TEXT,
    NOW(),
    lo.название_crm,
    'Заявки'::TEXT,  -- CAPITALIZE_FIX_2026-07-10 (Этап D — согласовано с Этапом A строка 176)
    lo.kol_vo_zayavok,
    lo.korr,
    lo.kval,
    lo.priezd,
    lo.prodazhi,
    lo.nekorr,
    lo.ne_otvechaet,
    lo.filtr,
    lo.nedozvon,
    lo.priedet,
    lo.dohod_do_kredita,
    lo.dobro
FROM leads_only_agg lo
"""


# ==================== TELEGRAM ====================

def send_telegram(message):
    """Отправка в Telegram с ротацией прокси (Amsterdam→DE→NL→FR→direct)."""
    url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage'
    for proxies in TELEGRAM_PROXY_VARIANTS:  # TG_PROXY_CHAIN_ROTATION_2026-06-17
        try:
            r = requests.post(url, data={
                'chat_id': TELEGRAM_CHAT_ID,
                'text': message,
                'parse_mode': 'HTML',
            }, proxies=proxies, timeout=30)
            if r.status_code == 200:
                return
        except Exception as e:
            logger.warning(f'Telegram (proxies={proxies}): {e}')


# ==================== MAIN ====================

def main():
    start = datetime.now()
    logger.info(f'СТАРТ: {start}')

    try:
        conn = psycopg2.connect(**DB_DST,
                                options='-c statement_timeout=600000 -c client_encoding=utf8')
        conn.autocommit = False
    except Exception as e:
        logger.exception(f'Ошибка подключения к БД: {e}')
        send_telegram(f'<b>step2_build_analytics</b>\n\nОшибка БД:\n<code>{e}</code>')
        return

    try:
        # Advisory lock — защита от параллельного запуска
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(202606302)")
            if not cur.fetchone()[0]:
                logger.warning("step2 уже запущен другим процессом (advisory lock занят). Выход.")
                conn.close()
                return
        conn.commit()

        logger.info('Загружаем статусы из local_crm_statuses...')
        agg_cases = build_leads_agg_sql(conn)
        logger.info('Статусы загружены.')

        with conn.cursor() as cur:
            cur.execute(DDL)
        conn.commit()
        logger.info('Таблица готова.')

        # Инкрементальное окно: MAX(date) - 61 день (fallback 2026-01-01 при пустой таблице)
        with conn.cursor() as cur:
            cur.execute(f"SELECT (MAX(date) - INTERVAL '61 days')::date FROM public.{TARGET_TABLE}")
            date_from = cur.fetchone()[0]
        if date_from is None:
            date_from = '2026-01-01'
        else:
            date_from = date_from.strftime('%Y-%m-%d')
        logger.info(f'Инкремент с {date_from}')

        params = {'date_from': date_from}

        # Guard: если источник лидов пуст за окно — НЕ стираем лид-данные (этап A).
        # local_leads_all — LOGGED, обновляется step0 (синк из ad_analytics).
        # Если запуститься на пустом источнике, этап A обнулит заявки у ~3.2М Direct-строк,
        # а этап B ничего не вернёт → безвозвратная потеря данных (инцидент 2026-06-01).
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*) FROM public.{LEADS_TABLE} "
                f"WHERE {_LEADS_FILTER} AND campaign_id IS NOT NULL "
                f"AND created_date >= %(date_from)s", params)
            leads_in_window = cur.fetchone()[0]
        if leads_in_window == 0:
            logger.error(
                f'{LEADS_TABLE} пуст за окно с {date_from} (0 подходящих строк) — '
                f'пропускаю этапы A/B/C/D, лид-данные НЕ трогаю')
            conn.close()
            send_telegram(
                f'<b>step2_build_analytics</b>\n\n'
                f'⚠️ {LEADS_TABLE} ПУСТ за окно с {date_from} (0 подходящих строк).\n'
                f'Этапы A/B/C/D пропущены — лид-данные в ARP НЕ стёрты.\n'
                f'Проверь step0 (синк local_leads_all из ad_analytics).')
            return
        logger.info(f'raw_leads за окно: {leads_in_window:,} строк — продолжаю')

        # Этап A: сбросить лид-данные Direct-строк за окно
        with conn.cursor() as cur:
            cur.execute(RESET_DIRECT_LEADS_SQL, params)
            reset_cnt = cur.rowcount
        conn.commit()
        logger.info(f'Этап A: сброшено лид-данных Direct-строк: {reset_cnt:,}')

        # Этап B: обогатить Direct-строки данными из raw_leads по key2
        with conn.cursor() as cur:
            cur.execute(build_enrich_direct_sql(agg_cases), params)
            enrich_cnt = cur.rowcount
        conn.commit()
        logger.info(f'Этап B: обогащено Direct-строк: {enrich_cnt:,}')

        # Этап C: удалить старые leads-only строки за окно
        with conn.cursor() as cur:
            cur.execute(DELETE_LEADS_ONLY_SQL, params)
            del_cnt = cur.rowcount
        conn.commit()
        logger.info(f'Этап C: удалено старых leads-only строк: {del_cnt:,}')

        # Этап D: вставить новые leads-only строки за окно
        with conn.cursor() as cur:
            cur.execute(build_insert_leads_only_sql(agg_cases), params)
            ins_cnt = cur.rowcount
        conn.commit()
        logger.info(f'Этап D: вставлено новых leads-only строк: {ins_cnt:,}')

        # ── Индексы: создаём/подтверждаем наличие после обогащения данных ──────
        # CREATE IF NOT EXISTS — идемпотентно (ничего не делает если уже есть).
        # Порядок: сначала данные залиты, потом индексы — быстрее чем наоборот.
        # INDEX_AUDIT_2026-06-27: удалены мёртвые (idx_scan=0):
        #   idx_arp_key2 — UPDATE FROM leads_agg использует Hash Join, а не Nested Loop →
        #                  index на key2 планировщик не берёт при 9.5M строк;
        #   idx_arp_date_login, idx_arp_login_null — partial-индексы по date; A/B/C/D работают
        #                  с большими фракциями таблицы (окно 61 день) → seq scan быстрее.
        #   Оставлен idx_arp_date (14 scans — реально используется).
        _arp_indexes = [
            ('idx_arp_date', 'CREATE INDEX IF NOT EXISTS idx_arp_date ON public.analytics_report_placement (date)'),
        ]
        try:
            vac_conn = psycopg2.connect(**DB_DST, options='-c statement_timeout=600000')
            vac_conn.autocommit = True
            with vac_conn.cursor() as _cur:
                for _iname, _isql in _arp_indexes:
                    _cur.execute(_isql)
                    logger.info(f'Индекс {_iname}: OK')
                # VACUUM ANALYZE вне транзакции (autocommit=True)
                _cur.execute('SET max_parallel_maintenance_workers = 0')
                _cur.execute('VACUUM (ANALYZE, PARALLEL 0) public.analytics_report_placement')
                logger.info('VACUUM ANALYZE analytics_report_placement: OK')
            vac_conn.close()
        except Exception as _ve:
            logger.warning(f'Индексы/VACUUM ARP (не критично): {_ve}')

    except Exception as e:
        logger.exception(f'Ошибка выполнения: {e}')
        conn.rollback()
        conn.close()
        send_telegram(f'<b>step2_build_analytics</b>\n\nОшибка:\n<code>{e}</code>')
        return

    conn.close()

    elapsed = str(datetime.now() - start).split('.')[0]
    summary = (
        f'<b>step2_build_analytics</b>\n\n'
        f'Окно: с {date_from}\n'
        f'A сброс: {reset_cnt:,}\n'
        f'B обогащено: {enrich_cnt:,}\n'
        f'C удалено: {del_cnt:,}\n'
        f'D вставлено: {ins_cnt:,}\n'
        f'Время: {elapsed}'
    )
    logger.info(f'ЗАВЕРШЕНО за {elapsed}')
    send_telegram(summary)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nПрервано')
        sys.exit(0)
    except Exception as e:
        logger.exception(f'Фатальная ошибка: {e}')
        send_telegram(f'<b>step2_build_analytics</b>\n\nФатальная ошибка:\n<code>{e}</code>')
        sys.exit(1)
