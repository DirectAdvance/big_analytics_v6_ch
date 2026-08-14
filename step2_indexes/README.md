# step2_indexes — OPTIMIZE RAW в ClickHouse

Шаг 2 v6_ch не создаёт PostgreSQL-индексы и не запускает `ANALYZE`. RAW-таблицы
создаются в step1 с ClickHouse `MergeTree ORDER BY` ключами; этот шаг делает
best-effort `OPTIMIZE TABLE ... FINAL`, чтобы перед тяжёлыми чтениями уменьшить
число частей.

## Что делает

Проходит по `config/ch_settings.py::RAW_TARGET_TABLES`:

| Объект | Действие |
|---|---|
| `ad_analytics.raw_yandex` | `OPTIMIZE TABLE ... FINAL` |
| `ad_analytics.raw_leads` | `OPTIMIZE TABLE ... FINAL` |
| `ad_analytics.raw_calls` | `OPTIMIZE TABLE ... FINAL` |
| `ad_analytics.raw_domains` | пропуск, если это `VIEW` |
| `ad_analytics.raw_perform_leads` | `OPTIMIZE TABLE ... FINAL`, если таблица существует |

Отсутствующий объект логируется как `skipped`; `VIEW` логируется как `view_skipped`.

## Запуск

```bash
.venv/bin/python3 pipeline.py --only-step=2
```

## Проверка

```bash
.venv/bin/python3 - <<'PY'
from config.ch_db import get_client
client = get_client()
rows = client.query("""
    SELECT name, engine, total_rows
    FROM system.tables
    WHERE database = 'ad_analytics' AND name LIKE 'raw_%'
    ORDER BY name
""").result_rows
for row in rows:
    print(row)
PY
```

## Связи

- **Зависит от:** step1 (`ad_analytics.raw_*` созданы).
- **Подготавливает:** step3 и downstream-чтения RAW-слоя.
