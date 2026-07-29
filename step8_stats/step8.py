"""
step6_stats/step6.py — финальная статистика + Telegram-отчёт (шаг 8)

Собирает полную статистику после всех шагов пайплайна,
формирует и отправляет итоговый Telegram-отчёт.
"""

import logging
import time
from datetime import datetime
from typing import Optional

import requests

from config.tokens import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_PROXY, TELEGRAM_PROXY_VARIANTS
from config.settings import (
    T_FULL, T_DIRECT, T_SEO, T_PIXEL, T_CROP, T_REVIEWS,
    T_YANDEX_LOCAL, T_LEADS_ALL_LOCAL,
    T_DATA_QUALITY_LOG,
    DATE_FROM,
)

logger = logging.getLogger('pipeline.step8')

T_CAMPAIGN_STATUS = 'campaign_status'
DASHBOARD_ISSUES_URL = 'http://localhost:5056/api/issues'


def _upsert_issue(issue_id: str, level: str, msg: str, count=None) -> None:
    try:
        requests.post(DASHBOARD_ISSUES_URL, json={
            'id': issue_id, 'level': level, 'agent': 'step8',
            'msg': msg, 'count': count,
        }, timeout=3)
    except Exception:
        pass


def _resolve_issue(issue_id: str) -> None:
    try:
        requests.delete(f'{DASHBOARD_ISSUES_URL}/{issue_id}', timeout=3)
    except Exception:
        pass

# ── Названия шагов для отчёта ─────────────────────────────────────────────────

STEP_LABELS = {
    'step0':              'Синхронизация',
    'step1':              'Загрузка RAW',
    'step2':              'Индексы',
    'step3':              'Источники',
    'corrections':        'Корректировки',
    'sync_pixel_config':  'Конфиг пикселей',
    'step5':              'Пиксели (build_pixel)',
    'step4':              'Статусы кампаний',
    'step6':              'big_analytics_full',
    'step7':              'Финализация',
    'step9':              'История Директа',
    'step10':             'Посевы Telega.in',
    'load_reviews':       'Загрузка отзывов',
    'load_crop':          'Загрузка посевов',
    '404_errors':         '404 ошибки',
    'normalize_salons':   'Нормализация салонов',
    'cleanup_old_dates':  'Очистка старых дат',
    'step11':             'Атрибуция пикселя (score)',
    'step11_pixel_score': 'Атрибуция пикселя (score)',
    'step8':              'Статистика',
}


# ── Отправка в Telegram ────────────────────────────────────────────────────────


_TG_MAX_LEN = 4096


def _split_chunks(text: str, limit: int = _TG_MAX_LEN) -> list[str]:
    """Split text into chunks ≤ limit, breaking on newlines where possible."""
    if len(text) <= limit:
        return [text]
    chunks, buf = [], []
    buf_len = 0
    for line in text.split('\n'):
        line_len = len(line) + 1  # +1 for '\n'
        if buf_len + line_len > limit and buf:
            chunks.append('\n'.join(buf))
            buf, buf_len = [], 0
        if line_len > limit:
            # single line longer than limit — hard-split
            chunks.append(line[:limit])
        else:
            buf.append(line)
            buf_len += line_len
    if buf:
        chunks.append('\n'.join(buf))
    return chunks


def _send_one(text: str, parse_mode: str, proxies) -> bool:
    r = requests.post(
        f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage',
        json={'chat_id': TELEGRAM_CHAT_ID, 'text': text, 'parse_mode': parse_mode},
        timeout=30,
        proxies=proxies,
    )
    if r.status_code == 200:
        return True
    logger.warning('Telegram API: HTTP %d — %s', r.status_code, r.text[:200])
    return False


def send_telegram(text: str, parse_mode: str = 'HTML') -> bool:
    """Отправить сообщение в Telegram с ротацией прокси (Amsterdam→DE→NL→FR→direct).
    Автоматически разбивает сообщения > 4096 символов на части."""
    chunks = _split_chunks(text)
    for proxies in TELEGRAM_PROXY_VARIANTS:  # TG_PROXY_CHAIN_ROTATION_2026-06-17
        try:
            ok = all(_send_one(chunk, parse_mode, proxies) for chunk in chunks)
            if ok:
                logger.info('Telegram: отправлено (%d частей)', len(chunks))
                return True
            return False
        except Exception as e:
            logger.warning('Telegram недоступен (proxies=%s): %s', proxies, e)
    return False


# ── Покрытие логинов ─────────────────────────────────────────────────────────

_LOGIN_FILTER = r"""
    direction = 'Авто'
    AND login_key IS NOT NULL
    AND login_key != ''
    AND login_key != 'Нет'
    AND login_key ~ '^[a-z0-9]'
    AND (
        block_date = '' OR block_date IS NULL
        OR (
            block_date ~ E'^[0-9]{2}\\.[0-9]{2}\\.[0-9]{4}$'
            AND TO_DATE(block_date, 'DD.MM.YYYY') >= '2026-01-01'
        )
    )
"""
# LOGIN_FILTER_REDESIGN_2026-06-18: убрана строка AND login_key IN (SELECT account_login
# FROM _active_logins). Теперь _LOGIN_FILTER = direction='Авто' + валидный login_key +
# block_date-фильтр БЕЗ фильтра расхода.
#
# НОВЫЙ ДИЗАЙН:
#   Эталон (r['total']) = ВСЕ активные Авто-логины из gsheet (~711).
#   Строка 'yandex' («с расходом в FDW»): в_ = логины эталона ∩ _active_logins (~654),
#   missing_ = эталонные логины вне _active_logins (~57) = активные в gsheet, но БЕЗ
#   открутки в FDW (аккаунты без расхода в текущем периоде). Это полезный разрыв.
#
# ПОЧЕМУ убрали IN _active_logins из эталона (было тавтологией):
#   Когда эталон = логины с cost>0, а строка 'yandex' проверяет EXISTS(_active_logins),
#   результат ВСЕГДА in_=total, missing_=[] — логин уже прошёл фильтр cost>0, значит
#   он точно есть в _active_logins. Ни одного нового знания, только ложный ✅.
#   После фикса: эталон 711 (все активные) ≠ in_spend 654 (с откруткой), missing 57 —
#   реальные аккаунты без расхода — полезная диагностика.
#
# MATERIALIZE-ONCE (_active_logins) из FDW:
#   CREATE TEMP TABLE _active_logins AS SELECT DISTINCT account_login FROM FDW WHERE total_cost>0
#   Один FDW-скан ~84s (неизбежен, но однократный). После фикса _active_logins используется
#   ТОЛЬКО в строке 'yandex' (через EXISTS), не в эталоне. FDW читается ЕДИНОКРАТНО.
#
# ПОЧЕМУ источник — FDW (не raw_yandex):
#   raw_yandex UNLOGGED жива в pipeline.py (TRUNCATE после step8, L1357),
#   но в fast_pipeline.py SPEND_PREFREE (L848) TRUNCATE-ит raw_yandex ДО spend-фазы,
#   а step8 вызывается ПОСЛЕ spend → raw_yandex пуста → 0 логинов.
#   FDW всегда доступна (не TRUNCATE-ится ни одним пайплайном). Стабильный источник.

_T_MGR = 'public.yandex_direct_manager_reports'


def _collect_pixel_validation(conn) -> dict:
    r = {}
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    (SELECT COALESCE(SUM(kol_vo_zayavok), 0) FROM big_analytics_pixel)       AS px_z,
                    (SELECT COALESCE(SUM(kol_vo_zayavok), 0) FROM big_analytics_pixel_score) AS sc_z,
                    (SELECT COALESCE(SUM(korr),           0) FROM big_analytics_pixel)       AS px_korr,
                    (SELECT COALESCE(SUM(korr),           0) FROM big_analytics_pixel_score) AS sc_korr,
                    (SELECT COALESCE(SUM(kval),           0) FROM big_analytics_pixel)       AS px_kval,
                    (SELECT COALESCE(SUM(kval),           0) FROM big_analytics_pixel_score) AS sc_kval,
                    (SELECT COALESCE(SUM(priezd),         0) FROM big_analytics_pixel)       AS px_priezd,
                    (SELECT COALESCE(SUM(priezd),         0) FROM big_analytics_pixel_score) AS sc_priezd,
                    (SELECT COALESCE(SUM(prodazhi),       0) FROM big_analytics_pixel)       AS px_prodazhi,
                    (SELECT COALESCE(SUM(prodazhi),       0) FROM big_analytics_pixel_score) AS sc_prodazhi,
                    (SELECT COALESCE(SUM(total_cost),     0) FROM big_analytics_pixel)       AS px_cost,
                    (SELECT COALESCE(SUM(total_cost),     0) FROM big_analytics_pixel_score) AS sc_cost
            """)
            row = cur.fetchone()
            r['px_z'],        r['sc_z']        = float(row[0]),  float(row[1])
            r['px_korr'],     r['sc_korr']     = float(row[2]),  float(row[3])
            r['px_kval'],     r['sc_kval']     = float(row[4]),  float(row[5])
            r['px_priezd'],   r['sc_priezd']   = float(row[6]),  float(row[7])
            r['px_prodazhi'], r['sc_prodazhi'] = float(row[8]),  float(row[9])
            r['px_cost'],     r['sc_cost']     = float(row[10]), float(row[11])

            cur.execute("""
                WITH
                p AS (
                    SELECT domain,
                           COALESCE(SUM(kol_vo_zayavok), 0) AS z,
                           COALESCE(SUM(korr),           0) AS korr,
                           COALESCE(SUM(kval),           0) AS kval,
                           COALESCE(SUM(priezd),         0) AS priezd,
                           COALESCE(SUM(prodazhi),       0) AS prodazhi
                    FROM big_analytics_pixel WHERE domain IS NOT NULL GROUP BY domain
                ),
                s AS (
                    SELECT domain,
                           COALESCE(SUM(kol_vo_zayavok), 0) AS z,
                           COALESCE(SUM(korr),           0) AS korr,
                           COALESCE(SUM(kval),           0) AS kval,
                           COALESCE(SUM(priezd),         0) AS priezd,
                           COALESCE(SUM(prodazhi),       0) AS prodazhi
                    FROM big_analytics_pixel_score WHERE domain IS NOT NULL GROUP BY domain
                )
                SELECT COUNT(*)
                FROM p LEFT JOIN s ON s.domain = p.domain
                WHERE ABS(p.z        - COALESCE(s.z,        0)) > 0.01
                   OR ABS(p.korr     - COALESCE(s.korr,     0)) > 0.01
                   OR ABS(p.kval     - COALESCE(s.kval,     0)) > 0.01
                   OR ABS(p.priezd   - COALESCE(s.priezd,   0)) > 0.01
                   OR ABS(p.prodazhi - COALESCE(s.prodazhi, 0)) > 0.01
            """)
            r['domain_mismatches'] = int(cur.fetchone()[0] or 0)

            # Домены без кампаний (leftover rows с CampaignId=NULL)
            # Фильтр: только домены наших специалистов (в public.specialists)
            # и с реальным login_key (не 'Нет', не '---...')
            cur.execute("""
                SELECT bps.domain,
                       SUM(bps.kol_vo_zayavok) AS z,
                       SUM(bps.total_cost)     AS cost
                FROM big_analytics_pixel_score bps
                JOIN public.local_gsheet_sites gs ON gs.domain = bps.domain
                WHERE bps."CampaignId" IS NULL
                  AND bps._source_table = 'пиксель'
                  AND bps.domain IS NOT NULL
                  AND gs.direction = 'Авто'
                  AND gs.directologist IN (SELECT name FROM public.specialists)
                  AND COALESCE(gs.login_key, '') NOT IN (
                      '', 'Нет',
                      '----', '-----', '------', '--------',
                      '---------', '------------', '--------------', '----------------'
                  )
                  AND gs.login_key NOT LIKE '-%'
                GROUP BY bps.domain
                ORDER BY SUM(bps.kol_vo_zayavok) DESC
                LIMIT 20
            """)
            r['leftover_domains'] = [
                (row[0], float(row[1] or 0), float(row[2] or 0))
                for row in cur.fetchall()
            ]
    except Exception as e:
        logger.warning('pixel_validation: %s', e)
    return r


def _collect_login_coverage(conn) -> dict:
    r = {}
    try:
        with conn.cursor() as cur:
            # LOGIN_FILTER_MATERIALIZE_2026-06-18: материализуем множество активных логинов
            # ОДИН РАЗ из FDW (~84s), затем _LOGIN_FILTER ссылается на локальную TEMP TABLE —
            # FDW сканируется ОДНОКРАТНО здесь, а не 7 раз (588s→84s+небольшой хвост).
            # Источник: FDW yandex_direct_manager_reports (не raw_yandex!) — raw_yandex пуста
            # в fast_pipeline.py (SPEND_PREFREE TRUNCATE-ит её ДО spend-фазы, а step8 ПОСЛЕ).
            # rollback() перед DDL: выводит соединение из aborted-транзакции (паттерн KNOWN_ISSUES #14).
            # DROP IF EXISTS: безопасно при повторном вызове (--only-step=8) в той же сессии.
            conn.rollback()
            cur.execute("DROP TABLE IF EXISTS _active_logins")
            cur.execute("""
                CREATE TEMP TABLE _active_logins AS
                SELECT DISTINCT account_login
                FROM public.yandex_direct_manager_reports
                WHERE total_cost > 0
                  AND account_login IS NOT NULL
            """)
            cur.execute("CREATE INDEX ON _active_logins(account_login)")
            conn.commit()

            # LOGIN_FILTER_REDESIGN_2026-06-18: эталон = ВСЕ активные Авто-логины из gsheet
            # (БЕЗ фильтра расхода в _LOGIN_FILTER). ~711 логинов против прежних ~654.
            cur.execute(f"SELECT COUNT(DISTINCT login_key) FROM public.local_gsheet_sites gs WHERE {_LOGIN_FILTER}")
            r['total'] = int(cur.fetchone()[0] or 0)

            # Строка 'yandex' — «с расходом в FDW (cost>0)»:
            #   in_  = логины эталона, которые ЕСТЬ в _active_logins (имеют cost>0) → ~654.
            #   missing_ = логины эталона, которых НЕТ в _active_logins → ~57.
            #   Эти ~57 = активные в gsheet, но без открутки в FDW — реальный диагностический сигнал.
            #   Запрос идёт через локальную TEMP TABLE _active_logins (НЕ через FDW напрямую) —
            #   FDW читается только в материализации выше, FDW-сканов больше нет.
            #
            # Строка 'full' — «доехал до витрины big_analytics_full»:
            #   EXISTS против локальной public.big_analytics_full — реальный сигнал «доехал ли
            #   активный логин до витрины». Тоже не FDW.
            for key, table, col in [
                ('yandex',  '_active_logins',             'account_login'),
                ('full',    'public.big_analytics_full',  'account_login'),
            ]:
                cur.execute(f"""
                    SELECT COUNT(DISTINCT gs.login_key)
                    FROM public.local_gsheet_sites gs
                    WHERE {_LOGIN_FILTER}
                      AND EXISTS (SELECT 1 FROM {table} t WHERE t.{col} = gs.login_key)
                """)
                r[f'in_{key}'] = int(cur.fetchone()[0] or 0)

                cur.execute(f"""
                    SELECT DISTINCT gs.login_key
                    FROM public.local_gsheet_sites gs
                    WHERE {_LOGIN_FILTER}
                      AND NOT EXISTS (SELECT 1 FROM {table} t WHERE t.{col} = gs.login_key)
                    ORDER BY gs.login_key
                """)
                r[f'missing_{key}'] = [row[0] for row in cur.fetchall()]

    except Exception as e:
        logger.warning('login_coverage: %s', e)
    return r


# ── Сбор финальной статистики ─────────────────────────────────────────────────

def _collect_final_stats(conn, run_id: str) -> dict:
    s = {}

    # ── Объединённый скан big_analytics_full (O-STEP8, 2026-06-11) ──────────────
    # Раньше big_analytics_full сканировался ТРИ раза подряд скалярными агрегатами:
    #   (1) MIN/MAX("Date")  (2) COUNT(*)  (3) 8 агрегатов WHERE not-пиксель.
    # На 2.6-3.6M строк это 3 полных скана. Объединено в ОДИН скан: COUNT/MIN/MAX —
    # по ВСЕМ строкам, 8 агрегатов — через FILTER(WHERE not-пиксель), что бит-в-бит
    # эквивалентно прежнему WHERE-предикату. Семантика и значения НЕ меняются.
    # GROUP BY-сканы (по источнику, по account_login ниже) НЕ трогаем — другая гранула.
    _NOT_PIXEL = "(направление NOT ILIKE '%пиксель%' OR направление IS NULL)"
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                    MIN("Date"), MAX("Date"), COUNT(*),
                    SUM(kol_vo_zayavok)         FILTER (WHERE {_NOT_PIXEL}),
                    SUM(korr)                   FILTER (WHERE {_NOT_PIXEL}),
                    SUM(priezd)                 FILTER (WHERE {_NOT_PIXEL}),
                    SUM(prodazhi)               FILTER (WHERE {_NOT_PIXEL}),
                    ROUND(SUM(total_cost) FILTER (WHERE {_NOT_PIXEL})::NUMERIC, 0),
                    COUNT(DISTINCT domain)      FILTER (WHERE {_NOT_PIXEL}),
                    COALESCE(SUM(dohod_do_kredita) FILTER (WHERE {_NOT_PIXEL}), 0),
                    COALESCE(SUM(dobro)            FILTER (WHERE {_NOT_PIXEL}), 0)
                FROM {T_FULL}
            """)
            row = cur.fetchone()
            s['date_from']      = str(row[0]) if row[0] else None
            s['date_to']        = str(row[1]) if row[1] else None
            s['rows_full']      = int(row[2] or 0)
            s['total_leads']    = int(row[3] or 0)
            s['total_korr']     = int(row[4] or 0)
            s['total_priezd']   = int(row[5] or 0)
            s['total_prodazhi'] = int(row[6] or 0)
            s['total_cost']     = float(row[7] or 0)
            s['domains']        = int(row[8] or 0)
            s['total_dohod']    = int(row[9] or 0)
            s['total_dobro']    = int(row[10] or 0)
    except Exception as e:
        logger.warning('Объединённый скан big_analytics_full: %s', e)
        s.setdefault('date_from', None)
        s.setdefault('date_to', None)
        s.setdefault('rows_full', -1)

    # Количество строк по остальным таблицам (full уже посчитан выше одним сканом)
    for key, table in [
        ('direct',       T_DIRECT),
        ('seo',          T_SEO),
        ('pixel',        T_PIXEL),
        ('crop',         T_CROP),
        ('reviews',      T_REVIEWS),
    ]:
        try:
            with conn.cursor() as cur:
                cur.execute(f'SELECT COUNT(*) FROM {table}')
                s[f'rows_{key}'] = cur.fetchone()[0]
        except Exception:
            s[f'rows_{key}'] = -1

    # CPL по объединённым агрегатам big_analytics_full
    try:
        if 'total_cost' in s:
            cost = s['total_cost']
            def _cpl(n):
                return f'{cost / n:,.0f} ₽' if n else '—'
            s['cpl_leads']    = _cpl(s['total_leads'])
            s['cpl_korr']     = _cpl(s['total_korr'])
            s['cpl_priezd']   = _cpl(s['total_priezd'])
            s['cpl_prodazhi'] = _cpl(s['total_prodazhi'])
            s['cpl_dohod']    = _cpl(s['total_dohod'])
            s['cpl_dobro']    = _cpl(s['total_dobro'])
    except Exception as e:
        logger.warning('Агрегаты full: %s', e)

    # Покрытие campaign_status
    # CAMPAIGN_STATUS_SOURCE_FIX_2026-07-02: T_DIRECT (big_analytics_direct) TRUNCATE-нута
    # пайплайном в SPEND_PREFREE после step11 (by-design) — всегда 0 строк к моменту step8.
    # Заменяем источник на T_CAMPAIGN_STATUS (всегда заполнена, не TRUNCATE-нуется).
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                    COUNT(DISTINCT "CampaignId"),
                    COUNT(DISTINCT "CampaignId"),
                    COUNT(DISTINCT CASE WHEN campaign_status IS NOT NULL
                                        THEN "CampaignId" END)
                FROM {T_CAMPAIGN_STATUS}
            """)
            row = cur.fetchone()
            s['direct_campaigns']      = int(row[0] or 0)   # = total в campaign_status
            s['campaigns_in_status']   = int(row[1] or 0)   # = то же
            s['campaigns_with_status'] = int(row[2] or 0)

            cur.execute(f"""
                SELECT campaign_status, COUNT(*)
                FROM {T_CAMPAIGN_STATUS}
                WHERE campaign_status IS NOT NULL
                GROUP BY campaign_status ORDER BY COUNT(*) DESC
            """)
            s['campaign_status_breakdown'] = cur.fetchall()

            cur.execute("""
                SELECT payment_model, COUNT(*) AS cnt
                FROM public.campaign_status
                WHERE campaign_status = 'Активна'
                GROUP BY payment_model
                ORDER BY cnt DESC
            """)
            s['payment_model_breakdown'] = cur.fetchall()
    except Exception as e:
        logger.warning('campaign_status stats: %s', e)

    # check_utm статистика
    try:
        with conn.cursor() as cur:
            # Уникальных кампаний в campaign_status
            # CAMPAIGN_STATUS_SOURCE_FIX_2026-07-02: T_DIRECT TRUNCATE-нута пайплайном by-design.
            # campaign_status всегда заполнена (шаг4) — используем как источник числа кампаний.
            cur.execute(f"""
                SELECT COUNT(DISTINCT "CampaignId")
                FROM {T_CAMPAIGN_STATUS}
                WHERE "CampaignId" IS NOT NULL
            """)
            s['direct_camp_groups'] = int(cur.fetchone()[0] or 0)

            # Строк и заполненность cls в check_utm
            cur.execute("SELECT COUNT(*), COUNT(cls) FROM public.check_utm")
            row = cur.fetchone()
            s['check_utm_total']      = int(row[0] or 0)
            s['check_utm_cls_filled'] = int(row[1] or 0)

            # По cls
            cur.execute("""
                SELECT cls, COUNT(*) FROM public.check_utm
                GROUP BY cls ORDER BY COUNT(*) DESC
            """)
            s['check_utm_by_cls'] = cur.fetchall()

            # Неверный UTM по специалистам
            cur.execute("""
                SELECT "специалист", COUNT(DISTINCT "CampaignId")
                FROM public.check_utm
                WHERE cls IN ('ДРУГОЙ_UTM', 'НЕТ_UTM')
                  AND "специалист" IS NOT NULL AND "специалист" != ''
                GROUP BY "специалист"
                ORDER BY COUNT(DISTINCT "CampaignId") DESC
                LIMIT 20
            """)
            s['bad_utm_by_directolog'] = cur.fetchall()
    except Exception as e:
        logger.warning('check_utm stats: %s', e)

    # ── Сверка расходов: FDW yandex_direct_manager_reports vs fact_big_analytics ──
    # RECON_FIX_2026-06-19: правая сторона переведена с big_analytics_direct (TRUNCATE'd
    # пайплайном SPEND_PREFREE → 0 строк после прогона) на public.fact_big_analytics —
    # durable-таблицу звезды, которая НЕ TRUNCATE-нуется cleanup_intermediate.
    #
    # Левая сторона: FDW yandex_direct_manager_reports за период DATE_FROM..today.
    #   "Cost" = double precision → кастуем ::NUMERIC; "Date" = text → ::DATE.
    #   Включает: direct + tp8/tp9/tp10 (МК/ТК — физически списаны через Директ).
    #
    # Правая сторона: fact_big_analytics WHERE атрибуция='По дате заявки'
    #   AND _source_table IN ('direct','tp8','tp9','tp10') AND "Date" >= DATE_FROM.
    #   tp8/tp9/tp10 уже включены в один запрос — отдельного T_CROP-запроса не нужно.
    #   "Date" в fact_big_analytics = DATE (не text), каст не требуется.
    #
    # Ожидаемый остаточный Δ после фикса:
    #   ~1–3% — за счёт НДС (FDW хранит Cost с НДС, total_cost в пайплайне = Cost без
    #   НДС, если step3 делит на 1.2), коррекций corrections.py (rule1 Кудерко сдвигает
    #   расход между аккаунтами), и точных границ периода (FDW = по дате клика;
    #   fact = по дате заявки при атрибуции 'По дате заявки').
    #   Δ -85% (ноль в big_analytics_direct) больше не возникнет.
    s['recon_period_from'] = DATE_FROM
    s['recon_period_to']   = datetime.now().strftime('%Y-%m-%d')
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COALESCE(SUM("Cost"::NUMERIC), 0)::NUMERIC AS yan
                FROM public.yandex_direct_manager_reports
                WHERE "Date"::DATE >= %s AND "Date"::DATE <= %s
            """, (s['recon_period_from'], s['recon_period_to']))
            s['cost_local_yandex'] = float(cur.fetchone()[0] or 0)

            # RECON_FIX_2026-06-19: читаем из durable fact_big_analytics
            # (не из TRUNCATE'd big_analytics_direct). Атрибуция='По дате заявки'
            # эквивалентна big_analytics_full (T_FULL). Источники direct+tp8/9/10
            # = весь Директ-бюджет, аналогично левой стороне FDW.
            cur.execute("""
                SELECT
                    COALESCE(SUM(total_cost), 0)::NUMERIC AS dir
                FROM public.fact_big_analytics
                WHERE "атрибуция" = 'По дате заявки'
                  AND _source_table IN ('direct', 'tp8', 'tp9', 'tp10')
                  AND "Date" >= %s AND "Date" <= %s
            """, (s['recon_period_from'], s['recon_period_to']))
            s['cost_big_direct'] = float(cur.fetchone()[0] or 0)

            # RECON_DIRECT_ONLY_2026-06-29: расход только 'direct' без tp8/9/10.
            # Δ vs FDW ≈ офлайн-коррекции step1 (by-design).
            # tp8/9/10 = МК/ТК surrogate кампании, в FDW их нет.
            cur.execute("""
                SELECT
                    COALESCE(SUM(total_cost), 0)::NUMERIC AS dir_only
                FROM public.fact_big_analytics
                WHERE "атрибуция" = 'По дате заявки'
                  AND _source_table = 'direct'
                  AND "Date" >= %s AND "Date" <= %s
            """, (s['recon_period_from'], s['recon_period_to']))
            s['cost_big_direct_only'] = float(cur.fetchone()[0] or 0)

            # tp8/tp9/tp10 уже включены в cost_big_direct выше — обнуляем отдельную метрику
            s['cost_tp8_in_crop'] = 0.0
    except Exception as e:
        logger.warning('cost_reconciliation: %s', e)
        s['cost_local_yandex'] = 0.0
        s['cost_big_direct']   = 0.0
        s['cost_tp8_in_crop']  = 0.0

    # ── Сверка продаж по источникам: local_leads_all vs big_analytics_full ──────
    # источник в big_analytics_full: Контекст/SEO/звонки/посевы/пиксели/telegram/Max/Telegram+Max
    # local_leads_all: derive по utm_source/utm_medium/domain. Звонки/пиксели/tp8/tp9/tp10
    # в leads_all не присутствуют — для них local=NULL.
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT COALESCE(NULLIF(TRIM(источник), ''), '(пусто)') AS src,
                       COALESCE(SUM(prodazhi), 0)::BIGINT AS sales
                FROM {T_FULL}
                WHERE "Date" >= %s AND "Date" <= %s
                GROUP BY 1
                ORDER BY 2 DESC
            """, (s['recon_period_from'], s['recon_period_to']))
            full_by_src = {row[0]: int(row[1] or 0) for row in cur.fetchall()}

            cur.execute("""
                WITH sale_statuses AS (
                    SELECT DISTINCT crm_status
                    FROM local_crm_statuses
                    WHERE kind = 'status' AND lead_status = 'sale'
                ),
                crop_domains AS (
                    SELECT DISTINCT LOWER(TRIM("Сайт")) AS d
                    FROM public.gsheets_crop_targeting_account
                    WHERE "Сайт" IS NOT NULL AND TRIM("Сайт") != ''
                )
                SELECT
                    CASE
                        WHEN LOWER(TRIM(d.name)) IN (SELECT d FROM crop_domains)
                            THEN 'посевы'
                        WHEN la.utm_source IS NULL
                          OR la.utm_source = ''
                          OR (la.utm_source = 'seo' AND la.utm_medium = 'organic')
                            THEN 'SEO'
                        ELSE 'Контекст'
                    END AS src,
                    COUNT(*)::BIGINT AS sales
                FROM local_leads_all la
                LEFT JOIN local_domains d ON d.id = la.domain_id
                WHERE la.created_date >= %s AND la.created_date <= %s
                  AND la.status IN (SELECT crm_status FROM sale_statuses)
                GROUP BY 1
                ORDER BY 2 DESC
            """, (s['recon_period_from'], s['recon_period_to']))
            local_by_src = {row[0]: int(row[1] or 0) for row in cur.fetchall()}

            s['sales_full_by_src']  = full_by_src
            s['sales_local_by_src'] = local_by_src
    except Exception as e:
        logger.warning('sales_reconciliation: %s', e)
        s['sales_full_by_src']  = {}
        s['sales_local_by_src'] = {}

    # Пиксели с расходом, но без специалиста — разбивка по domain.
    # PIXEL_NOSPEC_MIN_COST_2026-06-18: порог отображения домена (ниже — свёртка в одну строку).
    # Источник: big_analytics_pixel_score (post-step11 атрибуция) — тот же расход, что
    # показывается пользователю и лежит в витрине Power BI. T_PIXEL (pre-step11) даёт
    # другую сумму (до атрибуции) и рассинхронизируется с PBI-цифрой. Director confirmed:
    # pixel_score = 3 828 350 ₽ (35 доменов) = бит-в-бит число пользователя.
    # PIXEL_NOSPEC_SOURCE_FIX_2026-06-18
    # PIXEL_NOSPEC_SALON_NAME_2026-06-29: JOIN с local_gsheet_sites для имени салона.
    # PIXEL_NOSPEC_SALON_DEDUP_2026-07-02: группировка по salon_name вместо domain —
    # один салон с несколькими доменами теперь выходит одной строкой с суммой расхода.
    # Tuple: (salon_name, cost, n_domains) — n_domains>1 показывается явно в отчёте.
    PIXEL_NOSPEC_MIN_COST = 10_000  # PIXEL_NOSPEC_MIN_COST_2026-06-18
    try:
        with conn.cursor() as cur:
            cur.execute("""
                WITH domain_costs AS (
                    SELECT bps.domain,
                           SUM(bps.total_cost) AS cost,
                           COALESCE(MAX(gs.salon), bps.domain) AS salon_name
                    FROM big_analytics_pixel_score bps
                    LEFT JOIN public.local_gsheet_sites gs ON gs.domain = bps.domain
                    WHERE bps.total_cost > 0
                      AND (bps."специалист" IS NULL OR bps."специалист" = '')
                      AND bps.domain IS NOT NULL AND bps.domain != ''
                    GROUP BY bps.domain
                )
                SELECT salon_name,
                       ROUND(SUM(cost)::NUMERIC, 0) AS cost,
                       COUNT(DISTINCT domain) AS n_domains
                FROM domain_costs
                GROUP BY salon_name
                ORDER BY cost DESC
            """)
            s['pixels_no_specialist'] = [
                (row[0], float(row[1] or 0), int(row[2] or 1))
                for row in cur.fetchall()
            ]
            s['pixel_nospec_threshold'] = PIXEL_NOSPEC_MIN_COST
    except Exception as e:
        logger.warning('pixels_no_specialist: %s', e)
        s['pixels_no_specialist'] = []
        s['pixel_nospec_threshold'] = PIXEL_NOSPEC_MIN_COST

    # Обычные аккаунты (не пиксель) с расходом, но без специалиста.
    # PIXEL_NOSPEC_MIN_COST_2026-06-18: пиксели вынесены в отдельный блок выше.
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT account_login,
                       ROUND(SUM(total_cost)::NUMERIC, 0) AS cost
                FROM {T_FULL}
                WHERE total_cost > 0
                  AND ("специалист" IS NULL OR "специалист" = '')
                  AND account_login IS NOT NULL AND account_login != ''
                  AND account_login != 'пиксель'
                GROUP BY account_login
                ORDER BY cost DESC
                LIMIT 30
            """)
            s['accounts_no_specialist'] = cur.fetchall()
    except Exception as e:
        logger.warning('accounts_no_specialist: %s', e)
        s['accounts_no_specialist'] = []

    # Время выполнения шагов
    # rollback() сбрасывает aborted-транзакцию, если один из предыдущих SELECT упал
    try:
        conn.rollback()
    except Exception:
        pass
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT step, duration_sec FROM {T_DATA_QUALITY_LOG}
                WHERE run_id = %s AND status = 'ok'
                ORDER BY id
            """, (run_id,))
            s['step_durations'] = cur.fetchall()
    except Exception as e:
        logger.warning('step_durations: %s', e)
        s['step_durations'] = []

    # Стоимость квала без пикселя (KVAL_COST_CHECK_2026-06-28)
    # Согласовано с verify_big_analytics.py блок 14 (тот же источник и фильтр).
    # fact_big_analytics — durable, не TRUNCATE-нуется cleanup_intermediate.
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COALESCE(SUM(total_cost), 0)::NUMERIC AS total_cost,
                    COALESCE(SUM(kval),       0)::NUMERIC AS total_kval
                FROM public.fact_big_analytics
                WHERE "атрибуция" = 'По дате заявки'
                  AND _source_table NOT IN ('пиксель', 'пиксель_атрибуц')
            """)
            row = cur.fetchone()
            tc = float(row[0] or 0)
            tk = float(row[1] or 0)
            s['kval_cost'] = round(tc / tk) if tk > 0 else None
    except Exception as e:
        logger.warning('kval_cost: %s', e)
        s['kval_cost'] = None

    s['login_coverage']     = _collect_login_coverage(conn)
    s['pixel_validation']   = _collect_pixel_validation(conn)

    return s


def _fmt_sec(secs) -> str:
    secs = int(secs or 0)
    return f'{secs // 60}м {secs % 60:02d}с'


def _format_final_report(s: dict, run_id: str) -> str:
    now       = datetime.now().strftime('%d.%m.%Y %H:%M')
    date_from = s.get('date_from', '—')
    date_to   = s.get('date_to',   '—')
    pipeline_degraded = bool(s.get('pipeline_degraded'))
    degraded_steps = s.get('degraded_steps') or []

    lines = [
        '<b>big_analytics_v5</b> ⚠️ DEGRADED' if pipeline_degraded else '<b>big_analytics_v5</b> ✅',
        f'<code>{now}</code>  run_id: <code>{run_id}</code>',
        '',
    ]
    if pipeline_degraded:
        lines += [
            '<b>Основной fact_big_analytics собран, но отдельные spend-витрины не обновились:</b>',
            f'  {", ".join(degraded_steps)}',
            '',
        ]
    lines += [
        f'<b>Период:</b> {date_from} — {date_to}',
        '',
        '<b>Строк в таблицах:</b>',
        f'  full:          {s.get("rows_full", 0):,}',
        f'  direct:        {s.get("rows_direct", 0):,}',
        f'  seo:           {s.get("rows_seo", 0):,}',
        f'  reviews:       {s.get("rows_reviews", 0):,}',
        f'  crop:          {s.get("rows_crop", 0):,}',
        f'  pixel:         {s.get("rows_pixel", 0):,}',
        '',
    ]

    # ── Сверка расходов (FDW yandex_direct_manager_reports vs fact_big_analytics) ───
    # RECON_FIX_2026-06-19: правая сторона переведена на fact_big_analytics (durable).
    # RECON_DIRECT_ONLY_2026-06-29: три строки вместо одной (устранён ложный ❌):
    #   FDW (raw Cost) / fact direct only (с корр.) / fact всего (direct+tp8/9/10).
    #   Δ direct_only vs FDW ≈ офлайн-коррекции step1 by-design (+108M).
    #   tp8/9/10 = МК/ТК surrogate (не присутствуют в FDW) — отдельная строка с пояснением.
    #   Гейт по дельте удалён (расхождения by-design, не ошибка).
    yan_cost      = s.get('cost_local_yandex',    0.0)
    dir_cost      = s.get('cost_big_direct',       0.0)
    dir_only_cost = s.get('cost_big_direct_only',  0.0)
    diff_direct   = dir_only_cost - yan_cost
    tp_cost       = dir_cost - dir_only_cost
    rec_from = s.get('recon_period_from', '—')
    rec_to   = s.get('recon_period_to', '—')
    lines += [
        f'<b>Сверка расходов ({rec_from} .. {rec_to}):</b>',
        f'  FDW manager_reports (raw Cost):        {yan_cost:>15,.0f} ₽',
        f'  fact direct only (total_cost с корр.): {dir_only_cost:>15,.0f} ₽  (Δ vs FDW: {diff_direct:+,.0f} — офлайн-коррекции step1, by-design)',
        f'  fact всего (direct+tp8/9/10):          {dir_cost:>15,.0f} ₽  (tp8/9/10={tp_cost:,.0f} МК/ТК surrogate, в FDW их нет)',
        '',
    ]

    # ── Сверка продаж по источникам (local_leads_all vs big_analytics_full) ────
    full_by  = s.get('sales_full_by_src',  {}) or {}
    local_by = s.get('sales_local_by_src', {}) or {}
    all_srcs = set(full_by.keys()) | set(local_by.keys())
    # Порядок: derivable from leads_all сверху, затем full-only
    src_order = ['Контекст', 'SEO', 'посевы', 'звонки', 'пиксель', 'telegram', 'Max', 'Telegram + Max', 'VK', 'telegram_tp8']
    sorted_srcs = [s_ for s_ in src_order if s_ in all_srcs]
    sorted_srcs += sorted([s_ for s_ in all_srcs if s_ not in src_order])
    leads_derivable = {'Контекст', 'SEO', 'посевы'}

    lines.append('<b>Сверка продаж по источникам:</b>')
    lines.append(f'  {"источник":<14} {"leads_all":>10}  {"full":>8}  Δ')
    sum_local = 0
    sum_full  = 0
    for src in sorted_srcs:
        f_cnt = full_by.get(src, 0)
        l_cnt = local_by.get(src, 0) if src in leads_derivable else None
        sum_full += f_cnt
        if l_cnt is not None:
            sum_local += l_cnt
            d = l_cnt - f_cnt
            lines.append(f'  {src:<14} {l_cnt:>10,}  {f_cnt:>8,}  {d:+,}')
        else:
            lines.append(f'  {src:<14} {"—":>10}  {f_cnt:>8,}  (нет в leads_all)')
    diff_total = sum_local - sum_full
    lines.append(f'  {"ИТОГО":<14} {sum_local:>10,}  {sum_full:>8,}  {diff_total:+,}')
    lines.append('')

    lines += [
        '<b>Итого (full):</b>',
        f'  Заявок:    {s.get("total_leads", 0):,}',
        f'    CPL:     {s.get("cpl_leads", "—")}',
        f'  Корр:      {s.get("total_korr", 0):,}',
        f'    CPL:     {s.get("cpl_korr", "—")}',
        f'  Приездов:  {s.get("total_priezd", 0):,}',
        f'    CPL:     {s.get("cpl_priezd", "—")}',
        f'  Доход:     {s.get("total_dohod", 0):,}',
        f'    CPL:     {s.get("cpl_dohod", "—")}',
        f'  Добро:     {s.get("total_dobro", 0):,}',
        f'    CPL:     {s.get("cpl_dobro", "—")}',
        f'  Продаж:    {s.get("total_prodazhi", 0):,}',
        f'    CPL:     {s.get("cpl_prodazhi", "—")}',
        f'  Расходы:   {s.get("total_cost", 0):,.0f} ₽',
        f'  Доменов:   {s.get("domains", 0):,}',
    ]
    # KVAL_COST_CHECK_2026-06-28: стоимость квала без пикселя
    kval_cost = s.get('kval_cost')
    if kval_cost is not None:
        # KVAL_RANGE_UPDATE_2026-07-02: диапазон расширен [7k;15k] → [10k;30k] по согласованию с Семёном.
        _kc_icon = '✅' if 10_000 <= kval_cost <= 30_000 else '⚠️'
        lines.append(f'  Стоимость квала (без пикселя): {kval_cost:,} ₽ [10000;30000] {_kc_icon}')
    lines.append('')

    # Кампании (CAMPAIGN_STATUS_SOURCE_FIX_2026-07-02: источник — campaign_status, не big_analytics_direct)
    dc  = s.get('direct_campaigns', 0)   # = всего в campaign_status
    wcs = s.get('campaigns_with_status', 0)
    pct = f'{100 * wcs / dc:.1f}%' if dc else '—'
    lines += [
        '<b>Кампании (campaign_status):</b>',
        f'  Всего в campaign_status: {dc:,}',
        f'  С заполн. статусом:      {wcs:,} ({pct})',
    ]
    for st, cnt in s.get('campaign_status_breakdown', []):
        lines.append(f'  {st}: {cnt:,}')
    lines.append('')

    pm_data = s.get('payment_model_breakdown', [])
    if pm_data:
        lines.append('<b>Модель оплаты (активные кампании):</b>')
        total_pm = sum(cnt for _, cnt in pm_data)
        for pm, cnt in pm_data:
            lines.append(f'  {(pm or "не указано"):<22} {cnt:,}')
        lines.append(f'  {"Всего":<22} {total_pm:,}')
        lines.append('')

    # check_utm (CAMPAIGN_STATUS_SOURCE_FIX_2026-07-02: dg = campaign_status, не big_analytics_direct)
    dg     = s.get('direct_camp_groups', 0)
    cu_tot = s.get('check_utm_total', 0)
    cu_cls = s.get('check_utm_cls_filled', 0)
    pct_cu = f'{100 * cu_tot / dg:.1f}%' if dg else '—'
    lines += [
        '<b>UTM-аудит (check_utm):</b>',
        f'  Кампаний в campaign_status: {dg:,}',
        f'  Проверено в check_utm:      {cu_tot:,} ({pct_cu})',
        f'  Заполнен cls:               {cu_cls:,}',
    ]
    if cu_tot == 0:
        lines.append('  (check_utm заполняется ночным пайплайном — step13_utm_direct_audit)')
    for cls_val, cnt in s.get('check_utm_by_cls', []):
        lines.append(f'  {(cls_val or "NULL"):<16} {cnt:,}')
    lines.append('')

    bad = s.get('bad_utm_by_directolog', [])
    if bad:
        lines.append('<b>Неверный UTM по директологам:</b>')
        for d, cnt in bad:
            lines.append(f'  {(d or "—"):<22} {cnt:,} кампаний')
        lines.append('')

    # Пиксели с расходом без специалиста — разбивка по salon_name
    # PIXEL_NOSPEC_MIN_COST_2026-06-18: порог из SQL-блока выше
    # PIXEL_NOSPEC_SALON_DEDUP_2026-07-02: tuple (salon_name, cost, n_domains) —
    # один салон = одна строка; если у салона несколько доменов — сумма + "(N дом.)" рядом.
    pix_no_spec = s.get('pixels_no_specialist', [])
    pix_threshold = s.get('pixel_nospec_threshold', 10_000)
    if pix_no_spec:
        pix_total = sum(c for _, c, _ in pix_no_spec)
        n_domains_total = sum(nd for _, _, nd in pix_no_spec)
        pix_above = [(sn, c, nd) for sn, c, nd in pix_no_spec if c >= pix_threshold]
        pix_below = [(sn, c, nd) for sn, c, nd in pix_no_spec if c < pix_threshold]
        pix_below_total = sum(c for _, c, _ in pix_below)
        lines.append(
            f'<b>⚠️ Пиксели без специалиста ({len(pix_no_spec)} салонов'
            f' / {n_domains_total} доменов, расход: {pix_total:,.0f} ₽):</b>'
        )
        for salon_name, cost, n_domains in pix_above:
            dom_note = f' ({n_domains} дом.)' if n_domains > 1 else ''
            name_with_note = f'{salon_name}{dom_note}'
            lines.append(f'  {name_with_note:<42} {cost:>12,.0f} ₽')
        if pix_below:
            lines.append(f'  ... ещё {len(pix_below)} салонов на {pix_below_total:,.0f} ₽')
        lines.append('')

    # Обычные аккаунты с расходом без специалиста (не пиксель)
    no_spec = s.get('accounts_no_specialist', [])
    if no_spec:
        total_cost_no_spec = sum(float(c or 0) for _, c in no_spec)
        lines.append(f'<b>⚠️ Аккаунты с расходом, но без специалиста ({len(no_spec)}):</b>')
        lines.append(f'  Расход всего: {total_cost_no_spec:,.0f} ₽')
        for login, cost in no_spec:
            lines.append(f'  {login:<32} {float(cost or 0):>12,.0f} ₽')
        lines.append('')

    # Pixel — инвариант атрибуции
    pv = s.get('pixel_validation', {})
    if pv:
        px_z       = pv.get('px_z',        0)
        sc_z       = pv.get('sc_z',        0)
        px_korr    = pv.get('px_korr',     0)
        sc_korr    = pv.get('sc_korr',     0)
        px_kval    = pv.get('px_kval',     0)
        sc_kval    = pv.get('sc_kval',     0)
        px_priezd  = pv.get('px_priezd',   0)
        sc_priezd  = pv.get('sc_priezd',   0)
        px_prod    = pv.get('px_prodazhi', 0)
        sc_prod    = pv.get('sc_prodazhi', 0)
        px_cost    = pv.get('px_cost',     0)
        sc_cost    = pv.get('sc_cost',     0)
        mismatches = pv.get('domain_mismatches', 0)
        icon_pv    = '✅' if mismatches == 0 else '⚠️'
        lines += [
            f'<b>Пиксель — инвариант атрибуции {icon_pv}:</b>',
            f'  Доменов с расхождением: {mismatches}',
            f'  {"метрика":<12} {"pixel":>10}  {"score":>10}    Δ',
            f'  {"Заявки":<12} {px_z:>10,.0f}  {sc_z:>10,.0f}  {sc_z - px_z:>+8.2f}',
            f'  {"Корр":<12} {px_korr:>10,.0f}  {sc_korr:>10,.0f}  {sc_korr - px_korr:>+8.2f}',
            f'  {"Квал":<12} {px_kval:>10,.0f}  {sc_kval:>10,.0f}  {sc_kval - px_kval:>+8.2f}',
            f'  {"Приезд":<12} {px_priezd:>10,.0f}  {sc_priezd:>10,.0f}  {sc_priezd - px_priezd:>+8.2f}',
            f'  {"Продажи":<12} {px_prod:>10,.0f}  {sc_prod:>10,.0f}  {sc_prod - px_prod:>+8.2f}',
            f'  {"Расходы":<12} {px_cost:>10,.0f}  {sc_cost:>10,.0f}  {sc_cost - px_cost:>+8.2f} ₽',
        ]
        leftover_domains = pv.get('leftover_domains', [])
        if leftover_domains:
            total_ld_z    = sum(z    for _, z, _ in leftover_domains)
            total_ld_cost = sum(cost for _, _, cost in leftover_domains)
            lines.append(
                f'  ⚠️ Домены без кампаний (нет direct/crop/tp8): {len(leftover_domains)}'
                f', z={total_ld_z:,.0f}, cost={total_ld_cost:,.0f} ₽'
            )
            for domain, z, cost in leftover_domains:
                lines.append(f'  · {domain:<36} z={z:>7,.0f}  {cost:>12,.0f} ₽')
        lines.append('')

    # Покрытие логинов
    lc = s.get('login_coverage', {})
    if lc:
        total_lc  = lc.get('total', 0)
        in_yan    = lc.get('in_yandex', 0)
        in_full   = lc.get('in_full', 0)
        miss_yan  = lc.get('missing_yandex', [])
        miss_full = lc.get('missing_full', [])

        # LOGIN_GUARD_2026-06-18: guard ложного ✅ при total_lc==0.
        # Если эталон пуст (raw_yandex была транзиентна → missing=[] → not missing=True → ложный ✅).
        # Теперь ✅ только когда total_lc > 0 И missing пуст.
        def _ok(missing):
            if total_lc == 0:
                return '⚠️ нет данных'
            return '✅' if not missing else f'⚠️ нет {len(missing)}'

        # LOGIN_FILTER_REDESIGN_2026-06-18: метка строки 'yandex' осмыслена.
        # Эталон (~711) = ВСЕ активные Авто, строка «с расходом» (~654) = подмножество.
        # missing_yandex (~57) = активные в gsheet без открутки (нет cost>0 в FDW).
        lines += [
            '<b>Покрытие аккаунтов (Авто, активные):</b>',
            f'  Эталон (gsheet_sites):         {total_lc}',
            f'  С расходом в FDW (cost>0):     {in_yan}  {_ok(miss_yan)}',
            f'  В big_analytics_full:          {in_full}  {_ok(miss_full)}',
        ]
        # LOGIN_COVERAGE_COMPACT_2026-07-02: top-N + "и ещё K" вместо длинной простыни.
        # Пояснение к каждой категории — одной строкой перед списком.
        _TOP_N_LOGINS = 5
        for label, missing, expl in [
            ('big_analytics_full',
             miss_full,
             'есть в gsheet, но не попали в витрину (нет расхода в периоде)'),
            ('без расхода в FDW',
             miss_yan,
             'активны в gsheet, но нет открутки в Яндексе (на паузе или новые аккаунты)'),
        ]:
            if missing:
                lines.append(f'  ↳ {label} — {expl}:')
                shown  = missing[:_TOP_N_LOGINS]
                rest   = len(missing) - len(shown)
                suffix = f' и ещё {rest}' if rest > 0 else ''
                lines.append(f'    {", ".join(shown)}{suffix}')
        lines.append('')

    # Время выполнения
    # WALLTIME_FIX_2026-06-18: различаем wall-clock (фактическое) и сумму шагов.
    # При параллельных спендах сумма шагов > wall (3 билдера × ~32 мин = ~96 мин,
    # тогда как wall = ~32 мин). Показываем wall первым и чётко.
    durations = s.get('step_durations', [])
    if durations:
        lines.append('<b>Время выполнения:</b>')
        total = 0.0
        for step, secs in durations:
            if secs:
                total += float(secs)
                label = STEP_LABELS.get(step, step)
                lines.append(f'  {step} ({label}): {_fmt_sec(secs)}')
        # Сумма шагов — всегда показываем, с явной пометкой о параллели
        h_s  = int(total) // 3600
        m_s  = (int(total) % 3600) // 60
        sc_s = int(total) % 60
        lines.append(f'  Σ по шагам (включая параллельные): {h_s}ч {m_s}м {sc_s:02d}с')
        # Wall-clock — показываем если передан из fast_pipeline (параллельный прогон)
        wall = s.get('pipeline_wall_sec')
        if wall:
            h_w  = int(wall) // 3600
            m_w  = (int(wall) % 3600) // 60
            sc_w = int(wall) % 60
            lines += ['', f'🕒 Фактическое время прогона (wall): {h_w}ч {m_w}м {sc_w:02d}с']
        else:
            # pipeline.py — параллели нет, сумма ≈ wall
            lines += ['', f'🕒 Общее время: {h_s}ч {m_s}м {sc_s:02d}с']

    return '\n'.join(lines)


# ── Точка входа шага ─────────────────────────────────────────────────────────

def _check_and_report_utm_issues(conn) -> None:
    """Проверяет UTM-ошибки в local_telega_in_orders и постит persistent issues на дашборд."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE (utm_campaign IS NULL OR utm_campaign = '')
                                    AND status IN ('complete')) AS no_campaign,
                    SUM(price) FILTER (WHERE (utm_campaign IS NULL OR utm_campaign = '')
                                      AND status IN ('complete')) AS no_campaign_cost,
                    COUNT(*) FILTER (WHERE utm_source IS NULL
                                    AND status IN ('complete')) AS no_source,
                    COUNT(*) FILTER (WHERE utm_content IS NOT NULL
                                    AND LENGTH(utm_content) < 6
                                    AND status IN ('complete')) AS short_content
                FROM local_telega_in_orders
            """)
            row = cur.fetchone()
            if not row:
                return
            no_campaign, no_campaign_cost, no_source, short_content = row

            if no_campaign and no_campaign > 0:
                cost_str = f'{(no_campaign_cost or 0):,.0f}'.replace(',', ' ')
                _upsert_issue(
                    'telegain_no_utm_campaign', 'err',
                    f'telega.in: {no_campaign} строк без utm_campaign ({cost_str} ₽) — лиды не атрибутированы',
                    count=no_campaign,
                )
            else:
                _resolve_issue('telegain_no_utm_campaign')

            if no_source and no_source > 0:
                _upsert_issue(
                    'telegain_no_utm_source', 'warn',
                    f'telega.in: {no_source} строк без utm_source (нет "?" в URL) — источник не определён',
                    count=no_source,
                )
            else:
                _resolve_issue('telegain_no_utm_source')

            if short_content and short_content > 0:
                _upsert_issue(
                    'telegain_short_utm_content', 'warn',
                    f'telega.in: {short_content} строк с обрезанным utm_content (< 6 символов)',
                    count=short_content,
                )
            else:
                _resolve_issue('telegain_short_utm_content')

    except Exception as e:
        logger.warning('UTM issue check failed: %s', e)


def _check_and_report_crop_empty_channel(conn) -> None:
    """Data-quality сигнал: посевные лиды с пустым «Каналом» в листе лидов
    (gsheets_crop_targeting_account_leads). Эти строки приходят сиротами в витрину —
    step3._build_crop_sql пытается восстановить канал из листа закупов (каскад utm+Сайт →
    utm, дата-гард ±7 дней, только однозначный канал), невосстановимое уходит под лейбл
    '(посев: канал не определён)'. Сигнал нужен, чтобы менеджеры дозаполнили «Канал» в листе.
    См. POSEV_LOSSES_PLAYBOOK.md «пустой Канал в листе лидов»."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*)                                  AS rows_cnt,
                    COALESCE(SUM(COALESCE(kol_vo_zayavok, 0)), 0) AS zayavok,
                    (SELECT string_agg(utm, ', ')
                       FROM (
                         SELECT DISTINCT TRIM("utm утвержденная") AS utm
                         FROM gsheets_crop_targeting_account_leads
                         WHERE COALESCE(TRIM("Канал"), '') = ''
                           AND COALESCE(TRIM("utm утвержденная"), '') <> ''
                         LIMIT 5
                       ) s)                                   AS utm_primer
                FROM gsheets_crop_targeting_account_leads
                WHERE COALESCE(TRIM("Канал"), '') = ''
            """)
            row = cur.fetchone()
            if not row:
                return
            rows_cnt, zayavok, utm_primer = row

            if rows_cnt and rows_cnt > 0:
                primer = f' Примеры utm: {utm_primer}.' if utm_primer else ''
                _upsert_issue(
                    'crop_empty_channel', 'warn',
                    f'посевы: {rows_cnt} лид-строк ({int(zayavok or 0)} заявок) без «Канала» '
                    f'в gsheets_crop_targeting_account_leads — дозаполните «Канал» в листе лидов.{primer}',
                    count=rows_cnt,
                )
            else:
                _resolve_issue('crop_empty_channel')

    except Exception as e:
        logger.warning('Crop empty-channel check failed: %s', e)


# ── MISSING_ACCOUNTS_TG_2026-06-19 — Авто-аккаунты без gsheet_sites ──────────
# Отправляет ОТДЕЛЬНОЕ TG-сообщение со списком логинов Директа, у которых есть
# расход в FDW (yandex_direct_manager_reports, >= DATE_FROM) но которых НЕТ ни в
# одной строке local_gsheet_sites (ни под каким direction).
#
# Почему "нет в gsheet_sites вообще" = потеря:
#   step3._build_direct_sql делает JOIN big_analytics_direct → local_gsheet_sites
#   по account_login = login_key. Если логина нет в gsheet_sites — строка не
#   получит direction/специалиста/domain → выпадает из витрины big_analytics_full.
#   total_cost этих аккаунтов не учитывается нигде (теряется из финала).
#
# Почему НЕ фильтруем по direction='Авто':
#   Если аккаунта НЕТ в gsheet_sites — его direction НЕИЗВЕСТЕН. Нельзя
#   определить, Авто это или ВМ/Digital. Поэтому шлём все такие аккаунты с
#   пометкой «direction неизвестен» — пользователь сам решает, дозаполнить ли
#   этот аккаунт в gsheet. Аккаунты, которые ЕСТЬ в gsheet но не-Авто (ВМ и
#   пр.), — НЕ теряются (их расход идёт в своё направление), не включаем.
#
# Источник: FDW yandex_direct_manager_reports. _active_logins уже материализован
#   в _collect_login_coverage → повторный FDW-скан. Но эта функция вызывается
#   ПОСЛЕ _collect_final_stats (которая вызывает _collect_login_coverage) — к
#   этому моменту _active_logins DROP'нута (temp). Поэтому делаем свой легковесный
#   скан с DATE-фильтром; FDW сканируется однократно для этого запроса.
#
# PORG_GSHEET_ALERT_EXCLUDE_2026-07-06: технические/архивные аккаунты, которые
#   намеренно не добавляются в gsheet_sites (нет живых салонов — расход мизерный,
#   владелец продукта принял решение игнорировать в алерте). Добавлять сюда, а НЕ
#   в общую логику атрибуции/build_star — scope строго ограничен этим уведомлением.
EXCLUDED_FROM_GSHEET_ALERT: frozenset[str] = frozenset({
    'porg-2jd6mkdf',  # технический аккаунт без живого салона; расход ~2 050 ₽ — by design
})


def _send_missing_accounts_tg(conn, date_from: str) -> None:
    """MISSING_ACCOUNTS_TG_2026-06-19: шлёт TG-уведомление об аккаунтах Директа
    с расходом (>= date_from), которых нет в local_gsheet_sites ни под каким direction.
    Обёртка try/except — не роняет пайплайн при любой ошибке."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    fdw.account_login,
                    ROUND(SUM(fdw."Cost"::NUMERIC / 1.2), 0) AS cost_no_vat
                FROM public.yandex_direct_manager_reports fdw
                WHERE fdw.total_cost > 0
                  AND fdw.account_login IS NOT NULL
                  AND fdw."Date"::DATE >= %s
                  AND NOT EXISTS (
                      SELECT 1
                      FROM public.local_gsheet_sites gs
                      WHERE gs.login_key = fdw.account_login
                  )
                GROUP BY fdw.account_login
                ORDER BY cost_no_vat DESC
            """, (date_from,))
            rows = cur.fetchall()

        # PORG_GSHEET_ALERT_EXCLUDE_2026-07-06: исключаем технические аккаунты
        rows = [r for r in rows if r[0] not in EXCLUDED_FROM_GSHEET_ALERT]

        if not rows:
            msg = '✅ Все аккаунты Директа со спендом есть в gsheet_sites'
        else:
            total = sum(float(r[1] or 0) for r in rows)
            lines = ['⚠️ <b>Аккаунты Директа без gsheet_sites (расход теряется из финала):</b>',
                     '(direction неизвестен — дозаполните в gsheet)',
                     '']
            for login, cost in rows:
                lines.append(f'  <code>{login}</code> — {float(cost or 0):,.0f} ₽')
            lines.append('')
            lines.append(f'<b>Итого потерянный расход: {total:,.0f} ₽</b>')
            lines.append(f'<i>Период: с {date_from}</i>')
            msg = '\n'.join(lines)

        send_telegram(msg)
        logger.info(
            'MISSING_ACCOUNTS_TG_2026-06-19: отправлено, аккаунтов=%d', len(rows)
        )
    except Exception as e:
        logger.warning('MISSING_ACCOUNTS_TG_2026-06-19: ошибка (пайплайн не прерван): %s', e)


# ── PERFORM_FUNNEL_2026-07-06: guard свежести заявок Перформа ─────────────────
# Срабатывает если:
#   1) MAX(created_date) в local_perform_leads < CURRENT_DATE - 3  (данные протухли)
#   2) Расход Перформа за период лага > 1 000 000 ₽ (есть активные трат)
# Защита от повторения инцидента (июль 2026): данные не поступали несколько дней,
# CPL рассчитывался по устаревшим заявкам → искажение метрики.
# Использует big_analytics_full (доступна к моменту запуска step8).

def _check_perform_leads_freshness(conn) -> None:
    """PERFORM_FUNNEL_2026-07-06: TG-предупреждение о протухших заявках Перформа.

    Не роняет пайплайн при любой ошибке (try/except).
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    MAX(created_date)::DATE                            AS max_created,
                    (CURRENT_DATE - MAX(created_date)::DATE)::INTEGER  AS lag_days
                FROM public.local_perform_leads
            """)
            row = cur.fetchone()

        if not row or row[0] is None:
            logger.info('PERFORM_FUNNEL_2026-07-06: local_perform_leads пустая — freshness check пропущен')
            return

        max_created, lag_days = row
        if lag_days is None or lag_days <= 3:
            logger.info(
                'PERFORM_FUNNEL_2026-07-06: perform_leads актуальны (max_date=%s, lag=%s дн.) — OK',
                max_created, lag_days,
            )
            return

        # Лаг > 3 дней — проверяем расход Перформа за период лага
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COALESCE(SUM(total_cost), 0) AS spend_perform
                FROM public.big_analytics_full
                WHERE "направление" = 'Перформ'
                  AND "Date" >= CURRENT_DATE - %s
            """, (lag_days + 1,))
            spend_row = cur.fetchone()

        spend = float(spend_row[0]) if spend_row else 0.0

        if spend > 1_000_000:
            msg = (
                f'⚠️ <b>Перформ: заявки устарели!</b>\n'
                f'Последняя заявка в local_perform_leads: <b>{max_created}</b> '
                f'(отставание {lag_days} дн.)\n'
                f'Расход Перформа за {lag_days + 1} дн.: <b>{spend:,.0f} ₽</b>\n'
                f'<i>CPL считается по устаревшим данным — нужно обновить perform_leads в источнике.</i>'
            )
            send_telegram(msg)
            logger.warning(
                'PERFORM_FUNNEL_2026-07-06: perform_leads устарели на %d дн., расход=%.0f — TG отправлен',
                lag_days, spend,
            )
        else:
            logger.info(
                'PERFORM_FUNNEL_2026-07-06: perform_leads max=%s, lag=%d дн., spend=%.0f — расход < 1М, guard OK',
                max_created, lag_days, spend,
            )
    except Exception as e:
        logger.warning('PERFORM_FUNNEL_2026-07-06 freshness check: ошибка (пайплайн не прерван): %s', e)


def run(conn, run_id: str, pipeline_wall_sec=None, **kwargs) -> dict:
    logger.info('Шаг 8: статистика + Telegram-отчёт')
    t0 = time.perf_counter()

    stats = _collect_final_stats(conn, run_id)

    logger.info(
        'full=%s, direct=%s, leads=%s, priezd=%s, cost=%s',
        f"{stats.get('rows_full', 0):,}",
        f"{stats.get('rows_direct', 0):,}",
        f"{stats.get('total_leads', 0):,}",
        f"{stats.get('total_priezd', 0):,}",
        f"{stats.get('total_cost', 0):,.0f}",
    )

    # step8 ещё не записан в data_quality_log (run_step логирует после возврата),
    # поэтому добавляем синтетическую запись о самом себе для отчёта.
    durations = list(stats.get('step_durations', []))
    durations.append(('step8', time.perf_counter() - t0))
    stats['step_durations'] = durations
    # WALLTIME_FIX_2026-06-18: фактическое wall-clock время прогона (от fast_pipeline).
    # None при вызове из pipeline.py (там параллели нет, сумма ≈ wall).
    if pipeline_wall_sec is not None:
        stats['pipeline_wall_sec'] = pipeline_wall_sec
    stats['pipeline_degraded'] = bool(kwargs.get('pipeline_degraded'))
    stats['degraded_steps'] = list(kwargs.get('degraded_steps') or [])

    report = _format_final_report(stats, run_id)
    logger.info(
        '\n%s',
        report
        .replace('<b>', '').replace('</b>', '')
        .replace('<code>', '').replace('</code>', ''),
    )
    send_telegram(report)
    _check_and_report_utm_issues(conn)
    _check_and_report_crop_empty_channel(conn)
    # MISSING_ACCOUNTS_TG_2026-06-19: отдельное TG-сообщение с аккаунтами без gsheet_sites
    _send_missing_accounts_tg(conn, stats.get('recon_period_from', DATE_FROM))
    # PERFORM_FUNNEL_2026-07-06: guard свежести заявок Перформа (TG если lag > 3 дн. и расход > 1М)
    _check_perform_leads_freshness(conn)

    elapsed = time.perf_counter() - t0
    return {
        'rows': stats.get('rows_full', 0),
        'details': (
            f"full={stats.get('rows_full', 0):,}, "
            f"leads={stats.get('total_leads', 0):,}, "
            f"priezd={stats.get('total_priezd', 0):,}"
        ),
    }
