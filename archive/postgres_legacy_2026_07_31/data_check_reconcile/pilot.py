"""
data_check/reconcile/pilot.py — Пилотный reconcile-движок Кит-Авто

Читает лист «Воронка по дням контекст» Google Sheets (gid=375078004),
считает нашу сторону из public.fact_big_analytics (расход + продажи),
сравнивает помесячно и ставит флаг по допуску 2%.

Запуск:
    cd work/big_analytics_v5
    python3 data_check/reconcile/pilot.py           # все месяцы 26-го года
    python3 data_check/reconcile/pilot.py --months 2026-01 2026-02  # конкретные
    python3 data_check/reconcile/pilot.py --verbose  # + детали листа по строкам

Exit codes: 0=OK (все Δ ≤2%), 1=расхождение >2%, 2=ошибка скрипта

PILOT_RECONCILE_KIT_AVTO_2026-06-17
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date
from pathlib import Path
from typing import Optional

# Добавляем корень big_analytics_v5 в sys.path
_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT))

import psycopg2
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from config.settings import DB_DST

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
)
logger = logging.getLogger(__name__)

# ── Константы Кит-Авто ───────────────────────────────────────────────────────
SALON_NAME = 'Кит-Авто'
SPREADSHEET_ID = '1YGawnadMAVGBVwKmd16TvFL5fT7_hHGpXS_0Hi_8IQg'
GID = 375078004            # лист «Воронка по дням контекст»

# Набор направлений витрины для "лучшей базы" расхода (direct + tp8/посевы)
# Примечание: для Кит-Авто по FINDINGS использовалась база full (direct+tp8),
# но продавались посевы только в янв-мар. Движок считает full (Контекст+посевы).
DIRECTIONS_COST = ('Комплекс',)  # KOMPLEKS_REFACTOR_REDO_2026-07-09 (было: Контекст + посевы)

# Допуск расхождения (%)
TOLERANCE_PCT = 2.0

# SA-файл: ищем рядом с корнем проекта в .secret/
_SA_CANDIDATES = [
    _ROOT / 'config' / 'cedar-gearbox-464117-e5-676d6cc8937e.json',
    Path.home() / 'cedar-gearbox-464117-e5-676d6cc8937e.json',
    _ROOT.parent.parent / '.secret' / 'service_account.json',  # HomeServer_PythonProject/.secret/
]
_SA_FILE = next((str(p) for p in _SA_CANDIDATES if p.exists()), None)

_SCOPES = ['https://www.googleapis.com/auth/spreadsheets.readonly']

# Русские метки месяцев → (year, month)
_MONTH_RU = {
    'январь': 1, 'февраль': 2, 'март': 3, 'апрель': 4,
    'май': 5, 'июнь': 6, 'июль': 7, 'август': 8,
    'сентябрь': 9, 'октябрь': 10, 'ноябрь': 11, 'декабрь': 12,
}


# ── Google Sheets ─────────────────────────────────────────────────────────────

def _get_sheets_service():
    if _SA_FILE is None:
        raise FileNotFoundError(
            'Google Service Account JSON не найден. Проверен:\n'
            + '\n'.join(f'  {p}' for p in _SA_CANDIDATES)
        )
    creds = Credentials.from_service_account_file(_SA_FILE, scopes=_SCOPES)
    return build('sheets', 'v4', credentials=creds, cache_discovery=False)


def _gid_to_sheet_name(service, spreadsheet_id: str, gid: int) -> str:
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    for sheet in meta.get('sheets', []):
        if sheet['properties']['sheetId'] == gid:
            return sheet['properties']['title']
    raise ValueError(f'Лист gid={gid} не найден в {spreadsheet_id}')


def _parse_month_label(label: str) -> Optional[tuple[int, int]]:
    """'январь 26' → (2026, 1), 'октябрь 25' → (2025, 10), None если не распознан."""
    if not isinstance(label, str):
        return None
    parts = label.strip().lower().split()
    if len(parts) < 2:
        return None
    month_name = parts[0]
    year_str = parts[1]
    month_num = _MONTH_RU.get(month_name)
    if month_num is None:
        return None
    try:
        year = int(year_str)
        if year < 100:
            year += 2000
        return (year, month_num)
    except ValueError:
        return None


def read_sheet_voronka(
    service,
    spreadsheet_id: str,
    sheet_name: str,
    verbose: bool = False,
) -> dict[str, dict]:
    """
    Читает лист «Воронка по дням контекст» и возвращает dict:
        { 'YYYY-MM': {'cost': float, 'zayavki': int, 'priezdy': int, 'prodazhi': int} }

    Структура листа (выстрадана на Кит-Авто gid=375078004):
    - Строка 0 (row index 0): метки месяцев и числа-дни.
      Метка 'Месяц ГГ' в col tc, следующий блок в col tc+34.
    - Строка 2 (row index 2): 'ТОТАЛ' label в col tc+1.
    - Строки 3-9 (row index 3-9): Расход, Обращения, Заявки, Приезды, КО, Добро, Продажи.
      Total value в col tc+2 (anchor+2).
    - Строки 3-9 имеют label (Расход/Заявки/...) в col tc+1, total value в col tc+2.
    """
    # Читаем первые 10 строк, все колонки
    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f'{sheet_name}!1:10',
        valueRenderOption='UNFORMATTED_VALUE',
    ).execute()
    rows = result.get('values', [])
    if not rows:
        raise ValueError(f'Лист {sheet_name!r} пустой')

    row0 = rows[0] if len(rows) > 0 else []
    row3 = rows[3] if len(rows) > 3 else []   # Расход
    row5 = rows[5] if len(rows) > 5 else []   # Заявки
    row6 = rows[6] if len(rows) > 6 else []   # Приезды
    row9 = rows[9] if len(rows) > 9 else []   # Продажи

    def _safe_get(row: list, col: int, default=0):
        if col < len(row) and row[col] not in ('', None):
            try:
                return float(row[col])
            except (TypeError, ValueError):
                return default
        return default

    # Находим все блоки месяцев: col tc = метка 'месяц гг'
    month_blocks: dict[str, dict] = {}
    for tc, cell in enumerate(row0):
        ym = _parse_month_label(cell)
        if ym is None:
            continue
        year, month = ym
        key = f'{year}-{month:02d}'
        # total value в col tc+2
        tc_val = tc + 2

        cost = _safe_get(row3, tc_val)
        zayavki = int(_safe_get(row5, tc_val))
        priezdy = int(_safe_get(row6, tc_val))
        prodazhi = int(_safe_get(row9, tc_val))

        # Верификация: label в col tc+1 должен содержать 'расход'
        label_check = row3[tc + 1] if tc + 1 < len(row3) else ''
        if isinstance(label_check, str) and 'расход' not in label_check.lower():
            logger.warning(
                'Блок %s: row3[%d]=%r — ожидали "Расход", пропускаем',
                key, tc + 1, label_check,
            )
            continue

        month_blocks[key] = {
            'cost': cost,
            'zayavki': zayavki,
            'priezdy': priezdy,
            'prodazhi': prodazhi,
            'tc': tc,
            'tc_val': tc_val,
        }
        if verbose:
            logger.info(
                'Лист %s: col tc=%d tc_val=%d → cost=%.0f zayavki=%d priezdy=%d prodazhi=%d',
                key, tc, tc_val, cost, zayavki, priezdy, prodazhi,
            )

    return month_blocks


# ── Наша сторона (PostgreSQL) ─────────────────────────────────────────────────

_SQL_COST = """
    SELECT
        DATE_TRUNC('month', "Date")::date AS month,
        SUM(total_cost) AS cost
    FROM public.fact_big_analytics
    WHERE салон = %(salon)s
      AND направление = ANY(%(directions)s)
      AND атрибуция = 'По дате заявки'
    GROUP BY 1
    ORDER BY 1;
"""

_SQL_SALES = """
    SELECT
        DATE_TRUNC('month', "Date")::date AS month,
        SUM(prodazhi) AS prodazhi
    FROM public.fact_big_analytics
    WHERE салон = %(salon)s
      AND направление = ANY(%(directions)s)
      AND атрибуция = 'По дате заявки'
    GROUP BY 1
    ORDER BY 1;
"""


def fetch_our_side(db_params: dict, salon: str, directions: tuple) -> dict[str, dict]:
    """
    Возвращает dict { 'YYYY-MM': {'cost': float, 'prodazhi': int} }
    для указанного салона и набора направлений.
    """
    with psycopg2.connect(**db_params) as conn:
        with conn.cursor() as cur:
            # Расход
            cur.execute(_SQL_COST, {'salon': salon, 'directions': list(directions)})
            cost_rows = cur.fetchall()
            # Продажи
            cur.execute(_SQL_SALES, {'salon': salon, 'directions': list(directions)})
            sales_rows = cur.fetchall()

    result: dict[str, dict] = {}
    for month_date, cost in cost_rows:
        key = month_date.strftime('%Y-%m')
        result.setdefault(key, {})['cost'] = float(cost)
    for month_date, prodazhi in sales_rows:
        key = month_date.strftime('%Y-%m')
        result.setdefault(key, {})['prodazhi'] = int(prodazhi) if prodazhi else 0

    return result


# ── Reconcile ─────────────────────────────────────────────────────────────────

def _delta_pct(our: float, sheet: float) -> Optional[float]:
    """Δ% = (наш - лист) / лист * 100. None если лист=0."""
    if sheet == 0:
        return None
    return (our - sheet) / sheet * 100.0


def _flag(delta_pct: Optional[float], tol: float = TOLERANCE_PCT) -> str:
    if delta_pct is None:
        return '?'
    return 'OK' if abs(delta_pct) <= tol else 'FAIL'


def reconcile(
    sheet_data: dict[str, dict],
    our_data: dict[str, dict],
    target_months: Optional[list[str]] = None,
    include_current: bool = False,
    verbose: bool = False,
) -> list[dict]:
    """
    Сводит sheet_data и our_data помесячно.
    Возвращает список строк для отчёта.

    include_current=False (default): исключает текущий неполный месяц.
    Текущий месяц всегда помечается в выводе как '(неполный)' даже если включён.
    """
    today = date.today()
    current_month = f'{today.year}-{today.month:02d}'

    # Берём только месяцы >= 2026-01 (пилот — только 2026 год)
    all_months = sorted(set(sheet_data) | set(our_data))
    if target_months:
        all_months = [m for m in all_months if m in target_months]
    else:
        all_months = [m for m in all_months if m >= '2026-01']
        if not include_current and current_month in all_months:
            logger.info(
                'Исключаю текущий неполный месяц %s (используйте --include-current для включения)',
                current_month,
            )
            all_months = [m for m in all_months if m != current_month]

    rows = []
    for month in all_months:
        sd = sheet_data.get(month, {})
        od = our_data.get(month, {})

        sheet_cost = sd.get('cost', 0.0)
        our_cost = od.get('cost', 0.0)
        sheet_prodazhi = sd.get('prodazhi', 0)
        our_prodazhi = od.get('prodazhi', 0)
        sheet_priezdy = sd.get('priezdy', 0)

        delta_cost = _delta_pct(our_cost, sheet_cost)
        delta_prodazhi = _delta_pct(float(our_prodazhi), float(sheet_prodazhi))

        rows.append({
            'month': month,
            'sheet_cost': sheet_cost,
            'our_cost': our_cost,
            'delta_cost_pct': delta_cost,
            'flag_cost': _flag(delta_cost),
            'sheet_prodazhi': sheet_prodazhi,
            'our_prodazhi': our_prodazhi,
            'delta_prodazhi_pct': delta_prodazhi,
            'flag_prodazhi': _flag(delta_prodazhi),
            'sheet_priezdy': sheet_priezdy,   # доп. сигнал (loose)
        })

    return rows


# ── Форматирование отчёта ─────────────────────────────────────────────────────

def _fmt_pct(v: Optional[float]) -> str:
    if v is None:
        return 'N/A'
    sign = '+' if v >= 0 else ''
    return f'{sign}{v:.1f}%'


def format_report(rows: list[dict]) -> str:
    lines = [
        f'== Reconcile: {SALON_NAME} (пилот) ==',
        f'Лист gid={GID} vs fact_big_analytics, ось: По дате заявки',
        f'Направления: {", ".join(DIRECTIONS_COST)}',
        f'Допуск: ±{TOLERANCE_PCT}%',
        '',
        f'{"Месяц":<10} {"Лист Расход":>14} {"Наш Расход":>14} {"Δ%":>8} {"Ст":>5}  '
        f'{"Лист Прод":>10} {"Наш Прод":>10} {"Δ%":>8} {"Ст":>5}  '
        f'{"Лист Приезды":>14}',
        '-' * 105,
    ]

    sum_sheet_cost = 0.0
    sum_our_cost = 0.0
    sum_sheet_prodazhi = 0
    sum_our_prodazhi = 0
    fail_cost = []
    fail_prodazhi = []

    for r in rows:
        dc = _fmt_pct(r['delta_cost_pct'])
        dp = _fmt_pct(r['delta_prodazhi_pct'])
        fc = r['flag_cost']
        fp = r['flag_prodazhi']
        flag_cost_icon = '' if fc == 'OK' else '!!'
        flag_prod_icon = '' if fp == 'OK' else '!!'

        lines.append(
            f'{r["month"]:<10} '
            f'{r["sheet_cost"]:>14,.0f} '
            f'{r["our_cost"]:>14,.0f} '
            f'{dc:>8} {fc:>4}{flag_cost_icon}  '
            f'{r["sheet_prodazhi"]:>10} '
            f'{r["our_prodazhi"]:>10} '
            f'{dp:>8} {fp:>4}{flag_prod_icon}  '
            f'{r["sheet_priezdy"]:>14}'
        )

        sum_sheet_cost += r['sheet_cost']
        sum_our_cost += r['our_cost']
        sum_sheet_prodazhi += r['sheet_prodazhi']
        sum_our_prodazhi += r['our_prodazhi']
        if fc == 'FAIL':
            fail_cost.append(r['month'])
        if fp == 'FAIL':
            fail_prodazhi.append(r['month'])

    # Итоговая строка Σ
    sigma_delta_cost = _delta_pct(sum_our_cost, sum_sheet_cost)
    sigma_delta_prod = _delta_pct(float(sum_our_prodazhi), float(sum_sheet_prodazhi))
    lines.append('-' * 105)
    lines.append(
        f'{"ИТОГО":<10} '
        f'{sum_sheet_cost:>14,.0f} '
        f'{sum_our_cost:>14,.0f} '
        f'{_fmt_pct(sigma_delta_cost):>8} {"":>5}  '
        f'{sum_sheet_prodazhi:>10} '
        f'{sum_our_prodazhi:>10} '
        f'{_fmt_pct(sigma_delta_prod):>8} {"":>5}'
    )

    # ── Сверка с FINDINGS (эталон сессии 2026-06-11) ─────────────────────────
    # FINDINGS: Кит-Авто, Янв-Май 2026, база full (Контекст+tp8):
    #   Расход Σ −0.3%, Янв −3.2%, Фев/Мар/Апр ≤1.6%, Май −1.8%
    # ВАЖНО: FINDINGS не включал Июнь (данные были неполные на 06-11)
    FINDINGS_COST = {
        '2026-01': -3.2,
        '2026-02': None,   # ≤1.6%, точное не указано
        '2026-03': None,   # ≤1.6%
        '2026-04': None,   # ≤1.6%
        '2026-05': -1.8,
    }

    lines.append('')
    lines.append('== СВЕРКА С ЭТАЛОНОМ FINDINGS (сессия 2026-06-11) ==')
    lines.append('Эталон: Расход Σ ≈ −0.3%; Янв −3.2%, Фев/Мар/Апр ≤1.6%, Май −1.8%')
    lines.append('(продажи: лист 80, наш 70, Δ −10 шт — ось разная, by-design)')
    lines.append('')
    lines.append(f'{"Месяц":<10} {"Наш Δ%":>10} {"Эталон Δ%":>12} {"Совпадение":>12}')
    lines.append('-' * 48)

    matches_cost = []
    for r in rows:
        m = r['month']
        if m not in FINDINGS_COST:
            continue
        our_delta = r['delta_cost_pct']
        ref_delta = FINDINGS_COST[m]
        if our_delta is None:
            match_str = 'N/A'
        elif ref_delta is None:
            # Эталон FINDINGS: ≤1.6% (включительно, с учётом дрейфа данных ±0.5%)
            match_str = 'OK (≤1.6%)' if abs(our_delta) <= 2.1 else f'DRIFT (>{1.6}%)'
        else:
            # Допуск сравнения с эталоном: ±1.5% (данные дозрели с 06-11)
            match_str = 'OK' if abs(our_delta - ref_delta) <= 1.5 else 'DRIFT'
        ref_str = f'{ref_delta:+.1f}%' if ref_delta is not None else '≤1.6%'
        lines.append(f'{m:<10} {_fmt_pct(our_delta):>10} {ref_str:>12} {match_str:>12}')
        matches_cost.append(match_str)

    # Σ по Янв-Май для сравнения с эталоном −0.3%
    ym_rows = [r for r in rows if '2026-01' <= r['month'] <= '2026-05']
    if ym_rows:
        s_sheet = sum(r['sheet_cost'] for r in ym_rows)
        s_our = sum(r['our_cost'] for r in ym_rows)
        sigma_ym = _delta_pct(s_our, s_sheet)
        lines.append('-' * 48)
        lines.append(
            f'{"Σ Янв-Май":<10} {_fmt_pct(sigma_ym):>10} {"≈ −0.3%":>12} '
            f'{"OK" if sigma_ym is not None and abs(sigma_ym + 0.3) <= 1.5 else "DRIFT":>12}'
        )

    lines.append('')
    lines.append('Примечание: данные дозревают в CRM — небольшой дрейф от FINDINGS нормален.')
    lines.append('Июнь исключён из FINDINGS-сравнения (неполный месяц на обеих сторонах).')

    lines.append('')
    # Итог
    total_fails = len(fail_cost) + len(fail_prodazhi)
    if total_fails == 0:
        lines.append('ВЕРДИКТ: OK — все Δ расхода в допуске ±2%')
    else:
        if fail_cost:
            lines.append(f'ВЕРДИКТ РАСХОД: FAIL — месяцы >2%: {", ".join(fail_cost)}')
        if fail_prodazhi:
            lines.append(
                f'ВЕРДИКТ ПРОДАЖИ: месяцы с Δ>2%: {", ".join(fail_prodazhi)} '
                f'(продажи by-design плавают — ось лист vs витрина разная)'
            )

    return '\n'.join(lines)


# ── Главная точка входа ───────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description='Reconcile Кит-Авто: Google Sheets vs fact_big_analytics'
    )
    parser.add_argument(
        '--months', nargs='*', metavar='YYYY-MM',
        help='Конкретные месяцы (по умолчанию — все 2026-XX из листа)',
    )
    parser.add_argument(
        '--verbose', '-v', action='store_true',
        help='Детальный лог чтения листа',
    )
    parser.add_argument(
        '--include-current', action='store_true',
        help='Включить текущий неполный месяц (по умолчанию исключается)',
    )
    args = parser.parse_args()

    try:
        logger.info('Инициализация Google Sheets API...')
        service = _get_sheets_service()
        sheet_name = _gid_to_sheet_name(service, SPREADSHEET_ID, GID)
        logger.info('Лист: %r (gid=%d)', sheet_name, GID)

        logger.info('Читаю лист %s...', sheet_name)
        sheet_data = read_sheet_voronka(service, SPREADSHEET_ID, sheet_name, verbose=args.verbose)
        logger.info('Прочитано блоков месяцев: %d', len(sheet_data))
        for m, d in sorted(sheet_data.items()):
            logger.info('  %s: cost=%.0f zayavki=%d priezdy=%d prodazhi=%d',
                        m, d['cost'], d['zayavki'], d['priezdy'], d['prodazhi'])

        logger.info('Запрашиваю нашу сторону из fact_big_analytics...')
        our_data = fetch_our_side(DB_DST, SALON_NAME, DIRECTIONS_COST)
        logger.info('Наши месяцы: %s', sorted(our_data))
        for m, d in sorted(our_data.items()):
            logger.info('  %s: cost=%.0f prodazhi=%d', m, d.get('cost', 0), d.get('prodazhi', 0))

        logger.info('Свожу помесячно...')
        rows = reconcile(
            sheet_data, our_data,
            target_months=args.months,
            include_current=args.include_current,
            verbose=args.verbose,
        )

        report = format_report(rows)
        print(report)

        fail_count = sum(
            1 for r in rows
            if r['flag_cost'] == 'FAIL' or r['flag_prodazhi'] == 'FAIL'
        )
        return 1 if fail_count > 0 else 0

    except Exception as exc:
        logger.error('Ошибка: %s', exc, exc_info=True)
        return 2


if __name__ == '__main__':
    sys.exit(main())
