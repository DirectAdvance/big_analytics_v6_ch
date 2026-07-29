# PIPELINES.md — пайплайны, расписание, распределение шагов

> Детальное описание трёх пайплайнов, что в каком из них выполняется, двух-токенная
> схема Метрики, квоты, переименования таблиц. Вынесено из `CLAUDE.md` (lazy-load).
> Краткая шпаргалка запуска — в `CLAUDE.md`. Высокоуровневое расписание — в `PROJECT_CHARTER.md`.

---

## Три пайплайна: ночной, дневной, быстрый

### pipeline_night.py (cron 03:00 МСК)
Тяжёлые API-шаги, не влезающие в дневной режим. См. `step_cron_night/CLAUDE.md`.
1. `metrika_yandex.py` — grants счётчиков Метрики
2. `step13_utm_direct_audit/run.py` — UTM-аудит (~2 ч)
3. `korrektirovki/run.py` — корректировки ставок (~25 мин)
4. `direct_account_reviews/pipeline.py` — полный pipeline отзывов (API Директа + insert в big_analytics_full)
5. `404_errors/404_errors.py` — инкрементальная (~1 мин)
6. `step14_minus_snapshot/step14.py` — снапшот минус-фраз (campaign+groups+наборы) → `yandex_direct_minus_snapshot` (~5-15 мин)

### pipeline.py (вручную, ~30-40 мин)
Дневной полный пересбор `big_analytics_*`. Ночные шаги НЕ дублирует — данные за сегодня уже свежие после ночи.
- step0..7 (с фоновым step4_grid prefetch и step9_history prefetch)
- corrections.apply (после step3)
- step9_direct_history, step10_crop_targeting
- `load_reviews_to_big_analytics.py` — **перенос** из `yandex_direct_account_reviews` в `big_analytics_full` (не API; нужен потому что step6 пересоздаёт full через CTAS)
- `404_errors.py` — инкрементальная (намеренное дублирование с pipeline_night, 7-дн перекрытие)
- normalize_salons, fill_missing_regions, cleanup_old_dates, campaign_status_prefix
- step11_pixel_score, step12_proverka, crm_mappings_check
- step8_stats (последним, чтобы попало в Telegram-отчёт)

### fast_pipeline.py (вручную, ~5-10 мин)
Точечный (без API Директа, без step9). Использует кэш `campaign_status` и `yandex_direct_history`.
Spend-витрины НЕ строит (вынесены в `build_spend_daily.py`, маркер SPEND_NIGHT_JOB_2026-06-27).

### build_spend_daily.py (cron 09:00 UTC / 14:00 Екб — ежедневно)
Отдельный job сборки трёх spend-витрин `fact_region_spend`, `fact_adformat_spend`, `fact_criterion_spend`.
Вынесен из `pipeline.py` / `fast_pipeline.py` (маркер SPEND_NIGHT_JOB_2026-06-27).
3 режима disk-guard (staging ≥ 35 GB / sequential 10–35 GB / skip < 10 GB).
Staging: единый FDW-скан → `_spend_staging_tmp` → 3 роллапа → DROP (1 проход FDW вместо 3).
Не влияет на golden Кудерко — `fact_big_analytics` не затрагивается. Лог: `/tmp/build_spend_daily.log`.
Зависимость: `build_star.py` читает `fact_region_spend` для `Dim_Location` / `Dim_Distance`, поэтому
основной pipeline / build_star запускать не раньше 09:30 UTC / 14:30 Екб в тот же день.

**PIPELINE_GUARD (2026-07-03, маркер `PIPELINE_GUARD_2026-07-03`):** guard в начале `main()` — сканирует процессы на `pipeline.py`/`fast_pipeline.py`/`pipeline_powerbi.py`. Если жив → ждёт ≤30 мин (опрос каждые 2 мин) → истёк → TG SKIP + `exit 0`. TG «стартовал» отправляется только после прохождения guard. Предотвращает параллельный запуск с основным пайплайном.

---

## Таблица: что в каком пайплайне

| # | Шаг / скрипт | pipeline_night | pipeline.py | fast_pipeline.py | Время |
|---|--------------|:---:|:---:|:---:|---|
|   | metrika_yandex (grants) | ✅ | ❌ перенесено | ❌ | ~3м |
| 0 | step0_sync_local | ❌ | ✅ | ✅ | |
|   | sync_pixel_config (фон) | ❌ | ✅ | ❌ | |
|   | step4_grid prefetch (фон) | ❌ | ✅ API | ❌ | 6м19с |
|   | step9_history prefetch (фон) | ❌ | ✅ API | ❌ | |
| 1 | step1_load_raw | ❌ | ✅ | ✅ | |
| 2 | step2_indexes | ❌ | ✅ | ✅ | |
| 3 | step3_build_sources | ❌ | ✅ | ✅ | |
|   | corrections.apply | ❌ | ✅ | ✅ | |
| 4 | step4 campaign_status | ❌ | ✅ API | ✅ UPDATE из cache | 6м19с / ~5с |
| 5 | step5_build_pixel | ❌ | ✅ | ✅ | |
| 6 | step6_build_full | ❌ | ✅ | ✅ | |
| 7 | step7_finalize | ❌ | ✅ | ✅ | |
| 9 | step9_direct_history | ❌ | ✅ | ❌ | |
| 10 | step10_crop_targeting | ❌ | ✅ | — | |
|   | load_reviews (перенос в full) | ❌ | ✅ | ✅ | |
|   | load_api_leads | ❌ | — | ✅ | |
|   | load_crop | ❌ | (внутри step10) | ✅ | |
|   | 404_errors | ✅ | ✅ инкрементально | ❌ | 1м06с |
| 13 | step13_utm_direct_audit | ✅ ~2ч | ❌ перенесено | ❌ | |
|   | korrektirovki | ✅ ~25м | ❌ перенесено | ❌ | |
|   | reviews (полный pipeline API) | ✅ ~5м | ❌ перенесено | ❌ | |
| 14 | step14_minus_snapshot | ✅ ~5-15м | ❌ | ❌ | Снапшот минус-фраз (campaign+groups+наборы) → yandex_direct_minus_snapshot. Блоки: MINUS_SNAPSHOT_BLOCKS |
|   | normalize_salons | ❌ | ✅ | ✅ | |
|   | fill_missing_regions | ❌ | ✅ | ✅ | |
|   | cleanup_old_dates | ❌ | ✅ | ❌ | |
|   | campaign_status_prefix | ❌ | ✅ | ✅ | ~30с |
|   | step11_pixel_score | ❌ | ✅ | ✅ | 10с |
|   | step12_proverka | ❌ | ✅ | ✅ | |
|   | crm_mappings_check | ❌ | ✅ | ✅ | |
| 8 | step8_stats | ❌ | ✅ | ✅ | |
|   | **build_spend_daily** (3 spend-витрины) | ❌ | ❌ SPEND_NIGHT_JOB | ❌ | cron 09:00 UTC |

**Логика разделения:**
- Ночью отрабатывают тяжёлые API-шаги (~2.5ч в сумме) — `metrika_yandex`, `step13_utm_direct_audit`, `korrektirovki`, полный `direct_account_reviews/pipeline.py`. Дневной `pipeline.py` их **не повторяет** — данные за сегодня уже свежие.
- `load_reviews_to_big_analytics.py` в `pipeline.py` ≠ `direct_account_reviews/pipeline.py` в ночном. Это разные скрипты: дневной `load_reviews` — только перенос строк из готовой таблицы `yandex_direct_account_reviews` в пересозданный (через CTAS) `big_analytics_full`, без обращения к API.
- `404_errors` — намеренно в обоих пайплайнах. Скрипт инкрементальный с 7-дневным перекрытием (см. Block L в `BLOCKS.md`).
- `step13_utm_direct_audit` использует **отдельный токен Метрики** `victorylotsofads04` (Phase 4 metrika_yandex.py раздаёт grants).
- campaign_status_prefix есть в обоих дневных — step6 пересоздаёт `big_analytics_full` через CTAS, префикс накладывается заново.
- step11_pixel_score — атрибуция `big_analytics_pixel` → `big_analytics_pixel_score` → доливка в `big_analytics_full` (`_source_table='пиксель_атрибуц'`). См. `step11_pixel_score/CLAUDE.md`.
  - **Важно:** step6 НЕ льёт `big_analytics_pixel` в `big_analytics_full` напрямую — атрибуцию делает только step11.
- step14_minus_snapshot — изолирован от big_analytics_full/unified/golden. Читает Директ API напрямую, пишет только в `yandex_direct_minus_snapshot`. Блоки задаются через `config/settings.py::MINUS_SNAPSHOT_BLOCKS`.

---

## step13_utm_direct_audit — двух-токенная схема Метрики

Токены:
- **primary** `METRIKA_TOKEN_AUDIT` = `victorylotsofads04@yandex.ru` — используется ТОЛЬКО в step13_utm_direct_audit. Квота 5000/сутки независимая.
- **fallback** `METRIKA_TOKEN` = `victorylotsofads1@yandex.ru` — main токен для metrika_yandex sync. Audit переключается на него если primary выжмется.

Доступ к счётчикам Метрики раздаётся в `metrika_yandex.py` Фаза 4:
- Кандидаты: counter_id где есть активные tp6/7/8 кампании за последние 30 дней (~230 счётчиков)
- Донор grant'ов: `victoryagency-direct1618440` (OAUTH_TOKEN_2) + 2 fallback донора
- Флаг в БД: `metrika_yandex.grant_done_lots04` BOOLEAN
- Идемпотентно: повторный grant возвращает 400 "already issued" → код считает success

Логика 429 в `step4_campaign_status/check_utm/utm_direct_audit.py:metrika_get()`:
1. Первый 429 на текущем токене → ждём `Retry-After`, retry
2. Второй 429 на текущем токене → переключение на следующий токен
3. Все токены выжаты → `_metrika_quota_exhausted=True`, Метрика пропускается до конца запуска

Результат `step13_utm_direct_audit/run.py` пишется в `public.check_utm` + `public.check_utm_fuck_direct`. По завершению Telegram-сводка (start/end/error).

---

## Запуск пайплайна (полная шпаргалка)

```bash
# Ночной пайплайн (cron, 03:00 МСК / 00:00 UTC):
# Шаги: metrika_yandex → step13_utm_audit (~2ч) → korrektirovki (~25м) → reviews (~5м) → 404_errors (~1м)
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 step_cron_night/pipeline_night.py"

# Дневной полный прогон (вручную) — пересборка big_analytics_* (~30-40 мин):
# Шаги 0,1,2,3,4,5,6,7,9,10 + corrections + load_reviews(перенос) + 404_errors(инкр.) +
# normalize/cleanup/prefix + step11/step12/crm_mappings_check + step8
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 pipeline.py"

# Отдельный шаг:
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 pipeline.py --only-step=3"

# Быстрый дневной (без API Директа, без step9 — кэш из последнего pipeline.py):
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 fast_pipeline.py"

# Отдельные ночные шаги — вручную (если нужно повторить):
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 -u step_cron_night/step13_utm_direct_audit/run.py"
ssh victory "cd ~/big_analytics_v5 && nohup ~/venv/bin/python3 step_cron_night/korrektirovki/run.py > /tmp/korrektirovki.log 2>&1 &"
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 step_cron_night/direct_account_reviews/pipeline.py"
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 step_cron_night/metrika_yandex.py"

# Spend-витрины — вручную (обычно cron 09:00 UTC / 14:00 Екб):
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 step_cron_night/build_spend_daily.py"

# Фидовая воронка — вручную (при новых фидах: сначала обогатить URLs):
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 -m direct_feed_funnel.fetch_feed_urls_cookie --all-logins --apply && ~/venv/bin/python3 -m direct_feed_funnel.pipeline"

# Посевы — вручную:
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 step10_crop_targeting/pipeline.py"
```

<a id="steps-map"></a>
### Шаги пайплайна (карта «шаг → папка») — ЕДИНЫЙ ИСТОЧНИК

> Это **единственная полная таблица** шагов 0–13 в проекте. Остальные файлы
> (`CLAUDE.md`, `README.md`, `PROJECT_CHARTER.md`, `PLAN.md`) ссылаются сюда якорем
> `PIPELINES.md#steps-map`. `CLAUDE.md` держит свою сжатую копию только для быстрой навигации.

| Шаг | Папка | Описание | Время |
|-----|-------|----------|-------|
| 0 | `step0_sync_local/` | FDW → локальные копии (UPSERT / TRUNCATE+INSERT) | ~2–5 мин |
| 1 | `step1_load_raw/` | Локальные копии → RAW UNLOGGED таблицы | ~30–60 сек |
| 2 | `step2_indexes/` | Индексы + ANALYZE на RAW | ~1–2 мин |
| 3 | `step3_build_sources/` | Сборка direct/seo/pixel/telegram/reviews/crop | ~3–8 мин |
| 4 | `step4_campaign_status/` | campaign_status из Директ (Grid API, куки) → campaign_status | ~2–5 мин |
| 5 | `step5_build_pixel/` | Конфиг пикселей → big_analytics_pixel | <1 мин |
| 6 | `step6_build_full/` | UNION ALL → big_analytics_full + постобработка | ~1 мин |
| 7 | `step7_finalize/` | SET LOGGED + VACUUM ANALYZE + индексы | ~2–4 мин |
| 8 | `step8_stats/` | Статистика + финальный Telegram-отчёт (выполняется последним) | ~30 сек |
| 9 | `step9_direct_history/` | История изменений Директа → yandex_direct_history (фон с шага 0) | ~30–60 мин |
| 10 | `step10_crop_targeting/` | Посевы → big_analytics_crop_targeting | ~2–5 мин |
| 11 | `step11_pixel_score/` | Атрибуция пикселей → big_analytics_pixel_score + доливка в full | ~10 сек |
| 12 | `step12_proverka_big_analytics/` | Проверка CRM-маппингов → Telegram-отчёт | ~30 сек |
| 13 | `step13_arrival/` | Воронка по дате визита → big_analytics_full_arrival | ~1–2 мин |

> ⚠️ **`step13_utm_direct_audit`** живёт в `step_cron_night/step13_utm_direct_audit/` и
> запускается ночным `pipeline_night.py` (а не дневным `pipeline.py`). В корне шаг 13 = `step13_arrival`
> (см. `STEPS` в `pipeline.py`). Папок `crop_targeting/` (без префикса `step10_`) и корневого
> `step13_utm_direct_audit/` **не существует**. Номера шагов 6–9 намеренно сохранены.

### Post-loop этапы (после step11, в `pipeline.py` / `fast_pipeline.py`)

После основного цикла шагов идёт хвост финализации. Порядок: step11 (доливка пикселя в
`big_analytics_full`) → нормализация/cleanup/prefix/compactify full → **step13_rebuild**
(пересборка `big_analytics_full_arrival` с пикселем) → normalize_salons(arrival) →
**build_unified** → **датамарты расхода** → **build_star**.

| Этап | Модуль | Выход | Что делает |
|------|--------|-------|------------|
| `build_unified` | `step13_arrival/build_unified.py` | `big_analytics_unified` (TABLE) | UNION `big_analytics_full` (ось «По дате заявки») + `big_analytics_full_arrival` (ось «По дате визита») c колонкой **`атрибуция`**. PBI читает unified как партицию `big_analytics_full` — без этого шага свежие данные прогона не попадают в Power BI. Идемпотентно (DROP+CTAS, ~2 мин). Между step13_rebuild и build_star. |
| `build_region_spend` | `region_spend/build_region_spend.py` | `public.fact_region_spend` | Датамарт «расход по региону показа» из `yandex_direct_manager_reports`. golden Кудерко НЕ затрагивает (отдельная таблица). DROP+CTAS. **Запускается из `build_spend_daily.py` (cron 09:00 UTC), не из pipeline.py / fast_pipeline.py.** |
| `build_adformat_spend` | `adformat_spend/build_adformat_spend.py` | `public.fact_adformat_spend` | Датамарт «расход по формату объявления». DROP+CTAS, golden НЕ затрагивает. **Запускается из `build_spend_daily.py`.** |
| `build_criterion_spend` | `criterion_spend/build_criterion_spend.py` | `public.fact_criterion_spend` | Датамарт «расход по критерию». DROP+CTAS, golden НЕ затрагивает. **Запускается из `build_spend_daily.py`.** |
| `build_star` | `star_refactor/build_star.py` | `public.fact_big_analytics`, `public.arp_fact`, `public.fact_vk_ads` | Лёгкий факт (проекция unified) + ARP-view + **датамарт VK Ads** (`build_vk_ads_fact`, сегмент×оффер×объявление воронка, после `build_arp_fact`; golden НЕ затрагивает). Отдельный subprocess. PBI Этапа 3 репойнтит партиции на лёгкий факт (×6.7). |

> 3 датамарта расхода (`fact_*_spend`) — независимые витрины по грани локации/формата/критерия,
> в `big_analytics_full`/golden НЕ входят. Реплицируются на localhost-Mac через
> `copy_pbi_tables_to_localhost.py` (см. `PBI_TABLES.md`).
> **SPEND_NIGHT_JOB_2026-06-27:** spend-билдеры вынесены из `pipeline.py` / `fast_pipeline.py`
> в отдельный `step_cron_night/build_spend_daily.py` (cron 09:00 UTC / 14:00 Екб).
> `pipeline.py` / `fast_pipeline.py` spend-витрины больше не строят и не помечают DEGRADED
> из-за них — `build_spend_daily.py` самостоятельно логирует OK/FAIL и отправляет Telegram.
> Staging-режим (`spend/build_spend_staging.py`): единый FDW-скан → `_spend_staging_tmp`
> (UNLOGGED, создаётся и дропается внутри job'а, в списке постоянных таблиц нет).

### Итоговая таблица времён прогона

В конце каждого прогона `pipeline.py` печатает сводку по шагам:

```
============================================================
Шаг  Название                       Секунды  Статус
------------------------------------------------------------
0    step0_sync_local                  142.3 с  OK
1    step1_load_raw                     38.1 с  OK
2    step2_indexes                      71.5 с  OK
3    step3_build_sources               312.0 с  OK
4    step4_campaign_status             184.2 с  OK
5    step5_check_utm                   210.7 с  OK
6    step6_build_full                   55.4 с  OK
7    step7_finalize                    148.3 с  OK
8    step8_stats                        12.1 с  OK
============================================================
УСПЕШНО завершено за 1174 сек (run_id=067badcd)
```

Каждый шаг также записывает `duration_sec` в `data_quality_log`.

Если основной факт собран, но одна из spend-витрин не обновилась, финальный статус будет
`DEGRADED`, а не чистый success. Exit code остаётся `0`: это не блокирует основной
`fact_big_analytics`, но требует ручной проверки/перезапуска соответствующего
`build_region_spend`, `build_adformat_spend` или `build_criterion_spend`.

---

## run_id

Каждый запуск получает `run_id` — 8-символьный UUID. Все записи в `data_quality_log` привязаны к нему.

```sql
-- Смотреть последние запуски:
SELECT run_id, run_at, step, status, rows_affected, duration_sec, details
FROM data_quality_log
ORDER BY run_at DESC
LIMIT 50;
```

## Возобновление после ошибки

Если упал на шаге 3:

```bash
python pipeline.py --from-step=3
```

Шаги 0–2 не повторяются. RAW-таблицы уже созданы и проиндексированы.

Доступные флаги запуска:

```bash
python pipeline.py                # полный прогон (шаги 0–9 + доп. скрипты)
python pipeline.py --from-step=3  # продолжить с шага 3
python pipeline.py --only-step=0  # только один шаг
```

## Куки (cookies.json)

Шаги 4 (campaign_status), 5 (UTM-аудит) и 9 (direct_history) используют куки Яндекс аккаунтов.

Файл `cookies.json` в корне проекта читается **в первую очередь** (перед HTTP-запросом к домашнему серверу).
Формат: `{"login1": "<строка куки>", "login2": "..."}`.

Обновить файл на сервере:
```bash
scp cookies.json victory:~/big_analytics_v5/cookies.json
```

---

## Структура проекта

```
big_analytics_v5/
├── pipeline.py          ← точка входа
├── cookies.json         ← куки аккаунтов Яндекс (обновлять вручную)
├── PIPELINES.md         ← этот файл (пайплайны, расписание, карта шагов)
├── PLAN.md              ← архитектура и решения
├── DB_TABLES.md         ← схема всех таблиц
├── config/
│   ├── settings.py      ← константы, имена таблиц
│   ├── db.py            ← пул соединений
│   ├── tokens.py        ← API-ключи, Telegram бот
│   ├── brand_map.py     ← ct-коды → марки авто
│   └── status_sql.py    ← динамическая генерация SQL квалификации лидов из local_crm_statuses
├── step0_sync_local/    ← шаг 0
├── step1_load_raw/      ← шаг 1
├── step2_indexes/       ← шаг 2
├── step3_build_sources/ ← шаг 3
├── step4_campaign_status/  ← шаг 4
├── step5_build_pixel/   ← шаг 5
├── step6_build_full/    ← шаг 6
├── step7_finalize/      ← шаг 7
├── step8_stats/         ← шаг 8
├── step9_direct_history/ ← шаг 9
├── step10_crop_targeting/ ← шаг 10 (посевы)
├── step11_pixel_score/  ← шаг 11 (атрибуция пикселя)
├── step12_proverka_big_analytics/ ← шаг 12 (проверки CRM-маппингов)
├── step13_arrival/      ← шаг 13 (воронка по дате визита → big_analytics_full_arrival)
├── step_cron_night/     ← НОЧНОЙ пайплайн (pipeline_night.py, cron 03:00 МСК) +
│                            build_spend_daily.py (cron 09:00 UTC / 14:00 Екб):
│                            step13_utm_direct_audit, korrektirovki, 404_errors,
│                            report_placement, reviews, metrika_yandex, build_spend_daily
├── direct_feed_funnel/  ← Воронка по фидам Директа (pipeline.py, build_keyed.py,
│                            fetch_feed_urls_cookie.py, build.py-experimental)
│                            → fact_direct_feed_funnel, direct_feed_spend_keyed, direct_feed_leads_keyed
├── spend/               ← Staging-модуль для единого FDW-скана spend-витрин
│                            build_spend_staging.py → _spend_staging_tmp (UNLOGGED, временная)
├── data_check/          ← подсистема проверок качества (run.py + checks/*) — см. data_check/README.md
├── sales_attribution/   ← analytics_sales_attribution для PBI (build.py / verify.py)
├── crm_mappings_check/  ← проверка целостности local_crm_statuses (check.py)
├── star_refactor/       ← звезда (build_star.py / verify_star.py; объекты в схеме public — star консолидирована)
├── sql/                 ← вспомогательные SQL-вьюхи (v_monthly_kpi_avto.sql)
└── config/              ← settings.py / db.py / tokens.py / status_sql.py / brand_map.py
```

---

## pipeline_monday.py — удалён (2026-05-15)

Заменён ночным `step_cron_night/pipeline_night.py` (cron 03:00 МСК).

Ранее `pipeline_monday.py` запускал последовательно `pipeline.py` + `korrektirovki` + `direct_account_reviews` + `crop_targeting` + `step13_utm_direct_audit`. После того как `korrektirovki`, `direct_account_reviews/pipeline.py`, `step13_utm_direct_audit` и `metrika_yandex` стали частью ночного `pipeline_night.py` — содержимое monday-пайплайна осталось без работы.

Если нужно вручную прогнать всё ночное днём — запустить `step_cron_night/pipeline_night.py` напрямую.

---

## Переименованные таблицы (апрель 2026)

| Старое имя | Новое имя |
|-----------|-----------|
| `public.direct_history` | `public.yandex_direct_history` |
| `public.direct_account_reviews` | `public.yandex_direct_account_reviews` |
| `public.korrektirovki` | `public.yandex_direct_korrektirovki` |

`T_DIRECT_HISTORY` в `config/settings.py` обновлён на `'yandex_direct_history'`.

---

## UTM-аудит: квота Метрики (лимит по IP)

Квота `/stat/v1/data/`: **200 запросов / 5 минут** с одного IP + **5000 запросов / сутки**.
Сброс суточного лимита: 00:00 GMT (03:00 МСК).

Поведение при 429:
- Первый 429: ждём полный `Retry-After` (до 90с), одна повторная попытка
- Второй 429 подряд: устанавливается флаг `_metrika_quota_exhausted = True` → все Метрика-запросы в текущем запуске пропускаются мгновенно

**step13_utm_direct_audit запускать не раньше 03:00 МСК** если днём уже запускался.
