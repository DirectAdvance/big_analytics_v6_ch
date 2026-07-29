# step7_finalize — Финализация big_analytics_full

Шаг 7 пайплайна. Финализирует таблицы: переводит из UNLOGGED → LOGGED, создаёт финальные индексы и запускает `VACUUM ANALYZE` на главной таблице `big_analytics_full`.

## Назначение

После шагов 3-6 все таблицы UNLOGGED (для скорости INSERT). step7 переводит их в режим LOGGED (с записью в WAL) и создаёт финальные индексы для быстрого чтения из Power BI.

| Действие | На каких таблицах |
|----------|-------------------|
| `SET LOGGED` | `big_analytics_direct`, `big_analytics_seo`, `big_analytics_pixel`, `big_analytics_crop_targeting`, `big_analytics_reviews`, `big_analytics_full` |
| `ag_part1_name` ALTER+UPDATE | `big_analytics_full` |
| 7 индексов CREATE | `big_analytics_full` |
| `VACUUM ANALYZE` | `big_analytics_full` |

## Архитектурная схема

```
big_analytics_* UNLOGGED ──SET LOGGED──► big_analytics_* LOGGED
                                              │
                                              ▼
big_analytics_full ──ag_part1_name (SPLIT_PART)──► big_analytics_full + ag_part1_name
                          │
                          ├──► CREATE INDEX (7 потоков параллельно)
                          │       idx_full_date, idx_full_domain, idx_full_source_table,
                          │       idx_full_salon, idx_full_region, idx_full_account,
                          │       idx_full_campaign_id
                          │
                          └──► VACUUM ANALYZE
```

## Финальные индексы

| Индекс | Колонка | Зачем |
|--------|---------|-------|
| `idx_full_date` | `"Date"` | Фильтры по диапазону дат в PBI |
| `idx_full_domain` | `domain` | Drill-through, страницы по доменам |
| `idx_full_source_table` | `_source_table` | Фильтр источника (direct/seo/crop/...) |
| `idx_full_salon` | `"салон"` | Слайсер салона |
| `idx_full_region` | `"регион"` | Слайсер региона |
| `idx_full_account` | `account_login` | Аналитика по аккаунтам |
| `idx_full_campaign_id` | `"CampaignId"` | JOIN с `campaign_status`, drill в кампании |

Все индексы строятся **параллельно**: каждый в отдельном потоке + отдельном соединении из пула, но не более 3 одновременно (семафор `_IDX_MAX_PARALLEL=3` — защита пула от параллельного step9 prefetch). На 2.5M строк это занимает ~30-90 сек.

## VACUUM ANALYZE

```python
conn.autocommit = True  # ANALYZE/VACUUM нельзя в транзакции
cur.execute('SET max_parallel_maintenance_workers = 0')  # обход shared memory bug
cur.execute(f'ANALYZE {T_FULL}')            # ВСЕГДА
# VACUUM — только если bloat > VACUUM_BLOAT_PCT (20%)
if dead_pct > VACUUM_BLOAT_PCT:
    cur.execute(f'VACUUM ANALYZE {T_FULL}')
```

**ANALYZE** выполняется всегда (свежесть статистики критична после построения индексов). **VACUUM** — только если dead-tuple bloat > `VACUUM_BLOAT_PCT` (20%); `skip_vacuum=True` отключает VACUUM полностью (ANALYZE всё равно выполняется). Только `big_analytics_full` — source-таблицы только что созданы через CTAS, dead tuples = 0.

## Параметры

`run(conn, run_id: str, skip_vacuum: bool = False, set_logged_tables=None)`:
- `skip_vacuum=True` — отключает VACUUM (ANALYZE всё равно выполняется)
- `set_logged_tables=None` — список таблиц для SET LOGGED (None = полный; fast_pipeline передаёт список без T_DIRECT — P1_STEP7_SKIP_DIRECT_SETLOGGED_2026-06-18)

## Зависимости

- step6 (`big_analytics_full` создан и заполнен)
- Пул соединений из `config/db.py` (для параллельных индексов)

## Примеры запуска

```bash
# Только step7:
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 pipeline.py --only-step=7"

# Программно с skip_vacuum:
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 -c '
from step7_finalize import step7
from config.db import get_conn
c = get_conn()
step7.run(c, \"manual\", skip_vacuum=True)
'"
```

## Проверки после запуска

```sql
-- Все таблицы LOGGED (нет 'u' в relpersistence)
SELECT relname, relpersistence FROM pg_class
WHERE relname LIKE 'big_analytics_%' AND relkind='r';

-- 7 индексов на big_analytics_full
SELECT indexname FROM pg_indexes WHERE tablename='big_analytics_full' ORDER BY 1;

-- ag_part1_name заполнен
SELECT ag_part1, ag_part1_name FROM big_analytics_full
WHERE ag_part1 LIKE '% - %' LIMIT 5;

-- Размер таблицы и индексов
SELECT pg_size_pretty(pg_total_relation_size('big_analytics_full')) AS total,
       pg_size_pretty(pg_relation_size('big_analytics_full')) AS data,
       pg_size_pretty(pg_indexes_size('big_analytics_full')) AS indexes;
```

## История фиксов

| Дата | Фикс |
|------|------|
| Май 2026 | Параллельные индексы (7 потоков, по соединению на индекс) |
| Май 2026 | `SET max_parallel_maintenance_workers = 0` — обход shared memory bug |

## Связи

- **Зависит от:** step6 (`big_analytics_full`)
- **После step7:** `load_reviews_to_big_analytics`, `load_crop_to_big_analytics`, step11, step12, step8
- **Промежуточные UPDATE'ы после step7**: `404_errors`, `normalize_salons`, `fill_missing_regions`, `cleanup_old_dates`, `campaign_status_prefix`

## Файлы

| Файл | Описание |
|------|----------|
| `step7.py` | Основной скрипт |
| `__init__.py` | Пустой |
| `CLAUDE.md` | Краткая инструкция для ИИ |
| `README.md` | Этот файл |
