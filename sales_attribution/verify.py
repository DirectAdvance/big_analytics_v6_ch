"""
sales_attribution/verify.py

Сверка ключевых показателей analytics_sales_attribution
с источником big_analytics_full ("Название crm" = 'Фаиг').

Запуск:
  cd big_analytics_v5 && ~/venv/bin/python3 sales_attribution/verify.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2
from config.settings import DB_DST


def main() -> None:
    conn = psycopg2.connect(**DB_DST)
    cur = conn.cursor()

    # 1. Расходы Фаиг в BAF
    cur.execute("""
        SELECT ROUND(COALESCE(SUM(total_cost), 0), 2)
        FROM big_analytics_full
        WHERE "Название crm" = 'Фаиг'
    """)
    baf_cost = cur.fetchone()[0]

    cur.execute("""
        SELECT ROUND(COALESCE(SUM(cost_alloc), 0), 2)
        FROM analytics_sales_attribution
    """)
    our_cost = cur.fetchone()[0]

    # 2. Sale count
    cur.execute("""
        SELECT COUNT(*) FROM sale_xlsx
        WHERE utm_medium IS NOT NULL AND utm_medium <> ''
          AND COALESCE(utm_source, '') NOT IN ('victory-corp', 'leadgen')
    """)
    n_sale_src = cur.fetchone()[0]

    cur.execute("""
        SELECT COUNT(*) FROM analytics_sales_attribution WHERE row_type = 'sale'
    """)
    n_sale_dst = cur.fetchone()[0]

    # 3. Воронка
    cur.execute("""
        SELECT
            COALESCE(SUM(kol_vo_zayavok), 0),
            COALESCE(SUM(korr), 0),
            COALESCE(SUM(priezd), 0),
            COALESCE(SUM(prodazhi), 0)
        FROM big_analytics_full
        WHERE "Название crm" = 'Фаиг'
    """)
    baf_zay, baf_korr, baf_priezd, baf_prod = cur.fetchone()

    # 4. Распределение row_type
    cur.execute("""
        SELECT row_type,
               COUNT(*),
               ROUND(COALESCE(SUM(cost_alloc), 0), 2),
               COALESCE(SUM(marzha), 0),
               COALESCE(SUM(zayavok_camp_date), 0),
               COALESCE(SUM(priezd_camp_date), 0),
               COALESCE(SUM(prodazhi_camp_date), 0)
        FROM analytics_sales_attribution
        GROUP BY row_type
        ORDER BY 2 DESC
    """)
    stats = cur.fetchall()

    # 5. Sales без матча по camp_id (organic + utm не разпарсилась)
    cur.execute("""
        SELECT COUNT(*),
               COUNT(*) FILTER (WHERE is_organic),
               COUNT(*) FILTER (WHERE camp_id IS NULL AND NOT is_organic)
        FROM analytics_sales_attribution
        WHERE row_type = 'sale' AND cost_alloc = 0
    """)
    no_alloc, organic_no_alloc, other_no_alloc = cur.fetchone()

    # 6. Топ салонов по cost_alloc
    cur.execute("""
        SELECT salon,
               COUNT(*) FILTER (WHERE row_type = 'sale')              AS n_sales,
               ROUND(SUM(cost_alloc), 2)                              AS cost_total,
               COALESCE(SUM(marzha), 0)                               AS marzha_total,
               COALESCE(SUM(marzha), 0) - ROUND(SUM(cost_alloc), 2)   AS profit
        FROM analytics_sales_attribution
        WHERE salon IS NOT NULL
        GROUP BY salon
        ORDER BY cost_total DESC
        LIMIT 10
    """)
    top_salons = cur.fetchall()

    print('━' * 78)
    print('ПРОВЕРКА public.analytics_sales_attribution')
    print('━' * 78)

    print()
    print('1. Расходы (Фаиг)')
    print(f'   BAF "Название crm"=Фаиг SUM(total_cost):  {float(baf_cost or 0):>16,.2f} ₽')
    print(f'   analytics_sales_attribution SUM(cost_alloc): {float(our_cost or 0):>13,.2f} ₽')
    diff = float(baf_cost or 0) - float(our_cost or 0)
    pct  = abs(diff) / float(baf_cost or 1) * 100
    print(f'   Разница:                                  {diff:>16,.2f} ₽ ({pct:.4f}%)')
    print('   ✓ Совпадает' if pct < 0.01 else '   ✗ ОТКЛОНЕНИЕ — расследовать')

    print()
    print('2. Продажи (sale_xlsx после фильтра)')
    print(f'   sale_xlsx (фильтр utm):                   {n_sale_src:>16,}')
    print(f'   row_type=sale:                            {n_sale_dst:>16,}')
    print('   ✓ Совпадает' if n_sale_src == n_sale_dst
          else f'   ✗ ОТКЛОНЕНИЕ ({n_sale_src - n_sale_dst})')

    print()
    print('3. Воронка BAF Фаиг (источник)')
    print(f'   Заявок:    {baf_zay:>16,}')
    print(f'   Корректных:{baf_korr:>16,}')
    print(f'   Приездов:  {baf_priezd:>16,}')
    print(f'   Продаж:    {baf_prod:>16,}')

    print()
    print('4. Распределение по row_type:')
    header = f'   {"row_type":<22} {"count":>9} {"cost_alloc":>16} {"marzha":>13} {"zayavok":>10} {"priezd":>9} {"продажи":>9}'
    print(header)
    print('   ' + '─' * 75)
    for r in stats:
        print(f'   {r[0]:<22} {r[1]:>9,} {float(r[2]):>16,.2f} {r[3]:>13,} {r[4]:>10,} {r[5]:>9,} {r[6]:>9,}')

    print()
    print(f'5. Продажи без cost_alloc (cost=0):    {no_alloc:>6}')
    print(f'   из них органика (seo/organic):       {organic_no_alloc:>6}')
    print(f'   остальные (нет матча по camp_id):    {other_no_alloc:>6}')

    print()
    print('6. ТОП-10 салонов по cost_alloc:')
    print(f'   {"салон":<28} {"продаж":>7} {"расход":>16} {"маржа":>12} {"прибыль":>14}')
    print('   ' + '─' * 80)
    for r in top_salons:
        print(f'   {(r[0] or "—"):<28} {r[1]:>7} {float(r[2]):>16,.2f} {r[3]:>12,} {float(r[4]):>14,.2f}')

    print()
    print('━' * 78)

    cur.close()
    conn.close()


if __name__ == '__main__':
    main()
