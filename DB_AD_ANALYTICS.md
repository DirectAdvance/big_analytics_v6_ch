# DB_AD_ANALYTICS.md — регламент ведения ClickHouse `ad_analytics`

> Схема БА6 в Yandex Cloud ClickHouse `rc1b-q7j2ie10fdverqrk.mdb.yandexcloud.net:8443`.
> `DB_TABLES.md` — карта legacy PostgreSQL БА5, к этой схеме не относится.
> Правила уровня всего проекта — скил `db-rules`; здесь то, что специфично для `ad_analytics`.
>
> Замер, на котором построен документ: 2026-08-17, 123 объекта (65 таблиц + 58 вьюх, 2.06 ГиБ).
> Обновление 2026-08-19: локальный код PBI hidden keys в `bi_*_star` переведён на
> `reinterpretAsInt64(UInt64)`; до деплоя live `bi_*` остаются как на Victory.

## §1. Владение и границы схемы

**`ad_analytics` — витрины и звезда БА6. Единственный владелец — пайплайн
`work/big_analytics_v6_ch`.** Из этого следуют три запрета:

1. **Сырьё сюда не кладём.** Сырые выгрузки живут в `raw_data` (чужая схема, для нас read-only,
   владелец грузит её сам — просьбы в `RAW_DATA_REQUEST.md`). Если источника в `raw_data` нет,
   правильный путь — просьба владельцу, а не своя копия рядом с витринами.
2. **Чужой контур сюда не пишет.** Автоправила живут в `victoryads_direct_autorules`, отчёт
   поисковых запросов — у своего проекта. Таблица в `ad_analytics`, которую не читает пайплайн БА6
   и не читает модель Power BI, — по определению чужая и подлежит переезду.

   **Два признанных исключения (Семён, 2026-08-17):**

   - **`ar_*`** (`ar_benchmark_slices`, `ar_strategy_snapshot`, `ar_waste_entities`) — витрины
     прототипа новых автоправил. Живые: читаются ежедневно, строятся джойном по звезде БА6
     (`Dim_Campaign`, `Dim_Site`, `fact_big_analytics`) внутри этого же ClickHouse, поэтому переезд
     в PostgreSQL-контур автоправил означал бы гонять данные через сеть. Остаются здесь под
     префиксом `ar_`; при выходе прототипа из черновика — своя схема в этом же ClickHouse, а не
     `ad_analytics`.
   - **`yd_search_query_report_master`** (40.1 млн строк / 819 МиБ, пишет
     `work/yd_SEARCH_QUERY_REPORT/`) — временно остаётся: владелец `raw_data` заберёт поток к себе,
     после чего таблица удаляется отсюда.

   Исключение = запись в этом файле. Молча оставленная чужая таблица исключением не считается.
3. **Power BI смотрит только в слой `bi_*`.** Модель не имеет права ссылаться на физическую
   таблицу: между звездой и отчётом всегда вьюха (§3).

## §2. Слои и префиксы

Порядок соответствует потоку данных. Префикс обязателен и означает слой, а не автора.

| Слой | Префикс | Кто наполняет | Пример |
|---|---|---|---|
| Нормализованный срез сырья | `raw_*` | шаг 1 из `raw_data` | `raw_yandex`, `raw_leads`, `raw_calls`, `raw_perform_leads` |
| Локальные копии Google Sheets | `gsheets_*` | шаг 0 | `gsheets_crop_targeting_account` |
| Промежуточные широкие витрины | `big_analytics_*` | шаги 6, 10, 11 | `big_analytics_calls`, `big_analytics_pixel_score` |
| Звезда: измерения | `Dim_*` | шаги 145, 1451 | `Dim_Campaign`, `Dim_Site` |
| Звезда: факты | `fact_*` | шаги 141-144, 145 | `fact_big_analytics`, `fact_region_spend` |
| Совместимость со старой формой | `pbi_*`, `pbi_import_*` | шаг 146 | `pbi_import_big_analytics_full` |
| **Контракт с отчётами** | `bi_*` | шаг 146 | `bi_fact_region_spend` |
| Временные | `*_new`, `*_staging`, `_tmp_*` | шаг-владелец | `Dim_Site_new`, `direct_spend_staging` |

**`raw_*` в этой схеме — историческая ошибка имени:** это не сырьё, а нормализованный срез поверх
`raw_data`. Переименование в `src_*` — в долге (§6.4), до тех пор читать префикс как «срез сырья»,
а не «сырьё».

## §3. Таблица или вьюха — как выбирать

**Вьюха не хранит копию данных.** В ClickHouse `View` — это сохранённый SQL, 0 байт на диске;
пересчёт идёт при каждом чтении. Поэтому вьюха-обёртка не стоит места, стоит только времени
запроса. Дублирование данных создают таблицы, не вьюхи.

Отсюда правила:

1. **Проекция без агрегации и джойнов → вьюха.** Копировать данные в таблицу ради другого набора
   колонок нельзя.
2. **Слой `bi_*` обязателен даже когда это `SELECT *`.** Восемь `bi_*` сейчас — чистая проекция
   (`bi_Dim_VkBanner`, `bi_Dim_VkAdPlan`, `bi_Dim_VkAdGroup`, `bi_Dim_Location`,
   `bi_Dim_PlacementFeed`, `bi_yandex_direct_404_errors`, `bi_fact_direct_feed_funnel`,
   `bi_pbi_big_analytics_full`), и это не мусор: вьюха — шов, за которым можно перестроить
   физику, не трогая модель Power BI и не пересобирая отчёт. Остальные 29 `bi_*` уже делают работу:
   переименовывают колонки под модель, джойнят измерения, фильтруют.
3. **Материализовать вьюху в таблицу — только по замеру.** Единственный законный повод: Power BI
   не выдерживает джойн на каждом обновлении. Такой случай в схеме один —
   `pbi_import_fact_direct_feed_funnel` (13.58 млн строк, 144 МиБ поверх
   `fact_direct_feed_funnel_light`). Рядом есть облегчённый future-star слой
   `pbi_import_fact_direct_feed_funnel_star` (13.58 млн строк, 11 колонок, 115 МиБ) для перевязки
   Power BI на `Dim_PlacementFeed` по `placement_feed_id`; текущая модель пока читает старый
   compatibility-объект. `bi_fact_region_spend_star` и `bi_fact_criterion_spend_star` сделаны
   view, потому что это простые проекции без join. Материализация обязана быть помечена замером,
   который её оправдал, иначе она неотличима от случайной копии.
4. **Больше двух этажей вьюх — запрещено.** Цепочка
   `bi_pbi_big_analytics_full` → `pbi_big_analytics_full` → `pbi_import_big_analytics_full` →
   `fact_big_analytics` — три этажа на одну сущность, и параллельно существует независимая
   реализация той же сущности вьюхой `big_analytics_full`. Это долг (§6.5).

## §4. Как обновляются данные

**Способ записи один: shadow + swap.** Витрина пересобирается целиком —
`CREATE TABLE X_new` → `INSERT` → `RENAME` (`config/ch_utils.py::swap_shadow`). Инкрементальной
дозаписи в витрины нет и быть не должно; `ALTER UPDATE`/`DELETE` по живой витрине запрещены.

Полезное следствие: `system.tables.metadata_modification_time` — это дата последней **пересборки**.
Для витрин БА6 она и есть индикатор свежести; `raw_data.etl_runs` и `leads_all.updated_at` врут
(`KNOWN_ISSUES.md` #43).

| Контур | Расписание | Что обновляет |
|---|---|---|
| `cron_run.py` → `pipeline.py` | **ежедневно `0 4 * * *` UTC = 09:00 Екб**, крон Victory | шаги 0-148: срезы сырья, промежуточные витрины, звезда, `pbi_*`, `bi_*` |
| `step_cron_night/pipeline_night.py` | **ежедневно `10 18 * * *` UTC = 23:10 Екб**, крон Victory с `/tmp/ba6_night.lock` | шаги 101-106, 114: `metrika_yandex`, `check_utm`, `check_utm_fuck_direct`, `yandex_direct_korrektirovki`, `yandex_direct_404_errors`, `fact_ml_korrektirovki`, `yandex_direct_minus_snapshot` |
| `step_cron_night/pipeline_night.py --only-step 107` | **еженедельно**, отдельная крон-строка (см. `step_cron_night/README.md`) | шаг 107 (weekly, вне ежедневного набора — `WEEKLY_DEFAULT_STEPS`): `yandex_direct_account_reviews`, `yandex_direct_reports_reviews` |

**Ночной контур БА6 поставлен в крон 2026-08-17.** Окно выбрано до БА5 night (`0 21 * * *`) и
далеко от дневных БА5 (`0 2 * * *`) и БА6 (`0 4 * * *`). Проверенный ручной прогон на Victory: PASS за 14м15с,
самый длинный шаг `night_minus_snapshot` — 11м48с.

Разовые и maintenance-контуры:

| Контур | Расписание | Что обновляет |
|---|---|---|
| `tools/*.py` | разово руками | `raw_new_*` (`copy_pg_to_raw_new.py`) |
| Maintenance-шаги 2, 7 | только с `--include-maintenance` | индексы, финализация |

Соответствие шаг → объект (главные владельцы):

| Шаг | Модуль | Пишет |
|---|---|---|
| 0 | `step0_sync_local.step0` | `gsheets_crop_targeting_*`, `local_pixel_config`, `local_pixel_price_history` |
| 1 | `step1_load_raw.step1` | `raw_yandex`, `raw_leads`, `raw_calls`, `raw_perform_leads`, `raw_domains` |
| 4 | `step4_campaign_status.step4` | `campaign_status`, `campaign_status_v` |
| 6 | `step6_build_full.step6` | `big_analytics_full`, `big_analytics_calls` |
| 10 | `step10_crop_targeting.step10` | `big_analytics_cost_overlays`, `crop_targeting_api_telegain_lead`, `telega_in_order_*_overrides`, `local_telega_in_orders_errors` |
| 11 | `step11_pixel_score.step11` | `big_analytics_pixel_score` |
| 13, 131 | `step13_arrival.*` | `big_analytics_full_arrival`, `big_analytics_unified` |
| 139 | `direct_placement_links.build` | `yandex_direct_tp_placement_links`, `..._link_matches` |
| 141-144, 1431, 1432 | `region_spend`, `adformat_spend`, `criterion_spend`, `direct_feed_funnel` | `fact_region_spend`, `fact_adformat_spend`, `fact_criterion_spend`, `fact_*_zayavki`, `fact_direct_feed_funnel_light` |
| 145, 1451 | `star_refactor.build_star*` | все `Dim_*`, `fact_big_analytics`, `fact_vk_ads`, `fact_ml_korrektirovki`, `pixel_score` |
| 146 | `star_refactor.build_pbi_compat` | все `bi_*`, `pbi_*`, `pbi_import_*` |
| 147, 148 | `spend.cleanup_*`, `star_refactor.cleanup_wide_intermediates` | удаляют `*_staging` и широкие промежуточные |
| 14 | `step14_minus_snapshot.step14` | `yandex_direct_minus_snapshot`, `v_yandex_direct_minus_delta` |
| night 107 | `step_cron_night.direct_account_reviews.run` (weekly) | `yandex_direct_account_reviews`, `yandex_direct_reports_reviews` |

**Не обновляется автоматически** (замер 2026-08-17, данные не писались с 31.07):
`gsheets_crop_targeting_account`, `..._leads`, `..._pravilo_utm`, `local_pixel_config`,
`local_pixel_price_history`, `telega_in_order_price_overrides`. Ночной cron с 2026-08-17 обновляет
`yandex_direct_404_errors` и `yandex_direct_minus_snapshot`; 30-дневная история минус-фраз ещё
наполняется. `yandex_direct_cookie_analytics_website_pages` и `yd_search_query_report_master` —
12.08.

## §5. Обязательные требования при создании объекта

1. **`COMMENT` на каждый объект** — назначение, шаг-владелец, для материализации ещё и замер,
   её оправдавший. Текущее состояние: **3 из 123**.
2. **Строка в этом файле** — объект без записи здесь считается бесхозным и подлежит удалению.
3. **Префикс из §2.** Новый префикс — только решением Семёна, с записью здесь.
4. **Проверка на дубль до создания** — `SELECT DISTINCT новое EXCEPT SELECT существующее`
   (скил `db-rules` §3). Совпало — присоединяемся к существующему объекту.
5. **Регистр имён.** Всё новое — `lowercase`. Существующий `Dim_*` в CamelCase — контракт с
   моделью Power BI, менять только вместе с моделью. Помнить: в ClickHouse имена
   регистрозависимы, `Dim_Criterion` и `dim_criterion` — два разных объекта.

## §6. Технический долг

Порядок — по цене ошибки, а не по объёму. Каждый пункт трогать по протоколу `db-rules` §5:
бэкап, новая структура рядом, сверка сумм, переключение читателей, только потом удаление.

1. **Модель Power BI читает мёртвые копии.** ✅ **Площадки — закрыто 2026-08-17.**
   `analytics_report_placement_links.tmdl:464` переведён с `raw_new_tp_placement_links` на
   `bi_yandex_direct_tp_placement_links` (вьюха над живой таблицей шага 139, добавлена в
   `PBI_SOURCE_OBJECTS`). Перед переключением словарь обогащён снимком БА5 миграцией
   `migrations/03_placement_links_merge_ba5_2026-08-17.py`: 5 939 → 5 987 строк, ссылок
   5 180 → 5 527, иначе отчёт потерял бы 299 ссылок, которые знал только БА5. Проверено прогоном
   шага 139: обогащение удержано кэшем (`matched` 49 681 → 50 381, `missing` 1 290 → 590).

   **18.08 закрыто:** `yandex_direct_ads_texts` и
   `yandex_direct_type_placement_report_master` переведены в BA6 PBIP с `raw_new_*` на
   `bi_yandex_direct_ads_texts` / `bi_yandex_direct_type_placement_report_master`. Эти view читают
   `raw_data.direct_cookie_ads_texts_master` и `raw_data.direct_cookie_type_placement_master`,
   агрегируют данные в ClickHouse и сохраняют `type_placement_ru` без
   `raw_new_type_placement_types`. `goal_crm_order_paid` пока = 0, потому что новый источник
   отдаёт только общий `goals`.

   **Осталось:** `analytics_report_placement` и `analytics_report_placement_links` читают
   `raw_new_arp_fact`; `yandex_direct_search_query_report_master` читает
   `raw_new_search_query_report_master_pbi`; `yandex_direct_accounts_human_cyborgs` читает
   `raw_new_human_cyborgs`. Для ARP проверенный кандидат через `fact_direct_feed_funnel` не
   совпадает по суммам, поэтому нужен отдельный совместимый источник, а не простая подмена.
2. **`ar_*` и `yd_search_query_report_master`** — ✅ решено 2026-08-17: оба остаются как
   признанные исключения, условия в §1.2. Кода-писателя `ar_*` в репозитории нет (записаны
   16.08 в 19:38 юзером `clickhouse_avto`) — при следующем касании прототипа писателя нужно
   положить в репозиторий, иначе витрины невоспроизводимы.
3. **`Dim_AdFormat`** (5 строк) — единственный объект схемы с нулём обращений за 7 дней:
   `bi_Dim_AdFormat` собирает измерение напрямую из `fact_adformat_spend`, минуя таблицу.
   Либо подключить таблицу к bi-слою, либо убрать из звезды.
4. **Дубли данных:**
   - оставшиеся `raw_new_*` в активном PBIP: `raw_new_arp_fact`,
     `raw_new_search_query_report_master_pbi`, `raw_new_human_cyborgs`. Direct-cookie
     `raw_new_ads_texts_master_pbi`, `raw_new_type_placement_report_master` и
     `raw_new_type_placement_types` активным PBIP больше не читаются;
     `raw_new_tp_placement_links` с 17.08 не читает никто, кроме маппинга
     `powerbi_ba6/tools/add_table_from_ba5.py:43`;
   - `pixel_score` + `big_analytics_pixel_score` — по 243 258 строк, по имени неотличимы
     (названо в скиле `db-rules` §3 как известное нарушение);
   - `raw_yandex` (26.3 млн / 231 МиБ) — третья форма отчёта Директа после
     `raw_data.yandex_direct_report_raw` и `..._report_rows` (`docs/DB_MAP.md:45`);
   - `gsheets_crop_targeting_*` (3 шт) против `gsheet_*` (7 шт) в `raw_data` — два написания
     одного источника.
5. **Лишние этажи вьюх:** цепочка `bi_pbi_big_analytics_full` → `pbi_big_analytics_full` →
   `pbi_import_big_analytics_full` → факт (три этажа, §3.4) при наличии независимой реализации
   той же сущности вьюхой `big_analytics_full`; `campaign_status_v` — вьюха над вьюхой
   `campaign_status`; `bi_dim_criterion` и `dim_criterion` — две вьюхи-синонима над
   `Dim_Criterion`, причём `bi_dim_criterion` в коде БА6 не упомянута ни разу.
6. **Return commission** — ✅ закрыто 2026-08-17: `yandex_direct_return_commission_report` и
   `bi_yandex_direct_return_commission_report` выведены из BA6 PBI-контракта, очищены из BA6 PBIP
   и удалены из live ClickHouse. `system.tables LIKE '%return_commission%'` возвращает 0 строк.
7. **Имена:** `check_utm_fuck_direct` и `bi_check_utm_fuck_direct` — мат в имени объекта,
   тянется в модель Power BI (118 ссылок); префикс `raw_*` врёт про слой (§2); префикс `local_*`
   (`local_pixel_config`, `local_pixel_price_history`, `local_telega_in_orders_errors`,
   вьюха `local_telega_in_orders`) — наследие PG-слоя БА5, в ClickHouse не значит ничего.
8. **Ночной контур БА6.** ✅ Закрыто 2026-08-17: `step_cron_night/pipeline_night.py` поставлен
   в крон Victory на `10 18 * * *` UTC = 23:10 Екб с `/tmp/ba6_night.lock`.
   Перед постановкой полный ручной прогон прошёл PASS за 14м15с: `check_utm` = 28 288,
   `check_utm_fuck_direct` = 3 981, `yandex_direct_404_errors` 13 953→13 823 после recheck,
   `fact_ml_korrektirovki` = 16 647, `yandex_direct_minus_snapshot` total = 3 352.
9. **`docs/DB_MAP.md` не покрывает ClickHouse** — 448 объектов карты относятся к Victory PG и
   LXC 101. Именно поэтому чужие `ar_*` и появились незамеченными. Этот файл — первый шаг;
   ссылку на него нужно добавить в `DB_MAP.md` и в `CLAUDE.md` проекта.
