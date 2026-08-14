"""
step8_stats/funnel_drift_snapshot.py — снимок воронки по (month × источник) + алерт дрейфа.

ЗАЧЕМ:
  Метрики воронки (расход, заявки, продажи) при атрибуции «по дате заявки» пересчитываются
  каждый прогон: Яндекс дофинализирует расход задним числом, CRM-статусы дозревают.
  Этот модуль копит историю снапшотов и сигнализирует в Telegram когда что-то сдвинулось.

ТАБЛИЦЫ:
  public.data_funnel_drift_log — append-only, одна строка = (run_id, month, источник).
  public.v_funnel_change       — VIEW: Δ между двумя последними run_id по каждой (month, источник).

ВЫЗОВ:
  run(conn, run_id)              — запись снапшота + алерт (основной flow pipeline.py)
  run(conn, run_id, alert=False) — только запись снапшота без Telegram (тестирование)

ИДЕМПОТЕНТНОСТЬ:
  INSERT ... ON CONFLICT (run_id, month, источник) DO NOTHING — повторный вызов безопасен.

TELEGRAM-АЛЕРТ:
  Сравниваем текущий run_id с предыдущим (ближайший run_id != текущему по recorded_at DESC).
  Выводим ВСЕ месяцы × направления (включая нулевые Δ — полная картина каждый прогон).
  Флаг changed оставлен в view и доступен в строках — но НЕ используется для фильтрации.
  Три стоимости: CPL-заявки (cost/zayavki), стоимость визита (cost/vizity), стоимость
  продажи (cost/prodazhi). Деление на 0 → прочерк '—', не падает.
  Если сообщение длиннее 4096 символов — разбивается на несколько сообщений.

МАРКЕР: FUNNEL_DRIFT_SNAPSHOT_v2
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger('pipeline.funnel_drift')

# ─── Таблица снапшота ─────────────────────────────────────────────────────────

T_DRIFT = 'public.data_funnel_drift_log'

# MULTISTATEMENT_FIX_2026-07-02: psycopg2 не поддерживает multi-statement в одном execute() —
# каждый оператор выделен в отдельный элемент кортежа; run() итерирует по кортежу.
DDL_TABLE = (
    f"""CREATE TABLE IF NOT EXISTS {T_DRIFT} (
    id           SERIAL PRIMARY KEY,
    run_id       TEXT        NOT NULL,
    recorded_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    month        DATE        NOT NULL,
    источник     TEXT        NOT NULL,
    cost         NUMERIC(16, 4),   -- дробная атрибуция пикселя: НЕ приводить к int
    zayavki      BIGINT,           -- korr (квалифицированные обращения)
    vizity       BIGINT,           -- priezd
    prodazhi     BIGINT,
    UNIQUE (run_id, month, источник)
)""",
    f"CREATE INDEX IF NOT EXISTS idx_dfd_run_id   ON {T_DRIFT}(run_id)",
    f"CREATE INDEX IF NOT EXISTS idx_dfd_recorded ON {T_DRIFT}(recorded_at DESC)",
    f"CREATE INDEX IF NOT EXISTS idx_dfd_month    ON {T_DRIFT}(month)",
)

# SNAPSHOT_ON_UNIFIED_2026-06-20: переключено с big_analytics_full на big_analytics_unified.
# SPEND_PREFREE_FULL (pipeline.py ~L1305) TRUNCATE'ит big_analytics_full ПЕРЕД spend-фазой,
# а funnel_drift_snapshot вызывается ПОСЛЕ spend-фазы (pipeline.py L1577).
# big_analytics_unified жива до cleanup_intermediate (идёт после verify/step8) — безопасно.
# АХТУНГ (DELTA_AXIS_FIX_FUNNEL_DRIFT_2026-07-12, см. DELTA_AXIS_FIX_2026-07-10): big_analytics_unified =
# заявочная ось (big_analytics_full) ∪ визит-ось (big_analytics_full_arrival). direction='Авто' НЕ
# отсекает визит-ось — визит-строки тоже несут direction='Авто'. Единственный признак оси — колонка
# `атрибуция` ('По дате заявки' / 'По дате визита'). Поэтому ось снимка задаётся ЯВНЫМ фильтром
# `атрибуция='По дате заявки'` в WHERE ниже. Без него оси складывались → низ воронки задваивался
# (priezd/prodazhi ~×1.72, инцидент run 06971def).
# INSERT: агрегат по (month × источник) из big_analytics_unified.
# Пиксель-фильтр: направление NOT ILIKE '%пиксель%' исключает ВСЮ пиксель-ось ('пиксель' и
# 'пиксель_атрибуц', регистронезависимо) — funnel_drift это срез «без пикселя» (ср. алерт 'SEO без
# пикселя'). Это СОЗНАТЕЛЬНО шире узкого '<> Пиксель_атрибуц' в pipeline_log_snapshot, который
# оставляет 'Пиксель' для отдельного пиксель-компонента (тут пиксель-компонента нет).
# Поле «источник» — кириллическое, нормализуем COALESCE → '(неизвестно)' чтобы UNIQUE не ломался.
# cost: SUM(total_cost) оставляем NUMERIC (дробная пиксельная атрибуция — не приводить к int).
INSERT_SQL = f"""
INSERT INTO {T_DRIFT}
  (run_id, month, источник, cost, zayavki, vizity, prodazhi)
SELECT
  %s                                                     AS run_id,
  date_trunc('month', "Date")::date                      AS month,
  COALESCE(NULLIF(TRIM(источник), ''), '(неизвестно)')   AS источник,
  SUM(total_cost)                                        AS cost,
  SUM(korr)::bigint                                      AS zayavki,
  SUM(priezd)::bigint                                    AS vizity,
  SUM(prodazhi)::bigint                                  AS prodazhi
FROM public.big_analytics_unified
WHERE direction = 'Авто'
  AND (направление IS NULL OR направление NOT ILIKE '%%пиксель%%')
  AND атрибуция = 'По дате заявки'  -- DELTA_AXIS_FIX_FUNNEL_DRIFT_2026-07-12 (см. DELTA_AXIS_FIX_2026-07-10): только заявочная ось (иначе +визит-ось → двойной счёт priezd/prodazhi)
  AND "Date" IS NOT NULL
GROUP BY
  date_trunc('month', "Date"),
  COALESCE(NULLIF(TRIM(источник), ''), '(неизвестно)')
ON CONFLICT (run_id, month, источник) DO NOTHING
"""

# ─── VIEW для diff двух последних run_id ─────────────────────────────────────

# MULTISTATEMENT_FIX_2026-07-02: DROP VIEW и CREATE VIEW — два отдельных оператора
DDL_VIEW = (
    "DROP VIEW IF EXISTS public.v_funnel_change",
    """CREATE OR REPLACE VIEW public.v_funnel_change AS
WITH
ranked AS (
    SELECT run_id, recorded_at,
           DENSE_RANK() OVER (ORDER BY recorded_at DESC) AS rn
    FROM public.data_funnel_drift_log
    GROUP BY run_id, recorded_at
),
run_prev AS (
    SELECT run_id AS prev_run_id FROM ranked WHERE rn = 2 LIMIT 1
),
run_curr AS (
    SELECT run_id AS curr_run_id FROM ranked WHERE rn = 1 LIMIT 1
),
curr AS (
    SELECT d.month, d.источник, d.cost, d.zayavki, d.vizity, d.prodazhi, d.run_id, d.recorded_at
    FROM public.data_funnel_drift_log d
    JOIN run_curr ON d.run_id = run_curr.curr_run_id
),
prev AS (
    SELECT d.month, d.источник, d.cost, d.zayavki, d.vizity, d.prodazhi
    FROM public.data_funnel_drift_log d
    JOIN run_prev ON d.run_id = run_prev.prev_run_id
)
SELECT
    curr.run_id                                                  AS curr_run_id,
    (SELECT prev_run_id FROM run_prev)                           AS prev_run_id,
    curr.recorded_at,
    curr.month,
    curr.источник,
    -- текущие
    curr.cost                                                    AS cost_curr,
    curr.zayavki                                                 AS zayavki_curr,
    curr.vizity                                                  AS vizity_curr,
    curr.prodazhi                                                AS prodazhi_curr,
    -- предыдущие
    COALESCE(prev.cost,     0)                                   AS cost_prev,
    COALESCE(prev.zayavki, 0)                                    AS zayavki_prev,
    COALESCE(prev.vizity,  0)                                    AS vizity_prev,
    COALESCE(prev.prodazhi,0)                                    AS prodazhi_prev,
    -- дельты
    curr.cost     - COALESCE(prev.cost,     0)                   AS delta_cost,
    curr.zayavki  - COALESCE(prev.zayavki,  0)                   AS delta_zayavki,
    curr.vizity   - COALESCE(prev.vizity,   0)                   AS delta_vizity,
    curr.prodazhi - COALESCE(prev.prodazhi, 0)                   AS delta_prodazhi,
    -- CPL заявки (cost / zayavki) — стоимость привлечения заявки
    CASE WHEN curr.zayavki > 0
         THEN curr.cost / curr.zayavki
         ELSE NULL
    END                                                          AS cpl_zayavki_curr,
    CASE WHEN COALESCE(prev.zayavki, 0) > 0
         THEN COALESCE(prev.cost, 0) / prev.zayavki
         ELSE NULL
    END                                                          AS cpl_zayavki_prev,
    -- Стоимость визита (cost / vizity)
    CASE WHEN curr.vizity > 0
         THEN curr.cost / curr.vizity
         ELSE NULL
    END                                                          AS cost_per_visit_curr,
    CASE WHEN COALESCE(prev.vizity, 0) > 0
         THEN COALESCE(prev.cost, 0) / prev.vizity
         ELSE NULL
    END                                                          AS cost_per_visit_prev,
    -- Стоимость продажи (cost / prodazhi)
    CASE WHEN curr.prodazhi > 0
         THEN curr.cost / curr.prodazhi
         ELSE NULL
    END                                                          AS cost_per_sale_curr,
    CASE WHEN COALESCE(prev.prodazhi, 0) > 0
         THEN COALESCE(prev.cost, 0) / prev.prodazhi
         ELSE NULL
    END                                                          AS cost_per_sale_prev,
    -- флаги что изменилось
    CASE
        WHEN ABS(curr.cost - COALESCE(prev.cost, 0)) > 0.005
             AND ABS(curr.prodazhi - COALESCE(prev.prodazhi, 0)) > 0
            THEN 'cost+sales'
        WHEN ABS(curr.cost - COALESCE(prev.cost, 0)) > 0.005
            THEN 'cost'
        WHEN ABS(curr.prodazhi - COALESCE(prev.prodazhi, 0)) > 0
            THEN 'sales'
        ELSE 'none'
    END                                                          AS changed
FROM curr
LEFT JOIN prev USING (month, источник)
""",
)


# ─── Telegram-алерт ──────────────────────────────────────────────────────────
# Chunking (>4096 симв.) — на стороне notifications.telegram.send_html, не здесь.

# FUNNEL_DRIFT_MSG_CLARITY_2026-07-12: сколько последних месяцев показывать в TG-алерте.
# Полная история остаётся в data_funnel_drift_log (дашборд); «Итого» ниже считается по ВСЕМ месяцам.
_MAX_MONTHS_SHOWN = 3


def _golden_sales_range() -> tuple[int, int]:
    """Диапазон нормы итоговых продаж (заявка-ось, без пикселя, вся компания).

    Единый источник истины — мастер-чекер verify_big_analytics.py::GRAND_SALES_LO/HI,
    чтобы не дублировать константу. Импорт ленивый (не тянем config.settings в модуль-скоуп
    funnel_drift); при недоступности — fallback на те же цифры.
    ⚠️ Синхронизировать fallback (3000/3700) с data_check/verify_big_analytics.py::GRAND_SALES_LO/HI
       при изменении там.
    """
    try:
        from data_check.verify_big_analytics import GRAND_SALES_LO, GRAND_SALES_HI
        return int(GRAND_SALES_LO), int(GRAND_SALES_HI)
    except Exception:  # noqa: BLE001 — любой сбой импорта → безопасный fallback
        return 3000, 3700


def _aggregate_seo(rows: list[dict]) -> list[dict]:
    """SEO_AGGREGATE_2026-06-29: объединяет SEO-строки в одну «SEO (без пикселя)».

    Строки с источник='SEO' (регистр нечувствителен) складываются в один агрегат.
    Остальные источники (Контекст, звонки, посевы и т.д.) передаются без изменений.
    Переименование + суммирование: cost_curr/prev, zayavki, vizity, prodazhi, дельты.
    По данным 2026-06-29: в drift_log только один SEO-источник ('SEO') —
    агрегат тривиален (одна строка → переименование), но логика верна для N строк.
    """
    seo_rows   = [r for r in rows if (r.get('источник') or '').upper() == 'SEO']
    other_rows = [r for r in rows if (r.get('источник') or '').upper() != 'SEO']
    if not seo_rows:
        return rows
    # Агрегируем суммируемые метрики
    agg = dict(seo_rows[0])
    agg['источник'] = 'SEO (без пикселя)'
    for r in seo_rows[1:]:
        agg['cost_curr']      += r['cost_curr']
        agg['cost_prev']      += r['cost_prev']
        agg['zayavki_curr']   += r['zayavki_curr']
        agg['zayavki_prev']   += r['zayavki_prev']
        agg['vizity_curr']    += r['vizity_curr']
        agg['vizity_prev']    += r['vizity_prev']
        agg['prodazhi_curr']  += r['prodazhi_curr']
        agg['prodazhi_prev']  += r['prodazhi_prev']
        agg['delta_cost']     += r['delta_cost']
        agg['delta_zayavki']  += r['delta_zayavki']
        agg['delta_vizity']   += r['delta_vizity']
        agg['delta_prodazhi'] += r['delta_prodazhi']
    return other_rows + [agg]


def _fmt_cost(val) -> str:
    """Форматирует стоимость: None → '—', иначе RU-формат (NNBSP тысячи) + ₽."""
    if val is None:
        return '—'
    from notifications.telegram import format_ru_amount
    return f'{format_ru_amount(val)} ₽'


def _cost_arrow(curr, prev) -> str:
    """Стрелка ↑/↓ при сравнении curr > prev; '=' при равенстве; '?' если нет данных."""
    if curr is None or prev is None:
        return '?'
    if curr > prev:
        return '↑'
    if curr < prev:
        return '↓'
    return '='




def _send_drift_alert(rows: list[dict]) -> None:
    """Отправить алерт в Telegram по ВСЕМ (month × источник) — полная картина.

    Выводит все месяцы, включая нулевые Δ. Три стоимости:
      - CPL заявки  (cost / zayavki)
      - стоимость визита (cost / vizity)
      - стоимость продажи (cost / prodazhi)
    Деление на 0 → прочерк '—'. Длинное сообщение разбивается на несколько.

    Импортирует TELEGRAM_BOT_TOKEN/CHAT_ID/PROXY из config/tokens.py — они читаются
    из ~/.secret/.env (load_telegram('personal')), как во всём пайплайне.
    """
    if not rows:
        return

    try:
        from config.tokens import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_PROXY_VARIANTS
        from notifications.telegram import format_ru_amount, send_html
    except ImportError as e:
        logger.warning('funnel_drift: не удалось импортировать зависимости для TG: %s', e)
        return

    # FUNNEL_DRIFT_MSG_CLARITY_2026-07-12: явный охват периода — итог ниже это СУММА за ВЕСЬ
    # доступный период по ВСЕЙ компании (не один салон/месяц), иначе число выглядит нереально.
    _month_keys = sorted({(r['month'] or '')[:7] for r in rows if r.get('month')})
    if len(_month_keys) > 1:
        _period_str = f'{_month_keys[0]} … {_month_keys[-1]} ({len(_month_keys)} мес.)'
    elif _month_keys:
        _period_str = _month_keys[0]
    else:
        _period_str = 'период не определён'

    lines = [
        '<b>Воронка по месяцам (по дате заявки)</b>',
        f'<code>{datetime.now().strftime("%d.%m.%Y %H:%M")}</code>',
        f'период: {_period_str} · суммы за ВЕСЬ период по всей компании',
        '',
    ]

    # Группируем по месяцу (сначала новые), внутри — по источнику
    # SEO_AGGREGATE_2026-06-29: SEO-строки объединяются в одну «SEO (без пикселя)»
    from itertools import groupby
    # FUNNEL_DRIFT_MSG_CLARITY_2026-07-12: показываем только последние _MAX_MONTHS_SHOWN месяцев,
    # чтобы сообщение не разрасталось с каждым прогоном. Фильтр — ТОЛЬКО на вывод: дельты и
    # сравнение с прошлым прогоном (v_funnel_change) считаются в SQL по всем месяцам, полная
    # история — в data_funnel_drift_log (дашборд), «Итого» ниже — по ВСЕМ месяцам.
    _distinct_months = sorted({r['month'] for r in rows})
    _shown_months = set(_distinct_months[-_MAX_MONTHS_SHOWN:])
    _hidden = len(_distinct_months) - len(_shown_months)
    if _hidden > 0:
        lines.append(f'<i>(показаны последние {len(_shown_months)} мес.; полная история — в дашборде)</i>')
        lines.append('')

    rows_sorted = sorted(rows, key=lambda r: (r['month'], r['источник']))
    for month_str, month_rows_iter in groupby(rows_sorted, key=lambda r: r['month']):
        if month_str not in _shown_months:
            continue
        month_rows = _aggregate_seo(list(month_rows_iter))
        lines.append(f'<b>{month_str}</b>')
        for r in month_rows:
            src   = r['источник']
            dc    = r['delta_cost']
            dz    = r['delta_zayavki']
            dv    = r['delta_vizity']
            dp    = r['delta_prodazhi']
            c_cur = r['cost_curr']
            z_cur = r['zayavki_curr']
            v_cur = r['vizity_curr']
            p_cur = r['prodazhi_curr']
            c_prv = r['cost_prev']
            z_prv = r['zayavki_prev']
            v_prv = r['vizity_prev']
            p_prv = r['prodazhi_prev']

            # Стоимости: вычисляем в Python (защита от 0)
            cpl_z_c  = (c_cur / z_cur)  if z_cur  else None
            cpl_z_p  = (c_prv / z_prv)  if z_prv  else None
            cpv_c    = (c_cur / v_cur)   if v_cur  else None
            cpv_p    = (c_prv / v_prv)   if v_prv  else None
            cps_c    = (c_cur / p_cur)   if p_cur  else None
            cps_p    = (c_prv / p_prv)   if p_prv  else None

            parts = []

            # расход (всегда — полная картина)
            sign = '+' if dc > 0 else ''
            dc_str = f'({sign}{format_ru_amount(dc)} ₽)' if abs(dc) > 0.005 else '(=)'
            parts.append(f'расход {format_ru_amount(c_prv)}→{format_ru_amount(c_cur)} {dc_str}')

            # заявки
            sign = '+' if dz > 0 else ''
            dz_str = f'({sign}{dz})' if dz != 0 else '(=)'
            parts.append(f'заявки {z_prv}→{z_cur} {dz_str}')

            # визиты
            sign = '+' if dv > 0 else ''
            dv_str = f'({sign}{dv})' if dv != 0 else '(=)'
            parts.append(f'визиты {v_prv}→{v_cur} {dv_str}')

            # продажи
            sign = '+' if dp > 0 else ''
            dp_str = f'({sign}{dp})' if dp != 0 else '(=)'
            parts.append(f'продажи {p_prv}→{p_cur} {dp_str}')

            # CPL заявки (cost/zayavki)
            arrow_z = _cost_arrow(cpl_z_c, cpl_z_p)
            parts.append(f'CPL-заявки {_fmt_cost(cpl_z_p)}→{_fmt_cost(cpl_z_c)} {arrow_z}')

            # стоимость визита (cost/vizity)
            arrow_v = _cost_arrow(cpv_c, cpv_p)
            parts.append(f'ст.визита {_fmt_cost(cpv_p)}→{_fmt_cost(cpv_c)} {arrow_v}')

            # стоимость продажи (cost/prodazhi)
            arrow_s = _cost_arrow(cps_c, cps_p)
            parts.append(f'ст.продажи {_fmt_cost(cps_p)}→{_fmt_cost(cps_c)} {arrow_s}')

            # Собираем строку: каждая метрика с новой строки с отступом
            src_line = f'  <b>{src}</b>'
            metric_lines = [f'    {p}' for p in parts]
            lines.append(src_line)
            lines.extend(metric_lines)

        lines.append('')  # пустая строка между месяцами

    # FUNNEL_DRIFT_MSG_CLARITY_2026-07-12: итог за ВЕСЬ период + якорь на golden-диапазон.
    # Округляем ТОЛЬКО итоговую сумму (round(sum(...))); построчные значения здесь не int-кастуем.
    # Диапазон нормы — из verify_big_analytics.py::GRAND_SALES_LO/HI (единый источник, см. helper).
    _total_sales = round(sum(r['prodazhi_curr'] for r in rows))
    _lo, _hi = _golden_sales_range()
    _norm = 'норма' if _lo <= _total_sales <= _hi else '⚠ вне нормы'
    lines.append(
        f'<b>Итого продажи (вся компания, заявка-ось, без пикселя): '
        f'~{_total_sales} — {_norm} {_lo}-{_hi}</b>'
    )
    lines.append(
        '<i>продажи = сумма дробной пиксельной атрибуции по всем салонам/источникам, '
        'не количество сделок одного салона</i>'
    )

    text = '\n'.join(lines)
    # Pre-built HTML report (not a gate) — one sender+chunker: send_html handles
    # >4096-char splitting with tag-balanced <code>/<b> reopening across chunks,
    # so nothing here needs to truncate or hand-split the table.
    # collapse_whitespace=False (WHITESPACE_IS_CONTENT_2026-08-14): the 2/4-space
    # month→source→metric hierarchy above IS the layout — default sanitizing
    # flattened it to one space per line, director-caught round 3.
    # timeout=15 (was silently 10s default post-migration, matches the original
    # requests.post(..., timeout=15) this replaced).
    if send_html(text, bot_token=TELEGRAM_BOT_TOKEN, chat_id=TELEGRAM_CHAT_ID,
                proxy_variants=TELEGRAM_PROXY_VARIANTS, collapse_whitespace=False, timeout=15):
        logger.info('funnel_drift: алерт отправлен в Telegram')
    else:
        logger.error('funnel_drift: не удалось отправить алерт в Telegram')


# ─── Получение дельты из БД ───────────────────────────────────────────────────

def _fetch_diff(conn, run_id: str) -> list[dict]:
    """Читаем из v_funnel_change ВСЕ строки (включая Δ=0) — полная картина воронки."""
    rows = []
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    month::text,
                    источник,
                    delta_cost,
                    delta_zayavki,
                    delta_vizity,
                    delta_prodazhi,
                    cost_curr,
                    cost_prev,
                    zayavki_curr,
                    zayavki_prev,
                    vizity_curr,
                    vizity_prev,
                    prodazhi_curr,
                    prodazhi_prev,
                    changed
                FROM public.v_funnel_change
                WHERE curr_run_id = %s
                ORDER BY month DESC, источник
            """, (run_id,))
            for row in cur.fetchall():
                rows.append({
                    'month':          row[0],
                    'источник':       row[1],
                    'delta_cost':     float(row[2] or 0),
                    'delta_zayavki':  int(row[3] or 0),
                    'delta_vizity':   int(row[4] or 0),
                    'delta_prodazhi': int(row[5] or 0),
                    'cost_curr':      float(row[6] or 0),
                    'cost_prev':      float(row[7] or 0),
                    'zayavki_curr':   int(row[8] or 0),
                    'zayavki_prev':   int(row[9] or 0),
                    'vizity_curr':    int(row[10] or 0),
                    'vizity_prev':    int(row[11] or 0),
                    'prodazhi_curr':  int(row[12] or 0),
                    'prodazhi_prev':  int(row[13] or 0),
                    'changed':        row[14],
                })
    except Exception as e:
        logger.warning('funnel_drift: ошибка чтения v_funnel_change: %s', e)
    return rows


# ─── Основная точка входа ─────────────────────────────────────────────────────

def run(
    conn,
    run_id: Optional[str] = None,
    *,
    alert: bool = True,
) -> dict:
    """
    1. Создать таблицу/view если нет (DDL idempotent).
    2. Вставить снапшот (month × источник) для данного run_id.
    3. Если alert=True и есть предыдущий run_id — отправить Telegram-алерт по дрейфу.
    4. Вернуть {'rows': int, 'months': int, 'sources': int, 'run_id': str, 'alert_rows': int}.
    """
    rid = (run_id or '').strip() or datetime.now().isoformat()
    logger.info('funnel_drift_snapshot.run(run_id=%s, alert=%s)', rid, alert)

    with conn.cursor() as cur:
        # DDL — идемпотентно; MULTISTATEMENT_FIX_2026-07-02: по одному оператору на execute()
        for _stmt in DDL_TABLE:
            cur.execute(_stmt)
        for _stmt in DDL_VIEW:
            cur.execute(_stmt)

        # INSERT снапшота
        cur.execute(INSERT_SQL, (rid,))

        # Сколько строк записали
        cur.execute(
            f"SELECT COUNT(*), COUNT(DISTINCT month), COUNT(DISTINCT источник) "
            f"FROM {T_DRIFT} WHERE run_id = %s",
            (rid,),
        )
        rows, months, sources = cur.fetchone()

    conn.commit()
    rows    = int(rows    or 0)
    months  = int(months  or 0)
    sources = int(sources or 0)
    logger.info('funnel_drift: %d rows / %d months / %d sources for run_id=%s',
                rows, months, sources, rid)

    # Алерт — отправляем ВСЕ строки (полная картина по всем месяцам × источникам)
    alert_rows = 0
    if alert and rows > 0:
        diff = _fetch_diff(conn, rid)
        if diff:
            logger.info('funnel_drift: %d строк месяц×источник — отправляю алерт', len(diff))
            _send_drift_alert(diff)
            alert_rows = len(diff)
        else:
            logger.info('funnel_drift: нет данных для сравнения (первый run — нет prev)')

    return {
        'rows':       rows,
        'months':     months,
        'sources':    sources,
        'run_id':     rid,
        'alert_rows': alert_rows,
    }


# ─── Standalone-режим (тест/деплой) ─────────────────────────────────────────

if __name__ == '__main__':
    """
    Standalone: подцепиться к Victory и запустить снапшот.

    Использование:
        python3 step8_stats/funnel_drift_snapshot.py [run_id] [--no-alert]

    Если run_id не передан — генерируется новый UUID.
    Примеры:
        # Два прогона для теста diff:
        python3 step8_stats/funnel_drift_snapshot.py test_run_A
        python3 step8_stats/funnel_drift_snapshot.py test_run_B
        # test_run_B должен показать Telegram-алерт если данные изменились
    """
    import sys
    sys.path.insert(0, '/home/semen_vi/big_analytics_v5')

    import logging as _log
    _log.basicConfig(level=_log.INFO, format='%(asctime)s %(levelname)s %(message)s')

    import config.db as db_module
    db_module.init_pool()

    _no_alert = '--no-alert' in sys.argv
    _args = [a for a in sys.argv[1:] if not a.startswith('--')]
    _rid = _args[0] if _args else None

    c = db_module.get_conn()
    try:
        res = run(c, run_id=_rid, alert=not _no_alert)
        print(res)
    finally:
        db_module.put_conn(c)
        db_module.close_pool()
