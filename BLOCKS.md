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
(и `raw_perform_leads`, которая строится через ту же функцию): `LEFT JOIN raw_data.domains AS d`
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

В CTE `leads_direct` — исключение:
```sql
AND NOT (l.utm_source = 'seo' AND l.utm_medium = 'organic')
```
В CTE `leads_seo` — включение:
```sql
WHERE (utm_source IS NULL OR utm_source = '')
   OR (utm_source = 'seo' AND utm_medium = 'organic')
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

### Постпроцессинг: заполнение по салону (step4)

После UNION ALL — UPDATE: NULL "Название crm" заполняется значением из другой строки того же салона. Закрывает случай когда у домена нет лидов вообще, но у другого домена того же салона CRM есть.

Домены без CRM вообще ("Ай Кар" и др.) — остаются NULL, это ожидаемо.

### Постпроцессинг: заполнение по салону в load_crop_to_big_analytics.py (апрель 2026)

`load_crop_to_big_analytics.py` вставляет строки **после** step4 → backfill step4 уже не запустится для новых строк. Поэтому тот же backfill добавлен в конец `main()` этого скрипта:

```sql
UPDATE public.big_analytics_full f
SET "Название crm" = src.crm_name
FROM (
    SELECT "салон", MAX("Название crm") AS crm_name
    FROM public.big_analytics_full
    WHERE "Название crm" IS NOT NULL
    GROUP BY "салон"
) src
WHERE f."Название crm" IS NULL AND f."салон" = src."салон"
```

Покрывает ~774 строки Google Sheets посевов. "Ай Кар" остаётся NULL (нет CRM нигде).

---

## Block G2: step4 — постпроцессинговые UPDATE (после сборки big_analytics_full)

Выполняются в `step6_build_full/step6.py` после UNION ALL, каждый в отдельном `with conn.cursor()` + `conn.commit()`.

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

**Файл:** `corrections.py`, функция `_fix_missing_managers()`. **Живой код** — вызывается из
`corrections.py::apply()` (между step3 и step4), рядом с `_fix_account_domain_backfill`.

Ручной патч домена, чей `sales_manager` отсутствует в `local_gsheet_sites`:
`lotos91.ru` относится к аккаунту `avto_0358` (АвтоЛидер) — тот же менеджер, что и у аккаунта
`avto_0083` (Лидер).

```sql
UPDATE public.big_analytics_direct
SET менеджер = 'Михаил Яковлев'
WHERE domain = 'lotos91.ru'
  AND (менеджер IS NULL OR менеджер = '')
```

⚠️ **Отдельно (не путать с патчем выше):** таблица `gsheet_vse_klienty`/`local_gsheet_vse_klienty` и
функция `_patch_vse_klienty_manager()` из `step0_sync_local/step0.py` **удалены из кода** — их больше
нет ни в `config/settings.py` (список GSHEET-синков), ни в `step0.py`. Основной источник поля
`менеджер` теперь — `T_GSHEET_AUTOSALONY` (`local_gsheet_autosalony_clients`), у которой колонка
`менеджер` есть по умолчанию. `step3_build_sources/step3.py` берёт `COALESCE(NULLIF(TRIM(gs.sales_manager),''),
NULLIF(TRIM(auto.менеджер),''))` через `LEFT JOIN {T_GSHEET_AUTOSALONY} auto ON gs.client_id =
auto.id_салона` (см. например `step3.py:661-663`). `local_gsheet_vse_klienty` в БД осталась как
замороженные данные (22 строки, НЕ синкается) — см. `DB_TABLES.md`.

---

## Block H2: патч статусов Маркар из Google Sheets (step0)

**Файл:** `step0_sync_local/step0.py`, функция `_patch_marcar_statuses()`.

**Проблема:** Маркар ведёт Google Sheet «Маркар Доезды», где фиксирует факт продажи/визита. В `local_leads_all` у тех же лидов статус остаётся «Корзина» — CRM не синхронизирует обратно.

**Решение:** после синка в step0 — UPDATE статуса из gsheet в `local_leads_all` по 4 статусам с приоритетом
`_MARCAR_STATUS_PRIORITY` (`Продажа`=0 > `Дошел в КО`=1 > `Одобрение`=2 > `Приехал`=3); CRM-статус не
перезаписывается «вниз» по воронке (если уже `'Продажа'`, патч `'Приехал'` не применится):

```sql
UPDATE public.local_leads_all l
SET status = pm.status
FROM public.local_gsheet_priezdi_marcar pm
WHERE pm.link LIKE '%crm.marcar.ru%'
  AND pm.link ~ '^https?://.+/[0-9]+$'
  AND pm.status IN ('Продажа', 'Дошел в КО', 'Одобрение', 'Приехал')
  AND l.source_record_id = REGEXP_REPLACE(pm.link, '^.+/', '')
  AND l.source_type = 'marcar_crm_excel'
  AND l.status IS DISTINCT FROM pm.status
```

**4 статуса с приоритетом** (не только 'Продажа') — недостающие маппинги `Дошел в КО`/`Одобрение` в
`local_crm_statuses` добавляет `_ensure_marcar_crm_statuses()` перед патчем.

**Маппинг ID:** `link` = `https://crm.marcar.ru/leads/409449` → число после `/` = `local_leads_all.source_record_id` (TEXT). Только ссылки `crm.marcar.ru` (не plex-crm.ru).

**Порядок вызова в run():** `_patch_crm_statuses` (закомментирован с 2026-05-20, crm_statuses уже верный) → `_patch_marcar_statuses`. Выполняется до step1 → корректные статусы попадают в raw_leads.

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

### manager_login

| Таблица / файл | Значение manager_login |
|----------------|----------------------|
| `big_analytics_reviews` (step3 `_build_reviews_sql`) | `'отзывы'` |
| `big_analytics_crop_targeting` (step3 `_build_crop_sql`) | `'посевы'` |
| `big_analytics_full` (load_reviews_to_big_analytics.py) | `'отзывы'` |
| `big_analytics_full` (load_crop_to_big_analytics.py) | `'посевы'` |

**Зачем:** ранее `manager_login = NULL` → строки неотличимы от обычных. Теперь фильтрация по `manager_login IN ('отзывы','посевы')` работает.

### тип_заявки и тип_сайта для пост-пайплайн лоадеров (апрель 2026)

| Файл | тип_заявки | тип_сайта |
|------|-----------|----------|
| `load_reviews_to_big_analytics.py` | `'отзывы'` | `'отзывы'` |
| `load_crop_to_big_analytics.py` | `'заявки'` | `'посевы'` |

Ранее оба поля были `NULL` → строки с расходами не имели типа заявки. `тип_заявки='заявки'` — посевы-заявки учитываются наравне с прямыми. Канал определяется через `направление='посевы'` и `_source_table='crop_targeting'`. Фильтрация посевов: `"тип_сайта" IN ('отзывы','посевы')` или `направление='посевы'`.

**Звонки:** `тип_заявки = 'звонки'` (step4 inline SELECT) — `total_cost = NULL` для звонков, это ожидаемо.

### big_analytics_reviews — дополнительные маркеры 'отзывы'

В `_build_reviews_sql()` (step3) следующие колонки проставляются как `'отзывы'`:

| Колонка | Значение |
|---------|---------|
| `"RlAdjustmentId_total"` | `'отзывы'` |
| `manager_login` | `'отзывы'` |
| `"Название crm"` | `'отзывы'` |
| `тип_заявки` | `'отзывы'` |
| `"тип_сайта"` | `'отзывы'` |
| `"шаблон"` | `'отзывы'` |

---

## Block J: Нормализация салонов (corrections.py)

### normalize_salons

```python
normalize_salons(conn, tables=None)
# tables=None → ['big_analytics_full']  (вызов из pipeline.py после load_reviews/load_crop)
# tables=COMPONENT_TABLES               (вызов из apply() после step3)
```

`COMPONENT_TABLES` = `big_analytics_direct`, `big_analytics_seo`, `big_analytics_pixel`, `big_analytics_telegram`, `big_analytics_reviews`, `big_analytics_crop_targeting`.

### Поток нормализации

1. `corrections.apply()` (между step3 и step4) → `normalize_salons(conn, COMPONENT_TABLES)` — нормализует все компонентные таблицы
2. `pipeline.py` после `load_reviews` + `load_crop` → `normalize_salons(norm_conn)` (default) — нормализует `big_analytics_full`

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

## Block L2: порядок выполнения шагов и Telegram-отчёт (май 2026)

### step8 выполняется последним (deferred)

`step8_stats` вынесен из STEPS-массива в отдельную константу `STEP8_INFO`. Выполняется **после всех дополнительных скриптов**: load_reviews, 404_errors, normalize_salons, cleanup_old_dates.

```python
STEPS = [(0,...), (1,...), (2,...), (3,...), (5,...), (4,...), (6,...), (7,...), (9,...), (10,...)]
STEP8_INFO = (8, 'step8_stats', 'step8_stats.step8', 'step8_stats')
# ... после load_reviews, 404_errors, normalize_salons, cleanup_old_dates:
if not failed:
    run_step(8, 'step8_stats.step8', run_id)
```

**Зачем:** step8 читает `data_quality_log` для формирования Telegram-отчёта. Если step8 в STEPS-массиве — дополнительные шаги (load_reviews и др.) ещё не выполнились → не попадают в отчёт.

### step8 добавляет себя в отчёт (self-tracking)

step8 не может записать своё время в `data_quality_log` перед тем как сформирует отчёт (запись делается после `run_step`). Поэтому добавляет синтетическую запись:

```python
durations = list(stats.get('step_durations', []))
durations.append(('step8', time.perf_counter() - t0))
stats['step_durations'] = durations
```

### sync_pixel_config логируется в data_quality_log (май 2026)

`sync_pixel_config` ранее не писался в `data_quality_log` — не появлялся в Telegram-отчёте. Добавлен `log_step()` вызов в `pipeline.py`:

```python
log_step(conn, run_id, 'sync_pixel_config', 'ok', rows_affected=n, duration_sec=elapsed)
```

### Все шаги в Telegram-отчёте

После исправлений в отчёте появляются шаги: `sync_pixel_config`, `load_reviews`, `404_errors`, `normalize_salons`, `cleanup_old_dates`, `step10`, `step8`.

Метки в `STEP_LABELS` (step8_stats/step8.py): добавлены `'step10'`, `'load_reviews'`, `'404_errors'`, `'normalize_salons'`, `'cleanup_old_dates'`.

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

Запускается в `pipeline.py` **между шагом 3 и шагом 4**.
Применяет правила к `public.big_analytics_direct` и `public.big_analytics_crop_targeting`, в конце — нормализацию салонов по всем компонентным таблицам.

**Важно:** колонка специалиста называется `"специалист"` (не `директолог`).

### Правило 1: Кудерко Семен

`SET "специалист" = 'Кудерко Семен'` для строк `"Date" < '2026-04-10'` по списку ~65 аккаунтов.
Таблицы: `big_analytics_direct` + `big_analytics_crop_targeting`.

### Правило 1б: Сергеев Алексей

`SET "специалист" = 'Сергеев Алексей'` для строк `"Date" < '2026-04-21'` (до 20 апреля включительно).
Таблицы: `big_analytics_direct` + `big_analytics_crop_targeting`.
Аккаунты: `porg-tde4jof6`, `kazan-ca-532199-z761`, `e-20074360`, `porg-wzisnv32`, `porg-rmkn7sz4`, `porg-2xphfcul`, `e-20074359`, `porg-fuko7yzw`, `e-20074361`.

### Правило 1в: Питеркина Дарья (апрель 2026)

`SET "специалист" = 'Питеркина Дарья'` где `специалист IS NULL` для аккаунта `porg-o2lqtxk5`.
Таблицы: `big_analytics_direct` + `big_analytics_crop_targeting`.
Аккаунт `porg-54oakaa3` — пропускаем (нет данных о специалисте).

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

> **Правило 8 `_rule8_utm_classify`** (классификация пиксель-источников в `'пиксель'`) — см. `CANON.md` → канон «направление».

### SPEC_FALLBACK v2→v3 — каскадное заполнение специалиста (2026-07-03)

**Правило:** пустой `"специалист"` заполняется каскадом: `directologist` → `direction_main` → `'Звонки'`/`'Без специалиста'`.

**v2** (`_rule_fill_specialist_fallback` в `corrections.apply()`): покрывает строки в `COMPONENT_TABLES` на момент выполнения corrections (между step3 и step4).

**v3** (`apply_spec_fallback_v3(conn, tables)` в `corrections.py`): покрывает источники, создаваемые ПОСЛЕ corrections — pixel/calls/пиксель_атрибуц/crop_targeting/arrival.
Две точки вызова (маркер `SPEC_FALLBACK_V3_2026-07-03`):
1. `pipeline.py` / `fast_pipeline.py` — после step11, ДО step13_rebuild: закрывает `big_analytics_full` (calls, пиксель, пиксель_атрибуц, crop_targeting)
2. `pipeline.py` / `fast_pipeline.py` — после step13_rebuild: закрывает arrival (direct по дате визита, crop, pixel)

Результат: NULL специалист в `fact_big_analytics` = 0 (по 7 820 строкам в прогоне SPEC_FALLBACK_V3_ПРОГОН run_id=82211aeb).

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

**Файл:** `step_cron_night/build_spend_daily.py` (маркер `PIPELINE_GUARD_2026-07-03` × 4).

Guard в начале `main()` — ДО TG-отбивки «стартовал»: сканирует `/proc/*/cmdline` на `pipeline.py` / `fast_pipeline.py` / `pipeline_powerbi.py` (исключает собственный PID). Если жив — ждёт до 30 мин, опрашивает каждые 2 мин. Истёк GUARD_WAIT_MAX_SEC → TG SKIP + `exit 0`. TG «стартовал» и t_total — только после прохождения guard.

### TG_PROXY_ROTATE + TG_SEND_FAIL (2026-07-03)

**pipeline_powerbi.py** (маркер `TG_PROXY_ROTATE_2026-07-03`): `_send_telegram` переведён с единственного `TELEGRAM_PROXY` на цикл `TELEGRAM_PROXY_VARIANTS`; после исчерпания всех прокси — `logger.error`.

**config/cookies.py** (маркер `TG_SEND_FAIL_2026-07-03`): `logger.error(...)` добавлен в `send_tg` и `send_tg_cookies_dead` после for-цикла прокси. Ранее молчаливый провал TG теперь виден в логе.

### Форматирование отчётов step8/step12/verify (2026-07-02)

- `step8_stats/step8.py`: салоны группируются по `salon_name` с суммой (один салон = одна строка); источник блока Кампании/UTM переключён на `T_CAMPAIGN_STATUS` (вместо транзиентной `T_DIRECT`); kval-диапазон `[7 000;15 000]` → `[10 000;30 000]`.
- `data_check/verify_big_analytics.py`: `format_digest` переписан — чеклист 16 блоков с ✅/❌ в столбик; kval_cost `7k/15k` → `10k/30k`.
- `step12_proverka_big_analytics/step12.py`: пояснение концентрации с Δ% и CID; явная итоговая строка РАСХОД «✅ Все агентства в допуске» / «⚠️ Вне допуска: список».
