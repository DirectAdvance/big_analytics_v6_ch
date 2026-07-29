#!/usr/bin/env python3
"""
build_ml_korrektirovki_night.py — ночное обновление public.fact_ml_korrektirovki.

Вызывается из pipeline_night.py ШАГ 5, ПОСЛЕ korrektirovki/run.py (шаг 3).
Порядок зависимостей:
  • yandex_direct_korrektirovki — только что обновлена шагом korrektirovki (шаг 3) ✓
  • fact_big_analytics — от последнего pipeline.py (не пересобирается здесь, но актуальна) ✓

Пересобирает public.fact_ml_korrektirovki:
  JOIN fact_big_analytics.RlAdjustmentId = yandex_direct_korrektirovki.audience_id
  WHERE korrektirovki_bid ILIKE '%_ml_%'
  + новые колонки ml_audience_name / bid_percent / ml_tier

TABLE (не VIEW): JOIN присутствует (правило CLAUDE.md §«VIEW вместо TABLE»).

Маркер: ML_KORREKTIROVKI_FACT_2026-07-27
"""
import sys
import time
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('build_ml_korrektirovki_night')


def _resolve_paths():
    """Портабл-резолвер путей (идентично build_star.py): корень проекта + .secret."""
    here = Path(__file__).resolve()
    proj_root = None
    secret_dir = None
    for parent in here.parents:
        if proj_root is None and (parent / 'config' / 'settings.py').exists():
            proj_root = parent
        cand = parent / '.secret'
        if secret_dir is None and (cand / 'loader.py').exists():
            secret_dir = cand
        if proj_root and secret_dir:
            break
    return proj_root, secret_dir


_PROJ_ROOT, _SECRET_DIR = _resolve_paths()
for _p in (str(_SECRET_DIR) if _SECRET_DIR else '/home/semen_vi/.secret',
           str(_PROJ_ROOT)   if _PROJ_ROOT   else '/home/semen_vi/big_analytics_v5'):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)

import psycopg2
from loader import load_db

_DB = load_db('victory')
_DB['database'] = 'ad_analytics_bi'

_WORK_MEM = '512MB'


def main():
    t0 = time.perf_counter()
    conn = psycopg2.connect(**_DB)
    conn.autocommit = False
    cur = conn.cursor()
    cur.execute(f"SET work_mem = '{_WORK_MEM}'")

    # GUARD 1: fact_big_analytics должна существовать (от последнего pipeline.py)
    cur.execute("SELECT to_regclass('public.fact_big_analytics')::text")
    if cur.fetchone()[0] is None:
        log.error(
            'fact_big_analytics отсутствует — пропуск build_ml_korrektirovki_night. '
            'Причина: pipeline.py ещё не запускался ни разу. Запустите pipeline.py вручную.'
        )
        conn.close()
        sys.exit(1)

    # GUARD 2: yandex_direct_korrektirovki должна быть (только что обновлена шагом 3)
    cur.execute("SELECT to_regclass('public.yandex_direct_korrektirovki')::text")
    if cur.fetchone()[0] is None:
        log.error(
            'yandex_direct_korrektirovki отсутствует — пропуск. '
            'Неожиданно: korrektirovki/run.py (шаг 3) должен был создать таблицу.'
        )
        conn.close()
        sys.exit(1)

    log.info('Пересборка public.fact_ml_korrektirovki ...')
    cur.execute('DROP TABLE IF EXISTS public.fact_ml_korrektirovki')

    # SQL идентичен build_star.py::build_ml_korrektirovki_fact (ветка _korr_exists=True).
    # DISTINCT ON audience_id: гарантирует 1:1 джойн (одна ML-аудитория может применяться
    # в нескольких кампаниях с разным bid_percent; берём первый по алфавиту bid_percent).
    cur.execute(r"""
        CREATE TABLE public.fact_ml_korrektirovki AS
        WITH ml_korr AS (
            SELECT DISTINCT ON (audience_id)
                audience_id,
                modifier_name,
                bid_percent,
                korrektirovki_bid
            FROM public.yandex_direct_korrektirovki
            WHERE korrektirovki_bid ILIKE '%_ml_%'
            ORDER BY audience_id, bid_percent NULLS LAST
        )
        SELECT
            f.*,
            k.modifier_name                                       AS ml_audience_name,
            k.bid_percent,
            (regexp_match(k.modifier_name, '_ml_all_(\d+p)'))[1]  AS ml_tier
        FROM public.fact_big_analytics f
        JOIN ml_korr k ON f."RlAdjustmentId" = k.audience_id
    """)

    cur.execute(
        'CREATE INDEX IF NOT EXISTS idx_fact_ml_korr_date '
        'ON public.fact_ml_korrektirovki USING brin ("Date")'
    )
    conn.commit()

    # VACUUM ANALYZE (autocommit — обязателен для VACUUM)
    conn.autocommit = True
    cur.execute('SET max_parallel_maintenance_workers = 0')
    cur.execute("SET maintenance_work_mem = '256MB'")
    cur.execute('VACUUM (ANALYZE, PARALLEL 0) public.fact_ml_korrektirovki')
    conn.autocommit = False

    cur.execute('SELECT COUNT(*) FROM public.fact_ml_korrektirovki')
    n = cur.fetchone()[0]
    log.info('fact_ml_korrektirovki: %d строк за %.1f сек', n, time.perf_counter() - t0)
    conn.close()


if __name__ == '__main__':
    main()
