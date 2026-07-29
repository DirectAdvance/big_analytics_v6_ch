"""
step9_direct_history/step9.py — история изменений Яндекс.Директ (шаг 9)

Двухфазная архитектура:

  Фаза A — prefetch_history() — запускается из pipeline.py сразу после step0
  ─────────────────────────────────────────────────────────────────────────────
  Читает активные логины из local_gsheet_sites (status='Контекст активно')
  Удаляет из direct_history строки по логинам которые стали неактивными
  Для каждого логина определяет период:
    • нет данных → последние 30 дней (полная)
    • есть данные → от MAX(date)+1 до сегодня (инкрементальная)
    • MAX(date) = сегодня → пропускаем (актуально)
  Пишет в public.direct_history (ad_analytics_bi)
  Выполняется в фоновом потоке пока идут шаги 1–8

  Фаза B — run() — step9 в пайплайне
  ─────────────────────────────────────────────────────────────────────────────
  Ожидает завершения фонового потока (join)
  Обогащает direct_history: директолог, domain, salon из local_gsheet_sites

Куки: GET http://192.168.0.202:8765/cookies  X-API-Key: victory-gateway-key-2026
"""

import json
import logging
import os
from collections import defaultdict
import threading
import time
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote

import psycopg2.extras
import requests

from config.cookies import check_cookies_alive, send_tg_cookies_dead
from config.db import get_conn, put_conn
from config.settings import T_GSHEET_SITES, T_DIRECT_HISTORY, T_YANDEX_LOCAL

logger = logging.getLogger('pipeline.step9')

# ── Конфиг ────────────────────────────────────────────────────────────────────

COOKIES_URL  = 'http://192.168.0.202:8765/cookies'
COOKIES_KEY  = 'victory-gateway-key-2026'
COOKIES_FILE = os.path.join(os.path.dirname(__file__), '..', 'cookies.json')

DAYS_BACK     = 30     # дней при первой загрузке
PAGE_LIMIT    = 200    # макс записей за запрос (лимит API)
REQUEST_DELAY = 1.0    # пауза между страницами (сек)
LOGIN_DELAY   = 0.5    # пауза между логинами (сек)

ACTIVE_STATUS = 'Контекст активно'

# ── GraphQL запрос (внутренний API direct.yandex.ru) ─────────────────────────

GRAPHQL_QUERY = """
query userActionLog(
  $login:String $campaignIds:[Long!] $adGroupIds:[Long!] $adIds:[Long!]
  $logins:[String!] $limit:Int=200 $token:String
  $dateFrom:LocalDateTime $dateTo:LocalDateTime
  $categories:[CategoryInput!] $order:OrderInput $changeSources:[ChangeSourceInput!]
){
  userActionLog(
    clientLogin:$login campaignIds:$campaignIds adGroupIds:$adGroupIds
    adIds:$adIds logins:$logins limit:$limit pageToken:$token
    dateFrom:$dateFrom dateTo:$dateTo categories:$categories
    order:$order changeSources:$changeSources
  ){
    nextPageToken
    logRecords{
      datetime
      user{ uid login }
      changeSource
      gtid
      event{
        ...on CampaignValueChangeEvent{ __typename category clientId campaign{...LogCampaignView} oldValue newValue }
        ...on CampaignStatusChangeEvent{ __typename category clientId campaign{...LogCampaignView} }
        ...on CampaignStrategyEvent{ __typename category clientId campaign{...LogCampaignView} currencyCode oldStrategy{...FlatStrategyFragment} newStrategy{...FlatStrategyFragment} }
        ...on CampaignListChangeEvent{ __typename category clientId campaign{...LogCampaignView} oldList newList }
        ...on CampaignTimeTargetEvent{ __typename category clientId campaign{...LogCampaignView} }
        ...on CampaignNetworkEvent{ __typename category clientId campaign{...LogCampaignView} }
        ...on CampaignRegionsEvent{ __typename category clientId campaign{...LogCampaignView} oldRegions newRegions }
        ...on BannersEvent{ __typename category clientId campaign{...LogCampaignView} adGroup{id name} ads{id title} }
        ...on SingleMultiplierEvent{ __typename category clientId campaign{...LogCampaignView} adGroup{id name} oldMultiplier newMultiplier }
        ...on DemographyMultipliersEvent{ __typename category clientId campaign{...LogCampaignView} adGroup{id name} }
        ...on AdOptionsEvent{ __typename category adGroup{id name} campaign{...LogCampaignView} ads{id title} oldValue newValue }
        ...on AgencyChangeEvent{ __typename category oldAgencyName newAgencyName }
      }
    }
  }
}
fragment LogCampaignView on CampaignView{ id name calcType source }
fragment FlatStrategyFragment on GdCampaignFlatStrategy{
  __typename strategyType
  ...on GdCampaignStrategyAvgCpaPerCamp{ __typename bid avgCpa payForConversion attributionModel }
  ...on GdCampaignStrategyAvgCpcPerCamp{ __typename attributionModel bid avgBid }
  ...on GdCampaignStrategyManual{ __typename separateBidding attributionModel }
  ...on GdStrategyOptimizeClicks{ __typename avgBid attributionModel bid }
  ...on GdStrategyOptimizeConversions{ __typename avgCpa payForConversion bid goalId }
  platform isAutoBudget attributionModel
  budget{ autoProlongation start finish period sum }
}
"""

# ── Справочники событий ───────────────────────────────────────────────────────

EVENT_TYPE_RU = {
    'CampaignValueChangeEvent':    'Изменение параметра кампании',
    'CampaignStatusChangeEvent':   'Изменение статуса кампании',
    'CampaignStrategyEvent':       'Изменение стратегии',
    'CampaignListChangeEvent':     'Изменение списков',
    'CampaignTimeTargetEvent':     'Изменение временного таргетинга',
    'CampaignNetworkEvent':        'Изменение настроек сетей',
    'CampaignRegionsEvent':        'Изменение регионов',
    'BannersEvent':                'Изменение объявлений',
    'SingleMultiplierEvent':       'Изменение корректировки ставки',
    'DemographyMultipliersEvent':  'Изменение демографических корректировок',
    'AdOptionsEvent':              'Изменение параметров объявления',
    'AgencyChangeEvent':           'Изменение агентства',
}

CATEGORY_RU = {
    'CAMPAIGN_STRATEGY':              'Стратегия',
    'CAMPAIGN_STATUS':                'Статус кампании',
    'CAMPAIGN_VALUE':                 'Параметр кампании',
    'CAMPAIGN_LIST':                  'Списки',
    'CAMPAIGN_TIME_TARGET':           'Временной таргетинг',
    'CAMPAIGN_NETWORK':               'Настройки сетей',
    'CAMPAIGN_REGIONS':               'Регионы',
    'CAMPAIGN_EXTENDED_GEOTARGETING': 'Изменение гео таргетинга',
    'BANNERS':                        'Объявления',
    'MULTIPLIER':                     'Корректировки ставок',
    'AD_OPTIONS':                     'Параметры объявлений',
}

CATEGORY_SKIP = {'BANNERS_HIDE'}


# ── DDL ───────────────────────────────────────────────────────────────────────

def _ensure_table(conn) -> None:
    """Создать таблицу direct_history и индексы если не существуют."""
    with conn.cursor() as cur:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {T_DIRECT_HISTORY} (
                id            BIGSERIAL PRIMARY KEY,
                ulogin        TEXT,
                datetime      TIMESTAMPTZ,
                user_login    TEXT,
                user_uid      BIGINT,
                change_source TEXT,
                event_type    TEXT,
                category      TEXT,
                campaign_id   BIGINT,
                campaign_name TEXT,
                ad_group_id   BIGINT,
                ad_group_name TEXT,
                old_value     TEXT,
                new_value     TEXT,
                raw_event     JSONB,
                директолог    TEXT,
                domain        TEXT,
                salon         TEXT,
                loaded_at     TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        for ddl in [
            # INDEX_AUDIT_2026-06-27: удалены мёртвые (idx_scan=0):
            #   idx_dh_datetime, idx_dh_campaign, idx_dh_category.
            # Оставлен idx_dh_ulogin (11 scans).
            f"CREATE INDEX IF NOT EXISTS idx_dh_ulogin ON {T_DIRECT_HISTORY}(ulogin)",
        ]:
            cur.execute(ddl)
    conn.commit()


# ── Куки ──────────────────────────────────────────────────────────────────────

def _fetch_cookies() -> dict:
    """Загрузить cookies: сначала из файла, затем с домашнего сервера."""
    fpath = os.path.normpath(COOKIES_FILE)
    if os.path.exists(fpath):
        try:
            with open(fpath, encoding='utf-8') as f:
                data = json.load(f)
            logger.info('[history] Куки из файла: %d аккаунтов', len(data))
            return data
        except Exception as e:
            logger.warning('[history] Ошибка чтения cookies.json: %s', e)
    try:
        resp = requests.get(COOKIES_URL, headers={'X-API-Key': COOKIES_KEY}, timeout=3)
        resp.raise_for_status()
        data = resp.json()
        logger.info('[history] Куки с домашнего сервера: %d аккаунтов', len(data))
        return data
    except Exception as e:
        logger.warning('[history] Куки недоступны (домашний сервер): %s', e)
        return {}


# ── API ───────────────────────────────────────────────────────────────────────

def _make_headers(ulogin: str, cookie: str) -> dict:
    return {
        'Cookie':             cookie,
        'Accept':             '*/*, application/json',
        'Accept-Language':    'ru',
        'Content-Type':       'application/json',
        'Origin':             'https://direct.yandex.ru',
        'Referer':            f'https://direct.yandex.ru/dna/log/?ulogin={quote(ulogin)}',
        'User-Agent':         'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/147.0.0.0 Safari/537.36',
        'dna-operation-name': 'userActionLog',
        'x-direct-api':       '1',
        'x-detected-locale':  'ru',
        'x-page-pathname':    '/dna/log/',
    }


def _fetch_page(session: requests.Session, ulogin: str, cookie: str,
                date_from: str, date_to: str, page_token: Optional[str]) -> Optional[dict]:
    url = (
        f'https://direct.yandex.ru/web-api/user-action-log/api'
        f'?operationName=userActionLog&ulogin={ulogin}'
    )
    payload = {
        'operationName': 'userActionLog',
        'query':         GRAPHQL_QUERY,
        'variables': {
            'login':         ulogin,
            'dateFrom':      date_from,
            'dateTo':        date_to,
            'limit':         PAGE_LIMIT,
            'order':         'DESC',
            'token':         page_token,
            'campaignIds':   None,
            'adGroupIds':    None,
            'adIds':         None,
            'logins':        None,
            'categories':    None,
            'changeSources': None,
        },
    }
    try:
        resp = session.post(url, headers=_make_headers(ulogin, cookie), json=payload, timeout=30)
        return resp.json()
    except Exception as e:
        logger.warning('[history] Ошибка запроса %s: %s', ulogin, e)
        return None


# ── Парсинг ───────────────────────────────────────────────────────────────────

def _extract_value(event: dict, direction: str) -> Optional[str]:
    for key in (f'{direction}Value', f'{direction}Multiplier'):
        if key in event:
            v = event[key]
            return str(v) if not isinstance(v, (dict, list)) else json.dumps(v, ensure_ascii=False)
    for key in (f'{direction}Strategy', f'{direction}List', f'{direction}Network',
                f'{direction}TimeTarget', f'{direction}Regions'):
        if key in event:
            return json.dumps(event[key], ensure_ascii=False)
    return None


def _parse_record(rec: dict, ulogin: str) -> dict:
    event    = rec.get('event') or {}
    campaign = event.get('campaign') or {}
    ad_group = event.get('adGroup') or {}
    user     = rec.get('user') or {}
    return {
        'ulogin':        ulogin,
        'datetime':      rec.get('datetime'),
        'user_login':    user.get('login'),
        'user_uid':      user.get('uid'),
        'change_source': rec.get('changeSource'),
        'event_type':    EVENT_TYPE_RU.get(event.get('__typename', ''), event.get('__typename', '')),
        'category':      CATEGORY_RU.get(event.get('category', ''), event.get('category', '')),
        'campaign_id':   campaign.get('id'),
        'campaign_name': campaign.get('name'),
        'ad_group_id':   ad_group.get('id'),
        'ad_group_name': ad_group.get('name'),
        'old_value':     _extract_value(event, 'old'),
        'new_value':     _extract_value(event, 'new'),
        'raw_event':     json.dumps(event, ensure_ascii=False),
    }


# ── Загрузка одного логина ────────────────────────────────────────────────────

def _fetch_login(session: requests.Session, ulogin: str, cookies: dict,
                 date_from: str, date_to: str) -> list:
    """Загрузить все страницы истории для одного логина. Перебирает куки."""
    working_cookie = None
    first_data     = None

    for account, cookie in cookies.items():
        data = _fetch_page(session, ulogin, cookie, date_from, date_to, None)
        if data is None:
            continue
        if isinstance(data, dict) and data.get('text') == 'Нет прав':
            logger.debug('[history] %s: нет прав через %s', ulogin, account)
            continue
        if data.get('errors'):
            logger.debug('[history] %s: GraphQL ошибка через %s', ulogin, account)
            continue
        working_cookie = cookie
        first_data     = data
        break

    if working_cookie is None:
        return []

    rows = []
    log_data   = (first_data.get('data') or {}).get('userActionLog') or {}
    records    = log_data.get('logRecords') or []
    next_token = log_data.get('nextPageToken')

    rows.extend([
        _parse_record(r, ulogin) for r in records
        if (r.get('event') or {}).get('category') not in CATEGORY_SKIP and r.get('event')
    ])

    while next_token:
        time.sleep(REQUEST_DELAY)
        data = _fetch_page(session, ulogin, working_cookie, date_from, date_to, next_token)
        if data is None:
            break
        log_data   = (data.get('data') or {}).get('userActionLog') or {}
        records    = log_data.get('logRecords') or []
        next_token = log_data.get('nextPageToken')
        rows.extend([
            _parse_record(r, ulogin) for r in records
            if (r.get('event') or {}).get('category') not in CATEGORY_SKIP and r.get('event')
        ])

    return rows


# ── БД: вспомогательные ───────────────────────────────────────────────────────

def _get_active_logins(conn) -> list:
    """Активные логины из local_gsheet_sites + manager_login из yandex_local.

    Возвращает list of (login_key, manager_login).
    """
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT gs.login_key, MAX(y.manager_login) AS manager_login
            FROM {T_GSHEET_SITES} gs
            LEFT JOIN {T_YANDEX_LOCAL} y ON y.account_login = gs.login_key
                AND y.manager_login IS NOT NULL
            WHERE gs.status = %s
              AND gs.direction = 'Авто'
              AND gs.login_key IS NOT NULL AND gs.login_key != ''
              AND gs.directologist IS NOT NULL AND TRIM(gs.directologist) != ''
              AND gs.directologist IN (
                  SELECT name FROM public.specialists
                  WHERE name IS NOT NULL AND TRIM(name) != ''
              )
            GROUP BY gs.login_key
            ORDER BY gs.login_key
        """, (ACTIVE_STATUS,))
        rows = cur.fetchall()
    result = []
    for login, manager_login in rows:
        if not login.isascii():
            logger.warning('[history] Пропускаем невалидный login_key=%r (не ASCII)', login)
        elif not any(c.isalnum() for c in login):
            logger.warning('[history] Пропускаем невалидный login_key=%r (нет букв/цифр)', login)
        else:
            result.append((login, (manager_login or 'unknown').split('@')[0]))
    return result


def _delete_inactive(conn, active_logins: list) -> int:
    """Удалить строки по логинам которые больше не активны."""
    if not active_logins:
        return 0
    with conn.cursor() as cur:
        cur.execute(f"""
            DELETE FROM {T_DIRECT_HISTORY}
            WHERE ulogin != ALL(%s)
        """, (active_logins,))
        count = cur.rowcount
    conn.commit()
    return count


def _get_max_dates(conn, logins: list) -> dict:
    """Вернуть {ulogin: max_date} по московскому времени."""
    if not logins:
        return {}
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT ulogin,
                   MAX((datetime AT TIME ZONE 'Europe/Moscow')::date)
            FROM {T_DIRECT_HISTORY}
            WHERE ulogin = ANY(%s)
            GROUP BY ulogin
        """, (logins,))
        return {row[0]: row[1] for row in cur.fetchall()}


def _insert_rows(conn, rows: list) -> None:
    if not rows:
        return
    cols = [
        'ulogin', 'datetime', 'user_login', 'user_uid', 'change_source',
        'event_type', 'category', 'campaign_id', 'campaign_name',
        'ad_group_id', 'ad_group_name', 'old_value', 'new_value', 'raw_event',
    ]
    values = [tuple(r.get(c) for c in cols) for r in rows]
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            f"INSERT INTO {T_DIRECT_HISTORY} ({', '.join(cols)}) VALUES %s",
            values,
            page_size=500,
        )
    conn.commit()


# ── ФАЗА A: prefetch (запускается из pipeline.py после step0) ─────────────────

def _fetch_login_single(
    session: requests.Session,
    ulogin: str,
    cookie: str,
    date_from: str,
    date_to: str,
) -> tuple[list, bool, bool]:
    """Загрузить историю одного логина через одну куку.

    Возвращает (rows, had_error, no_access):
      had_error=True  → сетевая/API ошибка → retry через другую куку
      no_access=True  → «Нет прав» у этой куки → retry через другую куку
      оба False, rows=[] → успех, реально нет данных за период
    """
    data = _fetch_page(session, ulogin, cookie, date_from, date_to, None)
    if data is None:
        return [], True, False
    if isinstance(data, dict) and data.get('text') == 'Нет прав':
        return [], False, True
    if data.get('errors'):
        return [], True, False

    rows = []
    log_data   = (data.get('data') or {}).get('userActionLog') or {}
    records    = log_data.get('logRecords') or []
    next_token = log_data.get('nextPageToken')
    rows.extend([
        _parse_record(r, ulogin) for r in records
        if (r.get('event') or {}).get('category') not in CATEGORY_SKIP and r.get('event')
    ])

    while next_token:
        time.sleep(REQUEST_DELAY)
        data = _fetch_page(session, ulogin, cookie, date_from, date_to, next_token)
        if data is None:
            return rows, True, False
        log_data   = (data.get('data') or {}).get('userActionLog') or {}
        records    = log_data.get('logRecords') or []
        next_token = log_data.get('nextPageToken')
        rows.extend([
            _parse_record(r, ulogin) for r in records
            if (r.get('event') or {}).get('category') not in CATEGORY_SKIP and r.get('event')
        ])

    return rows, False, False


def _history_worker(
    manager_login: str,
    logins: list,
    cookie: str,
    max_dates: dict,
    date_to: str,
    failed_out: list,
) -> None:
    """Поток: обрабатывает свою группу логинов одной кукой. Ошибки → failed_out."""
    conn = get_conn()
    try:
        http_session = requests.Session()
        today        = datetime.now().date()
        total        = len(logins)
        inserted     = 0
        skipped      = 0

        for i, ulogin in enumerate(logins, 1):
            max_date = max_dates.get(ulogin)

            if max_date is not None and max_date >= today:
                skipped += 1
                continue

            if max_date is None:
                date_from_dt = datetime.now() - timedelta(days=DAYS_BACK)
                mode = f'полная ({DAYS_BACK}д)'
            else:
                date_from_dt = datetime.combine(
                    max_date + timedelta(days=1), datetime.min.time()
                )
                mode = f'с {date_from_dt.strftime("%Y-%m-%d")}'

            date_from = date_from_dt.strftime('%Y-%m-%dT%H:%M:%S')
            logger.info('[history:%s] [%d/%d] %s (%s)', manager_login, i, total, ulogin, mode)

            rows, had_error, no_access = _fetch_login_single(
                http_session, ulogin, cookie, date_from, date_to
            )

            if had_error:
                logger.warning('[history:%s] %s: ошибка API → retry', manager_login, ulogin)
                failed_out.append((ulogin, max_date))
            elif no_access:
                logger.info('[history:%s] %s: нет прав у этой куки → retry перебором', manager_login, ulogin)
                failed_out.append((ulogin, max_date))
            elif rows:
                _insert_rows(conn, rows)
                inserted += len(rows)
                logger.info('[history:%s] %s: +%d записей', manager_login, ulogin, len(rows))
            elif max_date is None:
                logger.warning('[history:%s] %s: нет данных за период', manager_login, ulogin)
            else:
                logger.info('[history:%s] %s: нет новых записей', manager_login, ulogin)

            if i < total:
                time.sleep(LOGIN_DELAY)

        logger.info('[history:%s] Завершён: +%d записей, %d актуальны', manager_login, inserted, skipped)
    except Exception as e:
        logger.error('[history:%s] Ошибка: %s', manager_login, e, exc_info=True)
    finally:
        put_conn(conn)


def prefetch_history() -> None:
    """
    Фоновый поток: инкрементальная загрузка истории изменений Директа.
    Использует собственное соединение из пула.
    """
    logger.info('[history] Старт фонового получения истории изменений')
    conn = get_conn()
    try:
        _ensure_table(conn)

        logins_with_mgr = _get_active_logins(conn)  # [(login, manager_login)]
        if not logins_with_mgr:
            logger.warning('[history] Нет активных логинов (%s) в %s', ACTIVE_STATUS, T_GSHEET_SITES)
            return

        active_logins = [l for l, _ in logins_with_mgr]
        logger.info('[history] Активных логинов: %d', len(active_logins))

        deleted = _delete_inactive(conn, active_logins)
        if deleted:
            logger.info('[history] Удалено строк по неактивным логинам: %d', deleted)

        max_dates = _get_max_dates(conn, active_logins)
        date_to   = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')

    except Exception as e:
        logger.error('[history] Ошибка на старте: %s', e, exc_info=True)
        return
    finally:
        put_conn(conn)

    # Куки
    cookies = _fetch_cookies()
    if not cookies:
        logger.warning('[history] Нет куков — домашний сервер недоступен, пропускаем')
        return

    if not check_cookies_alive(cookies):
        logger.warning('[history] Куки протухли — шаг пропущен, старые данные сохранены')
        send_tg_cookies_dead('step9 (история изменений Директа)')
        return

    # Группируем по manager_login
    by_manager: dict[str, list] = defaultdict(list)
    for login, mgr in logins_with_mgr:
        by_manager[mgr].append(login)

    # 3 потока — каждый со своей кукой
    failed_out: list = []  # [(ulogin, max_date), ...]  — list.append GIL-safe
    threads = []

    for mgr, logins in by_manager.items():
        if mgr not in cookies:
            logger.warning('[history] Нет куки для manager_login=%s (%d лог.) → retry', mgr, len(logins))
            for login in logins:
                failed_out.append((login, max_dates.get(login)))
            continue

        t = threading.Thread(
            target=_history_worker,
            args=(mgr, logins, cookies[mgr], max_dates, date_to, failed_out),
            daemon=True,
            name=f'history_{mgr}',
        )
        t.start()
        threads.append(t)
        logger.info('[history] Поток запущен: %s (%d логинов)', mgr, len(logins))

    for t in threads:
        t.join()

    # Retry: провалившиеся логины перебором всех куки
    if failed_out:
        logger.info('[history] Retry: %d логинов перебором куки', len(failed_out))
        http_session  = requests.Session()
        all_cookies   = list(cookies.items())
        retry_conn    = get_conn()
        try:
            for ulogin, max_date in failed_out:
                if max_date is None:
                    date_from_dt = datetime.now() - timedelta(days=DAYS_BACK)
                else:
                    date_from_dt = datetime.combine(
                        max_date + timedelta(days=1), datetime.min.time()
                    )
                date_from = date_from_dt.strftime('%Y-%m-%dT%H:%M:%S')

                rows = None
                no_access_count = 0
                error_count     = 0
                for _, cookie in all_cookies:
                    r, had_error, no_access = _fetch_login_single(
                        http_session, ulogin, cookie, date_from, date_to
                    )
                    if not had_error and not no_access:
                        rows = r
                        break
                    if no_access:
                        no_access_count += 1
                    if had_error:
                        error_count += 1

                if rows:
                    _insert_rows(retry_conn, rows)
                    logger.info('[history:retry] %s: +%d записей', ulogin, len(rows))
                elif rows is not None:
                    logger.info('[history:retry] %s: нет данных за период', ulogin)
                else:
                    logger.warning('[history:retry] %s: ни одна кука не дала доступ (no_access=%d, errors=%d)',
                                   ulogin, no_access_count, error_count)

                time.sleep(LOGIN_DELAY)
        except Exception as e:
            logger.error('[history:retry] Ошибка: %s', e, exc_info=True)
        finally:
            put_conn(retry_conn)

    logger.info('[history] prefetch_history завершён')


# ── ФАЗА B: run() ─────────────────────────────────────────────────────────────

def run(conn, run_id: str, history_thread=None) -> dict:
    """
    conn           — DST соединение (ad_analytics_bi)
    history_thread — threading.Thread из pipeline.py, ждём завершения
    """
    logger.info('Шаг 9: история изменений Директа')

    if history_thread is not None and history_thread.is_alive():
        logger.info('Ожидаем завершения фонового получения истории...')
        history_thread.join()
        logger.info('Фоновый поток завершён')

    # Обогащение: директолог, domain, salon из local_gsheet_sites
    # DISTINCT ON login_key т.к. у одного логина может быть несколько доменов
    with conn.cursor() as cur:
        cur.execute(f"""
            UPDATE {T_DIRECT_HISTORY} dh
            SET директолог = gs.directologist,
                domain     = gs.domain,
                salon      = gs.salon
            FROM (
                SELECT DISTINCT ON (login_key)
                    login_key, directologist, domain, salon
                FROM {T_GSHEET_SITES}
                WHERE login_key IS NOT NULL
                ORDER BY login_key
            ) gs
            WHERE gs.login_key = dh.ulogin
        """)
        enriched = cur.rowcount
    conn.commit()
    logger.info('Обогащено строк: %d', enriched)

    with conn.cursor() as cur:
        cur.execute(f'SELECT COUNT(*), COUNT(DISTINCT ulogin) FROM {T_DIRECT_HISTORY}')
        row          = cur.fetchone()
        total_rows   = row[0] or 0
        total_logins = row[1] or 0

    logger.info('direct_history: %d строк, %d логинов', total_rows, total_logins)

    return {
        'rows': total_rows,
        'details': f'rows={total_rows:,}, logins={total_logins}, enriched={enriched}',
    }
