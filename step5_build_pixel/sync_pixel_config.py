"""
pixel/sync_pixel_config.py — синхронизация конфига пикселей из Google Sheets.

Читает Лист1 A:E:
  A: salon         — Салон
  B: project       — Проект
  C: cost_per_lead — ЦЗ раздельно
  D: cost_total    — Общая ЦЗ (используется в total_cost = kol_vo_zayavok * cost_total)
  E: pixel_name    — Название пикселя (= source_name в leads_all)

Только строки с непустым pixel_name попадают в local_pixel_config.
"""

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

logger = logging.getLogger('pixel.sync_config')

SPREADSHEET_ID = '1TIiLbeAL9_th6tYT65X_zHmEZqtiKMBPsypTWRK_mYo'
DATA_RANGE = 'Лист1!A:E'
TABLE = 'local_pixel_config'

# PIXEL_PR_2026-07-09: пиксель-ретаргетинг Перформа (utm_source суффикс _pixel_pr).
# Добавляются ПОСЛЕ GSheet-синка, т.к. sync() делает TRUNCATE перед INSERT из листа.
# cost_per_lead/cost_total=None → total_cost=0 (ставка Перформа будет дана позже).
PIXEL_PR_EXTRAS: list[dict] = [
    {'salon': 'Перформ РФ', 'project': 'Перформ/Пиксель PR', 'cost_per_lead': 1000.0, 'cost_total': None, 'pixel_name': 'victory_mavto_pixel_pr'},
    {'salon': 'Перформ РФ', 'project': 'Перформ/Пиксель PR', 'cost_per_lead': 1000.0, 'cost_total': None, 'pixel_name': 'victory_urbancar_pixel_pr'},
    {'salon': 'Перформ РФ', 'project': 'Перформ/Пиксель PR', 'cost_per_lead': 1000.0, 'cost_total': None, 'pixel_name': 'victory_premier_pixel_pr'},
    {'salon': 'Перформ РФ', 'project': 'Перформ/Пиксель PR', 'cost_per_lead': 1000.0, 'cost_total': None, 'pixel_name': 'victory_uralauto_pixel_pr'},
    {'salon': 'Перформ РФ', 'project': 'Перформ/Пиксель PR', 'cost_per_lead': 1000.0, 'cost_total': None, 'pixel_name': 'victory_pixel_pr'},
    {'salon': 'Перформ РФ', 'project': 'Перформ/Пиксель PR', 'cost_per_lead': 1000.0, 'cost_total': None, 'pixel_name': 'victory_vershina_pixel_pr'},
    {'salon': 'Перформ РФ', 'project': 'Перформ/Пиксель PR', 'cost_per_lead': 1000.0, 'cost_total': None, 'pixel_name': 'victory_carcity_pixel_pr'},
]

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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
    os.path.join(_BASE, 'config', 'cedar-gearbox-464117-e5-676d6cc8937e.json'),  # Victory: config/
    os.path.expanduser('~/cedar-gearbox-464117-e5-676d6cc8937e.json'),
] if c]
GOOGLE_SA_FILE = next((f for f in _SA_FILE_CANDIDATES if os.path.exists(f)), None)
if not GOOGLE_SA_FILE:
    raise FileNotFoundError('Google SA-ключ не найден: .secret/service_account.json (mac) или config/cedar-*.json (Victory)')


def _read_sheet() -> list[dict]:
    creds = Credentials.from_service_account_file(
        GOOGLE_SA_FILE,
        scopes=['https://www.googleapis.com/auth/spreadsheets.readonly'],
    )
    service = build('sheets', 'v4', credentials=creds, cache_discovery=False)
    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=DATA_RANGE,
        valueRenderOption='FORMATTED_VALUE',
    ).execute()
    rows = result.get('values', [])
    if not rows:
        return []

    records = []
    for row in rows[1:]:  # skip header
        while len(row) < 5:
            row.append('')
        salon, project, cost_per_lead_str, cost_total_str, pixel_name = row[:5]
        pixel_name = pixel_name.strip()
        if not pixel_name:
            continue

        def to_num(s):
            s = s.strip().replace(' ', '').replace(',', '.')
            try:
                return float(s) if s else None
            except ValueError:
                return None

        records.append({
            'salon':        salon.strip(),
            'project':      project.strip(),
            'cost_per_lead': to_num(cost_per_lead_str),
            'cost_total':   to_num(cost_total_str),
            'pixel_name':   pixel_name,
        })
    return records


def _ensure_table(conn):
    with conn.cursor() as cur:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {TABLE} (
                id            SERIAL PRIMARY KEY,
                salon         TEXT,
                project       TEXT,
                cost_per_lead NUMERIC,
                cost_total    NUMERIC,
                pixel_name    TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_{TABLE}_pixel_name
                ON {TABLE} (pixel_name);
        """)
    conn.commit()


def sync(conn) -> int:
    records = _read_sheet()
    if not records:
        logger.warning('Google Sheet вернул 0 строк')
        return 0

    _ensure_table(conn)

    with conn.cursor() as cur:
        cur.execute(f'TRUNCATE TABLE {TABLE}')
        for r in records:
            cur.execute(
                f"""INSERT INTO {TABLE} (salon, project, cost_per_lead, cost_total, pixel_name)
                    VALUES (%s, %s, %s, %s, %s)""",
                (r['salon'], r['project'], r['cost_per_lead'], r['cost_total'], r['pixel_name']),
            )
        # PIXEL_PR_2026-07-09: добавляем Перформ-пиксели ПОСЛЕ GSheet-данных.
        # TRUNCATE выше их удаляет, поэтому всегда вставляем заново.
        # ON CONFLICT DO NOTHING: защита от дублей если pixel_name уже есть в листе.
        for r in PIXEL_PR_EXTRAS:
            cur.execute(
                f"""INSERT INTO {TABLE} (salon, project, cost_per_lead, cost_total, pixel_name)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (pixel_name) DO NOTHING""",
                (r['salon'], r['project'], r['cost_per_lead'], r['cost_total'], r['pixel_name']),
            )
    conn.commit()
    logger.info('local_pixel_config: %d строк из GSheet + %d pixel_pr (Перформ) загружено',
                len(records), len(PIXEL_PR_EXTRAS))
    return len(records) + len(PIXEL_PR_EXTRAS)


if __name__ == '__main__':
    import config.db as db_module
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    db_module.init_pool()
    conn = db_module.get_conn()
    try:
        n = sync(conn)
        print(f'Синхронизировано: {n} строк')
    finally:
        db_module.put_conn(conn)
        db_module.close_pool()
