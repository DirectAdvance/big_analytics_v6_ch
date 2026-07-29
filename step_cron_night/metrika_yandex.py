"""
metrika_yandex.py — синхронизация счётчиков Яндекс.Метрики

Фаза 1: UPSERT доменов из local_gsheet_sites (domain + login_key).
Фаза 2: для строк в metrika_yandex где counter_id IS NULL —
         загружает все счётчики Метрики и проставляет counter_id.
Фаза 2.5 (fallback): для доменов где counter_id всё ещё NULL —
         загружает счётчики через токен victoryagency-direct1618440,
         выдаёт grant view victorylotsofads1, записывает counter_id.
Фаза 3: для строк где counter_id IS NOT NULL и хотя бы одна цель IS NULL —
         запрашивает /goals для каждого такого счётчика и заполняет.
Фаза 4: grant view для всех аккаунтов из GRANT_TARGETS на все счётчики
         где соответствующий флаг = FALSE (round-robin: у всех одинаковый доступ).

Цели матчатся по названию:
  all_forms          ← "Все формы"
  crm_order_created  ← "CRM: Заказ создан"
  crm_order_paid     ← "CRM: Заказ оплачен"
  crm_spam_order     ← "CRM: Спам заказ"
  crm_order_canceled ← "CRM: Заказ отменен"

Запуск вручную:
    python metrika_yandex.py

Из pipeline.py вызывается как фоновый поток после step0.
"""

import logging
import os
import re
import sys
import time

# Добавляем BASE_DIR (big_analytics_v5/) в sys.path для subprocess-запуска
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2
import requests
from psycopg2.extras import execute_values

from config.settings import DB_DST, T_GSHEET_SITES
from config.tokens import (
    METRIKA_TOKEN,
    OAUTH_TOKEN_2,
    OAUTH_TOKEN_3,
    OAUTH_TOKEN_4,
    OAUTH_TOKEN_5,
)

logger = logging.getLogger('metrika_yandex')

METRIKA_BASE     = 'https://api-metrika.yandex.net'
T_METRIKA_YANDEX = 'metrika_yandex'

GOAL_NAME_MAP = {
    'Все формы':            'all_forms',
    'CRM: Заказ создан':    'crm_order_created',
    'CRM: Заказ оплачен':   'crm_order_paid',
    'CRM: Спам заказ':      'crm_spam_order',
    'CRM: Заказ отменен':   'crm_order_canceled',
}

# Донорские токены для выдачи grant (perm=view) target-аккаунтам.
# Главный донор victoryagency-direct1618440 — у него все доступы к счётчикам.
# Остальные — fallback если donor не владелец конкретного счётчика.
GRANT_DONOR_TOKENS = [
    ('victoryagency-direct1618440', OAUTH_TOKEN_2),
    ('y-direct-victory',            OAUTH_TOKEN_3),
    ('victoryagency14',             OAUTH_TOKEN_4),
    ('useful-call-agency',          OAUTH_TOKEN_5),
]

# Target-аккаунты получающие grant.
# Колонка в metrika_yandex отслеживает выдачу для каждого target отдельно.
#   victorylotsofads1   — main, для metrika_yandex sync (цели всех счётчиков)
#   victorylotsofads04  — audit, для utm_direct_audit (round-robin)
#   skuderko1           — extra, третий токен round-robin в utm_direct_audit
# Фаза 4 выдаёт grant ВСЕМ трём аккаунтам на ВСЕ счётчики (без фильтра по tp).
GRANT_TARGETS = [
    ('victorylotsofads1',  'grant_done_lots1'),
    ('victorylotsofads04', 'grant_done_lots04'),
    ('skuderko1',          'grant_done_skuderko1'),
]

_BAD_LOGIN = re.compile(r'^-+$')


def _valid_login(lk) -> bool:
    if not lk:
        return False
    lk = lk.strip()
    return bool(lk) and lk != 'Нет' and not _BAD_LOGIN.match(lk)


# ── Metrika API ───────────────────────────────────────────────────────────────

def _metrika_get(path: str, params: dict = None, attempt: int = 0) -> dict:
    headers = {'Authorization': f'OAuth {METRIKA_TOKEN}'}
    try:
        r = requests.get(f'{METRIKA_BASE}{path}', params=params or {},
                         headers=headers, timeout=30)
        if r.status_code == 429:
            wait = int(r.headers.get('Retry-After', 60))
            logger.warning('[Metrika 429] ждём %ds...', wait)
            time.sleep(min(wait, 60))
            if attempt < 3:
                return _metrika_get(path, params, attempt + 1)
        r.encoding = 'utf-8'
        return r.json()
    except Exception as e:
        if attempt < 2:
            time.sleep(3)
            return _metrika_get(path, params, attempt + 1)
        return {'errors': [str(e)]}


def load_metrika_counters() -> list:
    """Загружает все счётчики Метрики (постранично)."""
    all_counters: list = []
    seen_ids: set = set()
    page = 1
    while True:
        resp = _metrika_get('/management/v1/counters', {'per_page': 1000, 'page': page})
        counters = resp.get('counters', [])
        if not counters:
            break
        for c in counters:
            cid = c.get('id')
            if cid and cid not in seen_ids:
                seen_ids.add(cid)
                all_counters.append(c)
        rows_total = resp.get('rows', 0)
        if page * 1000 >= rows_total:
            break
        page += 1
        time.sleep(0.3)
    return all_counters


def load_goals(counter_id: int) -> dict:
    """Возвращает dict колонка→goal_id для счётчика. Пропускает 403/404."""
    resp = _metrika_get(f'/management/v1/counter/{counter_id}/goals')
    if 'errors' in resp or 'goals' not in resp:
        return {}
    result = {}
    for goal in resp['goals']:
        col = GOAL_NAME_MAP.get(goal.get('name', ''))
        if col:
            result[col] = goal['id']
    return result


def _grant_view_access(counter_id: int, target_login: str) -> bool:
    """Перебирает донорские токены, пытается выдать view-доступ target_login.
    True если grant выдан или уже существует у одного из доноров."""
    payload = {
        'grant': {
            'user_login': target_login,
            'perm':       'view',
            'comment':    f'auto-grant: analytics pipeline → {target_login}',
        }
    }
    for donor_login, token in GRANT_DONOR_TOKENS:
        try:
            r = requests.post(
                f'{METRIKA_BASE}/management/v1/counter/{counter_id}/grants',
                json=payload,
                headers={'Authorization': f'OAuth {token}'},
                timeout=30,
            )
            if r.status_code in (200, 201):
                logger.info('  grant выдан: %d → %s ← %s', counter_id, target_login, donor_login)
                return True
            if r.status_code == 409:
                logger.info('  grant уже существует: %d → %s ← %s', counter_id, target_login, donor_login)
                return True
            # API иногда возвращает 400 "Permission has already been issued" вместо 409
            if r.status_code == 400 and 'already been issued' in r.text:
                logger.info('  grant уже существует (400): %d → %s ← %s', counter_id, target_login, donor_login)
                return True
            # 403/404 — донор не владелец или нет счётчика, пробуем следующий
        except Exception as e:
            logger.warning('  grant ошибка %d → %s через %s: %s', counter_id, target_login, donor_login, e)
        time.sleep(0.3)
    return False


def _normalize_domain(raw) -> str:
    s = (raw or '').lower().strip()
    for prefix in ('https://', 'http://'):
        if s.startswith(prefix):
            s = s[len(prefix):]
    return s.replace('www.', '').strip('/')


def _find_counter(domain: str, counters: list) -> dict | None:
    clean = _normalize_domain(domain)
    if not clean:
        return None
    for c in counters:
        site = _normalize_domain(c.get('site', ''))
        if site and (clean in site or site in clean):
            return c
    return None


def _load_counters_with_token(token: str) -> list:
    """Загружает все счётчики Метрики с произвольным OAuth-токеном (постранично)."""
    all_counters: list = []
    seen_ids: set = set()
    page = 1
    headers = {'Authorization': f'OAuth {token}'}
    while True:
        try:
            r = requests.get(
                f'{METRIKA_BASE}/management/v1/counters',
                params={'per_page': 1000, 'page': page},
                headers=headers,
                timeout=30,
            )
            if r.status_code == 429:
                wait = int(r.headers.get('Retry-After', 60))
                logger.warning('[metrika_yandex] Fallback token 429: ждём %ds...', wait)
                time.sleep(min(wait, 60))
                continue
            r.encoding = 'utf-8'
            resp = r.json()
        except Exception as e:
            logger.warning('[metrika_yandex] Fallback token ошибка запроса счётчиков: %s', e)
            break
        counters = resp.get('counters', [])
        if not counters:
            break
        for c in counters:
            cid = c.get('id')
            if cid and cid not in seen_ids:
                seen_ids.add(cid)
                all_counters.append(c)
        rows_total = resp.get('rows', 0)
        if page * 1000 >= rows_total:
            break
        page += 1
        time.sleep(0.3)
    return all_counters


def _grant_view_with_token(counter_id: int, target_login: str, donor_login: str, token: str) -> bool:
    """Выдаёт grant view через конкретный токен донора. True если успешно или уже есть."""
    payload = {
        'grant': {
            'user_login': target_login,
            'perm':       'view',
            'comment':    f'auto-grant fallback: {donor_login} → {target_login}',
        }
    }
    try:
        r = requests.post(
            f'{METRIKA_BASE}/management/v1/counter/{counter_id}/grants',
            json=payload,
            headers={'Authorization': f'OAuth {token}'},
            timeout=30,
        )
        if r.status_code in (200, 201):
            logger.info('  [fallback] grant выдан: %d → %s ← %s', counter_id, target_login, donor_login)
            return True
        if r.status_code == 409:
            logger.info('  [fallback] grant уже существует: %d → %s ← %s', counter_id, target_login, donor_login)
            return True
        if r.status_code == 400 and 'already been issued' in r.text:
            logger.info('  [fallback] grant уже существует (400): %d → %s ← %s', counter_id, target_login, donor_login)
            return True
        logger.warning('  [fallback] grant не выдан %d → %s: HTTP %d %s', counter_id, target_login, r.status_code, r.text[:200])
    except Exception as e:
        logger.warning('  [fallback] grant ошибка %d → %s: %s', counter_id, target_login, e)
    return False


# ── БД ───────────────────────────────────────────────────────────────────────

def _ensure_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS public.{T_METRIKA_YANDEX} (
                domain              TEXT PRIMARY KEY,
                login_key           TEXT,
                counter_id          BIGINT,
                counter_name        TEXT,
                directologist       TEXT,
                all_forms           BIGINT,
                crm_order_created   BIGINT,
                crm_order_paid      BIGINT,
                crm_spam_order      BIGINT,
                crm_order_canceled  BIGINT,
                grant_done_lots1      BOOLEAN NOT NULL DEFAULT FALSE,
                grant_done_lots04     BOOLEAN NOT NULL DEFAULT FALSE,
                grant_done_skuderko1  BOOLEAN NOT NULL DEFAULT FALSE,
                updated_at            TIMESTAMP DEFAULT NOW()
            )
        """)
        for col, typedef in [
            ('directologist',         'TEXT'),
            ('all_forms',             'BIGINT'),
            ('crm_order_created',     'BIGINT'),
            ('crm_order_paid',        'BIGINT'),
            ('crm_spam_order',        'BIGINT'),
            ('crm_order_canceled',    'BIGINT'),
            ('grant_done_lots1',      'BOOLEAN NOT NULL DEFAULT FALSE'),
            ('grant_done_lots04',     'BOOLEAN NOT NULL DEFAULT FALSE'),
            ('grant_done_skuderko1',  'BOOLEAN NOT NULL DEFAULT FALSE'),
            ('updated_at',            'TIMESTAMP DEFAULT NOW()'),
            ('status',                'TEXT'),
        ]:
            cur.execute(f'ALTER TABLE public.{T_METRIKA_YANDEX} ADD COLUMN IF NOT EXISTS {col} {typedef}')

        # Миграция: legacy grant_done → grant_done_lots1 (если есть данные)
        cur.execute(f"""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM information_schema.columns
                           WHERE table_name = '{T_METRIKA_YANDEX}' AND column_name = 'grant_done')
                THEN
                    UPDATE public.{T_METRIKA_YANDEX}
                    SET grant_done_lots1 = grant_done
                    WHERE grant_done_lots1 = TRUE AND grant_done_lots1 = FALSE;
                    ALTER TABLE public.{T_METRIKA_YANDEX} DROP COLUMN grant_done;
                END IF;
            END$$;
        """)
    conn.commit()


def _upsert_domains(conn) -> int:
    """Фаза 1: UPSERT доменов из local_gsheet_sites (domain + login_key + directologist + status).
    Фильтр: только строки где directologist совпадает с name из public.specialists.
    После UPSERT синхронизирует status для ВСЕХ строк metrika_yandex (включая Удален/Стоп).
    """
    with conn.cursor() as cur:
        # Удаляем строки metrika_yandex у которых directologist пустой или не входит в specialists
        cur.execute(f"""
            DELETE FROM public.{T_METRIKA_YANDEX}
            WHERE (directologist IS NULL
               OR directologist = ''
               OR directologist NOT IN (SELECT name FROM public.specialists))
        """)
        deleted = cur.rowcount
        if deleted:
            logger.info('[metrika_yandex] Фаза 1: удалено %d строк с директологами не из specialists', deleted)

        cur.execute(f"""
            SELECT domain, login_key, directologist, status
            FROM {T_GSHEET_SITES}
            WHERE domain IS NOT NULL AND TRIM(domain) != ''
              AND login_key IS NOT NULL AND TRIM(login_key) != ''
              AND login_key != 'Нет'
              AND login_key !~ '^-+$'
              AND directologist IS NOT NULL AND directologist != ''
              AND directologist IN (SELECT name FROM public.specialists)
        """)
        domains = cur.fetchall()
    if not domains:
        return 0
    T = T_METRIKA_YANDEX
    with conn.cursor() as cur:
        execute_values(cur, f"""
            INSERT INTO public.{T} (domain, login_key, directologist, status)
            VALUES %s
            ON CONFLICT (domain) DO UPDATE SET
                login_key     = EXCLUDED.login_key,
                directologist = EXCLUDED.directologist,
                status        = EXCLUDED.status
        """, domains)

    # Дополнительная синхронизация: обновить status для строк которые не попали в UPSERT выше
    # (домены у которых login_key='Нет' или нет директолога — но они в metrika_yandex из-за истории)
    with conn.cursor() as cur:
        cur.execute(f"""
            UPDATE public.{T_METRIKA_YANDEX} m
            SET status = gs.status
            FROM {T_GSHEET_SITES} gs
            WHERE LOWER(TRIM(m.domain)) = LOWER(TRIM(gs.domain))
              AND m.status IS DISTINCT FROM gs.status
        """)
        extra_updated = cur.rowcount
        if extra_updated:
            logger.info('[metrika_yandex] Фаза 1: дополнительно синхронизировано status: %d строк', extra_updated)

    conn.commit()
    return len(domains)


def _fill_counter_ids(conn, counters: list) -> int:
    """Фаза 2: проставляет counter_id для строк где он IS NULL."""
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT domain FROM public.{T_METRIKA_YANDEX}
            WHERE counter_id IS NULL
        """)
        domains_without = [row[0] for row in cur.fetchall()]

    if not domains_without:
        logger.info('[metrika_yandex] Фаза 2: все строки уже имеют counter_id')
        return 0

    logger.info('[metrika_yandex] Фаза 2: ищем counter_id для %d доменов...', len(domains_without))
    updates = []
    for domain in domains_without:
        c = _find_counter(domain, counters)
        if c:
            updates.append((c['id'], c.get('name', ''), domain))

    if updates:
        with conn.cursor() as cur:
            cur.executemany(f"""
                UPDATE public.{T_METRIKA_YANDEX}
                SET counter_id = %s, counter_name = %s, updated_at = NOW()
                WHERE domain = %s
            """, updates)
        conn.commit()

    logger.info('[metrika_yandex] Фаза 2: обновлено %d/%d доменов', len(updates), len(domains_without))
    return len(updates)


def _fill_counter_ids_fallback(conn) -> int:
    """Фаза 2.5 (fallback): для доменов где counter_id всё ещё NULL —
    загружает счётчики через токен victoryagency-direct1618440,
    выдаёт grant view victorylotsofads1 и записывает counter_id.
    """
    _DONOR_LOGIN = 'victoryagency-direct1618440'
    _DONOR_TOKEN = OAUTH_TOKEN_2
    _TARGET_LOGIN = 'victorylotsofads1'
    _LOTS1_FLAG = 'grant_done_lots1'

    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT domain FROM public.{T_METRIKA_YANDEX}
            WHERE counter_id IS NULL
        """)
        domains_without = [row[0] for row in cur.fetchall()]

    if not domains_without:
        logger.info('[metrika_yandex] Фаза 2.5: counter_id IS NULL нет, fallback не нужен')
        return 0

    logger.info('[metrika_yandex] Фаза 2.5: загружаем счётчики через %s (%d доменов без counter_id)...',
                _DONOR_LOGIN, len(domains_without))
    fallback_counters = _load_counters_with_token(_DONOR_TOKEN)
    logger.info('[metrika_yandex] Фаза 2.5: счётчиков в API %s: %d', _DONOR_LOGIN, len(fallback_counters))

    if not fallback_counters:
        logger.warning('[metrika_yandex] Фаза 2.5: счётчики не получены, пропускаем')
        return 0

    found = 0
    granted = 0
    saved = 0
    for domain in domains_without:
        c = _find_counter(domain, fallback_counters)
        if not c:
            continue
        found += 1
        counter_id = c['id']
        counter_name = c.get('name', '')

        # Выдаём grant view victorylotsofads1 через токен victoryagency-direct1618440
        ok = _grant_view_with_token(counter_id, _TARGET_LOGIN, _DONOR_LOGIN, _DONOR_TOKEN)
        if ok:
            granted += 1
        else:
            logger.warning('[metrika_yandex] Фаза 2.5: не удалось выдать grant для counter_id=%d domain=%s, '
                           'всё равно записываем counter_id', counter_id, domain)

        # Записываем counter_id (и grant_done_lots1 если grant удался)
        with conn.cursor() as cur:
            cur.execute(f"""
                UPDATE public.{T_METRIKA_YANDEX}
                SET counter_id   = %s,
                    counter_name = %s,
                    {_LOTS1_FLAG} = %s,
                    updated_at   = NOW()
                WHERE domain = %s
            """, (counter_id, counter_name, ok, domain))
        conn.commit()
        saved += 1
        time.sleep(0.3)

    logger.info('[metrika_yandex] Фаза 2.5: найдено=%d, grant выдан=%d, записано counter_id=%d / %d доменов',
                found, granted, saved, len(domains_without))
    return saved


def _fill_goals(conn) -> int:
    """Фаза 3: заполняет goal_id для строк где counter_id есть, но цели IS NULL."""
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT DISTINCT counter_id FROM public.{T_METRIKA_YANDEX}
            WHERE counter_id IS NOT NULL
              AND (all_forms IS NULL
                OR crm_order_created IS NULL
                OR crm_order_paid IS NULL
                OR crm_spam_order IS NULL
                OR crm_order_canceled IS NULL)
        """)
        counter_ids = [row[0] for row in cur.fetchall()]

    if not counter_ids:
        logger.info('[metrika_yandex] Фаза 3: все цели уже заполнены')
        return 0

    # Фаза 3 работает с основным токеном (victorylotsofads1).
    # Подгружаем set счётчиков с уже выданным grant для lots1 — чтобы не дёргать API повторно.
    _LOTS1_LOGIN = 'victorylotsofads1'
    _LOTS1_FLAG  = 'grant_done_lots1'
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT DISTINCT counter_id FROM public.{T_METRIKA_YANDEX}
            WHERE counter_id IS NOT NULL AND {_LOTS1_FLAG} = TRUE
        """)
        granted_cids = {row[0] for row in cur.fetchall()}

    logger.info('[metrika_yandex] Фаза 3: загружаем цели для %d счётчиков (grant уже выдан: %d)...',
                len(counter_ids), len(granted_cids))
    updated = 0
    no_all_forms = []
    for i, cid in enumerate(counter_ids):
        g = load_goals(cid)
        if not g:
            if cid in granted_cids:
                logger.warning('[metrika_yandex] Фаза 3: счётчик %d — grant уже выдан, цели недоступны (skip)', cid)
                time.sleep(0.2)
                continue
            logger.warning('[metrika_yandex] Фаза 3: счётчик %d — нет доступа, пробуем grant перебором доноров...', cid)
            if _grant_view_access(cid, target_login=_LOTS1_LOGIN):
                # фиксируем grant_done_lots1=TRUE для всех строк с этим counter_id
                with conn.cursor() as cur:
                    cur.execute(f"""
                        UPDATE public.{T_METRIKA_YANDEX}
                        SET {_LOTS1_FLAG} = TRUE, updated_at = NOW()
                        WHERE counter_id = %s
                    """, (cid,))
                conn.commit()
                granted_cids.add(cid)
                time.sleep(1)  # дать API применить grant
                g = load_goals(cid)
            if not g:
                logger.warning('[metrika_yandex] Фаза 3: счётчик %d — цели не получены даже после grant', cid)
                time.sleep(0.2)
                continue
            logger.info('[metrika_yandex] Фаза 3: счётчик %d — цели получены после grant', cid)
        if 'all_forms' not in g:
            no_all_forms.append(cid)
        with conn.cursor() as cur:
            cur.execute(f"""
                UPDATE public.{T_METRIKA_YANDEX} SET
                    all_forms          = COALESCE(%s, all_forms),
                    crm_order_created  = COALESCE(%s, crm_order_created),
                    crm_order_paid     = COALESCE(%s, crm_order_paid),
                    crm_spam_order     = COALESCE(%s, crm_spam_order),
                    crm_order_canceled = COALESCE(%s, crm_order_canceled),
                    updated_at         = NOW()
                WHERE counter_id = %s
            """, (
                g.get('all_forms'),
                g.get('crm_order_created'),
                g.get('crm_order_paid'),
                g.get('crm_spam_order'),
                g.get('crm_order_canceled'),
                cid,
            ))
            updated += cur.rowcount
        conn.commit()
        if (i + 1) % 50 == 0:
            logger.info('[metrika_yandex]   ...%d/%d счётчиков', i + 1, len(counter_ids))
        time.sleep(0.2)

    if no_all_forms:
        logger.warning('[metrika_yandex] Фаза 3: all_forms не найдена у %d счётчиков: %s',
                       len(no_all_forms), no_all_forms)
    return updated


def _grant_all_targets(conn) -> None:
    """Фаза 4: grant view для всех аккаунтов из GRANT_TARGETS на все счётчики
    где соответствующий флаг = FALSE.

    Round-robin требует одинакового доступа у всех трёх аккаунтов — фильтр по tp не применяется.
    Каждый target обрабатывается независимо: если флаг уже TRUE — счётчик пропускается.
    """
    for target_login, flag_col in GRANT_TARGETS:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT DISTINCT counter_id
                FROM public.{T_METRIKA_YANDEX}
                WHERE counter_id IS NOT NULL
                  AND {flag_col} = FALSE
            """)
            candidates = [row[0] for row in cur.fetchall()]

        if not candidates:
            logger.info('[metrika_yandex] Фаза 4 [%s]: все счётчики уже имеют grant', target_login)
            continue

        logger.info('[metrika_yandex] Фаза 4 [%s]: grant view на %d счётчиков...',
                    target_login, len(candidates))
        granted = 0
        failed = []
        for i, cid in enumerate(candidates):
            if _grant_view_access(cid, target_login=target_login):
                with conn.cursor() as cur:
                    cur.execute(f"""
                        UPDATE public.{T_METRIKA_YANDEX}
                        SET {flag_col} = TRUE, updated_at = NOW()
                        WHERE counter_id = %s
                    """, (cid,))
                conn.commit()
                granted += 1
            else:
                failed.append(cid)
            if (i + 1) % 30 == 0:
                logger.info('[metrika_yandex]   [%s] ...%d/%d (success=%d, failed=%d)',
                            target_login, i + 1, len(candidates), granted, len(failed))
            time.sleep(0.2)

        logger.info('[metrika_yandex] Фаза 4 [%s]: grant выдан %d/%d, не удалось %d',
                    target_login, granted, len(candidates), len(failed))
        if failed:
            logger.warning('[metrika_yandex] Фаза 4 [%s]: счётчики без grant (%d): %s',
                           target_login, len(failed), failed[:20])


# ── Основная логика ───────────────────────────────────────────────────────────

def run_sync() -> None:
    logger.info('[metrika_yandex] Старт синхронизации')
    conn = psycopg2.connect(**DB_DST)
    try:
        _ensure_table(conn)

        # Фаза 1: синхронизируем домены
        n = _upsert_domains(conn)
        logger.info('[metrika_yandex] Фаза 1: %d доменов в таблице', n)

        # Фаза 2: проставляем counter_id где нет
        logger.info('[metrika_yandex] Загружаем список счётчиков Метрики...')
        counters = load_metrika_counters()
        logger.info('[metrika_yandex] Счётчиков в API: %d', len(counters))
        _fill_counter_ids(conn, counters)

        # Фаза 2.5: fallback через victoryagency-direct1618440 для доменов где counter_id всё ещё NULL
        try:
            _fill_counter_ids_fallback(conn)
        except Exception as e:
            logger.error('[metrika_yandex] Фаза 2.5 (fallback counter_id): %s', e, exc_info=True)

        # Фаза 3: заполняем ID целей где нет
        rows_updated = _fill_goals(conn)
        logger.info('[metrika_yandex] Фаза 3: обновлено %d строк с целями', rows_updated)

        # Фаза 4: grant view для всех трёх аккаунтов (round-robin)
        try:
            _grant_all_targets(conn)
        except Exception as e:
            logger.error('[metrika_yandex] Фаза 4 (grant all targets): %s', e, exc_info=True)

        logger.info('[metrika_yandex] Синхронизация завершена')

    except Exception as e:
        logger.error('[metrika_yandex] Ошибка: %s', e, exc_info=True)
    finally:
        conn.close()


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
    )
    run_sync()
