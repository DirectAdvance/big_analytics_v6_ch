"""
verify_star.py — ШАГ 6 верификация без потерь.

Сверяет агрегаты НОВОЕ (public.fact_big_analytics) vs СТАРОЕ (big_analytics_unified):
- глобальные суммы по атрибуция;
- помесячно × атрибуция;
- по _source_table;
- по салону (через Dim_Site join домена) — суммарно.
Метрики: total_cost, korr, kval, priezd, prodazhi, kol_vo_zayavok.

Также печатает эталонные числа без пикселя.
ARP: сверка cost/clicks/priezd/prodazhi vs analytics_report_placement.

Запуск: ~/venv/bin/python3 star_refactor/verify_star.py
"""
import sys
sys.path.insert(0, '/home/semen_vi/.secret')
sys.path.insert(0, '/home/semen_vi/big_analytics_v5')
import psycopg2
from loader import load_db

DB = load_db('victory'); DB['database'] = 'ad_analytics_bi'
conn = psycopg2.connect(**DB); cur = conn.cursor()

METRICS = ['total_cost', 'korr', 'kval', 'priezd', 'prodazhi', 'kol_vo_zayavok']
OLD = 'public.big_analytics_unified'
NEW = 'public.fact_big_analytics'

ok_all = True


def sums(table, where='TRUE', groupby=None):
    sel = ', '.join(f'ROUND(COALESCE(SUM({m}),0)::numeric,2)' for m in METRICS)
    g = f'{groupby},' if groupby else ''
    gb = f'GROUP BY {groupby}' if groupby else ''
    cur.execute(f"SELECT {g} {sel} FROM {table} WHERE {where} {gb}")
    return cur.fetchall()


def cmp(label, where='TRUE'):
    global ok_all
    o = sums(OLD, where)[0]
    n = sums(NEW, where)[0]
    same = all(abs(float(a) - float(b)) < 0.01 for a, b in zip(o, n))
    ok_all = ok_all and same
    print(f"\n[{label}] {'OK' if same else 'MISMATCH'}")
    for m, a, b in zip(METRICS, o, n):
        flag = '' if abs(float(a) - float(b)) < 0.01 else '  <-- DIFF'
        print(f"   {m:18} OLD={a}  NEW={b}{flag}")


def cmp_grouped(label, groupby):
    global ok_all
    o = {r[0]: r[1:] for r in sums(OLD, groupby=groupby)}
    n = {r[0]: r[1:] for r in sums(NEW, groupby=groupby)}
    keys = set(o) | set(n)
    bad = 0
    for k in keys:
        ov = o.get(k, (0,) * len(METRICS))
        nv = n.get(k, (0,) * len(METRICS))
        if any(abs(float(a) - float(b)) > 0.01 for a, b in zip(ov, nv)):
            bad += 1
            if bad <= 10:
                print(f"   DIFF {groupby}={k}: OLD={ov} NEW={nv}")
    status = 'OK' if bad == 0 else f'MISMATCH ({bad} groups)'
    ok_all = ok_all and bad == 0
    print(f"\n[{label}] {status} ({len(keys)} groups)")


print('=' * 70)
print('ШАГ 6 ВЕРИФИКАЦИЯ: public.fact_big_analytics vs big_analytics_unified')
print('=' * 70)

cmp('Всего (обе партиции)')
cmp('По дате заявки', where="\"атрибуция\"='По дате заявки'")
cmp('По дате визита', where="\"атрибуция\"='По дате визита'")
cmp_grouped('По атрибуция', '"атрибуция"')
cmp_grouped('По _source_table', '_source_table')
cmp_grouped('Помесячно (Date)', "to_char(\"Date\",'YYYY-MM')")
cmp_grouped('По домену', 'domain')

# ── Эталон без пикселя ───────────────────────────────────────────────────────
print('\n' + '=' * 70)
print('ЭТАЛОННЫЕ ЧИСЛА (для сверки с дашбордом)')
print('=' * 70)
for attr in ('По дате заявки', 'По дате визита'):
    cur.execute(f"""SELECT COALESCE(SUM(priezd),0), COALESCE(SUM(prodazhi),0),
                    ROUND(COALESCE(SUM(total_cost),0)::numeric,2)
                    FROM {NEW} WHERE "атрибуция"=%s""", (attr,))
    print(f"  [{attr}] priezd/prodazhi/cost (всё):", cur.fetchone())
    cur.execute(f"""SELECT COALESCE(SUM(priezd),0), COALESCE(SUM(prodazhi),0),
                    ROUND(COALESCE(SUM(total_cost),0)::numeric,2)
                    FROM {NEW} WHERE "атрибуция"=%s
                    AND (_source_table != 'pixel' OR _source_table IS NULL)""", (attr,))
    print(f"  [{attr}] priezd/prodazhi/cost (без пикселя):", cur.fetchone())

# ── ARP ──────────────────────────────────────────────────────────────────────
print('\n' + '=' * 70)
print('ARP: public.arp_fact vs analytics_report_placement')
print('=' * 70)
arp_metrics = ['cost', 'clicks', 'priezd', 'prodazhi', 'korr', 'kval']
for m in arp_metrics:
    cur.execute(f"SELECT ROUND(COALESCE(SUM({m}),0)::numeric,2) FROM public.analytics_report_placement")
    o = cur.fetchone()[0]
    cur.execute(f"SELECT ROUND(COALESCE(SUM({m}),0)::numeric,2) FROM public.arp_fact")
    n = cur.fetchone()[0]
    flag = '' if abs(float(o) - float(n)) < 0.01 else '  <-- DIFF'
    if flag:
        ok_all = False
    print(f"   {m:12} OLD={o}  NEW={n}{flag}")
cur.execute("SELECT count(*) FROM public.analytics_report_placement")
o = cur.fetchone()[0]
cur.execute("SELECT count(*) FROM public.arp_fact")
n = cur.fetchone()[0]
print(f"   rows         OLD={o}  NEW={n}  {'OK' if o == n else 'DIFF'}")

print('\n' + '=' * 70)
print('ИТОГ:', 'ВСЕ СВЕРКИ ПРОЙДЕНЫ — ПОТЕРЬ НЕТ' if ok_all else 'ЕСТЬ РАСХОЖДЕНИЯ — СМ. ВЫШЕ')
print('=' * 70)
conn.close()
