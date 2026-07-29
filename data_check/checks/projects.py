"""
step_data_check/checks/projects.py

Проверка: домены из Google Sheets vs local_gsheet_sites в PostgreSQL.
"""

from __future__ import annotations
import psycopg2
import psycopg2.extras


SPREADSHEET_ID = '1wMAfpMyHEwa99NT0-kosTcA5Uny_r_6YXmbO5WKqlKc'
GID = 1519720357

# Возможные имена колонки-домена (в нижнем регистре)
_DOMAIN_KEYS = {'сайт', 'домен', 'domain', 'site', 'url', 'адрес'}


def run(db_params: dict, sheets_rows: list[dict]) -> dict:
    """
    Возвращает:
    {
        "only_in_sheets": [...],      # домены есть в Sheets, нет в local_gsheet_sites
        "only_in_db": [...],          # домены есть в local_gsheet_sites, нет в Sheets
        "no_analytics_data": [...],   # домены в обоих источниках, но 0 строк в big_analytics_full
    }
    """
    domain_col = _find_domain_col(sheets_rows)
    sheets_domains = {
        r[domain_col].strip().lower()
        for r in sheets_rows
        if domain_col in r and isinstance(r.get(domain_col), str) and r[domain_col].strip()
    }

    with psycopg2.connect(**db_params) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT LOWER(TRIM(domain)) AS domain FROM local_gsheet_sites")
            db_domains = {row['domain'] for row in cur.fetchall() if row['domain']}

            only_in_sheets = sorted(sheets_domains - db_domains)
            only_in_db = sorted(db_domains - sheets_domains)

            common = sheets_domains & db_domains
            if common:
                cur.execute(
                    """
                    SELECT LOWER(TRIM(domain)) AS domain
                    FROM big_analytics_full
                    WHERE LOWER(TRIM(domain)) = ANY(%s::text[])
                    GROUP BY 1
                    """,
                    (list(common),),
                )
                domains_with_data = {row['domain'] for row in cur.fetchall()}
                no_analytics_data = sorted(common - domains_with_data)
            else:
                no_analytics_data = []

    return {
        'only_in_sheets': only_in_sheets,
        'only_in_db': only_in_db,
        'no_analytics_data': no_analytics_data,
    }


def _find_domain_col(rows: list[dict]) -> str:
    """Ищет имя колонки-домена в первом dict без учёта регистра."""
    if not rows:
        return 'domain'
    first = rows[0]
    # точное сравнение в нижнем регистре по известным ключам
    for key in first.keys():
        if isinstance(key, str) and key.strip().lower() in _DOMAIN_KEYS:
            return key
    # fallback: первая колонка
    return next(iter(first))
