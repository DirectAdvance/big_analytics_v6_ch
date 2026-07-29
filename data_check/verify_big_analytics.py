#!/usr/bin/env python3
"""
data_check/verify_big_analytics.py — ЕДИНЫЙ МАСТЕР-ЧЕКЕР big_analytics_v5.

«Дёрнул один скрипт — проверил всё». Подключается к ad_analytics_bi
(config.settings.DB_DST), прогоняет golden-инварианты витрины big_analytics_*
и печатает PASS/FAIL с цифрами. Опционально шлёт компактную сводку в личный
Telegram (config.tokens).

VERIFY_BIG_ANALYTICS_MARKER_13BLOCKS    ← grep-маркер для деплой-проверки.
VERIFY_BIG_ANALYTICS_MASTER_FULL        ← grep-маркер мастер-режима (--full).
POSEV_VISIT_FUNNEL_FIX_2026-06-24       ← посевы визит-оси by-design kval=0, вынесены из агрегата.
KVAL_COST_CHECK_2026-06-28              ← стоимость квала без пикселя (мягкий инвариант, блок 14).
KVAL_REBASELINE_2026-07-15              ← category-kval = корректная формула; норма стоимости 20913 ₽ (блок 14).

══════════════════════════════════════════════════════════════════════════════
ДВА РЕЖИМА
══════════════════════════════════════════════════════════════════════════════

БЫСТРЫЙ GOLDEN-ГЕЙТ (по умолчанию) — только LOGGED-таблицы, БЕЗ внешних API.
Так его вызывают три пайплайна (pipeline / fast_pipeline / pipeline_powerbi).
11 блоков:
  1. Golden Кудерко          — расход 25422774.00 ±100 ₽ / продажи >=54 (floor)
                                Продажи — растущая метрика (дозревание CRM):
                                проверяем floor>=54, не exact; рост сверху OK.
                                Проверено 2026-06-17: наблюд. 54, эталон floor>=54.
  2. Закон сохранения C1–C5  — SUM(cost) full == SUM(cost) по партициям источников
  3. Дубли заявок/звонков    — один key3 не в 2+ направлениях (обе оси)
  4. Звонки не задвоены       — calls ∩ посевы по key3 на визит-оси = 0
  5. Пиксель-визит присутств  — arrival rows >= 90k (FAIL если ~32k)
  6. Дробность пикселя         — есть дробные пиксель-строки (НЕ усечены к int)
  7. Инвариант воронки I3      — korr>=kval>=priezd>=prodazhi построчно (заявка-ось)
  8. Заявка grand total продаж — SUM(prodazhi без пикселя) floor-only (>=3000, кумулятивный)
  9. Свежесть данных           — max(Date) <= 3 дней; партиции непусты
 10. Пиксель == pixel_score    — read-only эквивалент инварианта step11:
                                  big_analytics_pixel_score (LOGGED) == строки
                                  'пиксель_атрибуц' в big_analytics_full (по домену).
                                  step11's live-инвариант (big_analytics_pixel vs
                                  output) сюда НЕ годится — big_analytics_pixel
                                  транзиентна и часто пуста между прогонами.
 11. Пер-салонный инвариант воронки (I_salon) — korr>=kval>=priezd>=prodazhi
                                  агрегатно по каждому салону (По дате заявки).
                                  Если хотя бы один салон нарушает — FAIL + список.
                                  Суммы дробные (пиксель); эпсилон 1e-6 против
                                  субмикронных хвостов дробной атрибуции.
 12. Полнота Маркар gsheet → fact_big_analytics — все mapped-домены с визитами
                                  в gsheet имеют priezd > 0 в финале.
 13. Нет задвоения посевов (POSEVDEDUP2+3) — social_посевы ∩ crop_targeting = 0
 14. Стоимость квала без пикселя (KVAL_COST_CHECK_2026-06-28) — SOFT-WARNING:
                                  SUM(total_cost)/SUM(kval) по fact_big_analytics,
                                  атрибуция='По дате заявки', без пикселя/пиксель_атрибуц.
                                  Норма [10000;30000] ₽. Текущий факт ~20913 ₽ (PASS,
                                  category-kval — КОРРЕКТНАЯ формула, KVAL_REBASELINE_2026-07-15).
                                  ok=True всегда (не валит гейт); ⚠️ в detail вне диапазона.
                                  Ловит откат к korr-negation формуле (~8870 ₽ пробивает LO).
                                  по lead_id (local_leads_all); telegram ∩ crop = 0.
                                  Проверяет ОБА пути crop: gsheets (pravilo_utm +
                                  nearest-prior) И API (crop_targeting_api_telegain_lead,
                                  utm_campaign + domain + месяц±1). POSEVDEDUP3 ловит
                                  майско-июньские дубли через Telega.in API-путь.
                                  Count-floor: social_посевы >= SOCIAL_POSEV_FLOOR,
                                  telegram_посевы >= TELEGRAM_POSEV_FLOOR
                                  (guard против over-deletion: если срезали слишком
                                  много → меньше baseline → FAIL).

ПОЛНЫЙ МАСТЕР-ПРОГОН (--full) — гейт + операционные проверки + star-сверка.
Для ручного «проверить всё одним запуском». Может тянуть Google Sheets и
star-схему — поэтому НЕ в авто-вызове пайплайна (чтобы не утяжелять и не падать
от внешних зависимостей). Дополнительно к 10 блокам:
 11. Операционка (data_check/run.py) — расход-без-лидов, NULL-поля, покрытие
     доменов (Google Sheets), нарушения воронки по проджектам. Вызывает
     существующие checks/*.py — БЕЗ дублирования SQL.
 12. Звезда (verify_star)    — public.fact_big_analytics == big_analytics_unified
     по суммам/срезам + ARP. Пропускается если public.fact пуста (build_star не
     прогонялся). Опц. отключить: --no-star.

ЧТО ОСОЗНАННО НЕ ВОШЛО В ГЕЙТ (с причиной):
  • step11 live-инвариант (big_analytics_pixel vs pixel_score) — источник
    транзиентный/пустой между прогонами; вместо него read-only блок 10.
  • step12 (сверка с CRM final_report) — НЕ read-only (TRUNCATE+INSERT
    analytics_proverka), читает транзиентную big_analytics_direct + remote FDW
    ad_analytics.final_report. Остаётся отдельным шагом пайплайна.
  • sales_attribution/verify.py — другая подсистема (atribuция продаж «Фаиг»),
    без exit-code, запускается своим пайплайном. Остаётся отдельно.

══════════════════════════════════════════════════════════════════════════════
‼️  ПРАВИЛО ДЛЯ БУДУЩИХ СЕССИЙ/АГЕНТОВ
══════════════════════════════════════════════════════════════════════════════
При появлении ЛЮБОЙ новой golden/инвариант-проверки — СНАЧАЛА спросить
пользователя, добавлять ли её в этот мастер-чекер, и только потом добавлять.
Не добавлять новые блоки молча. (Дубль правила — в work/big_analytics_v5/CLAUDE.md
и GOLDEN_BASELINE.md.)

Запуск:
    python3 data_check/verify_big_analytics.py          # быстрый гейт (10 блоков)
    python3 data_check/verify_big_analytics.py --tg     # + отправить в Telegram
    python3 data_check/verify_big_analytics.py --full    # мастер-прогон (всё)
    python3 data_check/verify_big_analytics.py --full --no-star  # без star-сверки

Контракт exit code (СОХРАНЁН — не менять, на него завязаны 3 пайплайна):
    0 = все блоки PASS
    1 = есть хотя бы один FAIL
    2 = ошибка скрипта (исключение при подключении/запросе)

Штатно запускается НА Victory (там load_db('victory') -> боевая БД localhost +
Telegram через Xray SOCKS5-прокси из .env). На маке без прокси проверки отработают,
но --tg упадёт (прокси нет) — это ожидаемо, отправку оборачиваем в try.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal

import psycopg2

# Корень big_analytics_v5 в sys.path (для запуска `python3 data_check/verify_big_analytics.py`)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import DB_DST, T_DATA_QUALITY_LOG  # noqa: E402

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger('verify_big_analytics')

# ── Эталоны (golden-oracle, см. GOLDEN_BASELINE.md) ──────────────────────────
GOLDEN_COST = Decimal('25422774.00')   # расход Кудерко, эталон
GOLDEN_COST_TOL = Decimal('100.00')     # допуск ±100 ₽ по расходу (расширен 2026-06-17):
#   пиксельный дрейф дробной атрибуции by-design продолжает расти между прогонами.
#   Дрейф +24 ₽ подтверждён пользователем как погрешность, не ошибка.
#   REF не менялся (25 422 774.00).
#   История: ±3 ₽ (2026-06-11) → ±10 ₽ (2026-06-16) → ±15 ₽ (2026-06-17) → ±100 ₽ (2026-06-17).
#   GOLDEN_BASELINE.md.
GOLDEN_SALES_FLOOR = 54                 # продажи Кудерко, FLOOR (минимум, не exact).
#   Продажи — растущая метрика: лиды дозревают в CRM асинхронно (47→53→54).
#   2026-06-17: floor поднят 47→54 (факт 54 подтверждён; механика — дозревание CRM,
#   не задвоение; fid-пересечение calls∩direct = 0; инвариант voronki соблюдён).
#   Проверяем prodazhi >= FLOOR, рост сверху НЕ фейлит.
#   Падение ниже 54 = регрессия данных → FAIL. GOLDEN_BASELINE.md обновлено 2026-06-17.
GOLDEN_DATE_FROM = '2026-01-01'
GOLDEN_DATE_TO = '2026-06-04'
GOLDEN_SPECIALIST = 'Кудерко Семен'      # точное написание в данных (НЕ "Семён")
# Источники Я.Директа+SEO+звонки для golden-среза (пиксель НЕ входит by design)
GOLDEN_SOURCES = ('direct', 'tp8', 'tp9', 'tp10', 'seo', 'calls', 'direct_unmatched', 'direct_zero')

ARRIVAL_ROWS_MIN = 90_000     # FAIL если ~32k (step11 пропустил пиксель на визит-оси)
# GRAND_SALES_FLOORONLY_2026-07-17: блок 8 (заявка-ось продаж) — floor-only.
# main = ROUND(SUM(prodazhi)) — глобальный КУМУЛЯТИВНЫЙ счётчик продаж с 01.01, растёт
# монотонно (помесячно 526/506/633/619/551/608/277, Σ=3720 на 17.07; к декабрю ≈6900).
# Прежний потолок HI=3700 обречён пробиваться ростом и НИЧЕГО не ловит (утечку
# 'пиксель_атрибуц' уже отсекает фильтр _source_table != 'пиксель_атрибуц'). Оставляем
# только нижнюю границу — она ловит обвал/потерю данных. Зеркалит golden «продажи floor≥54».
GRAND_SALES_LO = 3000         # floor: ниже — обвал/потеря данных (верхней границы нет by-design)
FRESHNESS_MAX_DAYS = 3

# ── Блок 13: дедуп посевов (POSEVDEDUP2_2026-06-19) ──────────────────────────
# Floor-guard: если social_посевы / telegram_посевы в big_analytics_full
# оказывается МЕНЬШЕ этого значения — значит фикс срезал лишнее (over-deletion).
# Baseline по данным прогона 2026-06-19: social_посевы ~526 строк, telegram ~191.
# Устанавливаем floor с запасом 30% ниже baseline (позволяет разброс между прогонами).
# При каждом обновлении baseline — обновлять floor здесь.
SOCIAL_POSEV_ROW_FLOOR = 120    # строк social_посевы в big_analytics_full (min)
                                 # POSEVDEDUP4_2026-06-19: снижен 300→120 (с учётом date-гейта)
TELEGRAM_POSEV_ROW_FLOOR = 90   # строк telegram_посевы в big_analytics_full (min)
                                 # POSEVDEDUP4_2026-06-19: снижен 100→90 (с учётом date-гейта)

# ── Блок 14: стоимость квала без пикселя (KVAL_COST_CHECK_2026-06-28) ───────────
# SOFT-WARNING: не валит exit code, виден в verify-отчёте и Telegram-дайджесте.
# Норма: SUM(total_cost)/SUM(kval) по fact_big_analytics (атрибуция='По дате заявки',
# _source_table NOT IN пиксель/пиксель_атрибуц) ∈ [KVAL_COST_LO; KVAL_COST_HI].
# KVAL_REBASELINE_2026-07-15: category-kval (kval из категории 'qualified') — КОРРЕКТНАЯ
# формула (решение пользователя), НЕ регрессия. Текущий факт: ~20913 ₽/квал (лежит по
# центру [10000;30000]). Регрессия теперь = откат к старой korr-negation формуле → kval
# растёт, стоимость падает к ~8870 ₽ и пробивает нижнюю границу 10000 → сигнал.
KVAL_COST_LO = 10_000   # нижняя граница нормы [₽/квал]  (ловит откат к korr-negation ~8870)
KVAL_COST_HI = 30_000   # верхняя граница нормы [₽/квал]  (санити-потолок над ~20913)

T_FULL = 'public.big_analytics_full'
T_ARRIVAL = 'public.big_analytics_full_arrival'
T_UNIFIED = 'public.big_analytics_unified'


# ── Результат одного блока ───────────────────────────────────────────────────
class Block:
    __slots__ = ('num', 'title', 'ok', 'detail')

    def __init__(self, num: int, title: str, ok: bool, detail: str):
        self.num = num
        self.title = title
        self.ok = ok
        self.detail = detail

    def render(self) -> str:
        mark = '✅' if self.ok else '❌'
        return f'{mark} {self.num}. {self.title}\n   {self.detail}'


def _scalar(cur, sql, params=None):
    cur.execute(sql, params or ())
    row = cur.fetchone()
    return row[0] if row else None


# ── Блок 1: Golden Кудерко ───────────────────────────────────────────────────
def check_1_golden(cur) -> Block:
    cur.execute(
        f"""
        SELECT ROUND(SUM(total_cost)::numeric, 2)  AS rashod,
               ROUND(SUM(prodazhi))                AS prodazhi
        FROM {T_UNIFIED}
        WHERE "специалист" = %s
          AND "атрибуция" = 'По дате заявки'
          AND _source_table IN %s
        """,
        (GOLDEN_SPECIALIST, GOLDEN_SOURCES),
    )
    rashod, prodazhi = cur.fetchone()
    rashod = Decimal(str(rashod)) if rashod is not None else Decimal('0')
    prodazhi = int(prodazhi) if prodazhi is not None else 0
    # Допуск по расходу ±100 ₽ (расширен 2026-06-17: пиксельный дрейф by-design,
    # дрейф +24 ₽ подтверждён пользователем как погрешность. REF неизменён. GOLDEN_BASELINE.md).
    # Продажи — floor >= 54: растущая метрика (дозревание CRM-лидов), рост сверху OK.
    # Падение ниже 54 = регрессия данных → FAIL. Floor поднят 47→54 2026-06-17.
    rashod_ok = abs(rashod - GOLDEN_COST) <= GOLDEN_COST_TOL
    sales_ok = (prodazhi >= GOLDEN_SALES_FLOOR)
    ok = rashod_ok and sales_ok
    sales_status = 'OK' if sales_ok else f'FAIL (ниже floor {GOLDEN_SALES_FLOOR})'
    detail = (f'расход={rashod} (эталон {GOLDEN_COST}±{GOLDEN_COST_TOL}, '
              f'Δ={rashod - GOLDEN_COST:+}), '
              f'продажи={prodazhi} (floor>={GOLDEN_SALES_FLOOR}, {sales_status})')
    return Block(1, 'Golden Кудерко', ok, detail)


# ── Блок 2: Закон сохранения C1–C5 ───────────────────────────────────────────
def check_2_conservation(cur) -> Block:
    """SUM(cost) в big_analytics_full == SUM(cost) по партициям-источникам.

    Промежуточные таблицы big_analytics_{direct,seo,pixel,crop_targeting,...}
    после финализации могут быть TRUNCATE-нуты — тогда блок пропускается (PASS).
    """
    src_tables = [
        'big_analytics_direct', 'big_analytics_seo', 'big_analytics_pixel',
        'big_analytics_crop_targeting', 'big_analytics_telegram', 'big_analytics_reviews',
    ]
    # full: расход по всем строкам
    full_cost = _scalar(cur, f'SELECT COALESCE(ROUND(SUM(total_cost)::numeric,2),0) FROM {T_FULL}')
    full_cost = Decimal(str(full_cost))

    parts = []
    available = []
    for t in src_tables:
        exists = _scalar(cur,
                         "SELECT to_regclass(%s) IS NOT NULL", (f'public.{t}',))
        if not exists:
            continue
        n = _scalar(cur, f'SELECT COUNT(*) FROM public.{t}')
        if n == 0:
            continue  # TRUNCATE-нута — пропустить
        available.append(t)
        c = _scalar(cur, f'SELECT COALESCE(ROUND(SUM(total_cost)::numeric,2),0) FROM public.{t}')
        parts.append(Decimal(str(c)))

    if not available:
        return Block(2, 'Закон сохранения C1–C5 (ИТОГ)', True,
                     'skipped (промежуточные таблицы не доступны / TRUNCATE-нуты)')

    parts_sum = sum(parts, Decimal('0'))
    # Часть источников (calls/seo) расхода не несут, full может быть >= суммы партиций.
    # Инвариант: расход партиций НЕ превышает full (cost из них втекает в full без потерь).
    ok = parts_sum <= full_cost + Decimal('0.01')
    detail = (f'full cost={full_cost}; сумма доступных источников ({len(available)})={parts_sum}; '
              f'источники: {", ".join(available)}')
    return Block(2, 'Закон сохранения C1–C5 (ИТОГ)', ok, detail)


# ── Блок 3: Дубли заявок/звонков по направлениям ─────────────────────────────
def check_3_dup_directions(cur) -> Block:
    """Один key3 не должен попасть в 2+ направления из набора каналов (обе оси).

    key3 NULL у звонков — исключаем. Проверяем по обеим осям (full + arrival).
    """
    dirs = ('Контекст', 'SEO', 'SEO Flow', 'посевы')
    total_dups = 0
    per_axis = []
    for axis_name, tbl in (('заявка', T_FULL), ('визит', T_ARRIVAL)):
        n = _scalar(cur, f"""
            SELECT COUNT(*) FROM (
                SELECT key3
                FROM {tbl}
                WHERE key3 IS NOT NULL
                  AND направление IN %s
                GROUP BY key3
                HAVING COUNT(DISTINCT направление) > 1
            ) d
        """, (dirs,))
        n = int(n or 0)
        total_dups += n
        per_axis.append(f'{axis_name}={n}')
    ok = total_dups == 0
    detail = f'key3 в 2+ направлениях: {", ".join(per_axis)} (ожидание 0)'
    return Block(3, 'Дубли заявок/звонков по направлениям', ok, detail)


# ── Блок 4: Звонки не задвоены на визит-оси ──────────────────────────────────
def check_4_calls_vs_posev(cur) -> Block:
    """В big_analytics_full_arrival звонки (направление='Звонки'/источник='Звонки')
    не пересекаются с посевами по key3."""
    # Определяем call-строки: тип_заявки='Звонки' ИЛИ источник='Звонки'
    # CAPITALIZE_FIX_2026-07-10: step6 теперь пишет 'Звонки' (с большой З)
    n = _scalar(cur, f"""
        SELECT COUNT(*) FROM (
            SELECT key3
            FROM {T_ARRIVAL}
            WHERE key3 IS NOT NULL
              AND (тип_заявки = 'Звонки' OR источник IN ('звонки', 'Звонки'))
            INTERSECT
            SELECT key3
            FROM {T_ARRIVAL}
            WHERE key3 IS NOT NULL
              AND "источник" LIKE 'Посевы_%%'  -- KOMPLEKS_REFACTOR_REDO_2026-07-09 %% escaped for psycopg2
        ) x
    """)
    n = int(n or 0)
    ok = n == 0
    detail = f'calls ∩ посевы по key3 = {n} (ожидание 0)'
    return Block(4, 'Звонки не задвоены на визит-оси', ok, detail)


# ── Блок 5: Пиксель-визит присутствует ───────────────────────────────────────
def check_5_pixel_visit(cur) -> Block:
    arrival_rows = int(_scalar(cur, f'SELECT COUNT(*) FROM {T_ARRIVAL}') or 0)
    # Пиксель-визит срез
    cur.execute(f"""
        SELECT COUNT(*)                              AS rows,
               COALESCE(ROUND(SUM(prodazhi)),0)      AS prodazhi,
               COALESCE(ROUND(SUM(priezd)),0)        AS priezd
        FROM {T_ARRIVAL}
        WHERE направление IN ('Пиксель','Пиксель_атрибуц')  -- KOMPLEKS_REFACTOR_REDO_2026-07-09
    """)
    px_rows, px_sales, px_arr = cur.fetchone()
    px_rows = int(px_rows or 0)
    px_sales = int(px_sales or 0)
    px_arr = int(px_arr or 0)
    ok = arrival_rows >= ARRIVAL_ROWS_MIN
    detail = (f'визит: продажи={px_sales} приезд={px_arr} pixel_rows={px_rows}; '
              f'arrival_rows={arrival_rows} (ожид ~94k, FAIL если ~32k → порог >={ARRIVAL_ROWS_MIN})')
    return Block(5, 'Пиксель-визит присутствует', ok, detail)


# ── Блок 6: Дробность пикселя не усечена ─────────────────────────────────────
def check_6_pixel_fractional(cur) -> Block:
    """Проверяем НА УРОВНЕ СТРОК через ROUND (не float!): должны быть строки, где
    prodazhi != ROUND(prodazhi,0). Если 0 — усечение к int (FAIL).

    Ловушка false-FAIL: SUM(prodazhi пиксель)=899.999...936 — float(raw) округлит к
    900.0 → frac=0 → ложный FAIL. Поэтому считаем дробные СТРОКИ, а не дробность суммы.
    """
    cur.execute(f"""
        SELECT COUNT(*) FILTER (WHERE prodazhi <> ROUND(prodazhi, 0)) AS frac_rows,
               COUNT(*)                                               AS px_rows,
               SUM(prodazhi)                                          AS px_sum
        FROM {T_FULL}
        WHERE направление IN ('Пиксель','Пиксель_атрибуц')  -- KOMPLEKS_REFACTOR_REDO_2026-07-09
    """)
    frac_rows, px_rows, px_sum = cur.fetchone()
    frac_rows = int(frac_rows or 0)
    px_rows = int(px_rows or 0)
    px_sum_d = Decimal(str(px_sum)) if px_sum is not None else Decimal('0')
    frac_part = px_sum_d - px_sum_d.to_integral_value(rounding='ROUND_FLOOR')
    ok = frac_rows > 0
    detail = (f'дробных пиксель-строк={frac_rows}/{px_rows} (усечение к int дало бы 0); '
              f'SUM={px_sum_d} (дробн.часть суммы={frac_part})')
    return Block(6, 'Дробность пикселя не усечена', ok, detail)


# ── Блок 7: Инвариант воронки I3 (POSEV_VISIT_FUNNEL_FIX_2026-06-24) ─────────
def check_7_funnel_i3(cur) -> list['Block']:
    """Три независимых под-инварианта вместо одного агрегата.

    FUNNEL_I3_PIXEL_FIX_2026-06-23: пиксель-строки (_source_table IN
    ('пиксель','пиксель_атрибуц')) by-design несут priezd>0 при korr=kval=0
    (пиксель знает только приезд/продажу). Из-за них SUM(kval) < SUM(priezd)
    по визит-оси → ложный FAIL. Вынесены в строку 2 «Пиксель-инвариант».

    POSEV_VISIT_FUNNEL_FIX_2026-06-24: посевы на визит-оси (step13_arrival /
    атрибуция='По дате визита') by-design несут priezd>0 при korr=kval=0.
    Механика: POSEV_VISIT_DATESHIFT_2026-06-23 переносит только priezd/prodazhi
    на дату визита; kval у посевов остаётся на заявка-оси, где воронка полная.
    Идентичная механика пикселю — частичный источник. Исключены из агрегата
    строки 1 аналогично пикселю; вынесены в строку 3 «Посевы визит-инвариант».

    Строка 1 — korr>=kval>=priezd>=prodazhi:
      - заявка-ось: построчно по big_analytics_full (нарушений=0)
      - визит-ось: агрегат unified БЕЗ пикселя И БЕЗ посевов (направление!='посевы')
        Заявка-ось посевов не трогаем — там полная воронка, инвариант держится.

    Строка 2 — пиксель by-design: только priezd>=prodazhi.
      - визит-ось: агрегат unified WHERE _source_table IN ('пиксель','пиксель_атрибуц')
      - заявка-ось: у пикселя нет строк → печатаем «нет пикселя», не FAIL.

    Строка 3 — посевы визит-инвариант (by-design korr=kval=0): только priezd>=prodazhi.
      - визит-ось: агрегат unified WHERE направление='посевы' И НЕ пиксель
      - FAIL только при реальной аномалии: prodazhi > priezd.
      - korr/kval не проверяем (они 0 by-design).

    Возвращает список из трёх Block (все с num=7).
    Вызывающий run_all() распаковывает: blocks.extend(check_7_funnel_i3(cur)).
    """
    PIXEL_SRCS = ('пиксель', 'пиксель_атрибуц')

    # ── Строка 1: Воронка (без пикселя, без посевов на визит-оси) ────────────
    # 1a) Построчно заявка-ось (big_analytics_full) — посевы ОСТАются
    #     (на заявка-оси у посевов полная воронка: korr>=kval>=priezd>=prodazhi)
    viol = int(_scalar(cur, f"""
        SELECT COUNT(*)
        FROM {T_FULL}
        WHERE NOT (korr >= kval AND kval >= priezd AND priezd >= prodazhi)
    """) or 0)

    # 1b) Агрегат обеих осей через unified:
    #     - пиксель исключён (_source_table NOT IN PIXEL_SRCS) — как раньше
    #     - посевы на ВИЗИТ-оси исключены (направление!='посевы') — НОВОЕ
    #     - посевы на ЗАЯВКА-оси остаются (там воронка полная, не ломает агрегат)
    cur.execute(f"""
        SELECT "атрибуция",
               ROUND(SUM(korr))     AS korr,
               ROUND(SUM(kval))     AS kval,
               ROUND(SUM(priezd))   AS priezd,
               ROUND(SUM(prodazhi)) AS prodazhi
        FROM {T_UNIFIED}
        WHERE _source_table NOT IN %s
          AND NOT ("атрибуция" = 'По дате визита' AND "источник" LIKE 'Посевы_%%')  -- KOMPLEKS_REFACTOR_REDO_2026-07-09 %% escaped for psycopg2
        GROUP BY 1
        ORDER BY 1
    """, (PIXEL_SRCS,))
    rows_no_px = cur.fetchall()

    agg_parts = []  # "заявка K>=V>=P>=S OK" etc.
    agg_ok = True
    for attr, korr, kval, priezd, prod in rows_no_px:
        korr, kval, priezd, prod = (int(x or 0) for x in (korr, kval, priezd, prod))
        axis_label = 'заявка' if 'заявки' in attr else 'визит'
        chain = f'{korr}>={kval}>={priezd}>={prod}'
        if korr >= kval >= priezd >= prod:
            agg_parts.append(f'{axis_label} {chain} OK')
        else:
            agg_parts.append(f'{axis_label} {chain} НАРУШЕНО')
            agg_ok = False

    ok1 = (viol == 0) and agg_ok
    detail1 = (
        f'заявка-ось построчно нарушений={viol} (ожид 0); '
        + ('; '.join(agg_parts) if agg_parts else 'нет строк unified без пикселя/посевов')
    )
    block1 = Block(7, 'Воронка (без пикселя)', ok1, detail1)

    # ── Строка 2: Пиксель-инвариант (priezd>=prodazhi) ───────────────────────
    # Визит-ось: агрегат unified по пикселю
    cur.execute(f"""
        SELECT ROUND(SUM(priezd))   AS priezd,
               ROUND(SUM(prodazhi)) AS prodazhi
        FROM {T_UNIFIED}
        WHERE _source_table IN %s
          AND "атрибуция" = 'По дате визита'
    """, (PIXEL_SRCS,))
    px_row = cur.fetchone()
    px_priezd = int(px_row[0] or 0) if px_row else 0
    px_prod   = int(px_row[1] or 0) if px_row else 0

    # Заявка-ось: у пикселя строк нет by-design (пиксель атрибутируется только на визит-ось)
    cur.execute(f"""
        SELECT COUNT(*)
        FROM {T_UNIFIED}
        WHERE _source_table IN %s
          AND "атрибуция" = 'По дате заявки'
    """, (PIXEL_SRCS,))
    px_claim_rows = int(cur.fetchone()[0] or 0)

    px_ok = (px_priezd >= px_prod)
    if px_claim_rows == 0:
        claim_note = 'нет пикселя на заявка-оси (by-design)'
    else:
        claim_note = f'заявка-ось: {px_claim_rows} строк (неожиданно — проверить)'

    detail2 = (
        f'визит priezd {px_priezd} >= prodazhi {px_prod} '
        f"{'OK' if px_ok else 'НАРУШЕНО'}; {claim_note}"
    )
    block2 = Block(7, 'Пиксель-инвариант (пиксель+пиксель_атрибуц)', px_ok, detail2)

    # ── Строка 3: Посевы визит-инвариант (by-design korr=kval=0) ─────────────
    # POSEV_VISIT_FUNNEL_FIX_2026-06-24
    # Посевы на визит-оси: POSEV_VISIT_DATESHIFT_2026-06-23 переносит только
    # priezd/prodazhi на дату визита; kval остаётся на заявка-оси.
    # Аналог пикселя: частичный источник, korr=kval=0 by-design.
    # Инвариант: priezd >= prodazhi (аномалия = продажи > приездов, что невозможно).
    # FAIL только при реальной аномалии, не при korr/kval=0.
    cur.execute(f"""
        SELECT ROUND(SUM(priezd))   AS priezd,
               ROUND(SUM(prodazhi)) AS prodazhi
        FROM {T_UNIFIED}
        WHERE "атрибуция" = 'По дате визита'
          AND "источник" LIKE 'Посевы_%%'  -- KOMPLEKS_REFACTOR_REDO_2026-07-09 %% escaped for psycopg2
          AND _source_table NOT IN %s
    """, (PIXEL_SRCS,))
    pv_row = cur.fetchone()
    pv_priezd = int(pv_row[0] or 0) if pv_row else 0
    pv_prod   = int(pv_row[1] or 0) if pv_row else 0

    pv_ok = (pv_priezd >= pv_prod)
    detail3 = (
        f'посевы визит: priezd={pv_priezd} >= prodazhi={pv_prod} '
        f"{'OK' if pv_ok else 'НАРУШЕНО (prodazhi > priezd — аномалия)'}; "
        f'korr=kval=0 by-design (kval на заявка-оси)'
    )
    block3 = Block(7, 'Посевы визит-инвариант (by-design korr=kval=0)', pv_ok, detail3)

    return [block1, block2, block3]


# ── Блок 8: Заявка-ось grand total продаж ────────────────────────────────────
def check_8_grand_total(cur) -> Block:
    """Grand total продаж на заявка-оси: direct+calls+seo+tp8+пиксель vs пиксель_атрибуц.

    GRAND_TOTAL_SPLIT_2026-06-29 (Вариант B):
      main      = SUM(prodazhi) без пиксель_атрибуц → гейт floor-only (>= GRAND_SALES_LO).
      px_attrib = SUM(prodazhi) для пиксель_атрибуц → информационно (by-design: визит-зеркало
                  step11, дублирует пиксель-продажи на заявка-ось; в гейт не входит).
    Прежний grand total 3821 = main(3308) + px_attrib(512) — гейт FAIL из-за пиксель_атрибуц.
    Источник: fact_big_analytics (durable — не TRUNCATE-нуется cleanup_intermediate),
    атрибуция='По дате заявки' (заявка-ось).
    GRAND_SALES_FLOORONLY_2026-07-17: main — кумулятивный счётчик с 01.01, растёт монотонно
    (Σ=3720 на 17.07). Верхняя граница снята: HI=3700 обречён пробиваться ростом и ничего
    не ловил. Проверка floor-only (>= GRAND_SALES_LO) ловит обвал/потерю данных.
    """
    T_FACT = 'public.fact_big_analytics'
    main = _scalar(cur, f"""
        SELECT COALESCE(ROUND(SUM(prodazhi)), 0)
        FROM {T_FACT}
        WHERE "атрибуция" = 'По дате заявки'
          AND _source_table != 'пиксель_атрибуц'
    """)
    main = int(main or 0)
    px_attrib = _scalar(cur, f"""
        SELECT COALESCE(ROUND(SUM(prodazhi)), 0)
        FROM {T_FACT}
        WHERE "атрибуция" = 'По дате заявки'
          AND _source_table = 'пиксель_атрибуц'
    """)
    px_attrib = int(px_attrib or 0)
    ok = GRAND_SALES_LO <= main
    detail = (
        f'direct+calls+seo+tp8+пиксель={main} [floor>={GRAND_SALES_LO}], '
        f'пиксель_атрибуц={px_attrib} (by-design, визит-зеркало), '
        f'grand total={main + px_attrib}'
    )
    return Block(8, 'Заявка-ось продаж', ok, detail)


# ── Блок 9: Свежесть данных ──────────────────────────────────────────────────
def check_9_freshness(cur) -> Block:
    max_date = _scalar(cur, f'SELECT MAX("Date") FROM {T_FULL}')
    full_rows = int(_scalar(cur, f'SELECT COUNT(*) FROM {T_FULL}') or 0)
    arr_rows = int(_scalar(cur, f'SELECT COUNT(*) FROM {T_ARRIVAL}') or 0)
    today = datetime.now(timezone.utc).date()
    if max_date is None:
        return Block(9, 'Свежесть данных', False,
                     'max(Date)=NULL — big_analytics_full пуста')
    age = (today - max_date).days
    ok = (age <= FRESHNESS_MAX_DAYS) and full_rows > 0 and arr_rows > 0
    detail = (f'max(Date)={max_date} (возраст {age} дн, порог <={FRESHNESS_MAX_DAYS}); '
              f'заявка-партиция={full_rows}, визит-партиция={arr_rows}')
    return Block(9, 'Свежесть данных', ok, detail)


# ── Блок 10: Пиксель == pixel_score (read-only эквивалент step11) ─────────────
def check_10_pixel_score(cur) -> Block:
    """READ-ONLY эквивалент встроенного инварианта step11.

    step11 сверяет big_analytics_pixel (источник) vs big_analytics_pixel_score
    (атрибутированный выход). НО big_analytics_pixel — транзиентная таблица
    (часто 0 строк между прогонами, см. KNOWN_ISSUES), её нельзя дёргать
    standalone. Зато ОБЕ персистентные стороны атрибуции остаются в БД:
      • big_analytics_pixel_score (LOGGED, NUMERIC) — точный выход атрибуции;
      • строки 'пиксель_атрибуц' в big_analytics_full (BIGINT, ±1 округление).
    Инвариант step11: SUM по pixel_score == SUM по 'пиксель_атрибуц' (с допуском
    на BIGINT-округление ≤ числа строк). Расхождение > допуска = перенос в full
    сломан (DELETE/INSERT 'пиксель_атрибуц' рассинхронизирован с pixel_score).

    Если big_analytics_pixel_score пуста (пиксель отключён / не прогонялся) —
    блок PASS со skip-пометкой (не валим гейт из-за отсутствия пикселя).
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
        FROM {T_FULL}
        WHERE _source_table = 'пиксель_атрибуц'
    """)
    f_z, f_korr, f_kval, f_priezd, f_prod, full_rows = cur.fetchone()
    full_rows = int(full_rows or 0)

    # Допуск = число строк full-пиксель (каждая BIGINT-строка ±1 от NUMERIC-источника).
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
    detail = (f'pixel_score rows={sc_rows} vs full[пиксель_атрибуц] rows={full_rows}; '
              f'макс. расхождение метрики ={worst} ({worst_name or "—"}), '
              f'допуск (≈±1×строк) <={tol}')
    return Block(10, 'Пиксель == pixel_score', ok, detail)


# ── Блок 11: Пер-салонный инвариант воронки (I_salon) ────────────────────────
def check_11_salon_funnel(cur) -> Block:
    """Агрегатный инвариант воронки korr>=kval>=priezd>=prodazhi по каждому салону.

    Проверяется на public.fact_big_analytics, атрибуция 'По дате заявки'.
    Суммы могут быть дробными (пиксельная атрибуция) — сравниваем с эпсилоном 1e-6,
    чтобы субмикронные хвосты дробной атрибуции не давали ложный FAIL.

    Нарушитель: SUM(kval) < SUM(priezd), или SUM(korr) < SUM(kval),
                или SUM(priezd) < SUM(prodazhi), или (kval=0 AND priezd>0).
    Если нарушителей 0 → PASS. Иначе → FAIL + список салонов с числами.

    Добавлено 2026-06-15 по явному запросу пользователя (после фикса excl_set
    в config/status_sql.py — проверяем, что ни один салон не сломан по воронке).
    """
    EPS = 1e-6  # эпсилон против субмикронных хвостов дробной пиксельной атрибуции
    T_FACT = 'public.fact_big_analytics'
    cur.execute(f"""
        SELECT COALESCE(NULLIF(TRIM("салон"),''),'(пусто)') AS salon,
               SUM(korr)     AS korr,
               SUM(kval)     AS kval,
               SUM(priezd)   AS priezd,
               SUM(prodazhi) AS prodazhi
        FROM {T_FACT}
        WHERE "атрибуция" = 'По дате заявки'
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
        detail = 'все салоны: korr>=kval>=priezd>=prodazhi (нарушений 0)'
    else:
        parts = []
        for salon, korr, kval, priezd, prod in rows:
            korr_f = float(korr or 0)
            kval_f = float(kval or 0)
            priezd_f = float(priezd or 0)
            prod_f = float(prod or 0)
            parts.append(
                f'{salon}: korr={korr_f:.2f} kval={kval_f:.2f} '
                f'priezd={priezd_f:.2f} prod={prod_f:.2f}'
            )
        detail = f'{n} салон(ов)-нарушителей воронки:\n   ' + '\n   '.join(parts)
    return Block(11, 'Пер-салонный инвариант воронки (I_salon)', ok, detail)


# ── Блок 12: Полнота local_gsheet_priezdi_marcar → fact_big_analytics ────────
# MARCAR_GSHEET_COVERAGE_CHECK_2026-06-17
def check_12_marcar_gsheet_coverage(cur) -> Block:
    """Агрегатная проверка полноты local_gsheet_priezdi_marcar в fact_big_analytics.

    Источник: local_gsheet_priezdi_marcar (Маркар Google Sheet, дата визита/продажи).
    Дата: записи с visit_date >= 2026-01-01 (дата в ISO-формате, тип text).
    Финал: fact_big_analytics (durable, атрибуция 'По дате заявки', Date >= 2026-01-01).

    Механика:
    - Группируем записи gsheet по source-домену (колонка source = домен сайта).
    - Для каждого домена делаем LEFT JOIN к local_gsheet_sites → canonical salon.
    - Домены без маппинга (victory_pxl, виктори пиксель 78 и т.п.) — вне Маркар-ETL
      scope, пропускаем (они могут быть пиксельными источниками другого потока).
    - Для mapped-доменов проверяем: SUM(priezd) в fact_big_analytics > 0 для данного
      salon с Date >= 2026-01-01.
    - FAIL: хотя бы один mapped-домен с gsheet-визитами > 0, но priezd в финале = 0.
    - WARNING (логируется в detail, не валит гейт): число продаж gsheet vs финал.
      Расхождение by-design: gsheet считает по дате визита, финал — по дате заявки,
      дробная атрибуция step11 размазывает prodazhi. TODO: ужесточить после стабилизации.

    Добавлен 2026-06-17 по запросу: проверка полноты Маркар-данных в пайплайне.
    Валидация на реальных данных Victory: все mapped-домены PASS (priezd > 0).
    Un-mapped (пиксельные): исключены из проверки как out-of-scope для Маркар-ETL.
    """
    T_FACT = 'public.fact_big_analytics'
    T_GSHEET = 'public.local_gsheet_priezdi_marcar'
    T_SITES = 'public.local_gsheet_sites'
    cur.execute(f"""
        WITH gsheet_by_domain AS (
            -- Считаем визиты и продажи из gsheet по домену-источнику с 2026-01-01.
            -- Дата хранится как text в ISO-формате (YYYY-MM-DD) в local_ (text type).
            -- Визиты = статусы реального приезда по local_crm_statuses-маппингу:
            --   'Приехал', 'Одобрение', 'Дошел в КО', 'Продажа'.
            -- Продажи = только статус 'Продажа'.
            SELECT
                LOWER(TRIM(source)) AS domain,
                COUNT(CASE WHEN status IN ('Приехал','Одобрение','Продажа','Дошел в КО')
                           THEN 1 END)                              AS g_vizity,
                COUNT(CASE WHEN status = 'Продажа' THEN 1 END)     AS g_prodazhi
            FROM {T_GSHEET}
            WHERE date IS NOT NULL
              AND TRIM(date) != ''
              AND date::date >= '2026-01-01'
            GROUP BY LOWER(TRIM(source))
        ),
        mapped AS (
            -- Маппинг domain → canonical salon через local_gsheet_sites.
            -- Домены без маппинга (виктори-пиксель коды, victory_vdl и т.п.)
            -- дадут salon=NULL и будут исключены из проверки ниже.
            SELECT
                g.domain,
                s.salon,
                g.g_vizity,
                g.g_prodazhi
            FROM gsheet_by_domain g
            LEFT JOIN {T_SITES} s
                ON LOWER(TRIM(s.domain)) = g.domain
        ),
        fact_by_salon AS (
            -- Агрегат прихожд. в финальной таблице по salon + дата >= 2026-01-01.
            -- Дробность СОХРАНЕНА: priezd NUMERIC, не int-каст.
            SELECT
                "салон",
                SUM(priezd)   AS f_priezd,
                SUM(prodazhi) AS f_prodazhi
            FROM {T_FACT}
            WHERE "атрибуция" = 'По дате заявки'
              AND "Date" >= '2026-01-01'
            GROUP BY "салон"
        ),
        coverage AS (
            SELECT
                m.domain,
                m.salon,
                m.g_vizity,
                m.g_prodazhi,
                COALESCE(f.f_priezd,   0) AS f_priezd,
                COALESCE(f.f_prodazhi, 0) AS f_prodazhi
            FROM mapped m
            LEFT JOIN fact_by_salon f ON m.salon = f."салон"
            WHERE m.salon IS NOT NULL   -- исключаем un-mapped (пиксельные/vdl коды)
              AND m.g_vizity > 0        -- только если в gsheet реально есть визиты
        )
        SELECT
            domain,
            salon,
            g_vizity,
            g_prodazhi,
            f_priezd,
            f_prodazhi,
            CASE WHEN f_priezd = 0 THEN 'FAIL' ELSE 'OK' END AS viz_check
        FROM coverage
        ORDER BY g_vizity DESC
    """)
    rows = cur.fetchall()
    fail_rows = [r for r in rows if r[6] == 'FAIL']
    ok = (len(fail_rows) == 0)

    # Агрегаты для detail
    total_g_vizity   = sum(r[2] for r in rows)
    total_g_prodazhi = sum(r[3] for r in rows)
    total_f_priezd   = sum(float(r[4]) for r in rows)
    total_f_prodazhi = sum(float(r[5]) for r in rows)
    covered_domains  = sum(1 for r in rows if r[6] == 'OK')
    total_domains    = len(rows)

    # WARNING по продажам (by-design расхождение — не гейт)
    prodazhi_delta = int(total_g_prodazhi) - int(total_f_prodazhi)
    prodazhi_note = (
        f'продажи gsheet={int(total_g_prodazhi)} vs финал={int(total_f_prodazhi)}'
        f' (Δ={prodazhi_delta}, by-design: разные даты+дробная атрибуция; '
        f'TODO ужесточить после стабилизации)'
    )

    if ok:
        detail = (
            f'Маркар gsheet (>= 2026-01-01): '
            f'mapped-доменов={total_domains}, покрыто={covered_domains}/'
            f'{total_domains}, '
            f'визиты gsheet={int(total_g_vizity)} vs priezd финал={int(total_f_priezd)}. '
            f'WARNING: {prodazhi_note}'
        )
    else:
        parts = [
            f'{r[0]} (salon={r[1]}): gsheet_vizity={r[2]}, f_priezd={float(r[4]):.0f}'
            for r in fail_rows
        ]
        detail = (
            f'{len(fail_rows)} домен(ов) с визитами в gsheet но priezd=0 в финале: '
            + '; '.join(parts)
            + f'. WARNING: {prodazhi_note}'
        )
    return Block(12, 'Полнота Маркар gsheet → fact_big_analytics', ok, detail)


# ── Блок 13: нет задвоения посевов по lead_id + count-floor ──────────────────
# POSEVDEDUP2_2026-06-19 + POSEVDEDUP3_2026-06-19
def check_13_posev_dedup(cur) -> Block:
    """Два под-инварианта:
    A) Нет пересечения по lead_id между social_посевы/telegram_посевы и crop_targeting.
       Дедуп реализован в step3._add_social_posev_to_crop_sql и _add_telegram_to_crop_sql
       через NOT EXISTS к ОБОИМ путям crop:
         Путь 1 (gsheets): NOT EXISTS к gsheets_crop_targeting_account (utm_effective +
           nearest-prior placement в 90-дневном окне на том же домене).
         Путь 2 (API): NOT EXISTS к crop_targeting_api_telegain_lead (utm_campaign +
           domain + месяц±1) — майско-июньские дубли через Telega.in API.
       Проверяем результат через local_leads_all (всегда доступна, дurable):
       лид задвоен если прошёл в social/telegram-ветку AND учтён в crop ЛЮБЫМ путём.
       Ожидание: 0 пересечений.

    B) Count-floor: строк social_посевы >= SOCIAL_POSEV_ROW_FLOOR, telegram >= TELEGRAM_POSEV_ROW_FLOOR.
       Guard от over-deletion: если фикс срезал слишком агрессивно — число строк упадёт
       ниже baseline → FAIL. Грубый (domain,date)-дедуп давал ~819 лишних срезанных строк.

    Источник для A: big_analytics_unified (_source_table, domain, "Date", utm_campaign, kol_vo_zayavok)
                    + gsheets_crop_targeting_account + crop_targeting_api_telegain_lead
    Источник для B: big_analytics_unified (_source_table)

    POSEVDEDUP2 добавлен 2026-06-19 — замена грубого (domain,date) на lead_id через gsheets.
    POSEVDEDUP3 добавлен 2026-06-19 — расширение под-инварианта A вторым путём (API).
    POSEVDEDUP6 добавлен 2026-06-20 — переход на фактический выход step3 (big_analytics_unified
                join по crm_lead_id). Устранена тавтология POSEVDEDUP5 (¬G∧¬P)∧(G∨P)≡FALSE.
    """
    T_RAW = 'public.raw_leads'
    T_LOCAL = 'public.local_leads_all'
    T_DOMAINS = 'public.local_domains'
    T_GSHEETS_ACCT = 'public.gsheets_crop_targeting_account'
    T_PRAVILO = 'public.gsheets_crop_targeting_account_pravilo_utm'

    # A) Инвариант дедупа посевов (POSEVDEDUP6_2026-06-20)
    #
    # История проблем с предыдущими версиями:
    # POSEVDEDUP5_2026-06-20: предикат (NOT EXISTS gsheets AND NOT EXISTS API) AND
    #   (EXISTS gsheets OR EXISTS API) — тавтология (¬G ∧ ¬P) ∧ (G ∨ P) ≡ FALSE:
    #   COUNT всегда 0 независимо от данных. Реальное задвоение не ловится.
    # BLOCK13FIX_2026-06-19: INNER JOIN big_analytics_full по (domain, Date) без
    #   utm_campaign → ложные срабатывания (236 social + 394 tg ложных).
    #
    # Правильный инвариант — проверять ФАКТИЧЕСКИЙ ВЫХОД step3 через unified:
    #   step3 агрегирует social/telegram лидов по (domain, created_date, utm_campaign).
    #   Строка в big_analytics_unified с _source_table=social_посевы и SUM(korr)>0
    #   означает что ≥1 лид за эту (domain, date, utm_campaign) прошёл дедуп step3.
    #
    #   Реальный дубль = (domain, date, utm_campaign)-группа из local_leads_all которая:
    #   (A) реально попала в big_analytics_unified как social/telegram —
    #       JOIN по (domain, "Date", utm_campaign) + _source_table + kol_vo_zayavok > 0
    #       (step3 не исключил лидов этой группы)
    #   (B) И при этом группа попадает под crop-критерий (EXISTS gsheets ИЛИ API) —
    #       лиды должны были быть исключены
    #   => группа одновременно в social/telegram unified И в crop → реальный дубль.
    #
    # При корректном step3 дедупе: строк в unified с (domain,date,utm_campaign) в crop = 0.
    # При сломанном step3: kol_vo_zayavok > 0 в social строке unified → COUNT > 0.
    #
    # Пример сконструированного задвоения (для ручной проверки):
    #   INSERT в leads_deduped строку с utm_source='max', utm_medium='posev',
    #   domain='site.ru', utm_campaign='abc', created_date='2026-05-15'
    #   при наличии crop_targeting_api_telegain_lead: utm_campaign='abc', domain='site.ru',
    #   Date='2026-05-10'. Если step3 дедуп сломан — строка с _source_table='social_посевы'
    #   появится в unified с kol_vo_zayavok=1 → дубль поймается (COUNT=1).
    #
    # Зернистость: группа (domain, date, utm_campaign), не lead_id. Это достаточно для
    # обнаружения проблемы дедупа — если step3 работает корректно, ни одна такая группа
    # не пройдёт одновременно в unified и в crop. Если сломан — COUNT>0 указывает на
    # конкретные (domain, date, utm) для расследования.
    #
    # big_analytics_unified жива во время verify (cleanup после verify) — безопасно.
    # big_analytics_full к этому моменту TRUNCATE'нута (SPEND_PREFREE_FULL) — не используем.
    T_API  = 'public.crop_targeting_api_telegain_lead'
    T_UNI  = 'public.big_analytics_unified'

    cur.execute(f"""
        WITH
        -- Кандидаты из big_analytics_unified: social-строки с реальными лидами
        -- (kol_vo_zayavok > 0 означает что step3 не исключил эту (domain,date,utm)-группу)
        social_in_unified AS (
            SELECT
                LOWER(TRIM(domain))  AS domain,
                "Date"::date         AS created_date,
                campaign_code        AS utm_campaign
            FROM {T_UNI}
            WHERE _source_table = 'social_посевы'
              AND kol_vo_zayavok > 0
              AND "Date" >= '2026-01-01'
              AND campaign_code IS NOT NULL
        ),
        -- Кандидаты telegram: строки telegram/telegram_посевы из unified с реальными лидами
        telegram_in_unified AS (
            SELECT
                LOWER(TRIM(domain))  AS domain,
                "Date"::date         AS created_date,
                campaign_code        AS utm_campaign
            FROM {T_UNI}
            WHERE _source_table IN ('telegram', 'telegram_посевы')
              AND kol_vo_zayavok > 0
              AND "Date" >= '2026-01-01'
              AND campaign_code IS NOT NULL
        ),
        -- POSEVDEDUP6_2026-06-20: считаем (domain,date,utm_campaign)-группы которые
        -- (A) прошли в social_посевы unified (step3 не исключил) И
        -- (B) попадают под crop-критерий (gsheets ИЛИ API) — должны были быть исключены.
        -- При корректном step3 = 0. При сломанном > 0 (конкретные группы-дубли).
        dup_social AS (
            SELECT COUNT(*) AS cnt
            FROM social_in_unified su
            WHERE
            -- (B) эта (domain,date,utm)-группа должна была быть исключена crop-критерием
            (
                -- Путь 1: gsheets (utm через pravilo + nearest-prior placement)
                EXISTS (
                    SELECT 1 FROM {T_PRAVILO} pr
                    WHERE (
                        LOWER(TRIM(CASE
                            WHEN pr."UTM" IS NOT NULL AND TRIM(pr."UTM") != '' THEN pr."UTM"
                            ELSE pr."utm утвержденная"
                        END)) = LOWER(TRIM(su.utm_campaign))
                    )
                    AND COALESCE(TRIM(pr."utm утвержденная"), '') != '-'
                    AND COALESCE(TRIM(pr."UTM"), '') != '-'
                    AND EXISTS (
                        SELECT 1 FROM {T_GSHEETS_ACCT} ga
                        WHERE ga."utm утвержденная" = pr."utm утвержденная"
                          AND LOWER(TRIM(ga."Сайт")) = su.domain
                          AND NULLIF(TRIM(ga."Дата"), '') IS NOT NULL
                          AND TO_DATE(NULLIF(TRIM(ga."Дата"), ''), 'FMDD.FMMM.YYYY')
                                  <= su.created_date
                          AND TO_DATE(NULLIF(TRIM(ga."Дата"), ''), 'FMDD.FMMM.YYYY')
                                  >= (su.created_date - INTERVAL '90 days')::date
                          AND TO_DATE(NULLIF(TRIM(ga."Дата"), ''), 'FMDD.FMMM.YYYY')
                                  >= '2026-01-01'
                    )
                )
                -- Путь 2: API (utm_campaign + domain + месяц±1, только с 2026-05-01)
                OR (
                    su.created_date >= '2026-05-01'
                    AND EXISTS (
                        SELECT 1 FROM {T_API} t
                        WHERE t.utm_campaign = su.utm_campaign
                          AND LOWER(TRIM(t.domain)) = su.domain
                          AND DATE_TRUNC('month', t."Date")::date BETWEEN
                                  (DATE_TRUNC('month', su.created_date) - INTERVAL '1 month')::date
                              AND (DATE_TRUNC('month', su.created_date) + INTERVAL '1 month')::date
                          AND t."Date" >= '2026-05-01'
                    )
                )
            )
        ),
        dup_telegram AS (
            SELECT COUNT(*) AS cnt
            FROM telegram_in_unified tu
            WHERE
            -- (B) эта (domain,date,utm)-группа должна была быть исключена crop-критерием
            (
                -- Путь 1: gsheets
                EXISTS (
                    SELECT 1 FROM {T_PRAVILO} pr
                    WHERE (
                        LOWER(TRIM(CASE
                            WHEN pr."UTM" IS NOT NULL AND TRIM(pr."UTM") != '' THEN pr."UTM"
                            ELSE pr."utm утвержденная"
                        END)) = LOWER(TRIM(tu.utm_campaign))
                    )
                    AND COALESCE(TRIM(pr."utm утвержденная"), '') != '-'
                    AND COALESCE(TRIM(pr."UTM"), '') != '-'
                    AND EXISTS (
                        SELECT 1 FROM {T_GSHEETS_ACCT} ga
                        WHERE ga."utm утвержденная" = pr."utm утвержденная"
                          AND LOWER(TRIM(ga."Сайт")) = tu.domain
                          AND NULLIF(TRIM(ga."Дата"), '') IS NOT NULL
                          AND TO_DATE(NULLIF(TRIM(ga."Дата"), ''), 'FMDD.FMMM.YYYY')
                                  <= tu.created_date
                          AND TO_DATE(NULLIF(TRIM(ga."Дата"), ''), 'FMDD.FMMM.YYYY')
                                  >= (tu.created_date - INTERVAL '90 days')::date
                          AND TO_DATE(NULLIF(TRIM(ga."Дата"), ''), 'FMDD.FMMM.YYYY')
                                  >= '2026-01-01'
                    )
                )
                -- Путь 2: API
                OR (
                    tu.created_date >= '2026-05-01'
                    AND EXISTS (
                        SELECT 1 FROM {T_API} t
                        WHERE t.utm_campaign = tu.utm_campaign
                          AND LOWER(TRIM(t.domain)) = tu.domain
                          AND DATE_TRUNC('month', t."Date")::date BETWEEN
                                  (DATE_TRUNC('month', tu.created_date) - INTERVAL '1 month')::date
                              AND (DATE_TRUNC('month', tu.created_date) + INTERVAL '1 month')::date
                          AND t."Date" >= '2026-05-01'
                    )
                )
            )
        )
        SELECT
            (SELECT cnt FROM dup_social)    AS social_dup_leads,
            (SELECT cnt FROM dup_telegram)  AS tg_dup_leads
    """)
    social_dups, tg_dups = cur.fetchone()
    social_dups = int(social_dups or 0)
    tg_dups     = int(tg_dups or 0)

    # B) Count-floor: строк social_посевы / telegram_посевы в big_analytics_unified
    # POSEVDEDUP5_2026-06-20: переключено с T_FULL (big_analytics_full) на big_analytics_unified.
    # SPEND_PREFREE_FULL_2026-06-20 добавила big_analytics_full в TRUNCATE перед spend-фазой
    # (освобождает ~9 GB диска). verify запускается ПОСЛЕ spend-фазы => T_FULL уже пустая.
    # big_analytics_unified (build_unified output) содержит те же _source_table строки +
    # arrival-строки; cleanup_intermediate TRUNCATE'ит unified ПОСЛЕ verify => она жива здесь.
    # BLOCK13FIX_2026-06-19: step3 пишет _source_table='telegram' (не 'telegram_посевы')
    # для telegram-посевов — IN ('telegram','telegram_посевы') ловит оба варианта.
    cur.execute("""
        SELECT
            COUNT(*) FILTER (WHERE _source_table = 'social_посевы')                        AS social_rows,
            COUNT(*) FILTER (WHERE _source_table IN ('telegram', 'telegram_посевы'))        AS tg_rows
        FROM public.big_analytics_unified
        WHERE _source_table IN ('social_посевы', 'telegram', 'telegram_посевы')
    """)
    social_rows, tg_rows = cur.fetchone()
    social_rows = int(social_rows or 0)
    tg_rows     = int(tg_rows or 0)

    dup_ok   = (social_dups == 0) and (tg_dups == 0)
    floor_ok = (social_rows >= SOCIAL_POSEV_ROW_FLOOR) and (tg_rows >= TELEGRAM_POSEV_ROW_FLOOR)
    ok = dup_ok and floor_ok

    detail_parts = [
        f'social_dup_leads={social_dups} (exp 0), tg_dup_leads={tg_dups} (exp 0)',
        f'social_rows={social_rows} (floor>={SOCIAL_POSEV_ROW_FLOOR}), '
        f'tg_rows={tg_rows} (floor>={TELEGRAM_POSEV_ROW_FLOOR})',
    ]
    if not dup_ok:
        detail_parts.append('FAIL: есть задвоенные lead_id в social/telegram ∩ crop_targeting')
    if not floor_ok:
        detail_parts.append('FAIL: over-deletion — строк меньше floor (фикс срезал лишнее)')
    detail = '; '.join(detail_parts)
    return Block(13, 'Нет задвоения посевов (POSEVDEDUP6)', ok, detail)


# ── Блок 14: Стоимость квала без пикселя (KVAL_COST_CHECK_2026-06-28) ─────────
def check_14_kval_cost(cur) -> Block:
    """SOFT-WARNING: стоимость квала без пикселя (KVAL_COST_CHECK_2026-06-28).

    stoimost_kvala = SUM(total_cost) / SUM(kval)
    Источник: fact_big_analytics (durable — жива после cleanup_intermediate).
    Фильтр: "атрибуция"='По дате заявки' AND _source_table NOT IN ('пиксель','пиксель_атрибуц').

    Норма: KVAL_COST_LO <= stoimost_kvala <= KVAL_COST_HI  (10000..30000).
    KVAL_REBASELINE_2026-07-15: category-kval — КОРРЕКТНАЯ формула (решение пользователя).
    Текущий эталонный факт: ~20913 ₽/квал (лежит по центру диапазона, PASS).
    Регрессия = откат к старой korr-negation формуле → стоимость падает к ~8870 ₽ и
    пробивает нижнюю границу 10000 → сигнал.

    SOFT-WARNING: ok=True ВСЕГДА — не влияет на exit code и не блокирует прогон.
    Вне диапазона → ⚠️ явный в detail (виден в verify-отчёте и Telegram-дайджесте).
    Обоснование SOFT: диапазон [10000;30000] широк (×3), случайный дрейф не должен
    блокировать CI-прогон; цель — мониторинг регрессии kval-формулы, не hard-блок.
    """
    T_FACT = 'public.fact_big_analytics'
    PIXEL_SRCS = ('пиксель', 'пиксель_атрибуц')
    cur.execute(f"""
        SELECT
            COALESCE(ROUND(SUM(total_cost)::numeric, 2), 0) AS total_cost,
            COALESCE(ROUND(SUM(kval)::numeric,       2), 0) AS total_kval
        FROM {T_FACT}
        WHERE "атрибуция" = 'По дате заявки'
          AND _source_table NOT IN %s
    """, (PIXEL_SRCS,))
    row = cur.fetchone()
    total_cost = Decimal(str(row[0])) if row and row[0] is not None else Decimal('0')
    total_kval = Decimal(str(row[1])) if row and row[1] is not None else Decimal('0')

    if total_kval <= 0:
        return Block(14, 'Стоимость квала без пикселя', True,
                     f'SKIP: kval=0 (нет данных без пикселя); total_cost={total_cost:,.0f}')

    kval_cost = total_cost / total_kval
    kval_cost_int = int(kval_cost.to_integral_value())
    in_range = KVAL_COST_LO <= kval_cost_int <= KVAL_COST_HI
    range_str = f'[{KVAL_COST_LO:,};{KVAL_COST_HI:,}]'
    if in_range:
        detail = (
            f'{kval_cost_int:,} ₽ {range_str} ✅ '
            f'(cost={total_cost:,.0f}, kval={total_kval:,.0f})'
        )
    else:
        detail = (
            f'⚠️ {kval_cost_int:,} ₽ вне {range_str} — возможна ошибка kval '
            f'(cost={total_cost:,.0f}, kval={total_kval:,.0f})'
        )
    # SOFT-WARNING: ok=True всегда (не валит exit code, не блокирует пайплайн).
    return Block(14, 'Стоимость квала без пикселя', True, detail)


# ── Прогон быстрого гейта (блоки 1–14) ───────────────────────────────────────
def run_all() -> list[Block]:
    params = dict(DB_DST)
    conn = psycopg2.connect(**params)
    try:
        conn.set_session(readonly=True, autocommit=True)
        blocks: list[Block] = []
        # Блок 7 запускаем отдельно — возвращает список из трёх Block.
        # FUNNEL_I3_PIXEL_FIX_2026-06-23: пиксель вынесен в отдельную строку.
        # POSEV_VISIT_FUNNEL_FIX_2026-06-24: посевы визит-оси вынесены в третью строку.
        with conn.cursor() as cur:
            try:
                b7_list = check_7_funnel_i3(cur)
            except Exception as exc:
                logger.error('Блок 7 (check_7_funnel_i3) упал: %s', exc, exc_info=True)
                conn.rollback()
                b7_list = [Block(7, 'Воронка (без пикселя)', False, f'ошибка проверки: {exc}'),
                           Block(7, 'Пиксель-инвариант (пиксель+пиксель_атрибуц)', False, f'ошибка проверки: {exc}'),
                           Block(7, 'Посевы визит-инвариант (by-design korr=kval=0)', False, f'ошибка проверки: {exc}')]

        # Порядковый прогон: блоки 1-6, затем 7 (три), затем 8-13
        checks_before_7 = [
            check_1_golden, check_2_conservation, check_3_dup_directions,
            check_4_calls_vs_posev, check_5_pixel_visit, check_6_pixel_fractional,
        ]
        checks_after_7 = [
            check_8_grand_total, check_9_freshness,
            check_10_pixel_score, check_11_salon_funnel, check_12_marcar_gsheet_coverage,
            check_13_posev_dedup, check_14_kval_cost,
        ]
        for i, fn in enumerate(checks_before_7, 1):
            with conn.cursor() as cur:
                try:
                    blocks.append(fn(cur))
                except Exception as exc:
                    logger.error('Блок %d (%s) упал: %s', i, fn.__name__, exc, exc_info=True)
                    conn.rollback()
                    blocks.append(Block(i, fn.__name__, False, f'ошибка проверки: {exc}'))
        # Блок 7 — три результата (воронка без пикселя/посевов + пиксель-инвариант + посевы визит-инвариант)
        blocks.extend(b7_list)
        for i, fn in enumerate(checks_after_7, 8):
            with conn.cursor() as cur:
                try:
                    blocks.append(fn(cur))
                except Exception as exc:
                    logger.error('Блок %d (%s) упал: %s', i, fn.__name__, exc, exc_info=True)
                    conn.rollback()
                    blocks.append(Block(i, fn.__name__, False, f'ошибка проверки: {exc}'))
        return blocks
    finally:
        conn.close()


# ── МАСТЕР-РЕЖИМ: операционка (data_check/run.py checks) ──────────────────────
# VERIFY_BIG_ANALYTICS_MASTER_FULL
def run_operational() -> list[Block]:
    """Блок 12: операционные проверки — переиспользуем существующие checks/*.py
    (БЕЗ дублирования SQL). Может тянуть Google Sheets (projects) — поэтому только
    в --full. Каждый сабчек = свой Block с номером 12.x. Любой сбой = FAIL-блок,
    но не валит весь скрипт (тянет за собой только тот сабчек).
    (Номер сдвинут с 11 на 12 — 11 теперь занят быстрым гейтом: I_salon.)"""
    blocks: list[Block] = []
    try:
        from data_check.checks import projects, fields, spending, funnel
        from data_check.checks.projects import SPREADSHEET_ID, GID
        from data_check.sheets_reader import read_sheet
    except Exception as exc:
        return [Block(12, 'Операционка (импорт)', False, f'не удалось импортировать checks: {exc}')]

    db = dict(DB_DST)

    # 12.1 — покрытие доменов (Google Sheets ↔ local_gsheet_sites ↔ big_analytics_full)
    try:
        sheets_rows = read_sheet(SPREADSHEET_ID, GID)
        pr = projects.run(db, sheets_rows)
        only_sheets = pr.get('only_in_sheets', [])
        no_data = pr.get('no_analytics_data', [])
        crit = len(only_sheets) + len(no_data)
        detail = (f'только в Sheets={len(only_sheets)}, в БД-без-данных={len(no_data)}, '
                  f'только в БД={len(pr.get("only_in_db", []))}')
        blocks.append(Block(12, 'Покрытие доменов (Sheets)', crit == 0, detail))
    except Exception as exc:
        blocks.append(Block(12, 'Покрытие доменов (Sheets)', False, f'ошибка/нет Sheets: {exc}'))

    # 12.2 — расход без лидов за 7 дней
    try:
        sp = spending.run(db)
        snl = sp.get('spend_no_leads_7d', [])
        detail = (f'проджектов с расходом без лидов (7д)={len(snl)}'
                  + (': ' + ', '.join(r['проджект'] for r in snl[:5]) if snl else ''))
        blocks.append(Block(12, 'Расход без лидов (7д)', len(snl) == 0, detail))
    except Exception as exc:
        blocks.append(Block(12, 'Расход без лидов (7д)', False, f'ошибка: {exc}'))

    # 12.3 — нарушения воронки по проджектам (per-project, 30 дней)
    try:
        fn = funnel.run(db)
        viol = fn.get('invariant_violations', [])
        detail = (f'проджектов с нарушением воронки (30д)={len(viol)}'
                  + (': ' + ', '.join(v['проджект'] for v in viol[:5]) if viol else ''))
        blocks.append(Block(12, 'Воронка по проджектам (30д)', len(viol) == 0, detail))
    except Exception as exc:
        blocks.append(Block(12, 'Воронка по проджектам (30д)', False, f'ошибка: {exc}'))

    # 12.4 — NULL ключевые поля local_gsheet_sites (предупреждение, НЕ валит гейт)
    try:
        fl = fields.run(db)
        nd = len(fl.get('null_directologist', []))
        nm = len(fl.get('null_manager_login', []))
        nk = len(fl.get('null_login_key', []))
        detail = (f'NULL: directologist={nd}, manager_login={nm}, login_key={nk} '
                  f'(предупреждение — не валит гейт)')
        # NULL-поля справочника — операционное предупреждение, всегда PASS.
        blocks.append(Block(12, 'NULL-поля справочника (warn)', True, detail))
    except Exception as exc:
        blocks.append(Block(12, 'NULL-поля справочника (warn)', False, f'ошибка: {exc}'))

    return blocks


# ── МАСТЕР-РЕЖИМ: star-сверка (public.fact_big_analytics vs unified) ────────────
def run_star() -> list[Block]:
    """Блок 13: read-only сверка звезды vs unified + ARP. Пропуск если public.fact
    пуста / отсутствует (build_star не прогонялся). Логика повторяет verify_star.py
    в read-only виде (суммы по обеим осям + ARP), без зависимости от его print-вывода.
    (Номер сдвинут с 12 на 13 — 12 теперь занято операционкой.)"""
    OLD = 'public.big_analytics_unified'
    NEW = 'public.fact_big_analytics'
    METRICS = ['total_cost', 'korr', 'kval', 'priezd', 'prodazhi', 'kol_vo_zayavok']
    blocks: list[Block] = []
    conn = psycopg2.connect(**dict(DB_DST))
    try:
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor() as cur:
            exists = _scalar(cur, "SELECT to_regclass(%s) IS NOT NULL", (NEW,))
            if not exists:
                return [Block(13, 'Звезда vs unified', True, 'skipped (public.fact_big_analytics отсутствует)')]
            n = int(_scalar(cur, f'SELECT COUNT(*) FROM {NEW}') or 0)
            if n == 0:
                return [Block(13, 'Звезда vs unified', True, 'skipped (public.fact_big_analytics пуста — build_star не прогонялся)')]

            sel = ', '.join(f'ROUND(COALESCE(SUM({m}),0)::numeric,2)' for m in METRICS)
            # 13.1 суммы по обеим осям
            for axis, where in (('заявка', "\"атрибуция\"='По дате заявки'"),
                                ('визит', "\"атрибуция\"='По дате визита'")):
                cur.execute(f'SELECT {sel} FROM {OLD} WHERE {where}')
                o = cur.fetchone()
                cur.execute(f'SELECT {sel} FROM {NEW} WHERE {where}')
                nv = cur.fetchone()
                diffs = [(m, float(a), float(b)) for m, a, b in zip(METRICS, o, nv)
                         if abs(float(a) - float(b)) > 0.01]
                ok = not diffs
                detail = ('все метрики совпали' if ok
                          else 'DIFF: ' + '; '.join(f'{m} unified={a} star={b}' for m, a, b in diffs))
                blocks.append(Block(13, f'Звезда vs unified ({axis}-ось)', ok, detail))

            # 13.2 ARP: public.arp_fact vs analytics_report_placement
            arp_metrics = ['cost', 'clicks', 'priezd', 'prodazhi', 'korr', 'kval']
            try:
                diffs = []
                for m in arp_metrics:
                    o = _scalar(cur, f'SELECT ROUND(COALESCE(SUM({m}),0)::numeric,2) FROM public.analytics_report_placement')
                    nv = _scalar(cur, f'SELECT ROUND(COALESCE(SUM({m}),0)::numeric,2) FROM public.arp_fact')
                    if abs(float(o) - float(nv)) > 0.01:
                        diffs.append((m, float(o), float(nv)))
                ok = not diffs
                detail = ('cost/clicks/воронка ARP совпали' if ok
                          else 'DIFF: ' + '; '.join(f'{m} arp={a} star={b}' for m, a, b in diffs))
                blocks.append(Block(13, 'Звезда ARP vs analytics_report_placement', ok, detail))
            except Exception as exc:
                blocks.append(Block(13, 'Звезда ARP vs analytics_report_placement', False, f'ошибка: {exc}'))
        return blocks
    finally:
        conn.close()


# ── Форматирование отчёта ────────────────────────────────────────────────────
def format_report(blocks: list[Block]) -> str:
    all_pass = all(b.ok for b in blocks)
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    n_fail = sum(1 for b in blocks if not b.ok)
    mode = 'master --full' if any(b.num >= 12 for b in blocks) else 'gate (11 блоков)'
    header = ('🟢 big_analytics: ВСЁ PASS' if all_pass
              else f'❌ big_analytics: ЕСТЬ ПРОБЛЕМЫ ({n_fail} FAIL)')
    lines = [header, f'{ts} · read-only verify · {mode}', '']
    for b in blocks:
        lines.append(b.render())
    return '\n'.join(lines)


# ── Дайджест полноты+правильности (консолидированный TG-дайджест) ────────────
# PIPELINE_DIGEST_2026-06-17

def _fetch_digest_extras(pipeline_name: str) -> dict:
    """Read-only запрос к БД: данные полноты для дайджеста.

    Читает: fact_big_analytics (golden, durable), big_analytics_full (строки /
    источники / NULL / свежесть), data_quality_log (шаги последнего run_id).
    Никогда не пишет в БД. Не пробрасывает исключения — при сбое возвращает {}.
    """
    try:
        conn = psycopg2.connect(**dict(DB_DST))
        conn.set_session(readonly=True, autocommit=True)
        d: dict = {'pipeline': pipeline_name}
        with conn.cursor() as cur:

            # Строки big_analytics_full
            cur.execute(f'SELECT COUNT(*) FROM {T_FULL}')
            d['rows_full'] = int(cur.fetchone()[0] or 0)

            # Источники (_source_table): N из N присутствуют
            cur.execute(f"""
                SELECT _source_table, COUNT(*)
                FROM {T_FULL}
                GROUP BY _source_table
                ORDER BY _source_table
            """)
            d['source_counts'] = {r[0]: int(r[1]) for r in cur.fetchall()}

            # NULL по ключевым полям (источник, domain, "специалист")
            cur.execute(f"""
                SELECT
                    COUNT(*) FILTER (WHERE источник IS NULL)     AS null_src,
                    COUNT(*) FILTER (WHERE domain IS NULL)        AS null_domain,
                    COUNT(*) FILTER (WHERE "специалист" IS NULL
                                       AND _source_table != 'pixel'
                                       AND _source_table != 'пиксель_атрибуц')
                                                                  AS null_spec
                FROM {T_FULL}
            """)
            row = cur.fetchone()
            d['null_src']    = int(row[0] or 0)
            d['null_domain'] = int(row[1] or 0)
            d['null_spec']   = int(row[2] or 0)

            # Свежесть: max(Date) в full
            cur.execute(f'SELECT MAX("Date") FROM {T_FULL}')
            d['max_date'] = cur.fetchone()[0]  # date или None

            # Атрибуция визит >= заявка (по продажам): только Контекст/SEO/SEO Flow —
            # на этих направлениях обе оси полны (step13_arrival не фильтрует их).
            # На grand total инвариант by-design ниже нормы:
            #   посевы step13 отфильтровывает (direction='Авто', ATTRIBUTION.md L52),
            #   пиксель_атрибуц сидит только в заявочной оси без визит-строк.
            # PIPELINE_DIGEST_2026-06-17
            cur.execute("""
                SELECT "атрибуция",
                       ROUND(SUM(prodazhi)) AS prod
                FROM public.fact_big_analytics
                WHERE направление = 'Комплекс'  -- KOMPLEKS_REFACTOR_REDO_2026-07-09
                GROUP BY 1
            """)
            attr_sales: dict = {}
            for attr, prod in cur.fetchall():
                attr_sales[attr] = int(prod or 0)
            visit_sales  = attr_sales.get('По дате визита',  0)
            claim_sales  = attr_sales.get('По дате заявки', 0)
            d['attr_visit_sales'] = visit_sales
            d['attr_claim_sales'] = claim_sales
            d['attr_ok'] = (visit_sales >= claim_sales)

            # Шаги последнего run_id из data_quality_log
            try:
                cur.execute(f"""
                    SELECT run_id FROM {T_DATA_QUALITY_LOG}
                    ORDER BY id DESC LIMIT 1
                """)
                row = cur.fetchone()
                last_run_id = row[0] if row else None
                d['last_run_id'] = last_run_id

                if last_run_id:
                    cur.execute(f"""
                        SELECT step, status
                        FROM {T_DATA_QUALITY_LOG}
                        WHERE run_id = %s
                        ORDER BY id
                    """, (last_run_id,))
                    steps_raw = cur.fetchall()
                    # Последний статус каждого шага (может повторяться при retry)
                    steps_last: dict = {}
                    for step, status in steps_raw:
                        steps_last[step] = status
                    d['steps_ok']   = [s for s, st in steps_last.items() if st == 'ok']
                    d['steps_fail'] = [s for s, st in steps_last.items() if st not in ('ok', 'skipped')]  # SKIPPED_NOT_FAIL_2026-06-21: штатный skip (compactify_full при низком bloat) не помечаем FAIL
                    d['steps_total'] = len(steps_last)
                else:
                    d['steps_ok']    = []
                    d['steps_fail']  = []
                    d['steps_total'] = 0
            except Exception as exc:
                logger.warning('digest: data_quality_log: %s', exc)
                d['steps_ok']    = []
                d['steps_fail']  = []
                d['steps_total'] = 0

        conn.close()
        return d
    except Exception as exc:
        logger.warning('digest extras: %s', exc)
        return {}


def format_digest(blocks: list[Block], extras: dict, pipeline_name: str) -> str:
    """Компактный TG-дайджест с явным чеклистом всех golden-проверок + полнота.

    PIPELINE_DIGEST_2026-06-17 (переработан DIGEST_CHECKLIST_2026-07-02):
    Каждая проверка — одна строка с ✅/❌ и названием.
    Для FAIL-блоков — дополнительная строка с причиной.
    Блок 7 (три варианта) помечается 7а/7б/7в.
    Блок 14 (SOFT-WARNING, ok=True) — ⚠️ если вне диапазона.
    """
    all_ok = all(b.ok for b in blocks)
    ts = datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M UTC')
    icon = '✅' if all_ok else '⚠️'
    pipe_label = pipeline_name or 'pipeline'

    lines: list[str] = []
    lines.append(f'{icon} big_analytics · {pipe_label} · {ts}')
    lines.append('')

    # ── GOLDEN ЧЕКЛИСТ — каждая проверка явно на одной строке ────────────────
    # DIGEST_CHECKLIST_2026-07-02: вместо текстовых блоков — пронумерованный список.
    lines.append('<b>GOLDEN-проверки:</b>')
    block7_idx = 0
    block7_labels = ['7а', '7б', '7в']
    for b in blocks:
        # Определяем метку блока (7а/7б/7в для трёх блоков воронки)
        if b.num == 7:
            lbl = block7_labels[block7_idx] if block7_idx < len(block7_labels) else '7?'
            block7_idx += 1
        else:
            lbl = str(b.num)

        # Блок 14 — SOFT-WARNING: ok=True всегда, но ⚠️ если вне диапазона
        if b.num == 14:
            mark = '⚠️' if '⚠️' in b.detail else '✅'
        else:
            mark = '✅' if b.ok else '❌'

        lines.append(f'  {mark} {lbl}. {b.title}')
        # Для FAILs (и SOFT-WARN блока 14) добавляем первые 100 символов detail
        if not b.ok or (b.num == 14 and '⚠️' in b.detail):
            lines.append(f'     → {b.detail[:100]}')

    lines.append('')

    # ── КЛЮЧЕВЫЕ ЦИФРЫ (только PASS-детали для самых важных блоков) ──────────
    b1 = next((b for b in blocks if b.num == 1), None)
    if b1:
        g_icon = '✅' if b1.ok else '❌'
        lines.append(f'{g_icon} Golden: {b1.detail[:160]}')

    # Атрибуция визит >= заявка (по продажам, срез Контекст/SEO/SEO Flow)
    if extras:
        av = extras.get('attr_visit_sales', 0)
        ac = extras.get('attr_claim_sales', 0)
        a_icon = '✅' if extras.get('attr_ok', True) else '❌'
        lines.append(f'{a_icon} Атрибуция (Контекст/SEO): визит={av} >= заявка={ac}')
    else:
        lines.append('? Атрибуция Контекст/SEO: нет данных')

    lines.append('')

    # ── ПОЛНОТА ───────────────────────────────────────────────────────────────
    if extras:
        rows_full = extras.get('rows_full', 0)
        lines.append(f'Строк big_analytics_full: {rows_full:,}')

        # Источники
        src_counts = extras.get('source_counts', {})
        n_src = len(src_counts)
        if n_src:
            lines.append(f'Источников: {n_src}')
            for src_k, src_v in sorted(src_counts.items()):
                lines.append(f'  {src_k} = {src_v:,}')
        else:
            lines.append('Источников: нет данных')

        # NULL ключевых полей
        null_src  = extras.get('null_src',    0)
        null_dom  = extras.get('null_domain', 0)
        null_spec = extras.get('null_spec',   0)
        if null_src == 0 and null_dom == 0 and null_spec == 0:
            lines.append('NULL ключевых полей: 0 ✅')
        else:
            null_parts = []
            if null_src:    null_parts.append(f'источник={null_src}')
            if null_dom:    null_parts.append(f'domain={null_dom}')
            if null_spec:   null_parts.append(f'специалист={null_spec}')
            lines.append(f'NULL ключевых: ⚠️ {", ".join(null_parts)}')

        # Свежесть
        max_date = extras.get('max_date')
        if max_date is not None:
            today = datetime.now(timezone.utc).date()
            age_d = (today - max_date).days
            f_icon = '✅' if age_d <= FRESHNESS_MAX_DAYS else '⚠️'
            lines.append(f'{f_icon} Свежесть: данные до {max_date} ({age_d} дн. назад)')
        else:
            lines.append('⚠️ Свежесть: max(Date) = NULL')

        # Шаги data_quality_log
        steps_ok   = extras.get('steps_ok', [])
        steps_fail = extras.get('steps_fail', [])
        steps_tot  = extras.get('steps_total', 0)
        if steps_tot:
            s_icon = '✅' if not steps_fail else '❌'
            lines.append(f'{s_icon} Шаги: {len(steps_ok)}/{steps_tot} OK')
            if steps_fail:
                lines.append(f'   FAIL шаги: {", ".join(steps_fail)}')
        else:
            lines.append('? Шаги: нет записей в data_quality_log')
    else:
        lines.append('(данные полноты недоступны — ошибка чтения БД)')

    return '\n'.join(lines)


# ── Telegram ─────────────────────────────────────────────────────────────────
def send_telegram(report: str) -> bool:
    """Отправка в Telegram с ротацией прокси (Amsterdam→DE→NL→FR→direct).
    Возвращает True если доставлено (HTTP200 + ok=true). Никогда не пробрасывает исключение."""
    try:
        import requests
        from config.tokens import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_PROXY_VARIANTS
    except Exception as exc:
        logger.warning('Telegram: не удалось загрузить креды/requests: %s', exc)
        return False

    url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage'
    payload = {'chat_id': TELEGRAM_CHAT_ID, 'text': report}
    for proxies in TELEGRAM_PROXY_VARIANTS:  # TG_PROXY_CHAIN_ROTATION_2026-06-17
        try:
            r = requests.post(url, json=payload, proxies=proxies, timeout=30)
            j = r.json()
            if r.ok and j.get('ok'):
                logger.info('Telegram доставлено (proxies=%s): message_id=%s',
                            proxies, j.get('result', {}).get('message_id'))
                return True
            logger.warning('Telegram (proxies=%s) ответ не ok: %s', proxies, r.text[:200])
        except Exception as exc:
            logger.warning('Telegram (proxies=%s) ошибка: %s', proxies, exc)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Единый мастер-чекер big_analytics_v5 (golden-инварианты + операционка + star)')
    parser.add_argument('--tg', action='store_true', help='отправить дайджест в Telegram')
    parser.add_argument('--full', action='store_true',
                        help='МАСТЕР-ПРОГОН: гейт + операционка (Sheets) + star-сверка')
    parser.add_argument('--no-star', action='store_true',
                        help='в --full пропустить star-сверку (блок 12)')
    parser.add_argument('--pipeline', default='pipeline',
                        help='имя пайплайна для заголовка дайджеста (pipeline/fast_pipeline/pipeline_powerbi)')
    args = parser.parse_args()

    try:
        # Блоки 1–11 — быстрый golden-гейт (так его зовут 3 пайплайна).
        blocks = run_all()
        # --full: дополнительные мастер-блоки (операционка + star).
        if args.full:
            blocks.extend(run_operational())
            if not args.no_star:
                blocks.extend(run_star())
    except Exception as exc:
        logger.error('verify_big_analytics: проверки упали: %s', exc, exc_info=True)
        return 2

    report = format_report(blocks)
    print(report)

    if args.tg:
        # PIPELINE_DIGEST_2026-06-17: при --tg шлём компактный дайджест
        # (правильность + полнота) вместо детального format_report.
        # Детальный report выводится в stdout/лог (строка выше) и остаётся
        # доступным для машинного разбора. В Telegram — только дайджест.
        try:
            extras = _fetch_digest_extras(args.pipeline)
        except Exception as _exc:
            logger.warning('digest extras: %s', _exc)
            extras = {}
        digest = format_digest(blocks, extras, args.pipeline)
        send_telegram(digest)

    return 0 if all(b.ok for b in blocks) else 1


if __name__ == '__main__':
    sys.exit(main())
