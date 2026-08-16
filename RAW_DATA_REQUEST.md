# Запрос владельцу `raw_data`: что big_analytics_v6 тянет из Яндекс.Директа мимо вашей схемы

_Составлено 2026-08-14 по коду `big_analytics_v6_ch` (ClickHouse
`rc1b-q7j2ie10fdverqrk`, БД `raw_data` и `ad_analytics`)._

## Суть просьбы

BA6 читает сырьё **почти только из `raw_data`** — это правильный контракт, и мы его держим.
Остались потоки, которые ходят за данными мимо вашей схемы, потому что таких данных в `raw_data`
сейчас нет. Просьба: завести их загрузку у себя (в текущие или новые таблицы), после чего мы
выкидываем сторонние вызовы из пайплайна и читаем ваши таблицы.

**Приоритет: сначала поток 4** — без него не собираются 7 таблиц модели Power BI, и перевод
отчётов с v5 на v6 невозможен. Потоки 1–3 — про снижение числа точек отказа, они терпят.

Ничего переделывать в уже существующих таблицах не нужно — всё, что вы грузите сейчас, нас
устраивает и активно используется.

---

## Поток 1 — минус-фразы (снапшот)

**Кто у нас:** `step14_minus_snapshot/step14.py`, шаг 14 пайплайна.
**Куда пишем сейчас:** `ad_analytics.yandex_direct_minus_snapshot` (гранула `date × login ×
campaign_id`, ~1 строка на кампанию в сутки, сейчас 1 546 строк).

**Что забираем из API v5** (OAuth-токен агентства, `https://api.direct.yandex.com/json/v5/`):

| Сервис | `FieldNames` | Зачем |
|---|---|---|
| `agencyclients` | `Login` | список логинов агентства |
| `campaigns` | `Id`, `Name`, `State`, `NegativeKeywords` + `TextCampaignFieldNames: NegativeKeywordSharedSetIds` | минус-фразы на кампании и ссылки на общие наборы |
| `adgroups` | `CampaignId`, `NegativeKeywords` | минус-фразы на группах |
| `negativekeywordsharedsets` | `Id`, `NegativeKeywords` | содержимое общих наборов минус-фраз |

**Что просим завести в `raw_data`** (сырьё как отдаёт API, без нашей агрегации):

```
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

Частота — раз в сутки, семантика снапшота (новый день = новые строки, старые не переписывать):
динамика «добавили/сняли минус-слова» считается именно по разнице снапшотов.

---

## Поток 2 — площадки Директа (PagesReport через Grid)

**Кто у нас:** `direct_placement_links/build.py`, шаг 139 пайплайна (62 секунды в прогоне).
**Куда пишем сейчас:** `ad_analytics.yandex_direct_tp_placement_links` (6 940 строк:
`placement → placement_link`) и `ad_analytics.yandex_direct_tp_placement_link_matches`
(50 777 строк — результат нашего матчинга, это уже наша логика, переносить не нужно).

**Что забираем:**

| Источник | Что именно |
|---|---|
| Внутренний Grid `https://direct.yandex.ru/web-api/grid/api`, **авторизация по кукам директолога** | PagesReport: площадка (`page_group`), ссылка на площадку, расход по кампании/логину — для блоков tp8-tp10 |
| API v5 `clients` (`Login`, `ClientId`) | сопоставление логинов |

**Что просим завести в `raw_data`:**

```
raw_data.direct_pages_report
    date Date, client_login String, manager_login String,
    campaign_id Int64, campaign_name String,
    page_group String,          -- название площадки как в Grid
    placement_link Nullable(String),
    spend Decimal(38, 9), clicks Int64, impressions Int64,
    loaded_at DateTime64(6)
```

⚠️ **Важная оговорка:** этих данных нет в официальном API v5 — только во внутреннем Grid, который
работает на **куках**, а не на OAuth-токене. Если у вас нет инфраструктуры кук — поток честно
остаётся на нашей стороне, тогда просто зафиксируем это как исключение и больше к нему не
возвращаемся.

---

## Поток 3 — отчёт агентских проверок (вне пайплайна)

**Кто у нас:** `yandex_direct_checking_report/report.py` — отдельный сервис, в `pipeline.py` не
входит, гоняется руками/по расписанию. Берёт `POST /json/v5/reports` (CUSTOM_REPORT) и складывает
`domain × account_login × manager_login × month × cost`.

Приоритет низкий: агрегат по месяцу, объём копеечный. Если поток 1 и 2 закроете — этот можно
оставить как есть.

---

## Поток 4 — источники, из-за которых не собираются отчёты Power BI 🔴

**Полная спецификация вынесена в
[`../../docs/DIRECT_RAW_HANDOVER.md`](../../docs/DIRECT_RAW_HANDOVER.md)** — там все 12 потоков,
которые мы тянем из Директа руками, с эндпоинтами, наборами полей, расписанием, объёмами,
предлагаемыми DDL и критерием приёмки. Это документ, который отдаём владельцу `raw_data`;
дублировать его здесь не нужно.

Коротко, почему это приоритет №1: без пяти источников (отчёт по фидам, реестр URL фидов, тексты
объявлений, типы площадок, площадки РСЯ) **7 из 31 таблицы модели Power BI в v6 не соберутся
вообще** — [`PBI_TABLES.md`](PBI_TABLES.md) §0. Потоки 1–3 ниже про снижение числа точек отказа,
поток 4 — про возможность вообще перевести отчёты на v6.

Плюс отзывы (`yandex_direct_reports_reviews` + `yandex_direct_account_reviews`): пока их нет
в `raw_data`, step3 BA6 ходит за ними напрямую в Victory PostgreSQL
(`_fetch_reviews_rows_from_postgres`) — единственное место, где контракт «читаем только
`raw_data`» формально нарушен.

---

## Что мы уже берём из `raw_data` и что менять НЕ надо

`yandex_direct_report_rows` (основной расход, 25.96 млн строк), `direct_campaigns`,
`direct_adgroups`, `yandex_direct_korrektirovki`, `leads_all`, `domains`, `crm_status_mapping`,
`gsheet_sites`, `gsheet_naming`, `gsheet_priezdi_marcar`, `metrika_yandex_counters`,
`metrika_yandex_utm_daily`, `metrika_yandex_goals`, `metrika_yandex_not_found_daily`,
`telega_in_orders`, `perform_leads`, `vk_ads_stats_day`, `vk_ads_agency_clients`, `etl_runs`.

Всё перечисленное работает. Три уточнения по замеру 2026-08-15 (read-only, v5 ↔ v6):

- `domains` — 4 866 строк против 5 188 в PostgreSQL; максимальный `created_at` = 2026-07-21 07:33,
  то есть загрузчик встал почти месяц назад. `domains` участвует в доменных join и атрибуции.
- `gsheet_sites` — 5 051 против 5 103, `_loaded_at` = 2026-08-07.
- `crm_status_mapping` — 791 против 796. Это источник правды по воронке: недостающие 5 строк
  маппинга статусов тихо смещают `korr`/`kval`/`priezd` в отчётах.
- `etl_runs` заполняют только CRM-загрузчики. По `yandex_direct` последняя запись — 2026-07-14
  со статусом `error`, при этом данные доезжают до вчера. Мы не можем использовать `etl_runs`
  как индикатор свежести; если это лечится на вашей стороне — было бы очень полезно.

---

## Формат, который нам удобно читать

Не требование, а то, что снимает лишние преобразования на нашей стороне:

- `ReplicatedMergeTree`, `PARTITION BY toYYYYMM(<дата>)`, в `ORDER BY` первой — дата.
- Колонка `loaded_at DateTime64(6)` в каждой таблице.
- Снапшотная семантика: новый прогон дописывает строки с новым `snapshot_date`, а не переписывает
  историю. Пересчёт задним числом ломает нам сверку «как было / как стало».
- Логины — в нижнем регистре; id — `Int64`; отсутствие значения — `NULL`, а не пустая строка.
- Массивы (`Array(String)`) вместо склейки через разделитель.

## Что это даёт

- Из ночного пайплайна BA6 уходят сетевые вызовы в Директ: меньше точек отказа (таймауты API,
  протухшие куки, лимиты units) в цепочке, от которой зависит Power BI.
- Минус-фразы и площадки становятся доступны не только нам, а всем, кто читает `raw_data`.
- У нас остаётся только логика поверх ваших данных — то, ради чего пайплайн и существует.
