"""
step_data_check/checks/fields.py

Проверка NULL / пустых значений в ключевых полях local_gsheet_sites.
"""

from __future__ import annotations
import psycopg2
import psycopg2.extras


def run(db_params: dict) -> dict:
    """
    Возвращает:
    {
        "null_directologist": [{"domain": ..., "niche": ...}, ...],
        "null_manager_login": [...],
        "null_login_key": [...],        # login_key IS NULL или = 'Нет'
    }
    """
    with psycopg2.connect(**db_params) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    domain,
                    niche,
                    directologist,
                    project_manager AS manager_login,
                    login_key
                FROM local_gsheet_sites
                ORDER BY niche NULLS LAST, domain
            """)
            rows = cur.fetchall()

    null_directologist = [
        {'domain': r['domain'], 'niche': r['niche']}
        for r in rows
        if not (r['directologist'] or '').strip()
    ]
    null_manager_login = [
        {'domain': r['domain'], 'niche': r['niche']}
        for r in rows
        if not (r['manager_login'] or '').strip()
    ]
    null_login_key = [
        {'domain': r['domain'], 'niche': r['niche']}
        for r in rows
        if not (r['login_key'] or '').strip() or (r['login_key'] or '').strip() == 'Нет'
    ]

    return {
        'null_directologist': null_directologist,
        'null_manager_login': null_manager_login,
        'null_login_key': null_login_key,
    }
