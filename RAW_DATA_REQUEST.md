# Запрос владельцу `raw_data`: что big_analytics_v6 грузит из Директа и Метрики сам

_Составлено 2026-08-14, перепроверено по коду и живым данным 2026-08-17
(ClickHouse `rc1b-q7j2ie10fdverqrk`, БД `raw_data` и `ad_analytics`)._

## Суть просьбы

BA6 читает сырьё почти только из `raw_data` — это правильный контракт, и мы его держим. Своих
сетевых вызовов в Директ и Метрику осталось четыре, но после сверки полей с вашими таблицами
(2026-08-17) картина такая:

- **просим завести один поток** — минус-фразы (§3.1), таких данных в `raw_data` нет;
- **один поток остаётся у нас** — площадки через Grid на куках (§3.2), официальным API не берётся;
- **два потока просить не нужно** — UTM-аудит (§3.3) и отчёт агентских проверок (§3.4) уже
  **полностью покрыты** вашими существующими таблицами и переписаны у нас.

То есть просьба к вам сократилась до одного пункта.

Ничего переделывать в уже существующих таблицах не нужно — всё, что вы грузите сейчас, нас
устраивает и активно используется (§4).

**Полный реестр из 12 потоков Директа, включая те, что живут в BA5, — в
[`../../docs/DIRECT_RAW_HANDOVER.md`](../../docs/DIRECT_RAW_HANDOVER.md).** Здесь то, что тянет
именно BA6, с точными полями и способом доступа.

## §1. Способы доступа — легенда

| Знак | Способ | Авторизация | Можно ли передать |
|---|---|---|---|
| 🔑 | Официальный API v5, `https://api.direct.yandex.com/json/v5/<сервис>` | OAuth-токен агентства | **да**, без оговорок |
| 📊 | API Метрики, `https://api-metrika.yandex.net` | OAuth-токен Метрики, у нас ротация нескольких по квоте | **да**, без оговорок |
| 🍪 | Внутренний Grid, `https://direct.yandex.ru/web-api/grid/api` | **куки директолога**, не токен | только если у вас есть инфраструктура кук |

## §2. Сводка: что грузим сами

| # | Поток | Способ | Где у нас | Как часто сейчас | Надо | Кому |
|---|---|---|---|---|---|---|
| 1 | Минус-фразы (снапшот) | 🔑 | `step14_minus_snapshot/step14.py`, шаг 14 | ✅ в BA6 night cron с 2026-08-17, но источник всё ещё наш OAuth | 1 раз в сутки | 🙏 **просим вас** |
| 2 | Площадки → ссылки (PagesReport) | 🍪 + 🔑 | `direct_placement_links/build.py`, шаг 139 | ежедневно, 9-14 сек | 1 раз в сутки | остаётся у нас (куки) |
| 3 | UTM-аудит объявлений | raw_data Direct | `step_cron_night/step13_utm_direct_audit/run.py`, ночной шаг 102 | ✅ восстановлен 2026-08-17, обновляется BA6 night cron | 1 раз в сутки | сделано у нас |
| 4 | Отчёт агентских проверок | raw_data Direct | `yandex_direct_checking_report/report.py`, вне пайплайна | ✅ переписан 2026-08-17 | 1 раз в месяц | сделано у нас |

Что **больше не наше** (было в BA5, в BA6 уже переведено на `raw_data`, сетевых вызовов нет):
404-ошибки (`step_cron_night/metrika_raw_builders.py::build_404_errors` — считается из
`metrika_yandex_counters` + `metrika_yandex_not_found_daily` + `gsheet_sites`), история изменений
(шаг 9 берёт `raw_data.direct_campaigns`; данные при этом урезаны — 35 823 строки против 77 836 в
BA5), справочник Метрики (`ad_analytics.metrika_yandex` — вьюха над вашими таблицами).

---

## §3. Потоки детально

### Поток 1 — минус-фразы, снапшот (🔑 OAuth v5) · P1

Куда пишем сейчас: `ad_analytics.yandex_direct_minus_snapshot`, гранула `snapshot_date × login ×
campaign_id`.

| Сервис v5 | Точные поля запроса | Зачем |
|---|---|---|
| `agencyclients` | `FieldNames: [Login]` | список логинов агентства |
| `campaigns` | `FieldNames: [Id, Name, State, NegativeKeywords]`, `TextCampaignFieldNames: [NegativeKeywordSharedSetIds]` | минус-фразы кампании и ссылки на общие наборы |
| `adgroups` | `FieldNames: [CampaignId, NegativeKeywords]` | минус-фразы групп |
| `negativekeywordsharedsets` | `FieldNames: [Id, NegativeKeywords]` | содержимое общих наборов |

Просим завести (сырьё как отдаёт API, без нашей агрегации):

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

Частота — **раз в сутки, семантика снапшота**: новый день = новые строки, старые не переписывать.
Динамика «добавили / сняли минус-слова» считается именно по разнице снапшотов, пересчёт задним
числом её убивает.

### Поток 2 — площадки → ссылки, PagesReport (🍪 Grid + 🔑 v5) · P1

Куда пишем сейчас: `ad_analytics.yandex_direct_tp_placement_links` (5 987 строк:
`placement → placement_link`) и `ad_analytics.yandex_direct_tp_placement_link_matches` (50 971 —
результат нашего матчинга, это наша логика, переносить не нужно).

| Источник | Точные поля запроса |
|---|---|
| 🍪 Grid `POST /web-api/grid/api`, GraphQL `cubeQueryReport` | `dimensions: [Targettype, PageGroup, Campaign]`; `attributes: [Targettype, PageGroup, PageGroupHomePage, Campaign, CampName]`; `measures: [Sum]`; фильтр — блоки tp8-tp10, постранично по 5 000 |
| 🍪 `https://direct.yandex.ru/dna/statistics/direct/reports/` | тот же отчёт как fallback-путь выгрузки |
| 🔑 `clients` | `FieldNames: [Login, ClientId]` — сопоставление логинов с операторами |

Просим завести:

```sql
raw_data.direct_pages_report
    date Date, client_login String, manager_login String,
    campaign_id Int64, campaign_name String,
    page_group String,            -- название площадки как в Grid
    page_group_home_page Nullable(String),   -- ссылка на площадку
    spend Decimal(38, 9), clicks Int64, impressions Int64,
    loaded_at DateTime64(6)
```

⚠️ **Оговорка:** в официальном API v5 этих данных нет — только внутренний Grid на **куках**. Если
инфраструктуры кук у вас нет, поток честно остаётся у нас; зафиксируем как исключение и больше к
нему не возвращаемся. Ключ соединения — строка названия площадки, поэтому важно: **отдавать
`page_group` как есть, в UTF-8.** У себя мы уже ловим на этом баг — часть значений в
`yandex_direct_report_rows.placement` приходит в двойной кодировке (UTF-8, прочитанный как
ISO-8859-1), см. §5.

### Поток 3 — UTM-аудит объявлений (raw_data Direct) · ✅ сделано 2026-08-17

Куда пишем сейчас: `ad_analytics.check_utm` и `ad_analytics.check_utm_fuck_direct` — сверка UTM в
tracking-параметрах групп Директа. До 2026-08-17 обе витрины были пустыми заглушками
`SELECT CAST(NULL, …)`, в BA5 в них было 35 663 и 1 828 строк.

| Источник | Точные поля запроса |
|---|---|
| 📊 `GET /management/v1/counters` | `per_page=1000` постранично — список счётчиков; плюс точечный поиск `q=<домен>`, `per_page=50` |
| 📊 `GET /stat/v1/data` | `metrics: ym:s:visits`; `dimensions: ym:s:date, ym:s:UTMSource, ym:s:UTMMedium, ym:s:UTMCampaign, ym:s:UTMContent, ym:s:UTMTerm` |
| 🔑 `campaigns` | `FieldNames: [Id, Type]`, `TextCampaignFieldNames: [CounterIds]` — привязка кампании к счётчику; отдельным запросом `FieldNames: [Id, Name]` |
| 🔑 `ads` | `FieldNames: [Id]`, `TextAdFieldNames: [Href]`, `DynamicTextAdFieldNames: [Href]` — ссылки объявлений |
| 🔑 `adgroups` | `FieldNames: [Id, Name, CampaignId, TrackingParams, Status]` |

**Заводить ничего не нужно — сверка полей 2026-08-17 показала полное покрытие:**

| Что берём из API | Уже есть в `raw_data` | Покрытие |
|---|---|---|
| 📊 `/management/v1/counters` → список счётчиков, домен | `metrika_yandex_counters`: `counter_id`, `domain`, `name`, `site`, `status`, `type` | ✅ полное, плюс уже проставлены `campaign_id` / `ad_group_id` |
| 📊 `/stat/v1/data` → визиты по UTM | `metrika_yandex_utm_daily`: `day`, `counter_id`, `utm_source`, `utm_medium`, `utm_campaign`, `utm_content`, `utm_term`, `visits` | ✅ ровно тот же разрез, 7.44 млн строк |
| 🔑 `campaigns` → `CounterIds`, `Type` | `direct_campaigns`: `counter_ids`, `type`, `campaign_name`, `tracking_params` | ✅ полное |
| 🔑 `ads` → `Href` | `direct_ads`: `href`, `tracking_params`, `status`, `state`, `type` (381 496 строк) | ✅ полное |
| 🔑 `adgroups` → `TrackingParams`, `Status` | `direct_adgroups`: `tracking_params`, `status`, `group_name` | ✅ полное |

Итог 2026-08-17: ночной шаг 102 переписан на `raw_data.direct_adgroups`,
`raw_data.direct_campaigns`, `raw_data.gsheet_sites` и `raw_data.yandex_direct_report_rows`.
Одна строка `check_utm` теперь соответствует группе Директа с расходом за последние 30 дней, а не
UTM-визиту Метрики. Живой прогон дал `check_utm` = 28 288 строк
(`OK` 27 930, `ДРУГОЙ_UTM` 204, `НЕТ_UTM` 154) и `check_utm_fuck_direct` = 3 981 строка за
2026-05-19..2026-08-16.

После этого из BA6 ушли обращения этого аудита к API Метрики и ротация её токенов
(`config/tokens.py::METRIKA_TOKEN_AUDIT` — отдельная квота держалась именно под этот аудит).

### Поток 4 — отчёт агентских проверок (raw_data Direct) · ✅ сделано 2026-08-17

`yandex_direct_checking_report/report.py`, в `pipeline.py` не входит.
До 2026-08-17 ходил в `POST /json/v5/reports`, `ReportType: CAMPAIGN_PERFORMANCE_REPORT`,
`FieldNames: [Month, Cost]`, `DateRangeType: CUSTOM_DATE`. Гранула результата —
`domain × account_login × manager_login × month × cost`.

Заводить ничего не нужно: нужный расход по месяцам уже лежит в
`raw_data.yandex_direct_report_rows.total_cost` (`day`, `client_login`, `manager_login`, `domain`,
`total_cost` — 26.3 млн строк). Семён подтвердил: `total_cost` = расход с НДС и комиссией, то есть
именно та сумма, которая нужна отчёту. Живой прогон 2026-08-17 создал
`public.yandex_direct_checking_report`: 865 строк, 261 аккаунт, 2026-01..2026-08,
651 319 874.51 ₽. Обращение к `POST /json/v5/reports` больше не нужно.

---

## §4. Что мы уже берём из `raw_data` — менять НЕ надо

Проверено 2026-08-17: всё перечисленное писалось в этот же день, загрузчики живые.

| Область | Таблицы |
|---|---|
| Директ | `yandex_direct_report_rows` (26.3 млн — основной расход), `yandex_direct_report_raw`, `direct_campaigns`, `direct_adgroups`, `direct_ads`, `yandex_direct_korrektirovki` (190 473), `yandex_direct_goal_conversions` (1.72 млн) |
| Метрика | `metrika_yandex_counters` (402 181), `metrika_yandex_goals` (29 059), `metrika_yandex_utm_daily` (7.44 млн), `metrika_yandex_not_found_daily` (13 953) |
| CRM и лиды | `leads_all` (1.9 млн), `perform_leads`, `crm_status_mapping`, `telega_in_orders` |
| Google Sheets | `gsheet_sites`, `gsheet_naming`, `gsheet_priezdi_marcar`, `gsheet_plan_fakt`, `gsheet_reestr`, `gsheet_autosalony_clients`, `gsheet_vse_klienty` |
| VK Ads | `vk_ads_stats_day` (2.12 млн), `vk_ads_goal_conversions` (3.56 млн) и 9 справочников |
| Прочее | `domains`, `salon_regions`, `data_sources`, `raw_rows`, `etl_runs` |

## §5. Просьбы по тому, что вы уже грузите

1. **`yandex_direct_report_rows.placement` — двойная кодировка.** 7 522 строки / 847 площадок /
   16 логинов за январь-июль 2026 приходят как UTF-8, прочитанный ISO-8859-1: «FMCG Инсайдер» →
   «FMCG Ð˜Ð½ÑÐ°Ð¹Ð´ÐµÑ€». Проверяется round-trip'ом: `convertCharset(s,'UTF-8','ISO-8859-1')`
   даёт валидный UTF-8, который кодируется обратно в исходное значение. У себя мы это чиним на
   чтении (`config/ch_utils.py::fix_mojibake_sql`), но правильное место — загрузчик. В августе новых
   случаев нет, последние — 30-31 июля, то есть баг плавающий, а не разовый.
2. **Отстающие загрузчики** (замер 2026-08-17): `vk_ads_leads`, `vk_ads_agency_clients`,
   `vk_ads_lead_forms` — последняя запись 23.07; `crm_status_mapping` — 04.08 (это источник правды
   по воронке, недостающие строки маппинга тихо смещают `korr`/`kval`/`priezd`); `perform_leads` —
   07.08; `reconciliation_results` — 14.07.
3. **`etl_runs` нельзя использовать как индикатор свежести.** Его заполняют только CRM-загрузчики;
   по `yandex_direct` последняя запись — 14.07 со статусом `error`, при этом данные доезжают до
   вчера. Если это лечится на вашей стороне — было бы очень полезно.
4. **Отзывы.** `yandex_direct_reports_reviews` (6 284) в `raw_data` нет, поэтому шаг 3 BA6 ходит за
   ними напрямую в Victory PostgreSQL (`_fetch_reviews_rows_from_postgres`) — единственное место,
   где контракт «читаем только `raw_data`» формально нарушен.

## §6. Формат, который снимает с нас преобразования

- `ReplicatedMergeTree`, `PARTITION BY toYYYYMM(<дата>)`, в `ORDER BY` первой — дата.
- Колонка `loaded_at DateTime64(6)` в каждой таблице.
- Снапшотная семантика: новый прогон дописывает строки с новым `snapshot_date`, а не переписывает
  историю. Пересчёт задним числом ломает сверку «как было / как стало».
- Логины — в нижнем регистре; id — `Int64`; отсутствие значения — `NULL`, а не пустая строка.
- Массивы (`Array(String)`) вместо склейки через разделитель.
- Текст — как отдал источник, в UTF-8, без промежуточного перекодирования (§5.1).

## §7. Что это даёт и что делаем мы

**От вас нужен один поток** — минус-фразы (§3.1). После него в BA6 остаётся ровно один сетевой
вызов в Яндекс: Grid PagesReport на куках (§3.2), который официальным API не воспроизводится.

**Наша часть работы, вам делать ничего не надо:**

1. ✅ 2026-08-17: переписать ночной шаг 102 (UTM-аудит) на `raw_data` — покрытие полное (§3.3).
   Обращения к API Метрики и ротация её токенов для этого аудита убраны.
2. ✅ 2026-08-17: переписать отчёт агентских проверок на `yandex_direct_report_rows.total_cost`
   (§3.4). OAuth Reports API больше не используется.
3. ✅ 2026-08-17: поставить ночной контур BA6 в крон Victory. Стоит на `10 18 * * *` UTC
   (23:10 Екб) с `/tmp/ba6_night.lock`; полный ручной прогон перед постановкой прошёл PASS за
   14м15с (`DB_AD_ANALYTICS.md` §6.8).
4. Снять фикс двойной кодировки площадок (`config/ch_utils.py::fix_mojibake_sql`) — когда §5.1
   будет починен на вашей стороне.
