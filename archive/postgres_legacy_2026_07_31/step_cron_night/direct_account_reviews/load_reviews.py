"""
direct_account_reviews/load_reviews.py

Читает лист "Power BI" A:E из Google Sheets.
Строка 1 — заголовки: город / салон / аккаунт / сайт / агентский аккаунт
Строки 2+ — данные (по одной строке на аккаунт).

Загружает в ad_analytics_bi.public.yandex_direct_account_reviews.

Запуск:
    python load_reviews.py
"""

import sys
import psycopg2
from psycopg2.extras import execute_values
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from config.settings import DB_DST

# ── Настройки ─────────────────────────────────────────────────────────────────

SPREADSHEET_ID = '1ec_qL-1zSfXA-cftEcFjrMJmM9yI78qVVpTxICGCQFU'
SHEET_NAME     = 'Power BI'
DATA_RANGE     = f'{SHEET_NAME}!A:E'

def _find_sa():
    """Walk-up до <repo>/.secret/service_account.json (на mac .secret лежит выше по дереву)."""
    p = os.path.abspath(__file__)
    for _ in range(6):
        p = os.path.dirname(p)
        c = os.path.join(p, '.secret', 'service_account.json')
        if os.path.exists(c):
            return c
    return None

_SA_CANDIDATES = [c for c in [
    _find_sa(),                                                # mac: <repo>/.secret/service_account.json
    os.path.expanduser('~/.secret/service_account.json'),     # Victory: ~/.secret
    os.path.expanduser('~/.secret/google/service_account.json'),
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 'config', 'cedar-gearbox-464117-e5-676d6cc8937e.json'),  # Victory: config/
    os.path.expanduser('~/cedar-gearbox-464117-e5-676d6cc8937e.json'),
] if c]
GOOGLE_SA_FILE = next((f for f in _SA_CANDIDATES if os.path.exists(f)), None)
if not GOOGLE_SA_FILE:
    raise FileNotFoundError('Google SA-ключ не найден: .secret/service_account.json (mac) или config/cedar-*.json (Victory)')

TABLE = 'yandex_direct_account_reviews'


# ── Google Sheets ──────────────────────────────────────────────────────────────

def _sheets_service():
    creds = Credentials.from_service_account_file(
        GOOGLE_SA_FILE,
        scopes=['https://www.googleapis.com/auth/spreadsheets.readonly'],
    )
    return build('sheets', 'v4', credentials=creds, cache_discovery=False)


def _read_raw():
    svc  = _sheets_service()
    resp = svc.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=DATA_RANGE,
        valueRenderOption='FORMATTED_VALUE',
    ).execute()
    return resp.get('values', [])


# ── Парсинг ────────────────────────────────────────────────────────────────────

def _cell(row, idx):
    return row[idx].strip() if idx < len(row) else ''


def parse_records(raw_rows):
    """
    Ожидает формат:
      строка 0: заголовки (город / салон / аккаунт / сайт / агентский аккаунт)
      строки 1+: данные
    """
    if not raw_rows:
        return []

    records = []
    for row in raw_rows[1:]:   # пропускаем заголовок
        gorod    = _cell(row, 0)
        salon    = _cell(row, 1)
        account  = _cell(row, 2)
        site     = _cell(row, 3)
        agency   = _cell(row, 4)

        if not account:
            continue

        records.append({
            'город':              gorod,
            'салон':              salon,
            'аккаунт':            account,
            'сайт':               site,
            'агентский аккаунт':  agency,
        })

    return records


# ── PostgreSQL ─────────────────────────────────────────────────────────────────

CREATE_SQL = f"""
CREATE TABLE IF NOT EXISTS public.{TABLE} (
    id                   SERIAL PRIMARY KEY,
    город                TEXT,
    салон                TEXT,
    аккаунт              TEXT,
    сайт                 TEXT,
    "агентский аккаунт"  TEXT
);
"""

ADD_AGENCY_COL_SQL = f"""
ALTER TABLE public.{TABLE}
ADD COLUMN IF NOT EXISTS "агентский аккаунт" TEXT;
"""

INSERT_SQL = f"""
INSERT INTO public.{TABLE} (город, салон, аккаунт, сайт, "агентский аккаунт")
VALUES %s
"""


def load_to_db(records):
    conn = psycopg2.connect(**DB_DST)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(CREATE_SQL)
            cur.execute(ADD_AGENCY_COL_SQL)
            cur.execute(f'TRUNCATE TABLE public.{TABLE};')
            rows = [(r['город'], r['салон'], r['аккаунт'], r['сайт'], r['агентский аккаунт'])
                    for r in records]
            execute_values(cur, INSERT_SQL, rows)
        conn.commit()
        print(f'OK Загружено {len(records)} записей в {TABLE}')
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print('Читаем Google Sheets...')
    raw = _read_raw()
    print(f'  Получено строк: {len(raw)}')
    records = parse_records(raw)
    print(f'  Распарсено записей: {len(records)}')
    if not records:
        print('  WARN: Записи не найдены')
        sys.exit(1)
    load_to_db(records)


if __name__ == '__main__':
    main()
