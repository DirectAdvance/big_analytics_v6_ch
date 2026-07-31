# step0_sync_local — ClickHouse preflight

В v6_ch `step0` ничего не копирует из PostgreSQL/v5. Шаг только проверяет, что в ClickHouse уже есть все источники, из которых дальше строится pipeline.

## Что проверяется

- `raw_data`: `yandex`, `leads`, `calls`, `domains`, `crm_statuses`, `direct_campaigns`, `gsheet_sites`, `telega_in_orders`, `vk_ads_stats_day`, `yandex_direct_korrektirovki`.
- `ad_analytics`: ручные/локальные CH inputs для посевов, пикселя и cookies:
  `local_pixel_config`, `local_pixel_price_history`, `gsheets_crop_targeting_account`,
  `gsheets_crop_targeting_account_leads`, `gsheets_crop_targeting_account_pravilo_utm`,
  `yandex_direct_cookie_analytics_website_pages`.

## Почему так

Раньше step0 синхронизировал `local_*` из PostgreSQL/FDW. В v6_ch это было ошибкой для parity: таблицы должны пересоздаваться текущим ClickHouse pipeline, а не копироваться из v5. Поэтому step0 стал read-only preflight.

## Запуск

```bash
python3 pipeline.py --only-step 0
```

Успешный шаг логирует row counts по каждому обязательному источнику. Если таблица отсутствует или критически пуста, шаг падает до тяжёлых downstream insert.
