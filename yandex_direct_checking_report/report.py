"""
yandex_direct_checking_report/report.py — независимый отчёт сверки расходов Директа

Назначение:
    Полная перезаливка таблицы `public.yandex_direct_checking_report` в БД
    `ad_analytics_bi` (Victory VPS). Расходы по аккаунтам, помесячно, с НДС.

Источник аккаунтов:
    reference_data.gsheet_sites
      WHERE status='Контекст активно' AND direction='Авто'
      AND login_key IS NOT NULL AND login_key != ''

Источник расходов:
    raw_data.yandex_direct_report_rows.total_cost — расход с НДС и комиссией.
    Группировка по месяцу делается в ClickHouse.

Поведение:
    TRUNCATE → загрузить целиком за период [2026-01-01 … вчера].
    Запускается как standalone скрипт, в pipeline.py НЕ включается.

Запуск:
    cd work/big_analytics_v6_ch
    python -m yandex_direct_checking_report.report
"""
from __future__ import annotations

import datetime as dt
import logging
import sys
import time
from pathlib import Path

import psycopg2
import psycopg2.extras

# ── sys.path: модуль внутри big_analytics_v6_ch ───────────────────────────────
_PKG_ROOT = Path(__file__).resolve().parents[1]
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from config.ch_db import get_client  # noqa: E402
from config.settings import DB_DST  # noqa: E402
from config.tokens import (  # noqa: E402
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_PROXY_VARIANTS,
)

# ── Имена/константы ───────────────────────────────────────────────────────────
TABLE_NAME = 'yandex_direct_checking_report'
DATE_FROM = '2026-01-01'

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


def _load_report_rows(date_from: str, date_to: str) -> tuple[list[tuple], int]:
    """Rows for PostgreSQL insert, built from ClickHouse raw_data."""
    client = get_client()
    active_accounts_sql = """
        SELECT count()
        FROM
        (
            SELECT DISTINCT lowerUTF8(trim(login_key)) AS login
            FROM reference_data.gsheet_sites
            WHERE status = 'Контекст активно'
              AND direction = 'Авто'
              AND ifNull(login_key, '') != ''
              AND match(ifNull(login_key, ''), '[a-zA-Z0-9]')
        )
    """
    active_accounts = client.query(active_accounts_sql).result_rows[0][0]
    rows_sql = f"""
        WITH active_accounts AS
        (
            SELECT
                lowerUTF8(trim(login_key)) AS login,
                anyLast(nullIf(ifNull(domain, ''), '')) AS domain
            FROM reference_data.gsheet_sites
            WHERE status = 'Контекст активно'
              AND direction = 'Авто'
              AND ifNull(login_key, '') != ''
              AND match(ifNull(login_key, ''), '[a-zA-Z0-9]')
            GROUP BY login
        )
        SELECT
            aa.domain AS domain,
            lowerUTF8(trim(rr.client_login)) AS account_login,
            anyLast(nullIf(rr.manager_login, '')) AS manager_login,
            toStartOfMonth(toDate(rr.day)) AS month,
            round(sum(rr.total_cost), 2) AS cost
        FROM raw_data.yandex_direct_report_rows AS rr
        INNER JOIN active_accounts AS aa ON aa.login = lowerUTF8(trim(rr.client_login))
        WHERE toDate(rr.day) >= toDate('{date_from}')
          AND toDate(rr.day) <= toDate('{date_to}')
          AND rr.total_cost > 0
        GROUP BY account_login, month, domain
        ORDER BY account_login, month
    """
    return client.query(rows_sql).result_rows, int(active_accounts)


def get_date_range() -> tuple[str, str]:
    """[DATE_FROM, вчера]."""
    yesterday = dt.date.today() - dt.timedelta(days=1)
    return DATE_FROM, yesterday.isoformat()


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
    """Pre-built HTML report (fixed-width `<code>` table) — one sender for the
    project: `notifications.telegram.send_html` (sanitize + chunk + proxy retry)."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning('Telegram не настроен, пропускаем отправку')
        return
    from notifications.telegram import send_html
    # timeout=30 (was silently 10s default post-migration, matches the original
    # requests.post(..., timeout=30) this replaced).
    # collapse_whitespace=False (WHITESPACE_IS_CONTENT_2026-08-14): the failed-
    # accounts bullet list ('  • login (domain)') uses a leading 2-space indent
    # as layout; the big table is separately safe inside multi-line <code>
    # regardless, since that's only skipped-normalize either way.
    if send_html(text, bot_token=TELEGRAM_BOT_TOKEN, chat_id=TELEGRAM_CHAT_ID,
                proxy_variants=TELEGRAM_PROXY_VARIANTS, timeout=30, collapse_whitespace=False):
        logger.info('Telegram отправлен')
    else:
        logger.warning('Telegram не доставлен ни через один прокси')


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

    # Таблица: account(14) month(7) raw(9) mgr(9) delta(9) = ~51 символ
    lines = ['<code>']
    lines.append(f'{"Аккаунт":<14} {"Мес":<7} {"RawData":>9} {"MgrRep":>9} {"Δ":>9}')
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
    # No more hard truncate-at-4090: send_html splits >4096-char messages into
    # several Telegram messages (tag-balanced <code> reopened per chunk) instead
    # of silently dropping table rows past the old length cutoff.

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

        all_insert_rows, active_accounts = _load_report_rows(date_from, date_to)
        ok_accounts = len({row[1] for row in all_insert_rows})
        no_data_accounts = active_accounts - ok_accounts
        total_rows = len(all_insert_rows)
        logger.info(
            'ClickHouse raw_data: active_accounts=%d, accounts_with_cost=%d, rows=%d',
            active_accounts, ok_accounts, total_rows,
        )
        if not active_accounts:
            logger.warning('Нет аккаунтов для обработки — выход')
            return 0.0, 0, 0

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
            'Готово: rows=%d, accounts_with_cost=%d, no_data=%d',
            total_rows, ok_accounts, no_data_accounts,
        )

        return run_comparison(conn), total_rows, ok_accounts
    finally:
        conn.close()
    return 0.0, 0, 0


if __name__ == '__main__':
    run()
