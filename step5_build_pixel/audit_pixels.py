#!/usr/bin/env python3
"""Audit pixel names in local_pixel_config vs local_leads_all."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config.db as db_module

db_module.init_pool()
conn = db_module.get_conn()

try:
    cur = conn.cursor()

    # Пиксели из конфига (из гугл-таблицы)
    cur.execute('SELECT COUNT(*), COUNT(DISTINCT pixel_name) FROM local_pixel_config')
    cfg_total, cfg_distinct = cur.fetchone()

    # Пиксели в leads (source_name)
    cur.execute("SELECT COUNT(DISTINCT source_name) FROM local_leads_all WHERE source_name IS NOT NULL AND source_name != ''")
    leads_distinct = cur.fetchone()[0]

    # Детали: какие пиксели в конфиге
    cur.execute('SELECT pixel_name FROM local_pixel_config ORDER BY pixel_name')
    config_pixels = [r[0] for r in cur.fetchall()]

    # Какие source_name в leads
    cur.execute("SELECT DISTINCT source_name FROM local_leads_all WHERE source_name IS NOT NULL AND source_name != '' ORDER BY source_name")
    leads_pixels = [r[0] for r in cur.fetchall()]

    print('=== PIXEL CONFIG (из гугл-таблицы) ===')
    print(f'Total: {cfg_total} rows, {cfg_distinct} distinct pixels')
    print(f'Pixels:\n  ' + '\n  '.join(config_pixels))

    print('\n=== LOCAL LEADS (source_name — откуда берутся данные) ===')
    print(f'Distinct source_names: {leads_distinct}')
    print(f'Pixels:\n  ' + '\n  '.join(leads_pixels))

    # Сравнение
    config_set = set(config_pixels)
    leads_set = set(leads_pixels)
    missing_in_leads = config_set - leads_set
    only_in_leads = leads_set - config_set

    print('\n=== MISSING IN LEADS ===')
    print(f'(из конфига, но НЕ найдены в local_leads_all source_name)')
    print(f'Count: {len(missing_in_leads)}')
    if missing_in_leads:
        for px in sorted(missing_in_leads):
            print(f'  ❌ {px}')

    print('\n=== EXTRA IN LEADS ===')
    print(f'(в local_leads_all, но НЕ в конфиге)')
    print(f'Count: {len(only_in_leads)}')
    if only_in_leads:
        for px in sorted(only_in_leads):
            print(f'  ⚠️ {px}')

    print(f'\n=== Статистика ===')
    print(f'Конфиг ожидает:    {cfg_distinct} пикселей')
    print(f'Leads содержит:    {leads_distinct} пикселей')
    print(f'Совпадают:         {len(config_set & leads_set)} пикселей')
    print(f'Пропущены:         {len(missing_in_leads)} пикселей')
    print(f'Лишние в leads:    {len(only_in_leads)} пикселей')

finally:
    db_module.put_conn(conn)
    db_module.close_pool()
