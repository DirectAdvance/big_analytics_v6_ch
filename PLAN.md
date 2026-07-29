# PLAN.md — big_analytics_v6_ch (миграция пайплайна на ClickHouse)

> Дата: 2026-07-27. Статус: черновик плана, код не написан.
> Источник: анализ `work/big_analytics_v5/` (CLAUDE.md, PIPELINES.md, PBI_TABLES.md).
> Оптимизация кода под специфику ClickHouse будет уточнена Семёном отдельно — этот план
> фиксирует архитектуру и известные точки риска, не конкретные решения.

---

## 1. Что такое v6_ch

Параллельная версия `big_analytics_v5`, адаптированная под **ClickHouse** (Yandex Cloud,
подключение `clickhouse_avto`) вместо PostgreSQL (`ad_analytics_bi` @ Victory VPS).
Логика ETL и воронка статусов не меняются — меняется СУБД и паттерны работы с ней.

Целевое развёртывание: уточнить (Victory VPS + Yandex Cloud CH / только Cloud CH / локальный CH
— открытый вопрос, см. раздел 6).

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

### Этап 1. Схема таблиц под ClickHouse (DDL)

- [ ] Для каждой ключевой таблицы v5 определить: движок, ORDER BY, партиционирование, тип колонок
- [ ] Особое внимание: тип для дробной атрибуции (Decimal vs Float64), тип для дат
- [ ] Создать `migrations/01_init_schema.sql` — DDL всех таблиц v6_ch
- [ ] Промежуточные таблицы (аналог UNLOGGED): движок MergeTree vs Memory vs Buffer
- [ ] Dim-таблицы (Dim_Campaign, Dim_Site, Dim_AdGroup, Dim_Date): движок Dictionary vs ReplacingMergeTree

### Этап 2. Перенос raw_* слоя (step0 + step1 + step2)

- [ ] `step0_sync_local`: заменить FDW на CH-совместимый механизм (CH Table Engine / прямой SELECT)
- [ ] `step1_load_raw`: переписать загрузку RAW с psycopg2 на clickhouse-connect INSERT
- [ ] `step2_indexes`: убрать, заменить на OPTIMIZE TABLE (если нужно) или оставить пустым

### Этап 3. Перенос local_*/corrections.py логики (step3 + corrections + step4..7)

- [ ] `step3_build_sources/`: перевести SQL на CH-диалект, заменить CTAS на INSERT SELECT
- [ ] `corrections.py`: переписать rule0..rule4 под CH (без транзакций, без VACUUM)
- [ ] `step4_campaign_status/`: API-часть 1:1, запись в БД — CH INSERT
- [ ] `step5_build_pixel/`: CH INSERT
- [ ] `step6_build_full/`: UNION ALL + INSERT SELECT в CH (движок big_analytics_full)
- [ ] `step7_finalize/`: убрать SET LOGGED / VACUUM, оставить disk-check через system.parts

### Этап 4. Перенос big_analytics_* / датамартов / build_star

- [ ] `step9..step13`: перевести на CH (API-части 1:1, SQL и запись — CH)
- [ ] `step10_crop_targeting`: посевы, дедупликация без UPSERT
- [ ] `step11_pixel_score`: дробная атрибуция — проверить тип Decimal в CH
- [ ] `build_unified.py`: UNION INSERT SELECT в CH
- [ ] `build_spend_daily.py`: staging без UNLOGGED, 3 spend-датамарта в CH
- [ ] `direct_feed_funnel/`: фидовая воронка на CH

### Этап 5. Звёздная схема для Power BI

- [ ] `star_refactor/build_star.py`: пересборка fact_big_analytics, arp_fact, fact_vk_ads в CH
- [ ] PBI-подключение: настроить CH ODBC / HTTP коннектор
- [ ] Переписать M-запросы (partition source) в TMDL под CH endpoint
- [ ] Тест: PBI Import из CH (время refresh vs Postgres baseline)

### Этап 6. Golden-baseline валидация против v5

- [ ] Перенести `data_check/verify_big_analytics.py` под CH-диалект
- [ ] Прогнать v6_ch параллельно с v5 на тех же входных данных
- [ ] Сверить: расход Кудерко `25 422 774.00 ±100 ₽`, продажи `floor ≥ 54`, воронка ~5069/677/575
- [ ] Проверить дробность пиксельной атрибуции (SUM(атрибуция_вес) по строкам не усекается до int)
- [ ] Сверить `big_analytics_full` v5 vs v6_ch построчно (или по агрегатам) для ключевых метрик

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
3. **⚠️ ОПЕРАЦИОННАЯ ПРОБЛЕМА, не связанная с миграцией:** `raw_data.etl_runs` показывает,
   что источник `yandex_direct` **падает с ошибкой на каждом прогоне** в диапазоне
   2026-06-21 .. 2026-07-14 (12 из 12 запусков — `status='error'`, ни одного `success`
   в этом окне). Последняя запись по `yandex_direct` в `etl_runs` — 2026-07-14 (ошибка).
   Если загрузчик с тех пор не чинили, Директ-данные в `raw_data` **не обновлялись
   ~2 недели** (сегодня 2026-07-28). `vk_ads` тоже падал 10 раз подряд с 2026-07-08.
   **Это отдельная задача, не блокирует наш DDL-этап, но Семёну стоит проверить отдельно —
   кто владеет этим ETL и почему падает.**

## 6. Открытые вопросы / ждём уточнений от Семёна

1. **Где подключение `clickhouse_avto`?** Реквизиты не найдены в `.secret/.env` и `loader.py` —
   возможно, ещё не добавлены. Нужны: хост, порт (8443 HTTPS?), SSL-сертификат, пользователь,
   пароль, имя БД.

2. **Где будет запускаться v6_ch?** Victory VPS (рядом с v5) или другой хост?
   Влияет на: network latency до CH Cloud, деплой-механику, где хранить venv.

3. **Паттерны CH под специфику проекта (Семён даст отдельно):**
   - Как заменить UPSERT/ON CONFLICT → `ReplacingMergeTree` или мутации или иное?
   - Какой движок для `big_analytics_full` (главная витрина ~3.4M строк, full-scan для PBI)?
   - Как делать corrections.py без транзакций? (rule1 Кудерко критичен)
   - Векторизация агрегаций (step3) — GROUP BY в CH работает иначе (два прохода)?
   - Партиционирование по месяцу (`toYYYYMM("Date")`) vs по дню?

4. **FDW-источники в step0:** что заменяет `yandex_direct_manager_reports` FDW в CH?
   Прямой CH PostgreSQL Table Engine (читает Postgres) или оставить шаг0 на Postgres?

5. **Параллельный режим v5/v6:** работают одновременно на одних данных или v6_ch начнёт
   читать из тех же источников что v5? Нужна изоляция источников или можно шарить?

6. **PBI-подключение к CH:** ODBC ClickHouse драйвер (Windows-зависимый) или HTTP API?
   Нужна локальная реплика на Маке или нет?

7. **Целевые таблицы:** сохранять имена таблиц v5 (big_analytics_full, fact_big_analytics и др.)
   в CH или переименовать (например, добавить суффикс _ch)?

8. **Посевы (step10_crop_targeting):** источник данных — Google Sheets в UTC+3.
   CH хранит DateTime в UTC — нужно ли явное преобразование или источник оставить в Postgres?
