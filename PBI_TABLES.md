# PBI_TABLES.md — Power BI source tables

> ⚠️ **Статус v6_ch:** большая часть этого файла ниже — legacy/v5 PostgreSQL-справочник
> Power BI-источников. В v6_ch активный слой строится в ClickHouse `ad_analytics`,
> а публикация/refresh Power BI должна проверяться отдельно по текущему `refresh_powerbi.py`,
> TMDL/PBIP и фактическим таблицам. Не использовать старые упоминания `pipeline_powerbi.py`
> и Victory как команду запуска v6.
>
> **Назначение legacy-раздела.** Исторически фиксировал, какие именно таблицы PostgreSQL
> подтягивал Power BI как источник данных, чтобы к вопросу «а что грузится в PBI?»
> больше не возвращаться.
>
> **Authoritative-источник списка** — массив `_ALL_TABLES` в
> [`refresh_powerbi.py`](refresh_powerbi.py) (строки 193–204). Именно эти таблицы
> триггерятся на refresh датасета в Power BI Service после прогона пайплайна
> ([`pipeline_powerbi.py`](pipeline_powerbi.py) → `refresh_powerbi(tables=_ALL_TABLES)`).
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
| 7 | `yandex_direct_return_commission_report` | `public.yandex_direct_return_commission_report` | 45 867 | 63 MB | 13 | `date` | Отчёт возвратной агентской комиссии. 1 строка = (логин × дата × тип сети × слот × тип кампании). | `work/calculation_agency_commission/step2_report.py`. **Вне `pipeline.py`.** |
| 8 | `big_analytics_full_arrival` | `public.big_analytics_full_arrival` | ~86k | ~22 MB | **73** | `"Date"` → 2026-01-01 … тек. | Воронка **по дате визита/приезда** (вместо даты заявки). Используется атрибуцией «По дате визита» в модели PBI. ⚠️ Колонок 73 (зеркало `big_analytics_full`, не 21 — расширено для PBI-совместимости). | Отдельный скрипт `step13_arrival` (`pipeline.py`, step 13 arrival). |
| 9 | `pixel_score` | `public.pixel_score` | 13 385 | 18 MB | 38 | `month` | CPL-скоры/веса по кампаниям для атрибуции пикселя. 1 строка = (месяц × салон × домен × кампания) со скорами квал/визит/продажа. | step11_pixel_score (`pipeline.py`, ~10 c). |
| 10 | `yandex_direct_cookie_analytics_website_pages` | `public.yandex_direct_cookie_analytics_website_pages` | 612 166 | 973 MB | 23 | `date_from` / `date_to` | Аналитика страниц сайтов (баннер → URL, расход, клики, цели). 1 строка = (логин × домен × баннер × период). | Отдельный сервис `work/yandex_direct_cookie_analytics_website_pages/`. **Вне `pipeline.py`.** |

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
| `big_analytics_pixel_score` | ~0.1 GB | Источник доливки в `big_analytics_full` (`_source_table='пиксель_атрибуц'`) |
| `fact_region_spend` | (датамарт) | ⏳ **pending**: датамарт «расход по регионам показа» (`region_spend/`), строится дневным пайплайном между build_unified и build_star. **Добавить в PBI-модель + в `_ALL_TABLES` (refresh_powerbi.py) ТОЛЬКО ПОСЛЕ того как пользователь создаст вкладку** — иначе pipeline_powerbi-refresh упадёт «нет таблицы в датасете». См. `region_spend/CLAUDE.md`. |
| `fact_adformat_spend` | (датамарт) | ⏳ **pending**: датамарт «расход по формату объявления» (`adformat_spend/`), грань `date×campaign_id×ad_group_id×ad_network_type×ad_format`. Строится дневным пайплайном сразу после build_region_spend (до build_star). **Регистрировать в `_ALL_TABLES` (refresh_powerbi.py) ТОЛЬКО ПОСЛЕ того как пользователь создаст вкладку** — иначе refresh упадёт. См. `adformat_spend/CLAUDE.md`. |
| `fact_criterion_spend` | (датамарт) | ⏳ **pending**: датамарт «расход по критерию» (`criterion_spend/`), грань `…×criterion_id×criterion` + `criterion_type` (autotargeting/retargeting/interests/keyword), скоуп ниши «Авто». Строится дневным пайплайном после build_adformat_spend (до build_star). **Регистрировать в `_ALL_TABLES` (refresh_powerbi.py) ТОЛЬКО ПОСЛЕ того как пользователь создаст вкладку** — иначе refresh упадёт. См. `criterion_spend/CLAUDE.md`. |
| `fact_region_zayavki` | ~58 MB | ✅ **в модели** (STAR + admin, TMDL добавлены 2026-06-15): датамарт «воронка по ЗАЯВКАМ в разрезе региона» (`region_spend/build_region_zayavki.py`). Грань `created_date×campaign_id×id_location`, 12 целочисленных CRM-метрик воронки (НЕ расход — близнец `fact_region_spend` по заявкам). Связи в PBI: **только 2** — `campaign_id→Dim_Campaign.CampaignId` + `created_date→Dim_Date.Date`. НЕТ связей по `ad_group`/`domain` (этих осей у лидов нет — by design, иначе задвоение). Дата = `created_date` (день заявки), НЕ день показа. **`_ALL_TABLES` (refresh_powerbi.py) — добавить ТОЛЬКО ПОСЛЕ публикации датасета с этой таблицей.** См. `region_spend/CLAUDE.md`. |
| `fact_criterion_zayavki` | ~41 MB | ✅ **в модели** (STAR + admin, TMDL добавлены 2026-06-15): датамарт «воронка по ЗАЯВКАМ в разрезе критерия/ключа» (`criterion_spend/build_criterion_zayavki.py`). Грань `created_date×campaign_id×criterion`, 12 целочисленных CRM-метрик воронки + `criterion_type`/`criterion_raw` (LEFT JOIN из `fact_criterion_spend`). Связи в PBI: **только 2** — `campaign_id→Dim_Campaign.CampaignId` + `created_date→Dim_Date.Date`. НЕТ связей по `ad_group`/`domain` (у лидов нет — by design). Дата = `created_date` (день заявки). **`_ALL_TABLES` (refresh_powerbi.py) — добавить ТОЛЬКО ПОСЛЕ публикации датасета.** См. `criterion_spend/CLAUDE.md`. |
| `fact_vk_ads` | (датамарт, ~0.4k строк) | ⏳ **pending**: датамарт «VK Ads отчёт» (сегмент×оффер×объявление воронка), строится в `star_refactor/build_star.py::build_vk_ads_fact` сразу после `build_arp_fact` (до view/index). Грань `date×account(салон)×ad_plan(оффер)×ad_group(сегмент)×banner(объявление)×атрибуция`. Спайн заявка-оси = `local_vk_ads_stats_day` (banner×date, только платный VK Авто, spent>0) → несёт shows/clicks/spent. Воронка = VK-лиды (`utm_source='vkads'`, `utm_content='ad_group_id/banner_id'`) через `config/status_sql`. **Дедуп рекламных метрик:** shows/clicks/spent несёт ТОЛЬКО ось 'По дате заявки'; ось 'По дате визита' имеет их =0 (SUM по всей таблице не удваивается). CTR/CPM/CPL/QCPL/CPV/CPS — меры PBI (DAX), в таблице НЕ материализованы. Посевы VK НЕ входят. **Страница «VK Ads» уже ДОБАВЛЕНА (2026-07-11)** в оба живых отчёта: **admin** (со своей моделью — TMDL таблица `fact_vk_ads` + 6 мер CTR/CPM/CPL/QCPL/CPV/CPS + связь `date→Dim_Date`) и **user** (тонкий byConnection — только страница). Матрица Кампания→Группа→Объявление, 13 столбцов (без Статус/Что делаем). **Отчёт STAR не существует** (живые только admin+user). ⏳ Осталось (ручной шаг Семёна): открыть admin v00 в Power BI Desktop → **опубликовать датасет с `fact_vk_ads`** → и ТОЛЬКО ПОСЛЕ публикации зарегистрировать `fact_vk_ads` в `_ALL_TABLES` (refresh_powerbi.py) — иначе refresh упадёт «нет таблицы в датасете». Схема колонок — ниже в разделе «Схема fact_vk_ads». |
| `yandex_direct_minus_snapshot` | (растёт, ~1 строка/РК/день) | ⏳ **pending до публикации датасета**: снапшоты минус-фраз Яндекс.Директ (campaign+groups+наборы). Гранула: `"date"`×login×campaign_id. Колонки: minus_in_campaign, minus_in_groups, minus_in_sets, minus_total, has_minus, check_ok, **block** (`'tp2'`/`'tp4'`/`'прочее'` — вычисляется из campaign_name, маркер BLOCK_COL_2026-06-17). `--block` управляет сканированием API, в таблицу пишется атрибут block. Заполняется step14 (ночной пайплайн). **`_ALL_TABLES` (refresh_powerbi.py) — добавить ТОЛЬКО ПОСЛЕ того как пользователь создаст вкладку/датасет**. `refresh_powerbi.py` НЕ трогать до этого (TODO). |
| `v_yandex_direct_minus_delta` | (VIEW) | ⏳ **pending до публикации датасета**: VIEW поверх `yandex_direct_minus_snapshot` с LAG-динамикой (minus_total_prev, delta, dynamics: первый замер/добавили/СНЯЛИ/без изменений). PARTITION BY login, campaign_id ORDER BY "date". В PBI рекомендуется Import как виртуальная таблица. **`_ALL_TABLES` (refresh_powerbi.py) — добавить ТОЛЬКО ПОСЛЕ создания вкладки**. |

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

1. `pipeline_powerbi.py` → пересборка `big_analytics_*` (`pipeline.main()`).
2. `refresh_powerbi(tables=_ALL_TABLES)` → OAuth client_credentials в Azure AD,
   `TakeOver` датасета, PATCH PostgreSQL-credentials, POST `/refreshes` с объектами
   `[{table: t} for t in _ALL_TABLES]`, затем polling статуса (до 60 мин).
3. Режим всех таблиц в модели — **Import** (не DirectQuery), без incremental refresh policy.
4. Конфиг PBI Service (`workspace_id`, `dataset_id`, `client_id/secret`, `tenant_id`) —
   в `~/.secret/.env` (powerbi-конфиг через `loader.py`; ⚠️ `tokens.json` не существует).
   **Никогда не хардкодить.**

Локальная модель/отчёт (PBIP): `Documents/креативы виктори/.../Большая аналитика_v00.SemanticModel`
+ `Большая аналитика_v00.Report` (живая file-based модель; облачный датасет — `Большая аналитика_v00`
в воркспейсе `Victory Analytics`).

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

**Текущий `_ALL_TABLES` (refresh_powerbi.py ~193-200):** big_analytics_full, Dim_Date,
Dim_Campaign, Dim_AdGroup, Dim_Site, analytics_report_placement, direct_history,
check_utm_fuck_direct, yandex_direct_korrektirovki, yandex_direct_404_errors,
yandex_direct_return_commission_report, pixel_score, yandex_direct_cookie_analytics_website_pages.

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
| yandex_direct_return_commission_report | `public.yandex_direct_return_commission_report` (PK id) | 46k | TABLE | целиком |
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
