# step2_indexes — Индексы и ANALYZE на RAW-таблицах

Третий шаг пайплайна. Создаёт индексы на UNLOGGED RAW-таблицах после их заполнения шагом 1, выполняет `ANALYZE` для обновления статистики планировщика.

## Назначение

После step1 RAW-таблицы заполнены, но **без индексов и статистики**. step3 будет делать массовые JOIN — без индексов это последовательный скан 2.5M+ строк.

step2 создаёт 10 индексов + 4 `ANALYZE` за ~10-30 секунд, что позволяет step3 работать через index scan.

## Архитектурная схема

```
raw_yandex   → idx_date, idx_campaign_id, idx_account
raw_leads    → idx_domain_id, idx_created, idx_utm_source, idx_utm_campaign
raw_calls    → idx_domain_id, idx_created
raw_domains  → idx_id

ANALYZE raw_yandex
ANALYZE raw_leads
ANALYZE raw_calls
ANALYZE raw_domains
```

Все операции — `CREATE INDEX IF NOT EXISTS`, идемпотентны.

## Retry логика

Каждая операция (`CREATE INDEX` или `ANALYZE`) запускается через `_exec_with_retry`:

- `SET LOCAL lock_timeout = '30s'` — не висим вечно
- 3 попытки с экспоненциальным backoff (2 сек)
- При `DeadlockDetected` / `LockNotAvailable` — повтор
- После 3 неудач — `ERROR` лог, шаг не падает (continue)

## Параметры

`config/settings.py` — имена таблиц через константы `T_RAW_YANDEX`, `T_RAW_LEADS`, `T_RAW_CALLS`, `T_RAW_DOMAINS`.

Список индексов хардкод в `step2.py` в массиве `INDEXES`.

## Зависимости

- step1 должен быть выполнен (RAW-таблицы существуют)
- psycopg2

## Примеры запуска

```bash
# Только step2:
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 pipeline.py --only-step=2"

# Проверка индексов:
psql -c "SELECT tablename, indexname FROM pg_indexes
         WHERE tablename LIKE 'raw_%' ORDER BY tablename, indexname;"
```

## Проверки после запуска

```sql
-- Все 10 индексов на месте
SELECT tablename, COUNT(*) AS idx_count FROM pg_indexes
WHERE tablename IN ('raw_yandex','raw_leads','raw_calls','raw_domains')
GROUP BY tablename;

-- ANALYZE обновил статистику
SELECT schemaname, relname, last_analyze FROM pg_stat_user_tables
WHERE relname IN ('raw_yandex','raw_leads','raw_calls','raw_domains');
```

## Связи

- **Зависит от:** step1
- **Подготавливает:** step3 (массивные CTE с JOIN по key3, domain_id, CampaignId)

## Файлы

| Файл | Описание |
|------|----------|
| `step2.py` | Основной скрипт (101 строка) |
| `CLAUDE.md` | Краткая инструкция для ИИ |
| `README.md` | Этот файл |
