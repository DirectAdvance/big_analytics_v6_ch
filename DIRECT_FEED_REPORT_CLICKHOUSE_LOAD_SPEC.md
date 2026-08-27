# Direct feed report для BA6 ClickHouse

Цель: вернуть в BA6 настоящую страницу фидов как в BA5. Для этого нужен не только справочник URL
фидов, а сырой расходный отчёт Директа на уровне `date × campaign × adgroup/ad × feed_id`.

## Что уже есть в BA6

В ClickHouse сейчас есть только реестр фидов:

```sql
raw_data.direct_cookie_feed_urls
    source_run_id UUID,
    extracted_at DateTime64(6, 'UTC'),
    loaded_at DateTime64(6, 'UTC'),
    manager_login String,
    client_login String,
    feed_id Int64,
    feed_name Nullable(String),
    feed_url Nullable(String),
    feed_url_key Nullable(String),
    feed_type Nullable(String),
    update_status Nullable(String),
    feed_source Nullable(String),
    offers_count Nullable(Int64),
    listings_count Nullable(Int64),
    last_change Nullable(DateTime64(3)),
    campaign_ids Array(Int64)
```

На 2026-08-25 проверено: в `reference_data` feed-таблиц нет. В
`raw_data.direct_cookie_feed_urls` есть 52 572 строк, 14 012 `feed_id`, 1 010 клиентов.

Этого недостаточно для расхода. `campaign_ids` нельзя использовать для разнесения денег:
1 898 кампаний привязаны к 2 фидам, ещё есть кампании с 3-11 фидами. Join
`campaign_id -> campaign_ids` размножит расход.

## Как было в BA5

BA5 строит фидовую витрину из двух разных источников:

1. `yandex_direct_raw.yandex_direct_feeds_report` — настоящий расходный отчёт по фидам.
2. `yandex_direct_raw.yandex_direct_feed_urls` — реестр URL фидов, только для обогащения
   `feed_url/feed_url_key`.

BA5 не считает фидовый расход из кампанийного расхода. В коде это лежит в
`work/big_analytics_v5/direct_feed_funnel/build_keyed.py`.

Ключевые правила BA5:

- расход берётся из `yandex_direct_feeds_report.cost`;
- `feed_url/feed_url_key` добираются из URL-реестра по `(login_key, feed_id)`;
- стабильный `feed_key` строится из URL фида, не из имени кампании;
- домен расхода берётся из URL фида;
- итоговая связка расхода и CRM-лидов идёт по `date|domain|feed_key`;
- `campaign_id` не входит в финальный ключ воронки, потому что кампания лида и кампания клика по
  фиду могут расходиться.

## Что нужно загрузить в ClickHouse

Нужна новая таблица:

```sql
raw_data.direct_feed_report_rows
(
    source_run_id UUID,
    extracted_at DateTime64(6, 'UTC'),
    loaded_at DateTime64(6, 'UTC'),

    date Date,
    manager_login String,
    client_login String,

    campaign_id Int64,
    campaign_name Nullable(String),
    ad_group_id Nullable(Int64),
    ad_group_name Nullable(String),
    ad_id Nullable(Int64),

    feed_id Int64,
    feed_name Nullable(String),
    feed_source_type Nullable(String),

    impressions Nullable(Int64),
    clicks Nullable(Int64),
    cost Nullable(Decimal(38, 9)),
    total_cost Nullable(Decimal(38, 9)),

    crm_order_created Nullable(Decimal(38, 9)),
    crm_order_paid Nullable(Decimal(38, 9)),
    crm_spam_order Nullable(Decimal(38, 9)),
    crm_order_canceled Nullable(Decimal(38, 9))
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(date)
ORDER BY (date, manager_login, client_login, campaign_id, ad_group_id, ad_id, feed_id);
```

`total_cost` можно писать равным `cost`, если в API нет отдельного поля. В BA5 фидовая витрина
считала деньги именно как `sum(cost)`.

`feed_url` и `feed_url_key` в этой таблице не обязательны: они уже есть в
`raw_data.direct_cookie_feed_urls`. Если загрузчику проще положить их сразу, можно добавить:

```sql
feed_url Nullable(String),
feed_url_key Nullable(String)
```

## Источник в Yandex Direct API

BA5 использовал такой механизм:

1. `CUSTOM_REPORT` по аккаунту за дату или окно дат:

```jsonc
{
  "params": {
    "SelectionCriteria": {"DateFrom": "YYYY-MM-DD", "DateTo": "YYYY-MM-DD"},
    "FieldNames": [
      "Date", "CampaignId", "CampaignName", "AdGroupId", "AdGroupName",
      "AdId", "Impressions", "Clicks", "Cost", "Conversions"
    ],
    "Goals": [/* CRM/Metrika goal ids */],
    "AttributionModels": ["AUTO"],
    "ReportType": "CUSTOM_REPORT",
    "DateRangeType": "CUSTOM_DATE",
    "Format": "TSV",
    "IncludeVAT": "YES",
    "IncludeDiscount": "NO"
  }
}
```

2. `ads.get` добирает `ad_id -> feed_id`.

```jsonc
{
  "params": {
    "SelectionCriteria": {"CampaignIds": [/* max 10 */]},
    "FieldNames": ["Id"],
    "ShoppingAdFieldNames": ["FeedId"]
  }
}
```

3. `feeds.get` добирает имя фида:

```jsonc
{
  "params": {
    "FieldNames": ["Id", "Name", "SourceType"]
  }
}
```

Важно:

- `AdId = "--"` из отчёта Директа пропускать, потому что это удалённое объявление без стабильного
  `ad_id`.
- В `feeds.get` не передавать `SelectionCriteria`: Яндекс возвращает ошибку 8000.
- `ads.get` батчить максимум по 10 кампаний.
- `IncludeVAT` должен быть `YES`, чтобы числа сходились с кабинетом и BA5.
- `Use-Operator-Units: true`, иначе баллы будут списываться с клиента.

## Логины

Брать клиентов из текущего BA6-источника сайтов/аккаунтов, аналогично другим Direct-загрузчикам:

- ниша/направление: авто;
- клиентский логин: `client_login`;
- агентский/операторский логин: `manager_login`;
- логины с не-ASCII символами пропускать или явно логировать как skipped.

## Минимальная приёмка загрузчика

После загрузки должны проходить такие проверки:

```sql
-- источник не пустой и свежий
SELECT
    count() AS rows,
    min(date) AS min_date,
    max(date) AS max_date,
    sum(clicks) AS clicks,
    round(sum(total_cost), 2) AS cost
FROM raw_data.direct_feed_report_rows
WHERE date >= toDate('2026-01-01');

-- все расходные строки имеют feed_id
SELECT count()
FROM raw_data.direct_feed_report_rows
WHERE ifNull(total_cost, 0) > 0 AND feed_id = 0;

-- не должно быть дубликатов по естественному ключу
SELECT
    date, manager_login, client_login, campaign_id, ad_group_id, ad_id, feed_id, count()
FROM raw_data.direct_feed_report_rows
GROUP BY date, manager_login, client_login, campaign_id, ad_group_id, ad_id, feed_id
HAVING count() > 1
LIMIT 20;

-- покрытие URL-реестром
SELECT count()
FROM raw_data.direct_feed_report_rows r
LEFT JOIN raw_data.direct_cookie_feed_urls u
    ON u.client_login = r.client_login
   AND u.feed_id = r.feed_id
WHERE r.date >= toDate('2026-01-01')
  AND u.feed_id IS NULL;
```

Приёмочный минимум для BA6: таблица свежая до последней полной даты Директа, `cost > 0`,
`feed_id` заполнен у расходных строк, нет дублей по естественному ключу.

## Что BA6 сделает после появления таблицы

После появления `raw_data.direct_feed_report_rows` BA6 сможет портировать старую BA5-логику:

1. собрать keyed spend по `date × client_login × campaign × adgroup × feed_id/feed_key`;
2. обогатить `feed_url/feed_url_key` из `direct_cookie_feed_urls`;
3. построить `feed_key` из URL;
4. связать с CRM-лидами по `date|domain|feed_key`;
5. вернуть PBI-страницу фидов без подмены площадками РСЯ.

До появления этой таблицы корректной фидовой страницы в BA6 нет: текущий
`ad_analytics.fact_direct_feed_funnel` является compatibility view по площадкам РСЯ/Direct
placement, а не товарным фидам.
