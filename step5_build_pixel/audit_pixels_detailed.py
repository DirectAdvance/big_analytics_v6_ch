#!/usr/bin/env python3
"""Detailed audit: pixel names by source_name AND utm_source."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config.db as db_module

db_module.init_pool()
conn = db_module.get_conn()

try:
    cur = conn.cursor()

    # Пиксели из конфига
    cur.execute('SELECT pixel_name FROM local_pixel_config ORDER BY pixel_name')
    config_pixels = [r[0] for r in cur.fetchall()]
    print(f'=== КОНФИГ (гугл-таблица): {len(config_pixels)} пикселей ===')
    for px in config_pixels:
        print(f'  {px}')

    print('\n' + '='*80)

    # Для каждого пиксела - проверить по source_name и utm_source
    results = []
    for pixel_name in config_pixels:
        # По source_name
        cur.execute("SELECT COUNT(*) FROM local_leads_all WHERE source_name = %s", (pixel_name,))
        by_source = cur.fetchone()[0]

        # По utm_source (case-insensitive)
        cur.execute("SELECT COUNT(*) FROM local_leads_all WHERE LOWER(utm_source) = LOWER(%s)", (pixel_name,))
        by_utm = cur.fetchone()[0]

        found_source = 'YES' if by_source > 0 else '-'
        found_utm = 'YES' if by_utm > 0 else '-'

        total = by_source + by_utm
        results.append((pixel_name, by_source, by_utm, found_source, found_utm, total))

    # Вывод таблицы
    print(f"\n{'Pixel Name':<40} {'source_name':<15} {'utm_source':<15} {'Total':<10}")
    print('-' * 80)
    for name, by_src, by_utm, f_src, f_utm, total in results:
        src_display = f'{by_src} ({f_src})' if f_src == 'YES' else '-'
        utm_display = f'{by_utm} ({f_utm})' if f_utm == 'YES' else '-'
        print(f"{name:<40} {src_display:<15} {utm_display:<15} {total:<10}")

    # Статистика
    print('\n' + '='*80)
    only_source = sum(1 for r in results if r[3] == 'YES' and r[4] == '-')
    only_utm = sum(1 for r in results if r[3] == '-' and r[4] == 'YES')
    both = sum(1 for r in results if r[3] == 'YES' and r[4] == 'YES')
    neither = sum(1 for r in results if r[3] == '-' and r[4] == '-')

    print(f'\n=== Статистика совпадений ===')
    print(f'Только по source_name:    {only_source}')
    print(f'Только по utm_source:     {only_utm}')
    print(f'По обоим:                 {both}')
    print(f'Ни по чему (MISSING):     {neither}')
    print(f'--')
    print(f'Найдено ХОТЯ БЫ ОДНИМ способом: {only_source + only_utm + both}')

    if neither > 0:
        print(f'\n=== MISSING (ни по source_name ни по utm_source) ===')
        for name, _, _, f_src, f_utm, _ in results:
            if f_src == '-' and f_utm == '-':
                print(f'  ❌ {name}')

finally:
    db_module.put_conn(conn)
    db_module.close_pool()
