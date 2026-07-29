"""
criterion_spend/build_dim_criterion.py — измерение «критерий» для Power BI.

DIM_CRITERION_2026-06-29

ЦЕЛЬ: таблица-измерение public.dim_criterion — общий ключ связи 1:* к обеим
  таблицам фактов (fact_criterion_spend и fact_criterion_zayavki) по полю criterion.
  Без этого измерения воронка (fact_criterion_zayavki) и расход (fact_criterion_spend)
  в PBI Matrix не фильтруются совместно — у каждого факта свой «многие», их нельзя
  соединить напрямую fact-to-fact.

СТРУКТУРА (уникальный criterion = PK, покрывает ОБЕ таблицы фактов):
  criterion       TEXT PK — ОЧИЩЕННЫЙ текст (fact_criterion_spend.criterion /
                             fact_criterion_zayavki.criterion, оба нормализованы
                             одинаковой _CRIT_CLEAN / _UTM_TERM_EXPR логикой)
  criterion_type  TEXT    — autotargeting/retargeting/interests/keyword
                             (из spend — приоритет; из zayavki — fallback)
  criterion_raw   TEXT    — исходный текст до очистки (из spend; NULL если только в zayavki)

ИСТОЧНИК: UNION ALL fact_criterion_spend + fact_criterion_zayavki (оба WHERE criterion IS NOT NULL),
  DISTINCT ON (criterion) с приоритетом spend (priority=1 < priority=2).

СВЯЗИ В PBI (протянет пользователь вручную в Power BI Desktop):
  dim_criterion[criterion] 1 → * fact_criterion_spend[criterion]
  dim_criterion[criterion] 1 → * fact_criterion_zayavki[criterion]
  Срез по dim_criterion фильтрует оба факта одновременно — воронка и расход рядом.

ИДЕМПОТЕНТНОСТЬ: полный пересбор DROP+CTAS + PK + ANALYZE.
  DISKFREE_DROP_FIRST_2026-06-22: DROP в autocommit ДО CTAS.

ЗАПУСК (standalone, после того как обе таблицы фактов пересобраны):
  cd ~/big_analytics_v5 && ~/venv/bin/python3 criterion_spend/build_dim_criterion.py

НЕ ВКЛЮЧЁН в pipeline.py — standalone, вызывается после пересборки criterion-фактов
или добавляется в build_spend_daily.py вместе с остальными criterion-билдерами.
"""

# DIM_CRITERION_2026-06-29

import logging
import os
import sys
import time
from datetime import datetime

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import DB_DST  # noqa: E402

logger = logging.getLogger('pipeline.build_dim_criterion')

TARGET_TABLE   = 'dim_criterion'
SRC_SPEND      = 'fact_criterion_spend'
SRC_ZAYAVKI    = 'fact_criterion_zayavki'


# ── DDL ───────────────────────────────────────────────────────────────────────
DDL = f"""
CREATE TABLE IF NOT EXISTS public.{TARGET_TABLE} (
    criterion       TEXT PRIMARY KEY,
    criterion_type  TEXT,   -- autotargeting/retargeting/interests/keyword
    criterion_raw   TEXT    -- исходный текст до очистки (из fact_criterion_spend; NULL если только в zayavki)
)
"""


def _build_sql() -> str:
    """
    UNION ALL обеих таблиц-фактов (spend — приоритет 1, zayavki — приоритет 2),
    DISTINCT ON (lower(criterion)) с тай-брейком по priority (spend > zayavki) →
    каждый criterion ровно один раз, criterion_raw/criterion_type из spend где есть.
    """
    return f"""
    SELECT DISTINCT ON (lower(u.criterion))
        u.criterion,
        u.criterion_type,
        u.criterion_raw
    FROM (
        SELECT
            criterion,
            criterion_type,
            criterion_raw,
            1 AS priority
        FROM public.{SRC_SPEND}
        WHERE criterion IS NOT NULL
          AND trim(criterion) <> ''

        UNION ALL

        SELECT
            criterion,
            criterion_type,
            criterion_raw,
            2 AS priority
        FROM public.{SRC_ZAYAVKI}
        WHERE criterion IS NOT NULL
          AND trim(criterion) <> ''
    ) u
    ORDER BY lower(u.criterion), u.priority
    """


def run(conn, run_id: str = None) -> dict:
    """
    DIM_CRITERION_2026-06-29

    Идемпотентный пересбор dim_criterion:
    DROP в autocommit (DISKFREE_DROP_FIRST-паттерн) → CTAS → PK → ANALYZE.

    Возвращает {'rows': int, 'details': str, 'spend_only': int, 'zayavki_only': int, 'both': int}.
    """
    t0 = time.perf_counter()

    # DISKFREE_DROP_FIRST: DROP в autocommit, OS сразу освобождает место.
    import psycopg2 as _psycopg2_dc
    _drop_conn = _psycopg2_dc.connect(**DB_DST)
    _drop_conn.autocommit = True
    try:
        with _drop_conn.cursor() as _ddc:
            _ddc.execute(f'DROP TABLE IF EXISTS public.{TARGET_TABLE}')
        logger.info('DISKFREE_DROP_FIRST: DROP public.%s (autocommit)', TARGET_TABLE)
    finally:
        _drop_conn.close()

    with conn.cursor() as cur:
        logger.info('build_dim_criterion: CTAS public.%s', TARGET_TABLE)
        cur.execute(
            f'CREATE TABLE public.{TARGET_TABLE} AS {_build_sql()}'
        )
        rows = cur.rowcount
        cur.execute(
            f'ALTER TABLE public.{TARGET_TABLE} ADD CONSTRAINT {TARGET_TABLE}_pkey '
            f'PRIMARY KEY (criterion)'
        )
        # Статистика покрытия: сколько criterion только в spend, только в zayavki, в обоих
        cur.execute(f"""
            SELECT
                COUNT(*) FILTER (WHERE s.criterion IS NOT NULL AND z.criterion IS NULL) AS spend_only,
                COUNT(*) FILTER (WHERE s.criterion IS NULL AND z.criterion IS NOT NULL) AS zayavki_only,
                COUNT(*) FILTER (WHERE s.criterion IS NOT NULL AND z.criterion IS NOT NULL) AS both
            FROM public.{TARGET_TABLE} d
            LEFT JOIN (
                SELECT DISTINCT lower(criterion) AS criterion FROM public.{SRC_SPEND}
                WHERE criterion IS NOT NULL
            ) s ON lower(d.criterion) = s.criterion
            LEFT JOIN (
                SELECT DISTINCT lower(criterion) AS criterion FROM public.{SRC_ZAYAVKI}
                WHERE criterion IS NOT NULL
            ) z ON lower(d.criterion) = z.criterion
        """)
        spend_only, zayavki_only, both = cur.fetchone()
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(f'ANALYZE public.{TARGET_TABLE}')
    conn.commit()

    elapsed = time.perf_counter() - t0
    details = (
        f'rows={rows}; dim_criterion: UNION {SRC_SPEND}+{SRC_ZAYAVKI}, '
        f'spend_only={spend_only}, zayavki_only={zayavki_only}, both={both}; '
        f'DIM_CRITERION_2026-06-29'
    )
    logger.info(
        'build_dim_criterion: готово за %.1f сек, %d строк '
        '(spend_only=%d, zayavki_only=%d, both=%d)',
        elapsed, rows, spend_only, zayavki_only, both
    )
    return {'rows': rows, 'details': details, 'spend_only': spend_only,
            'zayavki_only': zayavki_only, 'both': both}


# ── Standalone-режим ──────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )
    start = datetime.now()
    logger.info('build_dim_criterion СТАРТ: %s', start)
    conn = psycopg2.connect(
        **DB_DST,
        options='-c statement_timeout=600000 -c client_encoding=utf8',
    )
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(DDL)  # IF NOT EXISTS
        conn.commit()
        res = run(conn, run_id='manual')
        logger.info('ЗАВЕРШЕНО: %s', res['details'])
    except Exception as e:
        conn.rollback()
        logger.exception('Ошибка build_dim_criterion: %s', e)
        raise
    finally:
        conn.close()


if __name__ == '__main__':
    main()
