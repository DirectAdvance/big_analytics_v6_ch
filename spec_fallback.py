#!/usr/bin/env python3
"""SPEC_FALLBACK_V3_2026-08-06 — пост-проход по пустому `специалист`.

Зачем отдельный шаг, а не часть `corrections`
---------------------------------------------
`corrections.apply()` пересобирает `big_analytics_sources` МЕЖДУ step3 и step5,
поэтому его каскад специалиста (`_stage6_labels`) физически не видит строки,
которые появляются в `big_analytics_full` позже:

  * `calls`                     — step6 доливает звонки;
  * `пиксель` / `пиксель_атрибуц` — step11 пересоздаёт full с пиксельной атрибуцией;
  * посевной/`vk_ads` стоимостной оверлей — step10.

В v5 расклад ровно тот же, и там пробел закрывает отдельный проход
`corrections.apply_spec_fallback_v3()` (v5 `corrections.py:1859`, вызовы
v5 `pipeline.py:1857` и `:2030`). Здесь это шаг 115 пайплайна — сразу после
step10/step11 и до step12/step13, чтобы `big_analytics_full_arrival` и звезда
строились уже по заполненной колонке. Визитная ось обрабатывается тем же
механизмом отдельным шагом после step13.

Почему НЕ снят гейт `match_priority IN (1, 2)` в `step3._domain_specialist_expr`
--------------------------------------------------------------------------------
Там специалист берётся из gsheet ТОЛЬКО внутри окна `launch_date … block_date`
сайта. У `probeg-cars.ru` окно 07.10.2025–20.10.2025, у `autocenter93.ru` —
09.09.2025–25.09.2025; звонки от 2026-06-09 и 2026-07-22 в окна не попадают, и
`специалист` остаётся пустым, хотя `directologist` в `reference_data.gsheet_sites` у
обоих доменов заполнен. Снимать окно в step3 вслепую нельзя: выражение общее для
заявочной, визитной и пиксельной осей и работает ДО `corrections`, где правило 1
перекладывает расход по специалисту. Этот проход идёт ПОСЛЕ всех правил, матчит
по домену и БЕЗ окна дат — паритет с v5.

Каскад (v5-паритет)
-------------------
1. `directologist` из `reference_data.gsheet_sites` по домену (реальный специалист);
2. `direction_main` оттуда же (канал: SEO / Контекст / Посевы / …);
3. `'Звонки'` — для campaign_code='звонки', `_source_table='calls'` или источника
   `Посевы_Звонки` без связки;
4. `'Без специалиста'` — последний resort.

Идемпотентность и область
-------------------------
Трогает ТОЛЬКО строки с пустым/NULL `специалист` и ТОЛЬКО эту колонку: остальные
поля переносятся через `SELECT * REPLACE`. Заполненные значения (в т.ч. «Кудерко
Семен» из правил по аккаунту) не затираются, повторный запуск — no-op. Деньги и
воронка не меняются по построению, и это проверяет гейт `_invariants` ДО подмены
таблицы.

`big_analytics_full_arrival` запускается через `spec_fallback_arrival.py` после step13.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config.ch_db import get_client
from config.ch_settings import DATE_FROM, GSHEET_SITES_EFFECTIVE
from config.ch_utils import SAFE_QUERY_SETTINGS, column_names, count_rows, day_ranges, q, swap_shadow, table_exists
from corrections import _invariants, _specialist_cte, calls_specialist_correction_expr

logger = logging.getLogger("pipeline.spec_fallback")

TARGET = "ad_analytics.big_analytics_full"
SHADOW = "ad_analytics.big_analytics_full_spec_new"


def _empty_spec(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return f"ifNull(trim({prefix}`специалист`), '') = ''"


def _is_calls_expr(alias: str = "s") -> str:
    return (
        f"(ifNull({alias}.campaign_code, '') = 'звонки' "
        f"OR {alias}.`_source_table` = 'calls' "
        f"OR ifNull({alias}.`источник`, '') = 'Посевы_Звонки')"
    )


def _account_specialist_cte() -> str:
    return f"""
gs_account_specialist AS
(
    SELECT
        lowerUTF8(trim(ifNull(login_key, ''))) AS login_key,
        anyLast(directologist) AS directologist
    FROM {GSHEET_SITES_EFFECTIVE}
    WHERE ifNull(login_key, '') != ''
    GROUP BY login_key
)
"""


def _directologist_fallback_expr(alias: str = "s", gs: str = "gsp", ga: str = "gsa") -> str:
    dated_specialist = calls_specialist_correction_expr(
        f"{alias}.`Date`",
        f"{alias}.account_login",
        f"{gs}.directologist",
        f"{ga}.directologist",
    )
    return f"nullIf(trim(ifNull({dated_specialist}, '')), '')"


def _fallback_expr(alias: str = "s", gs: str = "gsp", ga: str = "gsa") -> str:
    """Каскад v5 `apply_spec_fallback_v3`. Пустое значение → gsheet → канал → дефолт."""
    return (
        f"if({_empty_spec(alias)}, "
        "coalesce("
        f"{_directologist_fallback_expr(alias, gs, ga)}, "
        f"nullIf(trim(ifNull({gs}.direction_main, '')), ''), "
        f"if({_is_calls_expr(alias)}, 'Звонки', NULL), "
        "'Без специалиста'), "
        f"{alias}.`специалист`)"
    )


def _tier_report(client, table: str) -> str:
    """Куда именно уедут пустые строки — по ступеням каскада (в лог, до правки)."""
    rows = client.query(
        f"""
        WITH {_specialist_cte().strip()},
        {_account_specialist_cte().strip()}
        SELECT
            multiIf(
                {_directologist_fallback_expr('s')} != '', '1_directologist',
                trim(ifNull(gsp.direction_main, '')) != '', '2_direction_main',
                {_is_calls_expr('s')}, '3_Звонки',
                '4_Без специалиста'
            ) AS tier,
            count() AS rows,
            round(sum(s.total_cost), 2) AS cost,
            round(sum(s.prodazhi), 4) AS prodazhi
        FROM {table} s
        LEFT JOIN gs_specialist gsp ON gsp.domain_key = lowerUTF8(trim(ifNull(s.domain, '')))
        LEFT JOIN gs_account_specialist gsa ON gsa.login_key = lowerUTF8(trim(ifNull(s.account_login, '')))
        WHERE {_empty_spec('s')}
        GROUP BY tier
        ORDER BY tier
        """
    ).result_rows
    return ", ".join(f"{tier}: rows={rows_:,} cost={cost} prodazhi={sales}" for tier, rows_, cost, sales in rows)


def _create_empty_like(client, target: str, shadow: str) -> None:
    client.command(f"DROP TABLE IF EXISTS {shadow} SYNC")
    client.command(
        f"""
        CREATE TABLE {shadow}
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(ifNull(`Date`, toDate('2026-01-01')))
        ORDER BY (ifNull(`Date`, toDate('2026-01-01')), ifNull(domain, ''), ifNull(_source_table, ''))
        AS SELECT * FROM {target} WHERE 0
        """,
        settings=SAFE_QUERY_SETTINGS,
    )


def _split_table(table: str) -> tuple[str, str]:
    database, name = table.split(".", 1)
    return database, name


def _rebuild(client, target: str, shadow: str) -> None:
    database, table = _split_table(target)
    cols_sql = ", ".join(q(col) for col in column_names(client, database, table))
    _create_empty_like(client, target, shadow)
    ranges = day_ranges(DATE_FROM)
    for idx, (lo, hi) in enumerate(ranges, start=1):
        client.command(
            f"""
            INSERT INTO {shadow} ({cols_sql})
            WITH {_specialist_cte().strip()},
            {_account_specialist_cte().strip()}
            SELECT s.* REPLACE ({_fallback_expr()} AS `специалист`)
            FROM {target} s
            LEFT JOIN gs_specialist gsp ON gsp.domain_key = lowerUTF8(trim(ifNull(s.domain, '')))
            LEFT JOIN gs_account_specialist gsa ON gsa.login_key = lowerUTF8(trim(ifNull(s.account_login, '')))
            WHERE s.`Date` >= toDate('{lo}') AND s.`Date` < toDate('{hi}')
            """,
            settings=SAFE_QUERY_SETTINGS,
        )
        if idx % 30 == 0 or idx == len(ranges):
            logger.info("  spec_fallback daily batch %d/%d: %s", idx, len(ranges), hi)


def run_for_table(target: str, shadow: str, step_label: str) -> dict:
    logger.info("%s v6_ch: SPEC_FALLBACK_V3 по %s", step_label, target)
    client = get_client()
    t0 = time.perf_counter()
    database, table = _split_table(target)

    if not table_exists(client, database, table):
        raise RuntimeError(f"{target} отсутствует — нечего доразмечать")

    before_empty = int(
        client.query(f"SELECT countIf({_empty_spec()}) FROM {target}").result_rows[0][0]
    )
    if not before_empty:
        logger.info("  пустых `специалист` нет — проход no-op")
        return {"rows": 0, "details": "empty_specialist=0 (no-op)"}
    logger.info("  пустых `специалист`: %d; распределение по каскаду → %s", before_empty, _tier_report(client, target))

    before_rows = count_rows(client, target)
    before_inv = _invariants(client, target)
    _rebuild(client, target, shadow)

    # Гейт ДО подмены: пересборка обязана сохранить и строки, и деньги, и воронку.
    after_rows = count_rows(client, shadow)
    after_inv = _invariants(client, shadow)
    after_empty = int(client.query(f"SELECT countIf({_empty_spec()}) FROM {shadow}").result_rows[0][0])
    if after_rows != before_rows:
        raise RuntimeError(f"spec_fallback: строки разъехались {before_rows} → {after_rows}")
    drift = {key: (before_inv[key], after_inv[key]) for key in before_inv if before_inv[key] != after_inv[key]}
    if drift:
        raise RuntimeError(f"spec_fallback: инварианты уехали {drift}")
    if after_empty:
        raise RuntimeError(f"spec_fallback: после прохода осталось {after_empty} пустых `специалист`")

    swap_shadow(client, target, shadow)
    details = f"filled={before_empty:,}, rows={after_rows:,}, cost={after_inv['total_cost']}"
    logger.info("%s v6_ch завершён за %.1f сек: %s", step_label, time.perf_counter() - t0, details)
    return {"rows": before_empty, "details": details}


def run(conn=None, run_id: str | None = None) -> dict:  # noqa: ARG001
    return run_for_table(TARGET, SHADOW, "Шаг 11.5")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(run())
