"""
step8_stats/pipeline_log_snapshot.py — снимок воронки по месяцам в data_pipeline_log.

После того как big_analytics_full финализирован (step7: SET LOGGED + VACUUM),
агрегируем по месяцам и сохраняем в public.data_pipeline_log.
Используется дашбордом /api/pipeline-delta для сравнения текущего run vs предыдущего.

Фильтр: direction='Авто' AND направление != 'Пиксель_атрибуц'
        AND атрибуция = 'По дате заявки'
(direction='Авто' сужает до Авто; пиксель-атрибуция исключается через кириллическую
направление, как в PBI; ось снимка = ТОЛЬКО заявочная — big_analytics_unified несёт обе
оси, direction='Авто' их НЕ разделяет, единственный признак оси — колонка атрибуция).

Если run_id пуст — fallback NOW()::TEXT.
ON CONFLICT (run_id, month) DO NOTHING — повторный вызов идемпотентен.
"""

import logging
from datetime import datetime

logger = logging.getLogger('pipeline.snapshot')

# SNAPSHOT_ON_UNIFIED_2026-06-20: переключено с big_analytics_full на big_analytics_unified.
# SPEND_PREFREE_FULL (pipeline.py ~L1305) TRUNCATE'ит big_analytics_full ПЕРЕД spend-фазой,
# а pipeline_log_snapshot вызывается ПОСЛЕ spend-фазы (pipeline.py L1539).
# big_analytics_unified жива до cleanup_intermediate (идёт после verify/step8) — безопасно.
# АХТУНГ (DELTA_AXIS_FIX_2026-07-10): big_analytics_unified = заявочная (big_analytics_full) ∪
# визит-ось (big_analytics_full_arrival). direction='Авто' НЕ отсекает визит-ось — визит-строки
# тоже несут direction='Авто'. Единственный признак оси — колонка `атрибуция`
# ('По дате заявки' / 'По дате визита'). Поэтому ось снимка задаётся ЯВНЫМ фильтром
# `атрибуция='По дате заявки'` в WHERE ниже. Без него оси складываются → низ воронки задваивается
# (инцидент run c5f9fde8: priezd/prodazhi ~×2). направление != 'Пиксель_атрибуц' убирает пиксель-атрибуцию.
T_LOG = 'public.data_pipeline_log'
T_SRC = 'public.big_analytics_unified'

DDL = f"""
CREATE TABLE IF NOT EXISTS {T_LOG} (
    id           SERIAL PRIMARY KEY,
    run_id       TEXT NOT NULL,
    recorded_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    month        DATE NOT NULL,
    cost         NUMERIC(14,2),
    obrashenia   BIGINT,
    cpl_obr      NUMERIC(10,2),
    zayavki      BIGINT,
    cpl_zayavki  NUMERIC(10,2),
    kval         BIGINT,
    cpl_kval     NUMERIC(10,2),
    priezd       BIGINT,
    cpl_priezd   NUMERIC(10,2),
    prodazhi     BIGINT,
    cpl_prodazhi NUMERIC(10,2),
    UNIQUE(run_id, month)
);
-- PIXEL_DELTA_2026-07-10: пиксель-компонент отдельными колонками (для виджета «Исключить Пиксель»).
-- Идемпотентная миграция — на уже существующей таблице колонки создадутся автоматически.
ALTER TABLE {T_LOG}
  ADD COLUMN IF NOT EXISTS cost_pixel        NUMERIC(14,2),
  ADD COLUMN IF NOT EXISTS obrashenia_pixel  BIGINT,
  ADD COLUMN IF NOT EXISTS zayavki_pixel     BIGINT,
  ADD COLUMN IF NOT EXISTS kval_pixel        BIGINT,
  ADD COLUMN IF NOT EXISTS priezd_pixel      BIGINT,
  ADD COLUMN IF NOT EXISTS prodazhi_pixel    BIGINT;
CREATE INDEX IF NOT EXISTS idx_dpl_run_id ON {T_LOG}(run_id);
CREATE INDEX IF NOT EXISTS idx_dpl_recorded ON {T_LOG}(recorded_at DESC);
"""

INSERT_SQL = f"""
INSERT INTO {T_LOG}
  (run_id, month, cost, obrashenia, cpl_obr, zayavki, cpl_zayavki,
   kval, cpl_kval, priezd, cpl_priezd, prodazhi, cpl_prodazhi,
   cost_pixel, obrashenia_pixel, zayavki_pixel, kval_pixel, priezd_pixel, prodazhi_pixel)
SELECT
  %s AS run_id,
  date_trunc('month', "Date")::date AS month,
  SUM(total_cost)::numeric(14,2) AS cost,
  SUM(kol_vo_zayavok)::bigint AS obrashenia,
  CASE WHEN SUM(kol_vo_zayavok) > 0
       THEN (SUM(total_cost)/SUM(kol_vo_zayavok))::numeric(10,2) END AS cpl_obr,
  SUM(korr)::bigint AS zayavki,
  CASE WHEN SUM(korr) > 0
       THEN (SUM(total_cost)/SUM(korr))::numeric(10,2) END AS cpl_zayavki,
  SUM(kval)::bigint AS kval,
  CASE WHEN SUM(kval) > 0
       THEN (SUM(total_cost)/SUM(kval))::numeric(10,2) END AS cpl_kval,
  SUM(priezd)::bigint AS priezd,
  CASE WHEN SUM(priezd) > 0
       THEN (SUM(total_cost)/SUM(priezd))::numeric(10,2) END AS cpl_priezd,
  SUM(prodazhi)::bigint AS prodazhi,
  CASE WHEN SUM(prodazhi) > 0
       THEN (SUM(total_cost)/SUM(prodazhi))::numeric(10,2) END AS cpl_prodazhi,
  -- PIXEL_DELTA_2026-07-10: компонент ТОЛЬКО «Пиксель» (не 'Пиксель_атрибуц', та исключена в WHERE).
  -- Пиксель по счётчикам целочисленный → BIGINT корректно; cost — NUMERIC. CPL не храним (производный).
  (SUM(total_cost)     FILTER (WHERE направление = 'Пиксель'))::numeric(14,2) AS cost_pixel,
  (SUM(kol_vo_zayavok) FILTER (WHERE направление = 'Пиксель'))::bigint        AS obrashenia_pixel,
  (SUM(korr)           FILTER (WHERE направление = 'Пиксель'))::bigint        AS zayavki_pixel,
  (SUM(kval)           FILTER (WHERE направление = 'Пиксель'))::bigint        AS kval_pixel,
  (SUM(priezd)         FILTER (WHERE направление = 'Пиксель'))::bigint        AS priezd_pixel,
  (SUM(prodazhi)       FILTER (WHERE направление = 'Пиксель'))::bigint        AS prodazhi_pixel
FROM {T_SRC}
WHERE direction = 'Авто'
  AND (направление IS NULL OR направление <> 'Пиксель_атрибуц')  -- KOMPLEKS_REFACTOR_REDO_2026-07-09
  AND атрибуция = 'По дате заявки'  -- DELTA_AXIS_FIX_2026-07-10: только заявочная ось (иначе +визит-ось → двойной счёт)
  AND "Date" IS NOT NULL
GROUP BY date_trunc('month', "Date")
ON CONFLICT (run_id, month) DO NOTHING
"""


def run(conn, run_id=None) -> dict:
    """
    Записать снимок воронки по месяцам в data_pipeline_log.

    Возвращает {'rows': <int>, 'run_id': <str>, 'months': <int>}.
    """
    rid = (run_id or '').strip()
    if not rid:
        rid = datetime.now().isoformat()
    logger.info('pipeline_log_snapshot.run(run_id=%s)', rid)

    with conn.cursor() as cur:
        # DDL idempotent
        cur.execute(DDL)
        # INSERT-SELECT
        cur.execute(INSERT_SQL, (rid,))
        # Сколько строк за этот run_id
        cur.execute(
            f"SELECT COUNT(*), COUNT(DISTINCT month) FROM {T_LOG} WHERE run_id=%s",
            (rid,),
        )
        rows, months = cur.fetchone()
    conn.commit()

    logger.info('pipeline_log_snapshot: %s rows / %s months for run_id=%s',
                rows, months, rid)
    return {'rows': int(rows or 0), 'months': int(months or 0), 'run_id': rid}


if __name__ == '__main__':
    # Standalone-режим: подцепиться к БД из config и запустить
    import sys
    sys.path.insert(0, '/home/semen_vi/big_analytics_v5')
    import config.db as db_module
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    db_module.init_pool()
    c = db_module.get_conn()
    try:
        res = run(c, run_id=sys.argv[1] if len(sys.argv) > 1 else None)
        print(res)
    finally:
        db_module.put_conn(c)
        db_module.close_pool()
