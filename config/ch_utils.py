"""Small ClickHouse helpers for the v6_ch migration."""

from __future__ import annotations

import os
from datetime import date, timedelta


# MEMORY_LIMIT_2026-08-14: 512 МБ → 1 ГБ (Семён) → 2 ГБ (по факту падения step6).
# Инстанс — 2 vCPU / 8.33 ГБ (`CGroupMaxCPU=1.99`), серверный потолок
# `max_server_memory_usage=7.49 ГБ`, резидентно ClickHouse держит ~2.2 ГБ. Запросы пайплайна идут
# последовательно, поэтому 2 ГБ на запрос — вчетверо ниже серверного потолка.
# Почему подняли: на недельных окнах step6 упал с `MEMORY_LIMIT_EXCEEDED` (241) на 979.51 МиБ при
# лимите 976.56 МиБ — не хватило буквально трёх мегабайт.
SAFE_QUERY_SETTINGS = {
    # THREADS_2026-08-14: было 1 — на двухъядерном инстансе это ровно половина мощности.
    # Замер: step3 direct за день 4.0 с → 1.0 с, step1 за неделю 6.7 с → 3.2 с.
    "max_threads": 2,
    # Окна расширены с дня до недели (PIPELINE_BATCH_DAYS), запрос живёт дольше — 60 с стало мало.
    "max_execution_time": 300,
    "max_memory_usage": 2_048_000_000,
    # Порог спилла на диск держим около половины лимита памяти, иначе поднятый лимит не работает.
    "max_bytes_before_external_group_by": 512_000_000,
    "max_bytes_before_external_sort": 512_000_000,
    "max_temporary_data_on_disk_size_for_query": 2_000_000_000,
    "max_block_size": 16_384,
}


def q(name: str) -> str:
    """Quote a ClickHouse identifier."""
    return "`" + name.replace("`", "``") + "`"


def table_exists(client, database: str, table: str) -> bool:
    return bool(
        client.query(
            """
            SELECT count()
            FROM system.tables
            WHERE database={database:String} AND name={table:String}
            """,
            parameters={"database": database, "table": table},
            settings=SAFE_QUERY_SETTINGS,
        ).result_rows[0][0]
    )


def table_engine(client, database: str, table: str) -> str | None:
    rows = client.query(
        """
        SELECT engine
        FROM system.tables
        WHERE database={database:String} AND name={table:String}
        """,
        parameters={"database": database, "table": table},
        settings=SAFE_QUERY_SETTINGS,
    ).result_rows
    return rows[0][0] if rows else None


# MOJIBAKE_2026-08-17. `raw_data` отдаёт часть текстовых значений в двойной кодировке: UTF-8
# прочитан как ISO-8859-1 («FMCG Инсайдер» → «FMCG Ð˜Ð½ÑÐ°Ð¹Ð´ÐµÑ€»). Замер на
# `raw_data.yandex_direct_report_rows.placement`: 7 522 строки / 847 площадок / 16 логинов за
# январь-июль 2026. Чиним у себя на чтении, чинить у источника — отдельная просьба владельцу
# `raw_data` (`RAW_DATA_REQUEST.md`).
def fix_mojibake_sql(col: str) -> str:
    """SQL-выражение, разворачивающее двойное кодирование UTF-8 → ISO-8859-1.

    Round-trip-проверка гарантирует, что чистая строка не пострадает: переписываем только то,
    что кодируется обратно в исходное значение. Строку, обрезанную источником посреди символа,
    не трогаем — после раскодирования она перестаёт быть валидным UTF-8.
    """
    decoded = f"convertCharset({col}, 'UTF-8', 'ISO-8859-1')"
    return (
        f"if(isValidUTF8({decoded}) AND convertCharset({decoded}, 'ISO-8859-1', 'UTF-8') = {col},"
        f" {decoded}, {col})"
    )


def column_names(client, database: str, table: str) -> list[str]:
    rows = client.query(
        """
        SELECT name
        FROM system.columns
        WHERE database={database:String} AND table={table:String}
        ORDER BY position
        """,
        parameters={"database": database, "table": table},
        settings=SAFE_QUERY_SETTINGS,
    ).result_rows
    return [row[0] for row in rows]


# STORAGE_CODECS_2026-08-14 (OPTIMIZATION_PLAN.md, фаза 2.2). Кодеки выбраны замером на партиции
# 202607 живых таблиц, а не по умолчанию. Лучший кодек зависит от колонки, поэтому здесь общее
# правило для таблиц с динамической схемой, а не универсальная истина:
#   * `Decimal(18,6)`-метрики фактов — чистый ZSTD(3) (таблица целиком −34.5% против −28.9% у
#     T64+ZSTD, замер на `fact_region_spend`);
#   * `Decimal(18,9) total_cost` в `raw_yandex` — наоборот, T64+ZSTD(3) (20.02 → 15.06 МиБ против
#     16.09 у чистого ZSTD), поэтому там кодек прописан явно в DDL, а не берётся отсюда;
#   * целочисленные id — T64+ZSTD(3), длинные строки — ZSTD(3), Float64 — ZSTD(3).
# Хэш-ключи (`*_key`, `site_key`) НЕ трогаем: T64 на случайном хэше только мешает.
_CODEC_BY_TYPE = {"decimal": "ZSTD(3)", "int_id": "T64, ZSTD(3)", "string": "ZSTD(3)"}


def _codec_for(name: str, col_type: str) -> str | None:
    if col_type.startswith("LowCardinality"):
        return None
    if "Decimal" in col_type or "Float" in col_type:
        return _CODEC_BY_TYPE["decimal"]
    # Только настоящие идентификаторы: `id`, `campaign_id`, `CampaignId`. Голое `endswith("id")`
    # цепляло бы любое слово на «id» (`valid`, `uid`) и вешало T64 на не-идентификатор.
    if "Int" in col_type and (name.lower() == "id" or name.lower().endswith("_id") or name.endswith("Id")):
        return _CODEC_BY_TYPE["int_id"]
    if "String" in col_type:
        return _CODEC_BY_TYPE["string"]
    return None


def apply_storage_codecs(client, target: str) -> None:
    """Навесить кодеки сжатия на ПУСТУЮ таблицу (мгновенно — данных ещё нет).

    Для таблиц, у которых схема выводится из SELECT и заранее неизвестна. Там, где схема
    задана явно в коде сборки, кодеки пишутся прямо в DDL — этот хелпер не нужен.
    """
    database, table = target.split(".", 1)
    rows = client.query(
        """
        SELECT name, type
        FROM system.columns
        WHERE database={database:String} AND table={table:String}
        ORDER BY position
        """,
        parameters={"database": database.strip('"`'), "table": table.strip('"`')},
        settings=SAFE_QUERY_SETTINGS,
    ).result_rows
    for name, col_type in rows:
        codec = _codec_for(name, col_type)
        if codec:
            client.command(f"ALTER TABLE {target} MODIFY COLUMN {q(name)} {col_type} CODEC({codec})")


def column_list(client, database: str, table: str, alias: str | None = None) -> str:
    prefix = f"{alias}." if alias else ""
    return ", ".join(f"{prefix}{q(name)}" for name in column_names(client, database, table))


def create_empty_like(client, target: str, source: str, engine_sql: str | None = None) -> None:
    """Create an empty table with the same schema as source.

    ClickHouse `CREATE TABLE t AS source` does not copy engine settings. When an
    engine is important for a large table, pass a full `ENGINE ... ORDER BY ...`
    clause in `engine_sql`.
    """
    client.command(f"DROP TABLE IF EXISTS {target} SYNC")
    if engine_sql:
        client.command(f"CREATE TABLE {target} AS {source} {engine_sql}", settings=SAFE_QUERY_SETTINGS)
    else:
        client.command(f"CREATE TABLE {target} AS {source}", settings=SAFE_QUERY_SETTINGS)


def swap_shadow(client, target: str, shadow: str) -> None:
    database, table = target.split(".", 1)
    engine = table_engine(client, database, table)
    if engine and engine != "View":
        client.command(f"EXCHANGE TABLES {target} AND {shadow}", settings=SAFE_QUERY_SETTINGS)
        client.command(f"DROP TABLE IF EXISTS {shadow} SYNC", settings=SAFE_QUERY_SETTINGS)
    elif engine:
        client.command(f"DROP TABLE IF EXISTS {target} SYNC", settings=SAFE_QUERY_SETTINGS)
        client.command(f"RENAME TABLE {shadow} TO {target}", settings=SAFE_QUERY_SETTINGS)
    else:
        client.command(f"RENAME TABLE {shadow} TO {target}", settings=SAFE_QUERY_SETTINGS)


def month_ranges_from_table(
    client,
    source_table: str,
    date_expr: str,
    where_sql: str = "1 = 1",
    date_from: str = "2026-01-01",
) -> list[tuple[str, str]]:
    row = client.query(
        f"""
        SELECT min(toDate({date_expr})), max(toDate({date_expr}))
        FROM {source_table}
        WHERE {where_sql}
          AND {date_expr} IS NOT NULL
          AND toDate({date_expr}) >= toDate('{date_from}')
        """,
        settings=SAFE_QUERY_SETTINGS,
    ).result_rows[0]
    if row[0] is None or row[1] is None:
        return []

    current = max(row[0].replace(day=1), date.fromisoformat(date_from))
    last = row[1].replace(day=1)
    ranges: list[tuple[str, str]] = []
    while current <= last:
        nxt = date(current.year + 1, 1, 1) if current.month == 12 else date(current.year, current.month + 1, 1)
        ranges.append((current.isoformat(), nxt.isoformat()))
        current = nxt
    return ranges


# BATCH_WIDTH_2026-08-14: ширина окна пакетной вставки для ВСЕХ шагов (18 мест звали `day_ranges`).
# Замер на живой БД, партиция 202607: step1 день 3.5 с → неделя 3.2 с (в 7 раз меньше строк за
# те же секунды — время съедали накладные расходы на запрос, а не данные); step3 direct
# день 4.0 с → неделя 6.1 с при семикратном объёме. Месяц в step3 НЕ проходит:
# `MEMORY_LIMIT_EXCEEDED` (код 241) на лимите 1 ГБ — поэтому неделя, а не месяц.
# Аварийный откат без правки кода: `PIPELINE_BATCH_DAYS=1 python3 pipeline.py`.
PIPELINE_BATCH_DAYS = max(1, int(os.getenv("PIPELINE_BATCH_DAYS", "7")))


def day_ranges(date_from: str = "2026-01-01", date_to: str | None = None) -> list[tuple[str, str]]:
    """Окна для пакетной вставки шириной до `PIPELINE_BATCH_DAYS`, в пределах одного месяца.

    Ширина влияет на память и время. На результат она не влияет ТОЛЬКО потому, что окно не
    пересекает границу месяца (см. `range_batches`): часть шагов считает внутри батча месячные
    агрегаты от `toStartOfMonth(lo)`, и без этой гарантии широкое окно молча портит данные.
    """
    return range_batches(date_from, date_to, days=PIPELINE_BATCH_DAYS)


def _next_month_start(day: date) -> date:
    return (day.replace(day=1) + timedelta(days=32)).replace(day=1)


def range_batches(
    date_from: str = "2026-01-01",
    date_to: str | None = None,
    days: int = 1,
) -> list[tuple[str, str]]:
    """Окна вставки, НИКОГДА не пересекающие границу календарного месяца.

    MONTH_SNAP_2026-08-14: окно шире дня вскрыло скрытую связь шагов с месяцем. `step11`
    (`step11_pixel_score/step11.py:63`) считает веса пикселя за месяц, взятый из ПЕРВОГО дня окна
    (`toStartOfMonth(lo)`), а пиксельные дни тянет за всё окно. Пока окно = день, `lo` и последний
    день окна всегда в одном месяце и это было незаметно. На недельных окнах 24 дня по ту сторону
    границы месяца не находили весов в INNER JOIN и падали в ветку `CampaignId=0` с неразделёнными
    значениями — 11% пиксельной оси, при сохранных тоталах (потому golden и не поймал).

    Прибивка к границе месяца лечит причину для ВСЕХ шагов сразу, а не только для step11:
    любой месячный агрегат внутри батча теперь по определению считается за свой месяц.
    """
    if days < 1:
        raise ValueError("days must be >= 1")
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to) if date_to else date.today() + timedelta(days=1)
    ranges: list[tuple[str, str]] = []
    current = start
    while current < end:
        nxt = min(current + timedelta(days=days), _next_month_start(current), end)
        ranges.append((current.isoformat(), nxt.isoformat()))
        current = nxt
    return ranges


def count_rows(client, table: str) -> int:
    return int(client.query(f"SELECT count() FROM {table}", settings=SAFE_QUERY_SETTINGS).result_rows[0][0])


def replace_view(client, name: str, select_sql: str) -> None:
    """Replace a ClickHouse table-like object with a normal VIEW."""
    client.command(f"DROP TABLE IF EXISTS {name} SYNC")
    client.command(f"CREATE VIEW {name} AS {select_sql}", settings=SAFE_QUERY_SETTINGS)
