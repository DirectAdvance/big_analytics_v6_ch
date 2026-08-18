# RAW_DATA_LOAD_GAPS_2026-08-18 — что ещё нужно грузить для BA6

Проверено на живом ClickHouse 2026-08-18: `raw_data` и `ad_analytics` на
`rc1b-q7j2ie10fdverqrk`. Цель файла — не просить лишнее и не плодить новые временные
`raw_new_*` в `ad_analytics`.

## Короткий вывод

`raw_data.direct_cookie_*` уже догрузились и закрывают две старые временные копии:
`raw_new_ads_texts_master_pbi` и `raw_new_type_placement_report_master`. 18.08 в BA6 добавлены
совместимые `bi_yandex_direct_ads_texts` и
`bi_yandex_direct_type_placement_report_master` поверх этих таблиц. Новые `raw_new_*` просить не
нужно.

Что всё ещё нужно от загрузчиков `raw_data`:

| Приоритет | Поток | Зачем BA6 | Статус сейчас |
|---|---|---|---|
| P0 | Фидовая воронка Директа | страница PBI `fact_direct_feed_funnel` как в BA5 | есть только `direct_cookie_feed_urls`, отчёта по фидам нет |
| P0 | Search-query PBI aggregate | не импортировать 40M строк в Power BI | есть `ad_analytics.yd_search_query_report_master`, но нет объекта в `raw_data` |
| P0 | ARP / placements с CRM-воронкой | страницы PBI `analytics_report_placement*` как в BA5 | есть spend/Direct goals в `yandex_direct_report_rows`, но нет v5 CRM-воронки по placement |
| P1 | Минус-фразы снапшотом | убрать сетевой OAuth-вызов из night BA6 | BA6 собирает сам, истории пока мало |
| P1 | Accounts human/cyborgs | маленький справочник PBI | временная копия есть только как `ad_analytics.raw_new_human_cyborgs` |
| P1 | Отзывы Direct | убрать последний PostgreSQL-read из step3 | в `raw_data` нет `yandex_direct_reports_reviews` |
| P2 | PagesReport placement links | убрать cookie/Grid-вызов из BA6 | BA6 строит сам; нужно только если у `raw_data` есть cookie-инфраструктура |

## Что уже есть и не нужно просить повторно

| Таблица `raw_data` | Живой объём 2026-08-18 | Что заменяет / покрывает |
|---|---:|---|
| `direct_cookie_ads_texts_master` | 65 241 324 строк, 2026-01-01..2026-08-17 | используется в `bi_yandex_direct_ads_texts`; старый `raw_new_ads_texts_master_pbi` был 5 106 097 строк до 2026-08-03 |
| `direct_cookie_ads_texts_master_current` | 65 241 324 строк, 2026-01-01..2026-08-17 | current-вьюха над тем же набором |
| `direct_cookie_type_placement_master` | 8 398 376 строк, 2026-01-01..2026-08-17 | используется в `bi_yandex_direct_type_placement_report_master`; старый `raw_new_type_placement_report_master` был 7 539 230 строк до 2026-08-03 |
| `direct_cookie_type_placement_master_current` | 8 397 852 строки, 2026-01-01..2026-08-17 | current-вьюха; 12 типов размещения |
| `direct_cookie_feed_urls` | 5 923 строки, 5 874 feed_id, 300 клиентов | справочник URL фидов; не заменяет сам отчёт по фидам |
| `yandex_direct_report_rows` | 26 416 882 строки, 2026-01-01..2026-08-17 | основной Direct spend/clicks/impressions/placement/criterion/goals |
| `domains`, `gsheet_*` | обновлены 2026-08-18 | прежний пункт про отставание `domains/gsheet_sites` уже не главный блокер |

Важная разница по `direct_cookie_*`: в новых таблицах одна метрика `goals`, а в старом PBI-контракте
были две: `goal_all_forms` и `goal_crm_order_paid`. Это не просьба к загрузчику, а решение на стороне
BA6/PBI-маппинга.

## P0. Фидовая воронка Директа

Нужно, если страница PBI `fact_direct_feed_funnel` должна остаться как в BA5: фид × кампания ×
группа × дата с расходом и воронкой. Сейчас `ad_analytics.fact_direct_feed_funnel` в BA6 — это
агрегат по площадкам РСЯ из `yandex_direct_report_rows.placement`, а не товарные фиды.

Уже есть:

```sql
raw_data.direct_cookie_feed_urls
    client_login, manager_login, feed_id, feed_name, feed_url, feed_url_key,
    feed_type, update_status, feed_source, offers_count, listings_count,
    last_change, campaign_ids, loaded_at
```

Не хватает расходного отчёта по фидам. Просьба завести один из вариантов:

```sql
raw_data.direct_feed_report_rows
    scope_from Date,
    scope_to Date,
    manager_login String,
    client_login String,
    campaign_id Int64,
    campaign_name Nullable(String),
    adgroup_id Nullable(Int64),
    adgroup_name Nullable(String),
    feed_id Int64,
    feed_name Nullable(String),
    feed_url Nullable(String),
    shows Int64,
    clicks Int64,
    cost Decimal(38, 9),
    goals Decimal(38, 9),
    loaded_at DateTime64(6)
```

Если API не отдаёт фидовый отчёт с целями, достаточно расходной части. CRM-воронку BA6 сможет
достроить сам, если `feed_id/feed_url_key` стабилен и совпадает со справочником feed URLs.

## P0. Search-query aggregate для Power BI

Сейчас есть:

- `ad_analytics.yd_search_query_report_master` — 40M+ строк сырья/мастера, слишком тяжело для PBI.
- `ad_analytics.raw_new_search_query_report_master_pbi` — временная копия BA5, 328 658 строк,
  2026-01-01..2026-08-03.

В `raw_data` нет ни `yandex_direct_search_query_report_master_pbi`, ни аналога. Просьба грузить
готовый PBI-агрегат в `raw_data`, чтобы BA6 не держал временную `raw_new_*` копию:

```sql
raw_data.yandex_direct_search_query_report_master_pbi
    loaded_at DateTime64(6),
    date_from Date,
    date_to Date,
    client_login String,
    query String,
    criterion String,
    criterion_type String,
    targeting_category String,
    brand_options String,
    campaign_id Int64,
    ad_group_id Int64,
    impressions Int64,
    clicks Int64,
    cost Decimal(38, 9),
    goal_all_forms Int64,
    goal_crm_order_paid Int64
```

Если раздельных `goal_all_forms` / `goal_crm_order_paid` нет в источнике, грузить одну `goals`
колонку и явно подписать семантику. Тогда BA6 сделает совместимую PBI-вьюху с понятным fallback.

## P0. ARP / placements с CRM-воронкой

Сейчас `raw_data.yandex_direct_report_rows` уже содержит spend-разрез по placement:
`day`, `client_login`, `campaign_id`, `ad_group_id`, `placement`, `ad_network_type`, `criterion`,
`location_of_presence_id`, `ad_format`, `domain`, `total_cost`, `clicks`, `impressions`,
`all_forms`, `crm_order_created`, `crm_order_paid`.

Этого достаточно для расхода и Direct goals, но недостаточно для v5-совместимой CRM-воронки
`korr/kval/priezd/prodazhi` по placement. Поэтому PBI-страницы `analytics_report_placement` и
`analytics_report_placement_links` остаются блокером, если их нужно сохранить 1-в-1.

Варианты, любой один:

1. Грузить готовый `raw_data.analytics_report_placement_pbi` в v5-совместимой схеме
   `raw_new_arp_fact`: `Date`, `domain`, `CampaignId`, `AdGroupId`, `placement`, `ad_network_type`,
   `cost`, `clicks`, `kol_vo_zayavok`, `korr`, `kval`, `priezd`, `prodazhi`, `nekorr`,
   `ne_otvechaet`, `nedozvon`, `filtr`, `priedet`, `dohod_do_kredita`, `dobro`,
   `Все формы`, `CRM: Заказ создан`, `CRM: Заказ оплачен`, `CRM: Спам заказ`,
   `CRM: Заказ отменен`, `логин`, `салон`, `тип_сайта`, `Специалист`, `tp`, `updated_at`.
2. Или грузить сырой `raw_data.yandex_direct_report_placement` плюс все ключи, по которым BA6
   сможет воспроизвести v5 join к лидам и статусам. Без ключа к CRM-лиду BA6 восстановит только
   расход и Direct goals, но не CRM-воронку.

## P1. Минус-фразы снапшотом

BA6 уже собирает `ad_analytics.yandex_direct_minus_snapshot` ночным cron, но это сетевой вызов в
Direct API из проекта BA6. Чтобы убрать его, нужны снапшотные таблицы в `raw_data`:

```sql
raw_data.direct_campaign_negative_keywords
    snapshot_date Date, client_login String, campaign_id Int64,
    campaign_name String, state String,
    negative_keywords Array(String), negative_keyword_shared_set_ids Array(Int64),
    loaded_at DateTime64(6)

raw_data.direct_adgroup_negative_keywords
    snapshot_date Date, client_login String, campaign_id Int64, ad_group_id Int64,
    negative_keywords Array(String), loaded_at DateTime64(6)

raw_data.direct_negative_keyword_sets
    snapshot_date Date, client_login String, set_id Int64,
    negative_keywords Array(String), loaded_at DateTime64(6)
```

Семантика важна: новый день дописывает новый снапшот, историю не перетирать. Дельта считается по
разнице дней.

## P1. Accounts human/cyborgs

В `raw_data` нет справочника `direct_accounts_human_cyborgs`. Сейчас есть только временная копия:
`ad_analytics.raw_new_human_cyborgs`, 17 строк.

Просьба завести:

```sql
raw_data.direct_accounts_human_cyborgs
    account_login String,
    human_cyborg String,
    loaded_at DateTime64(6)
```

Если исходное имя колонок нужно сохранить из BA5: `аккаунты`, `humancyborgs`.

## P1. Отзывы Direct

Последняя живая PostgreSQL-зависимость BA6: step3 читает
`yandex_direct_raw.yandex_direct_reports_reviews` с Victory PG. В `raw_data` таблицы
`yandex_direct_reports_reviews` нет.

Просьба завести:

```sql
raw_data.yandex_direct_reports_reviews
    -- минимум: те же колонки, что в Victory PG yandex_direct_raw.yandex_direct_reports_reviews
    loaded_at DateTime64(6)
```

Точный список колонок нужно снять с Victory PG перед реализацией. Для заявки сейчас важен сам факт:
без этого BA6 не будет полностью отвязан от PostgreSQL.

## P2. PagesReport placement links

BA6 сам строит `ad_analytics.yandex_direct_tp_placement_links`. Это cookie/Grid-поток, не
официальный Direct API. Просить `raw_data` имеет смысл только если у загрузчиков есть
cookie-инфраструктура.

Желаемый формат:

```sql
raw_data.direct_pages_report
    date Date,
    client_login String,
    manager_login String,
    campaign_id Int64,
    campaign_name String,
    page_group String,
    page_group_home_page Nullable(String),
    spend Decimal(38, 9),
    clicks Int64,
    impressions Int64,
    loaded_at DateTime64(6)
```

Если cookie-инфраструктуры нет, поток остаётся в BA6 как осознанное исключение.

## Что не просить у `raw_data`

- Новые `raw_new_*` в `ad_analytics`: это временные копии BA5, их надо удалять по мере замены.
- Повторную загрузку `direct_cookie_ads_texts_master` и `direct_cookie_type_placement_master`:
  они уже есть, свежее и больше старых копий.
- Отдельную загрузку `domains` / `gsheet_sites` как срочный блокер: 2026-08-18 они обновлены.
- `UTM`-аудит и агентский checking report: BA6 уже переписан на существующий `raw_data`.
