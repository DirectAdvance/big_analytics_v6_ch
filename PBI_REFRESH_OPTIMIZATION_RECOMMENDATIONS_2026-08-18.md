# PBI_REFRESH_OPTIMIZATION_RECOMMENDATIONS_2026-08-18

Проверено на живом ClickHouse 2026-08-18 после деплоя `b92f400`.

## Короткий вывод

Для Power BI refresh новые индексы в ClickHouse почти ничего не дадут, если модель делает полный
Import (`SELECT * FROM ...`) без `WHERE`. Главный выигрыш — уменьшить payload:

1. грузить факты в звезде, а текстовые атрибуты держать в `Dim_*`;
2. убрать из import-таблиц дубли колонок и константные нули;
3. не импортировать 40M/65M сырых строк, если странице нужен агрегат;
4. материализовать только те PBI-проекции, где VIEW каждый refresh заново делает дорогие join/cast.

## Обновление 2026-08-18

Сделано после перевязки spend/feed на `*_star`:

- `yandex_direct_ads_texts` и `yandex_direct_type_placement_report_master` в PBIP переведены с
  `raw_new_*` на `bi_*`;
- группировка этих двух источников перенесена из Power Query в ClickHouse view, чтобы Power BI не
  импортировал сырые 65M строк `direct_cookie_ads_texts_master`;
- `analytics_report_placement` не перевязан на `fact_direct_feed_funnel`: на одинаковом периоде
  2026-07-01…2026-08-13 суммы не совпадают (`raw_new_arp_fact`: 1.91M строк / 179.6M cost;
  candidate: 2.53M строк / 315.7M cost). Нужен отдельный совместимый ARP-источник или явное
  решение менять смысл вкладки.

## Текущий PBI-контур

`refresh_powerbi._ALL_TABLES` сейчас содержит 25 таблиц. Новый слой `build_pbi_compat.PBI_SOURCE_OBJECTS`
содержит 42 `bi_*` view, включая direct-cookie совместимые view:

| View | Engine | Строк |
|---|---|---:|
| `bi_yandex_direct_ads_texts` | View | 53 488 831 |
| `bi_yandex_direct_type_placement_report_master` | View | 8 398 376 |

Эти две view уже используются PBIP напрямую, но пока не добавлены в `_ALL_TABLES`: это правильно до
проверки опубликованной PBI-модели и фактического refresh в Service.

Подготовленные star-слои уже подключены в BA6 PBIP для feed/region/criterion. Старые совместимые
`bi_*` остаются fallback-объектами до успешного Desktop/Service refresh.

| Объект | Строк | Колонок | Диск |
|---|---:|---:|---:|
| `pbi_import_fact_direct_feed_funnel` | 13 475 572 | 29 | 144.45 MiB |
| `pbi_import_fact_direct_feed_funnel_star` | 13 475 572 | 11 | 115.16 MiB |
| `bi_fact_region_spend_star` | 14 384 620 | 14 | view |
| `bi_fact_criterion_spend_star` | 5 042 536 | 9 | view |

`region` и `criterion` оставлены view, а не физическими таблицами: это простые проекции без join и
агрегаций, поэтому материализация только увеличила бы диск ClickHouse.

## Где индексы не помогут

Power BI Import обычно читает таблицу целиком. При таком запросе ClickHouse не может отсечь части
по primary key:

```sql
SELECT * FROM ad_analytics.bi_fact_region_spend
SELECT * FROM ad_analytics.bi_fact_direct_feed_funnel
SELECT * FROM ad_analytics.bi_yandex_direct_ads_texts
```

`ORDER BY` полезен для ETL и выборок с фильтром по дате/ключам, но не ускоряет передачу полного
набора в Power BI. Data skipping indexes тут тоже не нужны: им нечего skip-ать без условия.

## Реальные кандидаты на ускорение refresh

### P0. `bi_yandex_direct_ads_texts`

Источник `raw_data.direct_cookie_ads_texts_master`.

Сырых строк 65.2M, агрегированная view отдаёт 53.5M. Индекс не поможет: полный импорт всё равно
прочитает все строки. Поэтому `bi_yandex_direct_ads_texts` теперь отдаёт набор в гранулярности
PBIP `loaded_at/client_login/campaign_id/ad_group_id/ad_type/status/title/text`.

Что поможет:

- не добавлять в `_ALL_TABLES`, пока PBI-модель не доказала, что ей реально нужна эта таблица;
- если refresh всё ещё тяжелый, держать тексты в `Dim_AdText` по `ad_id`;
- `title`, `text`, `banner_href` вынести в dimension, если они нужны только для детализации.

### P0. `bi_fact_direct_feed_funnel`

13.4M строк, 29 колонок. Физический `pbi_import_fact_direct_feed_funnel` весит 144 MiB.

Что поможет:

- уже создан `bi_fact_direct_feed_funnel_star`: факт оставляет только ключи и метрики;
- `bi_Dim_PlacementFeed` отдаёт `placement_feed_id`, `placement_feed_key_hash` и текстовые атрибуты;
- следующий шаг — в Power BI добавить связь
  `fact_direct_feed_funnel_star.placement_feed_id -> Dim_PlacementFeed.placement_feed_id`, после
  чего можно заменить старый 29-колоночный import в модели.

Индекс не поможет: текущий `ORDER BY` у обоих физических слоёв date-first, но refresh читает весь
факт. Выигрыш даёт не индекс, а меньше колонок и вынос строк в dimension.

### P1. `bi_fact_region_spend`

14.3M строк, 17 колонок. Физический `fact_region_spend` весит 116 MiB и уже имеет нормальный
date-first layout:

```text
PARTITION BY toYYYYMM(date)
ORDER BY (date, campaign_id, ad_group_id, ad_network_type_key, id_location)
```

Что поможет:

- уже создан `bi_fact_region_spend_star`: без `domain`, `updated_at`, `distance_km`, с
  `id_location` и `site_key`;
- в Power BI связать `id_location -> Dim_Location.id_location` и `site_key -> Dim_Site.site_key`;
- после перевязки заменить старый 17-колоночный import.

### P1. `bi_fact_criterion_spend`

5.0M строк, 22 колонки. В физическом факте 11 колонок, а PBI view добавляет совместимость:
строковый `criterion`, нулевые CRM-поля и `updated_at`.

Что поможет:

- уже создан `bi_fact_criterion_spend_star`: 9 колонок, без строкового `criterion`, `domain`,
  `updated_at` и нулевых funnel-колонок;
- в Power BI связать `criterion_key -> Dim_Criterion.criterion_key` и `site_key -> Dim_Site.site_key`;
- после перевязки заменить старый 22-колоночный import.

### P2. `bi_pbi_big_analytics_full`

5.3M строк, 42 колонки, 20 string-колонок. Физический `fact_big_analytics` уже лёгкий:
61 MiB, 35 колонок, date-first layout.

Что поможет:

- окончательно перевязать Power BI на звезду (`fact_big_analytics` + `Dim_*`) и не импортировать
  плоскую compatibility-витрину как главный источник;
- оставить `bi_pbi_big_analytics_full` только как fallback/transition object.

## Что не делать

- Не добавлять ClickHouse indexes на `bi_*` view: индексы ставятся на таблицы, а не на VIEW.
- Не добавлять skipping index на факты без конкретного `WHERE` из Power BI query diagnostics.
- Не материализовать всё подряд: это ускорит чтение некоторых view, но увеличит время pipeline и
  хранение. Материализовать только тяжёлые projection-views, если PBI реально читает их целиком.

## Следующий практический шаг

1. Открыть BA6 PBIP в Power BI Desktop и выполнить полный refresh.
2. После успешного Desktop refresh опубликовать датасет в Power BI Service.
3. По Query Diagnostics проверить, какие из оставшихся тяжёлых таблиц реально тормозят:
   `analytics_report_placement`, `analytics_report_placement_links`,
   `yandex_direct_search_query_report_master`.
4. Для `analytics_report_placement` не использовать `fact_direct_feed_funnel` как замену без
   отдельного пересчёта: проверка 2026-07-01..2026-08-13 показала разные суммы.
