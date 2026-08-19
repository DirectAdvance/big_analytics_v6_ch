# big_analytics_v6_ch — статус

_2026-08-18, после перевязки части Power BI на `*_star`/`bi_*`, push BA6/PBIP и live-обновления
direct-cookie PBI views. История — `git log -p STATE.md`._

## Где мы сейчас

Прогон 34 минуты, `ad_analytics` 2.06 ГиБ. `verify_big_analytics.py` = **PASS**
(golden Кудерко 25 642 434.57, Δ+219 660.57 игнорируется из-за `KUDERKO_RAW_INCOMPLETE`;
продажи 57 при floor 54). Факт свежий: `fact_big_analytics` = 5 274 040 строк,
максимальная `Date` = 2026-08-16.
Оптимизация и фикс двойного счёта пикселя **закоммичены** (`3c0c726`, `c0fd79c`).
2026-08-17: `Dim_Date.year_month` переведён с `YYYY-MM` на русские названия месяцев; `month_key`
остаётся числовым `YYYYMM` для сортировки. Код доставлен на Victory и `Dim_Date` пересобран.
2026-08-17: ночной step 102 `check_utm` переписан на `raw_data` Direct и вручную пересобран:
`check_utm` = 28 288 строк, `check_utm_fuck_direct` = 3 981; `bi_check_utm_fuck_direct` больше не
пустая заглушка.
2026-08-17: `yandex_direct_checking_report/report.py` переведён с Direct Reports API на
`raw_data.yandex_direct_report_rows.total_cost` (расход с НДС и комиссией). PostgreSQL-таблица
`public.yandex_direct_checking_report` перезалита: 865 строк, 261 аккаунт, 651 319 874.51 ₽.
2026-08-17: `step_cron_night/pipeline_night.py` поставлен в cron Victory на `10 18 * * *` UTC
(23:10 Екб) с `/tmp/ba6_night.lock`; проверочный ручной прогон PASS за 14м15с.
2026-08-17: `yandex_direct_return_commission_report` выведен из BA5/BA6 PBI-контрактов.
`PBI_EMPTY_ALLOWED` и `PBI_EMPTY_BY_DESIGN` в BA6 теперь пустые: любой активный пустой `bi_*` = FAIL.
2026-08-17: `Пиксель_атрибуц` выведен из BA5/BA6 контрактов. Канон: BA6 `_source_table='pixel'`,
BA5 `_source_table='пиксель'`; `pixel_score` остаётся score/diagnostics, full получает прямой пиксель.
2026-08-17: BA6 runtime задеплоен на Victory (md5 + remote py_compile), полный pipeline завершился
`big_analytics_v6_ch pipeline OK`. BA6 PBIP очищен от `return_commission`; live ClickHouse-вьюхи
`ad_analytics.yandex_direct_return_commission_report` и
`ad_analytics.bi_yandex_direct_return_commission_report` удалены. Post-drop verify — PASS.
2026-08-17: исправлен BA5-паритет источников для BA6: обычные `calls` снова
классифицируются как `Контекст`/`SEO`, crop-лиды получают BA5-типы
`telegram`/`social_посевы`/`vk_ads`/`vk_zero`/`vk_perform`, чтобы расход step10 overlay и
воронка агрегировались в одном источнике. Код доставлен на Victory; пересборка `--from-step=3`
дошла до `step146`, затем хвост перезапущен с `--from-step=146` и завершился
`verify_big_analytics.py` **PASS**. Живая сверка: `calls/Звонки` = 0; `calls` =
`Контекст` 60 284, `SEO` 3 394, `Посевы_Звонки` 2 092; crop `источник='Посевы'` = 0.
2026-08-17: в `raw_data` появились Direct cookie-источники
`direct_cookie_ads_texts_master`, `direct_cookie_type_placement_master`,
`direct_cookie_feed_urls`. `ads_texts` и `type_placement` уже переключены через совместимые
`bi_*`-вьюхи; не покрыты `raw_new_arp_fact`, `raw_new_search_query_report_master_pbi`,
`raw_new_human_cyborgs`.
На момент проверки новые таблицы уже растут: ads-texts >16M строк, type-placement >2.6M,
feed-url 5 923 строки; зерно дневное (`scope_from = scope_to`).
2026-08-17: оптимизированы безопасные горячие батчи: `step10` overlay full-copy и `step146`
`pbi_import_fact_direct_feed_funnel` переведены с принудительных дневных окон на общий
`day_ranges()` (`PIPELINE_BATCH_DAYS=7`, месяц не пересекается, откат `PIPELINE_BATCH_DAYS=1`).
Live probe на недельном окне прошёл без memory error. `step11`, `step140`, `step3` уже были
на `day_ranges()`; там исправлены только misleading log labels.
2026-08-17: оптимизирован `step145 build_star`: `Dim_Campaign` и merge `Dim_AdGroup` больше
не сканируют данные 64 бакетами. Live probe: `Dim_Campaign` 1 бакет 6.21 сек против 22.08
на 8 и 57.97 на 16; `Dim_AdGroup` merge 1 бакет 8.27 сек против 21.10/22.17. Временные
probe-таблицы удалены. В `build_star` добавлены подтайминги по измерениям и крупным фактам
для следующего full pipeline.
2026-08-17: оптимизации и BA5-паритет источников доставлены на Victory и проверены живыми
прогонами. `--from-step=3` (`run_id=3774d63b3312`) завершился PASS: step3 661.2с, step10 93.1с,
step11 150.4с, step140 150.5с, build_star 177.5с, build_pbi_compat 167.0с. Дополнительно найден
и исправлен остаток старой классификации на визитной оси: `step13_arrival` больше не ставит
`источник='Звонки'` для calls, а использует `Контекст`/`SEO`/`Посевы_Звонки` как step6.
Хвост `--from-step=13` (`run_id=f87cc8e52cea`) завершился PASS; live-сверка:
`big_analytics_full` bad-source tuple `(calls/Звонки, crop/Посевы, null_source, before_2026)` =
`(0,0,0,0)`, `fact_big_analytics` по источнику `Звонки` = 0, `Dim_Source` = 30.
Остаточные v5↔v6 дельты Feb-Jul без пикселя остаются data/source parity: cost +3.84 млн ₽
(+0.365%), обращения +4 450 (+1.52%), korr +5 207 (+3.52%), kval −1 238 (−2.79%),
приезды −18, продажи +2. Самые большие очаги: `Контекст` +3.61 млн ₽ и +5 992 обращений,
`Посевы_Звонки` −1 844 обращения, `SEO` +1 212 обращений.

Сегодняшний замер: [`RAW_DIFF_FINDINGS.md`](RAW_DIFF_FINDINGS.md) — сырьё,
[`PBI_TABLES.md`](PBI_TABLES.md) §0 — паритет 31 таблицы Power BI.

2026-08-18: локальный BA6 git-хвост закрыт: все 12 коммитов по БА6 проверены по составу и
запушены в `origin/main`, затем добавлены и запушены `e8dd18f`/`b92f400` для direct-cookie PBI.
`build_pbi_compat.py` доставлен на Victory (md5 `f830b0ee61143ae3646a7a00b5d128f1`, remote
`py_compile` OK). Live ClickHouse-вьюхи `bi_yandex_direct_ads_texts` и
`bi_yandex_direct_type_placement_report_master` пересозданы: обе агрегируют данные в ClickHouse,
`type_placement_ru` больше не зависит от `raw_new_type_placement_types`.
BA6 PBIP `powerbi_ba6` запушен коммитом `2569252`: `yandex_direct_ads_texts` и
`yandex_direct_type_placement_report_master` читают `bi_*`, `return_commission` и его визуалы
удалены. Desktop/Service refresh после этого не запускался.
2026-08-18: star-разнос `yandex_direct_ads_texts` подготовлен и задеплоен на Victory:
факт теперь `loaded_at/client/campaign/ad_group/banner_id + metrics`, тексты вынесены в
`bi_Dim_AdText`. `pipeline.py --only-step=146` завершился OK за 166.9с; live rows:
`bi_yandex_direct_ads_texts` 16 081 658, `bi_Dim_AdText` 1 025 253. PBIP локально обновлён,
но Desktop/Service refresh и публикация датасета ещё не запускались.
2026-08-19: исправлен Power BI overflow на BA6 hidden keys: `bi_fact_criterion_spend_star`,
`bi_Dim_Criterion`/`bi_dim_criterion`, `bi_Dim_Site`, `bi_fact_region_spend_star` и
`bi_fact_direct_feed_funnel_star` отдают `criterion_key`/`site_key` как signed-safe `Int64`.
Live-вьюхи пересозданы точечно; overflow=0, уникальность `Dim_Criterion`/`Dim_Site` сохранена.
`pytest` 168 passed, `verify_big_analytics.py` PASS. Desktop refresh ещё не запускался.
2026-08-19: Desktop refresh показал, что прежний signed-safe вариант через
`key % 9223372036854775807` всё равно даёт modulo-коллизию:
`dim_criterion.criterion_key=3943490909` повторяется на стороне связи Power BI. Локальный код
переведён на bijective `reinterpretAsInt64(UInt64)`; до деплоя и пересоздания live `bi_*` это
исправление не действует на Victory.

## Главное, что выяснилось 15.08

**Сырьё v6 не беднее v5 — оно богаче.** Директ +354 258 строк и +10.54 млн ₽; 99 «пропавших»
аккаунтов несут 0.00 ₽. CRM-лиды больше v5 в каждом месяце 2026. Витрина сошлась: заявочная ось
Feb–Jul без пикселя — cost +0.36%, приезды −0.08%, продажи −0.03%.

**Отчёты Power BI на v6 ещё не финализированы.** После 18.08 `ads_texts` и
`type_placement_report_master` технически закрыты через `bi_*` над `raw_data.direct_cookie_*`.
Остаются `analytics_report_placement/criterion/feed` (+ `placement_links`),
`accounts_human_cyborgs`, а также временные `raw_new_arp_fact`,
`raw_new_search_query_report_master_pbi`, `raw_new_human_cyborgs` в PBIP. Плюс
`fact_direct_feed_funnel` в v6 — это площадки РСЯ, а не воронка по фидам (имя совпадает, смысл
другой). Подробности — `PBI_TABLES.md` §0.

## Не сделано / ждёт решения Семёна

1. **Чужие изменения в git-индексе** от другой сессии: `.sqlfluff`,
   `migrations/01_init_schema.sql`, `sql/create_pbi_big_analytics_full_view.sql`,
   `sql/v_monthly_kpi_avto.sql`. В наши коммиты не брать.
2. **Передаточный документ готов, но не отправлен** владельцу `raw_data`:
   `docs/DIRECT_RAW_HANDOVER.md` — 12 потоков, что мы тянем из Директа руками. Шесть блокируют
   перевод отчётов на v6. ⚠️ Половина потоков идёт через Grid **на куках** — без инфраструктуры
   кук они остаются у нас, проговорить сразу.
3. **Фаза 3 `OPTIMIZATION_PLAN.md` частично сделана** — PBIP перевязан на
   `fact_direct_feed_funnel_star`, `fact_region_spend_star`, `fact_criterion_spend_star` и
   direct-cookie `bi_*`. Остался ручной шаг: открыть Power BI Desktop/Service, выполнить полный
   refresh и опубликовать датасет.
4. **Опубликовать BA6 PBIP в Power BI Service.** Локальный PBIP очищен, но cloud-service dataset
   не публиковался и не проверялся через API.
5. **`analytics_report_placement` не перевязан.** Проверенный кандидат через
   `fact_direct_feed_funnel` не совпал с `raw_new_arp_fact` на периоде 2026-07-01..2026-08-13:
   2.53M строк / 315.7M cost против 1.91M / 179.6M. Нужен отдельный ARP-источник или решение
   менять смысл вкладки.
## Что важно знать, чтобы не наступить

- **Свежесть v6 нельзя проверять по `raw_data.etl_runs` и `leads_all.updated_at`** — оба врут
  (#43). Смотреть даты в самих таблицах.
- **Окно батча не должно пересекать границу месяца.** `PIPELINE_BATCH_DAYS=7`, `range_batches`
  прибивает край к первому числу. `step11:63` считает веса пикселя за месяц от первого дня окна,
  а дни тянет за всё окно — на пересекающих окнах 11% пиксельной оси падало в `CampaignId=0`
  при сохранных тоталах (golden молчал). Аварийный откат: `PIPELINE_BATCH_DAYS=1`.
- **`big_analytics_pixel_score` — физическая таблица, её нельзя удалять по дороге.** Исключена
  из `FACT_SWAP_COMPAT_OBJECTS`, `cleanup_wide_intermediates` и `WIDE_COMPAT_VIEWS`. Пропустить
  одно из трёх мест = `UNKNOWN_TABLE` в `build_pbi_compat` — так сгорели два прогона 15.08.
- **`--from-step=` выше step3 после cleanup опасен, если шаг читает `big_analytics_sources`**:
  шаг 148 штатно заменяет wide-таблицы view и удаляет часть промежуточного source-слоя; step10/11
  могут упасть на `UNKNOWN_TABLE`. Хвост от step13 доказанно работает (`run_id=f87cc8e52cea`),
  потому что читает `big_analytics_full`/arrival/star, а не `big_analytics_sources`.
- **v6 не отвязан от PostgreSQL полностью**: step3 (`_fetch_reviews_rows_from_postgres`) ходит
  на Victory PG за отзывами, потому что их нет в `raw_data`.
- **Инстанс ClickHouse — 2 vCPU / 8.33 ГБ**, серверный потолок 7.49 ГБ, запрос ограничен 2 ГБ
  (`SAFE_QUERY_SETTINGS`), `max_threads=2`. На месячных окнах step3 падает по памяти — неделя
  это потолок ширины.
- **Дневной прогон в кроне с 16.08:** `0 2 * * *` UTC = 07:00 Екб, через обёртку `cron_run.py`
  (сам `pipeline.py` в Telegram не пишет ничего). **Ночной прогон в кроне с 17.08:**
  `10 18 * * *` UTC = 23:10 Екб, через `step_cron_night/pipeline_night.py`.
- **Код на Victory сверен с HEAD 16.08: 143/143 совпали.** Ничто не синкает его туда автоматически
  (Mutagen ходит на LXC 101) — дрейф копится молча: Victory отставал на три ETL-коммита от 13–14.08
  (`status_sql`, `corrections`, `step6`). Сверка md5 — `RUNBOOK.md` §3a, гонять перед доверием к прогону.
- Golden-дельта по Кудерко — не новая, root-cause #37 (неполное сырьё, 29/67 логинов).
  В текущем прогоне Δ+219 660.57 ₽ переведена verify-гейтом в warning и не валит PASS.

## Открытые дефекты

`KNOWN_ISSUES.md`: к прежним добавлены **#39** (часть PBI ещё на `raw_new_*`), **#40** (FIXED:
whitelist пустоты убран), **#41** (`big_analytics_reviews` = 0 из-за рассинхрона тега),
**#42** (минус-фразы в night cron, 30-дневная история ещё наполняется),
**#43** (врущие индикаторы свежести),
**#44** (`domains` −322, `gsheet_sites` −52, `crm_status_mapping` −5).
Из прежних актуальны #37 (бэкфил Кудерко) и #38 (`data_check/compare` не работает после star).
