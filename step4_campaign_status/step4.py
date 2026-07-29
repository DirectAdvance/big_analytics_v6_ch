"""
step7_campaign_status_check_utm/step7.py — статусы кампаний Яндекс.Директ

Двухфазная архитектура (для параллельного запуска):

  Фаза A — prefetch_statuses() — запускается из pipeline.py сразу после step0
  ─────────────────────────────────────────────────────────────────────────────
  CAMPSTATUS_SRC_DIRECT_2026-06-23 (v2): читает вселенную кампаний из FDW
  yandex_direct_manager_reports (SRC_YANDEX) — первоисточник, доступен сразу
  (не строится в пайплайне). Poll-цикл убран. Вселенная = CampaignId + account_login
  + manager_login за последние 60 дней. "Date" в FDW — TEXT, каст "Date"::date обязателен.
  Запрашивает статусы через Yandex Direct Grid API (куки с домашнего сервера).
  Grid API возвращает ТЕКУЩИЙ статус — кампания тратила 60 дн. назад, сейчас
  остановлена → STOPPED → 'Остановлена'; архивирована → ARCHIVED → 'Архив'.
  Пишет результат в таблицу _campaign_statuses_prefetch (временная).
  Выполняется в фоновом потоке пока идут шаги 1–6.

  REALSTATUS_2026-06-22: scope prefetch БЕЗ фильтра по gsheet_sites
  (status/direction/directologist) — охват все кампании с расходом за 60 дней.

  Фаза B — run() — step4 в пайплайне
  ─────────────────────────────────────────────────────────────────────────────
  1. Ожидает завершения фонового потока (join)
  2. Строит campaign_status из big_analytics_direct + читает статусы из prefetch
  3. ТК/МК (tp8/tp9/tp10): реальные статусы через Grid API (куки glavpotok)
     TKMK_REALSTATUS_2026-06-24: вместо хардкода 'Активна' — синхронный Grid API
     запрос прямо в фазе B (когда big_analytics_crop_targeting уже готова после step3).
     Если Grid API не даёт статус — NULL, не 'Активна'.
  4. Добавляет campaign_status в big_analytics_direct (ALTER + UPDATE)

Куки: GET http://192.168.0.202:8765/cookies  X-API-Key: victory-gateway-key-2026
"""

import json
import logging
import os
import re
import time
import threading
from collections import Counter, defaultdict
import psycopg2
import psycopg2.extras
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from config.cookies import send_tg_cookies_dead
from config.db import get_conn, put_conn
from config.settings import (
    T_DIRECT,
    SRC_YANDEX,  # CAMPSTATUS_SRC_DIRECT_2026-06-23: FDW-источник вселенной кампаний
)

logger = logging.getLogger('pipeline.step7')

# ── Конфиг ────────────────────────────────────────────────────────────────────

COOKIES_URL  = 'http://192.168.0.202:8765/cookies'
COOKIES_KEY  = 'victory-gateway-key-2026'
COOKIES_FILE = os.path.join(os.path.dirname(__file__), '..', 'cookies.json')

T_CAMPAIGN_STATUS  = 'campaign_status'
T_PREFETCH         = '_campaign_statuses_prefetch'   # временная, удаляется в конце

# Кампании «активны» если есть данные за последние N дней в yandex_local
ACTIVE_DAYS = 60

PAGE_LIMIT = 200
GRID_URL   = 'https://direct.yandex.ru/web-api/grid/api'
USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/124.0.0.0 Safari/537.36'
)

PRIMARY_STATUS_MAP = {
    'ACTIVE':             'Активна',
    'RUN_WARN':           'Активна',
    'STOPPED':            'Остановлена',
    'TEMPORARILY_PAUSED': 'Остановлена',
    'DRAFT':              'Остановлена',
    'MODERATION':         'Остановлена',
    'MODERATION_DENIED':  'Остановлена',
    'ARCHIVED':           'Архив',
    'ENDED':              'Остановлена',
}

_STATUS_FILTER = [
    'ACTIVE', 'RUN_WARN', 'STOPPED', 'TEMPORARILY_PAUSED',
    'DRAFT', 'MODERATION', 'MODERATION_DENIED', 'ARCHIVED',
]

_GRID_QUERY = """
query GridCampaigns($login: String!, $campaignInput: GdCampaignsContainerInput!) {
  client(searchBy: {login: $login}) {
    campaigns(input: $campaignInput) {
      totalCount
      rowset {
        id
        status {
          primaryStatus
        }
        strategy {
          __typename
          ... on GdCampaignStrategyAvgCpa { payForConversion }
          ... on GdCampaignStrategyAvgCpaPerCamp { payForConversion }
          ... on GdCampaignStrategyAvgCpaPerFilter { payForConversion }
          ... on GdCampaignStrategyAvgCpi { payForConversion }
          ... on GdCampaignStrategyAvgCpv { payForConversion }
          ... on GdCampaignStrategyCrr { payForConversion }
          ... on GdCampaignStrategyMultipleCpa { payForConversion }
          ... on GdStrategyOptimizeConversions { payForConversion }
          ... on GdStrategyOptimizeInstalls { payForConversion }
        }
      }
    }
  }
}
"""


# ── Куки ──────────────────────────────────────────────────────────────────────

def _fetch_cookies() -> dict:
    """Загрузить cookies: сначала из файла, затем с домашнего сервера."""
    fpath = os.path.normpath(COOKIES_FILE)
    if os.path.exists(fpath):
        try:
            with open(fpath, encoding='utf-8') as f:
                data = json.load(f)
            logger.info('Куки из файла: %d аккаунтов', len(data))
            return data
        except Exception as e:
            logger.warning('Ошибка чтения cookies.json: %s', e)
    try:
        resp = requests.get(COOKIES_URL, headers={'X-API-Key': COOKIES_KEY}, timeout=3)
        resp.raise_for_status()
        data = resp.json()
        logger.info('Куки с домашнего сервера: %d аккаунтов', len(data))
        return data
    except Exception as e:
        logger.warning('Куки недоступны (домашний сервер): %s', e)
        return {}


# ── Grid API ──────────────────────────────────────────────────────────────────

def _post_grid(login: str, cookie: str, csrf, payload: dict,
               retries: int = 3, retry_delay: float = 30.0):
    headers = {
        'Cookie':             cookie,
        'dna-operation-name': 'GridCampaigns',
        'x-direct-api':       '1',
        'x-detected-locale':  'ru',
        'Content-Type':       'application/json',
        'User-Agent':         USER_AGENT,
    }
    if csrf:
        headers['x-csrf-token'] = csrf
    url = f'{GRID_URL}?operationName=GridCampaigns&ulogin={login}'
    for attempt in range(1, retries + 1):
        try:
            return requests.post(url, json=payload, headers=headers, timeout=60, verify=False)
        except Exception as e:
            if attempt < retries:
                logger.warning('Grid попытка %d/%d: %s, повтор через %ds', attempt, retries, e, retry_delay)
                time.sleep(retry_delay)
            else:
                logger.error('Grid запрос упал: %s', e)
    return None


def _extract_csrf(resp):
    csrf = resp.cookies.get('_direct_csrf_token')
    if csrf:
        return csrf
    for sc in resp.headers.get('Set-Cookie', '').split(','):
        m = re.search(r'_direct_csrf_token=([^;,\s]+)', sc)
        if m:
            return m.group(1)
    return None


# Тестовые клиентские логины для валидации куки (по одному на каждый агентский аккаунт)
_SESSION_TEST_LOGINS = {
    'victorylotsofads1':            'e-20074351',
    'victoryagency-direct1618440':  'acbu-spb-436222-ns89',
    'victoryagency14':              'porg-7uhutcdh',
}


def _is_valid_json_response(resp):
    """Проверяет что ответ — валидный JSON с данными, не HTML. Возвращает (ok, csrf)."""
    csrf = _extract_csrf(resp)
    try:
        data = resp.json()
    except Exception:
        return False, csrf
    if data.get('errors'):
        return False, csrf
    return 'data' in data, csrf


def _build_sessions(raw: dict) -> list:
    """Проверить куки через тестовый клиентский логин. Вернуть [(name, cookie, csrf)]."""
    sessions = []
    for name, cookie in raw.items():
        test_login = _SESSION_TEST_LOGINS.get(name, name)
        payload = {
            'operationName': 'GridCampaigns',
            'query':         _GRID_QUERY,
            'variables': {
                'login': test_login,
                'campaignInput': {
                    'filter':           {'filterStatusIn': _STATUS_FILTER},
                    'orderBy':          [{'order': 'ASC', 'field': 'STATUS'}],
                    'statRequirements': {'preset': 'CURRENT_WEEK', 'goalIds': [], 'useCampaignGoalIds': False},
                    'limitOffset':      {'limit': 1, 'offset': 0},
                },
            },
        }
        resp = _post_grid(test_login, cookie, None, payload)
        if resp is None:
            logger.warning('  %s: нет соединения', name)
            continue
        if resp.status_code == 403:
            csrf = _extract_csrf(resp)
            if not csrf:
                logger.warning('  %s: 403 без CSRF', name)
                continue
            resp2 = _post_grid(test_login, cookie, csrf, payload)
            if resp2 is None:
                logger.warning('  %s: нет соединения (retry)', name)
                continue
            ok, csrf2 = _is_valid_json_response(resp2)
            if ok:
                logger.info('  %s: OK (CSRF)', name)
                sessions.append((name, cookie, csrf2 or csrf))
            else:
                logger.warning('  %s: 403→retry не дал JSON', name)
        elif resp.status_code == 200:
            ok, _ = _is_valid_json_response(resp)
            if ok:
                logger.info('  %s: OK', name)
                sessions.append((name, cookie, ''))
            else:
                logger.warning('  %s: 200 но не JSON (кука протухла?)', name)
        else:
            logger.warning('  %s: статус %d', name, resp.status_code)
    return sessions


def _fetch_grid_page(login: str, cookie: str, csrf: str, offset: int):
    payload = {
        'operationName': 'GridCampaigns',
        'query':         _GRID_QUERY,
        'variables': {
            'login':         login,
            'campaignInput': {
                'filter':           {'filterStatusIn': _STATUS_FILTER},
                'orderBy':          [{'order': 'ASC', 'field': 'STATUS'}],
                'statRequirements': {'preset': 'CURRENT_WEEK', 'goalIds': [], 'useCampaignGoalIds': False},
                'limitOffset':      {'limit': PAGE_LIMIT, 'offset': offset},
            },
        },
    }
    resp = _post_grid(login, cookie, csrf or None, payload)
    if resp is None:
        return None
    try:
        data = resp.json()
    except Exception:
        logger.warning('Grid: не JSON, status=%d', resp.status_code)
        return None
    if data.get('errors'):
        errs = '; '.join(e.get('message', '') for e in data['errors'])
        logger.error('Grid API: %s', errs)
        return None
    return (data.get('data') or {}).get('client', {}).get('campaigns')


def _fetch_states_for_login(login: str, sessions: list) -> tuple[dict, bool, str | None]:
    """Получить статусы всех кампаний аккаунта через первый рабочий куки.

    Возвращает (states, ok, session_name_used).
    session_name_used=None если все сессии упали (ok=False).
    Используется TP_SESSION_CACHE_2026-07-04 для обновления кеша.
    """
    for name, cookie, csrf in sessions:
        result = {}
        offset = 0
        ok = True
        while True:
            page = _fetch_grid_page(login, cookie, csrf, offset)
            if page is None:
                ok = False
                break
            rows = page.get('rowset') or []
            for row in rows:
                cid     = int(row['id'])
                primary = (row.get('status') or {}).get('primaryStatus')
                status  = PRIMARY_STATUS_MAP.get(primary, 'Остановлена') if primary else 'Остановлена'
                strategy      = row.get('strategy') or {}
                pay_for_conv  = strategy.get('payForConversion')
                payment_model = 'за конверсии' if pay_for_conv is True else 'за клики'
                result[cid] = (status, payment_model)
            total   = page.get('totalCount', 0)
            offset += len(rows)
            if offset >= total or not rows:
                break
            time.sleep(5)
        if ok:
            return result, True, name  # TP_SESSION_CACHE_2026-07-04: возвращаем имя рабочей сессии
    return {}, False, None


# ── ФАЗА A: prefetch (запускается из pipeline.py после step0) ─────────────────

def _prefetch_worker(
    manager_login: str,
    by_login: dict,
    session: tuple,
    failed_out: list,
) -> None:
    """Поток: обрабатывает свою группу аккаунтов одной кукой. Провалившиеся → failed_out."""
    conn = get_conn()
    try:
        total = len(by_login)
        batch: dict[int, tuple[str, str]] = {}  # cid → (campaign_status, payment_model)
        FLUSH_EVERY = 10

        def _flush(b: dict) -> None:
            if not b:
                return
            rows = [(cid, status, pm) for cid, (status, pm) in b.items()]
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(
                    cur,
                    f'INSERT INTO {T_PREFETCH} (campaign_id, campaign_status, payment_model) VALUES %s '
                    f'ON CONFLICT (campaign_id) DO UPDATE SET '
                    f'campaign_status = EXCLUDED.campaign_status, '
                    f'payment_model   = EXCLUDED.payment_model',
                    rows,
                )
            conn.commit()

        sessions = [session]
        for i, (login, ids) in enumerate(by_login.items(), 1):
            logger.info('[prefetch:%s] [%d/%d] %s (%d кампаний)',
                        manager_login, i, total, login, len(ids))
            states, ok, _ = _fetch_states_for_login(login, sessions)
            if ok:
                found = states
                batch.update(found)
            else:
                logger.warning('[prefetch:%s] %s: ошибка API → retry', manager_login, login)
                failed_out.append((login, ids))

            if i % FLUSH_EVERY == 0 and batch:
                _flush(batch)
                batch = {}
            if i < total:
                time.sleep(5)

        _flush(batch)
    except Exception as e:
        logger.error('[prefetch:%s] Ошибка: %s', manager_login, e, exc_info=True)
    finally:
        put_conn(conn)


def prefetch_statuses() -> None:
    """
    Фоновый поток: читает вселенную кампаний из FDW yandex_direct_manager_reports,
    тянет статусы через Grid API, пишет в _campaign_statuses_prefetch.
    Использует собственное соединение из пула.

    CAMPSTATUS_SRC_DIRECT_2026-06-23 (v2): источник вселенной переключён с
    big_analytics_direct (UNLOGGED, строится в step3 через ~20 мин) на FDW
    yandex_direct_manager_reports (SRC_YANDEX) — первоисточник, доступен немедленно
    в момент старта prefetch (после step0). Poll-цикл убран — FDW не нужен poll.
    Колонки "CampaignId"/account_login/manager_login/"Date" — те же.
    ВАЖНО: "Date" в FDW имеет тип TEXT → каст "Date"::date обязателен везде,
    где сравниваем с датой (иначе UndefinedFunction text >= timestamp).
    Grid API возвращает ТЕКУЩИЙ статус: кампания тратила 60 дн. назад, сейчас
    остановлена → API: STOPPED → PRIMARY_STATUS_MAP → 'Остановлена'/'Архив'.
    Вселенная за 60 дней ≈ 10k кампаний / 500 агентских аккаунтов — Grid API выдержит.
    Если FDW пуст/недоступен → 0 строк → WARNING + return (не падаем молча).
    """
    logger.info('[prefetch] Старт фонового получения статусов кампаний')
    conn = get_conn()
    try:
        # 1. Получить CampaignId + account_login + manager_login из FDW за 60 дней.
        #    SRC_YANDEX = yandex_direct_manager_reports (FDW, доступен сразу).
        #    ROW_NUMBER: актуальный manager_login по MAX(Date), чтобы кампания
        #    не попала в несколько потоков при смене менеджера.
        #    "Date"::date — обязателен: тип TEXT в FDW, без каста падает UndefinedFunction.
        rows = []
        with conn.cursor() as cur:
            cur.execute(f"""
                WITH agg AS (
                    SELECT
                        d."CampaignId",
                        d.account_login,
                        d.manager_login,
                        MAX(d."Date"::date) AS max_date
                    FROM {SRC_YANDEX} d
                    WHERE d."Date"::date >= CURRENT_DATE - INTERVAL '{ACTIVE_DAYS} days'
                      AND d."CampaignId" IS NOT NULL
                      AND d."CampaignId" != 0
                      AND d.account_login IS NOT NULL
                    GROUP BY d."CampaignId", d.account_login, d.manager_login
                ),
                ranked AS (
                    SELECT
                        "CampaignId",
                        account_login,
                        manager_login,
                        ROW_NUMBER() OVER (
                            PARTITION BY "CampaignId"
                            ORDER BY max_date DESC
                        ) AS rn
                    FROM agg
                )
                SELECT "CampaignId", account_login, manager_login
                FROM ranked
                WHERE rn = 1
            """)
            rows = cur.fetchall()

        if not rows:
            logger.warning(
                '[prefetch] FDW %s вернул 0 строк за последние %d дней — '
                'FDW недоступен или реально пуст. Статусы кампаний не будут получены.',
                SRC_YANDEX, ACTIVE_DAYS,
            )
            return

        logger.info('[prefetch] FDW %s: %d уникальных кампаний за %d дней',
                    SRC_YANDEX, len(rows), ACTIVE_DAYS)

        # 2. Группируем: by_manager[mgr][account_login] = {campaign_ids}
        by_manager: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
        for cid, account_login, manager_login in rows:
            mgr = (manager_login or 'unknown').split('@')[0]
            by_manager[mgr][account_login].add(cid)

        total_accounts = sum(len(a) for a in by_manager.values())
        logger.info('[prefetch] %d кампаний, %d аккаунтов, %d manager_login',
                    len(rows), total_accounts, len(by_manager))

        # 3. Создать/пересоздать prefetch-таблицу
        with conn.cursor() as cur:
            cur.execute(f'DROP TABLE IF EXISTS {T_PREFETCH}')
            cur.execute(f"""
                CREATE TABLE {T_PREFETCH} (
                    campaign_id     BIGINT PRIMARY KEY,
                    campaign_status TEXT,
                    payment_model   TEXT
                )
            """)
        conn.commit()

    except Exception as e:
        logger.error('[prefetch] Ошибка на старте: %s', e, exc_info=True)
        return
    finally:
        put_conn(conn)

    # 4. Загрузить куки, валидировать каждую отдельно
    raw_cookies = _fetch_cookies()
    if not raw_cookies:
        logger.warning('[prefetch] Нет куков — домашний сервер недоступен')
        return

    sessions_by_name: dict[str, tuple] = {}
    for name, cookie in raw_cookies.items():
        sl = _build_sessions({name: cookie})
        if sl:
            sessions_by_name[name] = sl[0]
            logger.info('[prefetch] Куки OK: %s', name)
        else:
            logger.warning('[prefetch] Куки не прошли валидацию: %s', name)

    if not sessions_by_name:
        logger.warning('[prefetch] Ни одна куки не работает — шаг пропущен')
        send_tg_cookies_dead('step7 (статусы кампаний Grid API)')
        return

    # 5. Запустить потоки — по одному на manager_login
    failed_accounts: list = []  # [(account_login, ids), ...]  — list.append GIL-safe
    threads = []

    for mgr, by_login in by_manager.items():
        if mgr not in sessions_by_name:
            logger.warning('[prefetch] Нет куки для manager_login=%s (%d акк.) → retry', mgr, len(by_login))
            for account_login, ids in by_login.items():
                failed_accounts.append((account_login, ids))
            continue

        t = threading.Thread(
            target=_prefetch_worker,
            args=(mgr, by_login, sessions_by_name[mgr], failed_accounts),
            daemon=True,
            name=f'prefetch_{mgr}',
        )
        t.start()
        threads.append(t)
        logger.info('[prefetch] Поток запущен: %s (%d аккаунтов)', mgr, len(by_login))

    for t in threads:
        t.join()

    # 6. Retry: провалившиеся аккаунты перебором всех рабочих сессий
    if failed_accounts:
        logger.info('[prefetch] Retry: %d аккаунтов перебором куки', len(failed_accounts))
        all_sessions = list(sessions_by_name.values())
        retry_conn = get_conn()
        try:
            for account_login, ids in failed_accounts:
                logger.info('[prefetch:retry] %s (%d кампаний)', account_login, len(ids))
                states, ok, _ = _fetch_states_for_login(account_login, all_sessions)
                if ok:
                    found = states
                    if found:
                        rows = [(cid, status, pm) for cid, (status, pm) in found.items()]
                        with retry_conn.cursor() as cur:
                            psycopg2.extras.execute_values(
                                cur,
                                f'INSERT INTO {T_PREFETCH} (campaign_id, campaign_status, payment_model) VALUES %s '
                                f'ON CONFLICT (campaign_id) DO UPDATE SET '
                                f'campaign_status = EXCLUDED.campaign_status, '
                                f'payment_model   = EXCLUDED.payment_model',
                                rows,
                            )
                        retry_conn.commit()
                else:
                    logger.warning('[prefetch:retry] %s: ошибка даже перебором', account_login)
                time.sleep(5)
        except Exception as e:
            logger.error('[prefetch:retry] Ошибка: %s', e, exc_info=True)
        finally:
            put_conn(retry_conn)

    # 7. Итог
    try:
        stat_conn = get_conn()
        try:
            with stat_conn.cursor() as cur:
                cur.execute(f'SELECT campaign_status, COUNT(*) FROM {T_PREFETCH} GROUP BY campaign_status')
                counts = Counter(dict(cur.fetchall()))
            logger.info('[prefetch] Готово: %d статусов. %s', sum(counts.values()), dict(counts))
        finally:
            put_conn(stat_conn)
    except Exception as e:
        logger.warning('[prefetch] Не удалось получить итог: %s', e)


# ── Кеш «login → агентская сессия» (TP_SESSION_CACHE_2026-07-04) ─────────────

_CACHE_TABLE        = '_tp_login_session_cache'
_CACHE_TTL_INTERVAL = '7 days'


def _ensure_cache_table(conn) -> bool:
    """CREATE TABLE IF NOT EXISTS кеш «login → session_key».

    DDL (TP_SESSION_CACHE_2026-07-04):
        account_login  TEXT PRIMARY KEY
        session_key    TEXT          -- NULL = ни одна сессия не подходит (skip)
        last_updated   TIMESTAMPTZ DEFAULT NOW()

    Возвращает True при успехе; False при любой ошибке — вызывающий код
    переходит на fallback (перебор всех трёх сессий), пайплайн не падает.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {_CACHE_TABLE} (
                    account_login  TEXT PRIMARY KEY,
                    session_key    TEXT,
                    last_updated   TIMESTAMPTZ DEFAULT NOW()
                )
            """)
        conn.commit()
        return True
    except Exception as e:
        logger.warning('[tp-cache] Не удалось создать кеш-таблицу: %s — fallback на перебор', e)
        try:
            conn.rollback()
        except Exception:
            pass
        return False


def _load_session_cache(conn) -> dict:
    """Загрузить актуальные записи кеша (TTL 7 дней).

    Возвращает {account_login: session_key | None}.
    session_key=None означает «ни одна сессия не работает» (skip-запись).
    При ошибке возвращает пустой dict — вызывающий код перебирает все сессии.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT account_login, session_key
                FROM {_CACHE_TABLE}
                WHERE last_updated > NOW() - INTERVAL '{_CACHE_TTL_INTERVAL}'
            """)
            return {row[0]: row[1] for row in cur.fetchall()}
    except Exception as e:
        logger.warning('[tp-cache] Не удалось загрузить кеш: %s — fallback на перебор', e)
        return {}


def _flush_session_cache(conn, updates: dict) -> None:
    """Batch UPSERT изменений кеша в конце фазы B.

    updates: {account_login: session_key | None}
    Один запрос на все изменения, не per-request.
    При ошибке — предупреждение в лог, пайплайн не падает.
    """
    if not updates:
        return
    try:
        rows = list(updates.items())
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                f"""
                INSERT INTO {_CACHE_TABLE} (account_login, session_key)
                VALUES %s
                ON CONFLICT (account_login) DO UPDATE SET
                    session_key  = EXCLUDED.session_key,
                    last_updated = NOW()
                """,
                rows,
            )
        conn.commit()
        logger.info('[tp-cache] Кеш обновлён: %d записей', len(rows))
    except Exception as e:
        logger.warning('[tp-cache] Не удалось сохранить кеш: %s', e)
        try:
            conn.rollback()
        except Exception:
            pass


# ── ТК/МК статусы через Grid API (TKMK_REALSTATUS_2026-06-24) ───────────────

def _fetch_tp_statuses_sync(conn, sessions: list) -> dict:
    """
    TKMK_REALSTATUS_2026-06-24: Синхронно получить реальные статусы ТК/МК (tp8/tp9/tp10)
    через Grid API прямо в фазе B (когда big_analytics_crop_targeting уже готова).

    TP_SESSION_CACHE_2026-07-04: кеш «login → агентская сессия» (_tp_login_session_cache).
    Алгоритм на каждый аккаунт (TTL кеша = 7 дней):
    - NULL в кеше  → пропускаем аккаунт (DEBUG-лог, статус будет NULL, sleep пропущен)
    - session_key  → эта сессия идёт первой в _fetch_states_for_login;
                     если она вернула page=None (401/протухла) — функция сама
                     перебирает остальные две → used_key обновляется в кеше
    - нет записи   → перебираем все три, UPSERT результат (key или NULL) в кеш
    Batch UPSERT всех изменений кеша — одним запросом в конце фазы B.
    Fallback: если кеш-таблица недоступна → прежнее поведение (перебор всех трёх),
    пайплайн не падает.

    Variant A (TP_SESSION_CACHE_2026-07-04): time.sleep(3 if ok else 0.5) вместо
    time.sleep(3) — убирает 3-секундную задержку для 401-аккаунтов ещё на первом
    прогоне (до прогрева кеша).

    Почему НЕ в prefetch_statuses (фаза A):
    - big_analytics_crop_targeting строится в step3 (~20 мин)
    - prefetch_statuses стартует после step0 — T_CROP ещё пустая в тот момент
    - Фаза B вызывается ПОСЛЕ join() и ПОСЛЕ step3 → T_CROP гарантированно готова

    Почему prefetch FDW (фаза A) не покрывает ТК/МК:
    - FDW yandex_direct_manager_reports содержит только обычные Директ-расходы
    - 103 из 188 ТК/МК аккаунтов не имеют расходов в FDW за 60 дней
      (они ведут только МК/ТК, без обычных кампаний), поэтому не попадают
      в вселенную prefetch_statuses — и не получают статус из Grid API

    Returns:
        dict {campaign_id_int: status_str}  — только для кампаний с найденным статусом
    """
    from config.settings import T_CROP

    if not sessions:
        logger.warning('[tp-statuses] Нет рабочих сессий — статусы ТК/МК будут NULL')
        return {}

    # 1. Читаем вселенную ТК/МК из T_CROP
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT DISTINCT ON (c."CampaignId")
                c."CampaignId",
                c.account_login,
                c.manager_login
            FROM {T_CROP} c
            WHERE c._source_table IN ('tp8', 'tp9', 'tp10')
              AND c."CampaignId" IS NOT NULL
              AND c.account_login IS NOT NULL
            ORDER BY c."CampaignId", c."Date" DESC NULLS LAST
        """)
        rows = cur.fetchall()

    if not rows:
        logger.warning('[tp-statuses] T_CROP вернул 0 строк tp8/tp9/tp10 — Grid API не вызывается')
        return {}

    logger.info('[tp-statuses] ТК/МК вселенная: %d кампаний из T_CROP', len(rows))

    # 2. Группируем по account_login (не по manager_login — Grid API работает по client login)
    #    Один account_login может иметь несколько ТК/МК кампаний → один HTTP-запрос на аккаунт
    by_account: dict[str, set] = defaultdict(set)
    for cid, acc_login, _mgr in rows:
        by_account[acc_login].add(int(cid))

    logger.info('[tp-statuses] %d уникальных аккаунтов ТК/МК → запрашиваем Grid API', len(by_account))

    # TP_SESSION_CACHE_2026-07-04: загружаем кеш «login → агентская сессия»
    cache_available = _ensure_cache_table(conn)
    session_cache: dict = {}
    if cache_available:
        session_cache = _load_session_cache(conn)
        logger.info('[tp-cache] Загружено %d актуальных записей кеша (TTL 7 дней)', len(session_cache))
    cache_updates: dict = {}  # {account_login: session_key | None} — собираем, пишем batch в конце

    # 3. Для каждого аккаунта запрашиваем ВСЕ кампании через Grid API
    #    (не фильтруем по ID — Grid API не поддерживает filterCampaignIdsIn в этом endpoint)
    result: dict[int, str] = {}
    total_accts  = len(by_account)
    skipped_count = 0
    ok = False  # инициализация для sleep-логики при первой итерации

    for i, (acc_login, target_cids) in enumerate(by_account.items(), 1):
        if i % 20 == 0 or i == 1:
            logger.info('[tp-statuses] [%d/%d] Аккаунт %s (%d кампаний)',
                        i, total_accts, acc_login, len(target_cids))

        # TP_SESSION_CACHE_2026-07-04: определяем порядок сессий для этого аккаунта
        if cache_available and acc_login in session_cache:
            cached_key = session_cache[acc_login]
            if cached_key is None:
                # Кеш говорит: ни одна из 3 сессий не работает — пропускаем без HTTP-вызова
                logger.debug('[tp-cache] %s: кеш=NULL — пропускаем (статус будет NULL)', acc_login)
                skipped_count += 1
                continue  # sleep пропускаем: HTTP-запроса не было

            # Закешированная сессия идёт первой; остальные две — fallback при 401/протухании
            # Если cached_key протух → _fetch_states_for_login пробует следующие сессии
            # → ok=True с другим used_key → cache_updates обновит запись
            ordered_sessions = (
                [s for s in sessions if s[0] == cached_key] +
                [s for s in sessions if s[0] != cached_key]
            )
        else:
            # Нет записи в кеше или TTL истёк → перебираем все три
            ordered_sessions = sessions

        states, ok, used_key = _fetch_states_for_login(acc_login, ordered_sessions)
        if ok:
            # states = {cid: (status_str, payment_model_str)}
            for cid in target_cids:
                if cid in states:
                    result[cid] = states[cid][0]  # только campaign_status, pm берём из cpc_cpa
                # если cid не в states → Grid API вернул ответ, но эта кампания не найдена
                # (возможно другой тип/аккаунт) → оставляем без статуса → INSERT запишет NULL

            # TP_SESSION_CACHE_2026-07-04: обновляем кеш если сессия изменилась или запись новая
            if cache_available and session_cache.get(acc_login) != used_key:
                cache_updates[acc_login] = used_key
        else:
            logger.warning('[tp-statuses] %s: Grid API не отвечает', acc_login)
            # TP_SESSION_CACHE_2026-07-04: помечаем как недоступный (NULL)
            # Пишем только если запись отсутствует или была непустой (избегаем лишних UPSERT)
            if cache_available:
                existing = session_cache.get(acc_login, 'ABSENT')
                if existing is not None:  # 'ABSENT' или непустой ключ → нужно записать NULL
                    cache_updates[acc_login] = None

        # TP_SESSION_CACHE_2026-07-04 Variant A: убираем 3-секундный sleep для 401-аккаунтов
        if i < total_accts:
            time.sleep(3 if ok else 0.5)

    # TP_SESSION_CACHE_2026-07-04: batch UPSERT всех изменений кеша — один запрос в конце
    if cache_available and cache_updates:
        _flush_session_cache(conn, cache_updates)

    found_count   = len(result)
    missing_count = len(rows) - found_count
    logger.info(
        '[tp-statuses] Готово: %d статусов получено, %d кампаний без статуса (→ NULL), '
        '%d аккаунтов пропущено по кешу',
        found_count, missing_count, skipped_count,
    )
    return result


# ── ФАЗА B: run() — вызывается как шаг пайплайна ─────────────────────────────

def _build_campaign_status(conn) -> int:
    """Создать campaign_status из big_analytics_direct + tp8/tp9/tp10-кампании + статусы из prefetch.

    Источники:
    - big_analytics_direct  — Директ-кампании (Grid API знает их статус/payment_model)
    - big_analytics_crop_targeting WHERE _source_table IN ('tp8','tp9','tp10') — ТК/МК
      TKMK_REALSTATUS_2026-06-24: реальный статус через _fetch_tp_statuses_sync()
      (Grid API в фазе B, когда T_CROP уже готова). Хардкод 'Активна' убран.
      Если Grid API не дал статус → NULL. payment_model из cpc_cpa (без изменений).
      tp8=Telegram, tp9=Max, tp10=Telegram+Max.
    """
    from config.settings import T_CROP

    with conn.cursor() as cur:
        # Проверяем наличие prefetch-таблицы
        cur.execute(f"""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_name = '{T_PREFETCH}'
            )
        """)
        has_prefetch = cur.fetchone()[0]

        cur.execute(f'DROP TABLE IF EXISTS {T_CAMPAIGN_STATUS}')
        cur.execute(f"""
            CREATE TABLE {T_CAMPAIGN_STATUS} (
                "CampaignId"     BIGINT PRIMARY KEY,
                account_login    TEXT,
                "статус"         TEXT,
                "специалист"     TEXT,
                "CampaignName"   TEXT,
                manager_login    TEXT,
                campaign_status  TEXT,
                payment_model    TEXT
            )
        """)

        if has_prefetch:
            # Берём статус и payment_model из prefetch если есть
            cur.execute(f"""
                INSERT INTO {T_CAMPAIGN_STATUS}
                SELECT DISTINCT ON (f."CampaignId")
                    f."CampaignId",
                    f.account_login,
                    f."статус",
                    f."специалист",
                    f."CampaignName",
                    f.manager_login,
                    p.campaign_status,
                    p.payment_model
                FROM {T_DIRECT} f
                LEFT JOIN {T_PREFETCH} p ON p.campaign_id = f."CampaignId"
                WHERE f."CampaignId" IS NOT NULL
                ORDER BY f."CampaignId", f."Date" DESC NULLS LAST
            """)
        else:
            logger.warning('prefetch-таблица не найдена — campaign_status и payment_model будут NULL')
            cur.execute(f"""
                INSERT INTO {T_CAMPAIGN_STATUS}
                SELECT DISTINCT ON ("CampaignId")
                    "CampaignId", account_login, "статус", "специалист",
                    "CampaignName", manager_login, NULL::TEXT, NULL::TEXT
                FROM {T_DIRECT}
                WHERE "CampaignId" IS NOT NULL
                ORDER BY "CampaignId", "Date" DESC NULLS LAST
            """)

        direct_count = cur.rowcount

    conn.commit()

    # TKMK_REALSTATUS_2026-06-24: получаем реальные статусы ТК/МК через Grid API
    # Сессии строим из тех же кук — T_CROP уже готова (step3 завершён до run())
    raw_cookies = _fetch_cookies()
    tp_sessions: list = []
    if raw_cookies:
        for name, cookie in raw_cookies.items():
            sl = _build_sessions({name: cookie})
            if sl:
                tp_sessions.extend(sl)
        if not tp_sessions:
            logger.warning('[tp-statuses] Ни одна куки не прошла валидацию — статусы ТК/МК будут NULL')
    else:
        logger.warning('[tp-statuses] cookies.json недоступен — статусы ТК/МК будут NULL')

    tp_statuses = _fetch_tp_statuses_sync(conn, tp_sessions)

    # INSERT ТК/МК с реальным статусом из Grid API (или NULL если не получен)
    # payment_model берём из cpc_cpa (не из Grid API — МК/ТК не в prefetch)
    # ON CONFLICT DO NOTHING: если CampaignId уже был из T_DIRECT — не перетираем
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT DISTINCT ON (c."CampaignId")
                c."CampaignId",
                c.account_login,
                c."статус",
                c."специалист",
                c."CampaignName",
                c.manager_login,
                CASE WHEN c.cpc_cpa = 'cpa' THEN 'за конверсии' ELSE 'за клики' END AS payment_model
            FROM {T_CROP} c
            WHERE c._source_table IN ('tp8', 'tp9', 'tp10')
              AND c."CampaignId" IS NOT NULL
            ORDER BY c."CampaignId", c."Date" DESC NULLS LAST
        """)
        tp_rows = cur.fetchall()

        if tp_rows:
            insert_rows = []
            for (cid, acc, stat, spec, name, mgr, pm) in tp_rows:
                campaign_status = tp_statuses.get(int(cid))  # None если Grid API не дал статус
                insert_rows.append((cid, acc, stat, spec, name, mgr, campaign_status, pm))

            psycopg2.extras.execute_values(
                cur,
                f"""
                INSERT INTO {T_CAMPAIGN_STATUS}
                    ("CampaignId", account_login, "статус", "специалист",
                     "CampaignName", manager_login, campaign_status, payment_model)
                VALUES %s
                ON CONFLICT ("CampaignId") DO NOTHING
                """,
                insert_rows,
            )
        tp8_count = cur.rowcount

    conn.commit()

    count = direct_count + tp8_count
    tp_found  = len(tp_statuses)
    tp_null   = tp8_count - tp_found if tp8_count > tp_found else 0
    logger.info(
        'campaign_status: %d кампаний всего (direct=%d, tp8/tp9/tp10=%d, '
        'tp_статус_из_grid=%d, tp_статус_null=%d, prefetch=%s)',
        count, direct_count, tp8_count, tp_found, tp_null, has_prefetch,
    )
    return count


def _patch_direct_table(conn) -> int:
    """Добавить/обновить campaign_status и payment_model в big_analytics_direct."""
    with conn.cursor() as cur:
        cur.execute(f'ALTER TABLE {T_DIRECT} ADD COLUMN IF NOT EXISTS campaign_status TEXT')
        cur.execute(f'ALTER TABLE {T_DIRECT} ADD COLUMN IF NOT EXISTS payment_model TEXT')
        cur.execute(f"""
            UPDATE {T_DIRECT} f
            SET campaign_status = cs.campaign_status,
                payment_model   = cs.payment_model
            FROM {T_CAMPAIGN_STATUS} cs
            WHERE f."CampaignId" = cs."CampaignId"
        """)
        count = cur.rowcount
        # Удаляем временную prefetch-таблицу
        cur.execute(f'DROP TABLE IF EXISTS {T_PREFETCH}')
    conn.commit()
    logger.info('big_analytics_direct: campaign_status/payment_model обновлены для %d строк', count)
    return count


def _patch_other_analytics_tables(conn) -> None:
    """Добавить campaign_status и payment_model в остальные big_analytics_* таблицы.

    T_CROP/T_REVIEWS имеют CampaignId → патчим значениями из campaign_status.
    tp8/tp9/tp10-строки теперь тоже есть в campaign_status (добавлены в _build_campaign_status),
    поэтому JOIN находит их и выставляет реальный campaign_status из Grid API + payment_model из cpc_cpa.
    TKMK_REALSTATUS_2026-06-24: campaign_status для ТК/МК = реальный (STOPPED→'Остановлена' и т.д.)
    или NULL если Grid API не дал статус. Хардкод 'Активна' убран.
    T_SEO/T_PIXEL не имеют CampaignId → только добавляем колонку (остаётся NULL).
    """
    from config.settings import T_CROP, T_REVIEWS, T_SEO, T_PIXEL

    for table in (T_CROP, T_REVIEWS):
        with conn.cursor() as cur:
            cur.execute(f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS campaign_status TEXT')
            cur.execute(f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS payment_model TEXT')
            # Патч через JOIN с campaign_status (только кампании из Grid API)
            cur.execute(f"""
                UPDATE {table} f
                SET campaign_status = cs.campaign_status,
                    payment_model   = cs.payment_model
                FROM {T_CAMPAIGN_STATUS} cs
                WHERE f."CampaignId" = cs."CampaignId"
                  AND f."CampaignId" IS NOT NULL
            """)
            n = cur.rowcount
        conn.commit()
        logger.info('%s: campaign_status/payment_model обновлены для %d строк (через campaign_status)', table, n)

    for table in (T_SEO, T_PIXEL):
        with conn.cursor() as cur:
            cur.execute(f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS campaign_status TEXT')
            cur.execute(f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS payment_model TEXT')
        conn.commit()


# ── ФАЗА B: run() ─────────────────────────────────────────────────────────────

def run_from_cache(conn, run_id: str = '') -> dict:
    """
    Кэш-режим для fast_pipeline: берёт campaign_status из существующей таблицы.
    Без Grid API. Требует предварительного запуска pipeline.py.
    """
    logger.info('Шаг 4 (кэш): campaign_status из предыдущего запуска pipeline.py')
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = '{T_CAMPAIGN_STATUS}'
            )
        """)
        exists = cur.fetchone()[0]
    if not exists:
        logger.warning(
            'campaign_status не найдена — campaign_status/payment_model будут NULL. '
            'Запусти полный pipeline.py сначала.'
        )
    n_patched = _patch_direct_table(conn)
    _patch_other_analytics_tables(conn)
    return {'rows': n_patched, 'details': f'cache_mode, direct_patched={n_patched}'}


def run(conn, run_id: str, prefetch_thread=None) -> dict:
    """
    conn            — DST соединение (ad_analytics_bi)
    prefetch_thread — threading.Thread из pipeline.py, ждём завершения
    """
    logger.info('Шаг 4: статусы кампаний')

    # Ждём завершения фонового prefetch
    if prefetch_thread is not None and prefetch_thread.is_alive():
        logger.info('Ожидаем завершения фонового получения статусов...')
        prefetch_thread.join()
        logger.info('Фоновый поток завершён')

    # Строим campaign_status и патчим все big_analytics_* таблицы
    n_campaigns = _build_campaign_status(conn)
    n_patched   = _patch_direct_table(conn)
    _patch_other_analytics_tables(conn)

    return {
        'rows': n_campaigns,
        'details': f'campaigns={n_campaigns}, direct_patched={n_patched}',
    }
