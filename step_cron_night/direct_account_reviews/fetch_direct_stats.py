"""
direct_account_reviews/fetch_direct_stats.py

Читает уникальные аккаунты из direct_account_reviews,
запрашивает статистику через Яндекс.Директ Reports API v5,
сохраняет в ad_analytics_bi.public.yandex_direct_reports_reviews.

Инкрементная стратегия (per-account):
  1. Таблица не существует       → полная загрузка с FULL_DATE_FROM (2026-01-01)
  2. Аккаунт в таблице не найден → полная загрузка с FULL_DATE_FROM
  3. Аккаунт есть в таблице      → date_from = MAX(Date) - SAFETY_DAYS
                                   DELETE WHERE login = ? AND Date >= date_from
                                   загрузить от date_from до сегодня

Токены: REVIEWS_TOKENS (4 агентских аккаунта) перебором.
Cost — с НДС (IncludeVAT=YES).

Запуск:
    python fetch_direct_stats.py
"""

import json
import sys
import os
from datetime import date, timedelta
from time import sleep

import psycopg2
import requests
from psycopg2.extras import execute_values

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from config.cookies import send_tg as _send_tg
from config.settings import DB_DST
from config.tokens import (
    REVIEWS_TOKENS,
    OAUTH_TOKEN_1, OAUTH_TOKEN_2, OAUTH_TOKEN_3, OAUTH_TOKEN_4, OAUTH_TOKEN_5,
)

# ── Настройки ─────────────────────────────────────────────────────────────────

# Все доступные токены: сначала REVIEWS_TOKENS, потом основные агентские
_MAIN_TOKENS = [
    (OAUTH_TOKEN_1, 'victorylotsofads1'),
    (OAUTH_TOKEN_2, 'victoryagency-direct1618440'),
    (OAUTH_TOKEN_3, 'y-direct-victory'),
    (OAUTH_TOKEN_4, 'victoryagency14'),
    (OAUTH_TOKEN_5, 'useful-call-agency'),
]
TOKENS = REVIEWS_TOKENS + [t for t in _MAIN_TOKENS if t not in REVIEWS_TOKENS]

REVIEWS_TABLE  = 'yandex_direct_account_reviews'
STATS_TABLE    = 'yandex_direct_reports_reviews'
FULL_DATE_FROM = '2026-01-01'    # старт при полной загрузке
SAFETY_DAYS    = 7               # перекрываем N дней назад при инкременте

DATE_TO        = date.today().strftime('%Y-%m-%d')

REPORTS_URL    = 'https://api.direct.yandex.com/json/v5/reports'
RETRY_TIMEOUT  = 20
MAX_ERRORS     = 5

FIELD_NAMES = [
    'Date', 'CampaignId', 'CampaignName', 'AdGroupId', 'AdGroupName',
    'AdNetworkType', 'Device', 'Impressions', 'Clicks', 'Cost', 'RlAdjustmentId',
]

CREATE_SQL = f"""
CREATE TABLE IF NOT EXISTS public.{STATS_TABLE} (
    id               SERIAL PRIMARY KEY,
    login            TEXT,
    "Date"           DATE,
    "CampaignId"     BIGINT,
    "CampaignName"   TEXT,
    "AdGroupId"      BIGINT,
    "AdGroupName"    TEXT,
    "AdNetworkType"  TEXT,
    "Device"         TEXT,
    "Impressions"    BIGINT,
    "Clicks"         BIGINT,
    "Cost"           NUMERIC(18,6),
    "RlAdjustmentId" BIGINT
);
CREATE INDEX IF NOT EXISTS idx_{STATS_TABLE}_login_date
    ON public.{STATS_TABLE} (login, "Date");
"""

INSERT_SQL = f"""
INSERT INTO public.{STATS_TABLE}
  (login, "Date", "CampaignId", "CampaignName", "AdGroupId", "AdGroupName",
   "AdNetworkType", "Device", "Impressions", "Clicks", "Cost", "RlAdjustmentId")
VALUES %s
"""


# ── Вспомогательные функции БД ────────────────────────────────────────────────

def _connect():
    return psycopg2.connect(**DB_DST)


def _table_exists(conn) -> bool:
    """Проверяет существование таблицы yandex_direct_reports_reviews."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name   = %s
            )
        """, (STATS_TABLE,))
        return cur.fetchone()[0]


def ensure_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(CREATE_SQL)
    conn.commit()


def ensure_agency_column(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f'ALTER TABLE public.{REVIEWS_TABLE} '
            f'ADD COLUMN IF NOT EXISTS "агентский аккаунт" TEXT'
        )
    conn.commit()


def get_logins(conn) -> list[tuple]:
    """Уникальные логины из direct_account_reviews."""
    with conn.cursor() as cur:
        cur.execute(
            f'SELECT аккаунт, MAX("агентский аккаунт") '
            f'FROM public.{REVIEWS_TABLE} '
            f"WHERE аккаунт IS NOT NULL AND аккаунт != '' "
            f'GROUP BY аккаунт ORDER BY аккаунт'
        )
        result = []
        for row in cur.fetchall():
            login = row[0]
            try:
                login.encode('latin-1')
            except (UnicodeEncodeError, UnicodeDecodeError):
                print(f'  SKIP login {login!r}: non-ASCII, пропускаем')
                continue
            result.append((login, row[1]))
        return result


def get_login_date_from(conn, login: str, table_exists: bool) -> str:
    """
    Определяет дату начала загрузки для конкретного аккаунта.

    Если таблицы нет или нет данных для этого login → FULL_DATE_FROM.
    Если данные есть → MAX(Date) - SAFETY_DAYS.
    """
    if not table_exists:
        return FULL_DATE_FROM

    with conn.cursor() as cur:
        cur.execute(
            f'SELECT MAX("Date") FROM public.{STATS_TABLE} WHERE login = %s',
            (login,)
        )
        max_date = cur.fetchone()[0]

    if max_date is None:
        return FULL_DATE_FROM

    return (max_date - timedelta(days=SAFETY_DAYS)).strftime('%Y-%m-%d')


def save_agency(conn, login: str, agency: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f'UPDATE public.{REVIEWS_TABLE} '
            f'SET "агентский аккаунт" = %s WHERE аккаунт = %s',
            (agency, login)
        )
    conn.commit()


def delete_and_insert(conn, login: str, date_from: str, rows: list) -> None:
    """
    Удаляет строки начиная с date_from для данного логина,
    затем вставляет новые данные.
    """
    with conn.cursor() as cur:
        cur.execute(
            f'DELETE FROM public.{STATS_TABLE} '
            f'WHERE login = %s AND "Date" >= %s',
            (login, date_from)
        )
        deleted = cur.rowcount
        if rows:
            execute_values(cur, INSERT_SQL, rows)
    conn.commit()
    return deleted


# ── Yandex Direct Reports API ─────────────────────────────────────────────────

def _headers(token: str, login: str) -> dict:
    return {
        'Authorization':       'Bearer ' + token,
        'Client-Login':        login,
        'Accept-Language':     'ru',
        'processingMode':      'auto',
        'returnMoneyInMicros': 'false',
        'skipReportHeader':    'true',
        'skipColumnHeader':    'false',
        'skipReportSummary':   'true',
    }


def _body(login: str, date_from: str, date_to: str) -> str:
    return json.dumps({
        'params': {
            'SelectionCriteria': {'DateFrom': date_from, 'DateTo': date_to},
            'FieldNames':        FIELD_NAMES,
            'ReportName':        f'reviews_{login}_{date_from}_{date_to}',
            'Page':              {'Limit': 10_000_000},
            'ReportType':        'CUSTOM_REPORT',
            'DateRangeType':     'CUSTOM_DATE',
            'Format':            'TSV',
            'IncludeVAT':        'YES',
            'IncludeDiscount':   'NO',
        }
    })


def fetch_report(token: str, login: str, date_from: str, date_to: str) -> str | None:
    """
    Возвращает TSV-строку при успехе, None при ошибке доступа (403/400).
    Бросает RuntimeError при критических ошибках.
    """
    headers   = _headers(token, login)
    body      = _body(login, date_from, date_to)
    err_count = 0
    while True:
        try:
            resp = requests.post(REPORTS_URL, body, headers=headers, timeout=120)
            resp.encoding = 'utf-8'
        except requests.RequestException as e:
            err_count += 1
            print(f'    Сетевая ошибка: {e}')
            if err_count >= MAX_ERRORS:
                raise RuntimeError(f'Сетевые ошибки > {MAX_ERRORS}: {e}')
            sleep(10)
            continue

        if resp.status_code == 200:
            return resp.text
        if resp.status_code in (400, 403, 404):
            print(f'    HTTP {resp.status_code}: {resp.text[:200]}')
            return None
        if resp.status_code == 201:
            print(f'    Очередь, ждём {RETRY_TIMEOUT}с...')
            sleep(RETRY_TIMEOUT)
            continue
        if resp.status_code == 202:
            wait = int(resp.headers.get('retryIn', RETRY_TIMEOUT))
            print(f'    Формируется, ждём {wait}с...')
            sleep(wait)
            continue
        err_count += 1
        print(f'    HTTP {resp.status_code}: {resp.text[:100]}')
        if err_count >= MAX_ERRORS:
            raise RuntimeError(f'Статус {resp.status_code} повторился {MAX_ERRORS} раз')
        sleep(5)


# ── Парсинг TSV ───────────────────────────────────────────────────────────────

def _to_int(v, default=None):
    try:
        return int(v)
    except (ValueError, TypeError):
        return default


def _to_float(v, default=0.0):
    try:
        return float((v or '').replace(',', '.'))
    except (ValueError, AttributeError):
        return default


def parse_tsv(tsv_text: str, login: str) -> list:
    rows  = []
    lines = tsv_text.strip().splitlines()
    if len(lines) < 2:
        return rows
    header = lines[0].split('\t')
    for line in lines[1:]:
        if not line.strip():
            continue
        parts = line.split('\t')
        d = dict(zip(header, parts))
        def v(field):
            val = d.get(field, '').strip()
            return None if val in ('', '--') else val
        rows.append((
            login,
            v('Date'),
            _to_int(v('CampaignId')),
            v('CampaignName'),
            _to_int(v('AdGroupId')),
            v('AdGroupName'),
            v('AdNetworkType'),
            v('Device'),
            _to_int(v('Impressions'), 0),
            _to_int(v('Clicks'), 0),
            _to_float(v('Cost') or '0'),
            _to_int(v('RlAdjustmentId')),
        ))
    return rows


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    conn = _connect()
    try:
        # Проверяем существование таблицы ДО её создания
        tbl_exists = _table_exists(conn)
        mode_label = 'инкрементный' if tbl_exists else 'полный (таблица не найдена)'
        print(f'Режим: {mode_label}')

        ensure_table(conn)
        ensure_agency_column(conn)

        logins = get_logins(conn)
    finally:
        conn.close()

    if not logins:
        print(f'Нет аккаунтов в {REVIEWS_TABLE}')
        return

    print(f'Аккаунтов: {len(logins)}\n')

    ok_count = skip_count = err_count = 0
    skip_logins = []

    for idx, (login, known_agency) in enumerate(logins, 1):
        # Определяем период загрузки для этого аккаунта
        conn = _connect()
        try:
            date_from = get_login_date_from(conn, login, tbl_exists)
        finally:
            conn.close()

        is_full = (date_from == FULL_DATE_FROM)
        print(f'[{idx}/{len(logins)}] {login}  '
              f'{"ПОЛНАЯ" if is_full else f"с {date_from}"} → {DATE_TO}')

        # Подбираем токен (кэшированный агентский аккаунт первым)
        tokens_ordered = sorted(TOKENS, key=lambda t: (t[1] != known_agency))

        tsv_text    = None
        agency_used = None
        for token, agency in tokens_ordered:
            label = agency + (' [кэш]' if agency == known_agency else '')
            print(f'  Токен {label}...')
            try:
                result = fetch_report(token, login, date_from, DATE_TO)
            except RuntimeError as e:
                print(f'  ОШИБКА: {e}')
                continue
            if result is not None:
                tsv_text    = result
                agency_used = agency
                break
            print(f'  Токен {agency} отклонён')

        if tsv_text is None:
            print(f'  SKIP: нет доступа к {login}')
            skip_count += 1
            skip_logins.append(login)
            continue

        # Сохраняем агентский аккаунт если изменился
        if agency_used != known_agency:
            conn = _connect()
            try:
                save_agency(conn, login, agency_used)
            finally:
                conn.close()

        rows = parse_tsv(tsv_text, login)
        print(f'  Строк в ответе: {len(rows)}')

        conn = _connect()
        try:
            deleted = delete_and_insert(conn, login, date_from, rows)
        except Exception as e:
            print(f'  ОШИБКА записи: {e}')
            err_count += 1
            conn.close()
            continue
        finally:
            conn.close()

        print(f'  OK (удалено: {deleted}, записано: {len(rows)})')
        ok_count += 1

    print(f'\nИтого: OK={ok_count}  пропущено={skip_count}  ошибок={err_count}')

    _KNOWN_NO_TOKEN = {
        'porg-26u7d4o2', 'porg-3xrlykv5', 'porg-4cd6tcsg',
        'porg-ej4tydh7', 'porg-ew54weam', 'porg-gt2if6bv', 'porg-hihccjx7',
    }
    unexpected_skips = [l for l in skip_logins if l not in _KNOWN_NO_TOKEN]
    if unexpected_skips:
        lines = '\n'.join(unexpected_skips[:50])
        tail  = f'\n...и ещё {len(unexpected_skips) - 50}' if len(unexpected_skips) > 50 else ''
        _send_tg(f'⚠️ direct_account_reviews: нет доступа по токенам для {len(unexpected_skips)} аккаунтов:\n{lines}{tail}')


if __name__ == '__main__':
    main()
