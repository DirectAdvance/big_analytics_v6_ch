"""
step_data_check/checks/funnel.py

Проверка воронки заявок по проджектам + инварианты.
"""

from __future__ import annotations
import psycopg2
import psycopg2.extras


def check_invariants(row: dict) -> list[str]:
    """
    Проверяет инварианты воронки для одной строки.
    Возвращает список строк с описанием нарушений (пустой список = OK).
    """
    issues = []
    korr  = row.get('korr', 0) or 0
    kval  = row.get('kval', 0) or 0
    priezd = row.get('priezd', 0) or 0
    prodazhi = row.get('prodazhi', 0) or 0
    dohod = row.get('dohod_do_kredita', 0) or 0
    dobro = row.get('dobro', 0) or 0

    if kval > korr:
        issues.append(f'kval({kval}) > korr({korr})')
    if priezd > kval:
        issues.append(f'priezd({priezd}) > kval({kval})')
    if prodazhi > priezd:
        issues.append(f'prodazhi({prodazhi}) > priezd({priezd})')
    if prodazhi > 0 and priezd == 0:
        issues.append(f'prodazhi({prodazhi}) > 0 но priezd=0 (продажа без визита)')
    if dobro > dohod:
        issues.append(f'dobro({dobro}) > dohod_do_kredita({dohod})')
    return issues


def run(db_params: dict) -> dict:
    """
    Возвращает:
    {
        "invariant_violations": [
            {"проджект": ..., "issues": [...], "metrics": {...}}, ...
        ],
        "zero_funnel_active": [{"проджект": ...}, ...],
        "per_project": [{"проджект": ..., "korr": ..., ...}, ...],
    }
    """
    with psycopg2.connect(**db_params) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    COALESCE(проджект, domain) AS проджект,
                    SUM(kol_vo_zayavok)     AS kol_vo_zayavok,
                    SUM(korr)               AS korr,
                    SUM(kval)               AS kval,
                    SUM(priezd)             AS priezd,
                    SUM(prodazhi)           AS prodazhi,
                    SUM(dohod_do_kredita)   AS dohod_do_kredita,
                    SUM(dobro)              AS dobro
                FROM big_analytics_full
                WHERE "Date" >= CURRENT_DATE - 30
                GROUP BY 1
                ORDER BY 1
            """)
            rows = [dict(r) for r in cur.fetchall()]

    invariant_violations = []
    zero_funnel_active = []

    for row in rows:
        proj = row['проджект']
        issues = check_invariants(row)
        if issues:
            invariant_violations.append({
                'проджект': proj,
                'issues': issues,
                'metrics': {k: int(row[k] or 0) for k in
                            ('kol_vo_zayavok', 'korr', 'kval', 'priezd',
                             'prodazhi', 'dohod_do_kredita', 'dobro')},
            })
        if not (row.get('kol_vo_zayavok') or 0):
            zero_funnel_active.append({'проджект': proj})

    return {
        'invariant_violations': invariant_violations,
        'zero_funnel_active': zero_funnel_active,
        'per_project': rows,
    }
