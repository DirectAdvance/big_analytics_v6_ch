#!/usr/bin/env python3
"""
data_check/golden_reward.py — числовой golden-reward скорер для best-of-N.

GOLDEN_REWARD_SCORER_v2_2026-06-17  ← grep-маркер для деплой-проверки.

НАЗНАЧЕНИЕ
----------
Превращает pass/fail вердикт `verify_big_analytics.py` в СКАЛЯРНЫЙ reward,
которым можно РАНЖИРОВАТЬ кандидатов патча при best-of-N поиске.

Прогоны пайплайна идут по ОЧЕРЕДИ на общей БД Victory (параллельно нельзя —
источники затрут друг друга). Использование:
  1. Прогнать вариант N пайплайна (pipeline.py / fast_pipeline.py).
  2. Вызвать golden_reward → получить reward_N.
  3. Откатить/перезаписать БД следующим вариантом.
  4. Выбрать вариант с наибольшим reward.

ГАРАНТИИ
--------
- СТРОГО read-only к БД (conn.set_session(readonly=True, autocommit=True)).
  Скорер никогда не мутирует данные — иначе best-of-N исказит сам себя.
- Идемпотентен: повторный запуск без изменений данных возвращает тот же reward.
- Переиспользует GOLDEN_COST / GOLDEN_COST_TOL / GOLDEN_SALES_FLOOR из
  verify_big_analytics.py — эталоны живут в одном источнике правды.

ИСТОЧНИК ДАННЫХ — DURABLE
--------------------------
Скорер читает ТОЛЬКО durable-таблицы, которые НЕ TRUNCATE-нуются cleanup_intermediate:

  public.fact_big_analytics          ← лёгкий факт звезды (durable, не в cleanup)
  public.big_analytics_full_arrival  ← durable (PBI читает, не в cleanup pipeline.py)
  public.big_analytics_pixel_score   ← durable (LOGGED, не в cleanup)

Транзиентные таблицы (big_analytics_full, big_analytics_unified) между прогонами
pipeline.py содержат 0 строк → скорер на них дал бы reward=-1000/hard_fail=True
(FALSE NEGATIVE). Переход на durable устраняет это.

Маппинг durable-эквивалентов:
  T_UNIFIED (big_analytics_unified)   → fact_big_analytics (все атрибуции)
  T_FULL    (big_analytics_full)      → fact_big_analytics WHERE атрибуция='По дате заявки'
  T_ARRIVAL (big_analytics_full_arrival) → big_analytics_full_arrival (дублируется как есть)

СОГЛАСОВАННОСТЬ СРЕЗА (блок 1 vs _fetch_raw_metrics)
------------------------------------------------------
Оба читают public.fact_big_analytics БЕЗ датного фильтра — по GOLDEN_BASELINE.md §5
(эталон 25 422 774.00/54 достигается БЕЗ фильтра по "Date"; с фильтром ОШИБОЧНО
даёт 24 566 397/45). Расхождения между sub-score и блоком 1 нет.

ФОРМУЛА REWARD
--------------
reward = BASE_SCORE
       - cost_dist (рублей отклонения от эталона)
       - sales_slack * SALES_SLACK_PENALTY (недобор продаж × 100)
       - n_violations * VIOLATION_PENALTY (нарушений блоков гейта × 50)

  Где:
    cost_dist  = abs(cost - GOLDEN_COST)           [0; +∞), ₽
    sales_slack = max(0, GOLDEN_SALES_FLOOR - sales) [0; +∞), шт.
    n_violations = число блоков гейта с ok=False

  hard_fail=True (любой блок FAIL) → reward = HARD_FAIL_REWARD = -1000.0
  Такой вариант никогда не выбирается при best-of-N.

  Все варианты-кандидаты, прошедшие golden (n_violations=0 и sales>=floor),
  получают reward > 0 и ранжируются по близости расхода к эталону.

CLI
---
  python3 data_check/golden_reward.py           → JSON в stdout
  python3 data_check/golden_reward.py --pretty  → JSON с отступами (для человека)
  python3 data_check/golden_reward.py --tg      → + отправить краткий отчёт в Telegram

EXIT CODE
---------
  0 = скрипт выполнен без исключений (reward может быть отрицательным — это не crash)
  2 = ошибка подключения / исключение скрипта
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal

import psycopg2

# Корень big_analytics_v5 в sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import DB_DST  # noqa: E402

# Импортируем эталоны из единственного источника правды
from data_check.verify_big_analytics import (  # noqa: E402
    GOLDEN_COST,
    GOLDEN_COST_TOL,
    GOLDEN_SALES_FLOOR,
    GOLDEN_SPECIALIST,
    GOLDEN_SOURCES,
    ARRIVAL_ROWS_MIN,
    GRAND_SALES_LO,
    GRAND_SALES_HI,
    FRESHNESS_MAX_DAYS,
    Block,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger('golden_reward')

# ── Durable-таблицы (не TRUNCATE-нуются cleanup_intermediate) ────────────────
#
# fact_big_analytics содержит обе атрибуции:
#   атрибуция='По дате заявки'   ← эквивалент big_analytics_full (T_FULL)
#   атрибуция='По дате визита'   ← эквивалент big_analytics_full_arrival (T_ARRIVAL)
#
T_DURABLE = 'public.fact_big_analytics'
T_DURABLE_ARRIVAL = 'public.big_analytics_full_arrival'  # durable: PBI читает, не в cleanup

# Фильтры атрибуции для имитации T_FULL / T_UNIFIED / T_ARRIVAL через T_DURABLE
_ATTR_ZAYAVKA = "\"атрибуция\" = 'По дате заявки'"
_ATTR_VIZIT   = "\"атрибуция\" = 'По дате визита'"

# ── Параметры скоринга (настройки, не хардкод эталонов) ─────────────────────

BASE_SCORE: float = 1000.0           # Базовый балл «идеального» варианта (cost=эталон, все PASS)
SALES_SLACK_PENALTY: float = 100.0   # Штраф за каждую недостающую продажу ниже floor
VIOLATION_PENALTY: float = 50.0      # Штраф за каждый блок гейта с ok=False
HARD_FAIL_REWARD: float = -1000.0    # Reward при hard_fail (любой блок FAIL → не выбирать)

# Допуск расхода в блоке 1 скорера — шире, чем GOLDEN_COST_TOL (10 ₽) в verify.
# Обоснование: GOLDEN_COST_TOL отражает допуск при «идеальном» прогоне.
# Между прогонами пиксельный дрейф накапливается: GOLDEN_BASELINE.md (2026-06-17)
# фиксирует Δ+12.20 ₽ — выход за ±10 ₽ by-design. Для best-of-N ранжирования
# нам важен ОТНОСИТЕЛЬНЫЙ порядок кандидатов, а не абсолютный гейт.
# Поэтому блок 1 скорера использует SCORER_COST_TOL=20 ₽ (расширен vs verify).
# cost_dist всё равно штрафует reward через формулу (1 ₽ = -1 балл).
# Изменение здесь НЕ влияет на verify_big_analytics.py / pipeline-гейты.
SCORER_COST_TOL: Decimal = Decimal('20.00')  # ±20 ₽, шире verify (±10 ₽)

# cost_dist уже в рублях → вычитаем как есть (1 ₽ отклонения = -1 балл)
# Диапазон «хорошего» варианта при текущем дрейфе: reward ≈ 987.8 (cost_dist=12.2).


def _scalar(cur, sql, params=None):
    cur.execute(sql, params or ())
    row = cur.fetchone()
    return row[0] if row else None


# ── Сбор «сырых» измерений из durable-источника ──────────────────────────────

def _fetch_raw_metrics(cur) -> dict:
    """Забирает числовые значения для sub-score расчёта.

    Читает public.fact_big_analytics (durable) БЕЗ датного фильтра —
    по GOLDEN_BASELINE.md §5 (эталон 25 422 774.00/54 без фильтра по "Date").
    Срез идентичен эталонному SQL в GOLDEN_BASELINE.md.
    """
    cur.execute(
        f"""
        SELECT ROUND(SUM(total_cost)::numeric, 2) AS rashod,
               ROUND(SUM(prodazhi))               AS prodazhi
        FROM {T_DURABLE}
        WHERE "специалист" = %s
          AND {_ATTR_ZAYAVKA}
          AND _source_table IN %s
        """,
        (GOLDEN_SPECIALIST, GOLDEN_SOURCES),
    )
    row = cur.fetchone()
    cost = Decimal(str(row[0])) if row and row[0] is not None else Decimal('0')
    sales = int(row[1]) if row and row[1] is not None else 0
    return {'cost': cost, 'sales': sales}


# ── Durable-блоки гейта (переопределяют транзиентные T_FULL/T_UNIFIED) ───────
#
# Вместо импорта check_* из verify_big_analytics (они привязаны к транзиентным
# T_UNIFIED/T_FULL) — определяем durable-версии, читающие из fact_big_analytics.
# Логика и инварианты идентичны; отличие только в источнике данных.

def _check_1_golden_durable(cur) -> Block:
    """Блок 1: Golden Кудерко — читает из durable fact_big_analytics, БЕЗ датного фильтра.

    GOLDEN_BASELINE.md §5: эталон 25 422 774.00/54 достигается БЕЗ фильтра по "Date".
    Срез совпадает с _fetch_raw_metrics → согласованность sub-score и блока 1 гарантирована.
    """
    cur.execute(
        f"""
        SELECT ROUND(SUM(total_cost)::numeric, 2) AS rashod,
               ROUND(SUM(prodazhi))               AS prodazhi
        FROM {T_DURABLE}
        WHERE "специалист" = %s
          AND {_ATTR_ZAYAVKA}
          AND _source_table IN %s
        """,
        (GOLDEN_SPECIALIST, GOLDEN_SOURCES),
    )
    rashod, prodazhi = cur.fetchone()
    rashod = Decimal(str(rashod)) if rashod is not None else Decimal('0')
    prodazhi = int(prodazhi) if prodazhi is not None else 0
    # Скорер использует SCORER_COST_TOL (±20 ₽) — шире, чем verify (±10 ₽).
    # Обоснование: пиксельный дрейф между прогонами накапливается до +12 ₽ (by-design).
    # cost_dist всё равно вычитается из reward — кандидаты ранжируются по расхождению.
    rashod_ok = abs(rashod - GOLDEN_COST) <= SCORER_COST_TOL
    sales_ok = (prodazhi >= GOLDEN_SALES_FLOOR)
    ok = rashod_ok and sales_ok
    sales_status = 'OK' if sales_ok else f'FAIL (ниже floor {GOLDEN_SALES_FLOOR})'
    detail = (f'расход={rashod} (эталон {GOLDEN_COST}±{SCORER_COST_TOL}[scorer], '
              f'verify-tol={GOLDEN_COST_TOL}, '
              f'Δ={rashod - GOLDEN_COST:+}), '
              f'продажи={prodazhi} (floor>={GOLDEN_SALES_FLOOR}, {sales_status}) '
              f'[durable: fact_big_analytics, без датного фильтра]')
    return Block(1, 'Golden Кудерко', ok, detail)


def _check_2_conservation_durable(cur) -> Block:
    """Блок 2: Закон сохранения — только full_cost из durable (партиций нет → skip).

    fact_big_analytics не содержит промежуточных партиций (big_analytics_direct и т.д.
    TRUNCATE-нуты). Проверяем наличие данных и общую сумму расхода по заявка-оси.
    """
    full_cost = _scalar(cur,
        f"SELECT COALESCE(ROUND(SUM(total_cost)::numeric,2),0) FROM {T_DURABLE} WHERE {_ATTR_ZAYAVKA}")
    full_cost = Decimal(str(full_cost))
    n_rows = int(_scalar(cur, f"SELECT COUNT(*) FROM {T_DURABLE} WHERE {_ATTR_ZAYAVKA}") or 0)
    ok = n_rows > 0 and full_cost > 0
    detail = (f'fact_big_analytics [заявка]: rows={n_rows}, cost={full_cost} '
              f'(партиции источников не проверяются — durable-режим; '
              f'{"непусто" if ok else "ПУСТО — данных нет"})')
    return Block(2, 'Данные durable-факта непусты', ok, detail)


def _check_3_dup_directions_durable(cur) -> Block:
    """Блок 3: Дубли заявок по направлениям — skipped в durable-режиме.

    Проверка требует колонку key3, которая не проецируется в fact_big_analytics
    (build_star.py не включает key3 в SELECT). В durable-режиме скипаем без FAIL.
    Полная проверка доступна только в verify_big_analytics.py сразу после прогона
    (пока T_FULL содержит key3).
    """
    return Block(3, 'Дубли заявок/звонков по направлениям', True,
                 'skipped в durable-режиме (key3 недоступен в fact_big_analytics)')


def _check_4_calls_vs_posev_durable(cur) -> Block:
    """Блок 4: Звонки не задвоены на визит-оси.

    Использует fact_big_analytics WHERE атрибуция='По дате визита' (durable).
    big_analytics_full_arrival может быть пустой между прогонами, тогда как
    fact_big_analytics содержит визит-данные постоянно (строится build_star.py
    из big_analytics_full_arrival, не TRUNCATE-нуется).
    Колонка key3 отсутствует в fact_big_analytics → используем fid как ключ лида
    (fid присутствует в fact и несёт тот же смысл уникального идентификатора лида).
    Если fid NULL (часть direct/seo строк) — исключаем из проверки, как key3 NULL.
    """
    n = _scalar(cur, f"""
        SELECT COUNT(*) FROM (
            SELECT fid
            FROM {T_DURABLE}
            WHERE fid IS NOT NULL
              AND {_ATTR_VIZIT}
              AND (тип_заявки = 'Звонки' OR источник IN ('звонки', 'Звонки'))  -- CAPITALIZE_FIX_2026-07-10
            INTERSECT
            SELECT fid
            FROM {T_DURABLE}
            WHERE fid IS NOT NULL
              AND {_ATTR_VIZIT}
              AND "источник" LIKE 'Посевы_%'  -- KOMPLEKS_REFACTOR_REDO_2026-07-09
        ) x
    """)
    n = int(n or 0)
    ok = n == 0
    detail = (f'calls ∩ посевы по fid (визит-ось) = {n} (ожидание 0) '
              f'[durable: fact_big_analytics визит-ось]')
    return Block(4, 'Звонки не задвоены на визит-оси', ok, detail)


def _check_5_pixel_visit_durable(cur) -> Block:
    """Блок 5: Пиксель-визит присутствует — через fact_big_analytics визит-ось (durable).

    fact_big_analytics содержит атрибуцию 'По дате визита' (93k+ строк).
    big_analytics_full_arrival может быть пуста между прогонами.
    """
    arrival_rows = int(_scalar(cur,
        f"SELECT COUNT(*) FROM {T_DURABLE} WHERE {_ATTR_VIZIT}") or 0)
    cur.execute(f"""
        SELECT COUNT(*) AS rows,
               COALESCE(ROUND(SUM(prodazhi)),0) AS prodazhi,
               COALESCE(ROUND(SUM(priezd)),0)   AS priezd
        FROM {T_DURABLE}
        WHERE {_ATTR_VIZIT}
          AND направление IN ('Пиксель','Пиксель_атрибуц')  -- KOMPLEKS_REFACTOR_REDO_2026-07-09
    """)
    px_rows, px_sales, px_arr = cur.fetchone()
    px_rows = int(px_rows or 0)
    px_sales = int(px_sales or 0)
    px_arr = int(px_arr or 0)
    ok = arrival_rows >= ARRIVAL_ROWS_MIN
    detail = (f'визит: продажи={px_sales} приезд={px_arr} pixel_rows={px_rows}; '
              f'arrival_rows={arrival_rows} (ожид ~94k, FAIL если ~32k → порог >={ARRIVAL_ROWS_MIN}) '
              f'[durable: fact_big_analytics визит-ось]')
    return Block(5, 'Пиксель-визит присутствует', ok, detail)


def _check_6_pixel_fractional_durable(cur) -> Block:
    """Блок 6: Дробность пикселя — через fact_big_analytics (заявка-ось, durable)."""
    cur.execute(f"""
        SELECT COUNT(*) FILTER (WHERE prodazhi <> ROUND(prodazhi, 0)) AS frac_rows,
               COUNT(*)                                               AS px_rows,
               SUM(prodazhi)                                          AS px_sum
        FROM {T_DURABLE}
        WHERE {_ATTR_ZAYAVKA}
          AND направление IN ('Пиксель','Пиксель_атрибуц')  -- KOMPLEKS_REFACTOR_REDO_2026-07-09
    """)
    frac_rows, px_rows, px_sum = cur.fetchone()
    frac_rows = int(frac_rows or 0)
    px_rows = int(px_rows or 0)
    px_sum_d = Decimal(str(px_sum)) if px_sum is not None else Decimal('0')
    frac_part = px_sum_d - px_sum_d.to_integral_value(rounding='ROUND_FLOOR')
    ok = frac_rows > 0
    detail = (f'дробных пиксель-строк={frac_rows}/{px_rows} (усечение к int дало бы 0); '
              f'SUM={px_sum_d} (дробн.часть суммы={frac_part}) '
              f'[durable: fact_big_analytics заявка-ось]')
    return Block(6, 'Дробность пикселя не усечена', ok, detail)


def _check_7_funnel_i3_durable(cur) -> Block:
    """Блок 7: Инвариант воронки I3 — через fact_big_analytics (обе атрибуции, durable)."""
    # Построчно заявка-ось
    viol = int(_scalar(cur, f"""
        SELECT COUNT(*)
        FROM {T_DURABLE}
        WHERE {_ATTR_ZAYAVKA}
          AND NOT (korr >= kval AND kval >= priezd AND priezd >= prodazhi)
    """) or 0)

    # Агрегат обеих осей через fact (имитирует T_UNIFIED GROUP BY атрибуция)
    cur.execute(f"""
        SELECT "атрибуция",
               ROUND(SUM(korr))     AS korr,
               ROUND(SUM(kval))     AS kval,
               ROUND(SUM(priezd))   AS priezd,
               ROUND(SUM(prodazhi)) AS prodazhi
        FROM {T_DURABLE}
        GROUP BY 1
    """)
    agg_ok = True
    for _attr, korr, kval, priezd, prod in cur.fetchall():
        korr, kval, priezd, prod = (int(x or 0) for x in (korr, kval, priezd, prod))
        if not (korr >= kval >= priezd >= prod):
            agg_ok = False
    ok = (viol == 0) and agg_ok
    detail = (f'заявка-ось построчно нарушений={viol} (ожид 0); '
              f"агрегат обеих осей {'OK' if agg_ok else 'НАРУШЕН'} "
              f'[durable: fact_big_analytics]')
    return Block(7, 'Инвариант воронки I3', ok, detail)


def _check_8_grand_total_durable(cur) -> Block:
    """Блок 8: Grand total продаж заявка-оси — через fact_big_analytics (durable)."""
    gt = _scalar(cur, f"""
        SELECT COALESCE(ROUND(SUM(prodazhi)),0)
        FROM {T_DURABLE}
        WHERE {_ATTR_ZAYAVKA}
    """)
    gt = int(gt or 0)
    ok = GRAND_SALES_LO <= gt <= GRAND_SALES_HI
    detail = (f'grand total (все направления, заявка-ось)={gt} (baseline 3326, '
              f'допуск [{GRAND_SALES_LO};{GRAND_SALES_HI}]) [durable: fact_big_analytics]')
    return Block(8, 'Заявка-ось grand total продаж', ok, detail)


def _check_9_freshness_durable(cur) -> Block:
    """Блок 9: Свежесть данных — через fact_big_analytics (обе атрибуции, durable).

    big_analytics_full_arrival может быть пуста между прогонами → arr_rows берём
    из fact_big_analytics WHERE атрибуция='По дате визита' (durable, всегда актуально).
    """
    max_date = _scalar(cur, f'SELECT MAX("Date") FROM {T_DURABLE} WHERE {_ATTR_ZAYAVKA}')
    full_rows = int(_scalar(cur, f'SELECT COUNT(*) FROM {T_DURABLE} WHERE {_ATTR_ZAYAVKA}') or 0)
    arr_rows = int(_scalar(cur,
        f"SELECT COUNT(*) FROM {T_DURABLE} WHERE {_ATTR_VIZIT}") or 0)
    today = datetime.now(timezone.utc).date()
    if max_date is None:
        return Block(9, 'Свежесть данных', False,
                     'max(Date)=NULL — fact_big_analytics пуста')
    age = (today - max_date).days
    ok = (age <= FRESHNESS_MAX_DAYS) and full_rows > 0 and arr_rows > 0
    detail = (f'max(Date)={max_date} (возраст {age} дн, порог <={FRESHNESS_MAX_DAYS}); '
              f'fact[заявка]={full_rows}, fact[визит]={arr_rows} [durable: fact_big_analytics]')
    return Block(9, 'Свежесть данных', ok, detail)


def _check_10_pixel_score_durable(cur) -> Block:
    """Блок 10: Пиксель == pixel_score — читает durable big_analytics_pixel_score.

    Сторона сравнения 'пиксель_атрибуц' берётся из fact_big_analytics (durable).
    Логика идентична check_10_pixel_score из verify_big_analytics.
    """
    px_score_exists = _scalar(cur, "SELECT to_regclass('public.big_analytics_pixel_score') IS NOT NULL")
    if not px_score_exists:
        return Block(10, 'Пиксель == pixel_score', True,
                     'skipped (big_analytics_pixel_score отсутствует)')

    cur.execute("""
        SELECT COALESCE(SUM(kol_vo_zayavok),0), COALESCE(SUM(korr),0),
               COALESCE(SUM(kval),0), COALESCE(SUM(priezd),0),
               COALESCE(SUM(prodazhi),0), COUNT(*)
        FROM public.big_analytics_pixel_score
    """)
    s_z, s_korr, s_kval, s_priezd, s_prod, sc_rows = cur.fetchone()
    sc_rows = int(sc_rows or 0)
    if sc_rows == 0:
        return Block(10, 'Пиксель == pixel_score', True,
                     'skipped (big_analytics_pixel_score пуста)')

    cur.execute(f"""
        SELECT COALESCE(SUM(kol_vo_zayavok),0), COALESCE(SUM(korr),0),
               COALESCE(SUM(kval),0), COALESCE(SUM(priezd),0),
               COALESCE(SUM(prodazhi),0), COUNT(*)
        FROM {T_DURABLE}
        WHERE {_ATTR_ZAYAVKA}
          AND _source_table = 'пиксель_атрибуц'
    """)
    f_z, f_korr, f_kval, f_priezd, f_prod, full_rows = cur.fetchone()
    full_rows = int(full_rows or 0)

    tol = Decimal(str(max(full_rows, 1)))
    pairs = [
        ('обращения', s_z, f_z), ('корр', s_korr, f_korr), ('квал', s_kval, f_kval),
        ('приезд', s_priezd, f_priezd), ('продажи', s_prod, f_prod),
    ]
    worst = Decimal('0')
    worst_name = ''
    for name, sc, fl in pairs:
        d = abs(Decimal(str(sc)) - Decimal(str(fl)))
        if d > worst:
            worst, worst_name = d, name
    ok = worst <= tol
    detail = (f'pixel_score rows={sc_rows} vs fact[пиксель_атрибуц] rows={full_rows}; '
              f'макс. расхождение метрики={worst} ({worst_name or "—"}), '
              f'допуск (≈±1×строк) <={tol} [durable: pixel_score + fact_big_analytics]')
    return Block(10, 'Пиксель == pixel_score', ok, detail)


def _check_11_salon_funnel_durable(cur) -> Block:
    """Блок 11: Пер-салонный инвариант воронки — читает fact_big_analytics (durable).

    Идентично check_11_salon_funnel из verify_big_analytics: уже читал fact_big_analytics,
    поэтому durable-версия не меняет логику — меняет только явную точку входа.
    """
    EPS = 1e-6
    cur.execute(f"""
        SELECT COALESCE(NULLIF(TRIM("салон"),''),'(пусто)') AS salon,
               SUM(korr)     AS korr,
               SUM(kval)     AS kval,
               SUM(priezd)   AS priezd,
               SUM(prodazhi) AS prodazhi
        FROM {T_DURABLE}
        WHERE {_ATTR_ZAYAVKA}
        GROUP BY 1
        HAVING SUM(kval) < SUM(priezd) - {EPS}
            OR (SUM(kval) = 0 AND SUM(priezd) > {EPS})
            OR SUM(korr) < SUM(kval) - {EPS}
            OR SUM(priezd) < SUM(prodazhi) - {EPS}
        ORDER BY 1
    """)
    rows = cur.fetchall()
    n = len(rows)
    ok = (n == 0)
    if ok:
        detail = 'все салоны: korr>=kval>=priezd>=prodazhi (нарушений 0) [durable: fact_big_analytics]'
    else:
        parts = []
        for salon, korr, kval, priezd, prod in rows:
            parts.append(
                f'{salon}: korr={float(korr or 0):.2f} kval={float(kval or 0):.2f} '
                f'priezd={float(priezd or 0):.2f} prod={float(prod or 0):.2f}'
            )
        detail = f'{n} салон(ов)-нарушителей воронки:\n   ' + '\n   '.join(parts)
    return Block(11, 'Пер-салонный инвариант воронки (I_salon)', ok, detail)


# ── Расчёт скалярного reward ──────────────────────────────────────────────────

def compute_reward(
    cost: Decimal,
    sales: int,
    blocks: list,
) -> dict:
    """Вычисляет reward и суб-оценки.

    Возвращает словарь (JSON-совместимый):
      reward       — скалярный итог (выше = лучше)
      hard_fail    — True если хоть один блок FAIL → reward = HARD_FAIL_REWARD
      cost_dist    — |cost - GOLDEN_COST|, рублей
      sales_slack  — max(0, GOLDEN_SALES_FLOOR - sales), штук
      n_violations — число блоков гейта с ok=False
      cost_ok      — bool (в пределах допуска)
      sales_ok     — bool (>= floor)
      blocks       — список {num, title, ok} для диагностики
    """
    cost_dist = float(abs(cost - GOLDEN_COST))
    sales_slack = max(0, GOLDEN_SALES_FLOOR - sales)
    n_violations = sum(1 for b in blocks if not b.ok)

    hard_fail = (n_violations > 0)

    if hard_fail:
        reward = HARD_FAIL_REWARD
    else:
        reward = (
            BASE_SCORE
            - cost_dist
            - sales_slack * SALES_SLACK_PENALTY
        )

    # cost_ok отражает scorer-допуск (±20 ₽), не verify-допуск (±10 ₽).
    # verify_ok_strict = abs(cost - GOLDEN_COST) <= GOLDEN_COST_TOL — для справки в JSON.
    cost_ok = (abs(cost - GOLDEN_COST) <= SCORER_COST_TOL)
    verify_cost_ok = (abs(cost - GOLDEN_COST) <= GOLDEN_COST_TOL)
    sales_ok = (sales >= GOLDEN_SALES_FLOOR)

    return {
        'reward': round(reward, 4),
        'hard_fail': hard_fail,
        'cost': float(cost),
        'cost_ref': float(GOLDEN_COST),
        'cost_tol_scorer': float(SCORER_COST_TOL),
        'cost_tol_verify': float(GOLDEN_COST_TOL),
        'cost_dist': round(cost_dist, 4),
        'cost_ok': cost_ok,
        'verify_cost_ok': verify_cost_ok,
        'sales': sales,
        'sales_floor': GOLDEN_SALES_FLOOR,
        'sales_slack': sales_slack,
        'sales_ok': sales_ok,
        'n_violations': n_violations,
        'blocks': [
            {'num': b.num, 'title': b.title, 'ok': b.ok}
            for b in blocks
        ],
    }


# ── Главная функция прогона ───────────────────────────────────────────────────

def run_scorer() -> dict:
    """Подключается к Victory БД (read-only), прогоняет 11 durable-блоков гейта,
    возвращает reward-словарь.

    Читает ТОЛЬКО durable-таблицы (fact_big_analytics, big_analytics_full_arrival,
    big_analytics_pixel_score) — корректен в любой момент времени, не зависит от
    фазы пайплайна. Никогда не мутирует БД.
    """
    params = dict(DB_DST)
    conn = psycopg2.connect(**params)
    try:
        conn.set_session(readonly=True, autocommit=True)

        # Durable-версии блоков гейта (читают fact_big_analytics / big_analytics_full_arrival)
        checks = [
            _check_1_golden_durable,
            _check_2_conservation_durable,
            _check_3_dup_directions_durable,
            _check_4_calls_vs_posev_durable,
            _check_5_pixel_visit_durable,
            _check_6_pixel_fractional_durable,
            _check_7_funnel_i3_durable,
            _check_8_grand_total_durable,
            _check_9_freshness_durable,
            _check_10_pixel_score_durable,
            _check_11_salon_funnel_durable,
        ]
        blocks = []
        for fn in checks:
            with conn.cursor() as cur:
                try:
                    blocks.append(fn(cur))
                except Exception as exc:
                    logger.warning('Блок %s упал: %s', fn.__name__, exc)
                    conn.rollback()
                    blocks.append(Block(0, fn.__name__, False, f'ошибка: {exc}'))

        # Сырые метрики для sub-score (fact_big_analytics, без датного фильтра)
        with conn.cursor() as cur:
            raw = _fetch_raw_metrics(cur)

    finally:
        conn.close()

    result = compute_reward(raw['cost'], raw['sales'], blocks)
    result['ts'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    result['scorer_version'] = 'GOLDEN_REWARD_SCORER_v2_2026-06-17'
    result['data_source'] = 'durable:fact_big_analytics'
    return result


# ── Telegram-сводка (опц.) ────────────────────────────────────────────────────

def _send_telegram_summary(r: dict) -> None:
    """Краткий Telegram-пинг с reward и ключевыми sub-scores."""
    try:
        import requests
        from config.tokens import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_PROXY_VARIANTS
    except Exception as exc:
        logger.warning('Telegram: не удалось загрузить креды: %s', exc)
        return

    fail_mark = 'HARD FAIL' if r['hard_fail'] else 'PASS'
    cost_status = 'OK' if r['cost_ok'] else 'FAIL'
    sales_status = 'OK' if r['sales_ok'] else f"SLACK={r['sales_slack']}"
    text = (
        f"golden_reward | {r['ts']}\n"
        f"{fail_mark} | reward={r['reward']}\n"
        f"cost={r['cost']:.2f} (delta={r['cost_dist']:+.4f}r) {cost_status}\n"
        f"sales={r['sales']} (floor>={r['sales_floor']}) {sales_status}\n"
        f"violations={r['n_violations']}/11\n"
        f"src={r.get('data_source','?')}"
    )
    url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage'
    payload = {'chat_id': TELEGRAM_CHAT_ID, 'text': text}
    for proxies in TELEGRAM_PROXY_VARIANTS:
        try:
            resp = requests.post(url, json=payload, proxies=proxies, timeout=30)
            if resp.ok and resp.json().get('ok'):
                logger.info('Telegram доставлено (proxies=%s)', proxies)
                return
        except Exception as exc:
            logger.debug('Telegram (proxies=%s): %s', proxies, exc)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description='golden_reward: числовой reward для best-of-N ранжирования вариантов патча'
    )
    parser.add_argument('--pretty', action='store_true',
                        help='JSON с отступами (читаемый вывод)')
    parser.add_argument('--tg', action='store_true',
                        help='отправить краткую сводку в Telegram')
    args = parser.parse_args()

    try:
        result = run_scorer()
    except Exception as exc:
        logger.error('golden_reward: ошибка подключения/выполнения: %s', exc, exc_info=True)
        return 2

    indent = 2 if args.pretty else None
    print(json.dumps(result, ensure_ascii=False, indent=indent))

    if args.tg:
        _send_telegram_summary(result)

    return 0


if __name__ == '__main__':
    sys.exit(main())
