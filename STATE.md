# big_analytics_v6_ch — статус

2026-08-27 13:15 Екб: закрыт баг визитной атрибуции и развязаны фиды/РСЯ-площадки. Root cause
по визитам: `step13_arrival._auto_domains_filter` пропускал только домены из `gsheet_sites`
с `direction='Авто'`, а заявочная ось уже содержит авто-домены без строки в справочнике; из-за
этого `По дате визита` терял 981 direct-продажу только на orphan-доменах. Теперь step13 также
допускает домены из уже собранного `big_analytics_full`, кроме `направление='Перформ'`.
Root cause падения финального gate после фидов: `bi_analytics_report_placement` продолжал читать
`fact_direct_feed_funnel`, который переведён на реальные фиды; ARP снова читает РСЯ-площадки из
`raw_data.yandex_direct_report_rows`, а фиды остаются в `fact_direct_feed_funnel`.
Деплой на Victory: md5 Mac==Victory, remote `py_compile` OK, точечные pytest OK. Прогон
`pipeline.py --from-step=3` пересобрал слои до verify, затем ожидаемо упал на ARP gate до hotfix;
после hotfix `pipeline.py --only-step=146` = OK и `data_check/verify_big_analytics.py` = PASS.
Live-check: `fact_big_analytics` 5,481,576 строк; продажи YTD `По дате визита` 4544.995563 >
`По дате заявки` 4191; `big_analytics_full_arrival` 123,186 строк; фиды 1,226,350 строк,
TP8/TP9/TP10 markers=0; `bi_analytics_report_placement` liveness 181,526/348,570 = 0.521.
Power BI refresh НЕ запускался.

2026-08-27 01:20 Екб: исправлен VK Ads cost-overlay в step10. До правки строки расхода VK Ads в
`pbi_big_analytics_full` теряли `domain/салон/регион/специалист` и весь расход попадал в
`без домена`, хотя `fact_vk_ads` уже был разложен по доменам. Теперь overlay берёт site dimensions
через `reference_data.vk_ads_agency_clients -> reference_data.gsheet_sites` только для `niche='Авто'`.
Также исправлена идемпотентность `_overlay_full`: перед повторной вставкой удаляются все старые
`cascade_level='cost_overlay'`, включая исторические overlay-строки без префикса в `key3`.
Live ClickHouse после пересборки derived-слоя: `bi_pbi_big_analytics_full` для VK Ads имеет
ненулевой расход в пустом домене = 0; `cost_overlay` = 2 420 строк / 21 523 099,18 ₽;
`verify_big_analytics.py --full` = PASS. Power BI refresh НЕ запускался по команде Семёна.

2026-08-25 18:05: подготовка ко второму прогону доведена до локального ready-state, сам pipeline
НЕ запускался. Исправлен текущий PBI/star стоппер: `pbi_big_analytics_full` теперь берёт
`специалист` из факта (`f.\`специалист\``), а не из бездатного `Dim_Site`; `bi_fact_region_spend_star`
возвращён к контракту "ключи+метрики" без `domain/updated_at`. Тестовые контракты Power BI refresh
обновлены под опубликованную модель: `yandex_direct_ads_texts` уже входит в selective refresh.
Проверки: `python3 -m pytest tests/ -q` = 244 passed / 1 skipped; `py_compile` по изменённым
runtime-файлам OK; `spend/dated_site_join.py` self-check OK. Live `verify_big_analytics.py`
сейчас падает только на ожидаемом stale-снимке `full_last_day_incomplete=1`: `ad_analytics.raw_yandex`
за 2026-08-24 содержит 2 060 878,89 ₽ / 32 932 строк / 71 логин, потому что первый прогон взял
недолитое сырьё. Первичная `raw_data.yandex_direct_report_rows` уже долилась: 2026-08-24 =
6 847 323,95 ₽ / 109 866 строк / 268 логинов, `updated_at` до 2026-08-25T12:23:27Z. Ожидание:
второй полный прогон должен пересобрать `raw_yandex` из `raw_data`, поднять последний день до
нормального уровня и снять `full_last_day_incomplete`; Кудерко остаётся warning по known issue #37,
не FAIL. Не сделано: Victory deploy, второй pipeline, Power BI refresh после второго pipeline.

2026-08-25: локально подготовлены фиксы перед деплоем/пересборкой, но деплой и pipeline НЕ
запускались. КО/добро: в `step3_build_sources/step3.py` dead `lower()` на кириллице заменён на
`lowerUTF8()` для reason/status matching, чтобы причины вроде `Был в КСО` матчились стабильно.
Расходы директологов: spend-факты (`fact_region_spend`, `fact_criterion_spend`,
`fact_adformat_spend`) теперь несут собственный `специалист`, рассчитанный через датированный
`gsheet_sites` match `(login,date)` + общие account/date corrections; PBI-слой больше не должен
тянуть директолога расходов из бездатного `Dim_Site`. SELECT-прогноз по
`raw_data.yandex_direct_report_rows` после барьеров: Кудерко-логины уходят на Терехова
10 227 077,58 ₽ и Тумашенко 3 045 793,77 ₽; Сергеев-логины — на Караваева 6 840 147,24 ₽,
Зубакина 3 426 900,72 ₽, Гордееву 99 099,41 ₽; Питеркина — 0 ₽ после 2026-06-19;
Чепелев-логины — на Караваева 2 000 011,37 ₽, Щербакову 320 209,38 ₽, Крючкову 238 376,19 ₽,
Терехова 109 609,36 ₽, Тумашенко 106 740,20 ₽. Остаток на старых владельцах после барьера:
Кудерко 507,89 ₽ за 2026-06-18 (`buauto54.ru`) и Чепелев 2 078,25 ₽ за 2026-07-17
(`tenet-park-msk.ru`) — это текущий владелец в `gsheet_sites` на дату строки, не срабатывание
старого правила после барьера. Проверки: `py_compile` по изменённым Python-файлам OK,
точечный pytest по KO/lowerUTF8 и spend-specialist OK, `spend/dated_site_join.py` self-check OK,
PBIP JSON 6293/6293 валидны. Не сделано: полный pipeline, Victory deploy, Power BI refresh;
`full_last_day_incomplete` оставлен отдельно как недолив сырья.

2026-08-24: BA6/PBIP audit по таблицам Power BI закрыт и проверен refresh. `refresh_powerbi.py` расширен с
25 до 40 import-таблиц: теперь `_ALL_TABLES` совпадает со всеми импортными таблицами текущей
admin semantic model; calculated/DAX остались только `Dim_Distance` и `Модель атрибуции`.
Служебная `Users` удалена из PBIP semantic model как неиспользуемая. Старое UI-слово
`Я.Директ_фиды`/`Фиды` заменено на `Я.Директ_площадки РСЯ`/`Площадки РСЯ`; внутреннее имя
`fact_direct_feed_funnel` не переименовывалось, потому что это compatibility layer. Проверки:
`py_compile refresh_powerbi.py` OK, JSON PBIP OK, `Users`/`Фиды` grep = 0, импортные таблицы
модели 40 == refresh-таблицы 40. Деплой BA6 на Victory: `refresh_powerbi.py`/`PBI_TABLES.md`/
`STATE.md` доставлены через scp, md5 Mac==Victory, remote `py_compile refresh_powerbi.py` OK.
Первый запуск refresh на Victory не дошёл до Power BI из-за DNS `api.powerbi.com` SERVFAIL; тот же
refresh запущен локально с Мака, BA6 datasource подтверждён (`extension`), selective refresh 40
таблиц завершился `Completed` в 2026-08-24 21:48:52 Екб. Не сделано: публикация PBIP в Power BI
Service — в этом репозитории она по README делается вручную через Power BI Desktop.

2026-08-24: BA6 коммит `d47bdb7` (`fix: ограничить BI справочники auto нишей`) доставлен на
Victory вручную по scp в `~/big_analytics_v6_ch`; md5 Mac==Victory по 7 файлам, remote
`py_compile` OK. Прогон `pipeline.py --from-step=3` (`run_id=8fb8ba739a07`) завершился
`pipeline OK`, `verify_big_analytics.py` = PASS; быстрый `--from-step=6` перед этим упал до
мутаций на ожидаемом отсутствии `ad_analytics.big_analytics_sources`, поэтому был заменён на
`--from-step=3`. Итоговые live-объёмы: `big_analytics_full=5,323,227`,
`fact_big_analytics=5,458,433`, `pbi_big_analytics_full=5,458,433`, `Dim_Site=4,673`,
`bi_analytics_report_placement=13,234,048`. Проверка проблемы: `2line`, `5 звезд`,
`«РЖД Медицина»` отсутствуют в `Dim_Site`/`bi_Dim_Site`/`Dim_Salon`/`bi_Dim_Salon`/
`bi_analytics_report_placement`; non-auto домены из `reference_data.gsheet_sites` в
`Dim_Site`/`bi_Dim_Site` и placement = 0. Кудерко по `По дате заявки` июнь/июль:
продажи 0; `probeg-cars.ru` июнь и `autocenter93.ru` июль ушли на `Контекст` по 1 продаже.
Остаток для отдельного решения: 4 account-only логина с `niche='None'` остаются в
`fact_big_analytics`/`bi_Dim_Campaign` без домена/салона (1,708 строк, 2 продажи) — это не
источник салонного среза и не вырезалось, чтобы не orphan-ить fact без отдельного правила.

2026-08-24 (2nd rework, director second pass, 3 Important + 5 Minor): все закрыты, код не
задеплоен/не закоммичен, ждёт review director.
IMPORTANT #1 — `step0_sync_local/step0.py::_check_reviews_freshness` больше не бросает
RuntimeError на устаревших reviews (было: 11+ дней → падает весь дневной pipeline, cron
никто не поставил). Теперь возвращает `(stale_days, warning)`, warning идёт в `details`
(Telegram/data_quality_log), exists+non-empty гейт остался жёстким (`_check_objects` в
`run()`, до вызова freshness). Заодно F4: `max(Date)` → `maxOrNull(Date)` (та же
non-nullable-column ловушка, что была в fetch_direct_stats defect B) — пустая таблица
больше не читается как "~20000 дней устарело". Живой прогон на одноразовой
`ad_analytics.zz_ponytail_freshness_proof` (удалена): 8д → warning=None, 11д →
warning с "stale", пустая таблица → жёсткий raise "has no rows" — все три показаны.
IMPORTANT #2 — `fetch_direct_stats.py::_check_swap_safe` (новая функция, вызывается перед
`swap_shadow`): отказывает в swap при `ok_count==0` ИЛИ когда `count()`/`sum(Cost)` shadow
падает ниже `live * SWAP_MIN_RETENTION_RATIO=0.95` (обоснование допуска — в комментарии у
константы, от реальных чисел таблицы 6284/88/SAFETY_DAYS=7). При отказе shadow дропается,
raise, как и у остальных failure-путей. Живой прогон на одноразовой
`ad_analytics.zz_ponytail_stats_proof(2)` (обе удалены): forced ok_count=0 → refuse + shadow
dropped + live rows unchanged (100); forced empty-payload (TSV только с заголовком) →
refuse ("shadow rows=76 < floor=95") + live unchanged; позитивный контроль (реальный мелкий
инкремент) → swap проходит нормально. tests/test_direct_account_reviews.py + 4 теста (zero
ok, empty payload, small-drop-within-tolerance, empty-table raise).
IMPORTANT #3 — `backfill_from_postgres.py` переписан: `--force` обязателен на непустой
target (иначе RuntimeError), TRUNCATE заменён на shadow+`EXCHANGE TABLES`
(`swap_shadow`, тот же примитив, что в load_reviews.py), accounts-таблица
(`yandex_direct_account_reviews`) вообще убрана из скрипта — он больше её не трогает
(раньше backfill заодно перетирал Sheets-managed справочник PG-снимком, это и случилось
2026-08-24 при recovery). Живой прогон на одноразовой `ad_analytics.zz_ponytail_backfill_proof`
(удалена, реальный PG-источник только читался): без `--force` на непустой таблице → refuse,
target unchanged (1 row); с `--force` → shadow+swap, target=6284 (реальные PG-данные);
`yandex_direct_account_reviews` до/после = 274/274 (не тронута).
Minor: F4 (см. выше, freshness). F5 — `step_cron_night/direct_account_reviews/CLAUDE.md`
переписан честно: снят миф "дубли из Sheets"/"argMax детерминированно берёт максимум id"
без объяснения лимитации porg-j47mlyp5, убрана устаревшая "273 аккаунта" (backfill больше
не трогает accounts, так что там больше нет счётчика для рассинхрона). F6 —
`step3_build_sources/step3.py::_build_reviews_sql` docstring исправлен: оба периода
porg-j47mlyp5 — ОДНА кампания (`CampaignId 710372643`, `uniqExact(CampaignId)=1`), пауза
2026-06-20..08-11 и возобновление, а не "разные кампании", как было написано раньше; вывод
(нужна колонка периода валидности, бизнес-решение, join не трогать) не менялся. F7 —
в `test_sync_reviews_stats_write_failure_aborts_without_swap` добавлен assert: каждый
`ALTER TABLE ... DELETE` бьёт в `{STATS_TABLE_FULL}_new`, ни разу в live `STATS_TABLE_FULL`.
Итог: `python3 -m pytest tests/ -q` = 227 passed / 1 skipped (было 223/1, +4 новых теста).
Прод-таблицы после всех проверок сверены и НЕ менялись: `yandex_direct_reports_reviews`
6284/88/1344281.23/2026-01-01..2026-08-16, `yandex_direct_account_reviews` 274/272 — байт-в-
байт совпадают с числами в начале задачи. Ни одна одноразовая proof-таблица не осталась
(проверено `system.tables LIKE 'zz_ponytail%'` = пусто). **Не сделано**: деплой на Victory,
коммит, живой коллектор не запускался — ждёт review `director`.

2026-08-24: восстановлен и переработан `step_cron_night/direct_account_reviews/fetch_direct_stats.py`
после инцидента потери данных (PID 32096, ручной kill на 109/272 аккаунте, лог
`logs/manual_reviews_step107_20260824_132850.log`). **Restore**: `backfill_from_postgres.py`
вернул `ad_analytics.yandex_direct_reports_reviews` к 6284 строкам / 88 логинов /
sum(Cost)=1344281.23 / 2026-01-01..2026-08-16 — сверено запросом после прогона, точное
совпадение с PG source of truth (`yandex_direct_raw.*` на Victory). Backfill заодно перетёр
`yandex_direct_account_reviews` до 273/272 (PG-снимок) — восстановлен обратно до 274/272
повторным `load_reviews.sync_reviews_accounts()` из живого Google Sheets (тот ряд, который
Семён просил не "восстанавливать" бэкфилом). **Три дефекта исправлены** (root cause —
`fetch_direct_stats.py` module docstring, блок ATOMICITY): A — `Date` шёл в insert как `str`,
теперь `_to_date()` конвертирует в `datetime.date` до любой записи, невалидный/отсутствующий
`Date` бросает `ValueError` громко. B — `max(Date)` по логину без строк отдавал ClickHouse
default `1970-01-01` (не NULL, колонка non-nullable) → `date_from` считался от эпохи
(`1969-12-25`); заменено на `maxOrNull(Date)` + явный пол `FULL_DATE_FROM=2026-01-01`.
C (главный) — `delete_and_insert` больше не трогает live-таблицу: `sync_reviews_stats` пишет
в shadow-копию (`{STATS_TABLE}_new`), swap в live через `swap_shadow`/`EXCHANGE TABLES`
только если ВСЕ логины записались чисто; любая ошибка = shadow дропается, live не тронут.
Добавлен `FAILURE_THRESHOLD=3` — ран прерывается досрочно, а не молотит по всем 272 логинам.
Доказано на одноразовой ClickHouse-таблице (`ad_analytics.zz_ponytail_reviews_proof`,
создана/удалена в рамках проверки, prod не трогала): форсированный TypeError не меняет
таблицу, успешный путь пишет `datetime.date`, threshold обрывает ран на 3-м логине из 5,
после — таблица подтверждённо удалена. `tests/test_direct_account_reviews.py` +10 тестов
(A/B/C), suite 223 passed / 1 skipped (было 213/1). **Не сделано**: живой коллектор
(`run.py` / night step 107) НЕ запускался повторно на проде — ждёт отдельного одобрения
Семёна после ревью `director`. Код не задеплоен на Victory (это Yandex Cloud managed
ClickHouse, деплоя как такового нет — `config/ch_db.py` ходит туда напрямую с Мака), не
закоммичен по условию задачи.

_2026-08-20, после гибридного пикселя, CRM fallback, BA5↔BA6 сверок и принятия BA6-ядра.
История — `git log -p STATE.md`._

## Где мы сейчас

2026-08-24: BA6 Power BI refresh включён на опубликованный ClickHouse-датасет. Локальный и Victory
`POWERBI_DATASET_ID` переведены с BA5 `Большая аналитика_v00` на `Большая аналитика_admin`;
`refresh_powerbi.py` доставлен на Victory (`md5=efa2ba734867824075683a4698396186`,
backup `refresh_powerbi.py.bak.20260824_065535`), local+remote `py_compile` OK.
Live Power BI API на Victory: datasource `Extension`, `_assert_ba6_datasource` = PASS,
последний selective refresh 25 таблиц = `Completed` (`endTime=2026-08-24T06:15:39.957Z`).
`cron_run.py` уже совпадал Mac==Victory; следующий cron ещё не проверялся.

2026-08-23: закрыто отставание Victory по reference-data refactor. На Victory перед копированием
сохранён backup `/home/semen_vi/ba6_v6_ch_pre_reference_data_sync_20260823_103536.tgz`, затем
точечно доставлены 13 runtime-файлов из локального состояния `a6f959d+`: `corrections.py`,
`spec_fallback.py`, builders spend, step4/9/14, `korrektirovki/run.py`,
`metrika_raw_builders.py`, `yandex_direct_checking_report/report.py` и связанные config/migration.
SHA-256 Mac==Victory по всем 13, remote `py_compile` OK, `data_check/verify_big_analytics.py` на
Victory = PASS. Остаточный code drift на тот момент: `refresh_powerbi.py` был отдельной Power BI
gate-задачей, закрытой 2026-08-24; remote-only `__codex_tmp_ba6_parity/*` и
`step5_build_pixel/check_pixel_table.py` остались как мусор/старый диагностический файл.

2026-08-21: локально подготовлен BA6 Power BI refresh после утреннего cron. `cron_run.py`
запускает `refresh_powerbi.py` только после успешного `pipeline.py`; refresh делает selective
transactional POST, ждёт финальный статус и блокирует PostgreSQL datasource до запуска.
На тот момент Power BI API показывал настроенный `Большая аналитика_v00` с PostgreSql (BA5), поэтому
деплой был отложен; состояние закрыто 2026-08-24 переводом на `Большая аналитика_admin`.

2026-08-21: BA6 вернул срез `тир_месяца`/`Dim_City_Tier` в PBIP admin+user и live
ClickHouse. `step0_sync_local/load_city_tier.py` грузит Google Sheet в
`ad_analytics.gsheet_city_tier`, `star_refactor/build_pbi_compat.py` добавляет
`city_tier_key` в `pbi_big_analytics_full` и строит `Dim_City_Tier`/`bi_Dim_City_Tier`.
Деплой на Victory: md5 Mac==Victory, remote `py_compile` OK, `sync_city_tier()` OK.
Прогон `pipeline.py --from-step=146` (`run_id=03c9c4978f64`) завершился `pipeline OK`,
`verify_big_analytics.py` = **PASS**. Live-check: `Dim_City_Tier=182`, `uniqExact(city_tier_key)=182`,
дублей = 0, `pbi_big_analytics_full LEFT JOIN Dim_City_Tier` orphan = 0.
PBIP-check: `Dim_City_Tier`/`тир_месяца` есть в 41 visual admin и 41 visual user;
лишняя user-кнопка `utmcheck2026new0000000/482c454b9796f6c13d0f` удалена.
КО/добро оставлены как есть: `dohod_do_kredita = credit + approved`, `dobro = approved`;
последний Adscope/autorules snapshot также держит `ko > dobro`.

2026-08-21: найден и закрыт остаток `avto_####` в PBI-срезе `салон`: после предыдущих
CRM/salon-фиксов коды уже не попадали из raw lead/call веток, но оставались в pixel-ветке
step5 (`_source_table='pixel'`). `step5_build_pixel/build_pixel.py` теперь резолвит
`victory_answers.salon` и matched/legacy `raw_data.leads_all.salon` через
`raw_data.gsheet_autosalony_clients.client_id → salon` тем же принципом, что step1.
Код доставлен на Victory (md5 + marker + remote `py_compile` OK). Прогон
`pipeline.py --from-step=3` (`run_id=34d80b1dd949`,
`logs/manual_pixel_salon_fix_from3_20260821_081659.log`) завершился `pipeline OK`,
`verify_big_analytics.py` = **PASS**. Live-check: `pbi_big_analytics_full`,
`Dim_Salon`, `big_analytics_full[pixel]` и `fact_big_analytics → Dim_Salon` по
`салон LIKE 'avto_%'` = 0. Воронка по заявкам с `dohod_do_kredita`/`dobro` уже есть:
`pbi_big_analytics_full`/`bi_pbi_big_analytics_full`, `fact_region_zayavki`/
`bi_fact_region_zayavki`, `fact_criterion_zayavki`/`bi_fact_criterion_zayavki`.
Маппинг берётся из `reference_data.crm_status_mapping`: `credit + approved → dohod_do_kredita`,
`approved → dobro`.

2026-08-21: BA6-фиксы срезов доставлены на Victory и пересобраны. Первый полный прогон
`--from-step=1` (`run_id=9273fa5a8abb`, `logs/manual_slices_rebuild_20260821_072915.log`)
успел обновить raw (`raw_leads=971 409`, `raw_calls=70 883`), но упал на step3 из-за
`anyLast(zvonki_cdr)` в `GROUP BY`; hotfix `37a0128` убрал агрегат из CDR group key.
Прогон `--from-step=3` (`run_id=77c6a767e107`) пересобрал wide/funnel-слой до star, затем упал
на `Dim_Campaign` из-за агрегата внутри fallback label. Hotfix вынес campaign-агрегаты во
внутренний SELECT. Финальный хвост `--from-step=145` (`run_id=9362d9b6387f`,
`logs/manual_slices_rebuild_from145_20260821_075531.log`) завершился `pipeline OK`,
`verify_big_analytics.py` = **PASS**. Live totals: `big_analytics_full=5 259 533`,
`big_analytics_unified=5 328 843`, `fact_big_analytics=5 328 843`,
`pbi_big_analytics_full=5 328 843`.

2026-08-21: в PBIP `Отчеты_victory_Powerbi` дополнительно удалены кнопки навигации `фиды/Фиды`,
которые всё ещё открывали hidden-страницу «Я.Директ_фиды/Фиды». Сами страницы оставлены hidden:
настоящего Direct feed-report в BA6 нет, текущий `fact_direct_feed_funnel` остаётся площадками РСЯ.
Тем же аудитом срезов найден и исправлен fallback label для `Dim_Campaign`/`Dim_AdGroup`:
пустой `номер кампании | название кампании` / `номер группы | название группы` теперь заменяется на
`id | name`, а не остаётся пустым значением в PBI-срезе. Live-check после пересборки:
`Dim_Campaign` blank labels = 0, `Dim_AdGroup` blank labels = 0, `Dim_ManagerLogin` non-email = 0,
`Dim_Salon` пустые `проджект/менеджер` схлопнуты в один `<NULL>/<NULL>` элемент,
`raw_leads.salon` вида `avto_XXXX` = 0.

2026-08-21: CRM/salon/campaign fixes применены в live ClickHouse. `Звонки_CDR` вернулся в
PBI-таблицу: `22 960` строк, `24 783` обращений, `34 040 850.31` ₽. `Название crm='Не указана'`
больше не несёт массовые CRM-заявки из скрина: осталось `252` обращения, все из `vk_perform`,
а основной хвост `Не указана` — cost-only Direct/VK/посевы без CRM-привязки
(`kol_vo_zayavok=0`, `direct` cost `351 388 261.69` ₽).

Принятый прогон BA6 `--from-step=3` от 2026-08-20 (`run_id=ed6bfc6f9c23`,
`logs/manual_hybrid_pixel_crm_from3_20260820.log`) завершился `pipeline OK`,
`verify_big_analytics.py` = **PASS**. Время прогона ≈30м22с. Итоговые live-объёмы:
`fact_big_analytics=5 288 442`, `big_analytics_full=5 218 726`,
`big_analytics_unified=5 288 442`, `pbi_import_big_analytics_full=5 288 442`.
`Dim_Source` не содержит мелких `Посевы_<domain>`; `Dim_PlacementFeed=35 441`, но
`feed_name/feed_url=0`, потому что настоящего Direct feed-report в `raw_data` нет.

Свежий прогон `--from-step=3` от 2026-08-20 (`run_id=2b10ff444daa`,
`logs/manual_posev_repaint_fix_20260820_091041.log`) завершился `pipeline OK`,
`verify_big_analytics.py` = **PASS** (golden Кудерко 25 697 413.60,
Δ+274 639.60 игнорируется из-за `KUDERKO_RAW_INCOMPLETE`; продажи 57 при floor 54).
Факт свежий: `fact_big_analytics` = 5 237 830 строк,
максимальная `Date` = 2026-08-19.
2026-08-20: восстановлен BA5-паритет посевных repaint-правил для BA6. Звонки посевных
доменов теперь берутся не только из hard crop-account, но и из `gsheet_sites.direction_main='Посевы'`
с BA5-приоритетом VK-Авто; SEO/direct_zero repaint больше не зависит от active crop-gate.
На Victory дополнительно досинкован уже закоммиченный `pipeline.py`, потому что remote отставал
и не запускал `step10a` перед step6. Живая проверка после прогона: `Посевы_Звонки/calls`
в `big_analytics_calls`, `big_analytics_full` и `fact_big_analytics` = 4 861 обращение /
3 814 корр / 1 274 квал / 738 приездов / 61 продажа; старого `источник='Звонки'` и пустых
источников в `big_analytics_full` = 0. BA5↔BA6 по `2026-01-01..2026-08-20`:
`Посевы_Звонки` 4 771→4 861 обращение, продажи 61→61; `Посевы_SEO` 1 177→1 205 обращений,
продажи 13→14. Контроль `2026-02-01..2026-07-31`: `Посевы_Звонки` 3 565→3 675,
`Посевы_SEO` 809→786.
2026-08-20: step5 pixel переведён на гибридный канон: до `2026-06-03` — `raw_data.leads_all`
по legacy цене BA5, с `2026-06-03` — `reference_data.victory_answers FINAL`
(`product='пиксель'`) с ценой из `cost`; статусы reference-заявок подтягиваются из
`raw_data.leads_all` по телефону и месяцу. Live после `ed6bfc6f9c23`:
`big_analytics_pixel=62 049`, `pixel_cost=143 061 550.00`,
`fact_big_analytics[pixel, По дате заявки]=62 049`,
`fact_big_analytics[pixel, По дате визита]=30 019`.
Оптимизация и фикс двойного счёта пикселя **закоммичены** (`3c0c726`, `c0fd79c`),
гибридный слой — `1ffdf07`.
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
2026-08-19: `direct_placement_links` теперь нормализует `placement_link` в валидный `https://...`
URL (`telegram.me` → `t.me`, голые домены получают `https://`) и не пишет строки без ссылки в
финальный `yandex_direct_tp_placement_links`; такие кандидаты остаются только в
`yandex_direct_tp_placement_link_matches`. Live CH пересобран: 5 399 строк, `empty_links=0`,
`bad_format=0`; Postgres-копия `yandex_direct_raw.yandex_direct_tp_placement_links`
синхронизирована 1:1, перед правкой сохранён backup
`yandex_direct_raw.yandex_direct_tp_placement_links_bak_20260819_urlfix`.
2026-08-19: для вкладки автоправил `/work/direct-autorules/placements` добавлена ClickHouse view
`ad_analytics.bi_direct_autorules_posevy_placement_links`: зерно день/логин/placement_link/
источник/campaign_id, фильтр кампаний `tp8`/`tp9`/`tp10` по `campaign_name`, пустые
`placement_link` исключены. Live view создана: 121 892 строки, 250 логинов, 5 311 ссылок,
расход 122 099 270.32 ₽; source-разбивка `телеграм`/`макс`/`другое`.
2026-08-19: BA6 raw CRM-слой больше не учитывает `raw_data.leads_all.is_copy_for_removal=1`:
фильтр добавлен в `raw_leads`, `raw_calls`, perform-ветки step1 и прямые `step13_arrival`
lookup/orphan-запросы. Коммит `513e729` доставлен на Victory, `pipeline.py --from-step=1`
завершился PASS (`run_id=cfecd7759368`, лог `logs/manual_copyfilter_20260819_165410.log`):
`raw_leads=959 181`, `raw_calls=70 943`, `fact_big_analytics=5 307 973`,
copy-строк в `raw_leads/raw_calls/raw_perform_leads` = 0. Compare v5→v6 теперь доходит до
чисел, но FAIL по всем 8 метрикам; отчёты сохранены в
`logs/compare_v5_v6_copyfilter_20260819.{txt,json}`.
2026-08-20: исправлена потеря `Название crm` в BA6. Для lead-based веток CRM теперь берётся
из `source_type`, а не из domain lookup; cost-overlay crop/Telega получает CRM через домен из
`raw_leads/raw_calls`; star `Dim_CRMStatus` нормализует пустую строку в `Не указана`.
Код доставлен на Victory, `pipeline.py --from-step=3` завершился PASS
(`run_id=435232fd3052`, лог `logs/manual_crm_fill_20260820_061158.log`): пустых CRM в
`big_analytics_full`, `fact_big_analytics`, `pbi_big_analytics_full`, `Dim_CRMStatus` = 0.
`Не указана` в BA6 осталась только на cost-only строках без обращений (`obr=0`), не на заявках
из `raw_data.leads_all`. Сверки сохранены в
`logs/compare_v5_v6_crmfill_20260820.json` и
`logs/crm_funnel_monthly_ba5_ba6_20260820.tsv`.
2026-08-20: отображаемое имя CRM для `rivendell_excel`/`perform_api` заменено на `Ривендел`.
Код доставлен на Victory; текущий `Dim_CRMStatus` обновлён live-мутацией:
`rivendell_excel=0`, `Ривиндел=0`, `Ривендел=19`.
2026-08-20: исправлены PBI-ошибки вокруг фидов/дубликатов. В BA6 удалены ручные builders
`build_arf_fact`/`build_arc_fact`, чтобы нельзя было пересоздать старые compatibility-view поверх
нефидовых данных; live `arf_fact/arc_fact/bi_arf_fact/bi_arc_fact` = 0 объектов. В BA6 PBIP скрыты
страницы «Я.Директ_фиды/Фиды» и два админских дубликата `Я.Директ_тексты объявлений_тексты`.
Код `build_pbi_compat.py` доставлен на Victory, `py_compile` OK, `pipeline.py --from-step=146`
завершился PASS (`run_id=3ef8f746462d`, лог `logs/manual_pbi_errors_fix_20260820_100556.log`).
Остаток не кодовый: настоящая фидовая воронка в BA6 всё ещё невозможна без сырого Direct-источника
с `feed_id/feed_url` и метриками/воронкой; текущий `fact_direct_feed_funnel` остаётся агрегатом
по площадкам РСЯ для страницы площадок.

## Главное, что выяснилось 15.08

**Сырьё v6 не беднее v5 — оно богаче.** Директ +354 258 строк и +10.54 млн ₽; 99 «пропавших»
аккаунтов несут 0.00 ₽. CRM-лиды больше v5 в каждом месяце 2026. После фильтра
`is_copy_for_removal` свежая витринная сверка Feb–Jul без пикселя:
расход +0.36%, обращения −1.99%, заявки −0.81%, продажи +0.62%.

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
- **Дневной прогон в кроне с 16.08:** `0 4 * * *` UTC = 09:00 Екб, через обёртку `cron_run.py`
  (сам `pipeline.py` в Telegram не пишет ничего). Сдвинут с `0 2` 24.08: старт попадал в окно
  загрузки `raw_data.yandex_direct_report_rows` 04:30–06:42 МСК и вчерашний расход приезжал
  на ~30% — подробности и цифры в `PIPELINES.md`. **Ночной прогон в кроне с 17.08:**
  `10 18 * * *` UTC = 23:10 Екб, через `step_cron_night/pipeline_night.py`.
- **Гейт на полноту свежего дня — `full_last_day_incomplete` в `verify_big_analytics.py`**
  (24.08). FAIL, если `sum(total_cost)` за `today()-1` меньше 0.6 медианы семи предыдущих дней.
  Порог откалиброван бэктестом 16.07–23.08: здоровые ratio 0.784–1.236, сломанный 23.08 = 0.300.
  До него ни одна проверка не смотрела на полноту последнего дня, и недолив расхода PASS-ил.
- **Код на Victory сверен с HEAD 16.08: 143/143 совпали.** Ничто не синкает его туда автоматически
  (Mutagen ходит на LXC 101) — дрейф копится молча: Victory отставал на три ETL-коммита от 13–14.08
  (`status_sql`, `corrections`, `step6`). Сверка md5 — `RUNBOOK.md` §3a, гонять перед доверием к прогону.
- Golden-дельта по Кудерко — не новая, root-cause #37 (неполное сырьё, 29/67 логинов).
  В текущем прогоне Δ+219 660.57 ₽ переведена verify-гейтом в warning и не валит PASS.

2026-08-25: коммит `6c7e5b9` (executor `oleg_programmer`, отдельно от `07575f1`/`9017fab`) —
`region_spend`/`criterion_spend`/`adformat_spend` builders получили колонку `специалист`
(`LowCardinality(Nullable(String))`), которой у них раньше не было вообще (только `site_key`).
Значение = `specialist_correction_expr(date, account_login, any(gb.directologist))` поверх
`gs_best` CTE из `07575f1` — общая функция из `corrections.py:162`, уже применяемая на
claim/pixel/visit/calls осях, здесь не переизобреталась. `GROUP BY`/`WHERE`/`sum(cost)` не
трогались — деньги по построению не двигаются. Проверено read-only симуляцией на живом
Victory ClickHouse (`raw_data.yandex_direct_report_rows` + `reference_data.gsheet_sites`, БЕЗ
записи, пайплайн не запускался — hard constraint): login `e-20086622` Jan–Apr(<04-10) уходит
Тумашенко→Кудерко (66 269/786 223/905 724/258 892 ₽), Apr(>=04-10) остаётся Тумашенко
(71 334 ₽) — барьер строго исключающий, ожидаемо. Августовские логины Кудерко
(`porg-x7wkhs7d`/`vzw5t7mt`/`ead45mqo`) не тронуты — правило не срабатывает, резолвятся через
директорию. Все 4 правила сработали во всех 3 фактах (Кудерко 227 630 строк/15,27М ₽, Сергеев
162 394/9,52М, Питеркина 173/9,45К — только пустой-fallback, Чепелев 32 740/2,87М).
`py_compile` OK, pytest 239/1 skip/2 pre-existing fail (`yandex_direct_ads_texts`, не связано).
**Не сделано:** сам прогон (`region_spend.run()`/`criterion_spend.run()`/`adformat_spend.run()`)
withheld по task-констрейнту — новую колонку никто не видел живьём в `fact_*`; `build_pbi_compat.py`
для этих трёх фактов всё ещё берёт только `домен` из `Dim_Site` и не прокидывает `специалист` —
вне скоупа задачи (явный список файлов), нужен отдельный проход, если Семён захочет видеть колонку
в Power BI. Июньская цифра Кудерко 1 707,89 ₽ из `07575f1` не перепроверялась — её код
(`corrections.py`/`step11`/`step13`/`step6`) в этом коммите не трогался.

2026-08-25: локально подготовлен пакет PBI tech fixes, см.
`PBI_TECH_FIX_PLAN_2026-08-25.md`. Важно: **второй pipeline не запускать**, пока Семён не
опубликует Power BI Service с текущими PBIP-правками. Ручной прогон `3914534`
(`run_id=54492f914f5c`) упал на step146 `build_pbi_compat`: `pbi_big_analytics_full` ссылался
на `f.\`специалист\``, но `fact_big_analytics` хранит только `site_key`. Локально исправлено на
`dsite.\`специалист\`` и проверено `EXPLAIN SYNTAX`. В этом же локальном пакете: relationship
`fact_region_spend.distance_km_agreg -> Dim_Distance.distance_km_agreg`; fact-row поля для
keyword/format/minus/corrections PBIP-страниц; русский `Dim_AdFormat`; VK Ads spend-строки
получают `салон/регион/тип_сайта/специалист` через
`reference_data.vk_ads_agency_clients.domain -> reference_data.gsheet_sites.domain` и fact
возвращает `ad_plan_name/ad_group_name/banner_name`. Проверено без записи: `py_compile`, JSON parse
обоих PBIP, `EXPLAIN SYNTAX` для изменённых SELECT. Не проверено: live rebuild после публикации BI.
Дополнено по скрину с двумя пустыми строками в coder-группах: пустые `Dim_AdGroup.adgroup_code`,
`марки авто`, `ag_part1..ag_part7`, `ag_part1_name` теперь нормализуются в `Не указано` в PBIP
Power Query и `_dim_adgroup_pbi_sql()`. Рискованные страницы перечислены в
`PBI_TECH_FIX_PLAN_2026-08-25.md`. Runtime `.py` доставлены на Victory без запуска pipeline:
md5 Mac==Victory, local+remote `py_compile`, markers OK.

2026-08-25 17:23 Екб: по ошибке Power BI Desktop “Запросы заблокированы” причина была не
в сырье, а в отсутствующих `bi_*` views после падения старого step146. Локально в PBIP
переведены пять проблемных queries на живые базовые объекты/compat-колонки:
`direct_history`, `check_utm_fuck_direct`, `yandex_direct_korrektirovki`, `pixel_score`,
`fact_region_spend`. Отдельно исправлен `_vk_ads_pbi_sql()` — имена плана/группы/баннера
берутся из `Dim_Vk*`, поэтому view работает и до полного пересчета `fact_vk_ads`; для
`bi_fact_region_spend_star` добавлены compatibility `domain`/`updated_at`.
Запущен **только** `star_refactor/build_pbi_compat.py`, не полный pipeline: создано
`bi_views_created=47`, heavy compat rows `49,679,623`. Проверено: все 41 ClickHouse-источник
из TMDL проходят `DESCRIBE`, `missing=0`; проблемные колонки из скрина есть либо в базовом
источнике, либо создаются Power Query; `py_compile build_pbi_compat.py` OK; PBIP JSON OK.
Второй полный pipeline всё ещё не запускался.

## Открытые дефекты

`KNOWN_ISSUES.md`: к прежним добавлены **#39** (часть PBI ещё на `raw_new_*`), **#40** (FIXED:
whitelist пустоты убран), **#41** (`big_analytics_reviews` = 0 из-за рассинхрона тега),
**#42** (минус-фразы в night cron, 30-дневная история ещё наполняется),
**#43** (врущие индикаторы свежести),
**#44** (`domains` −322, `gsheet_sites` −52, `crm_status_mapping` −5).
Из прежних актуальны #37 (бэкфил Кудерко) и #38 (`data_check/compare` не работает после star).
