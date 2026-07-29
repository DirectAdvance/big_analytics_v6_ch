"""
korrektirovki/korrektirovki.py
==============================
Корректировки ставок (bid modifiers) по всем активным аккаунтам Яндекс.Директ.

БД: ad_analytics_bi, таблица public.yandex_direct_korrektirovki
Источник логинов: local_gsheet_sites WHERE status IN ('Контекст активно', 'На модерации', 'В работе', 'Стоп', 'Удален')
Стратегия: DROP + CREATE + INSERT (полный снапшот)

Запуск вручную:
    ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 korrektirovki/run.py"
"""

import logging
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import psycopg2.extras
import requests

from config.cookies import send_tg as _send_tg
from config.db import get_conn, put_conn
from config.settings import T_GSHEET_SITES
from config.tokens import OAUTH_TOKEN_1, OAUTH_TOKEN_2, OAUTH_TOKEN_3, OAUTH_TOKEN_4, OAUTH_TOKEN_5

logger = logging.getLogger('korrektirovki')

# ── Конфиг ────────────────────────────────────────────────────────────────────

OAUTH_TOKENS       = [OAUTH_TOKEN_1, OAUTH_TOKEN_2, OAUTH_TOKEN_3, OAUTH_TOKEN_4, OAUTH_TOKEN_5]
CAMPAIGN_PAGE_SIZE = 1_000
CAMPAIGN_BATCH     = 500
REQUEST_DELAY      = 0.5   # между страницами / батчами одного логина
MAX_WORKERS        = 2     # параллельных потоков (больше = риск 429)

ACTIVE_STATUSES    = ('Контекст активно', 'На модерации', 'В работе')
T_KORREKTIROVKI    = 'public.yandex_direct_korrektirovki'
T_CAMPAIGN_STATUS  = 'campaign_status'

API_CAMPAIGNS    = 'https://api.direct.yandex.com/json/v5/campaigns'
API_BIDMODIFIERS = 'https://api.direct.yandex.com/json/v5/bidmodifiers'
API_RETARGETING  = 'https://api.direct.yandex.com/json/v5/retargetinglists'

MODIFIER_FIELD_NAMES = ['Id', 'CampaignId', 'AdGroupId', 'Level', 'Type']

MODIFIER_TYPE_FIELD_NAMES = {
    'DemographicsAdjustmentFieldNames':  ['Gender', 'Age', 'BidModifier', 'Enabled'],
    'MobileAdjustmentFieldNames':        ['BidModifier', 'OperatingSystemType'],
    'TabletAdjustmentFieldNames':        ['BidModifier', 'OperatingSystemType'],
    'DesktopOnlyAdjustmentFieldNames':   ['BidModifier'],
    'RegionalAdjustmentFieldNames':      ['RegionId', 'BidModifier', 'Enabled'],
    'RetargetingAdjustmentFieldNames':   ['RetargetingConditionId', 'BidModifier', 'Accessible', 'Enabled'],
    'VideoAdjustmentFieldNames':         ['BidModifier'],
    'SmartAdAdjustmentFieldNames':       ['BidModifier'],
    'SmartTvAdjustmentFieldNames':       ['BidModifier'],
    'SerpLayoutAdjustmentFieldNames':    ['SerpLayout', 'BidModifier', 'Enabled'],
    'IncomeGradeAdjustmentFieldNames':   ['Grade', 'BidModifier', 'Enabled'],
    'AdGroupAdjustmentFieldNames':       ['BidModifier'],
}

GENDER_LABEL = {
    'GENDER_MALE':   'Мужчины',
    'GENDER_FEMALE': 'Женщины',
}

AGE_LABEL = {
    'AGE_0_17':  'младше 18',
    'AGE_18_24': '18–24',
    'AGE_25_34': '25–34',
    'AGE_35_44': '35–44',
    'AGE_45_54': '45–54',
    'AGE_45':    '45+',
    'AGE_55':    '55+',
}

TYPE_LABEL = {
    'MOBILE_ADJUSTMENT':       'Мобильные',
    'TABLET_ADJUSTMENT':       'Планшеты',
    'DESKTOP_ONLY_ADJUSTMENT': 'Только десктоп',
    'DESKTOP_ADJUSTMENT':      'Десктоп',
    'VIDEO_ADJUSTMENT':        'Видео',
    'SMART_TV_ADJUSTMENT':     'Смарт ТВ',
    'SMART_AD_ADJUSTMENT':     'Смарт-баннеры',
    'AD_GROUP_ADJUSTMENT':     'Группа',
}


def _bid_percent_str(bid_modifier) -> str | None:
    if bid_modifier is None:
        return None
    pct = bid_modifier - 100
    return f'+{pct}%' if pct >= 0 else f'{pct}%'


def _enabled_str(obj: dict) -> str:
    return 'YES' if obj.get('Enabled') == 'YES' else 'NO'


# ── Авторизация ────────────────────────────────────────────────────────────────

def _find_working_token(ulogin: str) -> str | None:
    for token in OAUTH_TOKENS:
        headers = {
            'Authorization': f'Bearer {token}',
            'Client-Login':  ulogin,
            'Content-Type':  'application/json; charset=utf-8',
        }
        body = {'method': 'get', 'params': {
            'SelectionCriteria': {}, 'FieldNames': ['Id'], 'Page': {'Limit': 1},
        }}
        try:
            r = requests.post(API_CAMPAIGNS, headers=headers, json=body, timeout=30)
            if 'error' not in r.json():
                return token
        except Exception:
            pass
    return None


def _headers(token: str, ulogin: str) -> dict:
    return {
        'Authorization': f'Bearer {token}',
        'Client-Login':  ulogin,
        'Content-Type':  'application/json; charset=utf-8',
    }


# ── API: кампании ──────────────────────────────────────────────────────────────

def _fetch_campaigns(hdrs: dict) -> list[dict]:
    campaigns, offset = [], 0
    while True:
        body = {'method': 'get', 'params': {
            'SelectionCriteria': {},
            'FieldNames': ['Id', 'Name'],
            'Page': {'Limit': CAMPAIGN_PAGE_SIZE, 'Offset': offset},
        }}
        r = requests.post(API_CAMPAIGNS, headers=hdrs, json=body, timeout=60)
        data = r.json()
        if 'error' in data:
            raise RuntimeError(f"campaigns.get: {data['error']}")
        result = data.get('result', {})
        campaigns.extend(result.get('Campaigns', []))
        if result.get('LimitedBy') is None:
            break
        offset += CAMPAIGN_PAGE_SIZE
        time.sleep(REQUEST_DELAY)
    return campaigns


# ── API: ретаргетинг ───────────────────────────────────────────────────────────

def _fetch_retargeting_names(hdrs: dict, ids: list[int]) -> dict[int, str]:
    if not ids:
        return {}
    name_map = {}
    for i in range(0, len(ids), 1_000):
        body = {'method': 'get', 'params': {
            'SelectionCriteria': {'Ids': ids[i:i + 1_000]},
            'FieldNames': ['Id', 'Name'],
        }}
        try:
            r = requests.post(API_RETARGETING, headers=hdrs, json=body, timeout=60)
            data = r.json()
            if 'error' not in data:
                for item in data.get('result', {}).get('RetargetingLists', []):
                    name_map[item['Id']] = item['Name']
        except Exception:
            pass
        time.sleep(REQUEST_DELAY)
    return name_map


# ── API: корректировки ─────────────────────────────────────────────────────────

def _fetch_bidmodifiers(hdrs: dict, campaign_ids: list[int]) -> list[dict]:
    params = {
        'SelectionCriteria': {'CampaignIds': campaign_ids, 'Levels': ['CAMPAIGN', 'AD_GROUP']},
        'FieldNames': MODIFIER_FIELD_NAMES,
    }
    params.update(MODIFIER_TYPE_FIELD_NAMES)
    r = requests.post(API_BIDMODIFIERS, headers=hdrs, json={'method': 'get', 'params': params}, timeout=60)
    data = r.json()
    if 'error' in data:
        raise RuntimeError(f"bidmodifiers.get: {data['error']}")
    return data.get('result', {}).get('BidModifiers', [])


# ── Разворачивание в плоские строки ───────────────────────────────────────────

def _expand(raw: dict, ulogin: str, camp_names: dict, ret_names: dict) -> list[dict]:
    base = {
        'ulogin':        ulogin,
        'campaign_id':   raw.get('CampaignId'),
        'campaign_name': camp_names.get(raw.get('CampaignId'), ''),
        'ad_group_id':   raw.get('AdGroupId'),
        'level':         raw.get('Level', ''),
        'modifier_id':   raw.get('Id'),
        'modifier_type': raw.get('Type', ''),
    }
    t = raw.get('Type', '')

    if t == 'DEMOGRAPHICS_ADJUSTMENT':
        obj = raw.get('DemographicsAdjustment') or {}
        if not obj:
            return []
        g = GENDER_LABEL.get(obj.get('Gender'), 'Все')
        a = AGE_LABEL.get(obj.get('Age'), obj.get('Age') or 'Все')
        return [{**base,
            'modifier_name': f'Пол и возраст: {g} ({a})',
            'bid_percent':   _bid_percent_str(obj.get('BidModifier')),
            'enabled':       _enabled_str(obj),
        }]

    if t == 'RETARGETING_ADJUSTMENT':
        obj = raw.get('RetargetingAdjustment') or {}
        if not obj:
            return []
        cid = obj.get('RetargetingConditionId')
        return [{**base,
            'modifier_name': f'Целевая аудитория: {ret_names.get(cid, f"ID {cid}")}',
            'bid_percent':   _bid_percent_str(obj.get('BidModifier')),
            'enabled':       _enabled_str(obj),
            'audience_id':   cid,
        }]

    if t == 'RETARGETING_SEARCH_ADJUSTMENT':
        return [{**base, 'modifier_name': 'Целевая аудитория (поиск)', 'bid_percent': None, 'enabled': 'NO'}]

    if t == 'REGIONAL_ADJUSTMENT':
        obj = raw.get('RegionalAdjustment') or {}
        if not obj:
            return []
        return [{**base,
            'modifier_name': f'Регион {obj.get("RegionId")}',
            'bid_percent':   _bid_percent_str(obj.get('BidModifier')),
            'enabled':       _enabled_str(obj),
        }]

    if t == 'SERP_LAYOUT_ADJUSTMENT':
        obj = raw.get('SerpLayoutAdjustment') or {}
        if not obj:
            return []
        return [{**base,
            'modifier_name': f'Позиция в выдаче: {obj.get("SerpLayout", "")}',
            'bid_percent':   _bid_percent_str(obj.get('BidModifier')),
            'enabled':       _enabled_str(obj),
        }]

    if t == 'INCOME_GRADE_ADJUSTMENT':
        obj = raw.get('IncomeGradeAdjustment') or {}
        if not obj:
            return []
        return [{**base,
            'modifier_name': f'Доход: {obj.get("Grade", "")}',
            'bid_percent':   _bid_percent_str(obj.get('BidModifier')),
            'enabled':       _enabled_str(obj),
        }]

    single_map = {
        'MOBILE_ADJUSTMENT':       'MobileAdjustment',
        'TABLET_ADJUSTMENT':       'TabletAdjustment',
        'DESKTOP_ONLY_ADJUSTMENT': 'DesktopOnlyAdjustment',
        'DESKTOP_ADJUSTMENT':      'DesktopAdjustment',
        'VIDEO_ADJUSTMENT':        'VideoAdjustment',
        'SMART_TV_ADJUSTMENT':     'SmartTvAdjustment',
        'SMART_AD_ADJUSTMENT':     'SmartAdAdjustment',
        'AD_GROUP_ADJUSTMENT':     'AdGroupAdjustment',
    }
    if t in single_map:
        obj = raw.get(single_map[t])
        if not obj:
            return []
        return [{**base,
            'modifier_name': TYPE_LABEL.get(t, t),
            'bid_percent':   _bid_percent_str(obj.get('BidModifier')),
            'enabled':       _enabled_str(obj),
        }]

    logger.warning('[expand] Неизвестный тип: %s id=%s', t, raw.get('Id'))
    return [{**base, 'modifier_name': t, 'bid_percent': None, 'enabled': 'NO'}]


# ── Справочники из БД ──────────────────────────────────────────────────────────

def _load_logins(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT DISTINCT login_key FROM {T_GSHEET_SITES}
            WHERE status = ANY(%s) AND login_key IS NOT NULL AND login_key != ''
              AND directologist IS NOT NULL AND directologist != ''
              AND directologist IN (
                  SELECT name FROM public.specialists
                  WHERE name IS NOT NULL AND TRIM(name) != ''
              )
            ORDER BY login_key
        """, (list(ACTIVE_STATUSES),))
        result = []
        for (login,) in cur.fetchall():
            if not login.isascii() or not any(c.isalnum() for c in login):
                logger.warning('[logins] Пропускаем невалидный login_key=%r', login)
                continue
            result.append(login)
        return result


def _load_specialist_map(conn) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT DISTINCT ON (login_key) login_key, directologist
            FROM {T_GSHEET_SITES}
            WHERE login_key IS NOT NULL AND directologist IS NOT NULL
            ORDER BY login_key
        """)
        return {row[0]: row[1] for row in cur.fetchall()}


def _load_status_map(conn) -> dict[int, str]:
    try:
        with conn.cursor() as cur:
            cur.execute(f'SELECT "CampaignId", campaign_status FROM {T_CAMPAIGN_STATUS}')
            return {row[0]: row[1] for row in cur.fetchall()}
    except Exception as e:
        logger.warning('campaign_status недоступен: %s', e)
        return {}


def _load_site_status_map(conn) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT DISTINCT ON (login_key) login_key, status
            FROM {T_GSHEET_SITES}
            WHERE login_key IS NOT NULL AND login_key != ''
            ORDER BY login_key
        """)
        return {row[0]: row[1] for row in cur.fetchall()}


# ── БД: сохранение ────────────────────────────────────────────────────────────

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS public.yandex_direct_korrektirovki (
    id                BIGSERIAL PRIMARY KEY,
    ulogin            TEXT,
    campaign_id       BIGINT,
    campaign_name     TEXT,
    ad_group_id       BIGINT,
    level             TEXT,
    modifier_id       BIGINT,
    enabled           TEXT,
    modifier_type     TEXT,
    modifier_name     TEXT,
    bid_percent       TEXT,
    korrektirovki_bid TEXT,
    audience_id       BIGINT,
    "специалист"      TEXT,
    campaign_status   TEXT,
    status            TEXT,
    loaded_at         TIMESTAMPTZ DEFAULT NOW()
);
"""

_INSERT_SQL = """
INSERT INTO public.yandex_direct_korrektirovki (
    ulogin, campaign_id, campaign_name, ad_group_id, level,
    modifier_id, enabled, modifier_type, modifier_name, bid_percent,
    korrektirovki_bid, audience_id, "специалист", campaign_status, status, loaded_at
) VALUES (
    %(ulogin)s, %(campaign_id)s, %(campaign_name)s, %(ad_group_id)s, %(level)s,
    %(modifier_id)s, %(enabled)s, %(modifier_type)s, %(modifier_name)s, %(bid_percent)s,
    %(korrektirovki_bid)s, %(audience_id)s, %(специалист)s, %(campaign_status)s, %(status)s, NOW()
)
"""


def _save(conn, rows: list[dict]) -> int:
    prepared = []
    for row in rows:
        r = dict(row)
        r.setdefault('modifier_name', None)
        r.setdefault('bid_percent', None)
        r.setdefault('audience_id', 0)
        r.setdefault('enabled', 'NO')
        r.setdefault('специалист', None)
        r.setdefault('campaign_status', None)
        r.setdefault('status', None)
        mn, bp = r.get('modifier_name'), r.get('bid_percent')
        r['korrektirovki_bid'] = f'{mn} | {bp}' if mn and bp else (mn or bp)
        prepared.append(r)

    with conn.cursor() as cur:
        cur.execute('DROP TABLE IF EXISTS public.yandex_direct_korrektirovki')
        cur.execute(_CREATE_SQL)
        psycopg2.extras.execute_batch(cur, _INSERT_SQL, prepared, page_size=1_000)
    conn.commit()
    return len(prepared)


# ── Обработка одного аккаунта ─────────────────────────────────────────────────

def _fetch_login(ulogin: str, spec_map: dict, status_map: dict, site_status_map: dict) -> tuple[list[dict], bool]:
    token = _find_working_token(ulogin)
    if not token:
        logger.warning('[%s] Ни один токен не подошёл', ulogin)
        return [], True

    hdrs        = _headers(token, ulogin)
    spec        = spec_map.get(ulogin)
    site_status = site_status_map.get(ulogin)

    try:
        campaigns = _fetch_campaigns(hdrs)
    except Exception as e:
        logger.warning('[%s] campaigns.get: %s', ulogin, e)
        return [], False

    if not campaigns:
        return [], False

    camp_names  = {c['Id']: c['Name'] for c in campaigns}
    camp_ids    = list(camp_names.keys())
    all_raw     = []

    for i in range(0, len(camp_ids), CAMPAIGN_BATCH):
        try:
            batch = _fetch_bidmodifiers(hdrs, camp_ids[i:i + CAMPAIGN_BATCH])
            all_raw.extend(batch)
        except Exception as e:
            logger.warning('[%s] bidmodifiers батч %d: %s', ulogin, i // CAMPAIGN_BATCH, e)
        time.sleep(REQUEST_DELAY)

    ret_ids = list({
        m.get('RetargetingAdjustment', {}).get('RetargetingConditionId')
        for m in all_raw
        if m.get('Type') == 'RETARGETING_ADJUSTMENT'
        and m.get('RetargetingAdjustment', {}).get('RetargetingConditionId')
    })
    ret_names = _fetch_retargeting_names(hdrs, ret_ids)

    rows = []
    for raw_mod in all_raw:
        for row in _expand(raw_mod, ulogin, camp_names, ret_names):
            row['специалист']     = spec
            row['campaign_status'] = status_map.get(row.get('campaign_id'))
            row['status']          = site_status
            rows.append(row)

    return rows, False


# ── Точка входа ───────────────────────────────────────────────────────────────

def main() -> None:
    import config.db as db_module
    db_module.init_pool()

    conn = get_conn()
    try:
        logins          = _load_logins(conn)
        spec_map        = _load_specialist_map(conn)
        status_map      = _load_status_map(conn)
        site_status_map = _load_site_status_map(conn)
    finally:
        put_conn(conn)

    logger.info('Аккаунтов: %d, потоков: %d', len(logins), MAX_WORKERS)

    all_rows, ok = [], 0
    no_token_logins = []

    def _process(ulogin: str) -> tuple[str, list[dict], bool, float]:
        t0 = time.perf_counter()
        rows, auth_failed = _fetch_login(ulogin, spec_map, status_map, site_status_map)
        return ulogin, rows, auth_failed, time.perf_counter() - t0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_process, u): u for u in logins}
        for future in as_completed(futures):
            ulogin, rows, auth_failed, elapsed = future.result()
            if auth_failed:
                no_token_logins.append(ulogin)
            all_rows.extend(rows)
            if rows:
                ok += 1
                logger.info('[%s] %d строк за %.1fс', ulogin, len(rows), elapsed)
            else:
                logger.warning('[%s] 0 строк', ulogin)

    type_counts = Counter(r['modifier_type'] for r in all_rows)
    logger.info('Итого: %d строк, %d/%d аккаунтов, типы: %s',
                len(all_rows), ok, len(logins), dict(type_counts))

    if no_token_logins:
        lines = '\n'.join(no_token_logins[:50])
        tail  = f'\n...и ещё {len(no_token_logins) - 50}' if len(no_token_logins) > 50 else ''
        _send_tg(f'⚠️ korrektirovki: токен не подошёл для {len(no_token_logins)} аккаунтов:\n{lines}{tail}')
        logger.warning('Токен не подошёл: %d аккаунтов', len(no_token_logins))

    conn = get_conn()
    try:
        saved = _save(conn, all_rows)
        logger.info('Записано в БД: %d строк', saved)
    finally:
        put_conn(conn)

    db_module.close_pool()
