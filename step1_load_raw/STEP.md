# STEP.md — Шаг 1: RAW UNLOGGED таблицы

## Что делает

Создаёт RAW UNLOGGED таблицы из локальных копий. Каждый запуск: DROP → CREATE UNLOGGED → INSERT.

## Таблицы

| RAW таблица | Источник | Фильтры |
|-------------|----------|---------|
| `raw_yandex` | `yandex_direct_manager_reports` (FDW) | `CampaignId IS NOT NULL AND != 0`, `Date >= '2026-01-01'` |
| `raw_leads` | `local_leads_all` | `deal_type != 'Звонок'` AND `domain_id NOT IN (1645, 883)` |
| `raw_calls` | `local_leads_all` | `deal_type = 'Звонок'` |
| `raw_domains` | `local_domains` | нет |
| `raw_perform_leads` | `local_perform_leads` + ветка (b) `local_leads_all` | `deal_type IS NULL OR != 'Звонок'`, `domain_id NOT IN (1645, 883)` |

## Почему UNLOGGED

- Без записи в WAL → INSERT в 2–3× быстрее
- Таблицы пересоздаются каждый запуск — нет накопленного мусора
- При аварийном отключении сервера данные теряются (допустимо: пересоздаём при следующем запуске)

## Почему domain_id 1645 и 883 исключены

`1645` — priezd shared key3: общий ключ с другим доменом → лиды дублируются в директе.  
`883` — victory-crm.ru: не клиент. Оба исключены из `raw_leads` и `raw_perform_leads`.

## Следующий шаг

Шаг 2 добавляет индексы на RAW таблицы и запускает ANALYZE.  
Индексы создаются ПОСЛЕ вставки данных — так они строятся один раз по готовым данным.
