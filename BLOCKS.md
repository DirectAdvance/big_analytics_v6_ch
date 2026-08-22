# BLOCKS.md — технические блоки ETL (C–L) + corrections

> Документация нетривиальных решений, патчей и фиксов по шагам пайплайна.
> Вынесено из `CLAUDE.md` (lazy-load). Воронка статусов — в `FUNNEL.md`,
> канон значений — в `CANON.md`.

---

## Block C: Исключение доменов

### config/ch_settings.py — EXCLUDED_DOMAIN_NAMES (v6_ch, фильтр по ИМЕНИ)

```python
EXCLUDED_DOMAIN_NAMES = ("victory-crm.ru",)
# victory-crm.ru — тестовый домен, не является клиентом
```

⚠️ **Не по числовому `domain_id`.** `domain_id` непереносим между PostgreSQL (v5) и ClickHouse
(v6) — своя нумерация в каждой системе. Раньше здесь был буквально скопированный из v5
`EXCLUDED_DOMAIN_IDS = (1645, 883)`, который в CH ошибочно исключал реальных клиентов
(`multiautos-23.ru`, `rt-avtomarket-geely.ru`) и пропускал реальный мусор (`victory-crm.ru`,
id=17478 в CH) — 170 084 строки мусора молча текли в `raw_leads`/`raw_perform_leads`. Разбор,
замеры до/после и открытый вопрос по identity `1645` — `KNOWN_ISSUES.md` #33.

Фильтр применяется в `step1_load_raw/step1.py::_raw_leads_select_sql` при сборке `raw_leads`
(и `raw_perform_leads`, которая строится через ту же функцию): `LEFT JOIN reference_data.domains AS d`
+ `lowerUTF8(trim(ifNull(d.domain, ''))) NOT IN (...)`. Пустой `EXCLUDED_DOMAIN_NAMES` не подставляет
условие вовсе (guard в `_excluded_domain_names_sql`), а не превращается в синтаксически битый
`NOT IN ()`.

### Расходы Директа (`raw_yandex`) — `EXCLUDED_DOMAIN_NAMES` их НЕ фильтрует

⚠️ **Отличие от v5:** в v5 `step3_build_sources/step3.py` (CTE `base_join`) есть отдельный
WHERE-фильтр `LOWER(TRIM(gs."domain")) != 'victory-crm.ru'` для расходов. В `step3_build_sources/
step3.py` v6_ch (проверено `grep` по всему дереву, 2026-08-06) такого фильтра **нет** — расходы
victory-crm.ru по кампаниям Директа (если такие есть) в v6_ch ничем не отсекаются на стороне
step3. Не подтверждать наличие этого фильтра без перепроверки кода — это открытый разрыв паритета
с v5, не перенесённая по факту механика.

---

## Block D: SEO-лиды с UTM-тегами

Часть SEO-лидов имеет `utm_source='seo'`, `utm_medium='organic'` — без фикса
они попадают в `big_analytics_direct` вместо `big_analytics_seo`.

В `_direct_lead_universe_filter()` (`step3_build_sources/step3.py`, применяется в `lead_scored`
внутри `_build_direct_sql`) — исключение (симметрично шире, чем только seo/organic):
```sql
AND NOT (ifNull(utm_source, '') = '' OR (utm_source = 'seo' AND utm_medium = 'organic'))
```
В `_build_seo_sql()` (та же функция строит `big_analytics_seo`) — включение:
```sql
WHERE (ifNull(utm_source, '') = '')
   OR (utm_source = 'seo' AND ifNull(utm_medium, '') = 'organic')
```

---

## Block E: UTM-аудит — tp6/tp7/tp8/tp9/tp10

### Файл: step4_campaign_status/check_utm/utm_direct_audit.py

| tp | Маршрут | Источник данных |
|----|---------|-----------------|
| tp1–tp5 | `camp_ids` → Direct API | AdGroups отчёт Директа |
| tp6, tp7, tp8, tp9, tp10 | `mk_tk_ids` → Метрика | Источники трафика Метрики |

tp8/tp9/tp10 — МК/ТК кампании, не имеют групп объявлений в Директе. При tp8 в `camp_ids`,
когда `bad_cnt=0`, fallback на Метрику не запускался → tp8 пропадал из check_utm.
tp9 (Max) и tp10 (Telegram+Max) — аналогичная схема маршрутизации через Метрику.

### check_utm_fuck_direct — индекс на NULL group_id

```sql
CREATE INDEX ON check_utm_fuck_direct (COALESCE(group_id, 0));
```

---

## Block F: Название CRM

| source_type | Название crm |
|-------------|--------------|
| crmf_excel | Фаиг |
| plex_excel | Плекс |
| mega_crm_excel | Мега |
| marcar_crm_excel | Маркар |
| (прочие) | source_type как есть |

### Источник: domain_source_type CTE (step3)

CTE объединяет `raw_leads` + `raw_calls` через UNION ALL — чтобы домены, у которых только звонки (`deal_type='Звонок'`), тоже получали "Название crm" в строках `_source_table='direct'`.

**Причина:** step1 кладёт звонки в `raw_calls`, не в `raw_leads`. Без UNION ALL такие домены получали NULL.

### v6_ch: без UPDATE-постпроцессинга по салону

⚠️ **Отличие от v5:** v5 добивал NULL `"Название crm"` UPDATE-бэкфиллом по одноимённому салону
(в step4, затем повторно в `load_crop_to_big_analytics.py`, которого в v6_ch нет — см. Block I). В
v6_ch NULL не возникает вовсе: `step3_build_sources/step3.py::_crm_name_expr()` резолвит
`"Название crm"` через `CRM_NAME_BY_SOURCE_TYPE`-маппинг и в конце `ifNull(nullIf(..., ''), 'Не указана')`
— домен без известной CRM получает литерал `'Не указана'`, не `NULL`, и не наследует значение от
другого домена того же салона.

---

## Block G2: постпроцессинговые UPDATE (ЦЕЛИКОМ LEGACY v5/PostgreSQL, весь блок до Block H)

⚠️ **Проверено по коду 2026-08-22: ни одного из UPDATE ниже в v6_ch нет.** `big_analytics_full` —
ClickHouse **View** (не таблица, `UPDATE`/`CREATE INDEX`/`_tmp_salon_aggs` физически невозможны).
Реальный v6_ch: `step6_build_full/step6.py` строит calls ОДНИМ инлайн-SELECT в `_calls_select()` —
`campaign_status` для звонков всегда `NULL` (нет backfill по домену с активной Директ-кампанией),
`направление` для звонков всегда литерал `'Комплекс'` (не NULL с условным `'Контекст'`), `источник`
для обычных звонков — `multiIf(gs.status='SEO Flow','SEO Flow', gs.status='SEO','SEO','Контекст')`,
для посевных — `'Посевы_Звонки'`. `"Название crm"` NULL не возникает (см. правку выше — литерал
`'Не указана'`). `manager_login`-бэкфилл по салону/домену не портирован. Оставлено как исторический
контекст v5, не инструкция к действию для v6_ch.

### campaign_status для звонков

Если у домена есть хотя бы одна строка `_source_table='direct'` с `campaign_status='Активна'` →
все звонки этого домена получают `campaign_status='Активна'`.

```sql
UPDATE big_analytics_full SET campaign_status = 'Активна'
WHERE _source_table = 'calls' AND тип_заявки = 'звонки'
  AND campaign_status IS DISTINCT FROM 'Активна'
  AND EXISTS (SELECT 1 FROM big_analytics_full f2
              WHERE f2._source_table='direct' AND f2.domain=f.domain AND f2.campaign_status='Активна')
```

### направление='Контекст' для звонков (апрель 2026)

Если у домена в `local_gsheet_sites` заполнен `directologist` →
все call-строки этого домена получают `направление='Контекст'`.

**Почему:** звонки строятся с `NULL::TEXT AS направление` (т.к. у звонка нет CampaignId/направления).
Но если домен ведёт директолог — значит это контекстный трафик → направление известно.

```sql
UPDATE big_analytics_full SET направление = 'Контекст'
WHERE _source_table = 'calls' AND направление IS NULL
  AND EXISTS (SELECT 1 FROM local_gsheet_sites gs
              WHERE LOWER(TRIM(gs.domain)) = f.domain
                AND gs.directologist IS NOT NULL AND gs.directologist != '')
```

Строки без `directologist` остаются `направление=NULL` (домены без контекстной рекламы).

### Название crm по салону (апрель 2026)

NULL "Название crm" заполняется значением из другой строки того же салона.

Работает в связке с расширенным `domain_source_type` CTE в step3 (см. Block F).

### manager_login по домену (апрель 2026)

NULL `manager_login` заполняется из другой строки того же домена.

Покрывает ~510 строк (SEO/calls/direct домены у которых есть менеджер в других строках). Остальные ~10k без менеджера — домены с `login_key='Нет'` в `local_gsheet_sites`.

### manager_login по салону — реальный менеджер (апрель 2026)

Второй проход: заполняет NULL и маркеры (`'отзывы'`, `'посевы'`) реальным менеджером из другой строки того же салона.

Покрывает отзывы (3 419), telegram посевы (262), direct_zero (1 306) и др. (~14 500 строк).

### проджект по салону (апрель 2026)

Покрывает ~3 738 строк (отзывы, telegram посевы, часть direct_zero).

### Оптимизация: 3 salon-UPDATE → 1 через `_tmp_salon_aggs` (май 2026)

Три UPDATE по салону (Название crm, manager_login, проджект) объединены в **один UPDATE** через UNLOGGED-таблицу `_tmp_salon_aggs`:

```sql
CREATE UNLOGGED TABLE _tmp_salon_aggs AS
SELECT "салон",
    MAX("Название crm") FILTER (WHERE "Название crm" IS NOT NULL) AS crm_name,
    MAX(manager_login) FILTER (WHERE manager_login IS NOT NULL
        AND manager_login NOT IN ('отзывы','посевы')) AS mgr_real,
    MAX(проджект) FILTER (WHERE проджект IS NOT NULL) AS proj
FROM big_analytics_full WHERE "салон" IS NOT NULL GROUP BY "салон";
```

Потом один UPDATE с smart WHERE (срабатывает только если хотя бы одно из трёх полей действительно изменится).

**Зачем:** устраняет 3 полных скана big_analytics_full по индексу салона вместо 1. Снижает dead tuples.

### Оптимизация: индексы после CTAS, до UPDATEs (май 2026)

Сразу после CTAS UNION ALL в step6, перед UPDATEs создаются временные индексы:

```python
CREATE INDEX IF NOT EXISTS idx_full_tmp_cmpid  ON big_analytics_full ("CampaignId")
CREATE INDEX IF NOT EXISTS idx_full_tmp_domain ON big_analytics_full (domain)
CREATE INDEX IF NOT EXISTS idx_full_tmp_salon  ON big_analytics_full ("салон")
CREATE INDEX IF NOT EXISTS idx_full_tmp_src    ON big_analytics_full (_source_table)
ANALYZE big_analytics_full
```

Индексы дропаются в конце step6 (step7 создаёт финальные). Позволяют 9 UPDATE'ам работать по индексу вместо seqscan на 2.6M строк.

---

## Block H: менеджер = 'Михаил Яковлев' для домена lotos91.ru

**Файл:** `corrections.py`, константа `_MISSING_MANAGERS`, инлайн-фрагмент помечен комментарием
`# ── _fix_missing_managers ──` внутри `_stage6_labels()` (это НЕ отдельная вызываемая функция —
v6_ch порт выражает все правила v5 как SQL-выражения одной пересборки, см. заголовок файла).
**Живой код** — участвует в `corrections.py::apply()` (после step4 и step3 — порядок в
`pipeline.py::STEPS`: step4→step3→corrections→step5).

Ручной патч домена, чей `менеджер` отсутствует в `local_gsheet_sites`:
`lotos91.ru` относится к аккаунту `avto_0358` (АвтоЛидер) — тот же менеджер, что и у аккаунта
`avto_0083` (Лидер).

```python
_MISSING_MANAGERS = (("lotos91.ru", "Михаил Яковлев"),)
```
```sql
-- multiIf(domain_key = 'lotos91.ru', 'Михаил Яковлев', s.`менеджер`), применяется только когда
if(s.`_source_table` = 'direct' AND ifNull(trim(s.`менеджер`), '') = '',
   multiIf(domain_key = 'lotos91.ru', 'Михаил Яковлев', s.`менеджер`),
   s.`менеджер`)
```

⚠️ **v6_ch отличие:** `gsheet_vse_klienty`/`local_gsheet_vse_klienty` (PostgreSQL, v5) в ClickHouse-коде
v6_ch не используется вовсе — ни `step0_sync_local/step0.py` (чистый CH-preflight, никаких
UPDATE/патчей менеджера), ни `step3_build_sources/step3.py` её не читают. Единственный источник поля
`менеджер` в v6_ch — `reference_data.gsheet_sites.sales_manager` (`step3.py:1137`, `_gs_pick_expr`),
единственный оверлей поверх него — ручной патч `_MISSING_MANAGERS`/`lotos91.ru` из Block H выше.
`local_gsheet_vse_klienty` в Victory PostgreSQL осталась как замороженные данные (22 строки, НЕ
синкается ни в v5, ни в v6_ch) — см. `DB_TABLES.md`, `SPEC.md` §«Признано ненужным для v6».

---

## Block H2: патч статусов Маркар из Google Sheets (v6_ch: step1, не step0)

**Файл:** `step1_load_raw/step1.py` (маркер `MARCAR_GSHEET_STATUS_2026-08-05`), функции
`_marcar_priority_expr`, `_marcar_gsheet_subquery`, `_marcar_join_sql`,
`_marcar_patched_status_expr`. **Отличие от v5:** в v5 это был `UPDATE` по локальной копии
`local_leads_all` внутри `step0_sync_local/step0.py::_patch_marcar_statuses()`. `raw_data.leads_all`
в v6_ch — реплика CRM, писать в неё нельзя, поэтому патч сдвинут на шаг вниз и выражен JOIN +
`multiIf` при сборке `raw_leads`/`raw_calls` (`step0.py` в v6_ch — чистый ClickHouse-preflight,
никаких UPDATE не делает, см. Block H2 контекст выше).

**Проблема:** Маркар ведёт Google Sheet «Маркар Доезды» (`reference_data.gsheet_priezdi_marcar`),
где фиксирует факт продажи/визита. У тех же лидов в CRM статус остаётся «Корзина» — CRM не
синхронизирует обратно.

**Решение:** статус патчится ВЫРАЖЕНИЕМ (не UPDATE) при построении `raw_leads` (`step1.py:426`) и
`raw_calls` (`step1.py:530`), по 4 статусам с приоритетом `MARCAR_STATUS_PRIORITY`
(`Продажа`=0 > `Дошел в КО`=1 > `Одобрение`=2 > `Приехал`=3, статус вне списка = 9999); CRM-статус
не перезаписывается «вниз» по воронке (строго выше приоритетом — иначе исходный статус остаётся):

```sql
if(
    l.source_type = 'marcar_crm_excel'
    AND ifNull(mp.status, '') != ''
    AND {приоритет(mp.status)} < {приоритет(l.status)},
    CAST(mp.status, 'Nullable(String)'),
    l.status
)
```
(`mp` — `LEFT JOIN` на подзапрос по `reference_data.gsheet_priezdi_marcar`, см.
`_marcar_gsheet_subquery()`.)

**Маппинг ID:** `link` = `https://crm.marcar.ru/leads/409449` → число после `/` =
`leads_all.source_record_id`. Только ссылки `crm.marcar.ru` (не `plex-crm.ru`), regex
`^https?://.+/[0-9]+$`.

**Категории статусов:** в v5 недостающие маппинги добавляла `_ensure_marcar_crm_statuses()` в
`local_crm_statuses`; в v6_ch нет прав на запись в `raw_data.*`, поэтому категории заданы кодом —
`step3_build_sources/step3.py::CODE_STATUS_CATEGORY` (маркер `CODE_STATUS_CATEGORY_2026-08-06`),
проверяется fail-fast функцией `check_code_status_categories()`.

---

## Block H4: колонки dohod_do_kredita и dobro (апрель 2026)

Во все `big_analytics_*` таблицы добавлены колонки `dohod_do_kredita BIGINT` и `dobro BIGINT`.

**Логика:** хардкод, для всех CRM (фильтр по source_type убран):
```sql
CASE WHEN status IN (...) THEN 1 ELSE NULL END
```
NULL если статус не совпадает (не 0).

**dohod_do_kredita** — клиент дошёл до этапа оформления кредита:
`Дошел в КО, Одобрить, Одобрен, Одобрен банк, Одобренные, Одобрение, Отказ по банкам, Урез, Продажа в кредит`

**dobro** — кредит одобрен:
`Одобрен, Одобрен банк, Одобренные, Урез, Одобрение`

**Урез** = банк одобрил кредит на сумму меньше запрошенной (входит в оба).

**Где генерируется:**
- `config/status_sql.py` — `_build_status_cases`, `_build_calls_agg`, `_build_leads_agg`
- `step3_build_sources/step3.py` — RESULT_COLUMNS + все CTE/SELECT
- `step6_build_full/step6.py` — COLS
- `step_cron_night/report_placement/step2_build_analytics.py` — DDL + INSERT + SELECT
- `step10_crop_targeting/load_crop_targeting_leads.py` — METRICS + NULL в SQL
- `step10_crop_targeting/load_crop_to_big_analytics.py` — INSERT + NULL::BIGINT
- `step_cron_night/direct_account_reviews/load_reviews_to_big_analytics.py` — INSERT + NULL::BIGINT

**Таблицы с колонками:** `big_analytics_full`, `big_analytics_direct`, `big_analytics_crop_targeting`, `big_analytics_seo`, `big_analytics_pixel`, `big_analytics_telegram`, `big_analytics_reviews`, `analytics_report_placement`.

---

## Block H3: колонка priedet (апрель 2026)

Во все `big_analytics_*` таблицы добавлена колонка `priedet BIGINT DEFAULT 0`.

**Логика:** хардкод, аналог `ne_otvechaet`/`filtr`/`nedozvon` — один конкретный статус:
```sql
CASE WHEN status = 'Приедет' THEN 1 ELSE 0 END AS priedet
```

**Где генерируется:**
- `config/status_sql.py` — `_build_status_cases`, `_build_calls_agg`, `_build_leads_agg`
- `step3_build_sources/step3.py` — RESULT_COLUMNS + все CTE/SELECT
- `step6_build_full/step6.py` — COLS
- `step_cron_night/report_placement/step2_build_analytics.py` — DDL + INSERT + SELECT
- `step10_crop_targeting/load_crop_targeting_leads.py` — METRICS + SQL
- `step10_crop_targeting/load_crop_to_big_analytics.py` — INSERT + SELECT
- `step_cron_night/direct_account_reviews/load_reviews_to_big_analytics.py` — INSERT + NULL::BIGINT

**Таблицы с колонкой:** `big_analytics_full`, `big_analytics_direct`, `big_analytics_crop_targeting`, `big_analytics_seo`, `big_analytics_pixel`, `big_analytics_telegram`, `big_analytics_reviews`, `analytics_report_placement`.

---

## Block I: маркеры строк для отзывов и посевов

⚠️ **v6_ch отличие:** отдельных лоадеров `load_reviews_to_big_analytics.py` /
`load_crop_to_big_analytics.py` в дереве нет — механика консолидирована прямо в
`step3_build_sources/step3.py` (отзывы: `_fetch_reviews_rows_from_postgres` + `_insert_reviews_from_postgres`)
и `step10_crop_targeting/step10.py` (Telegain/Google Sheets посевная ветка `big_analytics_crop_targeting`).
`big_analytics_crop_targeting` пишется ДВУМЯ ветками с разными маркерами: `step3.py::_build_crop_sql`
(UTM-классифицированные посевные лиды из `raw_leads`) и `step10.py` (Telegain API + gsheets-аккаунты).

### manager_login

| Таблица / писатель | Значение manager_login |
|----------------|----------------------|
| `big_analytics_reviews` (step3 `_fetch_reviews_rows_from_postgres`) | `'отзывы'` |
| `big_analytics_crop_targeting` (step3 `_build_crop_sql`) | `gs.directologist` (НЕ маркер-литерал) |
| `big_analytics_crop_targeting` (step10 Telegain/gsheets ветки, step10.py:715/813) | `'посевы'` |

**Зачем:** фильтрация по `manager_login IN ('отзывы','посевы')` отличает отзывные/посевные строки
от обычных direct-строк — работает для reviews всегда, для crop_targeting только для строк step10.

### тип_заявки и тип_сайта

| Писатель | тип_заявки | тип_сайта |
|------|-----------|----------|
| step3 `_fetch_reviews_rows_from_postgres` (reviews) | `'Отзывы'` | `'отзывы'` |
| step3 `_build_crop_sql` (crop, UTM-ветка) | `_claim_type_expr(...)` (`'Заявка'`/`'Звонки_CDR'`/NULL) | `gs.site_type` |
| step10.py (crop, Telegain/gsheets ветка) | `'Заявки'` | `gs.site_type`/`gd.site_type` (VK Ads-ветка — NULL) |

Канал посевов определяется через **`источник`** (`Посевы_*`) и `_source_table IN
('crop_targeting','tp8','tp9','tp10','social_посевы')`, не через `тип_сайта` и не через
`направление` — в `направление` значений `Посевы_*` НЕТ (там только Комплекс/Пиксель/Перформ/Отзывы,
см. [`CANON.md`](CANON.md)).

**Звонки:** `тип_заявки = 'Звонки'` (с заглавной; `step6_build_full/step6.py:208` inline SELECT, не step4) — `total_cost = NULL` для звонков, это ожидаемо.

### big_analytics_reviews — дополнительные маркеры

В `_fetch_reviews_rows_from_postgres()` (step3) следующие колонки проставляются литералами:

| Колонка | Значение |
|---------|---------|
| `"RlAdjustmentId_total"` | `'отзывы'` |
| `manager_login` | `'отзывы'` |
| `"Название crm"` | `'отзывы'` |
| `тип_заявки` | `'Отзывы'` (с заглавной) |
| `"тип_сайта"` | `'отзывы'` |
| `"шаблон"` | `'отзывы'` |

---

## Block J: Нормализация салонов (corrections.py)

### `_stage5_domain_salon()` — v6_ch эквивалент v5 `normalize_salons`

⚠️ **v6_ch отличие:** `normalize_salons(conn, tables)` (Python-функция, PostgreSQL `UPDATE` по списку
`COMPONENT_TABLES`) в этом дереве не существует — ни этой функции, ни константы `COMPONENT_TABLES`.
Эквивалент — `_stage5_domain_salon()` в `corrections.py`, инлайн-`multiIf`-выражение (стадия S5,
маркер в шапке файла), применяется ОДНОЙ пересборкой `SOURCE_TABLE = ad_analytics.big_analytics_sources`
внутри `apply()` (нет отдельного вызова после `load_reviews`/`load_crop` — тех скриптов тоже нет,
см. Block I). `SALON_ALIASES`/`DOMAIN_SALON_MAP` ниже — актуальные константы, читаются напрямую из
`corrections.py`.

Таким образом `big_analytics_full` строится step6 уже из нормализованных данных.

### SALON_ALIASES (неверное → правильное)

| Неверное | Правильное |
|----------|-----------|
| `Центр Авто Казань` | `Казань Центр Авто` |
| `АЦ Кит-Авто` | `Кит-Авто` |
| `АЦ на Жукова` | `Автоцентр на Жукова` |
| `АвтоПарк Южный` | `Автопарк Южный` |
| `М-Авто` | `М-авто` |
| `Южный обход` | `Южный Обход` |

### DOMAIN_SALON_MAP (домен → салон если NULL)

| Домен | Салон |
|-------|-------|
| `autotorg-ekb.ru` | `Кит-Авто` |
| `buymashina-e.ru` | `АвтоМаркет` |
| `probeg-ek.ru` | `АвтоМаркет` |

---

## Block K: Звонки и SEO для не-Яндекс (посевы) доменов

⚠️ **v6_ch реализация другая (функции ниже — `_add_crop_calls_sql`/`_add_crop_seo_sql`/
`_move_tp8_to_crop` — в текущем `step3_build_sources/step3.py` не существуют, проверено grep
2026-08-22):**
- **Звонки:** порт живёт в `step6_build_full/step6.py` (маркер `CROP_CALLS_PARITY_2026-08-06`),
  функция `_calls_select(lo, hi, crop=...)` с предикатом `_POSEV_CALL_DOMAIN_SQL` — крутится
  напрямую внутри сборки `big_analytics_full`, отдельного INSERT в `big_analytics_crop_targeting`
  для звонков 19 доменов нет.
- **SEO:** порт живёт в `step3_build_sources/step3.py::_build_seo_sql()` — лиды 19 доменов
  ОСТАЮТСЯ в `big_analytics_seo` (не переезжают в `big_analytics_crop_targeting`), но получают
  `источник='Посевы_SEO'`/`поставщик='Посевы'` через оверрайд по `_CROP_ACCOUNT_DOMAIN_SUBQUERY`.

Таблица `_source_table` значений и остальные детали ниже описывают ПРЕЖНЮЮ (v5) реализацию —
сверять с кодом, не полагаться на конкретные имена функций.

**Домены-источник:** `gsheets_crop_targeting_account` (19 доменов: Telegram/VK/MAX посевы без Яндекс Директ).

**Правило:** для этих 19 доменов звонки и SEO-лиды идут через `big_analytics_crop_targeting`, а не через обычные пути.

### step3_build_sources/step3.py

- `_build_seo_sql()` — в CTE `leads_seo` добавлен фильтр NOT IN по доменам из `gsheets_crop_targeting_account`. Эти домены не попадают в `big_analytics_seo`.
- `_add_crop_calls_sql(calls_agg_cases)` — INSERT звонков из `raw_calls` в `big_analytics_crop_targeting` только для 19 доменов. Вызывается в `run()` после `_move_tp8_to_crop`.
- `_add_crop_seo_sql(status_cases, priezd_sql)` — INSERT SEO-лидов (без UTM / seo utm) в `big_analytics_crop_targeting` только для 19 доменов. Вызывается после `_add_crop_calls_sql`.
- Константа `_CROP_DOMAIN_SUBQUERY` — общий subquery для фильтрации доменов (переиспользуется в обеих функциях).

### step6_build_full/step6.py

- В inline SELECT звонков (шаг 6) добавлен фильтр `NOT IN (_CROP_DOMAIN_SUBQUERY)`. Звонки для 19 доменов не попадают в big_analytics_full из raw_calls напрямую.

### Итог потока данных

| Путь | До изменений | После изменений |
|------|-------------|----------------|
| Звонки 19 доменов | `raw_calls` → step6 inline → `big_analytics_full` | `raw_calls` → step3 `_add_crop_calls_sql` → `big_analytics_crop_targeting` → step6 UNION ALL → `big_analytics_full` |
| SEO 19 доменов | `raw_leads` → `big_analytics_seo` → step6 → `big_analytics_full` | `raw_leads` → step3 `_add_crop_seo_sql` → `big_analytics_crop_targeting` → step6 UNION ALL → `big_analytics_full` |

### `_source_table` значения в big_analytics_crop_targeting

| Источник | `_source_table` |
|----------|----------------|
| `_build_crop_sql()` (Google Sheets посевы) | `'crop_targeting'` |
| `_move_tp8_to_crop()` (tp8 МК/ТК Telegram) | `'tp8'` |
| `_move_tp8_to_crop()` (tp9 МК/ТК Max) | `'tp9'` |
| `_move_tp8_to_crop()` (tp10 МК/ТК Telegram+Max) | `'tp10'` |
| `_add_crop_calls_sql()` (звонки 19 доменов) | `'calls'` |
| `_add_crop_seo_sql()` (SEO 19 доменов) | `'seo'` |

### ⚠️ Баг-фикс 1 (апрель 2026): DELETE в load_crop_to_big_analytics.py

**Проблема:** `load_crop_to_big_analytics.py` удалял ВСЕ строки `направление='посевы' AND поставщик IS DISTINCT FROM 'Яндекс'` включая звонки и SEO 19 доменов. После DELETE → INSERT заявок из Google Sheets эти строки не возвращались.

**Фикс:** DELETE сужен до точного `_source_table`:
```python
DELETE_SQL = "DELETE FROM big_analytics_full WHERE _source_table = 'crop_targeting'"
```

### ⚠️ Баг-фикс 2 (апрель 2026): tp8 строки исчезали из big_analytics_full

**Проблема:** `_move_tp8_to_crop()` проставлял tp8 строкам `_source_table = 'crop_targeting'`. Потом `load_crop_to_big_analytics.py` удалял все `_source_table='crop_targeting'` из `big_analytics_full` и вставлял только данные из Google Sheets — tp8 строки (14 152 шт.) не возвращались.

**Фикс:** в `_move_tp8_to_crop()` (step3_build_sources/step3.py):
```python
# было:
SET направление = 'посевы', _source_table = 'crop_targeting', источник = 'telegram_tp8'
# стало:
SET направление = 'посевы', _source_table = 'tp8', источник = 'telegram_tp8'
```

Теперь `load_crop_to_big_analytics.py` DELETE не затрагивает tp8 строки.

---

## Block L2: порядок выполнения шагов и Telegram-отчёт (май 2026, ЦЕЛИКОМ LEGACY v5/PostgreSQL)

⚠️ **Весь блок ниже описывает старый PostgreSQL `pipeline.py`, а не текущий v6_ch.** Проверено
2026-08-22: `step8_stats/step8.py` в v6_ch — простой ClickHouse row-count логгер (`TABLES` + `run()`,
без `STEP_LABELS`, без salon-группировки); `STEP8_INFO`/`load_crop_to_big_analytics.py`/
`cleanup_old_dates` в активном дереве не встречаются — живут только в
`archive/postgres_legacy_2026_07_31/` (напр. `fast_pipeline.py:110,630,699`, где `STEP8_INFO`
называется `FAST_STEP8_INFO`). `STEP_LABELS` не встречается вообще нигде в `.py` — ни в активном
дереве, ни в архиве: только в этом файле и `step8_stats/README.md:58-59`.
`step5_build_pixel/sync_pixel_config.py` — файл на месте, но в v6_ch его никто не вызывает
(ни `pipeline.py::STEPS`, ни соседние модули): ручная утилита, не шаг пайплайна.
В `pipeline.py::STEPS` (см. [`PIPELINES.md`](PIPELINES.md#steps-map)) step8=8 стоит ПРОСТО ИНЛАЙН в
общем списке, рядом с verify=900, никакого deferred-механизма через отдельную константу нет.
Telegram-отчёт в v6_ch формирует `cron_run.py` (`build_message`/`build_steps_section`,
`notifications/telegram.py::send_html`) поверх `data_quality_log`, а не сам `step8`.
Оставлено как исторический контекст v5, не инструкция к действию для v6_ch.

---

## Block L: 404_errors — инкрементальная загрузка (апрель 2026)

**Файл:** `step_cron_night/404_errors/404_errors.py`

Таблица: `public.yandex_direct_404_errors`

### Логика дат

- Таблица **не существует** → загрузка с `2026-01-01` до сегодня
- Таблица **существует** → `MAX(visit_date) - 7 дней` → удалить строки `visit_date >= date_from` → загрузить заново

```python
# Ключевая функция:
def _get_date_range() -> tuple[str, str]:
    ...
    date_from = (max_date - timedelta(days=7)).isoformat()
```

7-дневный перекрёст нужен для перезаписи данных которые Метрика могла дополнить ретроспективно.

### Колонка week_start

Добавлена аналогично `big_analytics_full`:
```python
_week_start(visit_date)  # понедельник недели: date - timedelta(days=date.weekday())
```

`ALTER TABLE ... ADD COLUMN IF NOT EXISTS week_start DATE` — безопасно при апгрейде существующей таблицы без колонки.

---

## Block G: corrections.py — правила корректировок

⚠️ **v6_ch:** запускается в `pipeline.py::STEPS` как шаг `corrections` (после step4 и step3, порядок
step4→step3→corrections→step5). Применяется ОДНОЙ пересборкой `SOURCE_TABLE = ad_analytics.big_analytics_sources`
(ClickHouse `multiIf`-выражения, не построчный PostgreSQL `UPDATE` по отдельным `public.*`-таблицам).

**Важно:** колонка специалиста называется `"специалист"` (не `директолог`).

### Правило 1: Кудерко Семен

`специалист = 'Кудерко Семен'` для строк `Date < toDate('2026-04-10')` по списку 67 аккаунтов
(`_KUDERKO_LOGINS` в `corrections.py`).

### Правило 1б: Сергеев Алексей

`специалист = 'Сергеев Алексей'` для строк `Date < toDate('2026-04-21')` (до 20 апреля включительно)
по списку 9 аккаунтов (`_SERGEEV_LOGINS`).
Аккаунты: `porg-tde4jof6`, `kazan-ca-532199-z761`, `e-20074360`, `porg-wzisnv32`, `porg-rmkn7sz4`, `porg-2xphfcul`, `e-20074359`, `porg-fuko7yzw`, `e-20074361`.

### Правило 1в: Питеркина Дарья

⚠️ **Полнее, чем описано ранее** (проверено по коду 2026-08-22): ДВЕ ветки в `specialist_correction_expr`:
1. `специалист = 'Питеркина Дарья'` для строк `Date < toDate('2026-06-19')` по списку **28 аккаунтов**
   (`_PITERKINA_LOGINS`, включает `porg-o2lqtxk5`).
2. Отдельный fallback БЕЗ ограничения по дате: `специалист = 'Питеркина Дарья'` где `специалист` пуст
   для аккаунта `porg-o2lqtxk5` (`_PITERKINA_LOGIN`).

Аккаунт `porg-54oakaa3` в текущем коде не встречается (не найден в `corrections.py`).

### Правило 1д: Чепелев Никита (не документировано ранее)

`специалист = 'Чепелев Никита'` для строк `Date < toDate('2026-07-17')` по списку 7 аккаунтов
(`_CHEPELEV_LOGINS`). Матч ТОЛЬКО по `account_login` (маркер `CHEPELEV_LOGIN_ONLY_2026-08-06`),
не по паре домен+логин — см. комментарий в коде для мотивации.

⚠️ **Правила 2–4 и `_recompute_ag_parts()` ниже описывают старую PostgreSQL-реализацию.** В v6_ch
эквивалент — `_stage3_adgroup_maps()` в `corrections.py` (S3): 7 `LEFT JOIN reference_data.gsheet_naming`
по `ag_part1..7`, ClickHouse-выражение внутри пересборки `SOURCE_TABLE`, БЕЗ `ctid`-подзапроса (это
PostgreSQL-специфичный системный столбец, в ClickHouse не существует) и БЕЗ отдельной
UNLOGGED-таблицы `_tmp_ag_parts_lookup`. `local_gsheet_naming` (PostgreSQL) → `reference_data.gsheet_naming`
(ClickHouse). tp6/tp7 → `'MK/TK'` сохранено (см. код `_stage3_adgroup_maps`, строка с комментарием
про `_recompute_ag_parts`).

### Правило 2: Исправление AdGroupName (из v3)

| Аккаунт(ы) | Что → На что |
|------------|-------------|
| e-20076545, e-20074366 | `_g011_g` → `_ag011_g` |
| e-20076545 | `_off_` → `_aoff_` (guard) |
| cars-yekaterinburg-541349-lrqf | `_n000_n000_` → `_n000_` |
| porg-akiqrh6u | `ct0054aoff/aon`, `ct0076aoff/aon` → добавляет `_` |
| e-20085128/130/132/135, e-20086083 | `Новая группа` → правильное название |

### Правило 3: Переизвлечение adgroup_code

Regex из исправленного `AdGroupName` для аккаунтов где `adgroup_code IS NULL OR = 'неверный кодер'`.

### Правило 4: Пересчёт ag_part1..7 + adgroup_code для tp6/tp7

В том же UPDATE (JOIN с `local_gsheet_naming`, subquery с `ctid`):

- `ag_part1..7`: если `tp IN ('tp6','tp7')` → `'MK/TK'`, иначе — из `adgroup_code` через naming
- `adgroup_code`: если `tp IN ('tp6','tp7')` AND (`adgroup_code IS NULL` или `''` или `'неверный кодер'`) → `'MK/TK'`, иначе оригинал

Причина subquery: в PostgreSQL нельзя ссылаться на обновляемую таблицу в FROM/JOIN напрямую.

### `_recompute_ag_parts()` — глобальный пересчёт ag_part1..7 (Rule 0c)

Выполняется в `corrections.apply()` до остальных правил. Пересчитывает `ag_part1..7` и `verdict` для всех строк `big_analytics_direct`.

**Оптимизация (май 2026):** вместо UPDATE-by-ctid с 7 LEFT JOIN на 2.5M строк — двухэтапный подход через UNLOGGED lookup-таблицу:

1. Строится `_tmp_ag_parts_lookup` по DISTINCT `adgroup_code` (~14k строк) — JOIN делается один раз
2. UPDATE `big_analytics_direct` SET ag_part1..7 = lookup JOIN — сканируется по индексу

Результат: 188 сек → 63 сек (3× быстрее).

**Обработка tp6/tp7:** строки с `tp IN ('tp6','tp7')` обновляются отдельным UPDATE: `ag_part1..7 = 'MK/TK'`, `verdict = 'ok'`. Это сохраняет поведение v1 (tp6/tp7 не проходят через lookup, даже если `adgroup_code` некорректный).

> **Правило 8 `_rule8_utm_classify`/`_UTM_PIXEL_SOURCES`** — историческое, в текущем `corrections.py`
> не встречается (grep 2026-08-22: 0 совпадений). Живой канон пикселя — `'Пиксель'`, нормализуется
> после step11/step13, см. `CANON.md` → канон «направление».

### SPEC_FALLBACK v2→v3 — каскадное заполнение специалиста

**Правило:** пустой `"специалист"` заполняется каскадом: `directologist` → `direction_main` → `'Звонки'`/`'Без специалиста'`.

**v2** — внутри `_stage6_labels()` в `corrections.py::apply()` (стадия S6, `account_rules` +
`coalesce(gsp.directologist, gsp.direction_main, ...)`): покрывает `SOURCE_TABLE` на момент
выполнения corrections (после step4 и step3, до step5) — не видит строки, которые появляются в
`big_analytics_full` ПОЗЖЕ (calls из step6, пиксель из step11, посевной оверлей из step10).

**v3** — файл `spec_fallback.py` (маркер `SPEC_FALLBACK_V3_2026-08-06`, module-level docstring
цитирует происхождение из v5 `apply_spec_fallback_v3()`), **ОДНА** точка вызова: шаг `115` в
`pipeline.py::STEPS`, сразу после step10/step11 и **до** step12/step13 (не после, как было раньше) —
так что `big_analytics_full_arrival` и звезда строятся уже по заполненной колонке. Матчит по домену,
БЕЗ окна `launch_date…block_date` (в отличие от `step3._domain_specialist_expr`).

### VACUUM_WAVE3 + PREFREE_MOVE (2026-07-02)

**VACUUM_WAVE3** (`corrections.py`, маркер `VACUUM_WAVE3_2026-07-02`): третий `_interim_vacuum` добавлен в конец `apply()` после `_fix_account_domain_backfill`. Закрывает волну 3 (rule6 + normalize_salons + perform_direction + fix_missing_managers + fix_account_domain_backfill = 14+ UPDATE без vacuum). Снижает пик dead tuples `big_analytics_direct` на 5-7 GB.

Итого три вакуума в `apply()`: после волны 1 (rule0b/0c) → после волны 2 (rule1..rule4b) → после волны 3 (все остальные).

**PREFREE_MOVE** (`fast_pipeline.py`, маркер `PREFREE_MOVE_2026-07-02`): блок TRUNCATE `big_analytics_direct` + `raw_yandex` перенесён со строки после compactify на строку ДО `compactify_full`. compactify теперь всегда видит +14 GB свободного диска и выполняет CTAS-swap (9→5 GB). Безопасность: `cleanup_old_dates` (DELETE FROM big_analytics_direct) выполняется ДО PREFREE; `compactify_full` читает только `big_analytics_full`, `big_analytics_direct` не читает.

---

## Block M: Операционные фиксы конвейера (2026-07)

### CASCADE_MATCH step3 — каскадный матчинг direct_unmatched (2026-07-03)

**Файл:** `step3_build_sources/step3.py` (маркер `CASCADE_MATCH_2026-07-03` × 3).

**Проблема:** ~10 400 заявок в бине `direct_unmatched`/«неверный кодер». Root-cause — три типа несовпадения ключа `Date|CampaignId|AdGroupId|Device|RlAdjustmentId`:
- H3 (~70-80%): Медиа-Актив домены передают UTM без group_id → `key3` лида имеет `|0|`
- H3_RlAdj (~10%): park-auto93 — RlAdjustmentId в UTM, но CRM не парсит → `correction_id=0`
- H2 (~16%): byautos-34 — протухший yclid, `created_date` лида ≠ дата клика → date-несовпадение

**Решение — каскадный матчинг** (убираем поля справа по уровням):
- Level 4: `дата|campaign|group` (−correction_id) → ловит H3_RlAdj
- Level 3: `дата|campaign` (−group −correction_id) — не реализован, пропущен в финальном дизайне
- Level 2: `дата|campaign` (−group −device −correction_id) → ловит H3

Cascade-строки: `_source_table='direct'`, `total_cost=NULL`, колонка `cascade_level TEXT` ('4'/'3'/'2'). Дата и campaign_code сохраняются — перематч невозможен. Расход golden не затрагивается (total_cost=NULL).

Результат: `direct_unmatched` (бин «неверный кодер» в `big_analytics_unified`) — 2 400→68 заявок (-99.3%).

### PIPELINE_GUARD build_spend_daily (2026-07-03)

⚠️ **v6_ch:** `build_spend_daily.py` живёт только в `archive/postgres_legacy_2026_07_31/step_cron_night/`,
в активном дереве v6_ch его нет — историческая справка, не текущий код.

**Файл:** `step_cron_night/build_spend_daily.py` (маркер `PIPELINE_GUARD_2026-07-03` × 4).

Guard в начале `main()` — ДО TG-отбивки «стартовал»: сканирует `/proc/*/cmdline` на `pipeline.py` / `fast_pipeline.py` / `pipeline_powerbi.py` (исключает собственный PID). Если жив — ждёт до 30 мин, опрашивает каждые 2 мин. Истёк GUARD_WAIT_MAX_SEC → TG SKIP + `exit 0`. TG «стартовал» и t_total — только после прохождения guard.

### TG_PROXY_ROTATE + TG_SEND_FAIL (2026-07-03, СУПЕРСЕДЕНО 2026-08-14)

**pipeline_powerbi.py** (маркер `TG_PROXY_ROTATE_2026-07-03`): `_send_telegram` переведён с единственного `TELEGRAM_PROXY` на цикл `TELEGRAM_PROXY_VARIANTS`; после исчерпания всех прокси — `logger.error`. *(В v6_ch `pipeline_powerbi.py` — legacy, `archive/postgres_legacy_2026_07_31/`, не в активном пайплайне.)*

**config/cookies.py** (маркер `TG_SEND_FAIL_2026-07-03`): `logger.error(...)` добавлен в `send_tg` и `send_tg_cookies_dead` после for-цикла прокси. Ранее молчаливый провал TG теперь виден в логе. *(С 2026-08-14: `send_tg_cookies_dead` удалена, `send_tg` — тонкая обёртка над `notifications/telegram.py::send_html`, тот же `logger.error` behavior — см. COOKIES.md.)*

### Форматирование отчётов step8/step12/verify (2026-07-02, историческая правка v5)

⚠️ **v6_ch:** `step8_stats/step8.py` сейчас — простой ClickHouse row-count логгер (`TABLES` + `run()`,
без salon-группировки, без `T_CAMPAIGN_STATUS`). `data_check/verify_big_analytics.py` не содержит
функции `format_digest` и не выводит ✅/❌-чеклист — `run()` пишет результат напрямую. Запись ниже
относится к прежней PostgreSQL-версии обоих файлов, для v6_ch не актуальна.

- `step8_stats/step8.py`: салоны группируются по `salon_name` с суммой (один салон = одна строка); источник блока Кампании/UTM переключён на `T_CAMPAIGN_STATUS` (вместо транзиентной `T_DIRECT`); kval-диапазон `[7 000;15 000]` → `[10 000;30 000]`.
- `data_check/verify_big_analytics.py`: `format_digest` переписан — чеклист 16 блоков с ✅/❌ в столбик; kval_cost `7k/15k` → `10k/30k`.
- `step12_proverka_big_analytics/step12.py`: пояснение концентрации с Δ% и CID; явная итоговая строка РАСХОД «✅ Все агентства в допуске» / «⚠️ Вне допуска: список».
