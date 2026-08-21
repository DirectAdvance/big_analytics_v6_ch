# STEP.md — Шаг 0: ClickHouse Preflight

## Что делает

Проверяет наличие и ненулевые counts входных таблиц ClickHouse. Данные не копирует.

## Проверяемые группы

| Группа | Откуда | Пример |
|---|---|---|
| RAW | `config.ch_settings.RAW_SOURCE_TABLES` + `RAW_REQUIRED_EXTRA` | `raw_data.leads_all`, `raw_data.yandex_direct_report_rows` |
| Reference | `RAW_REQUIRED_EXTRA` | `reference_data.direct_campaigns`, `reference_data.gsheet_sites` |
| Manual | `CH_MANUAL_INPUTS` | `ad_analytics.local_pixel_config`, `ad_analytics.gsheets_crop_targeting_account` |

`local_pixel_price_history` разрешена пустой. Все остальные объекты должны существовать и иметь строки.

## Следующий Шаг

Шаг 1 (`step1_load_raw`) пересобирает рабочие `ad_analytics.raw_*` таблицы из ClickHouse-источников.
