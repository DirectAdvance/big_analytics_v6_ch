# KNOWN_ISSUES — big_analytics_v5

> Реестр известных дефектов и «грабель». Статусы: 🔴 OPEN · 🟡 PARTIAL · 🟢 FIXED · ⚪ WONTFIX/BY DESIGN.
> OPEN-дефекты — полная детализация. FIXED/BY DESIGN — краткая запись (полный разбор — в git history / MEMORY_ARCHIVE.md).
> Находки аудита 2026-06-05: obs #169–#221 агент `director`.

---

## OPEN — требуют работы

### 🔴 #1 — CPL посевов занижена в ~132x (неправильный знаменатель)

**Суть.** Для «посевы» CPL считается с неверным знаменателем приездов.
- Реальные посевы-приезды (`local_leads_all`, status ∈ Приехал/В салоне/Купил/Продажа, 2026-01-01…06-04): **254**
- Система использует `big_analytics_full_arrival` (BFA) → **33 630** (приезды ВСЕХ салонов, не только посевов)
- Истинный CPL ≈ **437 880 коп** (~4 379 ₽); система показывает 3 307 коп → занижение **132.4x**

**Где.** `big_analytics_full`, `big_analytics_full_arrival`, `local_leads_all` (utm_medium='posev').
**Корень.** Цепочка #2 + #3 + #4 ниже. Правильный знаменатель = приезды по дате визита именно для посевов, которых в BFA нет. Obs: #169, #178.

---

### 🔴 #2 — step13 фильтрует только `direction='Авто'` → посевы выпадают

**Суть.** `step13.py` строки ~111, ~229 содержат `WHERE gs.direction = 'Авто'`. Посевы полностью пропускаются при маппинге салон↔специалист и расчёте даты приезда. BFA физически НЕ содержит посевных строк.

**Где.** `step13_arrival/step13.py` строки ~111, ~229.
**Следствие.** При «по дате визита» для посевов система подтягивает Авто-приезды того же салона — данные другого направления.
**Как чинить.** Расширить step13 на `direction='посевы'` или вынести посевы в отдельную ветку ETL. Obs: #173, #175, #203.

---

### 🔴 #3 — truncated phone JOIN в step13 (8 цифр vs 11)

**Суть.** step13 джойнит с `gsheet_sites` по телефону, но `raw_leads.phone` обрезан до **8 цифр**, а в gsheet — полные **11**. JOIN не срабатывает (особенно Marcar). `eff_arrival_date` = fallback `created_date`.

**Где.** `step13_arrival/step13.py` строки ~116–145 (комментарии 131–134).
**Проверка.** gsheet JOIN работает только для 2 из 15 специалистов посевов; у 13 — `arr_correct=NULL`. Obs: #175, #176.

---

### 🔴 #4 — `arrival_date` заполнен только у 27% посевов на уровне источника

**Суть.** В `local_leads_all` 2 797 лидов посевов, но `arrival_date` есть только у **756 (27%)**. Upstream ETL, не только фильтр step13. Помесячно: Jan 123/Feb 138/Mar 160/Apr 139/May 187/Jun 8.

**Где.** `local_leads_all` (utm_medium='posev'). `priezd_arrival_date` покрыт только **tp8** (1 238); calls (6 945) и crop_targeting (268) → NULL.
**Глобально.** `big_analytics_full` priezd по дате = 11 978 vs BFA = 34 082 (расхождение 2.8x). Obs: #170, #174.

---

### 🔴 #5 — производительность step10 JOIN: 4.8 мин из-за seqscan по CTE

**Суть.** В `load_telega_in_orders.py` LATERAL JOIN по `leads_agg` (CTE) делает seqscan на каждый из 1 318 заказов → **289 419 ms (4.8 мин)**. PostgreSQL не индексирует CTE.

**Где.** `step10_crop_targeting/load_telega_in_orders.py` (`run_query`, строки ~81–224).
**Как чинить.** Материализовать `leads_agg` в UNLOGGED/temp + `CREATE INDEX (utm_campaign, lead_utm_content)` (паттерн `_tmp_ag_parts_lookup`). Обязательно до включения в боевой pipeline.py. Obs: #218.

---

### 🔴 #6 — семантический дрейф `utm_campaign` (расходы Telega.in vs лиды, 89% unmatched)

**Суть.** Главный killer матчинга: слаг канала в Telega.in ≠ слаг в клик-URL лида. Правильный ключ = `utm_content`(дата DDMMYYYY) + `utm_campaign` → 97.8% match (208/214 complete-заказов). 53/72 orphan: `chp_krasnodara_max` (заказ) vs `chp_krasnodara_i_kraya_max` (лиды). 19 заказов: кросс-доменное мультидоменных салонов.

**Где.** step10, `corrections.py` rule8 ~1234–1295; баг `victory_pxl` → должно быть «пиксель» ~1270. Obs: #217.

---

### 🔴 #7 — Power BI TREATAS пробрасывает только `[салон]+[Date]` (раздутие 14–31x)

**Суть.** Меры атрибуции «по дате визита» (`CALCULATE(SUM(BFA[...]), TREATAS([салон]), TREATAS([Date]))`). При разбивке глубже салона мера возвращает полный приезд салона:
- по домену: до **31x** (для «Уфа Центр Авто»/март)
- по специалисту: до **14x**; комбинация домен×специалист → до ~434x

**Где.** `Большая аналитика_v00.SemanticModel/.../big_analytics_full.tmdl` (меры ~263/277/291/305). BFA не содержит источник/направление/CampaignId/AdGroupName/поставщик → срезы молча игнорируются. ~59 визуалов, 17 комбинируют с разбивкой домен/специалист, ~36+ страниц затронуто. Корректны только разбивки ровно по салону.
**Как чинить.** Добавить недостающие измерения в BFA + TREATAS по ним, либо физически связать BAF↔BFA. Obs: #202, #203, #213, #214, #216, #221.

---

### 🔴 #22 — `perform_leads` FDW неполный: −657 обращений vs дилерский экспорт (upstream)

**Суть.** Обращений Перформа в системе −657 меньше дилерского эталона. НЕ баг пайплайна — источник FDW `perform_leads` отдаёт неполный набор. Пайплайн корректно обрабатывает полученное.
**Как чинить.** На стороне поставщика фида/FDW.

---

### 🔴 #25 — Victory диск-фул / осиротевший `pgsql_tmp` — ПОВТОРЯЮЩИЙСЯ (инфра, вне наших прав)

**Суть.** Каждый краш тяжёлого шага (ENOSPC/OOM) оставляет orphaned temp-файлы в `pgsql_tmp`. Postgres чистит только при рестарте — рестарта нет (нет root) → накапливается (~60 GB на 2026-07-11). Суммарный rebuild-footprint (`raw_yandex` ~9GB + `big_analytics_direct` ~16GB + `big_analytics_full` ~11GB + temp-спиллы) близок к потолку **184GB** диска (77 GB живых БД).

**Что НЕ решает.** `work_mem`/serial/disk-watchdog лишь сдвигают точку падения. TRUNCATE транзиентных = разовые ~9 GB. VACUUM FULL / WAL = тупик. Kill idle-in-transaction = 0 GB. `temp_file_limit` под `bi_analytic` = permission denied (SUSET). Legacy БД `ad_analytics`/`ad_analytics_other` трогать НЕЛЬЗЯ (живые FDW).

**Настоящий фикс (нужен root/админ Victory).** `systemctl restart postgresql` → чистит `pgsql_tmp` → ~70 GB свободно. Либо больше диска / отдельный том.

**⚠️ ПРАВИЛЬНАЯ РЕАКЦИЯ (анти-зацикливание).** При ENOSPC — НЕ перезапускать «на удачу» повторно. 1 диагностика → «нужен root-рестарт PG» → **СТОП.** Урок 2026-07-11: полдня/большой расход токенов ушли на повторные прогоны.

---

### 🔴 #26 — step3/big_analytics_direct: пиковый temp-спилл ~20GB валит прогон при недостатке диска

**Суть.** Пик диска в step3 ~**20GB** = heap `big_analytics_direct` (~8–14GB, ~21.7M строк) + temp-спилл (~7–13GB) от одного массивного SQL в `step3.py::_build_direct_sql()`. Глобальный `DISTINCT ON`/дедуп по всем датам сразу → полная внешняя сортировка широкой (~73 колонки) таблицы. `work_mem=4096MB` не помогает sort-узлам. `STEP3_DISK_WATCHDOG` самоотменяет запрос при free<2GB — это ШТАТНАЯ защита, не баг.

**Почему нельзя быстро починить.** `temp_file_limit` = permission denied; поднимать `work_mem` рискованно (swap=0 → OOM); `TRUNCATE fact_big_analytics` нельзя (защита /cpl API + PBI).

**Настоящее решение (кандидат).** Рефакторинг `_build_direct_sql` на батчинг по датам: `key3 = Date|CampaignId|...` (дата первым компонентом) + материализация `leads_deduped`/`leads_agg`/`leads_arrival_agg` **один раз глобально** (их батчить нельзя — глобальный дедуп; батчить можно только `base_join`/финальный `DISTINCT ON`). Требует golden-сверки построчно. Диагностика read-only 2026-07-12 (`oleg_read_bd`). Связано: #25.

---

## FIXED / BY DESIGN / PARTIAL — архив

> Полный разбор каждого — git history или `/work/big_analytics_v5/KNOWN_ISSUES_ARCHIVE.md` (если создан).

| # | Статус | Суть (1 строка) | Где / маркер |
|---|--------|-----------------|--------------|
| **#8** | ⚪ WONTFIX | `post_links::jsonb->>0` берёт только первый элемент — сейчас 0 delta, станет багом при многоссылочных заказах | `step10_crop_targeting/load_telega_in_orders.py` строки 201, 384, 391, 398 |
| **#9** | 📝 DOC-DEBT | PLAN.md содержит старые имена шагов (step4→step6, crop_targeting→step10). Актуальная карта — `PIPELINES.md`. WONTFIX | PLAN.md строки ~100-101, 459, 582-583 |
| **#10** | 🟢 FIXED 2026-06-11 | `Отказ клиента→visit` раздувал приезды в 3–5x (только PLEX). Фикс: `local_crm_statuses` `kind='status' Отказ клиента → qualified`. Код не менялся | `public.local_crm_statuses` |
| **#11** | 🟢 FIXED 2026-06-10 | `salon_overrides` разбудили латентную ветку `_build_calls_agg` → `c.salon does not exist`. Фикс: параметр `alias_salon='gs."salon"'` | `config/status_sql.py::_build_calls_agg` |
| **#12** | 🟢 ПРАВИЛО | Матчинг салонов — **только** через `corrections.salon_match_key()` (word-sort). Exact-сравнение запрещено. Авто-канонизация в `normalize_salons`. Коллизии word-sort проверять при добавлении салонов | `corrections.py::salon_match_key`, `normalize_salons` |
| **#13** | 🟢 FIXED 2026-06-11 | Golden продажи 42→47: недобор в звонках (step6 ПОСЛЕ corrections), не в rule1. Фикс: `_patch_kuderko_calls_specialist()` | `step6_build_full/step6.py` L300, маркер `FIX-KUDERKO-CALLS-SALES-2026-06-11` |
| **#14** | 🟢 FIXED | `corrections.apply()` падала на `_interim_vacuum` (autocommit без rollback → idle-in-transaction). Фикс: `vac.rollback()` L1601 | `corrections.py::_interim_vacuum` |
| **#15** | 🟢 FIXED 2026-06-11 | PBI refresh «key didnt match Schema=star» (star→public) + localhost-credential блокер. Auto-rebind `_ensure_datasource_host()` в начале каждого прогона | `star_refactor/build_star.py` L201-206; `refresh_powerbi.py::_ensure_datasource_host` |
| **#16** | ⚪ BY DESIGN | Дубли в `local_crm_statuses` (разный `lead_status` для разных CRM/салонов) — не чистить, это контекстный маппинг | `public.local_crm_statuses` |
| **#17** | ⚪ BY DESIGN | Расход `account_login` «растекается» по доменам (лид привязан к домену, не к аккаунту). Сверять по CRM целиком, tp8 — справочно | `step12_proverka/step12.py` v3 |
| **#18** | ⚪ BY DESIGN | Расхождение продаж «по визиту» vs «по заявке» (две независимые оси; у продаж без `arrival_date` — только заявочная). Симметрия специалиста в BFA — `PATCH-SPECIALIST-SYMMETRY-2026-06-15` | `step13_arrival/step13.py` |
| **#19** | 🟢 FIXED 2026-06-22 | `raw_yandex.total_cost=0` при транзиентном FDW-сбое → golden=0. Fail-fast guard добавлен в step1 | `step1_load_raw/step1.py` строки ~680–694, маркер `RAW_YANDEX_COST_GUARD_2026-06-22` |
| **#20** | 🟢 FIXED 2026-07-02 | psycopg2 multi-statement DDL → IndexError; `%пиксель%` без экранирования → IndexError. Фикс: split на кортежи + `%%пиксель%%` | `step8_stats/funnel_drift_snapshot.py` строки ~43, ~96, ~467, маркер `MULTISTATEMENT_FIX_2026-07-02` |
| **#21** | 🟢 FIXED 2026-07-03 | COMPARISON_SQL висел вечно на FDW-блокировке. Фикс: `SET LOCAL statement_timeout = '300000'` | `yandex_direct_checking_report/report.py` строка ~370, маркер `STMT_TIMEOUT_SVERKA_2026-07-03` |
| **#23** | ⚪ BY DESIGN | `fact_vk_ads` наполняется по мере VK-разметки (внедрена ~2026-07-06, near-0 воронка — норма) | `star_refactor/build_star.py::build_vk_ads_fact` |
| **#27** | 🟢 FIXED 2026-07-22 | DROP мёртвых таблиц `fact_criterion_spend_marka_kupit` (2416 MB) и `yandex_direct_cookie_analytics_website_pages_bak_20260718` (497 MB) — освобождено ~3 GB, диск 32 GB → 35 GB. Устраняет STEP6_DISK_GUARD false-positive | Victory VPS public schema |
| **#28** | 🟢 FIXED 2026-08-05 | PBIP `Большая аналитика_admin_ch`: `big_analytics_full` и `fact_direct_feed_funnel` имели по 12 месячных партиций → **Power BI Desktop не обновляет multi-partition таблицы** (меню «Обновить» серое целиком, обновление возможно только через XMLA/Tabular Editor). Плюс M-запрос `big_analytics_full` типизировал несуществующую колонку `week_start` (её нет в `bi_pbi_big_analytics_full`) → refresh падал бы и без первой проблемы. Фикс: 12 партиций → 1 без фильтра дат, `week_start` убран из `TransformColumnTypes`. **Правило: в PBIP под Desktop — одна партиция на таблицу** | `Отчеты_victory_Powerbi/Большая аналитика_admin_ch/…SemanticModel/definition/tables/{big_analytics_full,fact_direct_feed_funnel}.tmdl` |
| **#24** | 🟡 PARTIAL | `big_analytics_unified` на Victory наблюдался пустым — `cleanup_intermediate` TRUNCATE'ит его после логирования. Блок star-сверки в verify нечего проверять (golden по расходу/продажам при этом цел — читает `fact_big_analytics`) | `public.big_analytics_unified` |

---

### Как пополнять
По итогам сессии добавляй OPEN находки с полной детализацией. FIXED/BY DESIGN — однострочно в таблицу выше.
