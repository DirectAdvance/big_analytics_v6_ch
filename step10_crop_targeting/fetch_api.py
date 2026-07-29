"""
crop_targeting/fetch_api.py — загрузка посевов из Telega.in API
в таблицу public.crop_targeting_api_telegain (ad_analytics_bi).

Фильтры:
  - status = 'complete'
  - end_time >= 2026-05-01 (размещение закончилось в периоде или позже)

Записи без utm_content или с невалидным utm_content — сохраняются (аудит).
UTM парсится здесь (разрешение редиректов tglink.io — дорогое, делаем раз при fetch).

total_cost НЕ считается здесь — вычисляется в load_api_leads.py.

Запуск (из папки big analytics_v5/):
  python3 crop_targeting/fetch_api.py
"""

import json
import logging
import os
import re
import ssl
import sys
import time
from datetime import date, datetime
from urllib.parse import parse_qs, urlparse

import psycopg2
import requests
import urllib3
from psycopg2.extras import execute_values
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import DB_DST
from pathlib import Path as _Path
for _p in _Path(__file__).resolve().parents:
    if (_p / '.secret' / 'loader.py').exists():
        sys.path.insert(0, str(_p / '.secret'))
        break
from loader import load_telega  # noqa: E402

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_ssl_ctx = create_urllib3_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE
_ssl_ctx.set_ciphers('DEFAULT@SECLEVEL=1')
_ssl_ctx.options |= ssl.OP_NO_SSLv2 | ssl.OP_NO_SSLv3


class _LaxAdapter(HTTPAdapter):
    def send(self, request, **kwargs):
        kwargs['verify'] = False
        return super().send(request, **kwargs)


_SESSION = requests.Session()
_SESSION.mount('https://', _LaxAdapter())

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
)
logger = logging.getLogger('fetch_api_telegain')

_TG = load_telega()
API_BASE      = _TG['api_base']
PARTNER_KEY   = _TG['partner_key']
API_KEY       = _TG['api_key']
API_DATE_FROM = date(2026, 5, 1)

TABLE = 'crop_targeting_api_telegain'

_HEADERS = {
    'Accept': 'application/json',
    'Partner-Authorization': PARTNER_KEY,
    'Authorization': API_KEY,
}

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS public.crop_targeting_api_telegain (
    id                   INTEGER PRIMARY KEY,
    uid                  TEXT,
    channel_id           INTEGER,
    post_link            TEXT,
    customer_id          INTEGER,
    channel_name         TEXT,
    channel_link         TEXT,
    placement_format     TEXT,
    status               TEXT,
    cancel_comment       TEXT,
    user_price           NUMERIC,
    user_price_currency  TEXT,
    order_id             INTEGER,
    order_text           TEXT,
    order_links          TEXT,
    statistic            JSONB,
    created_at           TIMESTAMPTZ,
    completed_at         TIMESTAMPTZ,
    done_at              TIMESTAMPTZ,
    run_at               TIMESTAMPTZ,
    start_time           TIMESTAMPTZ,
    end_time             TIMESTAMPTZ,
    can_compaint         BOOLEAN,
    can_cancel_compaint  BOOLEAN,
    can_cancel           BOOLEAN,
    order_project_name   TEXT,
    order_comment        TEXT,
    order_status         TEXT,
    utm_source           TEXT,
    utm_medium           TEXT,
    utm_campaign         TEXT,
    utm_content          TEXT,
    utm_term             TEXT,
    domain               TEXT,
    fetched_at           TIMESTAMPTZ DEFAULT NOW()
)
"""

_UPSERT_SQL = """
INSERT INTO public.crop_targeting_api_telegain (
    id, uid, channel_id, post_link, customer_id,
    channel_name, channel_link, placement_format, status, cancel_comment,
    user_price, user_price_currency, order_id, order_text,
    order_links, statistic,
    created_at, completed_at, done_at, run_at, start_time, end_time,
    can_compaint, can_cancel_compaint, can_cancel,
    order_project_name, order_comment, order_status,
    utm_source, utm_medium, utm_campaign, utm_content, utm_term,
    domain
) VALUES %s
ON CONFLICT (id) DO UPDATE SET
    status             = EXCLUDED.status,
    cancel_comment     = EXCLUDED.cancel_comment,
    user_price         = EXCLUDED.user_price,
    order_text         = EXCLUDED.order_text,
    order_links        = EXCLUDED.order_links,
    statistic          = EXCLUDED.statistic,
    completed_at       = EXCLUDED.completed_at,
    done_at            = EXCLUDED.done_at,
    run_at             = EXCLUDED.run_at,
    utm_source         = EXCLUDED.utm_source,
    utm_medium         = EXCLUDED.utm_medium,
    utm_campaign       = EXCLUDED.utm_campaign,
    utm_content        = EXCLUDED.utm_content,
    utm_term           = EXCLUDED.utm_term,
    domain             = EXCLUDED.domain,
    order_project_name = EXCLUDED.order_project_name,
    order_comment      = EXCLUDED.order_comment,
    order_status       = EXCLUDED.order_status,
    fetched_at         = NOW()
"""


# ── API ───────────────────────────────────────────────────────────────────────

def _get(path: str, params: dict = None, attempt: int = 0):
    try:
        r = _SESSION.get(f'{API_BASE}{path}', headers=_HEADERS,
                         params=params or {}, timeout=90)
        if r.status_code == 429:
            wait = int(r.headers.get('Retry-After', 60))
            logger.warning('[429] ждём %ds...', wait)
            time.sleep(min(wait, 120))
            if attempt < 5:
                return _get(path, params, attempt + 1)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        if attempt < 4:
            wait = min(10 * (attempt + 1), 60)
            logger.warning('Ошибка (попытка %d/4), повтор через %ds: %s', attempt + 1, wait, e)
            time.sleep(wait)
            return _get(path, params, attempt + 1)
        logger.error('API error %s: %s', path, e)
        return None


def fetch_orders() -> dict:
    resp = _get('/api/public/v1/order/index')
    if not resp:
        return {}
    return {
        o['id']: {
            'project_name': o.get('project_name'),
            'comment':      o.get('comment'),
            'status':       o.get('status'),
        }
        for o in resp.get('orders', [])
    }


# ── UTM парсинг ───────────────────────────────────────────────────────────────

_redirect_cache: dict[str, str] = {}


def _resolve_redirect(url: str) -> str:
    if url in _redirect_cache:
        return _redirect_cache[url]
    try:
        r = _SESSION.head(url, allow_redirects=True, timeout=10)
        resolved = r.url
    except Exception:
        try:
            r = _SESSION.get(url, allow_redirects=True, timeout=10, stream=True)
            resolved = r.url
            r.close()
        except Exception:
            resolved = url
    _redirect_cache[url] = resolved
    return resolved


def _first_resolved_link(links: list):
    if not links:
        return None
    url = links[0]
    if 'tglink.io' in url:
        url = _resolve_redirect(url)
    return url


def _extract_domain(url):
    if not url:
        return None
    try:
        return urlparse(url).netloc or None
    except Exception:
        return None


def _parse_utm(url: str) -> dict:
    try:
        qs = parse_qs(urlparse(url).query)
        return {k: qs.get(k, [None])[0]
                for k in ('utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term')}
    except Exception:
        return {}


def _extract_utm_from_oc(oc: dict) -> dict:
    """Парсит UTM из ссылок поста: statistic.all_links, затем order_links."""
    stat = oc.get('statistic') or {}
    post_links = stat.get('all_links') or []
    if not post_links:
        post_links = oc.get('order_links') or []
    for link in post_links:
        resolved = _resolve_redirect(link) if 'tglink.io' in link else link
        u = _parse_utm(resolved)
        if any(u.values()):
            return u
    return {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ts(val):
    if not val:
        return None
    try:
        return datetime.fromisoformat(val)
    except Exception:
        return None


def _price(d):
    if not d:
        return None
    try:
        return float(d['value']) if d.get('value') is not None else None
    except (ValueError, TypeError, KeyError):
        return None


# ── БД ────────────────────────────────────────────────────────────────────────

def ensure_table(conn) -> None:
    with conn.cursor() as cur:
        # uid — маркер новой схемы; при отсутствии пересоздаём таблицу
        cur.execute("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s AND column_name = 'uid'
        """, (TABLE,))
        if cur.fetchone()[0] == 0:
            logger.info('Миграция: пересоздаём %s с новой схемой...', TABLE)
            cur.execute(f'DROP TABLE IF EXISTS public.{TABLE}')
        cur.execute(_CREATE_SQL)
        # order_links JSONB → TEXT (первая ссылка после редиректа)
        cur.execute("""
            SELECT data_type FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s AND column_name = 'order_links'
        """, (TABLE,))
        row = cur.fetchone()
        if row and row[0] == 'jsonb':
            logger.info('Миграция: order_links JSONB → TEXT, старые значения сбросятся...')
            cur.execute(f'ALTER TABLE public.{TABLE} ALTER COLUMN order_links TYPE TEXT USING NULL')
        # domen → domain
        cur.execute("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s AND column_name = 'domen'
        """, (TABLE,))
        if cur.fetchone()[0] > 0:
            logger.info('Миграция: переименовываем domen → domain...')
            cur.execute(f'ALTER TABLE public.{TABLE} RENAME COLUMN domen TO domain')
        # ADD COLUMN domain если нет ни domain ни domen
        cur.execute("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s AND column_name = 'domain'
        """, (TABLE,))
        if cur.fetchone()[0] == 0:
            cur.execute(f'ALTER TABLE public.{TABLE} ADD COLUMN domain TEXT')
    conn.commit()


def _build_rows(items: list, orders_map: dict) -> tuple[list[tuple], bool]:
    """Возвращает (rows, all_before_cutoff).
    all_before_cutoff=True: ни одна запись на странице не имеет end_time >= API_DATE_FROM
    → API отсортирован по убыванию даты, последующие страницы можно не читать.
    """
    rows = []
    skipped_status = 0
    skipped_date   = 0
    any_in_range   = False  # есть ли на странице хоть одна запись с end_time >= API_DATE_FROM

    for oc in items:
        created_at_dt = _ts(oc.get('created_at'))
        end_time_dt = _ts(oc.get('end_time'))

        if end_time_dt and end_time_dt.date() >= API_DATE_FROM:
            any_in_range = True

        if oc.get('status') != 'complete':
            skipped_status += 1
            continue

        if not end_time_dt or end_time_dt.date() < API_DATE_FROM:
            skipped_date += 1
            continue

        utm        = _extract_utm_from_oc(oc)
        order      = orders_map.get(oc.get('order_id'), {})
        price_dict = oc.get('user_price') or {}
        resolved   = _first_resolved_link(oc.get('order_links') or [])

        rows.append((
            oc['id'],
            oc.get('uid'),
            oc.get('channel_id'),
            oc.get('post_link'),
            oc.get('customer_id'),
            oc.get('channel_name'),
            oc.get('channel_link'),
            oc.get('placement_format'),
            oc.get('status'),
            oc.get('cancel_comment'),
            _price(price_dict),
            price_dict.get('currency'),
            oc.get('order_id'),
            oc.get('order_text'),
            resolved,
            json.dumps(oc.get('statistic') or {}, ensure_ascii=False),
            created_at_dt,
            _ts(oc.get('completed_at')),
            _ts(oc.get('done_at')),
            _ts(oc.get('run_at')),
            _ts(oc.get('start_time')),
            _ts(oc.get('end_time')),
            oc.get('can_compaint'),
            oc.get('can_cancel_compaint'),
            oc.get('can_cancel'),
            order.get('project_name'),
            order.get('comment'),
            order.get('status'),
            utm.get('utm_source'),
            utm.get('utm_medium'),
            utm.get('utm_campaign'),
            utm.get('utm_content'),
            utm.get('utm_term'),
            _extract_domain(resolved),
        ))

    if skipped_status:
        logger.info('Пропущено (status != complete): %d', skipped_status)
    if skipped_date:
        logger.info('Пропущено (end_time < %s): %d', API_DATE_FROM, skipped_date)

    return rows, not any_in_range


def upsert(conn, rows: list) -> int:
    if not rows:
        return 0
    with conn.cursor() as cur:
        execute_values(cur, _UPSERT_SQL, rows)
    conn.commit()
    return len(rows)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    logger.info('=== fetch_api_telegain start ===')
    conn = psycopg2.connect(**DB_DST)
    try:
        ensure_table(conn)

        logger.info('Загружаем заказы...')
        orders_map = fetch_orders()
        logger.info('Заказов: %d', len(orders_map))

        total_saved = 0
        page        = 1
        total_count = None

        while True:
            resp = _get('/api/public/v1/order_channel/index',
                        {'page': page, 'per_page': 100})
            if resp is None:
                logger.error('Страница %d: ошибка API, прерываем.', page)
                break

            items = resp.get('order_channels', [])
            if not items:
                break

            if total_count is None:
                total_count = resp.get('count', 0)

            rows, all_before_cutoff = _build_rows(items, orders_map)
            n    = upsert(conn, rows)
            total_saved += n
            logger.info('  Страница %d: +%d строк | итого: %d', page, n, total_saved)

            if all_before_cutoff:
                logger.info('Вся страница старее %s — останавливаемся.', API_DATE_FROM)
                break
            if page * 100 >= total_count:
                break
            page += 1
            time.sleep(0.5)

        logger.info('crop_targeting Telega.in API: загружено %d строк (с 01.05.2026)', total_saved)
    finally:
        conn.close()
    logger.info('=== Готово ===')


if __name__ == '__main__':
    main()
