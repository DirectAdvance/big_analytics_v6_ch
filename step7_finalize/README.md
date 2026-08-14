# step7_finalize — финализация ClickHouse-таблиц

Шаг 7 v6_ch не переводит таблицы `UNLOGGED → LOGGED`, не создаёт PostgreSQL-индексы
и не запускает `VACUUM ANALYZE`. Эти операции относятся к v5/legacy. В активном
ClickHouse-контуре шаг 7 оптимизирует собранные рабочие таблицы `ad_analytics`.

## Что делает

Для каждой существующей таблицы из списка ниже считает строки и выполняет
`OPTIMIZE TABLE ... FINAL`, если объект не является `VIEW`:

| Таблица | Роль |
|---|---|
| `ad_analytics.big_analytics_sources` | общий слой источников после step3/corrections |
| `ad_analytics.big_analytics_calls` | звонки для step6/full |
| `ad_analytics.big_analytics_full` | основная wide-витрина до unified/star |

Итог шага пишет в `data_quality_log`: суммарное число строк и детали вида
`big_analytics_full=...`.

## Запуск

```bash
.venv/bin/python3 pipeline.py --only-step=7
```

## Проверка

```bash
.venv/bin/python3 - <<'PY'
from config.ch_db import get_client
client = get_client()
for table in ["big_analytics_sources", "big_analytics_calls", "big_analytics_full"]:
    rows = client.query(
        "SELECT engine, total_rows FROM system.tables "
        "WHERE database='ad_analytics' AND name={table:String}",
        parameters={"table": table},
    ).result_rows
    print(table, rows[0] if rows else "missing")
PY
```

## Связи

- **Зависит от:** step6 (`big_analytics_full` собран).
- **После step7:** step9, step10, step11, spec fallback, step12, step13, unified/star/PBI-compat хвост.
