"""
step7_campaign_status_check_utm/check_utm/utm_direct_audit.py
===============================================================
Проверяет TrackingParams групп Яндекс.Директ.

Источник кампаний:  public.big_analytics_direct (account_login + CampaignId)
counter_id поиск:   1) public.metrika_yandex (предзаписана metrika_yandex.py)
                    2) CounterIds из кампаний Direct API
                    3) Метрика API fallback
Результат:          public.check_utm (полная перезапись)
БД:                 ad_analytics_bi

Запускается как шаг 5 пайплайна после step4_campaign_status
(step4 использует неофициальный Grid API с куками,
check_utm использует официальный OAuth API — разные эндпоинты, нет конфликта).

Запуск:
    python3 -m step7_campaign_status_check_utm.check_utm.utm_direct_audit
    # или через pipeline:
    python3 pipeline.py --only-step=5
"""

import re
import threading
import time
from collections import Counter, defaultdict
from datetime import date, timedelta

import psycopg2
import requests
from psycopg2.extras import execute_values

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.cookies import send_tg as _send_tg
from config.settings import DB_DST, T_GSHEET_SITES
from config.tokens import OAUTH_TOKEN_1, OAUTH_TOKEN_2, OAUTH_TOKEN_3, OAUTH_TOKEN_4, OAUTH_TOKEN_5, METRIKA_TOKEN, METRIKA_TOKEN_AUDIT, METRIKA_TOKEN_3

# ── Токены ────────────────────────────────────────────────────────────────────

TOKENS        = [OAUTH_TOKEN_1, OAUTH_TOKEN_2, OAUTH_TOKEN_3, OAUTH_TOKEN_4, OAUTH_TOKEN_5]
DIRECT_BASE   = 'https://api.direct.yandex.com/json/v5'
METRIKA_BASE  = 'https://api-metrika.yandex.net'

# Коды ошибок Директа — нет доступа к аккаунту ни по одному токену
_API_AUTH_ERRORS  = {52, 53, 58}
_api_auth_failed: set = set()

# ── Таблицы ───────────────────────────────────────────────────────────────────

SOURCE_TABLE       = 'public.big_analytics_direct'   # откуда берём account_login + CampaignId
TARGET_TABLE       = 'public.check_utm'
METRIKA_TABLE      = 'public.metrika_yandex'          # предзаписана metrika_yandex.py
FUCK_DIRECT_TABLE  = 'public.check_utm_fuck_direct'   # история групп с неверными UTM

# ── UTM-шаблон ────────────────────────────────────────────────────────────────

EXPECTED_PARTS = [
    'utm_source=s:{source}',
    'utm_campaign={campaign_id}|{campaign_name}',
    'utm_content=g:{gbid}',
    'geoname:{region_name}',
    'geoid:{region_id}',
    'dev:{device_type}',
    'r:{retargeting_id}',
    'cor:{coef_goal_context_id}',
]


# ── Direct API ────────────────────────────────────────────────────────────────

def _direct_call(service, params, token, client_login, attempt=0):
    headers = {
        'Authorization':   f'Bearer {token}',
        'Content-Type':    'application/json; charset=utf-8',
        'Accept-Language': 'ru',
    }
    if client_login:
        headers['Client-Login'] = client_login
    try:
        r = requests.post(
            f'{DIRECT_BASE}/{service}',
            json={'method': 'get', 'params': params},
            headers=headers,
            timeout=30,
        )
        d = r.json()
        if d.get('error', {}).get('error_code') == 152 or r.status_code == 429:
            wait = int(r.headers.get('Retry-After', 10))
            print(f'    [429] жду {wait}s...')
            time.sleep(min(wait, 60))
            if attempt < 3:
                return _direct_call(service, params, token, client_login, attempt + 1)
        return d
    except Exception as e:
        if attempt < 2:
            time.sleep(3)
            return _direct_call(service, params, token, client_login, attempt + 1)
        return {'error': {'error_code': -1, 'error_detail': str(e)}}


def api_get(service, params, client_login=None):
    """Перебирает токены. Возвращает первый успешный ответ."""
    global _api_auth_failed
    last = {}
    for i, token in enumerate(TOKENS):
        if i > 0:
            time.sleep(2)
        resp = _direct_call(service, params, token, client_login)
        if 'error' not in resp:
            return resp
        last = resp
    if client_login:
        error_code = (last.get('error') or {}).get('error_code', 0)
        if error_code in _API_AUTH_ERRORS:
            _api_auth_failed.add(client_login)
    return last


# ── Metrika API ───────────────────────────────────────────────────────────────

# Три-токенная схема с round-robin: 3 × 5000 = 15 000 req/сутки суммарно.
# При 429 на текущем токене — переключаемся на следующий (по кольцу).
# Если все три исчерпаны — пропускаем Метрику до конца запуска.
_METRIKA_TOKENS = [t for t in [
    ('audit',  METRIKA_TOKEN_AUDIT),   # victorylotsofads04
    ('main',   METRIKA_TOKEN),         # victorylotsofads1
    ('extra',  METRIKA_TOKEN_3),       # skuderko1
] if t[1]]                             # пропускаем токены с пустым значением
_metrika_token_idx       = 0          # текущий индекс в _METRIKA_TOKENS
_metrika_quota_exhausted = False      # True когда ВСЕ токены исчерпаны


def _switch_token() -> bool:
    """Переключение на следующий токен. True — успешно, False — токены закончились."""
    global _metrika_token_idx, _metrika_quota_exhausted
    if _metrika_token_idx + 1 < len(_METRIKA_TOKENS):
        _metrika_token_idx += 1
        new_label = _METRIKA_TOKENS[_metrika_token_idx][0]
        print(f'    [Metrika] переключаюсь на токен: {new_label}', flush=True)
        return True
    _metrika_quota_exhausted = True
    print('    [Metrika 429] все токены исчерпаны, пропускаем Метрику до конца запуска', flush=True)
    return False


def metrika_get(path, params, attempt=0):
    global _metrika_quota_exhausted
    if _metrika_quota_exhausted:
        return {'_quota_exhausted': True}
    label, token = _METRIKA_TOKENS[_metrika_token_idx]
    headers = {'Authorization': f'OAuth {token}'}
    try:
        r = requests.get(f'{METRIKA_BASE}{path}', params=params,
                         headers=headers, timeout=30)
        if r.status_code == 429:
            wait = int(r.headers.get('Retry-After', 60))
            if attempt < 1:
                # Первый 429 на текущем токене: ждём Retry-After и одна повторная попытка
                print(f'    [Metrika 429] лимит токена {label}, жду {min(wait, 90)}s...', flush=True)
                time.sleep(min(wait, 90))
                return metrika_get(path, params, attempt + 1)
            # Второй 429 подряд на текущем токене → пробуем переключиться
            if _switch_token():
                return metrika_get(path, params, attempt=0)  # сброс attempt на новом токене
            return {'_quota_exhausted': True}
        r.encoding = 'utf-8'
        return r.json()
    except Exception as e:
        if attempt < 2:
            time.sleep(3)
            return metrika_get(path, params, attempt + 1)
        return {'errors': [str(e)]}


def load_metrika_counters():
    all_counters = []
    seen_ids = set()
    page = 1
    while True:
        resp = metrika_get('/management/v1/counters', {'per_page': 1000, 'page': page})
        counters = resp.get('counters', [])
        if not counters:
            break
        for c in counters:
            cid = c.get('id')
            if cid and cid not in seen_ids:
                seen_ids.add(cid)
                all_counters.append(c)
        rows = resp.get('rows', 0)
        if page * 1000 >= rows:
            break
        page += 1
        time.sleep(0.3)
    return all_counters


def _normalize_domain(raw):
    s = (raw or '').lower().strip()
    for prefix in ('https://', 'http://'):
        if s.startswith(prefix):
            s = s[len(prefix):]
    return s.replace('www.', '').strip('/')


def find_counter_for_domain(domain, counters):
    clean = _normalize_domain(domain)
    if not clean:
        return None
    for c in counters:
        site = _normalize_domain(c.get('site', ''))
        if site and (clean in site or site in clean):
            return c
    return None


def search_counter_by_domain(domain):
    resp = metrika_get('/management/v1/counters', {'q': domain, 'per_page': 50})
    time.sleep(0.3)
    return resp.get('counters', [])


def get_utm_via_metrika(counter_id, camp_id):
    resp = metrika_get('/stat/v1/data', {
        'id':         counter_id,
        'metrics':    'ym:s:visits',
        'dimensions': 'ym:s:date,ym:s:UTMSource,ym:s:UTMMedium,ym:s:UTMCampaign,ym:s:UTMContent,ym:s:UTMTerm',
        'filters':    f"ym:s:UTMCampaign=~'{camp_id}'",
        'sort':       '-ym:s:date',
        'date1':      (date.today() - timedelta(days=30)).isoformat(),
        'date2':      date.today().isoformat(),
        'limit':      50,
        'accuracy':   '1',
    })
    time.sleep(0.4)

    rows = resp.get('data', [])
    if not rows:
        return None, None

    def d(row, i):
        dims = row.get('dimensions', [])
        return (dims[i].get('name') or '') if len(dims) > i else ''

    def visits(row):
        m = row.get('metrics', [])
        return m[0] if m else 0

    # Берём последнюю дату с трафиком, фильтруем только её строки,
    # сортируем по визитам убыванием, берём топ-5 (меньше false positives)
    latest_date = d(rows[0], 0)
    same_day    = [r for r in rows if d(r, 0) == latest_date]
    top10       = sorted(same_day, key=visits, reverse=True)[:5]

    # Классифицируем каждую строку
    classes = []
    for row in top10:
        cls = classify_from_metrika(d(row, 1), d(row, 2), d(row, 3), d(row, 4), d(row, 5))
        classes.append(cls)

    # Агрегируем: OK > НЕТ_UTM > ДРУГОЙ_UTM
    # Если хоть одна строка из топ-5 = OK → считаем что метка настроена правильно
    # (одна плохая строка может быть случайным мусором: бот, прямой заход с rest-параметрами)
    if 'OK' in classes:
        agg_cls = 'OK'
        # tracking берём из первой OK-строки (а не из top10[0] с самым большим трафиком)
        ok_idx = classes.index('OK')
        r0 = top10[ok_idx]
    elif 'НЕТ_UTM' in classes:
        agg_cls = 'НЕТ_UTM'
        r0 = top10[0]
    else:
        agg_cls = 'ДРУГОЙ_UTM'
        r0 = top10[0]

    tracking = (
        f'utm_source={d(r0, 1)}'
        f'&utm_medium={d(r0, 2)}'
        f'&utm_campaign={d(r0, 3)}'
        f'&utm_term={d(r0, 5)}'
        f'&utm_content={d(r0, 4)}'
    )
    return tracking, agg_cls


def classify_from_metrika(utm_source, utm_medium, utm_campaign, utm_content, utm_term):
    if not utm_source:
        return 'НЕТ_UTM'
    source_ok   = utm_source.startswith('s:')
    medium_ok   = utm_medium in ('cpc', 'cpa')
    campaign_ok = bool(re.match(r'^\d+\|', utm_campaign or ''))
    content_ok  = bool(re.search(r'g:\d+\|geoname:.+\|geoid:\d+\|dev:.+\|r:\d+\|cor:', utm_content or ''))
    if source_ok and medium_ok and campaign_ok and content_ok:
        return 'OK'
    return 'ДРУГОЙ_UTM'


# ── Поиск counter_id ──────────────────────────────────────────────────────────

def get_counter_from_db(login, conn):
    """
    Быстрый поиск counter_id из metrika_yandex (предзаписана metrika_yandex.py).
    Возвращает (counter_id_str, domain) или (None, None).
    """
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT counter_id, domain
            FROM {METRIKA_TABLE}
            WHERE login_key = %s AND counter_id IS NOT NULL
            LIMIT 1
        """, (login,))
        row = cur.fetchone()
    if row:
        return str(row[0]), row[1]
    return None, None


def get_counter_for_login(login, camp_ids, all_counters, conn):
    """
    Возвращает (counter_id, domain).
    Стратегия:
      1. Из metrika_yandex таблицы (быстро, без API)
      2. CounterIds из кампаний Direct API
      3. Домен из big_analytics_direct → Metrika поиск
      4. Домен из объявлений (Href) → Metrika поиск
    """
    found_domain = [None]

    def _find_by_domain(domain):
        counter = find_counter_for_domain(domain, all_counters)
        if counter:
            found_domain[0] = _normalize_domain(domain)
            return str(counter['id'])
        candidates = search_counter_by_domain(domain)
        counter = find_counter_for_domain(domain, candidates)
        if counter:
            found_domain[0] = _normalize_domain(domain)
            return str(counter['id'])
        return None

    # 1. Из metrika_yandex таблицы
    cid, dom = get_counter_from_db(login, conn)
    if cid:
        return cid, dom

    # 2. CounterIds из кампаний
    for i in range(0, min(len(camp_ids), 20), 10):
        batch = camp_ids[i:i + 10]
        resp = api_get('campaigns', {
            'SelectionCriteria': {'Ids': batch},
            'FieldNames':        ['Id', 'Type'],
            'TextCampaignFieldNames': ['CounterIds'],
            'Page':              {'Limit': 10},
        }, client_login=login)
        for c in resp.get('result', {}).get('Campaigns', []):
            ids = (c.get('TextCampaign') or {}).get('CounterIds') or []
            if ids:
                try:
                    first = ids[0] if isinstance(ids, (list, tuple)) else next(iter(ids.values()), None)
                    if first:
                        return str(first), None
                except (KeyError, TypeError, StopIteration):
                    pass
        time.sleep(0.2)

    # 3. Домен из big_analytics_direct
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT domain
                FROM public.big_analytics_direct
                WHERE account_login = %s
                  AND domain IS NOT NULL AND domain != ''
                LIMIT 5
            """, (login,))
            domains_from_db = [row[0] for row in cur.fetchall()]
        for domain in domains_from_db:
            cid = _find_by_domain(domain)
            if cid:
                return cid, found_domain[0]
    except Exception:
        pass

    # 4. Домен из объявлений (Href)
    resp = api_get('ads', {
        'SelectionCriteria': {'States': ['ON', 'OFF', 'SUSPENDED', 'ARCHIVED']},
        'FieldNames':        ['Id'],
        'TextAdFieldNames':  ['Href'],
        'DynamicTextAdFieldNames': ['Href'],
        'Page':              {'Limit': 5},
    }, client_login=login)
    for ad in resp.get('result', {}).get('Ads', []):
        for ad_type in ('TextAd', 'DynamicTextAd'):
            href = (ad.get(ad_type) or {}).get('Href', '')
            if href:
                m = re.match(r'https?://([^/?#]+)', href)
                if m:
                    cid = _find_by_domain(m.group(1))
                    if cid:
                        return cid, found_domain[0]

    return None, None


# ── UTM-классификация (Директ) ────────────────────────────────────────────────

def classify(tracking):
    """OK / НЕТ_UTM / ДРУГОЙ_UTM"""
    if not tracking or 'utm_source' not in tracking:
        return 'НЕТ_UTM'
    term_ok   = 'utm_term={keyword}' in tracking or 'utm_term={phrase}' in tracking
    medium_ok = 'utm_medium=cpc' in tracking or 'utm_medium=cpa' in tracking
    if all(p in tracking for p in EXPECTED_PARTS) and term_ok and medium_ok:
        return 'OK'
    return 'ДРУГОЙ_UTM'


# ── БД: чтение ────────────────────────────────────────────────────────────────

def load_campaigns(conn):
    """
    Возвращает {login: {'camp_ids': [...], 'mk_tk_ids': [...], 'domain': str|None, 'directolog': str|None}}
    из big_analytics_direct — только аккаунты со статусом 'Контекст активно' в gsheet_sites.
    mk_tk_ids — кампании tp6/tp7 (МК/ТК) и tp8/tp9/tp10 (посевы Метрики), проверяются через Метрику напрямую.
    """
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT "CampaignId", account_login, MAX(COALESCE(tp, '')) AS tp
            FROM {SOURCE_TABLE}
            WHERE account_login IS NOT NULL
              AND "CampaignId"  IS NOT NULL
              AND COALESCE(campaign_status, 'x') NOT IN ('Остановлена', 'Архив')
              AND "CampaignId" IN (
                  SELECT "CampaignId" FROM {SOURCE_TABLE}
                  WHERE total_cost > 0
                    AND "Date" >= CURRENT_DATE - 30
              )
              AND account_login IN (
                  SELECT login_key FROM {T_GSHEET_SITES}
                  WHERE status = 'Контекст активно'
                    AND login_key IS NOT NULL
                    AND directologist IN (
                        SELECT name FROM public.specialists
                        WHERE name IS NOT NULL AND TRIM(name) != ''
                    )
              )
            GROUP BY "CampaignId", account_login
        """)
        camp_rows = cur.fetchall()

        cur.execute(f"""
            SELECT account_login, MIN(domain)
            FROM {SOURCE_TABLE}
            WHERE account_login IS NOT NULL
              AND domain IS NOT NULL AND TRIM(domain) != ''
            GROUP BY account_login
        """)
        domain_rows = cur.fetchall()

        cur.execute(f"""
            SELECT account_login, MAX("специалист")
            FROM {SOURCE_TABLE}
            WHERE account_login IS NOT NULL
              AND "специалист" IS NOT NULL AND TRIM("специалист") != ''
            GROUP BY account_login
        """)
        directolog_rows = cur.fetchall()

    domain_map     = {login: domain for login, domain in domain_rows}
    directolog_map = {login: d      for login, d      in directolog_rows}

    by_login = defaultdict(lambda: {'camp_ids': [], 'mk_tk_ids': [], 'domain': None, 'directolog': None})
    for camp_id, login, tp in camp_rows:
        if tp in ('tp6', 'tp7', 'tp8', 'tp9', 'tp10'):
            by_login[login]['mk_tk_ids'].append(camp_id)
        else:
            by_login[login]['camp_ids'].append(camp_id)
        by_login[login]['domain']     = domain_map.get(login)
        by_login[login]['directolog'] = directolog_map.get(login)
    return by_login


# ── БД: запись ────────────────────────────────────────────────────────────────

def create_target_table(conn):
    with conn.cursor() as cur:
        cur.execute(f'DROP TABLE IF EXISTS {TARGET_TABLE}')
        cur.execute(f"""
            CREATE TABLE {TARGET_TABLE} (
                id              SERIAL PRIMARY KEY,
                checked_at      TIMESTAMP DEFAULT NOW(),
                login           TEXT,
                "CampaignId"    BIGINT,
                "CampaignName"  TEXT,
                group_id        BIGINT,
                group_name      TEXT,
                status          TEXT,
                cls             TEXT,
                tracking_params TEXT,
                utm_source_type TEXT,
                domain          TEXT,
                counter_id      TEXT,
                "специалист"    TEXT
            )
        """)
    conn.commit()
    print(f'✓ Таблица {TARGET_TABLE} создана')


def save_rows(conn, rows):
    if not rows:
        return
    with conn.cursor() as cur:
        execute_values(cur, f"""
            INSERT INTO {TARGET_TABLE}
              (login, "CampaignId", "CampaignName", group_id, group_name,
               status, cls, tracking_params, utm_source_type, domain, counter_id,
               "специалист")
            VALUES %s
        """, rows)
    conn.commit()


# ── История групп с неверными UTM ────────────────────────────────────────────

def update_fuck_direct(conn):
    """
    Создаёт/обновляет public.check_utm_fuck_direct — ПОДЕНЕВАЯ история кампаний/групп с неверными UTM.

    НОВАЯ СТРУКТУРА (май 2026):
      1 строка = 1 (date, login, CampaignId, group_id) с плохой меткой и расходом.

    Логика заполнения:
      1. Из check_utm берём все плохие (login, CampaignId, group_id) где cls IN ('ДРУГОЙ_UTM','НЕТ_UTM').
      2. Для каждой кампании/группы определяем first_bad_check:
         - если в check_utm_fuck_direct уже есть записи по этой (login, CampaignId, group_id) — MIN(date)
         - иначе — CURRENT_DATE
      3. Джойним yandex_direct_manager_reports (FDW): берём все даты от first_bad_check до сегодня где есть расход
         (cost > 0) по этой CampaignId (и AdGroupId если group_id NOT NULL).
      4. INSERT ... ON CONFLICT DO NOTHING — новые (date, login, CampaignId, group_id) добавляются,
         существующие не трогаются.
      5. tracking_params — текущая плохая метка из check_utm (одна и та же для всех новых дат — упрощение).
      6. Когда метка исправлена (cls='OK' в текущем audit) — старые строки НЕ удаляются.
    """
    T   = FUCK_DIRECT_TABLE
    CHK = TARGET_TABLE    # public.check_utm

    with conn.cursor() as cur:
        # Создание таблицы (если не существует) — НОВАЯ структура
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {T} (
                id              SERIAL PRIMARY KEY,
                date            DATE NOT NULL,
                login           TEXT,
                "CampaignId"    BIGINT,
                "CampaignName"  TEXT,
                group_id        BIGINT,
                group_name      TEXT,
                tracking_params TEXT,
                "специалист"    TEXT,
                "домен"         TEXT,
                cost            NUMERIC,
                utm_source_type TEXT
            )
        """)
        # Unique index для ON CONFLICT (COALESCE для NULL group_id)
        cur.execute(f"""
            CREATE UNIQUE INDEX IF NOT EXISTS check_utm_fuck_direct_uniq
            ON {T} (date, login, "CampaignId", COALESCE(group_id, 0))
        """)
        # Вспомогательные индексы для аналитики
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_fuck_direct_date ON {T} (date)
        """)
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_fuck_direct_camp ON {T} ("CampaignId")
        """)
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_fuck_direct_specialist ON {T} ("специалист")
        """)

        # Основной UPSERT: на каждую плохую (login, CampaignId, group_id) — N строк по датам
        # с расходом в yandex_direct_manager_reports (FDW) от first_bad_check до сегодня.
        # VARA_DROP_LOCAL_YANDEX_FDW_2026-06-17: local_yandex заменена на FDW напрямую.
        #
        # first_bad_check:
        #   COALESCE(history.min_existing_date, CURRENT_DATE)
        #
        # Расход берём из public.yandex_direct_manager_reports (FDW). Поля: "CampaignId", "AdGroupId",
        # "Date" (text→::DATE), "Cost" (double→::NUMERIC), total_cost (NUMERIC если есть).
        # Для group_id IS NULL (tp6/tp7/tp8/tp9/tp10 МК/ТК) — игнорируем AdGroupId,
        # суммируем все группы кампании на эту дату.
        cur.execute(f"""
            WITH bad AS (
                -- Берём по одной строке на (login, CampaignId, group_id) с плохой меткой
                SELECT DISTINCT ON (login, "CampaignId", COALESCE(group_id, 0))
                    login,
                    "CampaignId",
                    "CampaignName",
                    group_id,
                    group_name,
                    tracking_params,
                    "специалист",
                    domain,
                    utm_source_type
                FROM {CHK}
                WHERE cls IN ('ДРУГОЙ_UTM', 'НЕТ_UTM')
                  AND login IS NOT NULL
                  AND "CampaignId" IS NOT NULL
            ),
            history AS (
                -- Существующая история по тем же ключам (login, CampaignId, group_id)
                SELECT
                    login,
                    "CampaignId",
                    group_id,
                    MIN(date) AS min_existing_date
                FROM {T}
                GROUP BY login, "CampaignId", group_id
            ),
            bad_with_start AS (
                SELECT
                    b.*,
                    COALESCE(h.min_existing_date, CURRENT_DATE - INTERVAL '90 days') AS first_bad_check
                FROM bad b
                LEFT JOIN history h
                       ON h.login        = b.login
                      AND h."CampaignId" = b."CampaignId"
                      AND (h.group_id = b.group_id
                           OR (h.group_id IS NULL AND b.group_id IS NULL))
            ),
            -- VARA_DROP_LOCAL_YANDEX_FDW_2026-06-17: переведено с local_yandex
            -- на FOREIGN TABLE yandex_direct_manager_reports (FDW напрямую).
            -- FDW специфика: "Date" = text → кастуем ::DATE; "Cost" = double precision.
            -- total_cost в FDW есть (NUMERIC) — используем его если не NULL, иначе "Cost".
            -- Расход по дням из FDW (RAW Direct API).
            -- Для group_id IS NULL — все AdGroupId этой кампании на эту дату суммируются.
            -- Для group_id NOT NULL — только нужная группа.
            costs_per_day AS (
                SELECT
                    bws.login,
                    bws."CampaignId",
                    bws.group_id,
                    ly."Date"::DATE AS date,
                    SUM(COALESCE(ly.total_cost, ly."Cost"::NUMERIC)) AS day_cost
                FROM bad_with_start bws
                JOIN public.yandex_direct_manager_reports ly
                  ON ly."CampaignId" = bws."CampaignId"
                 AND ly."Date"::DATE >= bws.first_bad_check
                 AND ly."Date"::DATE <= CURRENT_DATE
                 AND (bws.group_id IS NULL OR ly."AdGroupId" = bws.group_id)
                WHERE COALESCE(ly.total_cost, ly."Cost"::NUMERIC) IS NOT NULL
                  AND COALESCE(ly.total_cost, ly."Cost"::NUMERIC) > 0
                GROUP BY bws.login, bws."CampaignId", bws.group_id, ly."Date"::DATE
            )
            INSERT INTO {T} (
                date, login, "CampaignId", "CampaignName",
                group_id, group_name, tracking_params,
                "специалист", "домен", cost, utm_source_type
            )
            SELECT
                cpd.date,
                bws.login,
                bws."CampaignId",
                bws."CampaignName",
                bws.group_id,
                bws.group_name,
                bws.tracking_params,
                bws."специалист",
                bws.domain,
                cpd.day_cost,
                bws.utm_source_type
            FROM costs_per_day cpd
            JOIN bad_with_start bws
              ON bws.login        = cpd.login
             AND bws."CampaignId" = cpd."CampaignId"
             AND (bws.group_id = cpd.group_id
                  OR (bws.group_id IS NULL AND cpd.group_id IS NULL))
            ON CONFLICT (date, login, "CampaignId", COALESCE(group_id, 0)) DO NOTHING
        """)
        inserted = cur.rowcount

    conn.commit()
    print(f'\ncheck_utm_fuck_direct: вставлено новых дневных строк: {inserted}')


# ── Аудит одного логина (Директ) ─────────────────────────────────────────────

def fetch_campaign_names(login, camp_ids):
    """Возвращает {camp_id: name} для списка кампаний."""
    if not camp_ids:
        return {}
    resp = api_get('campaigns', {
        'SelectionCriteria': {'Ids': list(camp_ids)},
        'FieldNames': ['Id', 'Name'],
        'Page': {'Limit': 10000},
    }, client_login=login)
    time.sleep(0.25)
    return {c['Id']: c['Name'] for c in resp.get('result', {}).get('Campaigns', [])}


def audit_login(login, camp_ids, domain=None, directolog=None):
    if not camp_ids:
        return [], set(), {}

    camp_names = fetch_campaign_names(login, camp_ids)
    if not camp_names and camp_ids:
        return [], set(camp_ids), {}

    results        = []
    found_camp_ids = set()

    for i in range(0, len(camp_ids), 10):
        batch = camp_ids[i:i + 10]
        gr = api_get('adgroups', {
            'SelectionCriteria': {'CampaignIds': batch},
            'FieldNames': ['Id', 'Name', 'CampaignId', 'TrackingParams', 'Status'],
            'Page': {'Limit': 10000},
        }, client_login=login)
        time.sleep(0.2)

        if 'error' in gr:
            continue

        for g in gr.get('result', {}).get('AdGroups', []):
            tracking = g.get('TrackingParams') or ''
            cls      = classify(tracking)
            camp_id  = g['CampaignId']
            found_camp_ids.add(camp_id)
            results.append((
                login,
                camp_id,
                camp_names.get(camp_id, ''),
                g['Id'],
                g['Name'],
                g.get('Status'),
                cls,
                tracking or '(пусто)',
                'direct',
                domain,
                None,
                directolog,
            ))

    failed_camp_ids = set(camp_ids) - found_camp_ids
    return results, failed_camp_ids, camp_names


# ── Fallback — Метрика ────────────────────────────────────────────────────────

_COUNTER_SEARCH_TIMEOUT = 90   # секунд на поиск счётчика для одного логина
_METRIKA_LOOP_TIMEOUT   = 120  # секунд на весь цикл запросов к Метрике по кампаниям


def _load_mk_group_ids(conn, login: str, camp_ids: list) -> dict:
    """Возвращает {campaign_id: [ad_group_id, ...]} из big_analytics_direct.
    Нужно чтобы tp6/tp7/tp8/tp9/tp10 строки в check_utm имели реальные group_id
    и JOIN с big_analytics_direct работал корректно."""
    if not camp_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT "CampaignId", "AdGroupId"
            FROM {SOURCE_TABLE}
            WHERE account_login = %s
              AND "CampaignId" = ANY(%s)
              AND "AdGroupId" IS NOT NULL
            GROUP BY "CampaignId", "AdGroupId"
        """, (login, camp_ids))
        result = {}
        for camp_id, group_id in cur.fetchall():
            result.setdefault(camp_id, []).append(group_id)
    return result


def audit_via_metrika(login, camp_ids, all_counters, conn, camp_names=None, directolog=None):
    results = []

    # Поиск counter_id с ограничением времени — некоторые аккаунты без API-доступа
    # вызывают каскадные ретраи и зависают на 10+ минут
    counter_result = [None, None]
    done = threading.Event()

    def _find():
        try:
            counter_result[0], counter_result[1] = get_counter_for_login(
                login, camp_ids, all_counters, conn)
        finally:
            done.set()

    t = threading.Thread(target=_find, daemon=True)
    t.start()
    if not done.wait(timeout=_COUNTER_SEARCH_TIMEOUT):
        print(f'    [metrika] таймаут {_COUNTER_SEARCH_TIMEOUT}s — пропущено', flush=True)
        return results

    counter_id, domain = counter_result
    time.sleep(0.3)

    if not counter_id:
        print(f'    [metrika] счётчик не найден для {login}')
        return results

    print(f'    [metrika] counter={counter_id}', flush=True)

    names    = camp_names or {}
    deadline = time.time() + _METRIKA_LOOP_TIMEOUT
    for camp_id in camp_ids:
        if _metrika_quota_exhausted:
            print(f'    [metrika] квота исчерпана — пропускаем оставшиеся {len(camp_ids) - len(results)} кампаний', flush=True)
            break
        if time.time() > deadline:
            print(f'    [metrika] таймаут {_METRIKA_LOOP_TIMEOUT}s — обработано {len(results)} из {len(camp_ids)}, пропускаем остаток', flush=True)
            break
        tracking, cls = get_utm_via_metrika(counter_id, camp_id)
        if tracking is None:
            cls      = 'НЕТ_ТРАФИКА'
            tracking = '(нет трафика в Метрике)'
        results.append((
            login, camp_id, names.get(camp_id, ''), None, None,
            None, cls, tracking, 'metrika', domain, counter_id, directolog,
        ))

    return results


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*60}")
    print('  UTM DIRECT AUDIT  (big_analytics_v5)')
    print(f"{'='*60}\n")

    conn = psycopg2.connect(**DB_DST)
    try:
        by_login = load_campaigns(conn)
    finally:
        conn.close()

    if not by_login:
        print(f'✗ Нет данных в {SOURCE_TABLE}')
        return

    total_camps  = sum(len(v['camp_ids']) for v in by_login.values())
    total_logins = len(by_login)
    print(f'Логинов: {total_logins}, кампаний: {total_camps}\n')

    print('▶ Загружаем счётчики Метрики...')
    all_counters = load_metrika_counters()
    print(f'  ✓ Счётчиков: {len(all_counters)}\n')

    conn = psycopg2.connect(**DB_DST)
    try:
        create_target_table(conn)
        print()

        all_results = []
        batch       = []
        FLUSH_EVERY = 5

        for idx, (login, info) in enumerate(by_login.items(), 1):
            camp_ids   = info['camp_ids']
            mk_tk_ids  = info['mk_tk_ids']
            domain     = info['domain']
            directolog = info['directolog']
            total_camp_count = len(camp_ids) + len(mk_tk_ids)
            print(f'[{idx}/{total_logins}] {login}  ({total_camp_count} кампаний)...', end=' ', flush=True)

            direct_results, failed_ids, camp_names = audit_login(login, camp_ids, domain, directolog)

            ok_cnt  = sum(1 for r in direct_results if r[6] == 'OK')
            bad_cnt = len(direct_results) - ok_cnt

            if direct_results:
                print(f'direct: ❌ {bad_cnt} проблемных, ✓ {ok_cnt} OK', end='')
            else:
                print('direct: — нет данных', end='')

            metrika_results = []
            if failed_ids and (not direct_results or bad_cnt > 0):
                print(f'  →  metrika fallback: {len(failed_ids)} кампаний...', end=' ', flush=True)
                metrika_results = audit_via_metrika(login, list(failed_ids), all_counters, conn,
                                                    camp_names=camp_names, directolog=directolog)
                m_ok         = sum(1 for r in metrika_results if r[6] == 'OK')
                m_no_traffic = sum(1 for r in metrika_results if r[6] == 'НЕТ_ТРАФИКА')
                m_no_utm     = sum(1 for r in metrika_results if r[6] == 'НЕТ_UTM')
                m_other      = sum(1 for r in metrika_results if r[6] == 'ДРУГОЙ_UTM')
                print(f'OK:{m_ok}  НЕТ_ТРАФИКА:{m_no_traffic}  НЕТ_UTM:{m_no_utm}  ДРУГОЙ:{m_other}', end='')

            mk_tk_results = []
            if mk_tk_ids:
                print(f'  →  МК/ТК метрика: {len(mk_tk_ids)} кампаний...', end=' ', flush=True)
                mk_names = fetch_campaign_names(login, mk_tk_ids)
                mk_tk_results = audit_via_metrika(login, mk_tk_ids, all_counters, conn,
                                                  camp_names=mk_names, directolog=directolog)
                # Расширяем: одна строка на каждый AdGroupId из big_analytics_direct
                # (иначе JOIN по group_id пропустит эти строки — group_id был бы NULL)
                mk_group_ids = _load_mk_group_ids(conn, login, mk_tk_ids)
                expanded = []
                for row in mk_tk_results:
                    groups = mk_group_ids.get(row[1])  # row[1] = camp_id
                    if groups:
                        for gid in groups:
                            r = list(row); r[3] = gid; expanded.append(tuple(r))
                    else:
                        expanded.append(row)
                mk_tk_results = expanded

                mk_ok         = sum(1 for r in mk_tk_results if r[6] == 'OK')
                mk_no_traffic = sum(1 for r in mk_tk_results if r[6] == 'НЕТ_ТРАФИКА')
                mk_no_utm     = sum(1 for r in mk_tk_results if r[6] == 'НЕТ_UTM')
                mk_other      = sum(1 for r in mk_tk_results if r[6] == 'ДРУГОЙ_UTM')
                print(f'OK:{mk_ok}  НЕТ_ТРАФИКА:{mk_no_traffic}  НЕТ_UTM:{mk_no_utm}  ДРУГОЙ:{mk_other}', end='')

            print()

            combined = direct_results + metrika_results + mk_tk_results
            all_results.extend(combined)
            batch.extend(combined)

            if idx % FLUSH_EVERY == 0 and batch:
                save_rows(conn, batch)
                print(f'  >> Сохранено: {len(batch)} (итого {len(all_results)})')
                batch = []

        if batch:
            save_rows(conn, batch)
            print(f'  >> Сохранено: {len(batch)} (итого {len(all_results)})')

        # Заполняем пустые CampaignName в check_utm из big_analytics_direct
        with conn.cursor() as cur:
            cur.execute(f"""
                UPDATE {TARGET_TABLE} cu
                SET "CampaignName" = b.name
                FROM (
                    SELECT "CampaignId", MAX("CampaignName") AS name
                    FROM {SOURCE_TABLE}
                    WHERE "CampaignName" IS NOT NULL AND TRIM("CampaignName") != ''
                    GROUP BY "CampaignId"
                ) b
                WHERE cu."CampaignId" = b."CampaignId"
                  AND (cu."CampaignName" IS NULL OR TRIM(cu."CampaignName") = '')
            """)
            filled = cur.rowcount
        conn.commit()
        if filled:
            print(f'  >> CampaignName заполнен для {filled} строк в check_utm')

    finally:
        conn.close()

    counts     = Counter(r[6] for r in all_results)
    src_counts = Counter(r[8] for r in all_results)
    print(f"\n{'='*60}")
    print('  ИТОГ:')
    print(f'  Логинов: {total_logins}')
    print(f'  Записей: {len(all_results)}')
    print(f'    direct:  {src_counts.get("direct", 0)}')
    print(f'    metrika: {src_counts.get("metrika", 0)}')
    print()
    for cls, cnt in sorted(counts.items(), key=lambda x: -x[1]):
        print(f'    {cls:<16} — {cnt}')
    print(f"{'='*60}\n")

    if _api_auth_failed:
        lines = '\n'.join(sorted(_api_auth_failed)[:50])
        tail  = f'\n...и ещё {len(_api_auth_failed) - 50}' if len(_api_auth_failed) > 50 else ''
        _send_tg(f'⚠️ utm_direct_audit: нет доступа по токенам для {len(_api_auth_failed)} аккаунтов:\n{lines}{tail}')

    # Обновляем историю групп с неверными UTM
    conn = psycopg2.connect(**DB_DST)
    try:
        update_fuck_direct(conn)
    finally:
        conn.close()


def run(conn=None, run_id=None, **kwargs) -> dict:
    """Точка входа для pipeline.py (шаг 4)."""
    main()
    # Считаем строки в check_utm для отчёта pipeline
    c = psycopg2.connect(**DB_DST)
    try:
        with c.cursor() as cur:
            cur.execute(f'SELECT COUNT(*) FROM {TARGET_TABLE}')
            rows = cur.fetchone()[0]
    finally:
        c.close()
    return {'rows': rows}


if __name__ == '__main__':
    main()
