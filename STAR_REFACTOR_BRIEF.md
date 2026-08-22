# ТЗ для director — рефакторинг big_analytics_v5 под звезду (star schema)

<!-- pixel-dedup-2026-08-15 -->
> 🧭 **Область в v6_ch (2026-08-15).** Бриф писался под v5/PostgreSQL (`public.arp_fact`,
> `public."Dim_*"`). В v6 звезда живёт в ClickHouse `ad_analytics`, а `arp_fact` / `arc_fact` /
> `arf_fact` **не существуют** — они вычеркнуты из контракта (`tests/test_pbi_contract_lists.py`),
> из-за чего три таблицы модели Power BI в v6 не собираются (`KNOWN_ISSUES.md` #39).
> Актуальный маппинг PBI — `PBI_TABLES.md` §0.

> ✅ **СТАТУС 2026-06-11: рефактор РЕАЛИЗОВАН и в проде.** ⚠️ Звезда **консолидирована в схему
> `public` (2026-06-10)** — отдельной схемы `star` БОЛЬШЕ НЕТ. Все упоминания `star.<имя>` ниже —
> исторические (момент PoC/построения); фактически объекты в `public`: `public.fact_big_analytics`,
> `public.arp_fact` (теперь **VIEW**), `public."Dim_*"`. Актуальный маппинг — [`PBI_TABLES.md`](PBI_TABLES.md)
> «ФАКТИЧЕСКИЙ маппинг». Этот файл сохранён как исторический бриф (мотивация, замеры, эталоны без потерь).
>
> Автор: главный агент (по итогам PoC 2026-06-06). Полный план + замеры:
> `Documents/креативы виктори/STAR_SCHEMA_PLAN.md`.
> Доступ к Victory (ssh victory + БД 5432) — у тебя есть. БД: `ad_analytics_bi` @ 103.88.240.90.

## Зачем
Power BI грузит денормализованные витрины медленно (~1100 строк/сек, egress ~2 МБ/с).
PoC доказал: лёгкий факт (только ключи+меры+атрибуция, 168 б вместо 1500 б) грузится **×6.7**.
Денормализация (JOIN справочников, запечённых в каждую из 3.68M строк) — корень проблемы.

## Что доказано (PoC, на живой БД, read-only)
- big_analytics_unified 5.37 ГБ / 3.68M; лёгкий факт → 7485 vs 1113 строк/сек (×6.7).
- ~4480 из 5373 МБ факта — справочный текст, зависящий от ключей `domain/CampaignId/AdGroupId/Date`.
- Звезда без потерь: салон по domain совпал 99.89%, CampaignName по CampaignId — 100% среди матчей.
- analytics_report_placement (12 ГБ, 8.7M) — тот же паттерн, ОБЩИЕ (conformed) измерения.

## Целевая архитектура (conformed star)
- **Dim_Site** ← `local_gsheet_sites` (ключ `domain`; дедуп 4 дубля domain).
- **Dim_Campaign** ← `campaign_status` (ключ `CampaignId`; проверить уникальность).
- **Dim_AdGroup** ← собрать по `AdGroupId` (+ `local_gsheet_naming` для ag_part1-7).
- **Dim_Date** ← дата-измерение (ключ `Date`).
- **fact big_analytics_*** (per-source, ЛЁГКИЕ): ключи + меры + `атрибуция` + `_source_table`.
- Те же dim обслуживают и **arp_fact** (лёгкий analytics_report_placement).

### Лёгкий факт — оставить ТОЛЬКО:
ключи: CampaignId, AdGroupId, domain, "Date"
маркеры: атрибуция, _source_table
меры: total_cost, Clicks, Impressions, kol_vo_zayavok, korr, kval, priezd, prodazhi, nekorr,
ne_otvechaet, nedozvon, filtr, priedet, dobro, dohod_do_kredita, RlAdjustmentId,
"План заявки", "План приезда", priezd_arrival_date, prodazhi_arrival_date

### Вынести в dim (убрать из факта):
салон, город, регион, тип_сайта, id_салона, направление, шаблон, site_quiz, проджект,
менеджер, специалист, "Название crm", "марки авто", статус (→ Dim_Site);
CampaignName, "номер кампании | название", campaign_status, payment_model, account_login (→ Dim_Campaign);
AdGroupName, adgroup_code, "номер группы | название группы", ag_part1-7, ag_part1_name (→ Dim_AdGroup);
"День недели", week_start (→ Dim_Date).
Удалить совсем (внутренние): key3, key_pixel_score (и в ARP: key, key2, row_hash).

## Задачи

### Шаг 2 (сверка файлов)
- Сравнить локальную `work/big_analytics_v5` (на маке/в репо) с `~/big_analytics_v5` на Victory.
- Сделать их идентичными (выяснить какая версия новее; локальная = feature/channel-folders).

### Шаг 4 (пайплайн/БД под звезду)
1. Создать dim-таблицы (как LOGGED): Dim_Site, Dim_Campaign, Dim_AdGroup, Dim_Date.
   - Dim_Site: `SELECT DISTINCT ON(domain) ...` из local_gsheet_sites (дедуп).
2. Переписать `step3_build_sources` / `step6_build_full`: писать **лёгкие per-source** big_analytics_*
   (БЕЗ справочного текста). Сохранить логику атрибуции (`атрибуция`, заявка+визита партиции).
3. **Прекратить** материализацию `big_analytics_full` и `big_analytics_unified` (экономия ~10 ГБ).
   - Снять чистку `big_analytics_direct` если она была ради места.
4. Аналогично для `analytics_report_placement` (`step_cron_night/report_placement/`): лёгкий arp_fact.
5. Обновить `refresh_powerbi.py` `_ALL_TABLES` (строки 48-59): заменить big_analytics_full на
   лёгкие per-source факты + dim-таблицы. Согласовать имена с моделью PBI (это сделает главный агент в шаге 5).

### Шаг 6 (запуск + верификация — данные не потеряны)
- Прогнать пайплайн на Victory (через **nohup**, см. rule_victory_nohup).
- Сверить агрегаты ДО/ПОСЛЕ: по месяцам/салону/источнику суммы
  total_cost, korr, kval, priezd, prodazhi должны совпасть с текущими и с эталон-дашбордом
  (приезды заявка/визит ИТОГО 34157/42979, продажи 2384/2838, расходы 854 755 513 — без пикселя).

## КРИТИЧНО — не сломать
- Атрибуция: колонка `атрибуция` (заявка/визита) и все меры ОСТАЮТСЯ в факте. claim/`(атрибуция)`-меры
  в модели PBI зависят от них — их главный агент уже починил, не трогать логику партиций.
- MIRROR+UNION (заявка + arrival/визита) сохранить — это основа атрибуции.
- Всё через секреты (`.secret/.env` через `loader.py`; ⚠️ `tokens.json` не существует), без хардкода.

## Разделение труда
- Director: шаги 2, 4, 6 (пайплайн + Victory + БД).
- Главный агент: шаг 5 (модель + отчёт Power BI: dim-таблицы, связи, перенацелить поля по 37 страницам).
- Координация по именам таблиц/колонок dim — через этот файл.

---

# СОЗДАННЫЕ ОБЪЕКТЫ (ШАГ 4 выполнен 2026-06-06, director)

> Все объекты — в **схеме `star`** БД `ad_analytics_bi` @ 103.88.240.90, РЯДОМ со старыми.
> Старые таблицы НЕ тронуты (см. ниже). Сборка: `star_refactor/build_star.py`,
> верификация: `star_refactor/verify_star.py` (запускать на Victory через `~/venv/bin/python3`).

## Источник истины для dim
Dim построены из **финального `big_analytics_unified`** (значения уже после
normalize_salons / backfill / corrections — ровно то, что показывает отчёт), плюс добивка
ключей из сырых справочников. Это решение в пользу цели «без потерь»: построение Dim_Site
напрямую из `local_gsheet_sites` сменило бы салон у ~60 доменов (4.6%) и потеряло бы имена
61 CampaignId, отсутствующих в `campaign_status`.

## Факты (лёгкие, ключи + маркеры + меры)

### `star.fact_big_analytics` — 698 МБ / 3 677 659 строк (было unified 5373 МБ → ×7.7)
Колоночная проекция `big_analytics_unified` (full ∪ arrival). MIRROR+UNION сохранён 1:1
через колонку `атрибуция`. Per-source — через `_source_table` (нативные партиции PBI).
Колонки:
```
КЛЮЧИ:    CampaignId(bigint), AdGroupId(bigint), domain(text), "Date"(date)
МАРКЕРЫ:  "атрибуция"(text: 'По дате заявки'|'По дате визита'), _source_table(text)
МЕРЫ дробные (numeric, ОБЯЗАТЕЛЬНО — пиксель-атрибуция дробит кредит):
          total_cost, kol_vo_zayavok, korr, kval, priezd, prodazhi
МЕРЫ целые (integer): "Clicks","Impressions",nekorr,ne_otvechaet,nedozvon,filtr,
          priedet,"План заявки","План приезда"
МЕРЫ (bigint): RlAdjustmentId, priezd_arrival_date, prodazhi_arrival_date,
          dohod_do_kredita, dobro
```
⚠️ priezd/prodazhi/korr/kval/kol_vo_zayavok — **numeric, не integer** (иначе теряется
дробная пиксель-атрибуция: priezd пикселя 6097 → 1614). Это была пойманная ошибка, исправлена.

### `star.arp_fact` — 1370 МБ / ~8 000 000 строк (было ARP 12 ГБ → ×8.7)
Лёгкий `analytics_report_placement`, те же conformed dim (Dim_Site по domain, Dim_Campaign
по CampaignId, Dim_AdGroup по AdGroupId, Dim_Date по "Date"). Колонки:
```
КЛЮЧИ:   CampaignId(bigint), AdGroupId(bigint), domain(text), "Date"(date)
АТРИБУТЫ факта: placement(text), ad_network_type(text), "тип_заявки"(text)
МЕРЫ:    cost(numeric), clicks(bigint), korr/kval/priezd/prodazhi(numeric),
         kol_vo_zayavok/nekorr/ne_otvechaet/nedozvon/filtr/priedet/
         dohod_do_kredita/dobro(integer)
```

## Измерения (conformed, LOGGED, с PRIMARY KEY)

### `star."Dim_Date"` — PK "Date" — 156 строк / 72 кБ
`"Date"(date)`, `week_start(date)`, `"День недели"(text)`, `year/month/day(smallint)`,
`month_key(int YYYYMM)`, `year_month(text: русское название месяца)`. → пометить как таблицу дат в PBI.

### `star."Dim_Campaign"` — PK CampaignId — 15 937 строк / 10 МБ
`CampaignId(bigint)`, `CampaignName`, `account_login`, `статус_кампании`, `специалист`,
`manager_login`, `campaign_status`, `payment_model`, `"номер кампании | название кампании"`.
(15 876 из campaign_status + 61 добивка из факта.)

### `star."Dim_AdGroup"` — PK AdGroupId — 151 166 строк / 60 МБ
`AdGroupId(bigint)`, `AdGroupName`, `adgroup_code`, `"номер группы | название группы"`,
`ag_part1..7`, `ag_part1_name`, `parent_CampaignId(bigint)`.

### `star."Dim_Site"` — PK domain — 4 262 строки / 1536 кБ
`domain(text)`, `салон`, `город`, `регион`, `тип_сайта`, `id_салона`, `направление`,
`шаблон`, `site_quiz`, `проджект`, `менеджер`, `специалист`, `Название crm`, `марки авто`,
`статус_сайта`. (1312 доменов из факта + добивка из local_gsheet_sites, дедуп DISTINCT ON.)

## Связи для модели PBI (single-direction, Dim 1 → факт *)
```
Dim_Date["Date"]      → fact_big_analytics["Date"]   и  arp_fact["Date"]
Dim_Site[domain]      → fact_big_analytics[domain]   и  arp_fact[domain]
Dim_Campaign[CampaignId] → fact_big_analytics[CampaignId] и arp_fact[CampaignId]
Dim_AdGroup[AdGroupId]   → fact_big_analytics[AdGroupId]  и arp_fact[AdGroupId]
```
Срез заявка/визита — фильтр по `fact_big_analytics["атрибуция"]` (TREATAS НЕ нужен).

## Перенацеливание полей отчёта (факт → dim)
- салон/город/регион/тип_сайта/id_салона/направление/шаблон/site_quiz/проджект/менеджер/
  специалист/«Название crm»/«марки авто»/статус → **Dim_Site**
- CampaignName/«номер кампании | название»/campaign_status/payment_model/account_login → **Dim_Campaign**
- AdGroupName/adgroup_code/«номер группы | название»/ag_part1-7/ag_part1_name → **Dim_AdGroup**
- «День недели»/week_start → **Dim_Date**
- claim/`(атрибуция)`-меры остаются на факте — НЕ трогать.

## Верификация без потерь (ШАГ 6 — ПРОЙДЕНА)
`star.fact_big_analytics` vs `big_analytics_unified` — совпали ВСЕ срезы (всего, по атрибуция,
по _source_table, помесячно, по 1313 доменам). Эталон:
- заявка без пикселя: **priezd 34157 / prodazhi 2384 / cost 854 755 512.77** ✅
- визита: **priezd 42979 / prodazhi 2838** ✅
- ARP: в одной консистентной транзакции source == arp_fact (count/cost/clicks полностью совпали).

## Что НЕ менялось (прод жив)
big_analytics_full (4963 МБ), big_analytics_full_arrival (21 МБ), big_analytics_unified
(5373 МБ), analytics_report_placement (12 ГБ) — НЕ тронуты. Пайплайн (pipeline.py /
fast_pipeline.py / refresh_powerbi.py `_ALL_TABLES`) НЕ менялся — переключение PBI на
star + интеграция в пайплайн делается после приёмки модели (шаг 5).

## Скрипты
- `star_refactor/build_star.py` — идемпотентная пересборка всех star-объектов (CTAS + dim).
  ⚠️ VACUUM с `max_parallel_maintenance_workers=0` (иначе DiskFull в /dev/shm).
- `star_refactor/verify_star.py` — сверка агрегатов new vs old.
