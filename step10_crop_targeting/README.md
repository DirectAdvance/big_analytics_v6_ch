# step10_crop_targeting — cost overlays посевов и VK Ads

`step10` в v6_ch восстанавливает ненулевые расходы для источников, которые не приходят как обычный Direct cost:

- `crop_targeting` / посевы из Google Sheets и Telega.in;
- `vk_ads` из `raw_data.vk_ads_stats_day`;
- локальные pixel price/config таблицы используются downstream как CH inputs, без PostgreSQL sync.

## Источники

- `raw_data.telega_in_orders` — базовые Telega.in заказы.
- `ad_analytics.telega_in_order_field_overrides` — ручные UTM/field overrides из `telega_in_orders_replacements.json`.
- `ad_analytics.telega_in_order_price_overrides` — CH-таблица ручных price overrides, сохраняется между прогонами.
- `ad_analytics.gsheets_crop_targeting_account*` — ручные Google Sheets посевы, уже загруженные в CH.
- `raw_data.vk_ads_stats_day` — VK Ads spend.
- `raw_data.gsheet_sites`, `ad_analytics.raw_leads` — lookup и матчинг заявок.

## Что строится

- `ad_analytics.local_telega_in_orders`
- `ad_analytics.local_telega_in_orders_errors`
- `ad_analytics.crop_targeting_api_telegain_lead`
- `ad_analytics.big_analytics_cost_overlays`
- обновлённый `ad_analytics.big_analytics_full` с overlay-расходами для `crop_targeting` и `vk_ads`

## Батчинг

Полный rebuild `big_analytics_full` в step10 идёт через shadow-table и недельные keep-batches:

```python
range_batches(DATE_FROM, days=7)
```

Это уменьшает число insert-select запросов на keep-части с 212 дневных до 31 недельного батча. После swap повторный step10 должен быть идемпотентным по сумме `total_cost`.

## Проверки

```bash
python3 pipeline.py --only-step 10
python3 data_check/verify_big_analytics.py
```

Ключевые SQL-инварианты:

- `big_analytics_cost_overlays` не должен иметь дублей по overlay key.
- `vk_ads` overlay spend должен совпадать с `raw_data.vk_ads_stats_day` по тем же ключам.
- `crop_targeting` и `vk_ads` в `big_analytics_full` должны иметь ненулевой `total_cost`, но нулевые funnel-метрики в overlay-строках.
