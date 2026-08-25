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

REASON_METRIC_KEY_2026-08-25: reason-сторона матчится ТЕМ ЖЕ `_category_match_expr`, что и
status-сторона — полным ключом (crm, status, salon, reason), а не только (crm, reason). Раньше
голый `(crm, reason)` смешивал разные категории одной и той же пары reason, если категория
зависела от status/salon (обнаружено: 203 лишних лида в dohod_do_kredita, 106 в dobro за
1-24.08.2026). Продажи на стороне reason тоже считаются (`sale ⊆ approved ⊆ credit`). На
status-стороне продажи также автоматически входят в `approved`, `credit`, `visit`, `qualified`,
`correct`. Исключение — `CASH_SALE_STATUSES` (`plex`/`genzes`, «Продажа за наличные»): продажа за
наличные не проходит через кредитный отдел, поэтому вычитается из обеих reason-метрик, но
остаётся в `prodazhi`.

## Маппинг метрик

| Метрика | lead_status | kind | Источник |
|---------|------------|------|----------|
| **kol_vo_zayavok** | — | — | `status IS NOT NULL` (хардкод) |
| **korr** | `correct` | `status` | reference_data.crm_status_mapping / CODE_STATUS_CATEGORY |
| **kval** | `qualified` | `status` | reference_data.crm_status_mapping / CODE_STATUS_CATEGORY |
| **priezd** | `visit` | `status` | reference_data.crm_status_mapping / CODE_STATUS_CATEGORY |
| **prodazhi** | `sale` | `status` | reference_data.crm_status_mapping / CODE_STATUS_CATEGORY |
| **nekorr** | `incorrect` | `status` | reference_data.crm_status_mapping / CODE_STATUS_CATEGORY |
| **dohod_do_kredita** | `credit`+`approved`+`sale`, минус `CASH_SALE_STATUSES` | `status`+`salon`+`reason` | reference_data.crm_status_mapping / CODE_STATUS_CATEGORY |
| **dobro** | `approved`+`sale`, минус `CASH_SALE_STATUSES` | `status`+`salon`+`reason` | reference_data.crm_status_mapping / CODE_STATUS_CATEGORY |

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

**Reason-сторона** (полный ключ crm/status/salon/reason, как status-сторона):
```
dohod_do_kredita = (credit + approved + sale) − CASH_SALE_STATUSES
dobro            = (approved + sale) − CASH_SALE_STATUSES
```

Гарантия: `korr ≥ kval ≥ priezd ≥ prodazhi` (status), `dohod_do_kredita ≥ dobro` (reason).

## Переопределения по CRM/салону

| Категория | crm_name | salon | Статус | kind |
|-----------|---------|-------|--------|------|
| `credit` | MEGA | Платина | Отказ по банкам | reason |
| `credit`/`visit` | PLEX | УрбанКар / — | Консультация | status/reason |
| `credit` | marcar (CODE_STATUS_CATEGORY) | — | Дошел в КО | status/reason |
| `approved` | marcar (CODE_STATUS_CATEGORY) | — | Одобрение | status/reason |

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

⚠️ **v6_ch: модуль не подключён к пайплайну.** `crm_mappings_check/check.py` — PostgreSQL/v5-код
(`run(conn, ...)` на `local_leads_all`/`local_crm_statuses`, свой докстринг говорит "вызывается из
pipeline.py / fast_pipeline.py"), но в `pipeline.py`/`cron_run.py` v6_ch на него нет ни одного
вызова (grep 2026-08-22: только сам файл и `tests/test_telegram_notifications.py`). Живые проверки
в v6_ch — `step3_build_sources/step3.py::check_crm_mapping_coverage()` (source_type без ключа в
`reference_data.crm_status_mapping` — вызывается ВНУТРИ `step3.run()`, не после step12) и
`check_code_status_categories()` (fail-fast на статус без категории в `CODE_STATUS_CATEGORY`).

Ниже — описание PostgreSQL-модуля (3 сверки, Telegram-отчёт с 2026-08-14 шлёт только 2 секции —
**UNUSED** остаётся в логе), не действует для активного ClickHouse-контура:
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
| 2026-08-25 | **REASON_METRIC_KEY**: dohod_do_kredita/dobro переведены с матча по голому (crm, reason) на тот же `_category_match_expr`, что и status-сторона (полный ключ crm/status/salon/reason) — устраняет 203/106 лишних лидов (Aug 1-24) от чужих status/salon с тем же reason. Добавлена категория `sale` в обе reason-метрики (`sale ⊆ approved ⊆ credit`). Добавлен `CASH_SALE_STATUSES` (plex/genzes «Продажа за наличные») — вычитается из reason-метрик, остаётся в prodazhi. Marcar «Дошел в КО»/«Одобрение» переведены в `CODE_STATUS_CATEGORY` из `visit` в `credit`/`approved` (раньше давали 0 KO/dobro для всех лидов Маркара); status-сторона (korr/kval/priezd) не изменилась — обе категории уже входили в объединения `visit`/`qualified`/`correct`. |

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

**Маппинг ID:** `link = 'https://crm.marcar.ru/leads/409449'` → `replaceRegexpOne(link, '^.+/', '') = '409449'` = `leads_all.source_record_id` (`raw_data.leads_all` в v6_ch, не `local_leads_all` — та таблица из v5/PostgreSQL)

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
