"""
step_data_check/checks/spending.py

Проверка расходов и лидов по проджектам за 7 и 30 дней.
"""

from __future__ import annotations
import psycopg2
import psycopg2.extras


def run(db_params: dict) -> dict:
    """
    Возвращает:
    {
        "spend_no_leads_7d": [{"проджект": ..., "total_cost": ..., "leads": 0}, ...],
        "no_spend_7d": [{"проджект": ..., "total_cost": 0}, ...],
        "per_project": [{"проджект": ..., "cost_7d": ..., "leads_7d": ...,
                          "cost_30d": ..., "leads_30d": ...}, ...],
    }
    """
    with psycopg2.connect(**db_params) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    COALESCE(проджект, domain) AS проджект,
                    SUM(CASE WHEN "Date" >= CURRENT_DATE - 7
                             THEN COALESCE(total_cost, 0) END)  AS cost_7d,
                    SUM(CASE WHEN "Date" >= CURRENT_DATE - 30
                             THEN COALESCE(total_cost, 0) END)  AS cost_30d,
                    SUM(CASE WHEN "Date" >= CURRENT_DATE - 7
                             THEN COALESCE(kol_vo_zayavok, 0) END) AS leads_7d,
                    SUM(CASE WHEN "Date" >= CURRENT_DATE - 30
                             THEN COALESCE(kol_vo_zayavok, 0) END) AS leads_30d
                FROM big_analytics_full
                GROUP BY 1
                ORDER BY 1
            """)
            rows = cur.fetchall()

    spend_no_leads_7d = [
        {'проджект': r['проджект'],
         'total_cost': float(r['cost_7d'] or 0),
         'leads': r['leads_7d']}
        for r in rows
        if (r['cost_7d'] or 0) > 0 and (r['leads_7d'] or 0) == 0
    ]
    no_spend_7d = [
        {'проджект': r['проджект'], 'total_cost': 0}
        for r in rows
        if (r['cost_7d'] or 0) == 0
    ]
    per_project = [
        {
            'проджект': r['проджект'],
            'cost_7d': float(r['cost_7d'] or 0),
            'leads_7d': r['leads_7d'] or 0,
            'cost_30d': float(r['cost_30d'] or 0),
            'leads_30d': r['leads_30d'] or 0,
        }
        for r in rows
    ]

    return {
        'spend_no_leads_7d': spend_no_leads_7d,
        'no_spend_7d': no_spend_7d,
        'per_project': per_project,
    }
