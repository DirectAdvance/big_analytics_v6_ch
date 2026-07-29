# ATTRIBUTION.md — единый авторитет по атрибуции

> Атрибуция — **исторически баг №1** проекта (циклы «опять не то получилось», 5–6 июня 2026).
> Этот файл сводит всё про атрибуцию в одно место. Источники: [`GOLDEN_BASELINE.md`](GOLDEN_BASELINE.md),
> [`CANON.md`](CANON.md), [`KNOWN_ISSUES.md`](KNOWN_ISSUES.md) #7, [`STAR_REFACTOR_BRIEF.md`](STAR_REFACTOR_BRIEF.md),
> [`BLOCKS.md`](BLOCKS.md), [`PROJECT_CHARTER.md`](PROJECT_CHARTER.md) §8.2. При конфликте — проверять по коду/БД.

---

## 0. Железные правила (нарушение = баг данных)

1. **НИКОГДА не приводить дробные меры к `int` по строкам.** Меры воронки
   (`kol_vo_zayavok, korr, kval, priezd, prodazhi`) — `numeric`. Усечение долей по строкам
   ломает суммы (например, приезды пикселя `6097 → 1614`). Округление — **только** у итоговой
   `SUM(...)` через `ROUND(...)`.
2. **`источник IS NOT NULL`** для всех строк `big_analytics_full` (см. [`CANON.md`](CANON.md)).
3. **Нет двойного учёта лидов:** `direct ∩ crop_targeting = 0`.
4. **Воронка вложена** (см. §4).
5. **Расход = только Я.Директ** при сверке с эталоном; пиксель в эталон НЕ входит.

---

## 1. Модель атрибуции (дробная, пиксельная)

Большинство строк — целочисленная атрибуция канала (директ/SEO/посевы/звонки): заявка
принадлежит одному каналу по UTM. Дробной становится **пиксель-атрибуция** (step11):

- `big_analytics_pixel` (step5) собирает лиды `utm_source LIKE 'victory_%'`.
- step11 (`pixel_score`) **не** льёт пиксель напрямую, а распределяет пиксель-воронку салона
  по цепочке **салон → домен → кампания** по взвешенному CR
  (`cr_composite = (1·kol_vo + 3·korr + 10·kval + 30·priezd + 100·prodazhi)/Clicks`),
  benchmarks по домену из `big_analytics_direct` за тот же месяц.
- Результат: строки в `big_analytics_full` с `_source_table='пиксель_атрибуц'`,
  `направление='пиксель_атрибуц'`, где меры — **дробные доли**. Отсюда требование numeric.

См. [`PROJECT_CHARTER.md`](PROJECT_CHARTER.md) §8.6.

---

## 2. «По дате заявки» vs «по дате визита»

| Срез | Витрина | Колонка-дата | Метрики |
|------|---------|--------------|---------|
| По дате заявки | `big_analytics_full` | `Date` = дата заявки | вся воронка + cost/clicks |
| По дате визита | `big_analytics_full_arrival` (step13) | `Date` = `arrival_date` | только `priezd + prodazhi`, без cost/clicks |
| Обе вместе | `big_analytics_unified` | `Date` + колонка `атрибуция` | full ∪ arrival |

- **`big_analytics_unified`** = MIRROR+UNION: `атрибуция='По дате заявки'` ← `full`,
  `атрибуция='По дате визита'` ← `full_arrival`. **Это таблица, которую читает Power BI**
  (дрифт имени: в модели называется `big_analytics_full`, но M-запрос читает `unified`).
- Эталон Кудерко снят при **`атрибуция='По дате заявки'`** (см. [`GOLDEN_BASELINE.md`](GOLDEN_BASELINE.md)).
- `step13_arrival` фильтрует `direction='Авто'` → **посевы выпадают** из BFA
  (см. [`KNOWN_ISSUES.md`](KNOWN_ISSUES.md) #1/#2). `arrival_date` для `marcar_crm_excel`
  берётся как `created_date` (помечено «ПРОБЛЕМА», требует доработки).

---

## 3. Посевы vs директ (нет двойного учёта)

- Лиды директа (`leads_direct`) и посевов (`crop_targeting`) **не пересекаются**: UTM-фильтры
  в step3 исключают посевные `utm_source/medium` (`telegram`/`vk`/`max`/`posev`/`paid_social`)
  из директа. Эмпирически: `direct ∩ crop = 0`.
- Граница посевов **1 мая 2026**: < мая — Google Sheets, ≥ мая — Telega.in API. Периоды
  не пересекаются (0 нахлёста). См. [`PROJECT_CHARTER.md`](PROJECT_CHARTER.md) §8.5.
- `tp8`-кампании переносятся из direct в crop с `_source_table='tp8'` (НЕ `'crop_targeting'`,
  иначе DELETE их сотрёт).
- VK/MAX заявки-сироты → `_source_table='social_посевы'`, `total_cost=0` (дыра атрибуции расхода).

---

## 4. Инварианты воронки

Источник правды — `public.local_crm_statuses` (auto-merge в `config/status_sql.py`):

```
Status-сторона (leads.status):
  обращения ⊇ kol_vo_zayavok ⊇ korr ⊇ kval ⊇ priezd ⊇ prodazhi
Reason-сторона (leads.reason):
  dohod_do_kredita ⊇ dobro
```

Гарантии: `korr ≥ kval ≥ priezd ≥ prodazhi` и `dohod_do_kredita ≥ dobro`.
Проверка по проджектам — `data_check/checks/funnel.py`. Детали маппинга — [`FUNNEL.md`](FUNNEL.md).

---

## 5. Power BI: TREATAS-ловушка (KNOWN_ISSUES #7)

В режиме «по дате визита» меры (Приездов/Продаж/Одобрено/Доход) используют
`CALCULATE(SUM(BFA[...]), TREATAS(...[салон]), TREATAS(...[Date]))`. Прямой связи
BAF↔BFA нет — только TREATAS по `[салон]+[Date]`. **При разбивке глубже салона** мера
возвращает полный приезд салона за период в каждую строку (раздутие 14–31×), потому что
BFA не содержит `источник/направление/CampaignId/AdGroupName/…`.

**Как чинить:** либо добавить недостающие измерения в BFA + TREATAS по ним, либо физически
связать BAF↔BFA, либо пометить визуалы глубже салона как несовместимые с «по визиту».
В star-схеме это решается срезом по `fact["атрибуция"]` — **TREATAS не нужен**
(см. [`STAR_REFACTOR_BRIEF.md`](STAR_REFACTOR_BRIEF.md) §«Связи»).

---

## 6. Как проверить, что атрибуция не сломана

1. Прогнать золотой SQL ([`GOLDEN_BASELINE.md`](GOLDEN_BASELINE.md) §«Эталонный SQL» /
   [`QUERIES.md`](QUERIES.md)): расход и продажи Кудерко **обязаны** совпасть до копейки.
2. Проверить инварианты воронки (`data_check/checks/funnel.py`).
3. Проверить `источник IS NULL` = 0 и `"Date" < '2026-01-01'` = 0 (CANON).
4. После правок звезды — сверить агрегаты `public.fact_big_analytics` vs `big_analytics_unified`
   (`star_refactor/verify_star.py`; схема `star` консолидирована в `public` 2026-06-10).

> Перед словами «атрибуция починена» — показать diff ДО→ПОСЛЕ на целевой мере и подтвердить,
> что соседние меры/суммы не затронуты. См. корневое правило «Проверяй перед готово».
