# FUNNEL.md — воронка заявок (`reference_data.crm_status_mapping`)

> Источник правды по воронке статусов. Вынесено из `CLAUDE.md` (lazy-load).
> Высокоуровневые инварианты воронки — также в `PROJECT_CHARTER.md` §5.

---

Источник активного ClickHouse-контура: `reference_data.crm_status_mapping`. Локальный
PostgreSQL-генератор `config/status_sql.py` оставлен как legacy/порт v5 и не является
источником расчёта `big_analytics_full` в v6.

Активный код: `step3_build_sources/step3.py::_metric_expr()` и `_category_match_expr()`.
Ключ CRM получается из `source_type` через `CRM_BY_SOURCE_TYPE`; недостающие категории
Маркара/PLEX временно задаются кодом в `CODE_STATUS_CATEGORY`.

## Воронка (май 2026 — рефакторинг status/reason разделение)

**Status-сторона** (берёт `leads.status`, `kind='status'`):

```
обращения  ⊇ заявки ⊇ квал ⊇ визит ⊇ продажа
kol_vo_zayavok ⊇ korr ⊇ kval ⊇ priezd ⊇ prodazhi
```

**Reason-сторона** (берёт `leads.reason`, `kind='reason'`):

```
доход ⊇ добро
dohod_do_kredita ⊇ dobro
```

Reason-сторона полностью отдельная от status — продажи на стороне reason тоже считаются
(`sale ⊆ approved ⊆ credit` через auto-merge внутри reason). На status-стороне продажи
также автоматически входят в `approved`, `credit`, `visit`, `qualified`, `correct`.

## Маппинг метрик

| Метрика | lead_status | kind | Источник |
|---------|------------|------|----------|
| **kol_vo_zayavok** | — | — | `status IS NOT NULL` (хардкод) |
| **korr** | `correct` | `status` | reference_data.crm_status_mapping / CODE_STATUS_CATEGORY |
| **kval** | `qualified` | `status` | reference_data.crm_status_mapping / CODE_STATUS_CATEGORY |
| **priezd** | `visit` | `status` | reference_data.crm_status_mapping / CODE_STATUS_CATEGORY |
| **prodazhi** | `sale` | `status` | reference_data.crm_status_mapping / CODE_STATUS_CATEGORY |
| **nekorr** | `incorrect` | `status` | reference_data.crm_status_mapping / CODE_STATUS_CATEGORY |
| **dohod_do_kredita** | `credit` | `reason` | reference_data.crm_status_mapping |
| **dobro** | `approved` | `reason` | reference_data.crm_status_mapping |

## Хардкод (не из таблицы)

| Метрика | Значения | Колонка |
|---------|---------|---------|
| **ne_otvechaet** | `'Не отвечает'`, `'Новая: Не отвечает'` | status |
| **filtr** | `'Фильтр'` | status |
| **nedozvon** | `'Недозвон'` | status |
| **priedet** | `'Приедет'` | status |

`kval` считается **прямо из категории `qualified`** в `reference_data.crm_status_mapping`
/ `CODE_STATUS_CATEGORY` (раньше в v5 была формула `korr − ne_otvechaet − filtr − nedozvon`).

> ✅ Это **корректная** формула (не регрессия). На golden-срезе Кудерко даёт **kval ≈ 677**
> (не старые ~1752), стоимость квала ≈ 20 913 ₽ — согласовано с [`GOLDEN_BASELINE.md`](GOLDEN_BASELINE.md)
> (re-baseline 2026-07-15) и блоком 14 `verify_big_analytics.py`.

## Auto-merge / category sets

В v6 вложенность воронки задаётся не отдельным SQL-мерджем, а наборами категорий в
`step3_build_sources/step3.py::_metric_expr()`:

**Status-сторона:**
```
korr    = correct + qualified + visit + sale + credit + approved
kval    = qualified + visit + sale + credit + approved
priezd  = visit + sale + credit + approved
prodazhi = sale
```

**Reason-сторона:**
```
dohod_do_kredita = reason category credit + approved
dobro            = reason category approved
```

Гарантия: `korr ≥ kval ≥ priezd ≥ prodazhi` (status), `dohod_do_kredita ≥ dobro` (reason).

## Переопределения по CRM/салону

| Категория | crm_name | salon | Статус | kind |
|-----------|---------|-------|--------|------|
| `credit` | MEGA | Платина | Отказ по банкам | reason |
| `credit`/`visit` | PLEX | УрбанКар / — | Консультация | status/reason |

> ⚠️ **salon-override `kind='status'` активирует salon-ветку и в ЗВОНКАХ.** Появление строки
> `salon<>''` `kind='status'` (например по «Лидер») включает salon-условие не только в
> агрегате лидов, но и в `_build_calls_agg` (генератор агрегата звонков, `config/status_sql.py`).
> Путь звонков = `raw_calls c LEFT JOIN gsheet_sites gs`, поэтому salon там берётся **только
> из `gs."salon"`** (в `raw_calls` колонки `salon` нет), а `source_type` — **сырой `c.source_type`**
> (`mauto`/`crmf_excel`/…, совпадает с ключом override; маппленый `Фаиг`/`Плекс` сломал бы матч).
> Латентный баг алиаса `c.salon`, разбуженный этим, исправлен — см.
> [`KNOWN_ISSUES.md`](KNOWN_ISSUES.md) #11.

## Как добавить новый статус

```sql
-- Канонический путь: добавить строку в ClickHouse-справочник
-- reference_data.crm_status_mapping (crm, status, reason, salon, category).
-- Если у pipeline-роли нет GRANT на запись или нужен временный мост,
-- добавить пару в step3_build_sources/step3.py::CODE_STATUS_CATEGORY
-- и держать ее там до появления строки в справочнике.
```

После изменения справочника или `CODE_STATUS_CATEGORY` следующий запуск подхватит категорию через
`step3_build_sources/step3.py::_metric_expr()`.

## Проверка маппингов: crm_mappings_check

Модуль `crm_mappings_check/check.py` запускается автоматически после step12. Считает 3 сверки, но
Telegram-отчёт (с 2026-08-14) шлёт только 2 секции — **UNUSED** остаётся в логе (Семён не хочет шум
по неиспользуемым маппингам), в Telegram не идёт:
1. (лог, не в Telegram) **UNUSED** — маппинги в `reference_data.crm_status_mapping` без записей в leads
2. **UNMAPPED status** — статусы в `leads.status` без маппинга в `reference_data.crm_status_mapping`/`CODE_STATUS_CATEGORY`
3. **UNMAPPED reason** — значения в `leads.reason` без маппинга

## История изменений

| Дата | Изменение |
|------|----------|
| 2026-05-07 | Добавлен `'Купил'` в `visit` (status + reason, crm_name='default') |
| 2026-05-07 | Добавлены `'Оформленные'`, `'Продажа в кредит'`, `'Продажа за наличные'` в `visit` |
| 2026-05-07 | `qualified` (11 статусов) → переведены в `correct` (были вне всех метрик) |
| 2026-05-08 | **Рефакторинг воронки**: разделение status/reason — kol_vo_zayavok/korr/kval/priezd/prodazhi/nekorr из status, dohod_do_kredita/dobro из reason. `qualified` восстановлена как отдельная kval-категория. Удалены 4 мёртвых маппинга, перенесены 14 status→reason. |

## local_gsheet_priezdi_marcar — воронка Маркар (детальный разбор)

> Перенесено из `anton_sql.md` (канон по этому quirk). Антон ссылается сюда индексом.

**Проблема:** Маркар ведёт Google Sheet «Маркар Доезды», где фиксирует факт продажи.
В CRM (`marcar_crm_excel`) у тех же лидов статус остаётся `'Корзина'` — CRM не синхронизирует обратно.

**Решение:** v6 портирует `_patch_marcar_statuses()` выражением в `step1_load_raw/step1.py`.
Патч применяет gsheet-статусы `Продажа`, `Дошел в КО`, `Одобрение`, `Приехал`:

```sql
SELECT
    lead_record_id,
    argMin(status, prio) AS status
FROM reference_data.gsheet_priezdi_marcar
WHERE link LIKE '%crm.marcar.ru%'
  AND status IN ('Продажа', 'Дошел в КО', 'Одобрение', 'Приехал')
GROUP BY lead_record_id
```

Патч не перезаписывает статус «вниз»: приоритет `Продажа > Дошел в КО > Одобрение > Приехал`.
Поле `status` в gsheet может содержать мусор, поэтому код берёт только четыре перечисленных статуса.

**Следствие для воронки:**
- `prodazhi` Маркар включает продажи из gsheet-патча
- `priezd` Маркар включает визитные gsheet-статусы из патча и `sale` через auto-merge
- После step1 → патченые статусы попадают в `ad_analytics.raw_leads` / `raw_calls`

**Маппинг ID:** `link = 'https://crm.marcar.ru/leads/409449'` → `REGEXP_REPLACE(link, '^.+/', '') = '409449'` = `local_leads_all.source_record_id`

**Не патчатся:** ссылки `plex-crm.ru` или любые не `crm.marcar.ru`.

**Валидация Маркар-продаж:**
```sql
-- Сколько Маркар-лидов получили один из gsheet-статусов через патч
SELECT status, count() AS marcar_status_patched
FROM ad_analytics.raw_leads
WHERE source_type = 'marcar_crm_excel'
  AND status IN ('Продажа', 'Дошел в КО', 'Одобрение', 'Приехал')
GROUP BY status;

-- Сколько строк в gsheet с полезными статусами
SELECT status, count() AS gsheet_status_rows
FROM reference_data.gsheet_priezdi_marcar
WHERE link LIKE '%crm.marcar.ru%'
  AND status IN ('Продажа', 'Дошел в КО', 'Одобрение', 'Приехал')
GROUP BY status;
```

**Сверка приездов vs продаж Маркар в big_analytics_full:**
```sql
SELECT "Название crm",
    SUM(priezd) AS приезды_crm,
    SUM(prodazhi) AS продажи_gsheet,
    SUM(prodazhi) - SUM(priezd) AS delta_prodazhi_minus_priezdy
FROM big_analytics_full
WHERE "Название crm" = 'Маркар' AND тип_заявки = 'заявки'
GROUP BY "Название crm";
-- delta > 0 = норма: gsheet фиксирует продажи без предварительного визита в CRM
-- delta < 0 = НЕ норма: больше приездов чем продаж — ok только если клиент приехал но не купил
```

---

## Сверка с гугл-таблицами салонов: ось ПО ДАТЕ ВИЗИТА

В Google-таблицах салонов метрики **ПРИЕЗДЫ (визиты)** и **ПРОДАЖИ** считаются **ПО ДАТЕ ВИЗИТА**
(`arrival` / `visit_date`), а **НЕ по дате заявки**. При сверке `public.fact_big_analytics` с листом
салона брать атрибуцию **«По дате визита»** (`priezd_arrival_date` / `prodazhi_arrival_date`).

Сравнение по **дате заявки** даёт помесячный сдвиг **±12%** — лиды «уезжают» created→arrival
в соседний месяц. Подтверждено на **УрбанКар/Тольятти** (книга `1DTGthpGJsyVsuuHmCsE`), 2026-06-09.

**Структура листа салона:**
- построчный блок сайтов под агентством «Виктори» (контекст-домены);
- сумма по сайтам = строка агрегата **«контекст»** = мульти + моно + Б/У;
- пиксель/pb/vdl (`victory_pixel` / `victory_pb` / `victory_urbancar_vdl`) идут отдельно в **«лидв»**
  и в «контекст» **НЕ входят**.
