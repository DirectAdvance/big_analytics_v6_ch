"""
big analytics_v5/crop_targeting/load_crop_targeting.py

Читает лист «Лист1» Google Sheets (колонки A–W),
клонирует строки где колонка L содержит несколько значений (через \\n),
загружает в две таблицы:
  - gsheets_crop_targeting_account          — основная (без UTM, с фильтрацией столбцов)
  - gsheets_crop_targeting_account_pravilo_utm — только UTM + utm утвержденная

БД: ad_analytics_bi

Запуск (из папки big analytics_v5/):
  python3 crop_targeting/load_crop_targeting.py
"""

import os
import sys

import psycopg2
from psycopg2.extras import execute_values
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import DB_DST

# ─── Настройки ────────────────────────────────────────────────────────────────

SPREADSHEET_ID = '1RgYaXiCgiipV1ljWFsiVYVzDQJZv1-V1w9hFdegQ0lI'
SHEET_NAME     = 'Лист1'
DATA_RANGE     = f'{SHEET_NAME}!A1:W'

def _find_sa():
    """Walk-up до <repo>/.secret/service_account.json (на mac .secret лежит выше по дереву)."""
    p = os.path.abspath(__file__)
    for _ in range(6):
        p = os.path.dirname(p)
        c = os.path.join(p, '.secret', 'service_account.json')
        if os.path.exists(c):
            return c
    return None

_SA_FILE_CANDIDATES = [c for c in [
    _find_sa(),                                                # mac: <repo>/.secret/service_account.json
    os.path.expanduser('~/.secret/service_account.json'),     # Victory: ~/.secret
    os.path.expanduser('~/.secret/google/service_account.json'),
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 'config', 'cedar-gearbox-464117-e5-676d6cc8937e.json'),  # Victory: config/
    os.path.expanduser('~/cedar-gearbox-464117-e5-676d6cc8937e.json'),
] if c]
GOOGLE_SA_FILE = next((f for f in _SA_FILE_CANDIDATES if os.path.exists(f)), None)
if not GOOGLE_SA_FILE:
    raise FileNotFoundError('Google SA-ключ не найден: .secret/service_account.json (mac) или config/cedar-*.json (Victory)')

TABLE     = 'gsheets_crop_targeting_account'
UTM_TABLE = 'gsheets_crop_targeting_account_pravilo_utm'
UTM_COL_IDX = 11   # колонка L (0-based)

MAPPING_SHEET_RANGE = 'лист2!A:B'  # A=Канал, B=utm утвержденная

# Столбцы, исключаемые из основной таблицы
EXCLUDE_COLS = {'ооо', 'Салон', 'Ссылка на таблицу', 'таблицы', 'Подрядчик', 'Счет', 'UTM'}

# Переименования столбцов в основной таблице
RENAME_COLS = {'Цена закупа': 'Цена закупа без ндс'}

# Столбцы для таблицы правил UTM
UTM_COL_NAME     = 'UTM'
UTM_UTV_COL_NAME = 'utm утвержденная'


# ─── Google Sheets ─────────────────────────────────────────────────────────────

def _sheets_service():
    creds = Credentials.from_service_account_file(
        GOOGLE_SA_FILE,
        scopes=['https://www.googleapis.com/auth/spreadsheets.readonly'],
    )
    return build('sheets', 'v4', credentials=creds, cache_discovery=False)


def read_sheet():
    svc  = _sheets_service()
    resp = svc.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=DATA_RANGE,
        valueRenderOption='FORMATTED_VALUE',
    ).execute()
    return resp.get('values', [])


# ─── Парсинг ───────────────────────────────────────────────────────────────────

def _cell(row, idx):
    val = row[idx] if idx < len(row) else ''
    return val.strip() if isinstance(val, str) else str(val)


def clean_header(name):
    return name.strip().replace('\n', '').replace('\r', '').strip()


def parse_rows(raw_rows):
    if not raw_rows:
        return [], []

    headers = [clean_header(h) for h in raw_rows[0]]

    while len(headers) < 23:
        headers.append(f'col_{len(headers)}')

    data = [[_cell(raw, i) for i in range(23)] for raw in raw_rows[1:]]
    return headers, data


def parse_utm_pairs(raw_rows, headers):
    try:
        utm_idx = headers.index(UTM_COL_NAME)
        utv_idx = headers.index(UTM_UTV_COL_NAME)
    except ValueError:
        return []

    pairs = []
    for raw in raw_rows[1:]:
        row     = [_cell(raw, i) for i in range(23)]
        utm_raw = row[utm_idx]
        utm_values = [v.strip() for v in utm_raw.replace('\r\n', '\n').replace('\r', '\n').split('\n')]
        utm_values = [v for v in utm_values if v] or ['']
        utv = row[utv_idx]
        for utm in utm_values:
            pairs.append((utm, utv))
    return pairs


def filter_for_main(headers, data):
    keep_indices     = [i for i, h in enumerate(headers) if h not in EXCLUDE_COLS]
    filtered_headers = [RENAME_COLS.get(headers[i], headers[i]) for i in keep_indices]
    filtered_data    = [tuple(row[i] for i in keep_indices) for row in data]
    return filtered_headers, filtered_data


# ─── Вычисляемые поля ──────────────────────────────────────────────────────────

def _parse_price(v):
    if not v:
        return ''
    cleaned = v.replace('р.', '').replace('\xa0', '').replace(' ', '').replace(',', '.')
    try:
        return str(int(float(cleaned)))
    except (ValueError, TypeError):
        return ''


def _parse_percent(v):
    if not v:
        return 0.0
    try:
        return float(v.replace('%', '').replace(',', '.').replace('\xa0', '').replace(' ', ''))
    except (ValueError, TypeError):
        return 0.0


def _parse_nds(v):
    if not v or v.strip() == '':
        return 0.0
    if 'без' in v.lower():
        return 22.0
    return _parse_percent(v)


def process_main_data(headers, data):
    def _idx(name):
        try:
            return headers.index(name)
        except ValueError:
            return None

    price_idx = _idx('Цена закупа без ндс')
    nds_idx   = _idx('НДС')
    ak_idx    = _idx('Проценты ак')

    new_headers = headers + ['total_cost']
    new_data    = []

    for row in data:
        row = list(row)

        if price_idx is not None:
            row[price_idx] = _parse_price(row[price_idx])

        price   = float(row[price_idx]) if price_idx is not None and row[price_idx] else 0.0
        nds_pct = _parse_nds(row[nds_idx])   if nds_idx   is not None else 0.0
        ak_pct  = _parse_percent(row[ak_idx]) if ak_idx    is not None else 0.0

        total = price * (1 + nds_pct / 100) * (1 + ak_pct / 100)
        row.append(str(round(total, 2)))

        new_data.append(tuple(row))

    return new_headers, new_data


# ─── Маппинг utm из лист2 ─────────────────────────────────────────────────────

def read_utm_mapping():
    svc  = _sheets_service()
    resp = svc.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=MAPPING_SHEET_RANGE,
        valueRenderOption='FORMATTED_VALUE',
    ).execute()
    mapping = {}
    for row in resp.get('values', []):
        if len(row) >= 2:
            kanal = str(row[0]).strip()
            utm   = str(row[1]).strip()
            if kanal and utm and kanal != 'Канал':
                mapping[kanal] = utm
    return mapping


def apply_utm_mapping(conn, mapping):
    if not mapping:
        return 0
    updated = 0
    with conn.cursor() as cur:
        for kanal, utm in mapping.items():
            cur.execute(f"""
                UPDATE public.{TABLE}
                SET "utm утвержденная" = %s
                WHERE "Канал" = %s
                  AND ("utm утвержденная" IS NULL OR "utm утвержденная" = '')
            """, (utm, kanal))
            updated += cur.rowcount
    conn.commit()
    return updated


# ─── PostgreSQL ────────────────────────────────────────────────────────────────

def _quoted(name):
    return '"' + name.replace('"', '""') + '"'


def ensure_table(conn, headers):
    cols_ddl = ',\n    '.join(f'{_quoted(h)} TEXT' for h in headers)
    sql = f"""
DROP TABLE IF EXISTS public.{TABLE};
CREATE TABLE public.{TABLE} (
    id   SERIAL PRIMARY KEY,
    {cols_ddl}
);
"""
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


def insert_rows(conn, headers, data):
    cols = ', '.join(_quoted(h) for h in headers)
    sql  = f'INSERT INTO public.{TABLE} ({cols}) VALUES %s'
    with conn.cursor() as cur:
        execute_values(cur, sql, data)
    conn.commit()


def ensure_utm_table(conn):
    sql = f"""
DROP TABLE IF EXISTS public.{UTM_TABLE};
CREATE TABLE public.{UTM_TABLE} (
    id                   SERIAL PRIMARY KEY,
    "UTM"                TEXT,
    "utm утвержденная"   TEXT
);
"""
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


def insert_utm_rows(conn, utm_pairs):
    sql = f'INSERT INTO public.{UTM_TABLE} ("UTM", "utm утвержденная") VALUES %s'
    with conn.cursor() as cur:
        execute_values(cur, sql, utm_pairs)
    conn.commit()


# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    print('Читаем Google Sheets...')
    raw = read_sheet()
    print(f'  Строк в листе (с заголовком): {len(raw)}')

    headers, data = parse_rows(raw)
    source_rows   = len(raw) - 1
    print(f'  Исходных строк данных: {source_rows}')

    main_headers, main_data = filter_for_main(headers, data)
    main_headers, main_data = process_main_data(main_headers, main_data)

    utm_pairs    = parse_utm_pairs(raw, headers)
    cloned_extra = len(utm_pairs) - source_rows
    print(f'  UTM-пар (с клонированием): {len(utm_pairs)} (+{cloned_extra} строк)')

    conn = psycopg2.connect(**DB_DST)
    try:
        print(f'Создаём таблицу {TABLE} и загружаем...')
        ensure_table(conn, main_headers)
        insert_rows(conn, main_headers, main_data)
        print(f'  OK: {len(main_data)} строк')

        print('Читаем лист2 (маппинг utm утвержденная)...')
        utm_mapping = read_utm_mapping()
        print(f'  Записей в маппинге: {len(utm_mapping)}')
        updated = apply_utm_mapping(conn, utm_mapping)
        print(f'  Обновлено "utm утвержденная": {updated} строк')

        print(f'Создаём таблицу {UTM_TABLE} и загружаем...')
        ensure_utm_table(conn)
        insert_utm_rows(conn, utm_pairs)
        print(f'  OK: {len(utm_pairs)} строк')
    finally:
        conn.close()


if __name__ == '__main__':
    main()
