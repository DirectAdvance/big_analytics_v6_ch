# STEP.md — Шаг 5: Финализация

## Что делает

1. `ALTER TABLE ... SET LOGGED` — включает WAL для всех result-таблиц  
   (direct, seo, pixel, telegram, crop, full)
2. `CREATE INDEX` — индексы на `big_analytics_full`
3. `VACUUM ANALYZE` — сбрасывает pending WAL, обновляет статистику планировщика

## Индексы на big_analytics_full

| Индекс | Колонка | Зачем |
|--------|---------|-------|
| `idx_full_date` | `"Date"` | Фильтрация по дате в Power BI |
| `idx_full_domain` | `домен` | GROUP BY домен |
| `idx_full_source_table` | `_source_table` | Отладка источника строки |
| `idx_full_salon` | `"салон"` | Фильтр по автосалону |
| `idx_full_region` | `"регион"` | Фильтр по региону |
| `idx_full_account` | `account_login` | Фильтр по аккаунту |
| `idx_full_campaign_id` | `"CampaignId"` | JOIN с внешними данными |

## Почему SET LOGGED после сборки

- UNLOGGED таблицы: INSERT в 2–3× быстрее (без WAL)
- Но при аварийном отключении данные теряются
- После `SET LOGGED` данные защищены репликацией WAL
- `VACUUM ANALYZE` после `SET LOGGED` — обязательно, чтобы статистика была актуальна
