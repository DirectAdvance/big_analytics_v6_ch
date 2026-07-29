# FUNNEL.md — воронка заявок (`local_crm_statuses`)

> Источник правды по воронке статусов. Вынесено из `CLAUDE.md` (lazy-load).
> Высокоуровневые инварианты воронки — также в `PROJECT_CHARTER.md` §5.

---

Источник: `public.local_crm_statuses` (ad_analytics_bi). Колонки: `crm_status`, `lead_status`, `crm_name`, `salon`, `kind`.

`kind='status'` → колонка `leads.status`; `kind='reason'` → колонка `leads.reason`.
`crm_name=''` → все CRM; `crm_name='default'` → маппится в `''` (все CRM); `crm_name='MEGA'/'PLEX'` → конкретный `source_type`.

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

Reason-сторона полностью отдельная от status — продажи на стороне reason тоже считаются (`sale ⊆ approved ⊆ credit` через auto-merge внутри reason).

## Маппинг метрик

| Метрика | lead_status | kind | Источник |
|---------|------------|------|----------|
| **kol_vo_zayavok** | — | — | `status IS NOT NULL` (хардкод) |
| **korr** | `correct` | `status` | local_crm_statuses |
| **kval** | `qualified` | `status` | local_crm_statuses |
| **priezd** | `visit` | `status` | local_crm_statuses |
| **prodazhi** | `sale` | `status` | local_crm_statuses |
| **nekorr** | `incorrect` | `status` | local_crm_statuses |
| **dohod_do_kredita** | `credit` | `reason` | local_crm_statuses |
| **dobro** | `approved` | `reason` | local_crm_statuses |

## Хардкод (не из таблицы)

| Метрика | Значения | Колонка |
|---------|---------|---------|
| **ne_otvechaet** | `'Не отвечает'`, `'Новая: Не отвечает'` | status |
| **filtr** | `'Фильтр'` | status |
| **nedozvon** | `'Недозвон'` | status |
| **priedet** | `'Приедет'` | status |

`kval` теперь считается **прямо из категории `qualified`** в `local_crm_statuses` (раньше — формула `korr − ne_otvechaet − filtr − nedozvon`).

> ✅ Это **корректная** формула (не регрессия). На golden-срезе Кудерко даёт **kval ≈ 677**
> (не старые ~1752), стоимость квала ≈ 20 913 ₽ — согласовано с [`GOLDEN_BASELINE.md`](GOLDEN_BASELINE.md)
> (re-baseline 2026-07-15) и блоком 14 `verify_big_analytics.py`.

## Auto-merge инварианты

В `_group_by_category()` (config/status_sql.py) автоматически копируются маппинги по цепочке:

**Status-сторона:**
```
sale  → visit
sale  → qualified
visit → qualified
qualified → correct
visit → correct
sale  → correct
```

**Reason-сторона:**
```
sale     → approved
approved → credit
sale     → credit (через approved)
```

Гарантия: `korr ≥ kval ≥ priezd ≥ prodazhi` (status), `credit ≥ approved` (reason).

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
-- Если значение в leads.status — kind='status':
INSERT INTO public.local_crm_statuses (crm_status, lead_status, crm_name, salon, kind)
VALUES ('Новый статус', 'visit', '', '', 'status');

-- Если значение в leads.reason — kind='reason':
INSERT INTO public.local_crm_statuses (crm_status, lead_status, crm_name, salon, kind)
VALUES ('Новая причина', 'visit', '', '', 'reason');

-- crm_name='MEGA'/'PLEX' — только для конкретной CRM (kind='status')
-- salon='АЦ Платина' — только для конкретного салона
```

После INSERT — пайплайн подхватит автоматически при следующем запуске (`load_status_sql()`).

## Проверка маппингов: crm_mappings_check

Модуль `crm_mappings_check/check.py` запускается автоматически после step12. Шлёт Telegram-отчёт с 3 секциями:
1. **UNUSED** — маппинги в `local_crm_statuses` без записей в leads
2. **UNMAPPED status** — статусы в `leads.status` без маппинга в `local_crm_statuses`
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

**Решение (step0):** `_patch_marcar_statuses()` патчит `local_leads_all.status = 'Продажа'` из gsheet:

```sql
UPDATE public.local_leads_all l
SET status = pm.status
FROM public.local_gsheet_priezdi_marcar pm
WHERE pm.link LIKE '%crm.marcar.ru%'
  AND pm.link ~ '^https?://.+/[0-9]+$'
  AND pm.status = 'Продажа'
  AND l.source_record_id = REGEXP_REPLACE(pm.link, '^.+/', '')  -- число после последнего /
  AND l.source_type = 'marcar_crm_excel'
  AND l.status IS DISTINCT FROM pm.status
```

**Только `'Продажа'`** — другие статусы (Приехал, Одобрение и т.д.) НЕ патчатся.
Поле `status` в gsheet содержит мусор (даты DD.MM.YYYY в некоторых строках).

**Следствие для воронки:**
- `prodazhi` Маркар включает продажи из gsheet-патча (без этого были бы 0)
- `priezd` Маркар берётся ТОЛЬКО из CRM-статусов через `local_crm_statuses` — gsheet-патч **не влияет на priezd**
- После step0, до step1 → корректные статусы попадают в `raw_leads`

**Маппинг ID:** `link = 'https://crm.marcar.ru/leads/409449'` → `REGEXP_REPLACE(link, '^.+/', '') = '409449'` = `local_leads_all.source_record_id`

**Не патчатся:** ссылки `plex-crm.ru` или любые не `crm.marcar.ru`.

**Валидация Маркар-продаж:**
```sql
-- Сколько Маркар-лидов получили статус Продажа через gsheet-патч
SELECT COUNT(*) AS marcar_prodazhi_patched
FROM local_leads_all
WHERE status = 'Продажа' AND source_type = 'marcar_crm_excel';

-- Сколько строк в gsheet со статусом Продажа (ожидаем ≈ равно)
SELECT COUNT(*) AS gsheet_prodazhi
FROM local_gsheet_priezdi_marcar
WHERE link LIKE '%crm.marcar.ru%'
  AND link ~ '^https?://.+/[0-9]+$'
  AND status = 'Продажа';
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
(`arrival` / `visited_at`), а **НЕ по дате заявки**. При сверке `public.fact_big_analytics` с листом
салона брать атрибуцию **«По дате визита»** (`priezd_arrival_date` / `prodazhi_arrival_date`).

Сравнение по **дате заявки** даёт помесячный сдвиг **±12%** — лиды «уезжают» created→arrival
в соседний месяц. Подтверждено на **УрбанКар/Тольятти** (книга `1DTGthpGJsyVsuuHmCsE`), 2026-06-09.

**Структура листа салона:**
- построчный блок сайтов под агентством «Виктори» (контекст-домены);
- сумма по сайтам = строка агрегата **«контекст»** = мульти + моно + Б/У;
- пиксель/pb/vdl (`victory_pixel` / `victory_pb` / `victory_urbancar_vdl`) идут отдельно в **«лидв»**
  и в «контекст» **НЕ входят**.
