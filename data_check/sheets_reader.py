"""
step_data_check/sheets_reader.py

Читает лист Google Sheets по spreadsheet_id + числовому gid.
Паттерн аналогичен step10_crop_targeting/load_crop_targeting.py.
"""

import os
from pathlib import Path

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# SA-файл ищем рядом с корнем big_analytics_v5
_BASE = Path(__file__).resolve().parent.parent
_SA_CANDIDATES = [
    _BASE / 'config' / 'cedar-gearbox-464117-e5-676d6cc8937e.json',
    Path.home() / 'cedar-gearbox-464117-e5-676d6cc8937e.json',
]
_SA_FILE = next((str(p) for p in _SA_CANDIDATES if p.exists()), str(_SA_CANDIDATES[0]))

_SCOPES = ['https://www.googleapis.com/auth/spreadsheets.readonly']

# Известные имена колонки-домена (в нижнем регистре)
_HEADER_TOKENS = {'сайт', 'домен', 'domain', 'site', 'url', 'адрес'}


def _get_service():
    if not Path(_SA_FILE).exists():
        raise FileNotFoundError(
            f'Google service account file not found. Tried:\n'
            + '\n'.join(f'  {p}' for p in _SA_CANDIDATES)
        )
    creds = Credentials.from_service_account_file(_SA_FILE, scopes=_SCOPES)
    return build('sheets', 'v4', credentials=creds, cache_discovery=False)


def get_sheet_name_by_gid(service, spreadsheet_id: str, gid: int) -> str:
    """Возвращает имя листа по числовому gid."""
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    for sheet in meta.get('sheets', []):
        if sheet['properties']['sheetId'] == gid:
            return sheet['properties']['title']
    raise ValueError(f'Sheet with gid={gid} not found in {spreadsheet_id}')


def _is_header_row(row: list[str]) -> bool:
    """Строка считается строкой заголовков, если содержит одно из ключевых слов колонки-домена."""
    for cell in row:
        if isinstance(cell, str) and cell.strip().lower() in _HEADER_TOKENS:
            return True
    return False


def read_sheet(spreadsheet_id: str, gid: int) -> list[dict]:
    """
    Читает все строки листа (gid) из spreadsheet_id.
    Возвращает список dict: {header: value, ...}.
    Ищет первую строку, в которой есть одно из известных имён колонки-домена,
    и использует её как заголовки. Всё что выше — игнорируется (UI-шапка).
    """
    service = _get_service()
    sheet_name = get_sheet_name_by_gid(service, spreadsheet_id, gid)
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=sheet_name)
        .execute()
    )
    rows = result.get('values', [])
    if not rows:
        return []

    header_idx = None
    for i, row in enumerate(rows):
        if _is_header_row(row):
            header_idx = i
            break
    if header_idx is None:
        # fallback: первая непустая строка
        header_idx = 0

    headers = [(h.strip() if isinstance(h, str) else '') for h in rows[header_idx]]
    records = []
    for row in rows[header_idx + 1:]:
        padded = row + [''] * (len(headers) - len(row))
        records.append(dict(zip(headers, padded)))
    return records
