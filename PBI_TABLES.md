# PBI_TABLES.md — Power BI source tables

> 📍 **Читать §0 — он актуальный. Всё, что ниже §0, — legacy-справочник v5/PostgreSQL.**

---

# §0. Паритет PBI v5 ↔ v6_ch — замер 2026-08-20, обновлено 2026-08-27

Вопрос, на который отвечает раздел: **хватит ли данных v6, чтобы собрать те же отчёты Power BI,
что живут на v5.** Метод: живая модель
`~/Documents/креативы виктори/Большая аналитика_admin/Большая аналитика_v00.SemanticModel`
(31 таблица с источником + 2 DAX-таблицы `Модель атрибуции` / `Dim_Distance`; служебная
`Users` удалена из текущего PBIP 2026-08-24),
для каждой — источник в v5 и объект в ClickHouse `ad_analytics`, построчная сверка колонок.

## §0.1 Короткий ответ

**Рабочее BI-ядро BA6 перенесено.** Главная витрина, star-измерения, spend-витрины по
регионам/форматам/критериям, фиды, РСЯ-площадки, корректировки, 404, cookie-страницы и поисковые
запросы есть в ClickHouse и по принятым гейтам сходятся с v5. Это не плоская копия v5: модель
читает `bi_*`/`*_star`, BI ограничен нишей `Авто`, а Service покажет новые данные только после
Power BI refresh.
После перевязки PBIP на `*_star` и direct-cookie/feed `bi_*` остаются такие пробелы:

| # | Таблица модели | Причина |
|---|---|---|
| 1 | `analytics_report_criterion` | legacy `arc_fact` удалён из активного контракта v6 (`tests/test_pbi_contract_lists.py`) |
| 2 | `analytics_report_feed` | legacy `arf_fact` удалён; активная фидовая страница читает `fact_direct_feed_funnel` |
| 3 | `yandex_direct_accounts_human_cyborgs` | справочника нет в `raw_data`; PBIP пока читает `raw_new_human_cyborgs` |
| — | `Dim_Distance` (DAX) | считается из `distance_km_agreg`; с 2026-08-24 колонка физическая в `fact_region_spend` (шаг 141, джойн к порту `ad_analytics.gsheet_yandex_direct_id_location` — `migrations/04_port_geo_location_dict_2026-08-24.py`), в `bi_*`-вьюхах это проекция поверх неё, а не единственный источник; в `fact_region_zayavki` колонки по-прежнему нет |

27.08 после появления `raw_data.direct_feed_report_rows` step144 строит реальные фиды:
`fact_direct_feed_funnel_light` = 1 226 350 строк, `bi_Dim_PlacementFeed` = 51 строка. PBIP
таблица `fact_direct_feed_funnel` читает `bi_fact_direct_feed_funnel_star`. РСЯ-площадки отделены:
`bi_analytics_report_placement` снова строится из `raw_data.yandex_direct_report_rows`, поэтому
фидовая таблица и отчёт площадок больше не подменяют друг друга.

18.08 `yandex_direct_ads_texts` и `yandex_direct_type_placement_report_master` переведены в PBIP
на `bi_yandex_direct_ads_texts` и `bi_yandex_direct_type_placement_report_master`. Обе view читают
новые `raw_data.direct_cookie_*` и агрегируют в ClickHouse то, что раньше группировал Power Query.
Временные `raw_new_ads_texts_master_pbi`, `raw_new_type_placement_report_master` и
`raw_new_type_placement_types` больше не читаются активным PBIP. `goal_crm_order_paid` пока
заполняется нулём: в новых cookie-таблицах есть только общий `goals`.

Оставшийся прямой `raw_new` в BA6 PBIP на 27.08: `raw_new_human_cyborgs`.

19.08 локальный код PBI hidden keys переведён: `bi_fact_criterion_spend_star`,
`bi_Dim_Criterion` / `bi_dim_criterion`, `bi_Dim_Site`, `bi_fact_region_spend_star` и
`bi_fact_direct_feed_funnel_star` отдают `criterion_key` / `site_key` как signed-safe `Int64`
через `reinterpretAsInt64(UInt64)`, без modulo-коллизий. До деплоя и пересоздания live `bi_*`
Victory продолжает работать по уже развернутым view. Старый вариант через
`% 9223372036854775807` давал дубль `criterion_key=3943490909` в `dim_criterion` на стороне
Power BI.

Пустые `bi_*`-объекты больше не разрешены. `check_utm_fuck_direct` восстановлен из `raw_data`,
а `yandex_direct_return_commission_report` исключён из активного PBI-контракта BA6 (#40).

## §0.2 Матрица 31 таблицы

Легенда: ✅ есть и полно · ⚠️ есть, но урезано/переименовано · ❌ нет.

| Таблица PBI | Источник v5 | v5 строк | Объект v6 `ad_analytics` | v6 строк | |
|---|---|---:|---|---:|:-:|
| `big_analytics_full` | `public.pbi_big_analytics_full` | 5 019 702 | `pbi_big_analytics_full` | 5 391 879 | ⚠️ |
| `Dim_AdGroup` | `public.Dim_AdGroup` | 229 264 | `Dim_AdGroup` | 603 116 | ✅ |
| `Dim_Campaign` | `public.Dim_Campaign` | 38 413 | `Dim_Campaign` | 24 445 | ⚠️ |
| `Dim_City_Tier` | `public.Dim_City_Tier` | — | `Dim_City_Tier` | 182 | ✅ |
| `Dim_Date` | `public.Dim_Date` | 226 | `Dim_Date` | 227 | ✅ |
| `Dim_Location` | `public.Dim_Location` | 16 200 | `Dim_Location` | 16 317 | ✅ |
| `Dim_Site` | `public.Dim_Site` | 5 023 | `Dim_Site` | 5 032 | ⚠️ |
| `analytics_report_criterion` | `public.arc_fact` | 151 288 | — | — | ❌ |
| `analytics_report_feed` | `public.arf_fact` | 91 898 | — | — | ❌ legacy |
| `analytics_report_placement` | `public.arp_fact` | 1 927 669 | `bi_analytics_report_placement` | 13 263 521 | ⚠️ |
| `analytics_report_placement_links` | `arp_fact` + `yandex_direct_tp_placement_links` | 5 093 | PBIP: `bi_analytics_report_placement` + `bi_yandex_direct_tp_placement_links` | живой join | ⚠️ |
| `check_utm_fuck_direct` | `public.check_utm_fuck_direct` | 1 828 | `check_utm_fuck_direct` | **3 981** | ✅ |
| `dim_criterion` | `public.dim_criterion` | 86 076 | `dim_criterion` | 94 217 | ✅ |
| `direct_history` | `yandex_direct_raw.yandex_direct_history` | 77 836 | `yandex_direct_history` | 35 823 | ⚠️ |
| `fact_adformat_spend` | `public.fact_adformat_spend_light` | 3 018 471 | `fact_adformat_spend` | 3 104 439 | ⚠️ |
| `fact_criterion_spend` | `public.fact_criterion_spend_light` | 4 837 544 | `fact_criterion_spend` | 4 977 987 | ⚠️ |
| `fact_criterion_zayavki` | `public.fact_criterion_zayavki` | 137 602 | `fact_criterion_zayavki` | 137 890 | ✅ |
| `fact_direct_feed_funnel` | `public.fact_direct_feed_funnel` | 92 016 | `bi_fact_direct_feed_funnel_star` | 1 226 350 | ⚠️ |
| `fact_ml_korrektirovki` | `public.fact_ml_korrektirovki` | 11 674 | `fact_ml_korrektirovki` | 15 396 | ✅ |
| `fact_region_spend` | `public.fact_region_spend_light` | 13 989 880 | `fact_region_spend` | 14 175 006 | ⚠️ |
| `fact_region_zayavki` | `public.fact_region_zayavki` | 188 432 | `fact_region_zayavki` | 188 691 | ⚠️ |
| `fact_vk_ads` | `public.fact_vk_ads` | 705 | `fact_vk_ads` | 783 | ⚠️ |
| `v_yandex_direct_minus_delta` | `yandex_direct_raw.v_yandex_direct_minus_delta` | 32 831 | `v_yandex_direct_minus_delta` | 1 546 | ⚠️ |
| `yandex_direct_404_errors` | `yandex_direct_raw.yandex_direct_404_errors` | 11 780 | `yandex_direct_404_errors` | 13 548 | ✅ |
| `yandex_direct_accounts_human_cyborgs` | `victoryads_direct_automation.…` | 17 | — | — | ❌ |
| `yandex_direct_ads_texts` | `yandex_direct_raw.…_ads_texts_master_light` | 5 106 097 | `bi_yandex_direct_ads_texts` | 16 162 458 | ⚠️ |
| `yandex_direct_cookie_analytics_website_pages` | `yandex_direct_raw.…` | 1 011 518 | `yandex_direct_cookie_analytics_website_pages` | 965 764 | ✅ |
| `yandex_direct_korrektirovki` | `yandex_direct_raw.…` | 43 603 | `yandex_direct_korrektirovki` | 190 286 | ✅ |
| `yandex_direct_minus_snapshot` | `yandex_direct_raw.…` | 32 831 | `yandex_direct_minus_snapshot` | 1 546 | ⚠️ |
| `yandex_direct_search_query_report_master` | `yandex_direct_raw.…_master_pbi` | 328 658 | `yd_search_query_report_master` | 40 136 496 | ⚠️ |
| `yandex_direct_type_placement_report_master` | `yandex_direct_raw.…_master_light` | 7 539 230 | `bi_yandex_direct_type_placement_report_master` | 8 453 279 | ⚠️ |

## §0.3 Что означает каждая ⚠️

**`big_analytics_full`** — v6 `pbi_big_analytics_full` отдаёт 43 колонки против 68 в v5. Это не
потеря данных, а **звезда**: `AdNetworkType`/`Device`/`источник`/`manager_login` заменены на
`*_key`, `city_tier_key`, а текстовые атрибуты (`CampaignName`, `AdGroupName`, `campaign_code`, `tp`, `cpc_cpa`,
`site_quiz`, `марки авто`, `ag_part1…7`, `campaign_status`, `payment_model`, `поставщик`,
`неверный_кодер_new`, `week_start`, `День недели`, конкатенации «номер | название») переехали в
`Dim_Campaign` / `Dim_AdGroup` / `Dim_Date` / `Dim_Source` / `Dim_Adjustment`. Все они проверены —
существуют в v6. **Модель PBI нужно перевязать на звезду; на плоскую таблицу она больше не сядет.**
С 2026-08-27 `pbi_big_analytics_full` намеренно отдаёт только ось
`атрибуция = 'По дате заявки'`: если сложить её с `По дате визита`, продажи задваиваются в PBI и
месячный CPL продажи искусственно падает.

С 2026-08-27 `pbi_big_analytics_full` сохраняет BA5-семантику поля `специалист`: fallback-метки
`Контекст`/`SEO`/`Посевы`/`Звонки` и исторические владельцы не обнуляются SQL-слоем, иначе Power BI
получает один большой пустой бакет с расходом и продажами. Страницы, где ось именно
специалист/директолог, чистятся whitelist-фильтрами в PBIP по `big_analytics_full.специалист` и
`Dim_Site.специалист`. Golden gate блокирует пустой PBI-бакет (`bi_contract_pbi_blank_specialist`)
и продажи на служебных специалистах (`sales_without_real_specialist`) отдельно.

Ровно две колонки v5 отсутствуют в v6 где бы то ни было:
`домен для зоны` и `id группы | логин | id кампании`.

**`Dim_Campaign`** — нет `статус_кампании` (есть только в `bi_Dim_Campaign`), `специалист`,
`manager_login`; последние два живут в `Dim_Salon` / `Dim_ManagerLogin`.
**`Dim_City_Tier`** — live-измерение для PBI-среза `тир_месяца`: `city_tier_key`, `город`,
`тир_месяца`, `тир_месяца_backfill`, `тир_текущий`. Строится из `ad_analytics.gsheet_city_tier`.
**Правило по нишам для BA6 Power BI** — в BI рассматривается только `niche='Авто'`. Не-авто ниши
(`Другое`, `Недвижимость`, `Медицина`, `Строительство` и т.д.) не добавляем в BI-измерения,
страницы и срезы; расход/строки таких ниш не являются дефектом доменной модели BA6.
**`Dim_Site`** — атрибуты переименованы с английского на русский (`city`→`город`, `salon`→`салон`,
`directologist`→`специалист` и т.д.); реально нет только `client_id` и `niche`. Справочник
`reference_data.gsheet_sites` используется для PBI только по `niche='Авто'`: остальные ниши не
должны попадать в `Dim_Site`, `bi_Dim_Campaign` и `bi_analytics_report_placement`.
**`Dim_AdGroup`** — нет `ag_part1_name` в физической таблице, есть в `bi_Dim_AdGroup`.

**`fact_region_spend` / `fact_adformat_spend` / `fact_criterion_spend`** — физические таблицы
несут `account_login`, `site_key` и собственный датно-корректный `специалист`: он считается через
`gsheet_sites` по `(login,date)` и затем через `specialist_correction_expr`. В физических таблицах
нет `network_key`, `updated_at` и части текстовых дублей; PBI читает `bi_fact_*` / `*_star`, где
`специалист` прокинут из факта, а не берётся из бездатного `Dim_Site`.
`fact_region_zayavki` дополнительно потерял `location`, `Область`, `GeoRegionType` (ушли в `Dim_Location`).

**`fact_direct_feed_funnel`** — имя сохранено, но реализация v6 ClickHouse другая. Step144 читает
`raw_data.direct_feed_report_rows`, обогащает `feed_name/feed_url/feed_url_key` из
`raw_data.direct_cookie_feed_urls` и исключает посевные `tp8/tp9/tp10`. В физическом факте есть
расход/клики/показы и CRM-цели (`all_forms`, `crm_order_created`, `crm_order_paid`); PBI-compat
добавляет legacy-колонки воронки как прямые проекции этих целей, поэтому это рабочий feed-слой,
но не полная копия старого BA5 алгоритма фидовой атрибуции.

**`direct_history`** — 35 823 строки против 77 836 и 12 колонок против 19 (нет `ulogin`,
`user_login`, `user_uid`, `category`, `ad_group_id`, `ad_group_name`, `raw_event`, `loaded_at`).
v6 строит историю из `reference_data.direct_campaigns`, а не из внутреннего API Директа.

**`yandex_direct_minus_snapshot` / `v_yandex_direct_minus_delta`** — 1 546 строк за **один день**
(2026-07-31) против 32 831 за 2026-07-17…2026-08-15 в v5. Step14 в дневном `pipeline.py`
остаётся в `NIGHTLY_DEFAULT_STEPS`, но с 2026-08-17 выполняется ночным cron
`10 18 * * *` UTC = 23:10 Екб. Дельта минус-фраз станет полноценной после наполнения
30-дневной истории.

**`yandex_direct_search_query_report_master`** — в v6 лежит сырьё (40 M строк, 2026-01-01…2026-08-03),
в v5 в PBI шёл готовый агрегат `…_master_pbi` (328 658). Данные есть, но объект в PBI-контракт v6 не
включён и агрегата нет — импортировать 40 M строк в модель нельзя.

**`yandex_direct_cookie_analytics_website_pages`** — 965 764 vs 1 011 518, свежесть до 2026-07-25
против 2026-08-01 в v5.

**`yandex_direct_ads_texts` / `yandex_direct_type_placement_report_master`** — закрыты через
совместимые `bi_*` над `raw_data.direct_cookie_ads_texts_master` и
`raw_data.direct_cookie_type_placement_master`. Группировка перенесена из Power Query в
ClickHouse. `type_placement_ru` теперь маппится внутри view; `goal_crm_order_paid` остаётся 0,
потому что новый источник отдаёт только общий `goals`.

## §0.4 Числовая сверка ядра

`fact_big_analytics`, 2026-02-01…2026-07-31, ось «По дате заявки», без пикселя,
после прогона `ed6bfc6f9c23`:

| метрика | v5 | v6 | Δ% |
|---|---:|---:|---:|
| cost | 1 052 258 244.29 | 1 056 015 699.73 | +0.36% |
| обращения | 292 409 | 286 631 | −1.98% |
| корректные | 147 907 | 146 716 | −0.81% |
| квалифицированные | 44 271 | 44 432 | +0.36% |
| приезды | 33 543 | 33 735 | +0.57% |
| продажи | 3 071 | 3 090 | +0.62% |

Подробности и разбор остаточных дельт — [`RAW_DIFF_FINDINGS.md`](RAW_DIFF_FINDINGS.md) §6.

## §0.5 Что нужно сделать, чтобы отчёты собрались

1. Довести оставшийся источник `accounts_human_cyborgs`: сейчас PBIP ещё читает
   `raw_new_human_cyborgs`.
2. После публикации/refresh Power BI проверить, что опубликованный Service читает текущие
   `bi_*`/`*_star`, а не старые снимки.
3. Пустые `bi_*` больше не разрешены: `check_utm_fuck_direct` должен оставаться непустым, а
   `yandex_direct_return_commission_report` / `bi_yandex_direct_return_commission_report`
   выведены из контракта и удалены из live ClickHouse 2026-08-17.
4. Проверить полный Power BI Desktop/Service refresh после перевязки на `*_star` и direct-cookie
   `bi_*`.
5. Дать 30-дневной истории step14 наполниться ночным cron; до этого `minus_delta` короткая.
6. Не возвращать legacy `arc_fact/arf_fact`: `analytics_report_criterion/feed` вычеркнуты из
   активного контракта тестом `tests/test_pbi_contract_lists.py`.
7. Держать `PBI_EMPTY_ALLOWED` пустым: любой активный пустой `bi_*` должен падать гейтом (#40).

---

> ⚠️ **Legacy-раздел (v5 / PostgreSQL) ниже.** Актуальный ответ — §0 выше.
> Большая часть этого файла — legacy/v5 PostgreSQL-справочник
> Power BI-источников. В v6_ch активный слой строится в ClickHouse `ad_analytics`,
> а публикация/refresh Power BI должна проверяться отдельно по текущему `refresh_powerbi.py`,
> TMDL/PBIP и фактическим таблицам. Не использовать старые упоминания `pipeline_powerbi.py`
> и Victory как команду запуска v6.
>
> **Назначение legacy-раздела.** Исторически фиксировал, какие именно таблицы PostgreSQL
> подтягивал Power BI как источник данных, чтобы к вопросу «а что грузится в PBI?»
> больше не возвращаться.
> Строки про `public.fact_direct_feed_funnel`, `feed_url` и ручную переливку ниже относятся к
> старому PostgreSQL/v5-контракту. В активном v6_ch контуре step144 строит ClickHouse
> `ad_analytics.fact_direct_feed_funnel_light`, а `ad_analytics.fact_direct_feed_funnel`
> является compatibility view через `Dim_PlacementFeed`.
>
> **Authoritative-источник списка** — массив `_ALL_TABLES` в
> [`refresh_powerbi.py`](refresh_powerbi.py) (40 import-таблиц). В v6_ch эти таблицы
> триггерятся на refresh датасета в Power BI Service отдельным подпроцессом `cron_run.py`
> (`refresh_powerbi.py --no-notify`, запускается после успешного `pipeline.py`) — файла
> `pipeline_powerbi.py` в этом репозитории нет, он существует только в `big_analytics_v5`.
> Если список в коде изменится — обновить и этот файл.

**Факты legacy/v5 в БД проверены:** 2026-06-04 (read-only, `ad_analytics_bi` на Victory `103.88.240.90`, роль `bi_analytic`).

---

## Главное: 10 таблиц-источников Power BI

Все 10 существуют, все `LOGGED` (relpersistence `p`), схема `public`.

| # | Имя в PBI (`_ALL_TABLES`) | Реальное имя в PostgreSQL | Строк | Размер | Кол. | Дата (колонка → период) | Что содержит / гранулярность | Обновление |
|---|---|---|---:|---:|---:|---|---|---|
| 1 | `big_analytics_full` | `public.big_analytics_full` | ~3.44 M | 9.4 GB | 73 | `"Date"` → 2026-01-01 … тек. | **Главная витрина.** UNION ALL всех источников (direct, seo, pixel-атрибуц, telegram, reviews, crop, calls, tp8). 1 строка = факт расхода/заявки/звонка за дату по кампании/домену/салону. Воронка статусов в колонках. | Полный пересбор через CTAS каждый прогон `pipeline.py` (step6), финализация step7 (SET LOGGED + индексы). |
| 2 | `direct_history` | `public.yandex_direct_history` ⚠ | 55 241 | 144 MB | 19 | `datetime` (timestamp события) | История изменений аккаунтов Яндекс.Директ. 1 строка = одно изменение (campaign/adgroup, old→new). | Инкрементально, step9 (фон + обогащение директолог/домен/салон). |
| 3 | `check_utm_fuck_direct` | `public.check_utm_fuck_direct` | 238 | 288 kB | 12 | `date` | Группы Директа с неверными/отсутствующими UTM + накопленный расход. 1 строка = группа с момента появления проблемы. | step13_utm_direct_audit (ночной cron + в `pipeline.py`). |
| 4 | `analytics_report_placement` | `public.analytics_report_placement` | ~8.13 M | 12 GB | 47 | `date` → 2026-01-01 … тек. | **Самая тяжёлая.** Размещения (плейсменты) РСЯ по дням. 1 строка = (дата × домен × логин × плейсмент × кампания). Воронка + расход по площадкам. | TRUNCATE+INSERT, **вне `pipeline.py`** — `step_cron_night/report_placement/step2_build_analytics.py`. Источник: `yandex_direct_report_placement` LEFT JOIN `raw_leads`. |
| 5 | `yandex_direct_korrektirovki` | `public.yandex_direct_korrektirovki` | 38 310 | 18 MB | 17 | `loaded_at` | Корректировки ставок (BidModifiers). 1 строка = корректировка на кампанию/уровень. | `step_cron_night/korrektirovki/korrektirovki.py` (ночной cron, ~25 мин). |
| 6 | `yandex_direct_404_errors` | `public.yandex_direct_404_errors` | 9 700 | 5.3 MB | 14 | `visit_date` → 2026-01-01 … тек. | 404-ошибки по визитам (Метрика). 1 строка = визит с 404. | Инкрементально, 7-дн. перекрытие. В обоих пайплайнах (`404_errors/404_errors.py`). |
| 7 | `big_analytics_full_arrival` | `public.big_analytics_full_arrival` | ~86k | ~22 MB | **73** | `"Date"` → 2026-01-01 … тек. | Воронка **по дате визита/приезда** (вместо даты заявки). Используется атрибуцией «По дате визита» в модели PBI. ⚠️ Колонок 73 (зеркало `big_analytics_full`, не 21 — расширено для PBI-совместимости). | Отдельный скрипт `step13_arrival` (`pipeline.py`, step 13 arrival). |
| 8 | `pixel_score` | `public.pixel_score` | 13 385 | 18 MB | 38 | `month` | CPL-скоры/веса по кампаниям для атрибуции пикселя. 1 строка = (месяц × салон × домен × кампания) со скорами квал/визит/продажа. | step11_pixel_score (`pipeline.py`, ~10 c). |
| 9 | `yandex_direct_cookie_analytics_website_pages` | `public.yandex_direct_cookie_analytics_website_pages` | 612 166 | 973 MB | 23 | `date_from` / `date_to` | Аналитика страниц сайтов (баннер → URL, расход, клики, цели). 1 строка = (логин × домен × баннер × период). | Отдельный сервис `work/yandex_direct_cookie_analytics_website_pages/`. **Вне `pipeline.py`.** |

⚠ **Расхождение имён (важно!):** в PBI таблица называется `direct_history`, но в PostgreSQL она
переименована в `yandex_direct_history` (апрель 2026). В refresh PBI обращается по старому имени
`direct_history` — это имя partition/таблицы в семантической модели, не в БД.

---

## Замечания по гранулярности и размеру

- **Тяжёлые (≈21 GB вместе):** `analytics_report_placement` (12 GB, 8.1M строк) и
  `big_analytics_full` (9.4 GB, 3.4M строк) — основной вклад во время refresh.
  Обе читаются Import-режимом как `SELECT * FROM table` без query folding.
- Остальные 8 таблиц суммарно < 1.5 GB — на время refresh влияют слабо.
- `big_analytics_full` и `big_analytics_full_arrival` **пересоздаются через CTAS** в `pipeline.py`.
  При запросе во время прогона пайплайна могут быть пустыми/промежуточными.

## Таблицы-источники (НЕ в PBI напрямую, но удалять нельзя)

| Таблица | Размер (ориент.) | Причина защиты |
|---|---|---|
| `yandex_direct_report_placement` | ~4.9 GB | Источник для `analytics_report_placement` (ARP) |
| `local_yandex` | ~1.4 GB | Источник для `raw_yandex` (step1) |
| `big_analytics_pixel_score` | ~0.1 GB | Score/diagnostics для пикселя; `big_analytics_full` получает прямой `_source_table='pixel'` |
| `fact_region_spend` | (датамарт) | ✅ **в текущем BA6 PBI/refresh**: датамарт «расход по регионам показа» (`region_spend/`), строится дневным пайплайном между build_unified и build_star. В `refresh_powerbi.py::_ALL_TABLES` зарегистрирован; admin PBIP содержит таблицу и связи. |
| `fact_adformat_spend` | (датамарт) | ✅ **в текущем BA6 PBI/refresh**: датамарт «расход по формату объявления» (`adformat_spend/`), грань `date×campaign_id×ad_group_id×ad_network_type×ad_format`. В `refresh_powerbi.py::_ALL_TABLES` зарегистрирован; admin PBIP читает `bi_fact_adformat_spend`. |
| `fact_criterion_spend` | (датамарт) | ✅ **в текущем BA6 PBI/refresh**: датамарт «расход по критерию» (`criterion_spend/`), грань `…×criterion_id×criterion` + `criterion_type` (autotargeting/retargeting/interests/keyword), скоуп ниши «Авто». В `refresh_powerbi.py::_ALL_TABLES` зарегистрирован; admin/user PBIP используют страницу критерия. |
| `fact_region_zayavki` | ~58 MB | ✅ **в модели и refresh**: датамарт «воронка по ЗАЯВКАМ в разрезе региона» (`region_spend/build_region_zayavki.py`). Грань `created_date×campaign_id×id_location`, 12 целочисленных CRM-метрик воронки (НЕ расход — близнец `fact_region_spend` по заявкам). Связи в PBI: **только 2** — `campaign_id→Dim_Campaign.CampaignId` + `created_date→Dim_Date.Date`. НЕТ связей по `ad_group`/`domain` (этих осей у лидов нет — by design, иначе задвоение). Дата = `created_date` (день заявки), НЕ день показа. |
| `fact_criterion_zayavki` | ~41 MB | ✅ **в модели и refresh**: датамарт «воронка по ЗАЯВКАМ в разрезе критерия/ключа» (`criterion_spend/build_criterion_zayavki.py`). Грань `created_date×campaign_id×criterion`, 12 целочисленных CRM-метрик воронки + `criterion_type`/`criterion_raw` (LEFT JOIN из `fact_criterion_spend`). Связи в PBI: **только 2** — `campaign_id→Dim_Campaign.CampaignId` + `created_date→Dim_Date.Date`. НЕТ связей по `ad_group`/`domain` (у лидов нет — by design). Дата = `created_date` (день заявки). |
| `fact_vk_ads` | (датамарт, ~0.4k строк) | ✅ **в модели и refresh**: датамарт «VK Ads отчёт» (сегмент×оффер×объявление воронка), строится в `star_refactor/build_star.py::build_vk_ads_fact` сразу после `build_arp_fact` (до view/index). Грань `date×account(салон)×ad_plan(оффер)×ad_group(сегмент)×banner(объявление)×атрибуция`. Спайн заявка-оси = `local_vk_ads_stats_day` (banner×date, только платный VK Авто, spent>0) → несёт shows/clicks/spent. Воронка = VK-лиды (`utm_source='vkads'`, `utm_content='ad_group_id/banner_id'`) через `config/status_sql`. **Дедуп рекламных метрик:** shows/clicks/spent несёт ТОЛЬКО ось 'По дате заявки'; ось 'По дате визита' имеет их =0 (SUM по всей таблице не удваивается). CTR/CPM/CPL/QCPL/CPV/CPS — меры PBI (DAX), в таблице НЕ материализованы. Посевы VK НЕ входят. |
| `yandex_direct_minus_snapshot` | (растёт, ~1 строка/РК/день) | ✅ **в модели и refresh**: снапшоты минус-фраз Яндекс.Директ (campaign+groups+наборы). Гранула: `"date"`×login×campaign_id. Колонки: minus_in_campaign, minus_in_groups, minus_in_sets, minus_total, has_minus, check_ok, **block** (`'tp2'`/`'tp4'`/`'прочее'` — вычисляется из campaign_name, маркер BLOCK_COL_2026-06-17). `--block` управляет сканированием API, в таблицу пишется атрибут block. Заполняется step14 (ночной пайплайн). |
| `v_yandex_direct_minus_delta` | (VIEW) | ✅ **в модели и refresh**: VIEW поверх `yandex_direct_minus_snapshot` с LAG-динамикой (minus_total_prev, delta, dynamics: первый замер/добавили/СНЯЛИ/без изменений). PARTITION BY login, campaign_id ORDER BY "date". В PBI импортируется как виртуальная таблица. |

Если `build_region_spend`, `build_adformat_spend` или `build_criterion_spend` падает,
`pipeline.py` / `fast_pipeline.py` помечают прогон как `DEGRADED`: основной
`fact_big_analytics` остаётся свежим, но соответствующая `fact_*_spend` может остаться
старой/неполной до успешной пересборки.

## Схема fact_vk_ads (VK Ads датамарт — для PBI-страницы)

`public.fact_vk_ads` — грань `date × account(салон) × ad_plan(оффер) × ad_group(сегмент) × banner(объявление) × атрибуция`.
Строит `star_refactor/build_star.py::build_vk_ads_fact` (DROP+CTAS + btree на `date` + ANALYZE).

| Колонка | Тип | Роль | Примечание |
|---|---|---|---|
| `date` | date | измерение | заявка-ось = день показа (stats); визит-ось = arrival_date |
| `account_id` | bigint | измерение | VK-кабинет (= vk_client_id Авто) |
| `салон` | text | измерение | из `local_gsheet_sites` по vk_client_id (niche='Авто') |
| `ad_plan_id` | bigint | измерение | ОФФЕР (ad_plan) — id |
| `ad_plan_name` | text | измерение | ОФФЕР — название |
| `ad_group_id` | bigint | измерение | СЕГМЕНТ (ad_group) — id |
| `ad_group_name` | text | измерение | СЕГМЕНТ — название |
| `banner_id` | bigint | измерение | ОБЪЯВЛЕНИЕ (banner) — id |
| `banner_name` | text | измерение | ОБЪЯВЛЕНИЕ — название |
| `атрибуция` | text | ось | `'По дате заявки'` / `'По дате визита'` |
| `shows` | bigint | метрика (реклама) | Показы. **Только на оси 'По дате заявки'**; на визит-оси =0 |
| `clicks` | bigint | метрика (реклама) | Клики. Только заявка-ось; визит-ось =0 |
| `spent` | numeric(14,2) | метрика (реклама) | Расход. Только заявка-ось; визит-ось =0 |
| `заявки` | bigint | метрика (воронка) | kol_vo_zayavok |
| `записи` | bigint | метрика (воронка) | status='Приедет' (priedet) |
| `квал` | bigint | метрика (воронка) | qualified (kval) |
| `визиты` | bigint | метрика (воронка) | visit (priezd) |
| `продажи` | bigint | метрика (воронка) | sale (prodazhi) |

**Дедуп рекламных метрик между осями:** shows/clicks/spent привязаны к дню показа и несутся ТОЛЬКО строками
оси `'По дате заявки'`. Строки оси `'По дате визита'` имеют shows=clicks=spent=0 → `SUM(spent)`/`SUM(shows)`
по всей таблице (обе оси) НЕ удваивается. В PBI для рекламных мер фильтр по оси не нужен; для воронки —
слайсер `атрибуция`.

**Меры PBI (DAX, НЕ в таблице):** CTR=clicks/shows, CPM=spent/shows*1000, CPL=spent/заявки,
QCPL=spent/квал, CPV=spent/визиты, CPS=spent/продажи.

**Связи в PBI:** `date→Dim_Date.Date`. Прочие оси (account/ad_plan/ad_group/banner/салон) — degenerate-dim
внутри самой таблицы (справочников под них нет — VK-иерархия несётся построчно).

**Golden Кудерко НЕ затрагивается** (VK вне GOLDEN_SOURCES; `build_star` fact_big_analytics её не читает).

## Промежуточные UNLOGGED таблицы (живут только внутри прогона pipeline)

Не читаются Power BI. Между запусками содержат данные от предыдущего прогона.

`big_analytics_direct`, `raw_yandex`, `raw_leads`, `raw_calls`, `raw_domains`,
`big_analytics_crop_targeting`, `big_analytics_seo`, `big_analytics_pixel`,
`big_analytics_telegram`, `pixel_leads`.

---

## Как Power BI забирает данные (механика refresh)

1. `cron_run.py` запускает `refresh_powerbi.py` только после успешного `pipeline.py`; отдельного
   `pipeline_powerbi.py` в активном v6-контуре нет.
2. `refresh_powerbi.py` → OAuth client_credentials в Azure AD, проверка datasource датасета
   (BA5/PostgreSQL блокируется до POST), selective transactional POST `/refreshes` с объектами
   `[{table: t} for t in _ALL_TABLES]`, затем polling статуса (до 60 мин).
3. Режим всех таблиц в модели — **Import** (не DirectQuery), без incremental refresh policy.
4. Конфиг PBI Service (`workspace_id`, `dataset_id`, `client_id/secret`, `tenant_id`) —
   в `~/.secret/.env` (powerbi-конфиг через `loader.py`; ⚠️ `tokens.json` не существует).
   **Никогда не хардкодить.**

Локальная модель/отчёт (PBIP): `Documents/креативы виктори/.../Большая аналитика_v00.SemanticModel`
+ `Большая аналитика_v00.Report` (живая file-based модель); облачный Service dataset на 2026-08-24 —
`Большая аналитика_admin` в воркспейсе `Victory Analytics`.

---

## ⚠️ Дрифт и план звезды (исторический раздел — план РЕАЛИЗОВАН, см. пометку)

> ✅ **Статус 2026-06-11: cutover на звезду СОСТОЯЛСЯ, схема консолидирована в `public`**
> (отдельной `star` нет). PBI читает лёгкий факт `public.fact_big_analytics` + `Dim_*` + VIEW
> `public.arp_fact`. Раздел ниже сохранён как историческая мотивация (замеры узкого места,
> почему делали звезду). Актуальный маппинг — в секции «ФАКТИЧЕСКИЙ маппинг» выше.

**Дрифт имени источника:** в семантической модели PBI таблица называется `big_analytics_full`,
но её M-запрос (partition) **фактически читает `public.big_analytics_unified`** (5.37 ГБ / 3.68M строк),
а не `big_analytics_full` (4.96 ГБ). `big_analytics_unified` = MIRROR+UNION заявка-факта +
`big_analytics_full_arrival` (визита) + пиксель-атрибуция. Это и есть таблица с колонкой `атрибуция`.

**Замеры узкого места (2026-06-06):** выгрузка широкой строки (~1500 б) ~1100 строк/сек при
egress Victory ~2 МБ/с; параллелизм 4 потока даёт лишь ×1.6 (общий потолок, не на соединение).
**~4480 из 5373 МБ факта — денормализованный справочный текст.**

**План звезды (см. `Documents/креативы виктори/STAR_SCHEMA_PLAN.md`, ТЗ — `STAR_REFACTOR_BRIEF.md`):**
лёгкий факт (ключи+меры+атрибуция, ~168 б/строку → ×6.7 быстрее) + conformed dim
(Dim_Site по `domain`, Dim_Campaign по `CampaignId`, Dim_AdGroup по `AdGroupId`, Dim_Date),
общие для `big_analytics_full` и `analytics_report_placement` (12 ГБ, тот же паттерн).
После cutover: дроп материализации full/unified (−~10 ГБ на сервере), обновить `_ALL_TABLES`.

**`_ALL_TABLES` на момент этого v5-плана (старый PostgreSQL `refresh_powerbi.py`, ~193-200):**
big_analytics_full, Dim_Date, Dim_Campaign, Dim_AdGroup, Dim_Site, analytics_report_placement,
direct_history, check_utm_fuck_direct, yandex_direct_korrektirovki, yandex_direct_404_errors,
pixel_score, yandex_direct_cookie_analytics_website_pages — 12 таблиц. Не путать с текущим
v6_ch `refresh_powerbi.py:48-59` (25 таблиц, включает `fact_vk_ads`, `fact_adformat_spend`,
`fact_criterion_spend`, `dim_criterion`, `fact_region_spend`/`_zayavki`, `Dim_PlacementFeed`,
`fact_direct_feed_funnel`, минус-снапшот, `fact_ml_korrektirovki` — см. §0).

---

## ✅ ФАКТИЧЕСКИЙ маппинг PBI-имя → таблица БД (M-query; факты БД проверены 2026-06-11)

> Источник правды — `Schema=`/`Item=` в partition-source PBIP-проектов.
> ⚠️ **Звезда консолидирована в схему `public` (2026-06-10) — отдельной схемы `star` БОЛЬШЕ НЕТ.**
> Все факты и измерения физически в `public` (проверено `pg_namespace`: есть только `public`).
> PBI-модель, перепубликованная пользователем, читает звезду из `public`. `arp_fact` теперь **VIEW**
> (не TABLE) над `analytics_report_placement`.
> Если в TMDL partition остаётся `Schema="star"` — это **рассинхрон**: refresh упадёт
> `The key didnt match any rows Key=[Schema="star", Item=...]` → перепубликовать модель из public
> (см. [`KNOWN_ISSUES.md`](KNOWN_ISSUES.md) #15).

| PBI-таблица (partition) | Реальная таблица БД (схема `public`) | ~строк | relkind | Режим копии |
|---|---|---:|---|---|
| big_analytics_full | `public.fact_big_analytics` | ~3.82M | TABLE | **батчи 300k** |
| analytics_report_placement | `public.arp_fact` (**VIEW** над ARP) | ~8.27M | VIEW | **батчи 300k** |
| Dim_AdGroup | `public."Dim_AdGroup"` (PK AdGroupId) | 152k | TABLE | целиком |
| Dim_Campaign | `public."Dim_Campaign"` (PK CampaignId) | 16 461 | TABLE | целиком |
| Dim_Date | `public."Dim_Date"` (PK Date) | 186 | TABLE | целиком |
| Dim_Site | `public."Dim_Site"` (PK domain) | 3826 | TABLE | целиком |
| big_analytics_full_arrival | `public.big_analytics_full_arrival` | ~86k | TABLE | целиком |
| check_utm_fuck_direct | `public.check_utm_fuck_direct` (PK id) | 238 | TABLE | целиком |
| direct_history | `public.yandex_direct_history` (PK id) | 60k | TABLE | целиком |
| pixel_score | `public.pixel_score` | 14k | TABLE | целиком |
| yandex_direct_404_errors | `public.yandex_direct_404_errors` (PK id) | 10k | TABLE | целиком |
| yandex_direct_cookie_analytics_website_pages | `public.yandex_direct_cookie_analytics_website_pages` (PK id) | 612k | TABLE | **батчи 300k** |
| yandex_direct_korrektirovki | `public.yandex_direct_korrektirovki` (PK id) | 42k | TABLE | целиком |
| fact_region_spend | `public.fact_region_spend` (PK row_hash) | (датамарт) | TABLE | целиком |
| fact_adformat_spend | `public.fact_adformat_spend` (PK row_hash) | (датамарт) | TABLE | целиком |
| fact_criterion_spend | `public.fact_criterion_spend` (PK row_hash) | (датамарт) | TABLE | целиком |
| fact_region_zayavki | `public.fact_region_zayavki` (PK row_hash) | ~128k | TABLE | целиком |
| fact_criterion_zayavki | `public.fact_criterion_zayavki` (PK row_hash) | ~95k | TABLE | целиком |
| fact_direct_feed_funnel | `public.fact_direct_feed_funnel` | ~563k | TABLE | целиком; включает `feed_url`, `feed_url_key` |

Всего **20 таблиц БД** — все в схеме **`public`** (факты+измерения звезды × 6 + витрины × 8 +
датамарты расход/заявки × 5: `fact_region/adformat/criterion_spend`, `fact_region/criterion_zayavki` +
фидовая воронка `fact_direct_feed_funnel`).

Для фидовой воронки после обновления URL/пересборки на Victory перелить локальную копию:

```bash
python3 work/copy_pbi_tables_to_localhost.py --force --only public.fact_direct_feed_funnel --no-telegram
```

Шаблоны `DateTableTemplate_*`, `LocalDateTable_*`, `Users`, «Модель атрибуции» — внутренние
объекты PBI без БД-источника, не копируются.

> **Dim_Campaign — заполненность** (факт 2026-06-11): total 16 461, `campaign_status` и
> `payment_model` непустые у **5905** строк (после `_warm_campaign_status.py` + `build_star.py`).
> Просадка cs/pm до ~906 = признак партиал-прогона `--from-step=3` без prefetch → лечится
> прогревом (см. [`KNOWN_ISSUES.md`](KNOWN_ISSUES.md) #14, [`RUNBOOK.md`](RUNBOOK.md)).

### Локальная реплика для ускорения refresh (PBI читает с localhost)

Power BI Desktop грузит данные с Victory по дырявому каналу (~8% потерь, одиночный TCP
~0.3 МБ/с) = часы на refresh. Решение — **локальная реплика 14 таблиц в localhost
PostgreSQL** (Postgres.app, `localhost:5432`, db `ad_analytics_bi`); PBIP читает с loopback
(~ГБ/с). Это НЕ incremental refresh PBI (он ЗАПРЕЩЁН) — это синк реплики БД; PBI по-прежнему
Import целиком, но с быстрого localhost.

**Два скрипта (выстрадано замерами 2026-06-09):**

1. **`work/bulk_load_heavy_via_gzip.py`** — ПЕРВИЧНАЯ заливка 3 ТЯЖЁЛЫХ таблиц
   (`public.fact_big_analytics`, `public.arp_fact`, `cookie`; схема `star` упразднена 2026-06-10 —
   `--only` указывать с `public.`). Метод-победитель:
   `COPY ... TO STDOUT | gzip` **на сервере** → `ssh` → файл `.gz` на маке → `gunzip | COPY FROM`.
   gzip жмёт текст витрины ×17–70 (CPU сервера дёшев), по каналу летит уже сжатое.
   Замеры: cookie 973 МБ→13.9 МБ gz/56 c (V==L); history 157 МБ→9 МБ gz/27.7 c.
   Провалившиеся альтернативы (не повторять): COPY чанками 6 потоков → backpressure/захлёб
   (count=0 за 30 мин); `pg_dump -Fc` single-stream → 0.013 МБ/с. ⛔ `pg_dump`/`psql` НА
   Victory сломаны (пустая обёртка) — дамп только с мака (Postgres.app) или server COPY|gzip.
   Запуск: `python3 work/bulk_load_heavy_via_gzip.py` (`--only schema.table`, `--keep-files`).

2. **`work/copy_pbi_tables_to_localhost.py`** — заливка/синк ОСТАЛЬНЫХ 11 (лёгких/средних) +
   ИНКРЕМЕНТ всех. Флаги: `--chunks N` (параллель внутри таблицы по диапазону колонки, ×5.7
   на средних), `--delta-days N` (ИНКРЕМЕНТ: тяжёлые date-таблицы ресинкают только окно
   последних N дней DELETE+COPY, мелкие — по fingerprint-сверке пропуск/перезалив),
   `--jobs N`, `--force`, `--dry-run`, `--verify`, `--only`. VPN-bypass (физ.bind) + Telegram
   встроены. Креды Victory — `.secret/loader.py`.

**Инкрементальный джоб (launchd):** `work/big_analytics_v5/sync_replica_incremental.sh`
(вызывает скрипт #2 с `--delta-days 7 --chunks 4 --jobs 1`); агент
`~/Library/LaunchAgents/com.victory.pbireplica.sync.plist` — ежедневно, переживает reboot.

**PBIP M-query:** host `analytics-marketing.ru` → `localhost` в 14 partition (TMDL обоих
проектов admin+STAR). Активный проект — **admin**.

---

## 📁 Конвенция папок отчётов «Большая аналитика» — источник данных (admin/user → Victory, STAR → локальная база)

> **Назначение.** Однозначно зафиксировать, какая папка отчёта «Большая аналитика»
> загружает данные из какого источника. По умолчанию использовать эту привязку при
> загрузке/выгрузке отчётов, пока не поступит другая команда от пользователя.
> Зафиксировано 2026-06-10.

| # | Отчёт | Папка (абсолютный путь на Маке) | Источник данных |
|---|-------|----------------------------------|-----------------|
| 1 | **АДМИНСКИЙ** | `/Users/semen/Documents/креативы виктори/Большая аналитика_admin` | **Сервер Victory** (`103.88.240.90`) |
| 2 | **ЮЗЕРСКИЙ** | `/Users/semen/Documents/креативы виктори/Большая аналитика_user` | **Сервер Victory** (`103.88.240.90`) |
| 3 | **ТЕСТОВЫЙ срез (STAR)** | `/Users/semen/Documents/креативы виктори/Большая аналитика_STAR` | **Локальная база** (по умолчанию, пока нет другой команды) |

**Правило по умолчанию:**
- **admin + user → Victory** (прод-БД `ad_analytics_bi` @ `103.88.240.90`).
- **STAR → локальная база** (Postgres.app / реплика на маке) — это тестовый срез,
  привязка остаётся локальной до явной команды переключить.

⚠️ Не путать с тем, какой M-query host прописан внутри TMDL конкретного `.pbip` (там бывает
`localhost` после cutover) — данная таблица фиксирует **конвенцию загрузки/выгрузки отчётов**,
которой следовать по умолчанию при работе с этими тремя папками.
