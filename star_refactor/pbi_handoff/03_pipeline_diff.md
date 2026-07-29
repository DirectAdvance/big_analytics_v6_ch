# План интеграции star в пайплайн (ДИФ, НЕ запускать до приёмки модели)

> ⚠️ **2026-06-11: схема `star` УПРАЗДНЕНА — звезда консолидирована в `public` 2026-06-10**
> (`build_star.py` `build_schema()` стал no-op). Везде ниже `star.fact_big_analytics` /
> `star.arp_fact` физически = `public.*`. Этот ДИФ — историческая мотивация этапа.
> См. [`00_README_NEED_FROM_USER.md`](00_README_NEED_FROM_USER.md), [`../../PBI_TABLES.md`](../../PBI_TABLES.md).
>
> Делается ТОЛЬКО после того, как пользователь принял новую PBI-модель на звезде
> (Этапы 1–8 чек-листа пройдены, отчёт сверён с эталоном). До этого — прод-пайплайн
> и прод-таблицы не трогаем. Ниже — что и где менять, с обоснованием.

---

## Текущее состояние потока данных
```
step13_arrival/build_unified.py
    big_analytics_full (заявка) + big_analytics_full_arrival (визита) + пиксель-атрибуция
    → MIRROR+UNION → public.big_analytics_unified (5.37 ГБ, колонка `атрибуция`)
                                  │
                                  └──> PBI читает big_analytics_unified (под именем big_analytics_full)

step_cron_night/report_placement/step2_build_analytics.py
    → public.analytics_report_placement (12 ГБ) → PBI читает напрямую
```

## Целевое состояние
```
build_unified.py → big_analytics_unified  (остаётся как ИСТОЧНИК для star)
star_refactor/build_star.py → star.fact_big_analytics (698 МБ) + 4 dim
                                  └──> PBI читает star.* (лёгкие)

step2_build_analytics.py → analytics_report_placement → star.arp_fact (1370 МБ)
                                  └──> PBI читает star.arp_fact
```

---

## ДИФ 1 — врезать build_star в пайплайн (после build_unified)

**Файл:** `pipeline.py` (и `fast_pipeline.py` если нужно)
**Где:** сразу после шага, который вызывает `step13_arrival/build_unified.py`
(построение `big_analytics_unified`).

**Добавить вызов** (псевдо-диф):
```python
# после build_unified():
from star_refactor.build_star import main as build_star_main   # или run-функция
log_step(conn, run_id, 'build_star', 'start')
build_star_main()        # пересобирает star.fact_big_analytics + 4 dim + arp_fact
log_step(conn, run_id, 'build_star', 'ok', duration_sec=...)
```

> `build_star.py` уже идемпотентен (DROP+CREATE в схеме `star`). Время сборки ~ минуты
> (факт 698 МБ + ARP 1370 МБ). VACUUM с `max_parallel_maintenance_workers=0` (DiskFull guard).
> Рефакторить build_star.py в функцию-параметр `conn` желательно, чтобы шёл в общей транзакции
> логирования (сейчас открывает своё соединение).

**Альтернатива (легче):** не материализовать unified отдельно, а строить star напрямую
из big_analytics_full ∪ big_analytics_full_arrival внутри build_star. Тогда можно
**прекратить материализацию `big_analytics_unified`** (−5.37 ГБ). НО: сначала убедиться,
что unified нигде больше не читается (кроме PBI). Сделать отдельной задачей после приёмки.

---

## ДИФ 2 — обновить _ALL_TABLES в refresh_powerbi.py

**Файл:** `refresh_powerbi.py`, строки ~193–204.

**Было:**
```python
_ALL_TABLES = [
    'big_analytics_full',
    'direct_history',
    'check_utm_fuck_direct',
    'analytics_report_placement',
    'yandex_direct_korrektirovki',
    'yandex_direct_404_errors',
    'yandex_direct_return_commission_report',
    'big_analytics_full_arrival',
    'pixel_score',
    'yandex_direct_cookie_analytics_website_pages',
]
```

**Станет** (имена таблиц = ИМЕНА В МОДЕЛИ PBI, согласовать с финальной моделью!):
```python
_ALL_TABLES = [
    'big_analytics_full',   # ← в модели остаётся это имя, но partition читает star.fact_big_analytics
    'Dim_Date',             # новые dim
    'Dim_Campaign',
    'Dim_AdGroup',
    'Dim_Site',
    'arp_fact',             # ← заменяет analytics_report_placement (если переименовал в модели)
    'direct_history',
    'check_utm_fuck_direct',
    'yandex_direct_korrektirovki',
    'yandex_direct_404_errors',
    'yandex_direct_return_commission_report',
    'pixel_score',
    'yandex_direct_cookie_analytics_website_pages',
    # big_analytics_full_arrival — БОЛЬШЕ НЕ НУЖЕН отдельно в PBI: визита-партиция
    #   уже внутри star.fact_big_analytics через колонку `атрибуция`. Убрать.
]
```

> ⚠️ ИМЕНА в `objects:[{table:t}]` для refresh API — это имена ТАБЛИЦ В СЕМАНТИЧЕСКОЙ
> МОДЕЛИ, не в БД (см. примечание про `direct_history` в PBI_TABLES.md). После того как
> пользователь финализирует имена dim/факта в модели — переписать список ровно под них.
> Если оставить старое имя `analytics_report_placement` для ARP в модели — не переименовывать.

---

## ДИФ 3 — (опц., экономия места) прекратить материализацию full/unified для PBI

После приёмки и стабильной работы star:
- `big_analytics_full` (4.96 ГБ) и `big_analytics_unified` (5.37 ГБ) → если их читает только
  PBI, можно НЕ финализировать в LOGGED / держать как UNLOGGED промежуток.
- ⚠️ `big_analytics_full` читают ДРУГИЕ потребители (см. `work/leads_api_perform/` — сервис CPL
  читает `big_analytics_full`). НЕ дропать, пока не проверены все потребители. Это отдельная
  задача с инвентаризацией (grep по коду + БД-зависимости).
- Экономия только на star-факте vs unified для PBI: 5373 → 698 МБ (×7.7) + ARP 12 ГБ → 1.37 ГБ.

---

## ДИФ 3b — ARP: заменить тяжёлый источник лёгкой звездой (минус 12 ГБ) ★ согласовано, делаем позже

> Статус: ПЛАН, утверждён пользователем 2026-06-06. Выполнить ПОСЛЕ приёмки PBI-модели.

**Суть:** в отличие от `big_analytics_full`, у `public.analytics_report_placement` (12 ГБ)
**единственный потребитель — Power BI**. Проверено grep'ом по коду:
- читает только `refresh_powerbi.py:197` (грузит в PBI);
- строит её `step_cron_night/report_placement/` (cron суббота);
- CPL-сервис / дашборд / adsensor её НЕ читают.

Поэтому тяжёлую `analytics_report_placement` можно **убрать совсем**, а PBI кормить лёгкой
`star.arp_fact` (1.37 ГБ). Экономия ~12 ГБ на диске, 0 потребителей ломается.

**Что сделать (позже):**
1. Переписать ночной `step_cron_night/report_placement/` так, чтобы он строил сразу лёгкую
   `star.arp_fact` (ключи + меры + placement + ad_network_type + тип_заявки) из upstream
   `yandex_direct_report_placement` LEFT JOIN `raw_leads`, БЕЗ справочного текста.
   Справочные атрибуты (салон/город/кампания/группа) берутся из conformed `Dim_*`.
2. Прекратить материализацию `public.analytics_report_placement` (или оставить как тонкий
   staging без справочного текста, если нужен для пересборки).
3. В `refresh_powerbi.py` `_ALL_TABLES` для ARP оставить `star.arp_fact` (уже учтено в ДИФ 2).
4. Верификация: cost/clicks/конверсии arp_fact ДО/ПОСЛЕ совпали (как verify_star).

**Почему позже:** не менять два звена сразу — сначала пользователь принимает модель в PBI
(arp_fact как источник), потом перекладываем пайплайн. Контраст с `big_analytics_full`
(ДИФ 3) — там источник убрать НЕЛЬЗЯ (читают CPL/дашборд/алёрты).

> ⚠️ БЛОКЕР (найдено 2026-06-07): лёгкая `star.arp_fact` (21 кол) НЕ содержит 6 conversion-колонок,
> которые нужны 2 сводным таблицам + 7 мерам на странице PBI «Площадки РСЯ»:
> `Все формы`, `CRM: Заказ создан/оплачен/отменён/Спам` (4 шт), `placement_key`.
> Они есть ТОЛЬКО в тяжёлой `public.analytics_report_placement` (placement-уровень РСЯ,
> в основной факт не заходит; данные реальны — 124 951 форм, 24 882 CRM-заказа, 26 694 placement_key).
> Чтобы полностью убрать тяжёлую таблицу из модели — сперва добавить эти 6 колонок в `star.arp_fact`
> (ALTER + backfill из public + патч build_star.py, +~50-100 МБ), затем перенацелить
> `analytics_report_placement`→`star.arp_fact` и удалить дубль. Пользователь решил (2026-06-07):
> ПОКА не трогаем — у тяжёлой таблицы стоит `excludeFromModelRefresh`, ежедневный Refresh её не тянет.
> На странице «Площадки РСЯ» её используют 8 визуалов (6 slicer → переносимы в Dim_*, 2 pivotTable
> держат conversion-меры). `arp_fact` сейчас визуалами НЕ используется (живёт ради 4 dim-связей).

---

## ДИФ 3c — big_analytics_unified: убрать PBI-only дубль (минус 5.4 ГБ) ★ согласовано, делаем позже

> Статус: ПЛАН, утверждён пользователем 2026-06-06. Выполнить ПОСЛЕ приёмки PBI-модели.
> Тот же паттерн, что ДИФ 3b (ARP): тяжёлая таблица, которую читает ТОЛЬКО Power BI.

**Суть:** `public.big_analytics_unified` (5.4 ГБ, 3.68М строк) строится шагом
`step13_arrival/build_unified.py` как `big_analytics_full ∪ big_analytics_full_arrival`
(+ MIRROR/UNION атрибуции «По дате заявки»/«По дате визита»).

Проверено grep'ом: **единственный потребитель — Power BI**. Ни CPL-сервис, ни дашборд,
ни adsensor её НЕ читают (они читают `big_analytics_full`). А `star.fact_big_analytics`
уже воспроизводит MIRROR+UNION через колонку `атрибуция` → для PBI unified больше не нужен.

**Что сделать (позже, после cutover PBI на star):**
1. Перестать материализовать `public.big_analytics_unified` в `step13_arrival/build_unified.py`
   (этот шаг вызывается из `pipeline.py:754` и `fast_pipeline.py:590`).
2. Убедиться, что вся атрибуция (заявка/визита) едет через `star.fact_big_analytics["атрибуция"]`.
3. `DROP TABLE public.big_analytics_unified` → **−5.4 ГБ**.

**НЕЛЬЗЯ трогать `big_analytics_full`** (4.96 ГБ) — его читают CPL/дашборд/алёрты (см. ДИФ 3).
Убираем ТОЛЬКО `unified` (надстройку над full ради PBI-атрибуции).

**Суммарная экономия ДИФ 3b + 3c:** analytics_report_placement (−13 ГБ) + big_analytics_unified
(−5.4 ГБ) = **≈ −18 ГБ**, ноль сломанных потребителей.

---

## ДИФ 4 — PBI_TABLES.md обновить

После cutover отразить в `PBI_TABLES.md`: новый список _ALL_TABLES, star-источники,
что big_analytics_full в модели теперь = star.fact_big_analytics.

---

## Порядок выкатки (рекомендация)
1. Пользователь принимает модель (star, dim, связи) на КОПИИ → публикует в тестовый/тот же воркспейс.
2. Врезать ДИФ 1 (build_star в пайплайн) → прогнать pipeline на Victory через nohup → verify_star.
3. Обновить ДИФ 2 (_ALL_TABLES) под финальные имена модели.
4. Прогнать refresh_powerbi → сверить дашборд с эталоном (34157/2384/854.7М).
5. Только потом ДИФ 3 (экономия места) — отдельной задачей с инвентаризацией потребителей.
