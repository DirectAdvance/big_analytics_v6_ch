# PLAN.md — big_analytics_v6_ch (миграция пайплайна на ClickHouse)

> Дата: 2026-07-30. Статус: Этапы 0–5 выполнены в v6_ch; полный ClickHouse-прогон замерен.
> Источник: анализ `work/big_analytics_v5/` (CLAUDE.md, PIPELINES.md, PBI_TABLES.md).
> Ключевые решения 2026-07-30 (§3а, §5а, §6): хост исполнения (Yandex Cloud), UPSERT-паттерны
> по категориям таблиц, доверие raw_data (выборочная перепроверка).

---

## 0. Итог реализации 2026-07-30

- [x] Полный pipeline v6_ch переведён на ClickHouse и работает поверх существующего `raw_data`.
      CRM-данные в ClickHouse не перезаливаются.
- [x] Дорогие вставки переписаны на shadow-table rebuild + помесячные batch `INSERT SELECT`, чтобы
      не собирать большие промежуточные наборы одной монолитной операцией.
- [x] Полный прогон замерен фактом: `/usr/bin/time -p .venv/bin/python3 pipeline.py`,
      `run_id=3407f9606419`, `real 1037.44` сек (≈17м17с), `user 2.28`, `sys 0.33`.
- [x] После оптимизации BI/view-контракта и default pipeline без maintenance-only шагов:
      `run_id=f4230bdb9714`, `real 898.26` сек (≈14м58с). После слияния traffic source tables
      проверочный downstream-прогон `--from-step=3`: `run_id=d6eeb28dcbda`, `real 721.91` сек, PASS.
- [x] Финальная проверка `data_check/verify_big_analytics.py --full` внутри pipeline завершилась PASS:
      нулевые нарушения по дате, `source`, вложенности воронки и сверкам `unified/fact`.
- [x] PBI-совместимые ClickHouse-таблицы созданы: `pbi_big_analytics_full`, `pixel_score`,
      `arp_fact`, `arc_fact`, `arf_fact`, `dim_criterion` и пустые compatibility-таблицы для
      старых TMDL-зависимостей без источников в `raw_data`.
- [x] Все объекты, которые читает Power BI, вынесены в `bi_*` ClickHouse VIEW; admin PBIP
      переключён на `Kind="View"`. Materialized compatibility-дубли заменены обычными VIEW,
      чтобы не занимать диск.
- [x] Traffic source tables слиты: вместо отдельных physical
      `big_analytics_direct/seo/pixel/crop_targeting/reviews` теперь один `big_analytics_sources`;
      старые имена оставлены только как compatibility VIEW.
- [x] PBIP-копии подготовлены в `/Users/semen/Documents/Отчеты_victory_Powerbi`:
      `Большая аналитика_admin_ch` подключён к ClickHouse, `Большая аналитика_user_ch` подключён
      к локальной admin semantic model. В Power BI Desktop визуально не открывалось.

Ключевые итоговые объёмы:
`raw_yandex=23,681,851`, `raw_leads=1,067,794`, `raw_calls=65,437`,
`big_analytics_sources=5,053,254`, `big_analytics_full=5,118,691`, `big_analytics_unified=5,349,863`,
`fact_big_analytics=5,349,863`, `arp_fact=12,923,840`, `arf_fact=12,194,556`.

---

## 1. Что такое v6_ch

Параллельная версия `big_analytics_v5`, адаптированная под **ClickHouse** (Yandex Cloud,
подключение `clickhouse_avto`) вместо PostgreSQL (`ad_analytics_bi` @ Victory VPS).
Логика ETL и воронка статусов не меняются — меняется СУБД и паттерны работы с ней.

Целевое развёртывание: локальный запуск Python-пайплайна против Yandex Cloud ClickHouse
`raw_data`/`ad_analytics`. Продовый хост исполнения можно выбрать отдельно.

---

## 2. Структура каталогов: v5 → v6_ch

### 2.1. Переносится 1:1 (СУБД-независимый код)

| Папка / файл v5 | Что делает | Статус переноса |
|---|---|---|
| `config/settings.py` | Константы, имена таблиц | 1:1, только имена таблиц под CH |
| `config/tokens.py` | API-ключи Директа, Telegram | 1:1 |
| `config/brand_map.py` | Коды кт → марки авто | 1:1 |
| `config/status_sql.py` | Динамическая генерация SQL воронки | Требует правок (синтаксис CH) |
| `step4_campaign_status/` | Grid API Яндекс.Директ → campaign_status | 1:1 (API-уровень), запись в БД — правки |
| `step9_direct_history/` | История изменений Директа | 1:1 (API-уровень), запись — правки |
| `step_cron_night/metrika_yandex.py` | Раздача грантов счётчиков | 1:1 |
| `step_cron_night/korrektirovki/` | Корректировки ставок из API | 1:1 (API), запись — правки |
| `step_cron_night/direct_account_reviews/` | Отзывы из API Директа | 1:1 (API), запись — правки |
| `step_cron_night/404_errors/` | Инкрементальная 404-проверка | Правки (UPSERT → CH-паттерн) |
| `step8_stats/` | Telegram-отчёт | SQL-запросы под CH |
| `step12_proverka_big_analytics/` | Проверки CRM-маппингов | SQL под CH |
| `data_check/verify_big_analytics.py` | Golden-oracle верификация | SQL под CH, логика та же |
| `refresh_powerbi.py` | PBI dataset refresh | 1:1 (OAuth Azure AD), таблицы те же |
| `pipeline_mutex.py` | Блокировка запуска (flock) | 1:1 |
| Оркестраторы `pipeline.py` / `fast_pipeline.py` / `pipeline_night.py` | Запуск шагов | Правки (DB-пул, степы) |

### 2.2. Требует существенных правок из-за смены СУБД

| Папка / файл v5 | Причина правок |
|---|---|
| `config/db.py` | psycopg2 connection pool → clickhouse-driver / clickhouse-connect |
| `step0_sync_local/` | FDW (Postgres-специфика) → альтернативный механизм синка local_* |
| `step1_load_raw/` | TRUNCATE+INSERT / UPSERT → CH INSERT + дедупликация |
| `step2_indexes/` | Индексы Postgres → ORDER BY / skip indexes / партиционирование CH |
| `step3_build_sources/` | CTAS / CTE / оконные функции → CH-диалект SQL |
| `corrections.py` | Транзакции + UPSERT + VACUUM → CH-паттерны (правки значительные) |
| `step5_build_pixel/` | UPSERT → CH INSERT |
| `step6_build_full/` | UNION ALL CTAS + SET LOGGED → CH INSERT SELECT + движок таблицы |
| `step7_finalize/` | SET LOGGED + VACUUM ANALYZE → не применимо в CH (см. п.3) |
| `step10_crop_targeting/` | UPSERT-логика посевов | CH-паттерн дедупликации |
| `step11_pixel_score/` | Дробная атрибуция пикселя (см. инварианты) → хранение Decimal в CH |
| `step13_arrival/` + `build_unified.py` | CTAS + UNION → CH INSERT SELECT |
| `star_refactor/build_star.py` | DROP+CTAS звёздных таблиц → CH движки + пересборка |
| `direct_feed_funnel/` | Составные ключи, UPSERT, CTE | CH-паттерны |
| `step_cron_night/build_spend_daily.py` | FDW + staging UNLOGGED + DROP+CTAS | CH-аналог staging |
| `region_spend/`, `adformat_spend/`, `criterion_spend/` | DROP+CTAS датамартов | CH INSERT OVERWRITE |
| `sales_attribution/` | SQL-атрибуция продаж | CH-диалект |

### 2.3. Новые файлы, специфичные для v6_ch

| Файл | Назначение |
|---|---|
| `config/ch_db.py` | Подключение к ClickHouse (clickhouse_avto) |
| `config/ch_settings.py` | CH-специфичные константы (движки, партиционирование) |
| `migrations/` | DDL-скрипты создания схемы CH (пустая папка, заполнить на этапе 1) |

---

## 3. Ключевые архитектурные отличия Postgres → ClickHouse

Список «требует решения» — конкретные паттерны уточняет Семён.

### 3.1. Нет полноценных транзакций и UPSERT

Postgres: `INSERT ... ON CONFLICT DO UPDATE`, атомарные транзакции в `corrections.py`.
ClickHouse: нет `ON CONFLICT`; `ALTER TABLE UPDATE` — мутация (медленная, асинхронная).
Альтернативы: `ReplacingMergeTree` + `FINAL`, дедупликация на SELECT, batch-overwrite.
**Требует решения:** паттерн для каждого шага, где есть UPSERT (step0, step1, corrections).

### 3.2. corrections.py — транзакционная логика

В v5: `corrections.apply()` выполняет rule0..rule4 внутри транзакции с промежуточными
VACUUM (KNOWN_ISSUES #14 — rollback в `_interim_vacuum`). В CH нет VACUUM и нет BEGIN/COMMIT.
Механика корректировок Кудерко (rule1 — 97 946 строк) требует эквивалента.
**Требует решения:** целиком.

### 3.3. UNLOGGED таблицы → нет аналога в CH

В v5: `raw_yandex`, `raw_leads`, `raw_calls`, `raw_domains`, `big_analytics_*_staging` —
UNLOGGED для скорости записи (нет WAL). В CH все таблицы persistentны, но запись в
MergeTree изначально быстрая (append-only, части сливаются фоново).
**Требует решения:** выбор движка для промежуточных таблиц (MergeTree / Memory / Buffer).

### 3.4. Дробная пиксельная атрибуция — инвариант

В v5: ключевой баг — усечение `float` → `int` по строкам. В CH тип `Decimal(18,6)` /
`Float64` — дробность сохраняется, но нужно явно выбрать тип колонки и не делать CAST.
**Требует решения:** тип колонки для атрибуционных весов в схеме CH.

### 3.5. Движки таблиц MergeTree / ReplacingMergeTree / AggregatingMergeTree

Каждая таблица требует осознанного выбора движка + ORDER BY (первичный ключ) +
партиционирования (обычно по дате). Нет правила «один размер для всех».
**Требует решения:** движок для каждой ключевой таблицы (big_analytics_full, fact_*, Dim_*).

### 3.6. FDW (шаг 0) — нет прямого аналога

В v5: `step0_sync_local/` использует FDW (`yandex_direct_manager_reports` и др.) для
синхронизации данных из источников напрямую в Postgres. В CH: встроенные Table Engines
(`PostgreSQL`, `MySQL`, `HTTP`, `JDBC`) или внешний ETL-слой.
**Требует решения:** как получать источниковые данные в CH-слой (прямой CH Table Engine vs
промежуточная копия на файловой системе vs оставить Postgres как staging).

### 3.7. Индексы → ORDER BY + skip indexes

В v5: B-tree индексы, BRIN на `"Date"`, составные индексы на (campaign_id, domain).
В CH: первичный ключ задаётся через ORDER BY (sparse index), skip indexes — опциональны.
Выбор ORDER BY — критично для производительности (нельзя поменять без пересоздания таблицы).
**Требует решения:** ORDER BY для каждой ключевой таблицы.

### 3.8. SET LOGGED / VACUUM ANALYZE / pg_stat_user_tables

В v5: `step7_finalize/` переводит UNLOGGED→LOGGED, запускает VACUUM ANALYZE, читает
`pg_stat_user_tables` для disk-guard (STEP6_DISK_GUARD). В CH — эти операции не нужны
(нет WAL-toggle, нет VACUUM), disk-guard строится через `system.parts`.
**Требует решения:** как строить disk-guard в CH (или убрать как ненужный).

### 3.9. Диалект SQL

CH поддерживает большинство оконных функций и CTE, но есть отличия:
`ILIKE` нет (использовать `lower() LIKE`), `EXTRACT` другой синтаксис, `date_trunc` →
`toStartOfMonth/toStartOfWeek`, `NOW()` → `now()`, `COALESCE` есть, `NULLIF` есть.
Конструкция `INSERT INTO ... SELECT` работает. `CREATE TABLE AS SELECT` — есть.
**Требует решения:** прогон `step3_build_sources/` SQL через CH-совместимость построчно.

### 3.10. Power BI → ClickHouse подключение

В v5: PBI подключается к Postgres через ODBC/JDBC, refresh через Azure AD + API.
В CH: нужен ClickHouse ODBC-драйвер или HTTP-интерфейс. M-запросы в TMDL нужно переписать.
**Требует решения:** способ подключения PBI к CH (ODBC / HTTP connector / локальная реплика).

---

## 4. Этапы работы

### Этап 0. Подготовка и подключение ClickHouse

- [x] Найти/подтвердить реквизиты (2026-07-28): `DB_VICTORY_CLICKHOUSE_*` в `.secret/.env`,
      Yandex Cloud managed CH, host `rc1b-q7j2ie10fdverqrk.mdb.yandexcloud.net:8443`,
      две базы — `raw_data` (сырые источники, уже наполнены, 37 таблиц) и `ad_analytics`
      (наша рабочая БД, пока пустая — сюда пойдёт звёздная схема)
- [x] Загрузчик кредов — существующий `load_db('victory_clickhouse')` в `.secret/loader.py`
      подошёл 1:1 (префикс `DB_VICTORY_CLICKHOUSE_*` уже совпадает с схемой `load_db()`),
      отдельная функция не понадобилась
- [x] Написан `config/ch_db.py` — клиент через `clickhouse-connect`, TLS верифицируется
      явным `ca_cert` (скачивается и кешируется с `storage.yandexcloud.net/cloud-certs/CA.pem`,
      т.к. Yandex Cloud CA не входит в системный trust store)
- [x] Коннект проверен с Мака (`.venv/bin/python3 config/ch_db.py` → ping обеих БД OK).
      Где будет запускаться v6_ch в проде (Victory VPS / Cloud) — открытый вопрос, см. раздел 6
- [x] Зависимости в `work/big_analytics_v6_ch/.venv/` — `clickhouse-connect` установлен
      (⚠️ системный python3 — 3.9 с LibreSSL, deprecation warning от clickhouse-connect;
      подходит для разработки, для прода/Victory стоит проверить версию python)
- [x] MCP `clickhouse-victory` зарегистрирован в `~/.claude.json` (обёртка
      `scripts/mcp_clickhouse_victory.py`, официальный пакет `mcp-clickhouse` через `uvx`;
      там TLS-верификация ОТКЛЮЧЕНА — `mcp-clickhouse` не поддерживает кастомный CA-путь)

### Этап 1. Схема таблиц под ClickHouse (DDL) — ✅ выполнено 2026-07-30 (oleg_programmer)

- [x] Для каждой ключевой таблицы v5 определено: движок, ORDER BY, партиционирование, тип колонок —
      см. `migrations/01_init_schema.sql` (комментарии перед каждой таблицей) и отчёт
      `.claude/sdd/v6-etap1-ddl-report.md`.
- [x] Тип для дробной атрибуции — `Decimal(18,6)`, применён ТОЛЬКО к `fact_big_analytics`/
      `fact_ml_korrektirovki` (единственные факты с реальным step11_pixel_score); остальные факты —
      `Int64` (проверено по исходному SQL v5 — там нет пиксельного дробления, не предположение).
      Тип для дат — `Date` (ClickHouse).
- [x] Создан `migrations/01_init_schema.sql` — DDL 19 объектов (15 MergeTree + 2 ReplacingMergeTree +
      2 парные VIEW с FINAL), применён к `ad_analytics` (реально выполнено, не только написан файл).
- [~] Промежуточные UNLOGGED-аналоги — НЕ созданы в этом файле (не входили в явный список задания;
      относятся к Этапу 2/3 при переносе step1/step3/corrections.py).
- [x] Dim-таблицы (`Dim_Campaign`, `Dim_Site`, `Dim_AdGroup`, `Dim_Date`, `Dim_Adjustment`,
      `Dim_Location`) — движок `MergeTree` (не `Dictionary`, не `ReplacingMergeTree`): они полностью
      пересобираются каждый прогон build_star (как в v5), Dictionary/Replacing тут не нужны.

### Этап 2. Перенос raw_* слоя (step0 + step1 + step2) — ✅ выполнено 2026-07-30 (Codex)

- [x] `step0_sync_local`: FDW/local_* синхронизация заменена на read-only preflight `raw_data`
      в ClickHouse. Источник v6_ch — уже существующий `raw_data`; CRM-данные не перезаливаются.
- [x] `step1_load_raw`: PostgreSQL UNLOGGED/FDW-путь заменён на `clickhouse-connect` DDL
      `DROP TABLE ... SYNC` + `CREATE TABLE ... ENGINE MergeTree AS SELECT` в `ad_analytics`.
      Создаются `raw_yandex`, `raw_leads`, `raw_calls`, `raw_domains`, `raw_perform_leads`.
- [x] `step2_indexes`: PostgreSQL `CREATE INDEX`/`ANALYZE` заменены на `OPTIMIZE TABLE ... FINAL`
      для raw-таблиц; ключи чтения задаются через `MergeTree ORDER BY` в step1.
- [~] `raw_perform_leads`: отдельного источника `raw_data.perform_leads` нет, поэтому на этой
      итерации создаётся пустая совместимая таблица. Это ограничение входного слоя, не попытка
      восстановить perform-логику из CRM.

Проверка 2026-07-30: `step0` PASS; `step1` создал `raw_yandex=23,681,851`,
`raw_leads=1,775,977`, `raw_calls=96,312`, `raw_domains=4,864`, `raw_perform_leads=0`;
`SUM(raw_yandex.total_cost)=1,207,598,245.284424993`; `step2` оптимизировал 5/5 таблиц.
Контрольные инварианты raw-слоя: `raw_yandex.key3=''` = 0, excluded domains в `raw_leads` = 0,
звонки в `raw_leads` = 0, не-звонки в `raw_calls` = 0.

### Этап 3. Перенос local_*/corrections.py логики (step3 + corrections + step4..7) — ✅ выполнено 2026-07-30 (Codex)

- [x] `step3_build_sources/`: переведён на CH-диалект и batch `INSERT SELECT`.
- [x] `corrections.py`: транзакционные UPDATE/VACUUM заменены на CH-safe hook; тяжёлые правила
      свернуты в builders, где есть исходные данные.
- [x] `step4_campaign_status/`: `campaign_status` строится из текущего `raw_data.direct_campaigns`,
      direct-витрина патчится помесячными batch INSERT.
- [x] `step5_build_pixel/`: CH-проверка/OPTIMIZE существующей pixel-витрины.
- [x] `step6_build_full/`: `big_analytics_calls` и `big_analytics_full` собираются batch `INSERT SELECT`.
- [x] `step7_finalize/`: PostgreSQL `SET LOGGED`/`VACUUM` убраны, остался ClickHouse `OPTIMIZE`.

### Этап 4. Перенос big_analytics_* / датамартов / build_star — ✅ выполнено 2026-07-30 (Codex)

- [x] `step9..step13`: переведены на CH; внешние CRM/API-загрузки не добавлялись.
- [x] `step10_crop_targeting`: проверка/OPTIMIZE CH-витрины посевов.
- [x] `step11_pixel_score`: `Decimal(18,6)` сохраняется; pixel_score и доливка в full идут батчами.
- [x] `build_unified.py`: `full + arrival` собирается batch `UNION`-слоем.
- [x] Spend-датамарты `region_spend`, `adformat_spend`, `criterion_spend` переведены на CH batch rebuild.
- [x] `direct_feed_funnel/`: текущая v6_ch витрина площадок РСЯ/Direct placement построена из
      `direct_spend_staging`; физический факт `fact_direct_feed_funnel_light`, старое имя
      `fact_direct_feed_funnel` оставлено как compatibility view.

### Этап 5. Звёздная схема для Power BI — ✅ выполнено 2026-07-30 (Codex)

- [x] `star_refactor/build_star.py`: пересборка `fact_big_analytics` и Dim-таблиц в ClickHouse.
- [x] Дополнительные BI-поля добавлены в звезду:
      `fact_big_analytics` содержит полный набор row-level полей
      (`специалист`, `status`, `campaign_status`, `город`, `регион`, `салон`, `шаблон`,
      `тип_сайта`, `направление`, `project_manager`, `campaign_code`, `tp`, `cpc_cpa`,
      `site_quiz`); `Dim_Site` содержит site/domain-атрибуты и alias
      `status/project_manager`; `Dim_Campaign` содержит campaign-атрибуты.
- [x] PBI-подключение: новая PBIP-копия admin semantic model использует native Power Query
      `ClickHouse.Database(...)` connector.
- [x] M-запросы в copied TMDL переключены с PostgreSQL на ClickHouse endpoint.
- [~] Тест: PBI Desktop refresh визуально не запускался; проверены CH-таблицы, TMDL-навигация и
      отсутствие старых PostgreSQL/Service-ссылок в новых PBIP-копиях.

### Этап 6. Golden-baseline валидация против v5

- [x] Перенести `data_check/verify_big_analytics.py` под CH-диалект.
- [~] Прогнать v6_ch параллельно с v5 на тех же входных данных: CH pipeline и downstream
      пересобраны, но exact golden против текущего v5 `fact_big_analytics` блокируется
      несовпадением входных/исторических данных.
- [~] Сверить: расход Кудерко `25 422 774.00 ±100 ₽`, продажи `floor ≥ 54`, воронка ~5069/677/575.
      После переноса Rule1 Кудерко, CRM-aware funnel mapping и восстановления pixel direct/attribution
      v6_ch даёт `total_cost=25 059 734.45`, `prodazhi=98`, `rows=99 734` в `fact_big_analytics`.
      Оставшийся расходной разрыв воспроизводится в raw: по `porg-kkhtgf2u` в CH
      `raw_yandex=808 529.01` только до `2026-01-27`, тогда как v5 fact по `bucars-stav.ru`
      содержит `direct=1 155 041.44` + старый pixel-слой `пиксель/пиксель_атрибуц=809 000.00`.
- [x] Проверить дробность пиксельной атрибуции: `fact_big_analytics`
      `_source_table='пиксель_атрибуц'` содержит дробные строки
      (`kol_vo_zayavok=5 279`, `korr=5 133`, `kval=5 077`, `priezd=4 900`,
      `prodazhi=1 260` fractional rows), conservation pixel→pixel_score в допуске `0.1`
      по доменам (`bad_domains=0`).
- [~] Сверить `big_analytics_full` v5 vs v6_ch построчно (или по агрегатам) для ключевых метрик:
      агрегатная сверка выполнена; построчная сверка невозможна до решения, какой v5 snapshot
      считать эталоном, потому что текущие v5 `big_analytics_full/unified` пустые, а
      `fact_big_analytics` содержит материализованный исторический pixel-слой.

---

## 5. Инварианты — обязательны в v6_ch (из v5)

1. **Дробная пиксельная атрибуция** — НИКОГДА не приводить к int/int64 по строкам.
2. **`источник IS NOT NULL`** для всех строк big_analytics_full.
3. **`"Date" >= '2026-01-01'`** — строк раньше быть не должно.
4. **Воронка вложена:** `korr >= kval >= priezd >= prodazhi`.
5. **Нет двойного учёта лидов:** `direct ∩ crop_targeting = 0`.
6. **Incremental refresh PBI — ЗАПРЕЩЁН** (для v6_ch так же, как для v5).
7. **Звонки** несут `campaign_code='звонки'` без CampaignId-маппинга — by-design.

---

## 5а. КРИТИЧЕСКОЕ ОТКРЫТИЕ (2026-07-28) — raw_data уже мигрирован, но требует ревизии

При подключении к ClickHouse (`raw_data`) обнаружено, что предположения плана в разделах
2.2/3.6 о переносе FDW/raw-слоя **устарели** — за нас это уже частично сделано:

1. **`raw_data` — не пустая заготовка, а результат готовой миграции Postgres v5 → CH**,
   формально сверенной построчно. Таблица `raw_data.migration_checkpoints` содержит записи
   вида `dataset='leads_all', status='reconciled', source_rows=794166, target_rows=794166`
   (снимок сверки от 2026-07-14T10:26:55Z) — практически для всех таблиц из
   `work/big_analytics_v5/step0_sync_local` / `step1_load_raw`: `domains`, `direct_campaigns`,
   `direct_adgroups`, `direct_ads`, `leads_all`, `metrika_yandex_*`, `gsheet_*`, `crm_statuses`
   и др. **Кто именно это сделал (отдельная сессия/агент/внешняя команда) — неизвестно,
   найти автора/репозиторий этой миграции у Семёна.**
2. Схема `raw_data` заметно отличается от Postgres-схемы v5 (не 1:1 копия колонок) — она
   уже частично нормализована под CH: `leads_all` содержит `campaign_parse_failed`,
   `is_copy_for_removal`, `correction_id` — это выглядит как результат ПРИМЕНЁННЫХ правил
   типа `corrections.py`, а не сырые данные. **Нужно понять, что уже посчитано/скорректировано
   в `raw_data`, а что ещё предстоит перенести из v5 `corrections.py`/`step3+`, чтобы не
   задвоить логику.**
3. **⚠️ РЕШЕНИЕ 2026-07-30: Загрузчик `yandex_direct`/`vk_ads` в `raw_data` — ВНЕ ОБЪЁМА ПРОЕКТА.**
   Это внешний мигратор (не наш код, мы его не грузим и не чиним). v6_ch потребляет `raw_data` как
   источник — если эти два источника там некоторое время не обновляются, это известное ограничение
   свежести данных (`yandex_direct` не обновлялся с 2026-07-14, `vk_ads` падает периодически),
   **не задача этого проекта.** Не расследовать, не чинить, не искать владельца. Только держать в уме
   при интерпретации Директ/VK-метрик в v6_ch и читать данные с допуском на частичную свежесть.

4. **✅ Решение по доверию `raw_data` (2026-07-30):** не доверять вслепую (одна сверка уже
   оказалась ошибочной — `crm_status_mapping`, см. `.claude/sdd/v6-ch-migration-audit.md` п.D),
   но и не пересобирать с нуля. **Выборочно перепроверить, затем использовать.**

   **✅ Выполнено (2026-07-30, oleg_read_bd, отчёт см. ниже):** точечная сверка по бизнес-ключам
   (не по агрегатам) 5 таблиц. Итог:

   | Таблица | Доверять для Этапа 1-2? | Риск |
   |---|---|---|
   | `raw_data.crm_status_mapping` | ✅ Да | — (побайтовое совпадение с источником) |
   | `raw_data.yandex_direct_report_rows` | ✅ Да | Отставание свежести на 1 день — терпимо |
   | `raw_data.direct_campaigns` | ✅ Да | ~13% строк без `state`/`status` — не готово для классификации активна/архив без доочистки |
   | `raw_data.metrika_yandex_utm_daily` | ✅ Да (по внутренней санитарии) | Нет PG-аналога для контрольной сверки |
   | `raw_data.leads_all` | ⚠️ **Частично — НЕ доверять данным `crmf_excel` (64% объёма таблицы)** | См. ниже — реальный дефект, не «исторический снепшот» |

   **⚠️ Найден реальный дефект, не покрытый исследованием 28.07** (тогда расхождение `leads_all`
   +57% строк списали на «CH хранит больше истории» — гипотеза НЕ подтвердилась для `crmf_excel`):
   - `id` в CH — суррогат/хэш, НЕ тот же бизнес-ключ, что `local_leads_all.id` в PG. Верный ключ для
     сверки — `(source_type, source_record_id)` либо `(source_type, created_date, phone, status)`.
   - По этому ключу `crmf_excel` теряет строки НЕ равномерно по времени, а конкретно в
     март–май 2026 (Δ = -10.7% / -6.1% / -5.7% по месяцам; июнь/июль в норме, ±0.3%) — 14 013
     групп с дефицитом только за март, суммарно -14 162 строк. Разбросано по статусам
     (Отказ/Некорректные данные/Дубль/Недозвон), не единичный баг.
   - `deal_type` для `crmf_excel` в CH урезан на ВСЮ историю 2026 до 2 значений (`Заявка`,`Звонок`)
     вместо 5 в PG (`''`,`Заявка`,`Кредит`,`Звонок`,`Наличные`) — похоже, что PG-строки с
     `deal_type IN ('','Кредит','Наличные')` при миграции переклассифицированы в `'Заявка'`.
   - **Практический вывод:** структуру/DDL `leads_all` переносить можно, но **данные `crmf_excel`
     нельзя брать из `raw_data` как есть** — воронка занижена за март-май, `deal_type`-логика
     (кредит/наличные) сломается молча. Не-crmf источники (plex/mega/marcar/redauto/genzes)
     сверены 45/45 точно — доверять можно.
   - **✅ Решение Семёна 2026-07-30:** CRM-данные в ClickHouse НЕ перезаливаем и сторонний
     мигратор НЕ чиним в рамках v6_ch. На первой итерации работаем с тем, что уже есть в
     `raw_data`, включая известные дефекты `crmf_excel`; интерпретация golden/воронки должна
     учитывать это как ограничение входного слоя, а не как баг v6_ch-пайплайна.
   - Полный отчёт: см. диалог сессии 2026-07-30 (файл `.claude/sdd/v6-raw-data-spotcheck.md`
     НЕ создан — read-only хук `oleg_read_bd` заблокировал запись в `big_analytics_v6_ch/**`;
     находки зафиксированы здесь).

   **✅ Lineage-проверка (2026-07-30, oleg_read_bd) — риск Codex «raw_data уже пропущен через
   corrections.py» СНЯТ, но найден ЕЩЁ ОДИН дефект `crmf_excel`:**
   - `correction_id` / `campaign_parse_failed` в `raw_data.leads_all` — это сырые source-колонки
     из CRM-выгрузки (`src.leads_all` FDW), НЕ изобретение стороннего CH-мигратора и НЕ результат
     `corrections.py`. Просто переносятся как есть, не нужно ничего разбирать.
   - `is_copy_for_removal` — в PG у 42 855 строк `crmf_excel` = TRUE (source-level флаг дублей,
     шире окна правила Кудерко); в CH `raw_data.leads_all` — **100% False, 0 строк** (потерян
     при миграции). Это **третий отдельный дефект `crmf_excel`** в дополнение к недобору строк
     март-май и урезанию `deal_type` (см. выше) — усиливает вывод «не брать `crmf_excel` из
     `raw_data` как есть».
   - Правило `run_dedup_crmf_lider` (corrections.py:1651-1704, патч Кудерко) в v5 пишет только в
     ЭФЕМЕРНУЮ `public.raw_leads` (пересобирается каждый прогон), не в `local_leads_all` — значит
     в CTE-цепочке для v6_ch (§3а) это правило нужно реализовать **с нуля поверх source-флага
     `is_copy_for_removal`**, а не искать/ожидать его уже применённым откуда-либо. Задваивания
     логики не будет — но source-флаг сначала нужно ПРАВИЛЬНО перенести (см. TODO выше).

## 6. Открытые вопросы / ждём уточнений от Семёна

1. ✅ **Где подключение `clickhouse_avto`?** РЕШЕНО в Этапе 0 (2026-07-28): `load_db('victory_clickhouse')`,
   хост `rc1b-q7j2ie10fdverqrk.mdb.yandexcloud.net:8443`, `config/ch_db.py` написан и проверен.

2. ✅ **Где будет запускаться v6_ch?** ПЕРЕРЕШЕНО (2026-08-10): запуск на **Victory VPS** из
   `~/big_analytics_v6_ch` через отдельный `~/venv-v6`; dashboard `victoryads.seoadvanced.ru`
   запускает его с LXC101 через SSH alias `victory` (`server=victory_v6`). ClickHouse остаётся в
   Yandex Cloud (`rc1b-q7j2ie10fdverqrk.mdb.yandexcloud.net:8443`). Victory хранит только код,
   venv и ротируемые логи; raw/mart/dump/cache данные должны оставаться в ClickHouse/Yandex Cloud.

3. **Паттерны CH под специфику проекта** — базовое решение принято 2026-07-30 (см. §3а ниже),
   уточнить в процессе Этапа 1:
   - ✅ UPSERT/ON CONFLICT → см. §3а (не один паттерн на все таблицы).
   - ✅ Движок `big_analytics_full`/`fact_big_analytics` → MergeTree + shadow-таблица + `EXCHANGE TABLES` (см. §3а).
   - ✅ corrections.py без транзакций → сворачивается в цепочку CTE самого INSERT SELECT (см. §3а), не физический UPDATE.
   - Векторизация агрегаций (step3) — GROUP BY в CH работает иначе (два прохода) — уточнить на этапе перевода SQL (Этап 3), не блокер DDL.
   - Партиционирование по месяцу (`toYYYYMM("Date")`) vs по дню — по умолчанию месяц (стандартная CH-практика для time-series ~3-5M строк/год), подтвердить при DDL Этапа 1.

4. ✅ **FDW-источники в step0:** РЕШЕНО (2026-07-30) — не переносим FDW-механизм 1:1, а **используем
   уже загруженный `raw_data`** (см. §5а) как источник raw-слоя для v6_ch, вместо повторной реализации
   step0/step1. Условие: перед использованием — выборочная перепроверка (см. §5а, TODO).

5. **Параллельный режим v5/v6 — источники всё ещё открыты, длительность/критерий cutover РЕШЕНО (2026-07-30):**
   работают одновременно на одних данных или v6_ch начнёт читать из тех же источников что v5?
   Нужна изоляция источников или можно шарить? — **открыт**.

   **✅ DoD (критерий готовности к переезду / когда гасим v5-Postgres):** параллельный режим
   (v5 на Postgres + v6_ch на ClickHouse работают одновременно) длится до тех пор, пока
   PBI-витрины, посчитанные ClickHouse-пайплайном (`fact_big_analytics`, `Dim_*`,
   `fact_region_spend`/`fact_adformat_spend`/`fact_criterion_spend`, `arp_fact` и др. —
   полный список см. `PBI_TABLES.md`), **не сойдутся** с теми же витринами, посчитанными
   Postgres-пайплайном v5. Сверка — тем же golden-baseline инструментом, что уже гоняет v5
   (`data_check/verify_big_analytics.py`: расход Кудерко `25 422 774 ±100 ₽`, продажи `floor≥54`,
   воронка `korr≥kval≥priezd≥prodazhi`, дробная пиксельная атрибуция и т.д.), перенесённым на
   CH-диалект (см. Этап 6). Как только сходится — переключаем PBI на ClickHouse-источник и
   останавливаем Postgres-пайплайн v5.
   ⚠️ Уточнить отдельно (не решено): «сошлись» = совпадение на ОДНОМ прогоне, или на N
   последовательных прогонах подряд (учитывая известный дрейф пикселя ±100 ₽ и живые данные,
   один удачный прогон может быть совпадением, не доказательством надёжности).

6. **PBI-подключение к CH:** ODBC ClickHouse драйвер (Windows-зависимый) или HTTP API?
   Нужна локальная реплика на Маке или нет? — открыт.

7. **Целевые таблицы:** сохранять имена таблиц v5 (big_analytics_full, fact_big_analytics и др.)
   в CH или переименовать (например, добавить суффикс _ch)? — открыт.

8. **Посевы (step10_crop_targeting):** источник данных — Google Sheets в UTC+3.
   CH хранит DateTime в UTC — нужно ли явное преобразование или источник оставить в Postgres? — открыт.

---

## 3а. Решение по UPSERT-паттернам (2026-07-30) — не один размер для всех

Разделение по категориям таблиц вместо единого паттерна:

| Категория | Примеры | Паттерн CH |
|---|---|---|
| **Полностью пересобираемые каждый прогон** (в v5 — TRUNCATE+INSERT / DROP+CTAS) | `big_analytics_direct/seo/pixel/crop_targeting/reviews`, `big_analytics_full`, `big_analytics_unified`, `fact_big_analytics`, `Dim_*`, spend-датамарты, `build_star` outputs | MergeTree, сборка в shadow-таблицу (`_new` суффикс) + атомарный `EXCHANGE TABLES old new`. UPSERT не нужен вообще — весь смысл дедупликации снимается полной пересборкой. Читается PBI полным Import — FINAL-оверхед не нужен. |
| **Накопительные таблицы состояния** (в v5 — построчный UPSERT/UPDATE во времени) | `campaign_status`, `analytics_report_placement` (arp raw), `step9_direct_history`, `yandex_direct_korrektirovki`, `yandex_direct_minus_snapshot` | `ReplacingMergeTree(version)` по натуральному бизнес-ключу; дедупликация через `FINAL` в VIEW-слое (как уже `arp_fact` — VIEW) или `argMax()` в запросах, если `FINAL` дорог на объёме (arp — 10.9M+ строк). |
| **corrections.py (rule0..rule6, физический UPDATE в v5)** | `big_analytics_direct` | НЕ переносится как пост-хок UPDATE. Сворачивается в цепочку CTE внутри самого `INSERT SELECT`, который строит `big_analytics_direct` уже «скорректированным» (архитектура расписана в `.claude/sdd/v6-fact-view-chain.md`). Убирает саму необходимость row-level UPSERT для корректировок. |

Это решение — рабочая гипотеза для Этапа 1 (DDL), не железобетонный факт: проверяется на практике при
переводе `step3`/`corrections.py`/`step6` на CH-диалект (Этап 3).
