# STEP.md — Шаг 0: Синхронизация локальных копий

## Что делает

Копирует данные из FDW-источников (`src.*`) в локальные таблицы (`public.*_local`).
После step0 все последующие шаги работают только с `local_*`; источник больше не трогается.

### Стратегии

| Таблица | Метод | Описание |
|---------|-------|---------|
| `local_yandex` | **ОТКЛЮЧЁН** | с VARA_DROP_LOCAL_YANDEX_FDW_2026-06-17; потребители читают FDW `yandex_direct_manager_reports` напрямую |
| `local_leads_all` | TRUNCATE+INSERT | атомарно в одной транзакции (`_sync_truncate_insert`) |
| `local_domains` | TRUNCATE+INSERT | динамическая схема |
| `local_crm_statuses` | TRUNCATE+INSERT | справочник статусов; после — `_ensure_*_crm_statuses` |
| `local_gsheet_*` | TRUNCATE+INSERT | 5 Google-таблиц: `local_gsheet_sites`, `local_gsheet_naming`, `local_gsheet_plan_fakt`, `local_gsheet_autosalony_clients`, `local_gsheet_priezdi_marcar` |
| `local_perform_leads` | TRUNCATE+INSERT | лиды Перформа по `created_date`/`arrival_date` (PERFORM_LEADS_2026-07-01) |
| `local_telega_in_orders` | TRUNCATE+INSERT | FDW в той же БД; защита от 0-строк |
| `local_vk_ads_stats_day` | TRUNCATE+INSERT | расходы ВК Реклама, только Авто-аккаунты, spent>0 (VK_ADS_INTEGRATION_2026-07-06) |

### Постобработка (после основных синков)

- `_patch_marcar_statuses()` — backfill статусов Маркар в `local_leads_all` из `local_gsheet_priezdi_marcar`
- `_apply_perform_statuses()` — UPDATE статуса `local_perform_leads` по телефону из `local_leads_all`
- `_apply_telega_replacements()` + `_fill_telega_utm_from_post_links()` — обогащение Telega.in ордеров

### Принцип локальных копий

Защита от обнуления: если src вернул 0 строк — TRUNCATE не делается, остаются старые данные. (`step0.py:1037`)

## Конфигурация

| Константа | Значение | Описание |
|-----------|----------|---------|
| `DATE_FROM` | `'2026-01-01'` | Начало данных (первый запуск) |
| `STREAM_CHUNK` | `20 000` | Строк за раз через server-side cursor |
| `TRUNCATE_LOCK_TIMEOUT_MS` | `45 000` | `_truncate_with_lock_guard`, 45 с на попытку (LOCK_TIMEOUT_GUARD_2026-07-10) |
| `TRUNCATE_LOCK_RETRIES` | `4` | 4 попытки TRUNCATE, 3 бэкоффа (5→10→20 с) |

## Ключевые таблицы OUT

### `local_leads_all` (LOGGED, постоянная)

Копия `src.leads_all`. Ключевые колонки: `id`, `created_date`, `domain_id`, `domain`, `deal_type`,
`status`, `utm_*`, `updated_at`, `phone_norm`.
Индексы: `(updated_at)`, `(domain_id)`, `(created_date)`.

### `local_yandex` (LOGGED, постоянная — НЕ синкается)

DDL сохранён для обратной совместимости. Синк отключён — потребители читают FDW напрямую.

## Следующий шаг

Шаг 1 (`step1_load_raw`) создаёт RAW UNLOGGED таблицы из `local_*` и FDW.
