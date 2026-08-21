# STEP.md — Шаг 1: RAW ClickHouse

## Что Делает

Пересобирает рабочие RAW-объекты в `ad_analytics` из `raw_data.*` и `reference_data.domains`.

## Таблицы

| RAW объект | Источник | Фильтр |
|---|---|---|
| `raw_yandex` | `raw_data.yandex_direct_report_rows` | `campaign_id != 0`, `date >= '2026-01-01'` |
| `raw_leads` | `raw_data.leads_all` + `reference_data.domains` | не звонки, `is_copy_for_removal=0`, домен не исключён |
| `raw_calls` | `raw_data.leads_all` + `reference_data.domains` | `deal_type='Звонок'`, `is_copy_for_removal=0` |
| `raw_domains` | `reference_data.domains` | view |
| `raw_perform_leads` | нет live-источника | совместимая пустая таблица |

## Важно

- `is_copy_for_removal=1` из `raw_data.leads_all` полностью исключается.
- `salon` с префиксным client-code резолвится через `raw_data.gsheet_autosalony_clients.client_id`.
  Пустой extracted key становится `NULL`, чтобы не матчить пустые `client_id`.
- `victory-crm.ru` фильтруется по имени домена, не по numeric id.
- `raw_yandex` проверяется guard-ом: `sum(total_cost)` не должен быть 0.

## Следующий Шаг

Шаг 2 выполняет `OPTIMIZE TABLE ... FINAL` для физических RAW-таблиц.
