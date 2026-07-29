# -*- coding: utf-8 -*-
"""
Копирует данные из локального big_analytics.public.metrika
в victory:ad_analytics_bi.public.metrika_yandex

Фильтр: только строки где директолог не пустой.
Добавляет login_key из local_gsheet_sites по домену.

Запуск: python copy_metrika.py
Подключение к Victory через SSH-туннель (ssh алиас 'victory' из ~/.ssh/config).
"""

import psycopg2
from psycopg2.extras import execute_values
import sys, os, subprocess, time, signal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config.settings import T_GSHEET_SITES, DB_DST, DB_LOCAL_SRC

TUNNEL_LOCAL_PORT = 5444   # локальный порт туннеля → victory:5432

DB_DST_TUNNEL = {**DB_DST, 'host': 'localhost', 'port': TUNNEL_LOCAL_PORT}

T_METRIKA = 'metrika_yandex'

CREATE_SQL = f"""
CREATE TABLE IF NOT EXISTS public.{T_METRIKA} (
    domain              TEXT PRIMARY KEY,
    login_key           TEXT,
    counter_id          BIGINT,
    counter_name        TEXT,
    directologist       TEXT,
    all_forms           BIGINT,
    crm_order_created   BIGINT,
    crm_order_paid      BIGINT,
    crm_spam_order      BIGINT,
    crm_order_canceled  BIGINT,
    updated_at          TIMESTAMP DEFAULT NOW()
);
ALTER TABLE public.{T_METRIKA} ADD COLUMN IF NOT EXISTS directologist      TEXT;
ALTER TABLE public.{T_METRIKA} ADD COLUMN IF NOT EXISTS all_forms          BIGINT;
ALTER TABLE public.{T_METRIKA} ADD COLUMN IF NOT EXISTS crm_order_created  BIGINT;
ALTER TABLE public.{T_METRIKA} ADD COLUMN IF NOT EXISTS crm_order_paid     BIGINT;
ALTER TABLE public.{T_METRIKA} ADD COLUMN IF NOT EXISTS crm_spam_order     BIGINT;
ALTER TABLE public.{T_METRIKA} ADD COLUMN IF NOT EXISTS crm_order_canceled BIGINT;
ALTER TABLE public.{T_METRIKA} ADD COLUMN IF NOT EXISTS updated_at         TIMESTAMP DEFAULT NOW();
"""


def _open_tunnel():
    """Открывает SSH-туннель victory:5432 → localhost:TUNNEL_LOCAL_PORT."""
    proc = subprocess.Popen(
        ['ssh', '-o', 'StrictHostKeyChecking=no',
         '-L', f'{TUNNEL_LOCAL_PORT}:localhost:5432',
         '-N', 'victory'],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(2)   # ждём установки туннеля
    print(f'SSH-tunnel open: localhost:{TUNNEL_LOCAL_PORT} -> victory:5432 (pid={proc.pid})')
    return proc


def main():
    tunnel = _open_tunnel()
    try:
        _run(tunnel)
    finally:
        tunnel.terminate()
        print('SSH-туннель закрыт')


def _run(tunnel):
    # ── 1. DST-соединение через SSH-туннель ───────────────────────────────────
    dst = psycopg2.connect(**DB_DST_TUNNEL)
    cur_dst = dst.cursor()

    cur_dst.execute(f"SELECT domain, login_key FROM {T_GSHEET_SITES} WHERE domain IS NOT NULL")
    login_map = {row[0]: row[1] for row in cur_dst.fetchall()}
    print(f'login_key известен для {len(login_map)} доменов из {T_GSHEET_SITES}')

    cur_dst.execute(CREATE_SQL)
    dst.commit()

    # ── 2. Читаем источник (WIN1251 для русских имён колонок) ─────────────────
    src = psycopg2.connect(**DB_LOCAL_SRC)
    src.set_client_encoding('WIN1251')
    cur_src = src.cursor()

    cur_src.execute("""
        SELECT
            domain,
            counter_id,
            "Все формы",
            "CRM: Заказ создан",
            "CRM: Заказ оплачен",
            "CRM: Спам заказ",
            "CRM: Заказ отменен",
            директолог,
            updated_at
        FROM public.metrika
        WHERE директолог IS NOT NULL AND TRIM(директолог) != ''
    """)
    src_rows = cur_src.fetchall()
    src.close()
    print(f'Прочитано из big_analytics.public.metrika: {len(src_rows)} строк с директологом')

    # ── 3. Upsert в DST ───────────────────────────────────────────────────────
    import re
    _BAD_LOGIN = re.compile(r'^-+$')

    def _valid_login(lk):
        if not lk:
            return False
        lk = lk.strip()
        if not lk:
            return False
        if lk == 'Нет':
            return False
        if _BAD_LOGIN.match(lk):
            return False
        return True

    rows = []
    skipped = 0
    for domain, counter_id, all_forms, crm_created, crm_paid, crm_spam, crm_canceled, directologist, updated_at in src_rows:
        login_key = login_map.get(domain)
        if not _valid_login(login_key):
            skipped += 1
            continue
        rows.append((
            domain,
            login_key,
            counter_id,
            None,                     # counter_name (нет в источнике)
            directologist,
            all_forms,
            crm_created,
            crm_paid,
            crm_spam,
            crm_canceled,
            updated_at,
        ))
    print(f'  Пропущено (невалидный login_key): {skipped}')

    execute_values(cur_dst, f"""
        INSERT INTO public.{T_METRIKA}
            (domain, login_key, counter_id, counter_name, directologist,
             all_forms, crm_order_created, crm_order_paid, crm_spam_order,
             crm_order_canceled, updated_at)
        VALUES %s
        ON CONFLICT (domain) DO UPDATE SET
            login_key          = COALESCE(EXCLUDED.login_key,          public.{T_METRIKA}.login_key),
            counter_id         = COALESCE(EXCLUDED.counter_id,         public.{T_METRIKA}.counter_id),
            directologist      = EXCLUDED.directologist,
            all_forms          = COALESCE(EXCLUDED.all_forms,          public.{T_METRIKA}.all_forms),
            crm_order_created  = COALESCE(EXCLUDED.crm_order_created,  public.{T_METRIKA}.crm_order_created),
            crm_order_paid     = COALESCE(EXCLUDED.crm_order_paid,     public.{T_METRIKA}.crm_order_paid),
            crm_spam_order     = COALESCE(EXCLUDED.crm_spam_order,     public.{T_METRIKA}.crm_spam_order),
            crm_order_canceled = COALESCE(EXCLUDED.crm_order_canceled, public.{T_METRIKA}.crm_order_canceled),
            updated_at         = EXCLUDED.updated_at
    """, rows)
    dst.commit()

    with_counter = sum(1 for r in rows if r[2] is not None)
    with_forms   = sum(1 for r in rows if r[5] is not None)
    print(f'Записано в {T_METRIKA}: {len(rows)} строк')
    print(f'  с counter_id: {with_counter}')
    print(f'  с all_forms:  {with_forms}')
    dst.close()


if __name__ == '__main__':
    main()
