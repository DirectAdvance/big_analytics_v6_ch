"""
data_check/reconcile/reconcile.py — Reconcile-движок по всем салонам реестра

Читает лист «Воронка по дням контекст» каждого салона из registry.json,
считает нашу сторону из public.fact_big_analytics (расход + продажи + приезды),
сравнивает помесячно и ставит 🟢/🟡/🔴 против эталона FINDINGS.

Расход   — ось заявки (атрибуция='По дате заявки'), fact_big_analytics.total_cost
Приезды  — ось визита, fact_big_analytics.priezd_arrival_date (счётчик СУММОЙ по строкам)
Продажи  — ось визита, fact_big_analytics.prodazhi_arrival_date

Запуск:
    cd work/big_analytics_v5
    python3 data_check/reconcile/reconcile.py                        # все салоны
    python3 data_check/reconcile/reconcile.py --salon "Кит-Авто"    # один салон
    python3 data_check/reconcile/reconcile.py --salon "Лидер" "АЦ Иртыш"
    python3 data_check/reconcile/reconcile.py --months 2026-01 2026-05
    python3 data_check/reconcile/reconcile.py --verbose              # детальный лог листа

Exit codes: 0=все OK, 1=есть FAIL, 2=ошибка скрипта

RECONCILE_ENGINE_ALL_SALONS_2026-06-17
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Optional

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

# ── Константы ─────────────────────────────────────────────────────────────────

_REGISTRY_FILE = Path(__file__).parent / 'registry.json'

# Направления витрины для расчёта расхода (Комплекс = Контекст + посевы + SEO + SEO Flow + VK ads)
# KOMPLEKS_REFACTOR_REDO_2026-07-09
DIRECTIONS_COST = ('Комплекс',)

# Допуск флага расхода (%) — FAIL если >2%
TOLERANCE_COST_PCT = 2.0

# Месяцы 2026 для сравнения (текущий неполный исключается по умолчанию)
_MIN_MONTH = '2026-01'

# SA-файл Google
_SA_CANDIDATES = [
    _ROOT.parent.parent / '.secret' / 'service_account.json',  # HomeServer/.secret/
    _ROOT / 'config' / 'cedar-gearbox-464117-e5-676d6cc8937e.json',
    Path.home() / 'cedar-gearbox-464117-e5-676d6cc8937e.json',
]
_SA_FILE = next((str(p) for p in _SA_CANDIDATES if p.exists()), None)
_SCOPES = ['https://www.googleapis.com/auth/spreadsheets.readonly']

# Русские метки месяцев
_MONTH_RU = {
    'январь': 1, 'февраль': 2, 'март': 3, 'апрель': 4,
    'май': 5, 'июнь': 6, 'июль': 7, 'август': 8,
    'сентябрь': 9, 'октябрь': 10, 'ноябрь': 11, 'декабрь': 12,
}

# Эталонные вердикты FINDINGS читаются из registry.json (поля findings_verdict, findings_note).
# Хардкод _FINDINGS_VERDICTS удалён 2026-06-17 — единый источник правды: registry.json.
# RECONCILE_ENGINE_REGISTRY_VERDICTS_2026-06-17


# ── Google Sheets API ──────────────────────────────────────────────────────────

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


def _parse_month_label(label) -> Optional[tuple[int, int]]:
    """'январь 26' → (2026, 1), 'март 2026' → (2026, 3). None если не распознан."""
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
    salon_name: str,
    verbose: bool = False,
) -> dict[str, dict]:
    """
    Читает лист «Воронка по дням контекст» и возвращает dict:
        { 'YYYY-MM': {'cost': float, 'zayavki': int, 'priezdy': int, 'prodazhi': int} }

    Структура листа (выстрадана на всех 21 салоне):
    - Строка 0 (row index 0): метки месяцев 'месяц гг' в col tc, следующий блок в col tc+34.
      ВНИМАНИЕ: у некоторых салонов порядок месяцев ОБРАТНЫЙ (последний месяц первым).
    - Метка может быть в col tc ИЛИ в col tc-1 (смещение 0 или -1) — проверяем оба.
    - Строка 2: 'ТОТАЛ' label в col tc+1.
    - Строки 3-9: Расход(r3), Обращения(r4), Заявки(r5), Приезды(r6), КО(r7), Добро(r8), Продажи(r9).
      Total value в col tc+2 (anchor+2).

    Фоллбэк при отсутствии 'расход' в label row3: пробуем без верификации label.
    """
    # Читаем первые 10 строк, все колонки
    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f'{sheet_name}!1:10',
        valueRenderOption='UNFORMATTED_VALUE',
    ).execute()
    rows = result.get('values', [])
    if not rows:
        logger.warning('[%s] Лист %r пустой', salon_name, sheet_name)
        return {}

    row0 = rows[0] if len(rows) > 0 else []
    row3 = rows[3] if len(rows) > 3 else []   # Расход
    row5 = rows[5] if len(rows) > 5 else []   # Заявки
    row6 = rows[6] if len(rows) > 6 else []   # Приезды
    row9 = rows[9] if len(rows) > 9 else []   # Продажи

    def _safe_float(row: list, col: int, default=0.0) -> float:
        if col < len(row) and row[col] not in ('', None):
            try:
                return float(row[col])
            except (TypeError, ValueError):
                return default
        return default

    month_blocks: dict[str, dict] = {}

    for tc, cell in enumerate(row0):
        ym = _parse_month_label(cell)
        if ym is None:
            continue
        year, month = ym
        key = f'{year}-{month:02d}'
        if key in month_blocks:
            continue  # уже нашли этот месяц (дубль в листе)

        # Стандартная позиция: total value = tc+2
        tc_val = tc + 2

        # Верифицируем label в row3[tc+1] — ожидаем 'расход'
        label_r3 = row3[tc + 1] if tc + 1 < len(row3) else ''
        has_cost_label = isinstance(label_r3, str) and 'расход' in label_r3.lower()

        # Если label не найден — пробуем смещение tc-1 (метка в предыдущей колонке)
        if not has_cost_label and tc > 0:
            label_r3_shifted = row3[tc] if tc < len(row3) else ''
            if isinstance(label_r3_shifted, str) and 'расход' in label_r3_shifted.lower():
                # Значит реальный anchor = tc-1, смещаем tc_val
                tc_val = tc + 1
                has_cost_label = True
                logger.debug('[%s] %s: метка "расход" на сдвиге tc-1 → tc_val=%d', salon_name, key, tc_val)

        if not has_cost_label:
            # Фоллбэк: берём значение без проверки label (лист может иметь иную структуру)
            # Проверяем что в tc_val есть числовое значение расхода (>0)
            cost_try = _safe_float(row3, tc_val)
            if cost_try <= 0:
                logger.debug(
                    '[%s] %s: пропуск tc=%d (нет label "расход" в row3[%d]=%r, cost=%.0f)',
                    salon_name, key, tc, tc + 1, label_r3, cost_try,
                )
                continue

        cost = _safe_float(row3, tc_val)
        zayavki = int(_safe_float(row5, tc_val))
        priezdy = int(_safe_float(row6, tc_val))
        prodazhi = int(_safe_float(row9, tc_val))

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
                '[%s] %s: tc=%d tc_val=%d cost=%.0f zayavki=%d priezdy=%d prodazhi=%d',
                salon_name, key, tc, tc_val, cost, zayavki, priezdy, prodazhi,
            )

    if not month_blocks:
        logger.warning('[%s] Лист %r: не найдено ни одного блока месяца в row0', salon_name, sheet_name)

    return month_blocks


# ── Наша сторона (PostgreSQL) ──────────────────────────────────────────────────

_SQL_COST = """
    SELECT
        DATE_TRUNC('month', "Date")::date AS month,
        SUM(total_cost) AS cost
    FROM public.fact_big_analytics
    WHERE "салон" = %(salon)s
      AND направление = ANY(%(directions)s)
      AND атрибуция = 'По дате заявки'
    GROUP BY 1
    ORDER BY 1;
"""

_SQL_ARRIVAL = """
    SELECT
        DATE_TRUNC('month', "Date")::date AS month,
        SUM(priezd_arrival_date)   AS priezdy,
        SUM(prodazhi_arrival_date) AS prodazhi
    FROM public.fact_big_analytics
    WHERE "салон" = %(salon)s
      AND направление = ANY(%(directions)s)
      AND атрибуция = 'По дате заявки'
    GROUP BY 1
    ORDER BY 1;
"""


def fetch_our_side(db_params: dict, vitrina_name: str, directions: tuple) -> dict[str, dict]:
    """
    Возвращает { 'YYYY-MM': {'cost': float, 'priezdy': int, 'prodazhi': int} }
    Расход — ось заявки (Date). Приезды/продажи — счётчики arrival_date из fact_big_analytics.
    """
    with psycopg2.connect(**db_params) as conn:
        with conn.cursor() as cur:
            params = {'salon': vitrina_name, 'directions': list(directions)}
            cur.execute(_SQL_COST, params)
            cost_rows = cur.fetchall()
            cur.execute(_SQL_ARRIVAL, params)
            arr_rows = cur.fetchall()

    result: dict[str, dict] = {}
    for month_date, cost in cost_rows:
        key = month_date.strftime('%Y-%m')
        result.setdefault(key, {})['cost'] = float(cost or 0)
    for month_date, priezdy, prodazhi in arr_rows:
        key = month_date.strftime('%Y-%m')
        result.setdefault(key, {})['priezdy'] = int(priezdy or 0)
        result.setdefault(key, {})['prodazhi'] = int(prodazhi or 0)

    return result


# ── Reconcile логика ───────────────────────────────────────────────────────────

def _delta_pct(our: float, sheet: float) -> Optional[float]:
    if sheet == 0:
        return None
    return (our - sheet) / abs(sheet) * 100.0


def _flag_cost(delta_pct: Optional[float]) -> str:
    if delta_pct is None:
        return '?'
    return 'OK' if abs(delta_pct) <= TOLERANCE_COST_PCT else 'FAIL'


def _verdict_symbol(sigma_cost_pct: Optional[float], has_arrival_deficit: bool) -> str:
    """
    Проставляем вердикт 🟢/🟡/🔴 по логике FINDINGS:
    - 🔴  если расход FAIL по Σ (>5%) ИЛИ явный дефект приездов/продаж
    - 🟡  если расход by-design 2-10% (посевы/PLEX) ИЛИ приезды с недобором by-design
    - 🟢  если расход ≤2% Σ и нет явных дефектов
    """
    if sigma_cost_pct is None:
        return '?'
    if has_arrival_deficit:
        return 'red'
    if abs(sigma_cost_pct) <= TOLERANCE_COST_PCT:
        return 'green'
    if abs(sigma_cost_pct) <= 10.0:
        return 'yellow'
    return 'red'


def reconcile_salon(
    sheet_data: dict[str, dict],
    our_data: dict[str, dict],
    target_months: Optional[list[str]] = None,
    include_current: bool = False,
) -> list[dict]:
    """Сводит sheet_data и our_data помесячно. Возвращает список строк-записей."""
    today = date.today()
    current_month = f'{today.year}-{today.month:02d}'

    all_months = sorted(set(sheet_data) | set(our_data))
    if target_months:
        all_months = [m for m in all_months if m in target_months]
    else:
        all_months = [m for m in all_months if m >= _MIN_MONTH]
        if not include_current and current_month in all_months:
            all_months = [m for m in all_months if m != current_month]

    rows = []
    for month in all_months:
        sd = sheet_data.get(month, {})
        od = our_data.get(month, {})

        sheet_cost    = sd.get('cost', 0.0)
        sheet_priezdy = sd.get('priezdy', 0)
        sheet_prodazhi = sd.get('prodazhi', 0)
        our_cost      = od.get('cost', 0.0)
        our_priezdy   = od.get('priezdy', 0)
        our_prodazhi  = od.get('prodazhi', 0)

        dc = _delta_pct(our_cost, sheet_cost)
        dp = _delta_pct(float(our_priezdy), float(sheet_priezdy))
        ds = _delta_pct(float(our_prodazhi), float(sheet_prodazhi))

        rows.append({
            'month': month,
            'sheet_cost': sheet_cost,
            'our_cost': our_cost,
            'delta_cost_pct': dc,
            'flag_cost': _flag_cost(dc),
            'sheet_priezdy': sheet_priezdy,
            'our_priezdy': our_priezdy,
            'delta_priezdy_pct': dp,
            'sheet_prodazhi': sheet_prodazhi,
            'our_prodazhi': our_prodazhi,
            'delta_prodazhi_pct': ds,
        })

    return rows


# ── Форматирование ─────────────────────────────────────────────────────────────

def _fmt(v: Optional[float], decimals: int = 1) -> str:
    if v is None:
        return 'N/A'
    sign = '+' if v >= 0 else ''
    return f'{sign}{v:.{decimals}f}%'


def _verdict_icon(v: str) -> str:
    return {'green': 'OK', 'yellow': 'WARN', 'red': 'FAIL', '?': '?'}.get(v, v)


def format_salon_report(
    salon: dict,
    rows: list[dict],
    our_data: dict[str, dict],
    sheet_data: dict[str, dict],
) -> tuple[str, dict]:
    """
    Возвращает (строка_отчёта_салона, summary_dict).
    summary_dict: {salon_name, sigma_cost_pct, sigma_priezdy_pct, sigma_prodazhi_pct,
                   verdict_ours, verdict_findings, match}
    """
    name = salon['name']
    vname = salon['vitrina_name']
    # Вердикт FINDINGS берётся из registry.json (findings_verdict, findings_note),
    # не из хардкода — единый источник правды. RECONCILE_ENGINE_REGISTRY_VERDICTS_2026-06-17
    findings_ref = {
        'sigma_verdict': salon.get('findings_verdict', '?'),
        'note': salon.get('findings_note', ''),
    }

    # Σ по всем месяцам в rows
    total_sheet_cost     = sum(r['sheet_cost'] for r in rows)
    total_our_cost       = sum(r['our_cost'] for r in rows)
    total_sheet_priezdy  = sum(r['sheet_priezdy'] for r in rows)
    total_our_priezdy    = sum(r['our_priezdy'] for r in rows)
    total_sheet_prodazhi = sum(r['sheet_prodazhi'] for r in rows)
    total_our_prodazhi   = sum(r['our_prodazhi'] for r in rows)

    sigma_cost     = _delta_pct(total_our_cost, total_sheet_cost)
    sigma_priezdy  = _delta_pct(float(total_our_priezdy), float(total_sheet_priezdy))
    sigma_prodazhi = _delta_pct(float(total_our_prodazhi), float(total_sheet_prodazhi))

    # Определяем явный дефект приездов: наш==0 при лист>100
    has_arrival_deficit = (total_our_priezdy == 0 and total_sheet_priezdy > 100)

    verdict_ours = _verdict_symbol(sigma_cost, has_arrival_deficit)
    findings_verdict = findings_ref.get('sigma_verdict', '?')
    findings_cost_pct = None  # sigma_cost_pct не хранится в registry.json (убран хардкод)

    # Совпадение с FINDINGS
    verdict_match = (verdict_ours == findings_verdict)

    lines = [
        f'',
        f'=== {name} ({salon["city"]}, {salon["crm"]}) ===',
        f'    Таблица: {salon["spreadsheet_id"]}  gid={salon["gid"]}',
        f'    Витрина: {vname}',
        f'',
        f'    {"Месяц":<10} {"Л.Расход":>12} {"Н.Расход":>12} {"ΔРасх":>8} {"Ст":>6}  '
        f'{"Л.Приезд":>10} {"Н.Приезд":>10} {"ΔПриезд":>8}  '
        f'{"Л.Продажи":>10} {"Н.Продажи":>10} {"ΔПродажи":>9}',
        f'    ' + '-' * 110,
    ]

    for r in rows:
        dc = _fmt(r['delta_cost_pct'])
        dp = _fmt(r['delta_priezdy_pct'])
        ds = _fmt(r['delta_prodazhi_pct'])
        fc = r['flag_cost']
        flag_icon = '!!' if fc == 'FAIL' else '  '
        lines.append(
            f'    {r["month"]:<10} '
            f'{r["sheet_cost"]:>12,.0f} '
            f'{r["our_cost"]:>12,.0f} '
            f'{dc:>8} {fc:>4}{flag_icon}  '
            f'{r["sheet_priezdy"]:>10} '
            f'{r["our_priezdy"]:>10} '
            f'{dp:>8}  '
            f'{r["sheet_prodazhi"]:>10} '
            f'{r["our_prodazhi"]:>10} '
            f'{ds:>9}'
        )

    lines.append(f'    ' + '-' * 110)
    lines.append(
        f'    {"SIGMA":<10} '
        f'{total_sheet_cost:>12,.0f} '
        f'{total_our_cost:>12,.0f} '
        f'{_fmt(sigma_cost):>8} {"":>6}  '
        f'{total_sheet_priezdy:>10} '
        f'{total_our_priezdy:>10} '
        f'{_fmt(sigma_priezdy):>8}  '
        f'{total_sheet_prodazhi:>10} '
        f'{total_our_prodazhi:>10} '
        f'{_fmt(sigma_prodazhi):>9}'
    )

    # Сравнение с FINDINGS
    ref_str = f'{findings_cost_pct:+.1f}%' if findings_cost_pct is not None else 'N/A'
    drift = ''
    if sigma_cost is not None and findings_cost_pct is not None:
        diff = abs(sigma_cost - findings_cost_pct)
        drift = f'drift={diff:.1f}pp' if diff > 2.0 else f'drift={diff:.1f}pp (OK)'

    match_icon = 'MATCH' if verdict_match else 'MISMATCH'
    lines += [
        f'',
        f'    Вердикт наш:     {verdict_ours} (sigma расход {_fmt(sigma_cost)}, приезды {_fmt(sigma_priezdy)})',
        f'    Вердикт FINDINGS: {findings_verdict} (sigma {ref_str}) — {findings_ref.get("note", "")}',
        f'    Совпадение:      {match_icon} {drift}',
    ]

    if has_arrival_deficit:
        lines.append(f'    ВНИМАНИЕ: Наши приезды=0 при лист={total_sheet_priezdy} — признак дефекта витрины!')

    summary = {
        'salon': name,
        'vitrina': vname,
        'city': salon['city'],
        'crm': salon['crm'],
        'sigma_cost_pct': sigma_cost,
        'sigma_priezdy_pct': sigma_priezdy,
        'sigma_prodazhi_pct': sigma_prodazhi,
        'verdict_ours': verdict_ours,
        'verdict_findings': findings_verdict,
        'match': verdict_match,
        'has_arrival_deficit': has_arrival_deficit,
        'sheet_months': len(rows),
        'note': findings_ref.get('note', ''),
    }

    return '\n'.join(lines), summary


# ── Итоговая сводная таблица ───────────────────────────────────────────────────

def format_summary(summaries: list[dict]) -> str:
    lines = [
        '',
        '=' * 130,
        'СВОДНАЯ ТАБЛИЦА — ВСЕ САЛОНЫ vs FINDINGS',
        '=' * 130,
        f'{"#":<3} {"Салон":<22} {"Город":<20} {"CRM":<6} '
        f'{"Σ Расход Δ%":>12} {"Σ Приезды Δ%":>13} {"Σ Продажи Δ%":>13} '
        f'{"Наш":>6} {"FINDINGS":>9} {"Совпад":>8}',
        '-' * 130,
    ]

    matches = []
    mismatches = []

    for i, s in enumerate(summaries, 1):
        dc = _fmt(s['sigma_cost_pct']) if s['sigma_cost_pct'] is not None else 'N/A'
        dp = _fmt(s['sigma_priezdy_pct']) if s['sigma_priezdy_pct'] is not None else 'N/A'
        ds = _fmt(s['sigma_prodazhi_pct']) if s['sigma_prodazhi_pct'] is not None else 'N/A'
        v_our = _verdict_icon(s['verdict_ours'])
        v_fin = _verdict_icon(s['verdict_findings'])
        match_str = 'MATCH' if s['match'] else 'MISMATCH'
        lines.append(
            f'{i:<3} {s["salon"]:<22} {s["city"]:<20} {s["crm"]:<6} '
            f'{dc:>12} {dp:>13} {ds:>13} '
            f'{v_our:>6} {v_fin:>9} {match_str:>8}'
        )
        if s['match']:
            matches.append(s['salon'])
        else:
            mismatches.append(s['salon'])

    lines += [
        '-' * 130,
        f'',
        f'Совпало с FINDINGS: {len(matches)}/{len(summaries)} — {", ".join(matches) if matches else "нет"}',
        f'',
    ]

    if mismatches:
        lines.append(f'Разошлось с FINDINGS ({len(mismatches)}):')
        for s in summaries:
            if not s['match']:
                lines.append(
                    f'  - {s["salon"]}: наш={_verdict_icon(s["verdict_ours"])} '
                    f'(Σрасход={_fmt(s["sigma_cost_pct"])}), '
                    f'FINDINGS={_verdict_icon(s["verdict_findings"])} ({s["note"]})'
                )

    return '\n'.join(lines)


# ── Краткий Telegram-дайджест ──────────────────────────────────────────────────

def format_tg_digest(summaries: list[dict]) -> str:
    """
    Краткий дайджест по СЛОЮ РАСХОДА (надёжная метрика).
    Формат строки на салон: ✅/⚠️/🔴 Имя Δ%
    Приезды/продажи — одна сводная строка внизу (by-design недобор, зашумляют).
    TG_RECONCILE_DIGEST_2026-06-17
    """
    today = date.today().strftime('%d.%m.%Y')
    green, yellow, red, unknown = [], [], [], []

    for s in summaries:
        v = s.get('verdict_ours', '?')
        pct = s.get('sigma_cost_pct')
        pct_str = f'{pct:+.1f}%' if pct is not None else '?'
        line = f'{s["salon"]} {pct_str}'
        if v == 'green':
            green.append(f'✅ {line}')
        elif v == 'yellow':
            yellow.append(f'⚠️ {line}')
        elif v == 'red':
            red.append(f'🔴 {line}')
        else:
            unknown.append(f'❓ {line}')

    total = len(summaries)
    n_green, n_yellow, n_red = len(green), len(yellow), len(red)

    lines = [
        f'Reconcile витрины — {today}',
        f'Итог: {n_green} ок / {n_yellow} внимание / {n_red} дефект (из {total})',
        '',
    ]
    if red:
        lines.append('-- Дефекты --')
        lines.extend(red)
        lines.append('')
    if yellow:
        lines.append('-- Внимание --')
        lines.extend(yellow)
        lines.append('')
    if green:
        lines.append('-- OK --')
        lines.extend(green)
        lines.append('')
    if unknown:
        lines.append('-- Нет данных --')
        lines.extend(unknown)
        lines.append('')
    lines.append(
        'Приезды/продажи: ось arrival, by-design недобор −35…−60%, см. полный отчёт.'
    )
    return '\n'.join(lines)


# ── Главная точка входа ────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description='Reconcile все салоны реестра: Google Sheets vs fact_big_analytics'
    )
    parser.add_argument(
        '--salon', nargs='*', metavar='SALON',
        help='Имена салонов (по реестру). По умолчанию — все.',
    )
    parser.add_argument(
        '--months', nargs='*', metavar='YYYY-MM',
        help='Конкретные месяцы. По умолчанию — все 2026-XX кроме текущего.',
    )
    parser.add_argument(
        '--include-current', action='store_true',
        help='Включить текущий неполный месяц.',
    )
    parser.add_argument(
        '--verbose', '-v', action='store_true',
        help='Детальный лог чтения листа.',
    )
    parser.add_argument(
        '--skip-errors', action='store_true',
        help='Продолжать при ошибке чтения листа (пропускать салон).',
    )
    parser.add_argument(
        '--tg', action='store_true',
        help='Отправить краткий дайджест (по слою расхода) в личный Telegram.',
    )
    parser.add_argument(
        '--json', action='store_true',
        help='Вывести полный JSON-дамп summaries в stdout (вместо текстового отчёта).',
    )
    args = parser.parse_args()

    # Загружаем реестр
    with open(_REGISTRY_FILE, encoding='utf-8') as f:
        registry = json.load(f)
    all_salons = registry['salons']

    # Фильтр по --salon
    if args.salon:
        filter_names = {s.lower() for s in args.salon}
        salons_to_run = [s for s in all_salons if s['name'].lower() in filter_names]
        if not salons_to_run:
            logger.error('Салоны не найдены в реестре: %s', args.salon)
            logger.info('Доступные: %s', [s['name'] for s in all_salons])
            return 2
    else:
        salons_to_run = all_salons

    logger.info('Инициализация Google Sheets API...')
    service = _get_sheets_service()

    logger.info('Инициализация DB (fact_big_analytics)...')
    # Тест соединения
    with psycopg2.connect(**DB_DST) as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT COUNT(*) FROM public.fact_big_analytics')
            total = cur.fetchone()[0]
    logger.info('fact_big_analytics: %d строк', total)

    summaries: list[dict] = []
    full_report_lines: list[str] = []
    overall_fail = False

    for salon in salons_to_run:
        name = salon['name']
        vname = salon['vitrina_name']
        sid = salon['spreadsheet_id']
        gid = salon['gid']

        logger.info('--- [%s] spreadsheet_id=%s gid=%d ---', name, sid, gid)

        try:
            # 1. Читаем лист
            sheet_name = _gid_to_sheet_name(service, sid, gid)
            logger.info('[%s] Лист: %r', name, sheet_name)
            sheet_data = read_sheet_voronka(service, sid, sheet_name, salon_name=name, verbose=args.verbose)
            logger.info('[%s] Блоков месяцев из листа: %d — %s', name, len(sheet_data), sorted(sheet_data.keys()))

            # 2. Наша сторона
            our_data = fetch_our_side(DB_DST, vname, DIRECTIONS_COST)
            logger.info('[%s] Витрина месяцев: %s', name, sorted(our_data.keys()))

            # 3. Reconcile
            rows = reconcile_salon(
                sheet_data, our_data,
                target_months=args.months,
                include_current=args.include_current,
            )

            if not rows:
                logger.warning('[%s] Нет пересекающихся месяцев для сравнения', name)
                # Добавляем заглушку в summary
                summaries.append({
                    'salon': name, 'vitrina': vname, 'city': salon['city'], 'crm': salon['crm'],
                    'sigma_cost_pct': None, 'sigma_priezdy_pct': None, 'sigma_prodazhi_pct': None,
                    'verdict_ours': '?', 'verdict_findings': salon.get('findings_verdict', '?'),
                    'match': False, 'has_arrival_deficit': False,
                    'sheet_months': 0, 'note': 'нет пересекающихся месяцев',
                })
                continue

            # 4. Форматируем отчёт
            report_str, summary = format_salon_report(salon, rows, our_data, sheet_data)
            full_report_lines.append(report_str)
            summaries.append(summary)

            if summary['verdict_ours'] == 'red':
                overall_fail = True

        except Exception as exc:
            if args.skip_errors:
                logger.error('[%s] ОШИБКА (пропускаем): %s', name, exc)
                summaries.append({
                    'salon': name, 'vitrina': vname, 'city': salon['city'], 'crm': salon['crm'],
                    'sigma_cost_pct': None, 'sigma_priezdy_pct': None, 'sigma_prodazhi_pct': None,
                    'verdict_ours': '?', 'verdict_findings': salon.get('findings_verdict', '?'),
                    'match': False, 'has_arrival_deficit': False,
                    'sheet_months': 0, 'note': f'ОШИБКА: {exc}',
                })
            else:
                logger.error('[%s] ОШИБКА: %s', name, exc, exc_info=True)
                return 2

    # --json: полный JSON-дамп в stdout (для машинной обработки)
    if args.json:
        print(json.dumps(summaries, ensure_ascii=False, indent=2, default=str))
        return 1 if overall_fail else 0

    # Вывод полного отчёта по салонам
    print('\n'.join(full_report_lines))

    # Сводная таблица
    summary_str = format_summary(summaries)
    print(summary_str)

    # --tg: краткий дайджест в Telegram (по слою расхода, без детализации приездов)
    if args.tg:
        try:
            from config.tokens import (  # noqa: E402
                TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_PROXY_VARIANTS,
            )
            from data_check.reporter import send_telegram  # noqa: E402
            digest = format_tg_digest(summaries)
            send_telegram(
                digest,
                bot_token=TELEGRAM_BOT_TOKEN,
                chat_id=TELEGRAM_CHAT_ID,
                proxy_variants=TELEGRAM_PROXY_VARIANTS,
            )
            logger.info('Telegram-дайджест отправлен (%d символов)', len(digest))
        except Exception as tg_exc:
            logger.warning('Telegram-дайджест не отправлен: %s', tg_exc)

    return 1 if overall_fail else 0


if __name__ == '__main__':
    sys.exit(main())
