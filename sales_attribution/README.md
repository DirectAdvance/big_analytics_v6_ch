# sales_attribution — витрина атрибуции продаж для Power BI

> Строит таблицу `public.analytics_sales_attribution` — пер-продажная атрибуция расходов
> Я.Директа на продажи из выгрузки `sale_xlsx`, с расчётом маржи и прибыли.
> Узкий срез: **только CRM «Фаиг»** (`big_analytics_full."Название crm" = 'Фаиг'`).
> Отдельный скрипт, **не часть `pipeline.py`** — запускается вручную по необходимости.

---

## Запуск

```bash
cd big_analytics_v5
~/venv/bin/python3 sales_attribution/build.py     # пересобрать таблицу (DROP + CREATE + INSERT)
~/venv/bin/python3 sales_attribution/verify.py    # сверить агрегаты с источником
```

Подключение к БД — `config.settings.DB_DST` (`ad_analytics_bi`).

---

## Назначение

Связать каждую **продажу** (строка `sale_xlsx`) с **расходом** Директа той кампании
в дату создания, чтобы Power BI показывал: расход на продажу, маржу, прибыль по салонам.

Гранулярность результата (`row_type`):
- `sale` — 1 строка = 1 продажа (из `sale_xlsx`, после UTM-фильтра);
- `cost_unattributed` — пара `(CampaignId, Date)` с расходом, но **без продажи** в эту дату;
- `cost_no_campaign_id` — расходы без `CampaignId` (звонки / SEO / посевы / …).

Три части объединяются `UNION ALL` — сумма `cost_alloc` по всей таблице равна
`SUM(big_analytics_full.total_cost)` для «Фаиг» (инвариант, проверяется `verify.py`).

---

## Логика (`build.py`)

**Фильтры источников:**
- `sale_xlsx`: `utm_medium IS NOT NULL/''` AND `utm_source NOT IN ('victory-corp','leadgen')`;
- `big_analytics_full`: `"Название crm" = 'Фаиг'`.

**Разбор UTM продажи:** `camp_id` — из `utm_campaign` (`^[0-9]+\|`), `adgroup_id` — из
`utm_content` (`g:[0-9]+`), флаг `is_organic` = `utm_source='seo' AND utm_medium='organic'`.

**Аллокация расхода:** `cost_alloc = cost_camp_date / n_sales_camp_date` — расход кампании
за дату делится поровну на число продаж этой кампании в эту дату (точная пара `camp_id + date`).

**Маржа:** `200 000 ₽` за каждую продажу (`row_type='sale'`); для cost-строк маржа = 0.
Прибыль = маржа − `cost_alloc` (считается в `verify.py` / в PBI).

**Схема таблицы:** см. `DDL` в `build.py` — реквизиты продажи (ФИО-маска, авто, регион,
UTM), ключи кампании (`camp_id/adgroup_id/camp_date`), измерения (`salon/specialist/city/
region/template/tp/ag_part1`), меры (`cost_alloc`, `cost_camp_date_total`, воронка
`zayavok/korr/priezd/prodazhi_camp_date`, `n_sales_camp_date`, `marzha`). 6 индексов.

---

## Верификация (`verify.py`)

Печатает сверку с источником `big_analytics_full` («Фаиг»):
1. **Расход** — `SUM(cost_alloc)` должен совпасть с `SUM(total_cost)` (допуск <0.01%);
2. **Продажи** — `count(row_type='sale')` == `count(sale_xlsx после фильтра)`;
3. воронка BAF «Фаиг» (заявки/корр/приезды/продажи);
4. распределение по `row_type`;
5. продажи без `cost_alloc` (органика + не разобранный UTM);
6. ТОП-10 салонов по `cost_alloc` (расход / маржа / прибыль).

---

## Связи

- **Источники:** `sale_xlsx` (выгрузка продаж), `big_analytics_full` (расход «Фаиг»).
- **Выход:** `public.analytics_sales_attribution` — отдельная PBI-таблица (не в `_ALL_TABLES`
  основного refresh; обновляется этим скриптом вручную).
- **Инвариант:** Σ`cost_alloc` == Σ`total_cost` («Фаиг»). Расхождение → расследовать.
- **Маржа** жёстко зашита = 200 000 ₽/продажа (`MARZHA_PER_SALE` в `build.py`).
