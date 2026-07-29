"""
yandex_direct_checking_report/report.py — независимый отчёт сверки расходов Директа

Назначение:
    Полная перезаливка таблицы `public.yandex_direct_checking_report` в БД
    `ad_analytics_bi` (Victory VPS). Расходы по аккаунтам, помесячно, без НДС.

Источник аккаунтов:
    public.local_gsheet_sites
      WHERE status='Контекст активно' AND direction='Авто'
      AND login_key IS NOT NULL AND login_key != ''

API:
    Яндекс.Директ Reports API v5
    POST https://api.direct.yandex.com/json/v5/reports
    ReportType=CAMPAIGN_PERFORMANCE_REPORT, FieldNames=['Month','Cost']
    Группировка по месяцу — на стороне API; локально только агрегируем по аккаунту.
    Заголовки: IncludeVAT=NO (без НДС), returnMoneyInMicros=false (рубли, не микрорубли).

Токены:
    Агентские (4 шт.), берутся из .secret/.env через loader.load_yandex_direct().
    Для каждого account_login — перебираем токены, первый давший 200 OK на
    Reports → этот используется. manager_login = логин агентки чей токен прошёл.

Поведение:
    TRUNCATE → загрузить целиком за период [2026-01-01 … вчера].
    Запускается как standalone скрипт, в pipeline.py НЕ включается.

Запуск:
    cd work/big_analytics_v5
    python -m yandex_direct_checking_report.report
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import psycopg2
import psycopg2.extras
import requests

# ── sys.path: модуль внутри big_analytics_v5 ──────────────────────────────────
_PKG_ROOT = Path(__file__).resolve().parents[1]
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from config.settings import DB_DST  # noqa: E402
from config.tokens import (  # noqa: E402
    OAUTH_TOKEN_1, OAUTH_TOKEN_2, OAUTH_TOKEN_3, OAUTH_TOKEN_4, OAUTH_TOKEN_5,
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_PROXY, TELEGRAM_PROXY_VARIANTS,
)

# ── Имена/константы ───────────────────────────────────────────────────────────
TABLE_NAME = 'yandex_direct_checking_report'
REPORTS_URL = 'https://api.direct.yandex.com/json/v5/reports'
DATE_FROM = '2026-01-01'

# (oauth_token, manager_login) — порядок = приоритет перебора
TOKENS: list[tuple[str, str]] = [
    (OAUTH_TOKEN_1, 'victorylotsofads1'),
    (OAUTH_TOKEN_2, 'victoryagency-direct1618440'),
    (OAUTH_TOKEN_3, 'y-direct-victory'),
    (OAUTH_TOKEN_4, 'victoryagency14'),
    (OAUTH_TOKEN_5, 'useful-call-agency'),
]

MAX_RETRY_HTTP5XX = 5
MAX_WORKERS = 4
PAUSE_PER_WORKER = 0.5  # сек между запросами — 2 rps на токен, 60% запас от лимита 5 rps

# ── Логгер ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger('yandex_direct_checking_report')


# ── DDL ───────────────────────────────────────────────────────────────────────
CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
    id            SERIAL PRIMARY KEY,
    domain        TEXT,
    account_login TEXT NOT NULL,
    manager_login TEXT,
    month         DATE NOT NULL,
    cost          NUMERIC(15,2) NOT NULL DEFAULT 0,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_login ON {TABLE_NAME}(account_login);
CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_month ON {TABLE_NAME}(month);
"""

INSERT_SQL = f"""
INSERT INTO {TABLE_NAME} (domain, account_login, manager_login, month, cost)
VALUES %s
"""

COMPARISON_SQL = """
WITH checking AS (
    SELECT account_login, month, cost
    FROM public.yandex_direct_checking_report
),
manager AS (
    SELECT
        account_login,
        DATE_TRUNC('month', "Date"::date)::date AS month,
        ROUND(SUM("Cost")::numeric, 2)          AS cost
    FROM public.yandex_direct_manager_reports
    GROUP BY account_login, DATE_TRUNC('month', "Date"::date)::date
)
SELECT
    c.account_login,
    c.month,
    c.cost                       AS cost_checking,
    COALESCE(m.cost, 0)          AS cost_manager,
    c.cost - COALESCE(m.cost, 0) AS delta
FROM checking c
LEFT JOIN manager m ON c.account_login = m.account_login AND c.month = m.month
WHERE ABS(c.cost - COALESCE(m.cost, 0)) > 0.01
ORDER BY ABS(c.cost - COALESCE(m.cost, 0)) DESC
"""

TG_MAX_ROWS = 40

SELECT_ACCOUNTS_SQL = """
SELECT DISTINCT ON (login_key) login_key, domain
FROM public.local_gsheet_sites
WHERE status = 'Контекст активно'
  AND direction = 'Авто'
  AND login_key IS NOT NULL AND TRIM(login_key) <> ''
  AND login_key ~ '[a-zA-Z0-9]'
ORDER BY login_key, domain
"""


# ── Дата ──────────────────────────────────────────────────────────────────────
def _process_account(
    account_login: str,
    domain: str,
    date_from: str,
    date_to: str,
    idx: int,
    total: int,
    worker_id: int = 0,
) -> tuple[str, str, Optional[str], Optional[dict]]:
    """Fetch one account via API. Thread-safe — no shared mutable state."""
    logger.info('[%d/%d] %s (%s) ...', idx, total, account_login, domain or '-')
    tsv, manager_login = fetch_report_with_token_rotation(
        account_login, date_from, date_to, worker_id=worker_id
    )
    if tsv is None:
        logger.warning('  [%s] → ни один токен не дал доступ', account_login)
        return account_login, domain, None, None
    monthly = parse_tsv_monthly(tsv)
    if monthly:
        logger.info('  [%s] → %d мес., итого %.2f ₽ (mgr=%s)',
                    account_login, len(monthly), sum(monthly.values()), manager_login)
    else:
        logger.info('  [%s] → нет расходов (mgr=%s)', account_login, manager_login)
    time.sleep(PAUSE_PER_WORKER)
    return account_login, domain, manager_login, monthly


def get_date_range() -> tuple[str, str]:
    """[DATE_FROM, вчера]."""
    yesterday = dt.date.today() - dt.timedelta(days=1)
    return DATE_FROM, yesterday.isoformat()


# ── Direct API ────────────────────────────────────────────────────────────────
def _make_headers(token: str, client_login: str) -> dict:
    return {
        'Authorization':       f'Bearer {token}',
        'Client-Login':        client_login,
        'Content-Type':        'application/json; charset=utf-8',
        'Accept-Language':     'ru',
        'returnMoneyInMicros': 'false',
        'skipReportHeader':    'true',
        'skipColumnHeader':    'false',
        'skipReportSummary':   'true',
    }


def _request_report(
    token: str,
    client_login: str,
    date_from: str,
    date_to: str,
) -> Optional[str]:
    """
    POST /reports. Возвращает TSV-текст при HTTP 200, иначе None.
    При 401/403/400 → None (этим токеном нет доступа — перебираем дальше).
    При 201/202/429 → ждём и повторяем.
    При 5xx — до MAX_RETRY_HTTP5XX попыток.
    """
    body = {
        'params': {
            'SelectionCriteria': {'DateFrom': date_from, 'DateTo': date_to},
            'FieldNames':        ['Month', 'Cost'],
            'ReportName':        f'chk_vat_{client_login}_{date_from}_{date_to}',
            'ReportType':        'CAMPAIGN_PERFORMANCE_REPORT',
            'DateRangeType':     'CUSTOM_DATE',
            'Format':            'TSV',
            'IncludeVAT':        'YES',
            'IncludeDiscount':   'NO',
        }
    }
    headers = _make_headers(token, client_login)

    conn_errors = 0
    attempt_5xx = 0
    while True:
        try:
            r = requests.post(REPORTS_URL, headers=headers, json=body, timeout=120)
        except requests.exceptions.ConnectionError as e:
            conn_errors += 1
            logger.warning('ConnectionError (%d/%d): %s', conn_errors, MAX_RETRY_HTTP5XX, e)
            if conn_errors >= MAX_RETRY_HTTP5XX:
                return None
            time.sleep(20)
            continue
        except Exception as e:
            logger.warning('Request exception: %s', e)
            return None

        if r.status_code == 200:
            return r.text
        if r.status_code in (201, 202):
            wait = min(int(r.headers.get('retryIn', 10)), 60)
            time.sleep(wait)
            continue
        if r.status_code == 429:
            wait = min(int(r.headers.get('Retry-After', 30)), 60)
            time.sleep(wait)
            continue
        if r.status_code in (500, 502, 503):
            attempt_5xx += 1
            if attempt_5xx >= MAX_RETRY_HTTP5XX:
                return None
            time.sleep(30 * attempt_5xx)
            continue
        # 400 / 401 / 403 — нет доступа этой паре (token, client_login)
        return None


def _rotated_tokens(worker_id: int) -> list[tuple[str, str]]:
    """Токены для воркера: primary = TOKENS[worker_id % N], остальные — fallback.
    Гарантирует что параллельные воркеры используют разные primary-токены."""
    n = len(TOKENS)
    return [TOKENS[(worker_id + i) % n] for i in range(n)]


def fetch_report_with_token_rotation(
    client_login: str,
    date_from: str,
    date_to: str,
    worker_id: int = 0,
) -> tuple[Optional[str], Optional[str]]:
    """
    Перебираем агентские токены до первого 200 OK.
    worker_id задаёт порядок токенов — каждый воркер стартует со своего.
    Возвращает (tsv_text, manager_login).  Если ни один не сработал → (None, None).
    """
    for token, manager_login in _rotated_tokens(worker_id):
        tsv = _request_report(token, client_login, date_from, date_to)
        if tsv is not None:
            return tsv, manager_login
    return None, None


# ── Парсинг ───────────────────────────────────────────────────────────────────
def parse_tsv_monthly(tsv_text: str) -> dict[dt.date, float]:
    """
    TSV (формат API):
        Month       Cost
        2026-01-01  12345.67
        2026-02-01  890.12
    Возвращает {month_date: total_cost_rub_no_vat}.
    """
    monthly: dict[dt.date, float] = {}
    reader = csv.DictReader(io.StringIO(tsv_text), delimiter='\t')
    for row in reader:
        month_raw = (row.get('Month') or '').strip()
        if not month_raw or month_raw == '--':
            continue
        cost_raw = (row.get('Cost') or '0').strip().replace(',', '.')
        if cost_raw in ('--', ''):
            continue
        try:
            month = dt.date.fromisoformat(month_raw)
            cost = float(cost_raw)
        except (ValueError, TypeError):
            continue
        monthly[month] = monthly.get(month, 0.0) + cost
    return monthly


# ── БД ────────────────────────────────────────────────────────────────────────
def get_active_accounts(conn) -> list[tuple[str, str]]:
    """[(login_key, domain), ...] — активные авто-аккаунты."""
    with conn.cursor() as cur:
        cur.execute(SELECT_ACCOUNTS_SQL)
        rows = cur.fetchall()
    result = []
    for login, domain in rows:
        if not login.isascii():
            logger.warning('Пропускаем не-ASCII login_key=%r', login)
            continue
        result.append((login, domain or ''))
    return result


def _make_conn() -> psycopg2.extensions.connection:
    return psycopg2.connect(
        host=DB_DST['host'], port=DB_DST['port'],
        database=DB_DST['database'], user=DB_DST['user'],
        password=DB_DST['password'], connect_timeout=30,
        keepalives=1, keepalives_idle=60, keepalives_interval=10, keepalives_count=5,
    )


def insert_rows(conn, rows: list[tuple]) -> None:
    if not rows:
        return
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, INSERT_SQL, rows, page_size=1000)
    conn.commit()


# ── Telegram ──────────────────────────────────────────────────────────────────
def _send_telegram(text: str) -> None:
    """Отправка в Telegram с ротацией прокси (Amsterdam→DE→NL→FR→direct)."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning('Telegram не настроен, пропускаем отправку')
        return
    url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage'
    payload = {'chat_id': TELEGRAM_CHAT_ID, 'text': text, 'parse_mode': 'HTML'}
    for proxies in TELEGRAM_PROXY_VARIANTS:  # TG_PROXY_CHAIN_ROTATION_2026-06-17
        try:
            r = requests.post(url, json=payload, proxies=proxies, timeout=30)
            if r.status_code == 200:
                logger.info('Telegram отправлен (proxies=%s)', proxies)
                return
            logger.warning('Telegram ошибка: %s %s', r.status_code, r.text[:200])
            return
        except Exception as e:
            logger.warning('Telegram исключение (proxies=%s): %s', proxies, e)
            continue


def _n(val: float) -> str:
    """Число с пробелами как разделитель тысяч (русский стиль): 1 066 485."""
    return f'{int(round(val)):,}'.replace(',', ' ')


def run_comparison(conn, failed_list: list[tuple[str, str]] | None = None) -> float:
    """Сверка расходов. Возвращает Σ разница (сумма дельт по всем строкам, может быть отриц.)."""
    today = dt.date.today().isoformat()
    logger.info('Сверка с yandex_direct_manager_reports ...')
    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '300000'")  # STMT_TIMEOUT_SVERKA_2026-07-03: 5 мин защита от FDW lock hang (yandex_direct_manager_reports — FDW на ad_analytics)
        cur.execute(COMPARISON_SQL)
        rows = cur.fetchall()

    failed_section = ''
    if failed_list:
        lines_f = [f'  • {login} ({domain or "-"})' for login, domain in failed_list]
        failed_section = f'\n\n⚠️ Нет доступа ({len(failed_list)} акк.):\n' + '\n'.join(lines_f)

    if not rows:
        text = f'📊 Сверка расходов Директа: {today}\n\n✅ Расхождений нет{failed_section}'
        logger.info('Сверка: расхождений нет')
        _send_telegram(text)
        return 0.0

    accounts_set = {r[0] for r in rows}
    total_delta = sum(r[4] for r in rows)
    sign_total = '+' if total_delta > 0 else ''
    header = (
        f'📊 Сверка расходов Директа: {today}\n'
        f'❌ {len(rows)} строк · {len(accounts_set)} аккаунтов\n'
        f'Σ разница: {sign_total}{_n(float(total_delta))} ₽\n\n'
    )

    # Таблица: account(14) month(7) api(9) mgr(9) delta(9) = ~51 символ
    lines = ['<code>']
    lines.append(f'{"Аккаунт":<14} {"Мес":<7} {"НашAPI":>9} {"MgrRep":>9} {"Δ":>9}')
    lines.append('─' * 51)
    for account_login, month, cost_checking, cost_manager, delta in rows[:TG_MAX_ROWS]:
        sign = '+' if delta > 0 else ''
        lines.append(
            f'{account_login[:14]:<14} {month.strftime("%Y-%m"):<7}'
            f' {_n(float(cost_checking)):>9}'
            f' {_n(float(cost_manager)):>9}'
            f' {sign}{_n(abs(float(delta))):>8}'
        )
    if len(rows) > TG_MAX_ROWS:
        lines.append(f'... и ещё {len(rows) - TG_MAX_ROWS} строк')
    lines.append('</code>')

    text = header + '\n'.join(lines) + failed_section
    if len(text) > 4090:
        text = text[:4087] + '...'

    logger.info('Сверка: %d расхождений, %d аккаунтов', len(rows), len(accounts_set))
    _send_telegram(text)
    return float(total_delta)


# ── Main ──────────────────────────────────────────────────────────────────────
def run() -> tuple[float, int, int]:
    date_from, date_to = get_date_range()
    logger.info('Период: %s … %s', date_from, date_to)
    logger.info('БД: %s/%s', DB_DST['host'], DB_DST['database'])

    conn = _make_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(CREATE_TABLE_SQL)
            cur.execute(f'TRUNCATE {TABLE_NAME} RESTART IDENTITY')
        conn.commit()
        logger.info('Таблица %s готова, TRUNCATE выполнен', TABLE_NAME)

        accounts = get_active_accounts(conn)
        logger.info('Активных аккаунтов (Контекст активно, Авто): %d', len(accounts))
        if not accounts:
            logger.warning('Нет аккаунтов для обработки — выход')
            return

        total_rows = 0
        ok_accounts = 0
        failed_accounts = 0
        no_data_accounts = 0
        failed_accounts_list: list[tuple[str, str]] = []
        all_insert_rows: list[tuple] = []

        logger.info('Параллельный fetch: %d воркеров, %d аккаунтов', MAX_WORKERS, len(accounts))
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_map = {
                executor.submit(
                    _process_account,
                    login, domain, date_from, date_to, i, len(accounts),
                    worker_id=(i - 1) % MAX_WORKERS,
                ): (login, domain)
                for i, (login, domain) in enumerate(accounts, 1)
            }
            for future in as_completed(future_map):
                account_login, domain, manager_login, monthly = future.result()
                if manager_login is None and monthly is None:
                    failed_accounts += 1
                    failed_accounts_list.append((account_login, domain))
                elif not monthly:
                    no_data_accounts += 1
                else:
                    rows = [
                        (domain or None, account_login, manager_login, month, round(cost, 2))
                        for month, cost in sorted(monthly.items())
                    ]
                    all_insert_rows.extend(rows)
                    total_rows += len(rows)
                    ok_accounts += 1

        # Единый batch-insert после завершения всех API-запросов
        if all_insert_rows:
            try:
                insert_rows(conn, all_insert_rows)
            except psycopg2.OperationalError as e:
                logger.warning('DB connection lost, reconnecting: %s', e)
                try:
                    conn.close()
                except Exception:
                    pass
                for attempt in range(1, 5):
                    try:
                        conn = _make_conn()
                        break
                    except psycopg2.OperationalError as re:
                        logger.warning('Reconnect attempt %d/4 failed: %s', attempt, re)
                        if attempt == 4:
                            raise
                        time.sleep(15 * attempt)
                insert_rows(conn, all_insert_rows)

        logger.info(
            'Готово: rows=%d, accounts ok=%d, no_data=%d, failed=%d',
            total_rows, ok_accounts, no_data_accounts, failed_accounts,
        )

        return run_comparison(conn, failed_list=failed_accounts_list or None), total_rows, ok_accounts
    finally:
        conn.close()
    return 0.0, 0, 0


if __name__ == '__main__':
    run()
