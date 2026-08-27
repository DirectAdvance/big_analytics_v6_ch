# big_analytics_v6_ch — ClickHouse-пайплайн аналитики

`big_analytics_v6_ch` — ClickHouse-контур миграции `big_analytics_v5`.
Локально его можно запускать из этого репозитория; рабочий дневной контур стоит в cron на Victory
через `~/venv-v6/bin/python3 ~/big_analytics_v6_ch/cron_run.py` и пишет в Yandex Cloud ClickHouse:
`raw_data` — сырьё, `reference_data` — справочники, `ad_analytics` — рабочие таблицы и витрины. Victory
`~/big_analytics_v5/` — это отдельный production-контур v5 на PostgreSQL; команды v5
не являются командами запуска v6.

## Запуск

```bash
.venv/bin/python3 pipeline.py                # полный прогон: 29 шагов (0…900), ~31–32 мин
.venv/bin/python3 pipeline.py --from-step=3  # с шага 3 до конца
.venv/bin/python3 pipeline.py --only-step=0  # только один шаг
```

> ⚠️ `--from-step=` выше step3 после успешного прогона не работает: шаг 148
> `cleanup_wide_intermediates` штатно удаляет `big_analytics_sources`, и step11 падает на
> `UNKNOWN_TABLE`. Перепрогон любого пост-step3 шага = полный прогон.

> Карта «шаг → папка» в формате таблицы — единый источник [`PIPELINES.md`](PIPELINES.md#steps-map)
> (шаги 0–13 с временами; там же 3 пайплайна и расписание). Ниже — текстовое описание шагов 0–9.

## Шаги пайплайна

**Шаг 0 — ClickHouse preflight** (`step0_sync_local`)
Ничего не копирует из PostgreSQL/v5. Проверяет, что в ClickHouse уже есть обязательные
`raw_data.*` факты, `reference_data.*` справочники и CH-managed manual inputs в `ad_analytics.*`. Если источник
отсутствует или критически пустой, пайплайн падает до тяжёлых downstream-шагов.

---

**Шаг 1 — RAW в ClickHouse** (`step1_load_raw`)
Пересоздаёт `ad_analytics.raw_yandex`, `raw_leads`, `raw_calls`, `raw_domains`,
`raw_perform_leads` из `raw_data.*` через ClickHouse `MergeTree`/`VIEW`-слой.
`raw_yandex` сохраняет `domain` из `raw_data.yandex_direct_report_rows`; это обязательная ось
для сверки расхода по сайтам. Это не PostgreSQL `UNLOGGED`.

---

**Шаг 2 — ClickHouse prepare** (`step2_indexes`)
В v6 не создаёт PostgreSQL-индексы и не запускает `ANALYZE`; шаг оставлен как
совместимый подготовительный этап для ClickHouse RAW-слоя.

---

**Шаг 3 — Сборка источников** (`step3_build_sources`)
Собирает источниковые таблицы:
- `big_analytics_direct` — Яндекс.Директ (расходы + лиды)
- `big_analytics_seo` — SEO-трафик из Метрики
- `big_analytics_pixel` — пиксельные данные
- `big_analytics_reviews` — отзывы. 🔌 **Единственный живой мост в PostgreSQL:**
  `_fetch_reviews_rows_from_postgres` читает `yandex_direct_raw.yandex_direct_reports_reviews`
  и `yandex_direct_account_reviews` прямо с Victory PG, потому что их нет в `raw_data`.
- `big_analytics_crop_targeting` — посевы (в v6 сюда же схлопнуты `telegram` / `social_посевы`
  / `vk_zero`, которые в v5 были отдельными `_source_table`)

Для строк расхода Директа домен берётся в порядке: домен лида → `raw_yandex.domain` →
fallback из `reference_data.gsheet_sites`. Fallback не должен перетирать raw-домен, иначе расход
переезжает между сайтами одного логина.

> ⚠️ Вьюха `ad_analytics.big_analytics_reviews` сейчас отдаёт **0 строк**: она пересоздаётся
> шагом 148 с фильтром `_source_table IN ('reviews')`, а step3 пишет тег
> `direct_account_reviews`. Строки в факте есть (4 996 / 1 041 642.40 ₽) —
> см. `KNOWN_ISSUES.md` #41.

---

**Шаг 4 — Статусы кампаний** (`step4_campaign_status`)
В v6 это чистая ClickHouse VIEW, без кук и без Grid API: `prefetch_statuses()` — no-op для
совместимости (сигнатура унаследована от v5-оркестраторов), `run()` строит
`ad_analytics.campaign_status` / `campaign_status_v` из `reference_data.direct_campaigns` с
проверкой активного авто-аккаунта в `reference_data.gsheet_sites`.

---

**Шаг 5 — Сборка пикселя** (`step5_build_pixel`)
Гибридный пиксель → `big_analytics_pixel`: до `2026-06-03` строки берутся из
`raw_data.leads_all` по legacy-конфигу `local_pixel_config`/`local_pixel_price_history`,
с `2026-06-03` — из `reference_data.victory_answers FINAL` (`product='пиксель'`).
Для reference-строк статусы подтягиваются из `raw_data.leads_all` по телефону и месяцу.

> ⚠️ **UTM-аудит** (`check_utm` / `check_utm_fuck_direct`) — это НЕ дневной шаг 5.
> Он уехал в `step_cron_night/step13_utm_direct_audit/` и запускается ночным
> пайплайном (`10 18 * * *` UTC = 23:10 Екб). Подробности — [`PIPELINES.md`](PIPELINES.md).

---

**Шаг 6 — Сборка общей таблицы** (`step6_build_full`)
UNION ALL всех источников → `big_analytics_full`.
Подтягивает звонки и копирует `campaign_status` из таблицы `campaign_status`.

---

**Шаг 7 — Финализация** (`step7_finalize`)
Финализирует ClickHouse-таблицы и служебные проверки. PostgreSQL-операции
`UNLOGGED → LOGGED`, `VACUUM ANALYZE` и btree-индексы относятся к v5/legacy, не к
активному v6 ClickHouse-контуру.

---

**Шаг 8 — Статистика** (`step8_stats`)
Считает итоговую статистику по таблицам ClickHouse, выполняется последним из содержательных
шагов перед `verify`. **Telegram не отправляет** — только `logger.info` (см.
`step8_stats/CLAUDE.md`). Отправку делает standalone `funnel_drift_snapshot.py`, которого нет
в `pipeline.py::STEPS`.

---

**Шаг 9 — История изменений Директа** (`step9_direct_history`)
В v6 это ClickHouse snapshot-view, а не старый GraphQL-журнал. `prefetch_history()` — no-op для
совместимости; `run()` строит `ad_analytics.yandex_direct_history` из `reference_data.direct_campaigns`
и обогащает `директолог` / `domain` / `salon` через `reference_data.gsheet_sites`.

---

**Шаг 10 — Посевы** (`step10_crop_targeting`)
Посевы Telega.in API (≥ май 2026) + Google Sheets (< май) + VK/MAX →
`big_analytics_crop_targeting` → доливка в `big_analytics_full`.

---

**Шаг 11 — Атрибуция пикселя** (`step11_pixel_score`)
Распределяет пиксель-воронку салона по цепочке салон → домен → кампания по взвешенному CR.
Результат: `pixel_score` (PBI) + `big_analytics_pixel_score`.
С 2026-08-15 пиксель **разведён по осям** (убран двойной счёт, дубль был 127 554 695.53 ₽):

| ось | `_source_table` | `источник` | строк |
|---|---|---|---:|
| По дате заявки | `pixel` | `Пиксель` | 62 049 |
| По дате визита | `pixel` | `Пиксель` | 30 019 |

⚠️ Меры и визуалы PBI должны фильтровать пиксель только как `источник='Пиксель'`.

---

**Шаг 12 — Проверка CRM-маппингов** (`step12_proverka_big_analytics`)
Проверки качества. Telegram не отправляет.

> ⚠️ **Дневной `pipeline.py` не шлёт в Telegram ничего — ни успех, ни падение.** Проверено
> 2026-08-16: ни один шаг из `STEPS` не вызывает отправку, падение шага только пишется в
> `logger.exception` и в `data_quality_log`. Уведомления есть только у ночного
> `step_cron_night/pipeline_night.py` (ошибки + отчёт) и у standalone-скриптов
> (`watch_pipeline.py`, `funnel_drift_snapshot.py`, `yandex_direct_checking_report`).
> Дневной cron шлёт lifecycle-уведомления из `cron_run.py`: старт/финиш pipeline и
> старт/финиш Power BI refresh, если включён `BA6_POWERBI_REFRESH=1`.
> Refresh запускается только после PASS шага 900. BI golden блокирует pipeline и refresh при
> потере авто-расхода raw→BI, потере расхода фидов raw→BI, расхождении воронки
> request/arrival с `pbi_big_analytics_full`, скачке закрытых месяцев больше 4% по продажам/CPL
> продажи или появлении авто-метрик без специалиста/города/салона.
> Сам канал живой: тестовая отправка 2026-08-16 доставлена и с Мака, и с Victory.

---

**Шаг 13 — Воронка по дате визита** (`step13_arrival`)
Пересчёт воронки по `arrival_date` (дата визита) → `big_analytics_full_arrival`.
Только `priezd + prodazhi`, фильтр `direction='Авто'`, без cost/clicks.

> ⚠️ В корне `pipeline.py` **шаг 13 = `step13_arrival`**. Папка `step13_utm_direct_audit`
> с UTM-аудитом живёт в `step_cron_night/` и относится к **ночному** пайплайну.

## Статус star-cutover

В v6 star-слой строится в ClickHouse шагом `star_refactor.build_star` (`pipeline.py`
step 145). Актуальные инварианты и открытые расхождения v5↔v6 см. в
[`GOLDEN_BASELINE.md`](GOLDEN_BASELINE.md), [`KNOWN_ISSUES.md`](KNOWN_ISSUES.md) и
[`RAW_DIFF_FINDINGS.md`](RAW_DIFF_FINDINGS.md).

Активные выходы v6 находятся в ClickHouse `ad_analytics`: `fact_big_analytics`,
`big_analytics_full`, `big_analytics_full_arrival`, `big_analytics_unified`,
`Dim_*`, spend/feed/PBI-compat витрины. Секция исторического PostgreSQL `public.*`
из v5 оставлена в legacy-доках и не является инструкцией для v6.

## Таблицы результата

| Таблица | Описание |
|---------|----------|
| `big_analytics_full` | Основная аналитическая таблица (все источники + статусы) |
| `campaign_status` | Справочник статусов кампаний Яндекс.Директ |
| `data_quality_log` | Лог выполнения шагов с метриками |

## Отдельные витрины

`direct_feed_funnel/` — ClickHouse-витрина товарных фидов Директа. Активный шаг:
`direct_feed_funnel.build`, вызывается из корневого `pipeline.py` как step 144. Физический факт —
`fact_direct_feed_funnel_light` строится из `raw_data.direct_feed_report_rows` и
`raw_data.direct_cookie_feed_urls`; старое имя `fact_direct_feed_funnel` оставлено как
compatibility view.
Старые v5 helper-скрипты перенесены в `archive/postgres_legacy_2026_07_31/`.
Подробности: [`direct_feed_funnel/README.md`](direct_feed_funnel/README.md).

## Статусы прогона

- `ok` — основной факт и дополнительные витрины обновились.
- `degraded` — основной `fact_big_analytics` собран, но одна из spend-витрин
  (`fact_region_spend`, `fact_adformat_spend`, `fact_criterion_spend`) не обновилась.
  Такой прогон не роняет пайплайн, но не считается полностью успешным для Power BI,
  если отчёты используют эти таблицы.
- `err` — критичный шаг не собран, публикацию данных считать неуспешной.

## Зависимости

- Python 3.10+
- `clickhouse-connect`, requests, urllib3; `psycopg2` — только для моста отзывов в step3
- Доступ к ClickHouse Yandex Cloud `rc1b-q7j2ie10fdverqrk.mdb.yandexcloud.net:8443`
  (`raw_data`/`reference_data` — чтение, `ad_analytics` — запись) через `load_db('victory_clickhouse')`
- Read-only доступ к Victory PostgreSQL `ad_analytics_bi` — только для отзывов (step3)
- Куки Яндекс.Директ нужны только шагу 139 `direct_placement_links` (Grid API для ссылок площадок);
  step4/step9 их больше не используют. Автообновление — glavpotok.ru; домашний сервер
  `http://192.168.0.202:8765/cookies` — устаревший fallback (см. `COOKIES.md`)

---

## Power BI

**Файл (Mac, актуальный):** `/Users/semen/Documents/Отчеты_victory_Powerbi/Большая аналитика_admin_ch/Большая аналитика_admin.pbip`
(встроенная модель admin; user-отчёт лежит рядом в `Большая аналитика_user_ch`).
_Legacy Windows-путь `C:\Users\Mi\Desktop\креативы виктори\готовые таблицы\…` устарел — не использовать._

BA6 Power BI работает только по нише `Авто`. Не-авто ниши не добавляются в BI-измерения,
страницы и срезы и не рассматриваются как часть отчёта.

### VK Ads domain grain

`star_refactor.build_star.build_vk_ads_fact` собирает `fact_vk_ads` на доменном зерне:
статистика и лиды несут `domain`/`site_key`, а заявки и визиты VK Ads попадают в факт
только при совпадении с баннером из `banner_dim`. PBI-compat витрина экспортирует эти
поля для связи `fact_vk_ads.site_key -> Dim_Site.site_key`; без пересборки BA6 pipeline
Power BI продолжит показывать старое распределение VK Ads по доменам.

В общей витрине `pbi_big_analytics_full` расход VK Ads приходит через cost-overlay step10.
Эти строки тоже обязаны получать site dimensions из
`reference_data.vk_ads_agency_clients -> reference_data.gsheet_sites`, иначе Power BI покажет
расход в `без домена`, хотя отдельная `fact_vk_ads` уже разложена по доменам.

Структура (POSIX-разделители на Mac):
- `Большая аналитика_v00.Report/definition/pages/<id>/page.json` — страницы (filterConfig + visuals)
- `Большая аналитика_v00.Report/definition/pages/<id>/visuals/<id>/visual.json` — визуалы (pivotTable, slicer, shape, actionButton)
- `Большая аналитика_v00.SemanticModel/definition/tables/*.tmdl` — таблицы модели (типы колонок, formatString, partitions)
- `Большая аналитика_v00.SemanticModel/definition/relationships.tmdl` — связи

### Алиас в чате

Если пользователь пишет «**добавь в повер биай**» / «**в повер биай**» / «**пбикс**» / «**pbi**» — речь про этот файл (`Большая аналитика_v00.pbip`).

### Правило: вычисления — в БД, не в Power BI

Все тяжёлые вычисления (агрегации, JOIN, оконные функции, форматирование дат, бизнес-логика) выполняются на стороне ClickHouse (в шагах пайплайна или в SQL-DDL источника).

> ⚠️ **Паритет с v5.** BA6 PBIP уже читает ClickHouse и feed-слой восстановлен, но это не плоская
> копия v5: главная витрина работает через star-измерения, BI ограничен нишей `Авто`, а прямой
> `raw_new_*` хвост остался только в `yandex_direct_accounts_human_cyborgs`.
> Полный разбор — [`PBI_TABLES.md`](PBI_TABLES.md) §0.

Power BI используется **только** для:
- Простых агрегаций: `Sum/Min/Max/Count/Avg` по готовым колонкам
- Слайсеров и фильтров страницы (`filterConfig.filters`)
- Условного форматирования (цвета по значению)
- Drill-through и tooltip-страниц

**Запрещено в PBI:**
- Сложные DAX-меры с `CALCULATE/FILTER/TREATAS` поверх миллионных таблиц
- `NativeVisualCalculation` с DAX-выражениями (`FORMAT`, `DATEDIFF`, `IF` и т.д.) — вместо этого добавлять готовую колонку в БД
- Power Query трансформации сверх простого `RenameColumns` / выбора partition

**Если нужна новая колонка:**
1. Добавить её на стороне БД (в соответствующем шаге пайплайна или в SQL-источнике таблицы)
2. Запустить пайплайн → колонка появится в ClickHouse `ad_analytics`
3. В семантической модели Power BI → добавить колонку в `<table>.tmdl` (тип данных + formatString)
4. В визуале использовать прямую ссылку на колонку через `Aggregation` (`Function: 0/3/4`) или как dimension

### Формат дат в визуалах

В TMDL для колонок типа `dateTime` ставить `formatString: dd.MM.yyyy`, чтобы агрегации `Min/Max` отображались как `01.02.2026`. Не использовать `FORMAT(...)` в DAX-выражениях.

### Backup при массовых правках

Перед массовыми изменениями страниц/визуалов делать backup папки `definition\pages` (например, `pages_backup_YYYYMMDD_HHMMSS`).
