# big_analytics_v6_ch — ClickHouse-пайплайн аналитики

`big_analytics_v6_ch` — локальный Mac-контур миграции `big_analytics_v5` на ClickHouse.
Он запускается вручную из этого репозитория и пишет в Yandex Cloud ClickHouse:
`raw_data` — сырьё, `ad_analytics` — рабочие таблицы и витрины. Victory
`~/big_analytics_v5/` — это отдельный production-контур v5 на PostgreSQL; команды v5
не являются командами запуска v6.

## Запуск

```bash
python3 pipeline.py                   # все шаги 0–10 + 13 (+ доп. скрипты, step8 последним)
python3 pipeline.py --from-step=3     # с шага 3 до конца
python3 pipeline.py --only-step=0     # только один шаг
```

> Карта «шаг → папка» в формате таблицы — единый источник [`PIPELINES.md`](PIPELINES.md#steps-map)
> (шаги 0–13 с временами; там же 3 пайплайна и расписание). Ниже — текстовое описание шагов 0–9.

## Шаги пайплайна

**Шаг 0 — ClickHouse preflight** (`step0_sync_local`)
Ничего не копирует из PostgreSQL/v5. Проверяет, что в ClickHouse уже есть обязательные
`raw_data.*` источники и CH-managed manual inputs в `ad_analytics.*`. Если источник
отсутствует или критически пустой, пайплайн падает до тяжёлых downstream-шагов.

---

**Шаг 1 — RAW в ClickHouse** (`step1_load_raw`)
Пересоздаёт `ad_analytics.raw_yandex`, `raw_leads`, `raw_calls`, `raw_domains`,
`raw_perform_leads` из `raw_data.*` через ClickHouse `MergeTree`/`VIEW`-слой.
Это не PostgreSQL `UNLOGGED`.

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
- `big_analytics_telegram` — Telegram Ads
- `big_analytics_reviews` — отзывы (из yandex_direct_reports_reviews)
- `big_analytics_crop_targeting` — посевы

---

**Шаг 4 — Статусы кампаний** (`step4_campaign_status`)
Двухфазная работа:
- **Фаза A (фон, с шага 0)** — запрашивает статусы кампаний через Yandex Direct Grid API,
  используя куки с домашнего сервера. Только для активных аккаунтов (за 60 дней).
- **Фаза B (шаг 4)** — ждёт завершения фазы A, строит таблицу `campaign_status`,
  патчит колонку `campaign_status` в `big_analytics_direct`.

---

**Шаг 5 — Сборка пикселя** (`step5_build_pixel`)
Конфиг пикселей (Google Sheets `local_pixel_config`) → `big_analytics_pixel`.
Лиды `utm_source LIKE 'victory_%'`, стоимость заявки из конфига.

> ⚠️ **UTM-аудит** (`check_utm` / `check_utm_fuck_direct`) — это НЕ дневной шаг 5.
> Он уехал в `step_cron_night/step13_utm_direct_audit/` и запускается ночным
> пайплайном (cron 03:00 МСК). Подробности — [`PIPELINES.md`](PIPELINES.md).

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

**Шаг 8 — Статистика и отчёт** (`step8_stats`)
Считает итоговую статистику по всем таблицам.
Отправляет финальный Telegram-отчёт (выполняется последним).

---

**Шаг 9 — История изменений Директа** (`step9_direct_history`)
Двухфазная работа:
- **Фаза A (фон, с шага 0)** — инкрементально загружает историю изменений по активным
  логинам (status='Контекст активно') через внутренний API direct.yandex.ru.
- **Фаза B (шаг 9)** — ждёт завершения фазы A, обогащает `direct_history` данными
  директолога, домена, салона из `local_gsheet_sites`.

---

**Шаг 10 — Посевы** (`step10_crop_targeting`)
Посевы Telega.in API (≥ май 2026) + Google Sheets (< май) + VK/MAX →
`big_analytics_crop_targeting` → доливка в `big_analytics_full`.

---

**Шаг 11 — Атрибуция пикселя** (`step11_pixel_score`)
Распределяет пиксель-воронку салона по цепочке салон → домен → кампания по взвешенному CR.
Результат: `pixel_score` (PBI) + строки в `big_analytics_full` (`_source_table='пиксель_атрибуц'`).

---

**Шаг 12 — Проверка CRM-маппингов** (`step12_proverka_big_analytics`)
Проверки качества → Telegram-отчёт.

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

`direct_feed_funnel/` — ClickHouse-витрина фидовой воронки. Активный шаг:
`direct_feed_funnel.build`, вызывается из корневого `pipeline.py` как step 144.
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
- psycopg2, requests, urllib3
- Доступ к `ad_analytics` (чтение) и `ad_analytics_bi` (запись)
- Куки Яндекс.Директ на домашнем сервере: `http://192.168.0.202:8765/cookies`

---

## Power BI

**Файл (Mac, актуальный):** `/Users/semen/Documents/креативы виктори/Большая аналитика_admin/Большая аналитика_v00.pbip`
(встроенная модель `v00` — редактирует агент `pbip_editor`).
_Legacy Windows-путь `C:\Users\Mi\Desktop\креативы виктори\готовые таблицы\…` устарел — не использовать._

Структура (POSIX-разделители на Mac):
- `Большая аналитика_v00.Report/definition/pages/<id>/page.json` — страницы (filterConfig + visuals)
- `Большая аналитика_v00.Report/definition/pages/<id>/visuals/<id>/visual.json` — визуалы (pivotTable, slicer, shape, actionButton)
- `Большая аналитика_v00.SemanticModel/definition/tables/*.tmdl` — таблицы модели (типы колонок, formatString, partitions)
- `Большая аналитика_v00.SemanticModel/definition/relationships.tmdl` — связи

### Алиас в чате

Если пользователь пишет «**добавь в повер биай**» / «**в повер биай**» / «**пбикс**» / «**pbi**» — речь про этот файл (`Большая аналитика_v00.pbip`).

### Правило: вычисления — в БД, не в Power BI

Все тяжёлые вычисления (агрегации, JOIN, оконные функции, форматирование дат, бизнес-логика) выполняются на стороне Postgres (в шагах пайплайна `step1`–`step9` или в SQL-DDL источника).

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
2. Запустить пайплайн → колонка появится в `ad_analytics_bi`
3. В семантической модели Power BI → добавить колонку в `<table>.tmdl` (тип данных + formatString)
4. В визуале использовать прямую ссылку на колонку через `Aggregation` (`Function: 0/3/4`) или как dimension

### Формат дат в визуалах

В TMDL для колонок типа `dateTime` ставить `formatString: dd.MM.yyyy`, чтобы агрегации `Min/Max` отображались как `01.02.2026`. Не использовать `FORMAT(...)` в DAX-выражениях.

### Backup при массовых правках

Перед массовыми изменениями страниц/визуалов делать backup папки `definition\pages` (например, `pages_backup_YYYYMMDD_HHMMSS`).
