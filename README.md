# big_analytics_v6_ch — Пайплайн аналитики (форк big_analytics_v5, миграция на ClickHouse)

> ⚠️ Код пока идентичен `big_analytics_v5` и работает на PostgreSQL (та же прод-БД `ad_analytics_bi`
> @ Victory) — CH-миграция ещё не выполнена, см. [`PLAN.md`](PLAN.md). v6_ch не задеплоен на Victory
> отдельно от v5 (путь `/home/semen_vi/big_analytics_v5` ниже — это код v5, для справки).

## Запуск

```bash
python3 pipeline.py                   # все шаги 0–10 + 13 (+ доп. скрипты, step8 последним)
python3 pipeline.py --from-step=3     # с шага 3 до конца
python3 pipeline.py --only-step=0     # только один шаг
```

> Карта «шаг → папка» в формате таблицы — единый источник [`PIPELINES.md`](PIPELINES.md#steps-map)
> (шаги 0–13 с временами; там же 3 пайплайна и расписание). Ниже — текстовое описание шагов 0–9.

## Шаги пайплайна

**Шаг 0 — Синхронизация локальных копий** (`step0_sync_local`)
Копирует данные из `ad_analytics` (источник, только чтение) в `ad_analytics_bi` (целевая БД).
- `leads_local` — лиды с JOIN domains: инкрементально по `updated_at` (UPSERT)
- `yandex_local` — статистика Яндекс.Директ: инкрементально (UPSERT)
- Остальные справочники (аккаунты, бренды, домены, seo и др.) — полная замена (TRUNCATE + INSERT)

После завершения шага 0 автоматически запускаются фоновые потоки:
- получение статусов кампаний (шаг 4)
- история изменений Директа (шаг 9)

---

**Шаг 1 — Загрузка в RAW** (`step1_load_raw`)
Перекладывает данные из локальных копий в UNLOGGED RAW-таблицы.
RAW-таблицы не журналируются — быстрая запись на время сборки.

---

**Шаг 2 — Индексы на RAW** (`step2_indexes`)
Создаёт индексы на RAW-таблицах и запускает `ANALYZE`.
Готовит данные для быстрой сборки источников.

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
- Переводит RAW-таблицы из UNLOGGED → LOGGED
- `VACUUM ANALYZE` на основных таблицах
- Создаёт финальные индексы на `big_analytics_full`

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

## Статус star-cutover (актуально на 2026-06-11) — ✅ ЗАВЕРШЁН, схема консолидирована в `public`

Миграция с денормализованной (`big_analytics_full`) на **star-схему** ЗАВЕРШЕНА.
⚠️ Звезда **консолидирована в схему `public` (2026-06-10)** — отдельной схемы `star` БОЛЬШЕ НЕТ
(`build_star.build_schema()` — no-op). Полный план/эталоны — [`STAR_REFACTOR_BRIEF.md`](STAR_REFACTOR_BRIEF.md).

**Звезда в схеме `public`** (БД `ad_analytics_bi`, факты проверены 2026-06-11):
- `public.fact_big_analytics` — лёгкий факт (ключи + меры + `атрибуция`), ~3.82M строк (vs unified 5.46 ГБ).
- `public.arp_fact` — **VIEW** над `analytics_report_placement` (не TABLE; чистая проекция, −3.4 ГБ).
- Conformed измерения: `public."Dim_Site"` (по `domain`), `public."Dim_Campaign"` (по `CampaignId`,
  16 461 строк, cs/pm заполнены у 5905), `public."Dim_AdGroup"` (по `AdGroupId`), `public."Dim_Date"` (по `Date`).
- Без потерь: расход/продажи совпали 1:1 с golden (25 422 774.00 / 47, проверено 2026-06-11).
- PBI-модель перепубликована пользователем на `public.fact_big_analytics` + dim. Если в TMDL остался
  `Schema="star"` → refresh упадёт «key didnt match» (см. [`KNOWN_ISSUES.md`](KNOWN_ISSUES.md) #15).

**Ещё на старой архитектуре (живой прод, НЕ тронут):**
- `big_analytics_full` / `big_analytics_full_arrival` / `big_analytics_unified` (источник правды
  пайплайна) и `analytics_report_placement` — материализуются по-прежнему.
- **Пайплайн НЕ переключён на star:** `pipeline.py` / `fast_pipeline.py` /
  `refresh_powerbi.py` (`_ALL_TABLES`) пока собирают и обновляют старые денормализованные витрины.
  Интеграция star в пайплайн делается после приёмки PBI-модели.

> Эта секция — навигационная. Детали (колонки факта, связи, перенацеливание полей) —
> только в [`STAR_REFACTOR_BRIEF.md`](STAR_REFACTOR_BRIEF.md), не дублировать.

## Таблицы результата

| Таблица | Описание |
|---------|----------|
| `big_analytics_full` | Основная аналитическая таблица (все источники + статусы) |
| `campaign_status` | Справочник статусов кампаний Яндекс.Директ |
| `data_quality_log` | Лог выполнения шагов с метриками |

## Отдельные витрины

`direct_feed_funnel/` — воронка Яндекс.Директа по фидам.

Запуск:

```bash
cd /home/semen_vi/big_analytics_v5
/home/semen_vi/venv/bin/python3 -m direct_feed_funnel.pipeline
```

Итоговая таблица: `public.fact_direct_feed_funnel`.
Ключ соединения расходов и лидов: `Date|CampaignId|AdGroupId|feed_key`, для `tp6`/`tp7` — `Date|CampaignId|feed_key`.
Реальный URL фида берётся отдельным cookie/web-api шагом:
`python3 -m direct_feed_funnel.fetch_feed_urls_cookie --all-logins --apply`.
В витрине доступны `feed_url` и `feed_url_key`; `feed_url_key` — последняя часть URL с `.xml`.
При появлении новых фидов порядок такой: сначала `fetch_feed_urls_cookie --all-logins --apply`,
затем `python3 -m direct_feed_funnel.pipeline`, затем копирование
`public.fact_direct_feed_funnel` на localhost для Power BI. Большой `pipeline_powerbi.py`
сам URL фидов сейчас не обновляет.
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
