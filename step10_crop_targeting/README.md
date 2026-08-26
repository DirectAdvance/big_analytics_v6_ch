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
- `reference_data.gsheet_sites`, `ad_analytics.raw_leads` — lookup и матчинг заявок.

## Что строится

- `ad_analytics.local_telega_in_orders` — view над `raw_data.telega_in_orders` + ручные field/price overrides.
- `ad_analytics.local_telega_in_orders_errors`
- `ad_analytics.crop_targeting_api_telegain_lead` с BA5-формулой Telega.in API: `kval = korr - ne_otvechaet - filtr - nedozvon`.
- `ad_analytics.big_analytics_cost_overlays`
- обновлённый `ad_analytics.big_analytics_full` с overlay-расходами и funnel-метриками для Google Sheets/Telega.in; raw `social_посевы`/`telegram`, покрытые Telega.in-заказом, вырезаются по UTM-ключу.
- Для посевных overlay продажа поднимает нижние кредитные шаги: `dohod_do_kredita >= dobro >= prodazhi`; иначе итоговая BI-воронка получает невозможное состояние "продажа без одобрения".

`local_telega_in_orders` не держим физически: сырьё уже есть в `raw_data.telega_in_orders`, а отличия
хранятся отдельно в `telega_in_order_field_overrides` и `telega_in_order_price_overrides`.

## Батчинг

Полный rebuild `big_analytics_full` в step10 идёт через shadow-table и общие дневные батчи pipeline:

```python
day_ranges(DATE_FROM)
```

После swap повторный step10 должен быть идемпотентным по сумме `total_cost`: перед новой вставкой
из `big_analytics_full` удаляются все старые строки `cascade_level='cost_overlay'`, включая
исторические строки без префикса `crop_cost|`.

## Проверки

```bash
python3 pipeline.py --only-step 10
python3 data_check/verify_big_analytics.py
```

Ключевые SQL-инварианты:

- `big_analytics_cost_overlays` не должен иметь дублей по overlay key.
- `vk_ads` overlay spend должен совпадать с `raw_data.vk_ads_stats_day` по тем же ключам и не
  терять `domain`/`салон`/`регион`/`специалист`: lookup идёт через
  `reference_data.vk_ads_agency_clients -> reference_data.gsheet_sites` только для `niche='Авто'`.
- `vk_ads` overlay-строки остаются с нулевой воронкой.
- Google Sheets/Telega.in overlay-строки несут свою funnel-воронку, где `prodazhi` включены в `dohod_do_kredita` и `dobro`; перекрытые raw `social_посевы`/`telegram` строки удаляются из `big_analytics_full_new`.
