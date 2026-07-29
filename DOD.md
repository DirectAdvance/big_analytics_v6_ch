# DOD — Definition of Done, big_analytics_v5

> Задача РЕАЛЬНО готова только когда пройден весь чеклист своего типа И своего источника.
> «Должно работать» — не DoD. DoD = измеренный факт.

---

## Общие инварианты (обязательно для ЛЮБОГО типа задачи)

Проверяй ДО слов «готово / исправлено / принято»:

| Инвариант | Проверка |
|-----------|----------|
| Дробная пиксельная атрибуция НИКОГДА не int по строкам | grep по правке: нет `::int`, `int(`, `CAST(... AS INTEGER)` на столбцах с весом |
| `источник IS NOT NULL` для всех строк `big_analytics_full` | `SELECT COUNT(*) FROM big_analytics_full WHERE источник IS NULL` = 0 |
| `"Date" >= '2026-01-01'` — строк раньше быть не должно | `SELECT COUNT(*) FROM big_analytics_full WHERE "Date" < '2026-01-01'` = 0 |
| Воронка вложена: `korr ≥ kval ≥ priezd ≥ prodazhi`, `credit ≥ approved` | verify блок 7 PASS (I3/I_salon); или SQL: 0 нарушений |
| Нет двойного учёта: `direct ∩ crop_targeting = 0` | verify блок 3 PASS (дубли key3 = 0) |
| Таймзоны: посевы=Москва UTC+3, код агента на Маке=Екб UTC+5 | при работе с датами посевов: `replace(tzinfo=ZoneInfo("Europe/Moscow"))` |
| Расход Кудерко: `25 422 774.00 ± 100 ₽` | verify блок 1 PASS; эталон — `GOLDEN_BASELINE.md` |
| Продажи Кудерко: floor ≥ 54 | verify блок 1 PASS |

**Инструмент «всё за один запуск»:**
```bash
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 data_check/verify_big_analytics.py"
# exit 0 = PASS, 1 = FAIL, 2 = crash
```
Скил: `/ba5-golden-check` — справка по блокам и интерпретации.

---

## Часть 1 — DoD по ТИПАМ задач

### 1.1 Атрибуция (дробная / пиксель / step11 / pixel_score)

**Целевая метрика:** `SUM(total_cost)` по пиксельным строкам, дробность (`kol_vo_zayavok` не целые).

**Чеклист:**

- [ ] **Механика объяснена СЛОВАМИ до правки** — как новый код изменяет вес/долю строки. Не можешь объяснить — СТОП, исследуй.
- [ ] **Diff кода показан**: какой файл, какие строки, что было → что стало.
- [ ] **Нет `::int` по строкам**: `grep -n "::int\|::integer\|CAST.*INTEGER\|int(" step11_pixel_score/*.py` — пусто.
- [ ] **verify блок 6 PASS** — дробность пикселя существует (> 0 строк с нецелым весом).
- [ ] **verify блок 10 PASS** — `big_analytics_pixel == pixel_score` (нет рассинхрона источников).
- [ ] **verify блок 1 PASS** — расход Кудерко в допуске ±100 ₽ (пиксель влияет на расход).
- [ ] **Соседние метрики НЕ затронуты**: расход по direct/seo/calls (не-пиксельные источники) не изменился — проверить `SUM(total_cost) WHERE _source_table NOT LIKE '%пиксель%'` ДО и ПОСЛЕ.

**Чем проверяется:** `ba5-golden-check` (verify default), MCP `postgres-victory` (read-only SQL).

---

### 1.2 step*.py (ETL-шаг: step0–step13)

**Целевая метрика:** зависит от шага (см. таблицу ниже). Главный oracle — строки в `big_analytics_full` / `fact_big_analytics`.

| Шаг | Что меняется | Метрика ДО→ПОСЛЕ |
|-----|-------------|-----------------|
| step0 | local-копии | COUNT по целевой таблице (напр. `local_perform_leads`) |
| step3 | источники direct/seo/pixel/telegram/crop | COUNT по `_source_table`, SUM(total_cost) |
| step6 | UNION → big_analytics_full | COUNT(*), NULL-инвариант источника |
| step11 | pixel_score доливка | пиксельные строки, дробность |
| step13 | arrival (визит-ось) | `big_analytics_full_arrival` COUNT |

**Чеклист:**

- [ ] **py_compile чистый**: `python3 -m py_compile путь/к/файлу.py` — exit 0, stderr пустой. (Если есть ruff: `ruff check --select E9,F путь/к/файлу.py`.)
- [ ] **Маркер патча** в коде (строка-комментарий вида `# PATCH_NAME_2026-MM-DD`) — для grep-верификации доезда на Victory.
- [ ] **Деплой через `deploy-victory`**: `python3 scripts/deploy_victory.py <файл> --marker <маркер>` — md5 Mac==Victory, grep-маркер найден, py_compile remote OK.
- [ ] **Не запускать прогон до подтверждения доезда** (md5 совпал + маркер найден на Victory).
- [ ] **После прогона — verify**: `ba5-golden-check` (exit 0). Если verify недоступен (таблицы TRUNCATE-нуты) — golden до cleanup, зафиксировать в STATE.md.
- [ ] **Соседние шаги не затронуты**: описать какие шаги/поля НЕ трогались (в отчёте oleg_programmer).

**Чем проверяется:** `deploy-victory` скил, `ba5-golden-check`, MCP `postgres-victory`.

---

### 1.3 build_star.py (пересборка звёздной схемы)

**Целевая метрика:** `public.fact_big_analytics` строки и суммы совпадают с `big_analytics_full` по расходу/воронке.

**Чеклист:**

- [ ] **py_compile**: `python3 -m py_compile build_star.py` — OK.
- [ ] **Деплой через `deploy-victory`** с маркером — md5 Mac==Victory подтверждён ДО запуска.
- [ ] **verify --full блок 12** PASS (star-сверка: `fact_big_analytics` vs unified).
- [ ] **verify блок 1 PASS** — расход Кудерко в допуске (star-прогон не должен сдвинуть расход).
- [ ] **VIEW vs TABLE решение**: если новый объект — VIEW при чистой проекции без JOIN/агрегации (экономия диска, быстрее деплой); TABLE — только при нужде в BRIN/lz4.
- [ ] **Нет лишних TABLE-копий**: `\dt public.*` на Victory — нет дублей star-таблиц из прошлых итераций.
- [ ] ⚠️ **НЕ запускать build_star.py с Victory-версией без md5-проверки** (инцидент 2026-06-07: стале-код затёр ручные DB-фиксы).

**Чем проверяется:** `ba5-golden-check --full --no-star` (быстро), затем `--full` (с блоком 12).

---

### 1.4 corrections.py (apply — правка данных между step3 и step4)

**Целевая метрика:** расход Кудерко `25 422 774.00 ± 100 ₽` (rule1 Кудерко — главный флаг работы corrections).

**Чеклист:**

- [ ] **Механика rule объяснена** — какой rule срабатывает, на каком срезе строк, что меняет.
- [ ] **py_compile**: `python3 -m py_compile corrections.py` — OK.
- [ ] **Rollback в `_interim_vacuum`**: убедиться что `vac.rollback()` на месте (KNOWN_ISSUES #14 — без него autocommit ломает транзакцию apply и rule1 не отрабатывает).
- [ ] **pipeline.py не глушит падение**: corrections.apply() НЕ должен проходить молча при ошибке — проверить лог прогона на `WARNING corrections` (KNOWN_ISSUES: pipeline ловит исключение как warning).
- [ ] **Деплой через `deploy-victory`** + маркер + md5.
- [ ] **Прогон + verify блок 1 PASS** — расход в допуске (главный индикатор rule1 Кудерко).
- [ ] **Строки rule1 в логе**: в stdout pipeline.py должна быть строка вида `Rule 1 (Кудерко) = N строк` с N > 90 000 (если меньше — rule1 не отработал).
- [ ] **Соседние rules не затронуты**: если правили rule0c/rule1 — не изменились rule2/rule3/etc.

**Чем проверяется:** `ba5-golden-check`, лог прогона (stdout corrections.apply).

---

### 1.5 PBIP (Power BI / звёздная схема → отчёт)

**Целевая метрика:** DAX-мера показывает правильное значение в нужном визуале; переключение атрибуции работает (По дате заявки / По дате визита).

**Чеклист:**

- [ ] **Правильный файл** — редактируется PBIP `v00` со встроенной моделью (не тонкий отчёт, не .pbix-бинарь).
- [ ] **Нет `::int`/целочисленных каст** на мерах с дробным пиксельным весом.
- [ ] **Конкретные DAX-выражения/метрики показаны**: что именно изменилось в TMDL/DAX (diff строк).
- [ ] **ScopedEval-выражения проверены**: они НЕ реагируют на переключение контекста — убедиться отдельно что нужная мера обновилась.
- [ ] **Соседние метрики не затронуты**: перечислить конкретно какие меры/визуалы НЕ трогались.
- [ ] **Запись применилась**: перечитать файл после правки; при root-owned файлах — проверить права.
- [ ] **Incremental refresh — ЗАПРЕЩЁН** (по всему проекту). Не предлагать, не включать.

**Чем проверяется:** pbip-editor агент; визуальная проверка в Power BI Desktop после публикации.

---

## Часть 2 — DoD по ИСТОЧНИКАМ ПРАВДЫ (консистентность между ними)

Проект сводит три источника. Расхождение между ними — суть задачи сверки.

### Источник А: Google Sheets проджектов (таблицы салонов)

Что это: листы «Воронка по дням контекст» в таблицах салонов — расход/приезды/продажи от менеджеров.

**Как проверить консистентность с витриной:**
```bash
# Все салоны + краткий дайджест в Telegram:
python3 data_check/reconcile/reconcile.py --tg --skip-errors

# Один салон с детальным логом:
python3 data_check/reconcile/reconcile.py --salon "Кит-Авто" --verbose
```
Скил: `/crm-reconcile`

**Порог:** расход ≤ 2% расхождения — норма. Приезды/продажи: известный лимит −35…−60% (Asterisk≠CRM), это by-design.

**Реестр салонов:** `data_check/reconcile/registry.json` (21 салон).

**DoD-чеклист для задач сверки с GSheets:**
- [ ] `reconcile.py` exit 0 (все салоны ≤ 2% по расходу) или объяснение каждого флага.
- [ ] Если флаг — root-cause по методике `SHEET_RECONCILE_METHODOLOGY.md` (дефект A–E).
- [ ] Новый салон/месяц — добавлен в `registry.json`.

---

### Источник Б: PostgreSQL `ad_analytics_bi` на Victory (витрина)

Что это: `public.fact_big_analytics` (лёгкий факт звезды), `public.big_analytics_full` (staging), `big_analytics_unified`.

**Как проверить:**

```bash
# Быстрый (14 блоков):
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 data_check/verify_big_analytics.py"

# Полный (+ GSheets + star):
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 data_check/verify_big_analytics.py --full"

# Golden Кудерко напрямую (если скрипт недоступен):
# через MCP postgres-victory:
SELECT round(sum(total_cost)::numeric,2) AS rashod, floor(sum(prodazhi)) AS prodazhi
FROM public.fact_big_analytics
WHERE "специалист"='Кудерко Семен'
  AND "атрибуция"='По дате заявки'
  AND _source_table IN ('direct','tp8','tp9','tp10','seo','calls','direct_unmatched','direct_zero');
-- Ожидаем: rashod ≈ 25422774.00 ±100, prodazhi ≥ 54
```

**Свежесть витрины** (нет колонки `updated_at` — смотреть через лог):
```sql
SELECT step, MAX(run_at) AS last_ok
FROM public.data_quality_log
WHERE status='ok' AND step IN ('step8','build_star','cleanup_intermediate')
GROUP BY step ORDER BY last_ok DESC;
-- Если > суток назад — нужен прогон
```

**DoD-чеклист для задач по БД:**
- [ ] `verify_big_analytics.py` exit 0 (все 14 блоков PASS).
- [ ] Свежесть: `last_ok step8` не старше 24 часов (или объяснение почему старше — by-design).
- [ ] Нет `pipeline_degraded warning` в `data_quality_log` без объяснения.
- [ ] Диск Victory `Use% < 85%` (иначе — чистка bloat перед прогоном, `work-ba-check` блок 2).

---

### Источник В: Power BI (свежесть витрины в отчёте)

Что это: датасет «Victoryanalyst» — Import-режим, обновляется через API-рефреш после прогона.

**Как проверить свежесть:**
```bash
# Статус последнего PBI-рефреша (через pipeline_powerbi.py лог):
ssh victory "~/venv/bin/python3 ~/pgq.py \"
SELECT step, status, run_at FROM public.data_quality_log
WHERE step='powerbi_refresh' ORDER BY run_at DESC LIMIT 3\""
```

**DoD-чеклист для задач связанных с PBI:**
- [ ] `powerbi_refresh` в `data_quality_log` — `status=ok`, время — после последнего прогона пайплайна.
- [ ] В Power BI Desktop: последнее обновление датасета совпадает со временем рефреша.
- [ ] Визуально: значение по Кудерко в отчёте совпадает с golden SQL (расход ≈ 25.4M, продажи ≥ 54).
- [ ] Отчёт открывается без ошибок «источник данных недоступен».

---

## Сводная матрица «тип задачи → инструмент проверки»

| Тип задачи | Основной инструмент | Дополнительно |
|------------|--------------------|--------------------|
| Атрибуция / пиксель | `ba5-golden-check` блоки 6, 10, 1 | MCP postgres-victory (дробность) |
| step*.py ETL | `ba5-golden-check` (все блоки) | `deploy-victory` (доезд) |
| build_star.py | `ba5-golden-check --full` блок 12 | `\dt public.*` (лишние TABLE) |
| corrections.py | `ba5-golden-check` блок 1 | Лог прогона (rule1 строки) |
| PBIP / Power BI | pbip-editor агент | Визуальная проверка в PBI Desktop |
| Сверка с GSheets | `/crm-reconcile` (reconcile.py) | `SHEET_RECONCILE_METHODOLOGY.md` |
| Витрина PostgreSQL | `verify_big_analytics.py` | `work-ba-check` (диск, статус) |
| Свежесть PBI | `data_quality_log` step=powerbi_refresh | PBI Desktop последнее обновление |

---

## Протокол сдачи oleg_programmer → director

После шага «правка кода» oleg_programmer передаёт:
1. Root-cause механикой: что на что влияет, какая мера/строка/стадия зависит от правки.
2. Diff кода: файл, строки, было → стало.
3. Маркер патча и план деплоя (через `deploy-victory`).
4. Точки golden-проверки для director: какие блоки verify ожидаются PASS.

После шага «деплой+прогон» oleg_programmer передаёт:
1. Статус деплоя: md5 Mac==Victory + маркер найден + py_compile remote OK.
2. Результат прогона: успех/ошибка, ключевые числа ДО→ПОСЛЕ.
3. verify exit code + блоки PASS/FAIL.

Director НЕ принимает задачу без пункта 4 / без exit code verify.

---

*Эталонные числа — `GOLDEN_BASELINE.md`. Скрипт верификации — `data_check/verify_big_analytics.py`.*
*Этот файл обновлять при появлении нового типа задач или нового инструмента проверки.*
