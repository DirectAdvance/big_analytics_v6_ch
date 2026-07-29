# PROJECT_CHARTER.md — Устав проекта big_analytics_v5

> Высокоуровневый устав: зачем проект существует, кто владелец, как устроена
> архитектура, какие таблицы критичны, какие инварианты нельзя нарушать, что и
> когда запускается, где точки отказа. Технические подробности по шагам — в
> [`CLAUDE.md`](CLAUDE.md), [`PIPELINES.md`](PIPELINES.md), [`DB_TABLES.md`](DB_TABLES.md).
>
> Составлено: 2026-06-04. Факты по БД проверены read-only на Victory `ad_analytics_bi`.

---

## 1. Назначение

`big_analytics_v5` — аналитический ETL-пайплайн рекламного агентства Victory Direct.
Собирает данные из множества источников (Яндекс.Директ, CRM-заявки, звонки, SEO,
пиксель, Telegram/VK/MAX-посевы, отзывы) в единую витрину `big_analytics_full` и
сопутствующие справочные таблицы, которые затем потребляет дашборд **Power BI**
(«Большая аналитика»). Пайплайн считает сквозную **воронку**:
расход → клики → заявки → квалификация → визит → продажа → доход → одобрение кредита.

**Зачем:** один достоверный срез эффективности рекламы по кампаниям, доменам,
салонам, специалистам и каналам — для принятия решений по ставкам, бюджетам и UTM.

---

## 2. Владелец и контекст

| Поле | Значение |
|---|---|
| Владелец / разработчик | Семён (semenkuderko315@gmail.com), DirectAdvance |
| Заказчик данных | Victory Direct (рекламное агентство) |
| Git | приватный `git@github.com:DirectAdvance/big_analytics_v5.git` (nested в `HomeServer_PythonProject`) |
| Локальный исходник | `HomeServer_PythonProject/work/big_analytics_v5/` |
| Деплой | `scp` → `victory:~/big_analytics_v5/`, запуск `~/venv/bin/python3` |
| Прод-БД | PostgreSQL 15 `ad_analytics_bi` на Victory VPS `103.88.240.90` (в Docker), роль `bi_analytic` |
| Источник-БД | `ad_analytics` (FDW `src.*`, только чтение) |
| Секреты | `HomeServer_PythonProject/.secret/.env` (+ `loader.py`); на Victory `~/.secret/.env`. Никогда не в коде/коммитах. |

---

## 3. Архитектура: источники → pipeline → витрины → Power BI

```
ИСТОЧНИКИ (FDW src.* в ad_analytics, read-only)
  ├─ leads ................ CRM-заявки и звонки
  ├─ yandex_direct_* ...... расходы/клики Яндекс.Директ
  ├─ domains, lead_statuses, crm_statuses
  ├─ gsheet_* ............. сайты, нейминг, реестр, план/факт, клиенты, Маркар-доезды
  └─ Grid/OAuth API ....... статусы кампаний, история, UTM-аудит, корректировки, 404 (Метрика)
        │
        ▼  step0: TRUNCATE+INSERT / UPSERT
ЛОКАЛЬНЫЕ КОПИИ (public.local_*, LOGGED, эталон, не удаляются)
        │
        ▼  step1: DROP+CREATE UNLOGGED (фильтрация)
RAW (raw_yandex / raw_leads / raw_calls / raw_domains, UNLOGGED, пересоздаются)
        │  step2: индексы + ANALYZE
        ▼  step3: сборка по источникам + corrections.apply (между 3 и 4)
ИСТОЧНИКОВЫЕ ВИТРИНЫ (big_analytics_direct / seo / pixel / telegram / reviews / crop_targeting)
        │  step4 campaign_status (Grid API) · step5 pixel · step11 pixel_score
        ▼  step6: UNION ALL + звонки + постобработка (UPDATE по салону/домену)
big_analytics_full  ──step7──▶ SET LOGGED + VACUUM ANALYZE + индексы
        │  step8 stats (Telegram-отчёт) · step9 history · step10 crop · step12 проверка
        ▼
ВИТРИНЫ ДЛЯ POWER BI (10 шт., см. PBI_TABLES.md)
        │  refresh_powerbi(_ALL_TABLES) → OAuth → POST /refreshes → polling
        ▼
POWER BI SERVICE (датасет «Большая аналитика_v00», воркспейс «Victory Analytics», Import-режим)
```

**Принцип:** все тяжёлые вычисления — в PostgreSQL (шаги пайплайна). Power BI делает
только простые агрегации, слайсеры, условное форматирование, drill-through.
Сложный DAX поверх миллионных таблиц запрещён (см. `README.md` → «Power BI»).

---

## 4. Ключевые таблицы

### Витрины, которые читает Power BI (10 шт.)
Полный справочник с гранулярностью, размерами и расписанием — **[`PBI_TABLES.md`](PBI_TABLES.md)**.
Authoritative-список — `_ALL_TABLES` в [`refresh_powerbi.py`](refresh_powerbi.py).

Кратко: `big_analytics_full` (главная, 9.4 GB), `analytics_report_placement` (12 GB),
`big_analytics_full_arrival`, `yandex_direct_history` (в PBI = `direct_history`),
`check_utm_fuck_direct`, `yandex_direct_korrektirovki`, `yandex_direct_404_errors`,
`yandex_direct_return_commission_report`, `pixel_score`,
`yandex_direct_cookie_analytics_website_pages`.

### Защищённые источники (не в PBI, удалять нельзя)
`yandex_direct_report_placement` (→ ARP), `local_yandex` (→ raw_yandex),
`big_analytics_pixel_score` (→ доливка в full).

### Конфигурация (правится вручную в БД)
`local_crm_statuses` (маппинг статусов воронки), `name_replacements` (замены имён).

---

## 4a. Текущий статус star-cutover (на 2026-06-07)

Проект переходит с денормализованной витрины `big_analytics_full` на **star-схему**
(лёгкий факт + conformed dim). Полный план, замеры и эталоны без потерь —
**[`STAR_REFACTOR_BRIEF.md`](STAR_REFACTOR_BRIEF.md)** (здесь не дублируется).

**✅ ЗАВЕРШЕНО (2026-06-11). ⚠️ Звезда консолидирована в схему `public` (2026-06-10) — отдельной `star` нет:**
- `public.fact_big_analytics` (лёгкий факт, ~3.82M строк vs unified 5.46 ГБ), `public.arp_fact`
  (**VIEW** над ARP, −3.4 ГБ); измерения `public."Dim_Site"` / `"Dim_Campaign"` (16 461, cs/pm у 5905) /
  `"Dim_AdGroup"` / `"Dim_Date"`.
- Верификация без потерь пройдена: расход/продажи совпали с golden 1:1 (25 422 774.00 / 47, 2026-06-11).
- PBI-модель перепубликована на `public.fact_big_analytics` + dim. Рассинхрон `Schema="star"` в TMDL →
  refresh «key didnt match» (см. [`KNOWN_ISSUES.md`](KNOWN_ISSUES.md) #15).

**Ещё НЕ переключено (живой прод):** `big_analytics_full` / `_arrival` / `_unified` и
`analytics_report_placement` материализуются по-старому; **пайплайн** (`pipeline.py` /
`fast_pipeline.py` / `refresh_powerbi.py` `_ALL_TABLES`) пока собирает и обновляет старые
денормализованные витрины. Интеграция star в пайплайн — после приёмки PBI-модели.

---

## 5. Инварианты воронки (нельзя нарушать)

Источник правды по статусам — `public.local_crm_statuses` (`kind='status'` → `leads.status`,
`kind='reason'` → `leads.reason`). Auto-merge в `config/status_sql.py` гарантирует вложенность:

**Status-сторона (по `leads.status`):**
```
обращения ⊇ kol_vo_zayavok ⊇ korr ⊇ kval ⊇ priezd(визит) ⊇ prodazhi(продажа)
```
**Reason-сторона (по `leads.reason`), отдельная:**
```
dohod_do_kredita(доход) ⊇ dobro(одобрено)
```

Гарантии: `korr ≥ kval ≥ priezd ≥ prodazhi` и `dohod_do_kredita ≥ dobro`.

Дополнительные инварианты:
- **Нет двойного учёта лидов:** direct ∩ crop_targeting = 0 (UTM-фильтры в step3 `leads_direct`).
- **Граница посевов 1 мая 2026:** до мая — ручной реестр Google Sheets, с мая — Telega.in API.
  Периоды не пересекаются (проверено: 0 нахлёста).
- **Заявки-сироты посевов** (VK/MAX без расхода) попадают в `_source_table='social_посевы'`
  с `total_cost=0` — заявки видны, расход не атрибутирован (известная дыра данных).
- **`big_analytics_full`** пересоздаётся через CTAS каждый прогон — на лету может быть пустой.

---

## 6. Пайплайны и расписание

| Пайплайн | Запуск | Что делает | ~Время |
|---|---|---|---|
| `step_cron_night/pipeline_night.py` | **cron 03:00 МСК** (00:00 UTC) | Тяжёлые API: metrika_yandex (grants) → step13_utm_direct_audit → korrektirovki → reviews → 404_errors | ~2.5 ч |
| `pipeline.py` | вручную | Полный дневной пересбор `big_analytics_*` (step0–10 + corrections + load_reviews + normalize/cleanup/prefix + step11/12 + step8). Ночные API не дублирует | ~30–40 мин |
| `fast_pipeline.py` | вручную | Точечный пересбор без API Директа/step9 (кэш campaign_status + history) | ~5–10 мин |
| `pipeline_powerbi.py` | вручную | Сверка расходов (стоп при \|Σ\| > 200 000 ₽) → `pipeline.main()` → **триггер refresh Power BI** | ~40–50 мин |
| `refresh_powerbi.py` | вручную / из pipeline_powerbi | Только refresh датасета PBI Service (`_ALL_TABLES`), polling до 60 мин | до 60 мин |

Вне `pipeline.py` строятся: `analytics_report_placement` (ночной report_placement),
`yandex_direct_return_commission_report` (calculation_agency_commission),
`yandex_direct_cookie_analytics_website_pages` (отдельный сервис).

Запуск (детали — `CLAUDE.md`):
```bash
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 pipeline.py"
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 pipeline_powerbi.py"
```

---

## 7. Точки отказа

| Точка | Симптом | Защита / реакция |
|---|---|---|
| Протухшие куки Яндекс.Директ | step4/step9 не получают статусы/историю | `config/cookies.py` self-healing: проверка → авто-рефреш с glavpotok → Telegram + стоп если мертво |
| Квота Метрики (200/5мин, 5000/сут) | 429 в UTM-аудите | 2 токена (primary `victorylotsofads04` + fallback), backoff, флаг `_metrika_quota_exhausted` |
| Скачок расходов | \|Σ разница\| > 200 000 ₽ | `pipeline_powerbi.py` стоп + retry через 30 мин + Telegram, PBI не обновляется |
| CTAS-пересборка full | запрос к `big_analytics_full` во время прогона → пусто/промежуточно | дожидаться стабилизации (count > 1M) |
| refresh Power BI | Failed/Cancelled/таймаут | polling статуса + Telegram-уведомление |
| Кириллические lookalike в CampaignName | `campaign_code='неверный кодер'` | нормализация `chr(1089)→'c'` в step1 |
| PG-настройки (ALTER SYSTEM) | нужен суперюзер | `bi_analytic` не суперюзер; per-role GUC через `ALTER ROLE … SET` (USERSET-параметры) |
| Деплой/секреты | токен в коде/коммите | `.gitignore` + ручная проверка diff; секреты только в `.secret/.env` |

Все уведомления (отчёты/ошибки/статистика) — в Telegram (чат `336635373`, прямой Bot API).

---

## 8. Реализация по источникам данных

> Как **фактически** (по коду pipeline) собирается каждая метрика: откуда берутся
> расходы, заявки, статусы и т.д. Формат каждого блока:
> **Источник → способ получения → скрипт/модуль → raw/local таблица → итоговая витрина.**
> Здесь — про **происхождение** данных. Справочник готовых таблиц для PBI — [`PBI_TABLES.md`](PBI_TABLES.md).

### 8.0 Общая карта источников

| Что | Способ получения | Скрипт / модуль | Промежуточная таблица | Итоговая витрина |
|---|---|---|---|---|
| **Расходы Директа** | FDW `ad_analytics.yandex_direct_manager_reports` | step0 `_sync_yandex_full` → step1 `_build_raw_yandex_sql` | `local_yandex` → `raw_yandex` | `big_analytics_direct` → `big_analytics_full` |
| **Заявки (лиды)** | FDW `ad_analytics.leads_all` (CRM-выгрузки) | step0 TRUNCATE+INSERT → step1 | `local_leads_all` → `raw_leads` | `big_analytics_direct` / `_seo` / `full` |
| **Звонки** | те же `leads_all`, `deal_type='Звонок'` | step1 → step6 inline | `local_leads_all` → `raw_calls` | `big_analytics_full` (`_source_table='calls'`) |
| **Статусы воронки** | конфиг-таблица `local_crm_statuses` | `config/status_sql.py` | `local_crm_statuses` | CASE-метрики во всех витринах |
| **Статусы кампаний** | Grid API (куки) | step4 `prefetch_statuses` | `campaign_status` | `big_analytics_full.campaign_status` |
| **Клики / показы** | те же расходы Директа | step0/step1 | `raw_yandex.Clicks/Impressions` | `big_analytics_full` |
| **Корректировки ставок** | Direct API v5 (OAuth) | `korrektirovki/run.py` (ночь) | — | `yandex_direct_korrektirovki` (PBI) |
| **404-ошибки** | Метрика API (OAuth) | `404_errors/404_errors.py` | — | `yandex_direct_404_errors` (PBI) |
| **Placement-отчёт** | Direct Reports API + `raw_leads` | `report_placement` (суббота) | — | `analytics_report_placement` (PBI) |
| **Cookie-аналитика** | Grid API BannerHref (куки) | отдельный сервис | — | `yandex_direct_cookie_analytics_website_pages` (PBI) |
| **Пиксель** | лиды `utm_source LIKE 'victory_%'` + Google Sheets | step5 → step11 | `big_analytics_pixel` → `pixel_score` | `full` (`пиксель_атрибуц`) + `pixel_score` (PBI) |
| **Посевы** | Google Sheets (<май) + Telega.in API (≥май) + VK/MAX | step10 | `crop_targeting_api_telegain_lead`, `gsheets_crop_targeting_account*` | `big_analytics_crop_targeting` → `full` |
| **Отзывы** | Direct Reports API (OAuth, еженедельно) | `direct_account_reviews` | `yandex_direct_reports_reviews` | `full` (`_source_table='reviews'`) |
| **История Директа** | GraphQL API (куки) | step9 | — | `yandex_direct_history` (PBI = `direct_history`) |
| **UTM-аудит** | Direct API + Метрика (OAuth) | `step13_utm_direct_audit` (ночь) | — | `check_utm`, `check_utm_fuck_direct` (PBI) |

---

### 8.1 Расходы (cost / total_cost)

Три независимых источника расходов в `big_analytics_full`:

**A. Расходы контекста (Яндекс.Директ) — основной поток**

`ad_analytics.yandex_direct_manager_reports` (FDW, выгрузка отчётов Директа во
внешнем сервисе) → step0 `_sync_yandex_full()` копирует в `local_yandex`
TRUNCATE+INSERT (атомарно, в одной транзакции; resync при любом расхождении
`count`/`MAX(Date)`) → step1 `_build_raw_yandex_sql()` фильтрует `CampaignId IS NOT NULL
AND CampaignId != 0` в `raw_yandex` (UNLOGGED) → step3 `_build_direct_sql()` агрегирует
в `big_analytics_direct` (фильтр расходов `victory-crm.ru` в CTE `base_join`) →
step6 UNION ALL → `big_analytics_full`.

- Колонка денег: `local_yandex.total_cost` (NUMERIC). `Cost` — расход без коррекции.
- `Clicks`, `Impressions` идут тем же путём (расходы и трафик — одна строка отчёта).

**B. Расходы посевов** — см. 8.5 (Google Sheets / Telega.in API, наценка ×1.22×1.30).

**C. Расходы пикселя** — см. 8.6 (вычисляемые: `заявки × ЦЗ` из Google Sheets-конфига).

**Звонки** имеют `total_cost = NULL` намеренно (у звонка нет расхода Директа).
**Отзывы** — расход берётся из Reports API кампаний отзывов (см. 8.7).

---

### 8.2 Заявки (лиды) и атрибуция

**Источник всех лидов и звонков — одна FDW-таблица** `ad_analytics.leads_all`
(в неё внешний слой сливает CRM-выгрузки: `crmf_excel` Фаиг, `plex_excel` Плекс,
`mega_crm_excel` Мега, `marcar_crm_excel` Маркар и др. — поле `source_type`).

Поток: `leads_all` → step0 TRUNCATE+INSERT → `local_leads_all` (постоянная) → step1
JOIN с `local_domains`:

| RAW-таблица | Фильтр |
|---|---|
| `raw_leads` | `deal_type != 'Звонок'` AND `domain_id NOT IN EXCLUDED_DOMAIN_IDS (1645,883)` |
| `raw_calls` | `deal_type = 'Звонок'` |

**Атрибуция по каналам** (step3, по `utm_*` лида):

| Канал | CTE / функция | Условие UTM |
|---|---|---|
| Контекст | `leads_direct` в `_build_direct_sql` | есть UTM Директа; **исключение** SEO-лидов `utm_source='seo' AND utm_medium='organic'` (Block D) |
| SEO | `_build_seo_sql` `leads_seo` | UTM пустой ИЛИ `seo/organic`; **NOT IN** 19 посевных доменов (Block K) |
| Пиксель | step5 | `utm_source LIKE 'victory_%'` |
| Посевы | `_build_crop_sql` + `_add_*` | домен из `gsheets_crop_targeting_account` или `utm_medium='posev'` |

- **Дедупликация лидов:** CTE `leads_deduped` по `phone+yclid` (приоритет — строка
  со статусом visit/sale, затем более старая).
- **Нет двойного учёта:** direct ∩ crop_targeting = 0 (UTM-фильтры step3). См. §5.
- **Маркар-патч:** статус `'Продажа'` доливается из Google Sheets «Маркар Доезды»
  (`local_gsheet_priezdi_marcar`) в step0 `_patch_marcar_statuses` — CRM не синхронит обратно.
- **fid-атрибуция:** `corrections.py:_patch_fid_attribution()` (после step3, не в step1).

---

### 8.3 Статусы воронки (`local_crm_statuses`)

**Конфиг-таблица, источник правды по воронке.** Базово синхронизируется из FDW
`ad_analytics.crm_statuses` (step0, маппинг колонок `_COLUMN_REMAPS`: `value→crm_status`,
`category→lead_status` и т.д.), затем **правится вручную в БД**
(`_patch_crm_statuses` закомментирован с 2026-05-20 — таблица считается всегда верной).

- `config/status_sql.py:load_status_sql()` генерирует 4 SQL-выражения
  (`status_cases`, `priezd_sql`, `calls_agg_cases`, `leads_agg_cases`).
- `kind='status'` → читается `leads.status`; `kind='reason'` → `leads.reason`.
- Auto-merge `_group_by_category()` гарантирует вложенность метрик (см. §5).
- Метрики `kol_vo_zayavok/korr/kval/priezd/prodazhi/nekorr/dohod_do_kredita/dobro`
  считаются во всех витринах (`big_analytics_*`, `analytics_report_placement`).
- Хардкод (не из таблицы): `ne_otvechaet`, `filtr`, `nedozvon`, `priedet`.
- Проверка целостности маппингов — `crm_mappings_check/check.py` (Telegram-отчёт: UNUSED / UNMAPPED status / UNMAPPED reason).

---

### 8.4 Клики / показы, статусы кампаний, история, корректировки, 404, placement, UTM-аудит (Директ)

**Клики/показы** — `raw_yandex.Clicks/Impressions`, тот же поток что расходы (8.1.A).

**Статусы кампаний (`campaign_status`)** — step4 `prefetch_statuses()`:
неофициальный **Grid API** Яндекс.Директа с **куками** (`cookies.json`, автообновление
с glavpotok.ru). 3 потока = 3 manager_login. Маппинг `PRIMARY_STATUS_MAP`
(`ACTIVE→Активна`, `STOPPED→Остановлена`, `ARCHIVED→Архив` …). Доп. `payment_model`
(за клики / за конверсии) из `strategy.payForConversion`. Результат → справочник
`campaign_status` → UPDATE `big_analytics_full.campaign_status` + emoji-префикс в step6.

**История изменений (`yandex_direct_history`, PBI = `direct_history`)** — step9
`prefetch_history()`: внутренний **GraphQL API** `direct.yandex.ru` с теми же куками
(стартует после step4 prefetch — избегает CSRF-конфликта). Инкрементально от
`MAX(date)+1`. Логины из `local_gsheet_sites` (`status='Контекст активно'`).

**Корректировки ставок (`yandex_direct_korrektirovki`)** — ночной `korrektirovki/run.py`:
**официальный Direct API v5** (OAuth `OAUTH_TOKEN_1/2`). Уровни CAMPAIGN+AD_GROUP,
DROP+CREATE+INSERT (полный снапшот, ~188 аккаунтов). Логины из `local_gsheet_sites`
(`status='Контекст активно'`). Специалист — из `directologist`, статус — из `campaign_status`.

**404-ошибки (`yandex_direct_404_errors`)** — ночной + дневной `404_errors.py`:
**Метрика API** (OAuth `victoryagency`, метрика `ym:pv:pageviews`, фильтр
`ym:pv:title=@'404'`). Список сайтов — Google Sheets `1Hw0...` лист «ВСЕ САЙТЫ»
(`Контекст активно`). Инкрементально: `MAX(visit_date) − 7 дней`, DELETE+INSERT.

**Placement-отчёт (`analytics_report_placement`)** — **отдельный cron (суббота 00:00 МСК)**,
`step_cron_night/report_placement/`. step1 `step1_fetch_direct.py`: **Direct Reports API**
(OAuth) по площадкам, аккаунты из `metrika_yandex` (5 целей), напрямую в
`analytics_report_placement` (с 2026-06-01 минуя `yandex_direct_report_placement`).
step2 `step2_build_analytics.py`: обогащает Direct-строки лидами из `raw_leads` по
`key2 = date|campaign_id|group_id|placement_key` (4-этапный UPDATE/DELETE/INSERT на
окне 61 день; **guard:** если `raw_leads` пуст → этапы A–D пропускаются, см. инцидент 2026-06-01).

**UTM-аудит (`check_utm`, `check_utm_fuck_direct`)** — ночной `step13_utm_direct_audit/run.py`:
**Direct API v5** (tp1–tp5, `TrackingParams` групп) + **Метрика** (tp6/tp7/tp8 МК/ТК без групп,
двух-токенная схема `METRIKA_TOKEN_AUDIT` primary + `METRIKA_TOKEN` fallback). Не использует
куки. `check_utm` DROP+CREATE каждый раз, `check_utm_fuck_direct` UPSERT (история проблем).

---

### 8.5 Посевы (crop targeting) — граница 1 мая 2026

Два источника одной сущности (закупка размещений), **разделённые по дате**, без нахлёста
(проверено: 0 пересечений). Полный pipeline — `step10_crop_targeting/pipeline.py` (4 шага):

| Период | Источник | Скрипт | Таблица |
|---|---|---|---|
| **< 2026-05-01** | Google Sheets `1RgYa...` (ручной реестр) | `load_crop_targeting.py` → `load_crop_targeting_leads.py` | `gsheets_crop_targeting_account` + `_pravilo_utm` → `gsheets_crop_targeting_account_leads` |
| **≥ 2026-05-01** | **Telega.in API** (через FDW `telega_in_orders`) | `load_telega_in_orders.py` (`load_api_leads.py`) | `local_telega_in_orders` (UPSERT в step0) → `crop_targeting_api_telegain_lead` |

- **Расход посевов:** Telega.in `price × 1.22 × 1.30` (НДС + наценка агентства) → `total_cost`.
  `utm_content` (DDMMYYYY) → `"Date"`. `channel_link` → `"CampaignName"`. Домен/салон/город —
  lookup из `local_gsheet_sites`.
- **Финальная доливка** — `load_crop_to_big_analytics.py` (после step7/CTAS):
  DELETE `_source_table='crop_targeting'` → INSERT gsheets (<май) + INSERT API (≥май)
  в `big_analytics_crop_targeting` → INSERT в `big_analytics_full`.
- **VK/MAX-посевы** — `_add_social_posev_to_crop_sql` (step3): заявки из лидов
  `utm_medium='posev'` для VK/MAX. **Заявки-сироты** (есть заявка, расход не заведён)
  попадают в `_source_table='social_посевы'` с `total_cost=0` (известная дыра, см. §5).
- **tp8** (МК/ТК-кампании Директа) — `_move_tp8_to_crop()` переносит из direct в crop
  с `_source_table='tp8'` (НЕ `'crop_targeting'`, иначе DELETE их сотрёт).
- **Звонки/SEO 19 посевных доменов** — `_add_crop_calls_sql` / `_add_crop_seo_sql`
  (Block K): идут через `big_analytics_crop_targeting`, а не обычными путями.

---

### 8.6 Пиксель (`pixel_score`) — атрибуция

**Сбор (`big_analytics_pixel`, step5 `build_pixel.py`):** лиды `local_leads_all` где
`utm_source LIKE 'victory_%'` (для пикселей `domain = utm_source`). Стоимость заявки —
из Google Sheets-конфига `local_pixel_config` (ID `1TIiLb...`):
`total_cost = kol_vo_zayavok × COALESCE(cost_per_lead, cost_total, 0)`. `sync_pixel_config.py`
синхронизирует конфиг до step5.

**Атрибуция (`pixel_score`, step11):** `big_analytics_pixel` **не** льётся в full через step6 —
step11 распределяет pixel-воронку салона по цепочке **салон → домен → кампания** по
взвешенному CR (`cr_composite = (1·kol_vo + 3·korr + 10·kval + 30·priezd + 100·prodazhi)/Clicks`),
benchmarks по домену из `big_analytics_direct` за тот же месяц. Результат:
`pixel_score` (PBI-таблица весов/скоров CPL) + строки в `big_analytics_full` с
`_source_table='пиксель_атрибуц'`.

---

### 8.7 Отзывы (`reviews`)

**Еженедельно** (вс 21:00 UTC) + дневной перенос. `direct_account_reviews/pipeline.py`:
1. `load_reviews.py` — Google Sheets лист «Power BI» A:E → справочник `yandex_direct_account_reviews`
   (город/салон/аккаунт/сайт/агентский аккаунт).
2. `fetch_direct_stats.py` — **Direct Reports API** (OAuth `DIRECT_REVIEWS_TOKEN_1..4`),
   `аккаунт`=`Client-Login` → `yandex_direct_reports_reviews` (статистика расходов кампаний отзывов).
3. `load_reviews_to_big_analytics.py` — INSERT в `big_analytics_full` (после step6/CTAS) с
   маркерами `manager_login='отзывы'`, `тип_заявки='отзывы'`, `_source_table='reviews'`,
   метрики воронки `NULL` (отзывы вне воронки заявок).

---

### 8.8 Cookie-аналитика страниц сайта (отдельный сервис)

Проект **вне** `big_analytics_v5`: `work/yandex_direct_cookie_analytics_website_pages/`
(`direct_master_report.py`). **Grid/GraphQL API** Мастера отчётов по измерению `BannerHref`
(URL объявления с UTM-шаблонами — публичный Reports API его не отдаёт). Куки агентского
аккаунта `victoryagency14` из `~/.secret/cookies.json`. Аккаунты — `local_gsheet_sites`
(`niche='Авто'`), цели — `metrika_yandex` (JOIN по domain). Пишет напрямую в PBI-таблицу
`yandex_direct_cookie_analytics_website_pages` (DROP+CREATE, `sum`=расход без НДS, `clicks`).

---

### 8.9 Воронка по дате визита (`big_analytics_full_arrival`)

step13_arrival: пересчёт воронки по **`arrival_date`** (дата фактического визита), не по
`created_date`. Источник — `raw_leads` + звонки `local_leads_all (deal_type='Звонок')`,
только `priezd + prodazhi`, фильтр `direction='Авто'`. Без cost/clicks (расходы привязаны
к дате заявки). `arrival_date` берётся по-разному по `source_type` (crmf/mega — из leads_all;
plex/marcar — `created_date`).

---

### 8.10 Места, требующие уточнения

> Не подтверждено напрямую кодом в рамках этой задачи (опираться осторожно):

- **Происхождение FDW-источников `ad_analytics.*`** (`leads_all`, `yandex_direct_manager_reports`,
  `crm_statuses`, `gsheet_*`): кто и как наполняет саму БД `ad_analytics` (внешние выгрузки CRM,
  отдельные коннекторы) — **за пределами этого репозитория**, в коде pipeline не виден.
- **Колонка-дата ARP** = `date` (lowercase), у full — `Date`. Логика `arrival_date` для
  `marcar_crm_excel` помечена в step13_arrival как «ПРОБЛЕМА» (используется `created_date`
  вместо реальной даты приезда) — **требует доработки**.
- **`payment_model=NULL`** у части активных кампаний (Direct API не вернул `payForConversion`
  для отдельных стратегий) — известная неполнота, источник не финализирован.
- **VK/MAX заявки-сироты** (`social_посевы`, `total_cost=0`): расход по ним не заведён ни в
  Google Sheets, ни в Telega.in (gsheets обрывается апрелем, Telega.in — только Telegram-каналы) —
  реальная дыра атрибуции расхода (см. §5).
- **Точные имена FDW vs `crop_targeting_api_telegain` (старая)**: после миграции 2026-05-18
  источник = `local_telega_in_orders` (FDW в той же БД `ad_analytics_bi`); старая
  `crop_targeting_api_telegain` не используется.

---

## 9. Ссылки на ключевые файлы

| Что | Файл |
|---|---|
| Технические решения, блоки A–L, воронка CRM | [`CLAUDE.md`](CLAUDE.md) |
| Шаги пайплайна + времена + run_id | [`PIPELINES.md`](PIPELINES.md#steps-map) |
| Схема всех таблиц + жизненный цикл | [`DB_TABLES.md`](DB_TABLES.md) |
| **Таблицы для Power BI (справочник)** | **[`PBI_TABLES.md`](PBI_TABLES.md)** |
| Точка входа пайплайна | [`pipeline.py`](pipeline.py) |
| Пайплайн со сверкой + refresh PBI | [`pipeline_powerbi.py`](pipeline_powerbi.py) |
| Триггер refresh PBI + `_ALL_TABLES` | [`refresh_powerbi.py`](refresh_powerbi.py) |
| Корректировки специалистов/имён | [`corrections.py`](corrections.py) |
| Генерация SQL воронки из local_crm_statuses | `config/status_sql.py` |
| Имена таблиц / настройки подключения | `config/settings.py` |
| Ночной пайплайн | `step_cron_night/pipeline_night.py` |
