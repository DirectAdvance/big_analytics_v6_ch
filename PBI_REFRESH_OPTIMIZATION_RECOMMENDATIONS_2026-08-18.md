# PBI_REFRESH_OPTIMIZATION_RECOMMENDATIONS_2026-08-18

Проверено на живом ClickHouse 2026-08-18 после деплоя `18b4782`.

## Короткий вывод

Для Power BI refresh новые индексы в ClickHouse почти ничего не дадут, если модель делает полный
Import (`SELECT * FROM ...`) без `WHERE`. Главный выигрыш — уменьшить payload:

1. грузить факты в звезде, а текстовые атрибуты держать в `Dim_*`;
2. убрать из import-таблиц дубли колонок и константные нули;
3. не импортировать 40M/65M сырых строк, если странице нужен агрегат;
4. материализовать только те PBI-проекции, где VIEW каждый refresh заново делает дорогие join/cast.

## Текущий PBI-контур

`refresh_powerbi._ALL_TABLES` сейчас содержит 25 таблиц. Новый слой `build_pbi_compat.PBI_SOURCE_OBJECTS`
содержит 39 `bi_*` view, включая добавленные:

| View | Engine | Строк |
|---|---|---:|
| `bi_yandex_direct_ads_texts` | View | 65 241 324 |
| `bi_yandex_direct_type_placement_report_master` | View | 8 398 376 |

Эти две view пока не добавлены в `_ALL_TABLES`: это правильно до проверки опубликованной PBI-модели.

Дополнительно подготовлен безопасный star-слой для самой тяжёлой вкладки «Фиды»:

| Объект | Строк | Колонок | Диск |
|---|---:|---:|---:|
| `pbi_import_fact_direct_feed_funnel` | 13 475 572 | 29 | 144.45 MiB |
| `pbi_import_fact_direct_feed_funnel_star` | 13 475 572 | 11 | 115.16 MiB |

`bi_fact_direct_feed_funnel_star` не заменяет старый `bi_fact_direct_feed_funnel`: текущая Power BI
модель продолжает получать совместимый 29-колоночный объект. Новый слой нужен для перевязки модели
на звезду без риска сломать действующий refresh.

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

65.2M строк, 18 колонок, источник `raw_data.direct_cookie_ads_texts_master`.

Это слишком много для прямого Import, особенно если в отчёте нужна не каждая строка объявления за
день, а агрегаты. Индекс не поможет: полный импорт всё равно прочитает все 65.2M строк.

Что поможет:

- не добавлять в `_ALL_TABLES`, пока PBI-модель не доказала, что ей реально нужна эта таблица;
- если таблица нужна, сделать отдельный PBI-агрегат под фактическую страницу: например
  `date_from/date_to, client_login, campaign_id, ad_group_id, ad_type, state/status` без текстов
  объявления, либо держать тексты в `Dim_AdText` по `ad_id`;
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

- не индекс, а убрать из import всё, что восстанавливается из `Dim_Location`/`Dim_Site`;
- проверить DAX/визуалы на фактическое использование `domain`, `updated_at`, `distance_km`;
- если они не нужны, сделать более узкую `bi_fact_region_spend_light`.

### P1. `bi_fact_criterion_spend`

5.0M строк, 22 колонки. В физическом факте 11 колонок, а PBI view добавляет совместимость:
строковый `criterion`, нулевые CRM-поля и `updated_at`.

Что поможет:

- PBI должен читать `criterion_key` + `Dim_Criterion`, а не тянуть строковый `criterion` в каждый
  ряд факта;
- убрать нулевые funnel-колонки из import после проверки DAX.

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

1. Снять из Power BI Service/Desktop фактические M-запросы и список используемых колонок для самых
   тяжёлых таблиц.
2. Первым перевязать Power BI на `bi_fact_direct_feed_funnel_star` + `bi_Dim_PlacementFeed`.
3. Новые `yandex_direct_ads_texts` не включать в selective refresh до решения: сырые 65M строк или
   отдельный агрегат/звезда по `ad_id`.
