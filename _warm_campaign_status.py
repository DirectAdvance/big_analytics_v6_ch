#!/usr/bin/env python3
"""
_warm_campaign_status.py — точечный прогрев campaign_status/payment_model БЕЗ step0/step8.

Контекст: партиал-прогон pipeline.py --from-step=3 не запускал prefetch (он висит на
блоке step_num==0), поэтому таблица campaign_status получила campaign_status/payment_model
= NULL для большинства кампаний (только tp8 'Активна' = ~906). Полный step0/step8 нельзя:
они читают SRC ad_analytics.yandex_direct_manager_reports, которая под чужим суперюзер-локом.

Этот скрипт делает ТОЛЬКО Фазу A+B step4 (Grid API direct.yandex.ru через куки — НЕ SRC):
  1. prefetch_statuses() — синхронно, наполняет _campaign_statuses_prefetch через Grid API
  2. step4.run(conn, run_id, prefetch_thread=None) — строит campaign_status из prefetch +
     патчит big_analytics_direct/crop campaign_status/payment_model

НЕ запускает step0/step1/step2/step8 — locked SRC не трогается.
"""
import sys
import time
import importlib

from config.db import get_conn, put_conn, init_pool

RUN_ID = 'warm_cs_' + time.strftime('%H%M%S')


def main() -> int:
    step4 = importlib.import_module('step4_campaign_status.step4')

    # Только DST-пул (ad_analytics_bi). init_src_pool НЕ вызываем — locked SRC не трогаем.
    init_pool()
    print(f'[warm] run_id={RUN_ID} — старт прогрева campaign_status (Grid API, без SRC), DST pool init', flush=True)

    # ── Фаза A: prefetch синхронно (Grid API + куки, пишет _campaign_statuses_prefetch) ──
    t0 = time.time()
    print('[warm] Фаза A: prefetch_statuses() ...', flush=True)
    step4.prefetch_statuses()
    print(f'[warm] Фаза A завершена за {time.time()-t0:.1f}с', flush=True)

    # Проверим, что prefetch-таблица реально создана и наполнена
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT EXISTS(SELECT 1 FROM information_schema.tables
                              WHERE table_name = '_campaign_statuses_prefetch')
            """)
            has_pref = cur.fetchone()[0]
            n_pref = 0
            if has_pref:
                cur.execute('SELECT COUNT(*), COUNT(campaign_status), COUNT(payment_model) '
                            'FROM _campaign_statuses_prefetch')
                n_pref = cur.fetchone()
        print(f'[warm] prefetch-таблица: exists={has_pref} '
              f'(rows/cs_notnull/pm_notnull={n_pref})', flush=True)
        if not has_pref:
            print('[warm] ОШИБКА: prefetch-таблица не создана — куки протухли или Grid недоступен. '
                  'Прерываю, НЕ строю campaign_status (старые данные сохранятся).', flush=True)
            return 2

        # ── Фаза B: run() с prefetch_thread=None → читает уже готовую prefetch-таблицу ──
        t1 = time.time()
        print('[warm] Фаза B: step4.run(conn, run_id, prefetch_thread=None) ...', flush=True)
        res = step4.run(conn, RUN_ID, prefetch_thread=None)
        print(f'[warm] Фаза B завершена за {time.time()-t1:.1f}с: {res}', flush=True)
    finally:
        put_conn(conn)

    print('[warm] ГОТОВО — campaign_status/payment_model наполнены. '
          'Теперь нужен повторный build_star для Dim_Campaign.', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
