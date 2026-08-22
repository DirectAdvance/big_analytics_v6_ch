# MEMORY.md — big_analytics_v5 (condensed patterns)

<!-- v6-scope-banner -->
> 🧭 **Область в v6_ch (2026-08-15).** Файл — дистиллят паттернов эпохи v5/PostgreSQL.
> Инварианты (дробная атрибуция, вложенность воронки, `специалист` вместо `директолог`) остаются
> в силе; всё, что про `UNLOGGED`, индексы, `VACUUM` и FDW, к v6 не относится.
> Актуальный статус — `STATE.md`, дефекты — `KNOWN_ISSUES.md`.

> Дистиллированные паттерны и инварианты. Полные нарративы сессий — `MEMORY_ARCHIVE.md`.
> Актуальны на 2026-07-15. Обновлять после сессий, меняющих поведение системы.

---

## Диск / Инфра

**DISKFREE_DROP_FIRST.** DROP + CREATE DDL + INSERT вместо CTAS: CTAS держит старую версию до конца → двойной пик диска. Паттерн: DROP IF EXISTS → CREATE TABLE → INSERT INTO ... SELECT.

**EARLY_DISK_GUARD.** Читает диск ПОСЛЕ step0/1/2/3 (обычно ~24.8 GB занято), НЕ до пайплайна (~29–34 GB baseline с фоновыми процессами). Порог `EARLY_DISK_GUARD` настроен под это.

**AUTOHEAL.** Включает ТОЛЬКО транзиентные таблицы (big_analytics_direct, raw_yandex, big_analytics_full, big_analytics_unified, pixel_score, full_arrival). НЕ включает `fact_big_analytics` (дурабл/protect). После добавления в AUTOHEAL — проверить, что таблица не читается downstream шагами в этой же сессии.

**P3 freshness-skip.** Если `fdw_count == raw_count` → step1 пропускает пересборку raw_yandex → RAW_YANDEX_PREFREE освободит 0 GB. EARLY_DISK_GUARD при этом всё равно может TRUNCATE big_analytics_direct от предыдущего провального прогона — это +15 GB. Учитывать при диагностике «почему AUTOHEAL мало освободил».

**SPEND_PREFREE.** TRUNCATE big_analytics_direct + raw_yandex ПЕРЕД spend-фазой (region/adformat/criterion CTAS), т.к. это последние читатели обоих объектов. `fast_pipeline.py`, маркер `SPEND_PREFREE_2026-06-18`. Порядок: build_unified (последний читатель) → TRUNCATE direct+raw → spend-билдеры.

**Disk-crisis (повторяющийся).** При ENOSPC от pgsql_tmp — НЕ перезапускать «на удачу» (#25). 1 диагностика → «нужен root-рестарт PG (systemctl restart postgresql)» → СТОП. Задеплоенные меры (CLEANUP_ON_FAILURE, disk-guard, watchdog) — смягчение, не решение. Полный разбор — KNOWN_ISSUES #25.

---

## Деплой / Запуск

**STANDALONE внешний скрипт.** Нужны: `init_pool()` ДО `get_conn()` + `conn.rollback()` ДО `conn.autocommit = True`. Без `rollback()` — psycopg2 в idle-in-transaction → `set_session cannot be used inside a transaction`. Паттерн:
```python
from config.db import init_pool, get_conn, put_conn
init_pool()
conn = get_conn()
try: conn.rollback()
except Exception: pass
conn.autocommit = True
# ... TRUNCATE / VACUUM ...
conn.autocommit = False
put_conn(conn)
```
Тот же паттерн в `corrections.py::_interim_vacuum` (KNOWN_ISSUES #14 FIXED: `vac.rollback()` L1601).

**STEP8 standalone.** Запускать через `python3 -c "import config.db as db_module; from pipeline import run_step; db_module.init_pool(); run_step(8, 'step8_stats.step8', RUN_ID, pipeline_wall_sec=X)"`. Флаг `--only-step=8` не работает (step8 не в `_ALL_STEPS`).

**build_star standalone.** `python3 star_refactor/build_star.py` напрямую. NOT standalone-скрипт до `init_pool()`. При partial pipeline: `build_star` строит из `big_analytics_unified` → нужно чтобы unified была заполнена.

**VIEW without full step run.** Вьюха пересоздаётся в `ensure_schema()` → `python3 -c "... ensure_schema(conn) ..."` напрямую на Victory без API-прогона step14 (2+ часа). Пример: добавить `"специалист"` в `v_yandex_direct_minus_delta` (маркер SPECIALIST_VIEW_2026-06-19).

**Mutagen НЕ синкает на Victory.** Всегда scp + grep-маркер + md5 Mac==Victory + py_compile ПЕРЕД запуском. Скилл `deploy-victory` (`scripts/deploy_victory.py`) — ⚠️ только для v5: зашит на `work/big_analytics_v5/` → `~/big_analytics_v5`, файлы v6_ch отвергает. Для v6_ch — ручной scp в `~/big_analytics_v6_ch` + `~/venv-v6`, см. `RUNBOOK.md` §3a.

**pipeline.py глушит падение corrections.** `corrections.apply()` исключение ловится как warning ~L482-483 → прогон продолжается молча. «Прогон без ошибки» ≠ «corrections отработали».

**--from-step=6 stale data trap.** step3 не перезапускается → big_analytics_direct содержит устаревшие CRM-данные из прошлого прогона. При диагностике воронки — проверить, когда последний раз шёл step3.

**--run-timeout для fast_pipeline.** При полном прогоне (~72+ мин) ставить `--run-timeout ≥5400с`. Короткий timeout убивает ssh-обёртку → broken-pipe → downstream шаги получают exit 120 → pipeline.py считает их критическими.

---

## SQL / ETL

**NOT IN с NULL.** `col NOT IN (list)` при `col IS NULL` → возвращает NULL (не False) → строки молча теряются. Всегда добавлять `OR col IS NULL`.

**psycopg2 % в строках.** Любой `%` в SQL (даже в LIKE/ILIKE) требует `%%` при передаче через psycopg2 параметризацию. `'%пиксель%'` → IndexError. Фикс: `'%%пиксель%%'` (маркер MULTISTATEMENT_FIX_2026-07-02, KNOWN_ISSUES #20 FIXED).

**psycopg2 multi-statement.** Несколько операторов через `;` в одном `execute()` → ProgrammingError. Разбивать на кортеж + итерировать: `for stmt in (DDL_A, DDL_B): cur.execute(stmt)`.

**INNER JOIN по (domain, date) → false positives.** Survivor-лид с другим utm_campaign за ту же (domain, date) → INNER JOIN хватает его → все кандидаты на той же дате считаются дублями. Всегда фильтровать по полному composite key или через прямой NOT EXISTS. (Урок POSEVDEDUP4→5 2026-06-19/20.)

**LOGGED vs UNLOGGED для cron-источников.** Cron-источники (`local_leads_all`, campaign_status и т.п.) — ТОЛЬКО LOGGED. UNLOGGED (raw_*) теряются при краше. WAL-взрыв: SET LOGGED для big_analytics_direct (~21M строк) генерирует WAL на весь размер таблицы → риск при маленьком WAL-буфере. Паттерн: только транзиентные промежуточные → UNLOGGED; cron-persistent → LOGGED.

**FULL OUTER JOIN + display fields.** Для unmatched строк (только один источник) display-поля = NULL без fallback. Добавлять `COALESCE(a.field, b.field)` для всех nullable display columns (паттерн FEED_META_LOOKUP 2026-06-29).

**MATERIALIZED CTE (MATONCE).** Дорогие CTEs (`account_manager_map`, `domain_source_type`), используемые в нескольких CTAS → материализовать как TEMP TABLE один раз:
```sql
CREATE TEMP TABLE _account_manager_map AS
  SELECT account_login, MAX(manager_login) AS manager_login
  FROM raw_yandex GROUP BY account_login;
CREATE INDEX ON _account_manager_map (account_login);
```
Паттерн MATONCE_ACCOUNT_MANAGER_MAP_2026-06-18 / MATONCE_DOMAIN_SOURCE_TYPE_2026-06-18.

**Тавтология IN temp-table в эталоне.** Если эталон фильтруется по `IN(tmp)` и строка проверяет `EXISTS(tmp)` — тавтология, missing всегда 0. Убрать `IN(tmp)` из эталона. (LOGIN_FILTER_REDESIGN_2026-06-18.)

**FDW lock / statement_timeout.** FDW-курсор зависает при DDL-lock на реальной таблице → добавлять `SET LOCAL statement_timeout` перед долгими FDW-запросами. Паттерн: KNOWN_ISSUES #21 FIXED (300000ms в `report.py`).

**Grain change в локальной таблице.** Изменение гранулярности `local_vk_*` ломает downstream консьюмеров → не менять grain, вместо этого CTE `vk_ads_by_plan` в step3 агрегирует до нужного grain перед join.

**COUNT без SUM.** `COUNT > 0` недостаточно для финансовых таблиц — проверять `SUM(total_cost) != 0` отдельно (урок RAW_YANDEX_COST_GUARD_2026-06-22, KNOWN_ISSUES #19 FIXED).

**Параллельные spend-CTAS.** 3 spend-билдера (region/adformat/criterion) независимы → ThreadPoolExecutor(max_workers=3) в fast_pipeline.py. Безопасно: нет общих TEMP-таблиц, разные target-таблицы, отдельные conn/транзакции. Маркер SPEEDUP_PARALLEL_SPEND_2026-06-18. Disk-guard: если свободно <15 GB → откат на последовательный.

**ANALYZE точечный.** `ANALYZE T_FULL` на 3.9M строк = 19 мин. Замена: `ANALYZE T_FULL ("направление")` — 1-2 мин, планировщик получает нужную статистику. Маркер SPEEDUP_ANALYZE_2026-06-18.

---

## Step-специфические паттерны

**step1 cost-guard.** После загрузки raw_yandex: `SELECT SUM(total_cost) FROM raw_yandex`. Если = 0 → RuntimeError (fail-fast). Маркер `RAW_YANDEX_COST_GUARD_2026-06-22` (~L680-694 step1.py). KNOWN_ISSUES #19 FIXED.

**step3 ordering-race.** telegain orders в `_telegain_orders_current_cte` должны быть детерминированно отсортированы (`ORDER BY` по всем ключевым полям). Маркер `ORDERING_RACE_FIX_2026-07-15`. Без этого: недетерминированные дубли посевов при параллельном прогоне.

**step6 Кудерко calls.** Звонки собираются INLINE в step6 ПОСЛЕ `corrections.apply()` → получают специалиста из gsheet_sites → недобор продаж при переданных логинах. Фикс: `_patch_kuderko_calls_specialist()` в step6.py L300, маркер `FIX-KUDERKO-CALLS-SALES-2026-06-11`. KNOWN_ISSUES #13 FIXED.

**step8 pixel nospec source.** ВСЕГДА читать из `big_analytics_pixel_score` (post-step11), НЕ из `big_analytics_pixel` (T_PIXEL). Цифра pixel_score бит-в-бит = витрина PBI. Маркер PIXEL_NOSPEC_SOURCE_FIX_2026-06-18.

**step10 seqscan.** LATERAL JOIN по CTE `leads_agg` → seqscan на каждый из 1318 заказов → 4.8 мин. Фикс (не сделан): материализовать в TEMP + CREATE INDEX (utm_campaign, lead_utm_content). KNOWN_ISSUES #5 OPEN.

**step11 pixel attribution.** НИКОГДА не приводить к int (усечение долей = главный исторический баг). Атрибуционные веса (`attr_pixel_квал_кампании`/`_приезд_кампании`/`_продажи_кампании`, `расход`) — дробные NUMERIC.

**step13 direction='Авто'.** step13 строки ~111, ~229 фильтруют только `direction='Авто'` → посевы выпадают из `big_analytics_full_arrival`. KNOWN_ISSUES #2 OPEN. Маппинг марокарских телефонов: 8 цифр vs 11 → JOIN не срабатывает. KNOWN_ISSUES #3 OPEN.

**step13 мёртвый phone JOIN.** Перед добавлением phone-матча — проверить SELECT COUNT на оба источника. Если 0 — использовать record_id из CRM-ссылки (если есть). Маркер MARCAR_ID_JOIN_FIX_2026-06-18.

**step14 DDL без прогона.** `ensure_schema(conn)` пересоздаёт вьюху → применять инлайн без запуска полного step14 API.

**corrections _interim_vacuum.** `vac.rollback()` ПЕРЕД `autocommit = True` (L1601 corrections.py). Без rollback — idle-in-transaction → `set_session cannot be used inside a transaction`. KNOWN_ISSUES #14 FIXED.

**cleanup_intermediate truncates unified.** `cleanup_intermediate` TRUNCATE'ит `big_analytics_unified` ПОСЛЕ логирования → verify star-блок нечего сверять (golden по расходу/продажам при этом цел — он читает `fact_big_analytics`). KNOWN_ISSUES #24.

---

## Golden / Verify

**Golden НЕ фильтровать по Date.** `verify_big_analytics.py` читает `fact_big_analytics` БЕЗ датного фильтра (`GOLDEN_BASELINE.md §5`). Датный фильтр даёт неверные числа.

**kval re-baseline 2026-07-15.** kval = 677 = category-kval (категория `qualified` в `config/status_sql.py`). НОРМА, не баг. Маркер `KVAL_REBASELINE_2026-07-15`. Эталон: kval_cost ~20928 ₽ ∈ [10000;30000]. Регрессия = откат к korr-negation (~8870 ₽ пробивает LO).

**POSEVDEDUP правильный инвариант.** Реальный дубль = лид одновременно: (A) выжил бы в social-фильтре step3 (NOT EXISTS gsheets AND NOT EXISTS API) И (B) попадает под crop-критерий (EXISTS gsheets OR EXISTS API). При корректном step3 это логически невозможно → COUNT всегда 0. НЕ использовать INNER JOIN к big_analytics_full по (domain, date) — false positives. Маркер POSEVDEDUP5_2026-06-20, POSEVDEDUP6 (block 13 в verify).

**BLOCK13 source_table.** Проверять `_source_table IN ('telegram', 'telegram_посевы')`, NOT exact 'telegram_посевы'. step3 пишет `'telegram'` (не `'telegram_посевы'`). Маркер BLOCK13FIX_2026-06-19.

**REFRESH_GATE Bug2.** Проверка наполнения пикселя → читать из ДУРАБЛ `fact_big_analytics`, НЕ из `big_analytics_full` (которая cleanup_intermediate truncate'ит). Иначе GATE всегда видит 0 строк и считает их «новыми». Маркер REFRESH_GATE_2026-07-12.

**golden_reward.py.** `reward = 1000 - cost_dist - sales_slack*100`; `-1000` при `hard_fail=True`. Константы из `verify_big_analytics.py` (единый источник). CLI: `python3 data_check/golden_reward.py [--pretty|--tg]`. Формула для best-of-N сравнения вариантов. Маркер `golden_reward_v2_2026-06-17`.

---

## VK / Посевы

**VK stats уровень.** `level='banners'` в `vk_ads_stats_day` — единственный уровень, SUM без фильтра = то же самое (дублей нет). `utm_content` VK-лида = `'ad_group_id/banner_id'` (слэш); id-bearing pattern: `^[0-9]{5,}/[0-9]{5,}$`.

**POSEV двойной учёт.** Инвариант: `direct ∩ crop_targeting = 0`. Проверять в POSEVDEDUP6 block 13. Нарушение означает ошибку в UTM-фильтрах step3 (`leads_direct` CTE).

**Telega.in utm_campaign drift.** Правильный ключ JOIN = `utm_content` (дата DDMMYYYY) + `utm_campaign`. Дрейф слага канала → до 89% unmatched если JOIN только по utm_campaign. KNOWN_ISSUES #6.

---

## Salon / CRM

**salon_match_key.** Матчинг ТОЛЬКО через word-sort ключ (`corrections.salon_match_key`). Exact-сравнение запрещено (перестановка слов → no match). `normalize_salons` авто-канонизирует при совпадении word-sort, расхождении exact. Новые салоны — проверить коллизии: `GROUP BY wkey HAVING COUNT(DISTINCT salon) > 1`. KNOWN_ISSUES #12.

**COMPARISON_SQL timeout.** FDW-запрос `yandex_direct_checking_report/report.py` (~L370) может зависнуть на DDL-lock. Фикс: `SET LOCAL statement_timeout = '300000'` перед COMPARISON_SQL. KNOWN_ISSUES #21 FIXED.

**Расход «растекается».** account_login в Директе обслуживает несколько доменов → расход «размазывается» по лидам каждого домена. Сверять по CRM целиком (`"Название crm"`), tp8 — справочно. KNOWN_ISSUES #17 BY DESIGN.

**Kval formula.** kval = `korr - ne_otvechaet - filtr - nedozvon` = category 'qualified' formula (`config/status_sql.py`). НЕ korr-negation (~8870 ₽). При расхождении kval — первым делом проверить `status_sql.py` и KVAL_COST_LO/HI границы.

---

## Ссылки

- Полные нарративы сессий: `MEMORY_ARCHIVE.md` (3093 строки, 2026-06-18 → 2026-07-12)
- Реестр дефектов: `KNOWN_ISSUES.md`
- Эталон golden: `GOLDEN_BASELINE.md` + `data_check/verify_big_analytics.py`
- Операционка восстановления: `RUNBOOK.md`
