# STEP.md — Шаг 2: Индексы + ANALYZE на RAW

## Что делает

Добавляет индексы на RAW UNLOGGED таблицы и запускает `ANALYZE`.

## Индексы

| Таблица | Индекс | Колонки | Зачем |
|---------|--------|---------|-------|
| `raw_yandex` | `idx_raw_yandex_date` | `"Date"` | JOIN по дате с лидами |
| `raw_yandex` | `idx_raw_yandex_campaign_id` | `"CampaignId"` | JOIN по кампании |
| `raw_yandex` | `idx_raw_yandex_account` | `account_login` | JOIN с нэймингом |
| `raw_leads` | `idx_raw_leads_domain_id` | `domain_id` | JOIN с gsheet_sites |
| `raw_leads` | `idx_raw_leads_created` | `created_date` | фильтрация по дате |
| `raw_leads` | `idx_raw_leads_utm_source` | `utm_source` | фильтрация источника (SEO/pixel/telegram) |
| `raw_leads` | `idx_raw_leads_utm_campaign` | `utm_campaign` | JOIN с посевами |
| `raw_calls` | `idx_raw_calls_domain_id` | `domain_id` | агрегация звонков по домену |
| `raw_calls` | `idx_raw_calls_created` | `created_date` | агрегация по дате |
| `raw_domains` | `idx_raw_domains_id` | `id` | JOIN по domain_id |

## Почему после вставки

Создание индексов до `INSERT` замедляет запись (индекс обновляется на каждой строке).  
Создание после `INSERT` — один проход по готовым данным, быстрее в 3–10×.

## ANALYZE

После индексов выполняется `ANALYZE` на всех RAW таблицах.  
Это обновляет статистику планировщика → корректные планы JOIN в шагах 3–4.
