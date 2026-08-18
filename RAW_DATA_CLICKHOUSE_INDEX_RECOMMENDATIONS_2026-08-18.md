# RAW_DATA_CLICKHOUSE_INDEX_RECOMMENDATIONS_2026-08-18

Проверено на живом ClickHouse 2026-08-18. Ничего в `raw_data` не менялось: это слой внешних
загрузчиков. Ниже — что стоит попросить у владельца `raw_data` или учесть при следующей версии
таблиц.

## Короткий вывод

Индексировать всё подряд не нужно. Для BA6 реально болит только один источник:
`raw_data.yandex_direct_report_rows`.

| Приоритет | Таблица | Решение | Почему |
|---|---|---|---|
| P0 | `raw_data.yandex_direct_report_rows` | сделать date-first projection или новую версию таблицы с `PARTITION BY toYYYYMM(day_date)` и `ORDER BY (day_date, client_login, campaign_id, ad_group_id, row_key)` | BA6 почти всегда читает её недельными/месячными окнами по дате без `client_login`; текущая сортировка не отсекает гранулы |
| Done | `raw_data.vk_ads_stats_day` | BA6-фильтры поправлены в `step10_crop_targeting/step10.py` и `star_refactor/build_star.py`; индекс в `raw_data` не нужен | физически таблица уже отсортирована по `date`, после строкового фильтра ClickHouse отсекает месячную партицию и гранулы |
| Не нужно | `direct_cookie_ads_texts_master`, `direct_cookie_type_placement_master`, `metrika_yandex_utm_daily`, `leads_all` | оставить как есть | текущие partition/order уже совпадают с нашими основными date-фильтрами |
| Не нужно | `gsheet_sites`, `crm_status_mapping`, `domains`, `direct_campaigns`, `direct_adgroups`, `direct_ads`, `telega_in_orders` | оставить как есть | маленькие таблицы или уже нормальный ключ под lookup/group-by |

## Факты по текущей физике таблиц

В `raw_data` сейчас нет data skipping indexes для проверенных BA6-таблиц. Работают только
`PARTITION BY` и primary/sorting key.

| Таблица | Строки | Размер | Partition | Sorting key |
|---|---:|---:|---|---|
| `yandex_direct_report_rows` | 26 416 882 | 2.08 GiB | `cityHash64(client_login) % 64` | `client_login, day, row_key` |
| `leads_all` | 1 913 120 | 160.70 MiB | `toYYYYMM(created_date)` | `id` |
| `vk_ads_stats_day` | 2 128 717 | 225.58 MiB | `substring(date, 1, 7)` | `date, account_kind, account_id, level, object_id` |
| `metrika_yandex_utm_daily` | 7 462 367 | 202.94 MiB | `toYYYYMM(day)` | `day, counter_id, utm_source, ...` |
| `direct_cookie_ads_texts_master` | 65 241 324 | 2.23 GiB | `toYYYYMM(scope_from)` | `scope_from, client_login, banner_id` |
| `direct_cookie_type_placement_master` | 8 398 376 | 220.83 MiB | `toYYYYMM(scope_from)` | `scope_from, client_login, position_type` |

## P0. `yandex_direct_report_rows`

BA6 читает эту таблицу в самых тяжёлых местах:

- `step1_load_raw` строит `raw_yandex`;
- `direct_spend_staging` строит общий staging для spend-фактов;
- `step_cron_night` UTM-аудит читает последние 30/90 дней;
- `direct_placement_links` ищет tp8/tp9/tp10 placements;
- проверки golden читают логины Кудерко.

Типичный BA6-фильтр:

```sql
WHERE toDate(day) >= toDate('2026-07-01')
  AND toDate(day) < toDate('2026-07-08')
  AND campaign_id != 0
```

Текущий `EXPLAIN indexes=1` для недельного date-only фильтра:

```text
Parts: 230/230
Granules: 3318/3318
PrimaryKey Keys: day
Ranges: 230
```

То есть недельный батч читает все гранулы. Если добавить `client_login`, текущая физика работает:

```text
Partition: Parts 1/89
PrimaryKey: Granules 3/27
```

Проблема не в отсутствии любого индекса, а в том, что таблица отсортирована login-first, а BA6
основной объём читает date-first.

### Лучший вариант для следующей версии таблицы

Если владелец `raw_data` может поменять физику при пересоздании:

```sql
PARTITION BY toYYYYMM(day_date)
ORDER BY (day_date, client_login, campaign_id, ad_group_id, row_key)
```

Где `day_date Date` — нормальная Date-колонка вместо строкового `day`. Это лучше любого
secondary/skipping index: ClickHouse сможет отрезать месяцы и внутри месяца искать недельные
диапазоны бинарно по primary key.

### Если таблицу нельзя пересоздать

Вариант с projection под BA6 date-first чтение:

```sql
ALTER TABLE raw_data.yandex_direct_report_rows
ADD PROJECTION p_ba6_day
(
    SELECT *
    ORDER BY (toDate(day), client_login, campaign_id, ifNull(ad_group_id, 0), row_key)
);

ALTER TABLE raw_data.yandex_direct_report_rows
MATERIALIZE PROJECTION p_ba6_day;
```

Минус: projection фактически хранит второй отсортированный layout, то есть добавит место и работу
на вставках. Для таблицы около 2.1 GiB это терпимо, но применять должен владелец `raw_data` после
проверки на их ingest-скорости.

Что не рекомендую:

```sql
ALTER TABLE ... ADD INDEX idx_day toDate(day) TYPE minmax GRANULARITY 1;
```

При текущем `ORDER BY (client_login, day, row_key)` каждый логин несёт широкий диапазон дат, поэтому
minmax по `day` даст мало пользы для date-only батчей. Projection/date-first layout честнее.

## Done. `vk_ads_stats_day`

Физика таблицы уже нормальная для дат:

```text
PARTITION BY substring(date, 1, 7)
ORDER BY (date, account_kind, account_id, level, object_id)
```

Но BA6 местами фильтрует так:

```sql
WHERE toDateOrNull(date) >= toDate('2026-01-01')
  AND account_id IN (...)
```

`EXPLAIN indexes=1` на такой шаблон:

```text
Parts: 20/20
Granules: 770/770
PrimaryKey Keys: account_id
```

BA6 SQL исправлен на строковый фильтр по ISO-дате:

```sql
WHERE date >= '2026-01-01'
```

Так как `date` хранится строкой `YYYY-MM-DD`, лексикографический порядок совпадает с датой. Это
дешевле и безопаснее, чем просить новый raw_data index.

Проверка 2026-08-18:

- `raw_data.vk_ads_stats_day`: 2 128 717 строк, не-ISO дат `0`, диапазон `2026-01-01..2026-08-17`;
- старый и новый фильтры дают одинаковые строки/расход/показы/клики: дельта `0`;
- недельный `EXPLAIN indexes=1`: было `Parts 20/20`, `Granules 770/770`; стало
  `Parts 1/20`, `PrimaryKey Granules 24/38`.

Projection не нужен. Возвращаться к нему стоит только если профиль полного прогона снова покажет
`fact_vk_ads` или `step10` VK-overlay в горячем списке:

```sql
ALTER TABLE raw_data.vk_ads_stats_day
ADD PROJECTION p_ba6_date_account
(
    SELECT *
    ORDER BY (date, account_id, ad_plan_id, ad_group_id, banner_id)
);
```

## Что оставить как есть

### `direct_cookie_ads_texts_master`

`EXPLAIN indexes=1` для недельного окна по `scope_from`:

```text
Parts: 1/63
Granules: 344/9126
```

Физика совпадает с будущим BA6/PBI использованием: `PARTITION BY toYYYYMM(scope_from)`,
`ORDER BY (scope_from, client_login, banner_id)`.

### `direct_cookie_type_placement_master`

`EXPLAIN indexes=1` для недельного окна по `scope_from`:

```text
Parts: 1/34
Granules: 41/1108
```

Индексировать дополнительно не надо.

### `leads_all`

Date-фильтр по `created_date` уже отсекает месяцы:

```text
Parts: 2/58
Granules: 24/274
```

Lookup по `id` тоже покрыт sorting key `id`. Таблица всего 160 MiB, дополнительные индексы не
окупятся. Если когда-нибудь будет отдельный тяжёлый поток по `arrival_date`, тогда обсуждать
projection `(arrival_date, id)`, но сейчас это YAGNI.

### Остальные таблицы

- `metrika_yandex_utm_daily`: date-first layout уже есть.
- `metrika_yandex_not_found_daily`: маленькая и date-first.
- `direct_campaigns`, `direct_adgroups`, `direct_ads`: сортировка `account_login + id`, объём малый.
- `yandex_direct_korrektirovki`: 3.3 MiB, индекс не нужен.
- `gsheet_sites`, `crm_status_mapping`, `domains`: тысячи строк, скан дешевле обслуживания индекса.
- `telega_in_orders`: 7 381 строк, индекс не нужен.

## Что делать дальше

1. Попросить владельца `raw_data` рассмотреть date-first projection или новую физику
   `yandex_direct_report_rows`.
2. Не добавлять index/projection на `raw_data.vk_ads_stats_day`: BA6 уже использует текущий
   sorting key.
3. Не добавлять skipping indexes без `EXPLAIN` до/после: сейчас видимый выигрыш есть только у
   date-first layout для `yandex_direct_report_rows`.
