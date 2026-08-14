# MEMORY — step8_stats

> ⚠️ Историческая запись ниже относится к PostgreSQL-версии `step8.py` (аккаунт-покрытие,
> `_LOGIN_FILTER`/`_ok()`). Текущий `step8.py` — read-only ClickHouse row-count-скрипт без этой
> логики (полностью переписан под v6_ch); маркеры `LOGIN_FILTER_FDW_2026-06-18` и т.п. в коде
> больше не существуют. Оставлено для истории миграции, не как описание текущего кода.

## 2026-06-18 — _LOGIN_FILTER: raw_yandex транзиентна → ложный эталон 0 + ложный ✅

**Симптом:** блок «Покрытие аккаунтов» показывал эталон = 0 и ✅ у всех источников
(in_yandex=0, missing=[]) между прогонами пайплайна.

**Причина:** `_LOGIN_FILTER` содержал `EXISTS (SELECT 1 FROM public.raw_yandex ... WHERE total_cost > 0)`.
`raw_yandex` — UNLOGGED таблица, очищается SPEND_PREFREE перед каждым прогоном.
Вне прогона она пустая → фильтр не пропускает ни один логин → `total_lc = 0` →
`missing = []` → `not missing = True` → `_ok()` возвращал ложный ✅.

**Фикс 1 (_LOGIN_FILTER):** заменили EXISTS по raw_yandex на:
```sql
AND login_key IN (
    SELECT DISTINCT account_login
    FROM public.yandex_direct_manager_reports
    WHERE total_cost > 0
)
```
FDW `yandex_direct_manager_reports` не очищается → эталон стабилен (~654 логина).
Колонка расхода: `total_cost` (double precision, идентична `Cost`) — 719 уникальных логинов в FDW.
EXPLAIN план: FDW один раз HashAggregate (cost≈100..141), затем Hash Join с local_gsheet_sites (~711 строк) — нет N коррелированных запросов.

**Фикс 2 (guard ложного ✅):** `_ok()` переписан:
```python
def _ok(missing):
    if total_lc == 0:
        return '⚠️ нет данных'
    return '✅' if not missing else f'⚠️ нет {len(missing)}'
```
`total_lc` видна в замыкании (определена в том же `if lc:` блоке выше).

**Фикс 3 (метка):** строка `'В yandex_direct_manager:'` → `'В manager_reports (FDW):'`
(`_T_MGR = public.yandex_direct_manager_reports`, таблицы `yandex_direct_manager` не существует).

**Файл:** `step8_stats/step8.py`, маркеры `LOGIN_FILTER_FDW_2026-06-18`, `LOGIN_GUARD_2026-06-18`, `LOGIN_LABEL_FDW_2026-06-18`.

**Числа после фикса (вне прогона, Victory):**
- Эталон: 654 (было 0)
- in_FDW manager_reports: 654 ✅ (все эталонные логины есть в FDW — по построению)
- in_manager_reports (FDW): 654 ✅
- in_big_analytics_full: 654 (пустая таблица вне прогона → после прогона реальное число)
