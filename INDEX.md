# INDEX — big_analytics_v6_ch

> Сгенерировано `scripts/gen_project_index.py`. Руками не править — перегенерировать.
> Назначение: найти нужный файл БЕЗ обхода дерева грепом.

Файлов в индексе: **252**

## корень проекта

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `ATTRIBUTION.md` | 7 | ATTRIBUTION.md — единый авторитет по атрибуции |  |
| `BLOCKS.md` | 38 | BLOCKS.md — технические блоки ETL (C–L) + corrections |  |
| `CANON.md` | 6 | CANON.md — канон значений `big_analytics_full` |  |
| `CLAUDE.md` | 17 | CLAUDE.md — big_analytics_v6_ch |  |
| `COLUMNS_big_analytics_full.md` | 11 | COLUMNS_big_analytics_full.md — поколоночный словарь главной витрины |  |
| `COOKIES.md` | 7 | COOKIES.md — куки Яндекс.Директ |  |
| `DB_TABLES.md` | 51 | DB_TABLES.md — Таблицы на сервере (ad_analytics_bi) |  |
| `DOD.md` | 17 | DOD — Definition of Done, big_analytics_v5 |  |
| `FUNNEL.md` | 11 | FUNNEL.md — воронка заявок (`local_crm_statuses`) |  |
| `GOLDEN_BASELINE.md` | 27 | GOLDEN BASELINE — эталонные значения для проверки данных |  |
| `INDEX.md` | 31 | INDEX — big_analytics_v6_ch |  |
| `KNOWN_ISSUES.md` | 15 | KNOWN_ISSUES — big_analytics_v5 |  |
| `MEMORY.md` | 17 | MEMORY.md — big_analytics_v5 (condensed patterns) |  |
| `MEMORY_ARCHIVE.md` | 275 | MEMORY.md — ba_pipeline: нетривиальные уроки |  |
| `PBI_TABLES.md` | 31 | Таблицы, которые читает Power BI — справочник |  |
| `PIPELINES.md` | 26 | PIPELINES.md — пайплайны, расписание, распределение шагов |  |
| `PLAN.md` | 42 | PLAN.md — big_analytics_v6_ch (миграция пайплайна на ClickHouse) |  |
| `POSEV_LEADS_LOSS_PLAN.md` | 23 | POSEV_LEADS_LOSS_PLAN — системный план ловли и починки потерь посевных ЗАЯВОК |  |
| `POSEV_LOSSES_PLAYBOOK.md` | 44 | POSEV_LOSSES_PLAYBOOK — направление «посевы» big_analytics_v5 |  |
| `PROJECT_CHARTER.md` | 32 | PROJECT_CHARTER.md — Устав проекта big_analytics_v5 |  |
| `QUERIES.md` | 9 | QUERIES.md — SQL-шпаргалка |  |
| `README.md` | 15 | big_analytics_v6_ch — Пайплайн аналитики (форк big_analytics_v5, миграция на ClickHouse) |  |
| `RUNBOOK.md` | 14 | RUNBOOK.md — операционка и восстановление |  |
| `SHEET_RECONCILE.md` | 10 | Сверка с гугл-таблицей «посевы» — FINDINGS + METHODOLOGY |  |
| `SHEET_RECONCILE_FINDINGS.md` | 55 | Сверка Google-таблиц салонов ↔ public.fact_big_analytics (контекст) |  |
| `SHEET_RECONCILE_METHODOLOGY.md` | 28 | Методика сверки Google-таблиц салонов ↔ витрина (КОНТЕКСТ) |  |
| `SPEC.md` | 33 | SPEC — big_analytics v6 на ClickHouse |  |
| `STAR_REFACTOR_BRIEF.md` | 14 | ТЗ для director — рефакторинг big_analytics_v5 под звезду (star schema) |  |
| `STATE.md` | 53 | big_analytics_v5 — Состояние (handoff) |  |
| `STATE_ARCHIVE.md` | 375 | Сессия 2026-07-15 (oleg_programmer — restore-прогон на откаченном коде) — ⚠️ kval НЕ восстановился |  |
| `_rebuild_arrival.py` | 1 |  | log |
| `_set_serverhost_domain.py` | 3 | _set_serverhost_domain.py — сменить ServerHost параметр датасета на домен | load_pbi, get_token, main |
| `_warm_campaign_status.py` | 3 | _warm_campaign_status.py — точечный прогрев campaign_status/payment_model БЕЗ step0/step8. | main |
| `copy_metrika.py` | 6 | Копирует данные из локального big_analytics.public.metrika | main |
| `corrections.py` | 5 | ClickHouse v6 corrections hook. | specialist_correction_expr, apply |
| `explain_all.py` | 18 | explain_all.py — EXPLAIN ANALYZE замер всех измеримых шагов пайплайна. | explain_step, skip_step |
| `explain_capture.py` | 10 | explain_capture.py — захват EXPLAIN ANALYZE планов тяжёлых шагов. | write_header, wrap_explain, log_wall_sub, write_step7_header, capture_step |
| `fast_pipeline.py` | 109 | fast_pipeline.py — быстрый пайплайн big_analytics_v5 | preflight_check, main |
| `pipeline.py` | 8 | ClickHouse orchestrator for big_analytics_v6_ch. | ensure_quality_log, log_step, run_step, run_step_isolated, selected_steps, main |
| `pipeline_mutex.py` | 5 | pipeline_mutex.py — PIPELINE_MUTEX_2026-07-12 | PipelineBusy, acquire |
| `pipeline_powerbi.py` | 24 | pipeline_powerbi.py — пайплайн big_analytics_v5 с проверкой расходов и триггером Power BI | main |
| `refresh_cookies.py` | 8 | refresh_cookies.py — автоматическое обновление cookies.json с эндпоинта glavpotok.ru. | refresh_cookies |
| `refresh_powerbi.py` | 19 | refresh_powerbi.py — только обновление отчётов Power BI Service. | refresh_powerbi |
| `sync_replica_incremental.sh` | 3 | bin/bash |  |
| `watch_pipeline.py` | 11 | watch_pipeline.py — хвостит /tmp/fast_pipeline.log, детектирует завершение | tail_forever, main |

## `adformat_spend/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `CLAUDE.md` | 2 | adformat_spend — датамарт «расход по формату объявления» |  |
| `build_adformat_spend.py` | 4 | Build ClickHouse `fact_adformat_spend` from raw Yandex report rows. | run |

## `config/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `__init__.py` | 1 |  |  |
| `brand_map.py` | 7 | brand_map.py — маппинг ct-кодов групп объявлений → марки авто | build_brand_case_sql |
| `ch_db.py` | 2 | Подключение к ClickHouse Victory (Yandex Cloud) для big_analytics_v6_ch. | get_ca_cert_path, get_client |
| `ch_settings.py` | 1 | ClickHouse-specific constants for big_analytics_v6_ch. |  |
| `ch_utils.py` | 5 | Small ClickHouse helpers for the v6_ch migration. | q, table_exists, table_engine, column_names, column_list, create_empty_like, swap_shadow, month_ranges_from_ta |
| `cookies.py` | 12 | cookies.py — проверка валидности кук Яндекс.Директ перед шагами пайплайна | check_cookies_alive, check_all_cookies_strict, send_tg, send_tg_cookies_dead, CookiesDeadError, ensure_cookies |
| `db.py` | 7 | db.py — пулы соединений к ad_analytics (SRC) и ad_analytics_bi (DST) | init_pool, get_conn, put_conn, close_pool, init_src_pool, get_src_conn, put_src_conn, close_src_pool |
| `settings.py` | 7 | settings.py — константы конфигурации big_analytics_v5 |  |
| `status_sql.py` | 35 | config/status_sql.py — динамическая генерация SQL квалификации лидов | load_status_sql, build_leads_agg_sql |
| `tokens.py` | 2 | tokens.py — API-ключи и адреса внешних сервисов |  |

## `criterion_spend/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `CLAUDE.md` | 6 | criterion_spend — датамарт «расход по критерию (ключ/таргетинг)» |  |
| `build_criterion_spend.py` | 5 | Build ClickHouse `fact_criterion_spend` from raw Yandex report rows. | run |
| `build_criterion_zayavki.py` | 5 | Build ClickHouse `fact_criterion_zayavki` from CRM leads by utm_term. | run |
| `build_dim_criterion.py` | 8 | criterion_spend/build_dim_criterion.py — измерение «критерий» для Power BI. | run, main |

## `crm_mappings_check/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `__init__.py` | 1 |  |  |
| `check.py` | 12 | crm_mappings_check/check.py — отчёт о неиспользуемых маппингах в local_crm_statuses. | run |

## `data_check/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `README.md` | 5 | data_check — подсистема проверок качества данных |  |
| `__init__.py` | 1 | step_data_check package |  |
| `golden_reward.py` | 32 | data_check/golden_reward.py — числовой golden-reward скорер для best-of-N. | compute_reward, run_scorer, main |
| `reporter.py` | 5 | step_data_check/reporter.py | format_report, send_telegram |
| `run.py` | 3 | data_check/run.py — точка входа агента проверки данных. | main |
| `sheets_reader.py` | 3 | step_data_check/sheets_reader.py | get_sheet_name_by_gid, read_sheet |
| `verify_big_analytics.py` | 6 | Master verification for big_analytics_v6_ch ClickHouse tables. | run, main |

## `data_check/checks/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `__init__.py` | 1 | checks package |  |
| `fields.py` | 1 | step_data_check/checks/fields.py | run |
| `funnel.py` | 3 | step_data_check/checks/funnel.py | check_invariants, run |
| `projects.py` | 2 | step_data_check/checks/projects.py | run |
| `spending.py` | 2 | step_data_check/checks/spending.py | run |

## `data_check/reconcile/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `__init__.py` | 1 |  |  |
| `pilot.py` | 21 | data_check/reconcile/pilot.py — Пилотный reconcile-движок Кит-Авто | read_sheet_voronka, fetch_our_side, reconcile, format_report, main |
| `reconcile.py` | 31 | data_check/reconcile/reconcile.py — Reconcile-движок по всем салонам реестра | read_sheet_voronka, fetch_our_side, reconcile_salon, format_salon_report, format_summary, format_tg_digest, ma |

## `data_verification/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `CHECKLIST_post_run_calls_visit_2026-06-08.md` | 5 | Чек-лист пост-прогонной верификации: визит-сторона (Var A) + звонки→посевы |  |
| `README.md` | 4 | data_verification — как проверять данные big_analytics_v5 и какие бывают ошибки |  |
| `error_big.md` | 36 | error_big — Аудит расхождений big_analytics_full vs Google Sheets клиентов |  |
| `ПОСЕВЫ_матчинг_итоги_2026-06-03.md` | 9 | Посевы Telega.in ↔ лиды: матчинг, правки, итоги |  |

## `data_verification/посевы/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `AUDIT_PLAN.md` | 17 | Посевы — аудит консистентности и план действий (июнь 2026) |  |
| `PROJECT_CHARTER.md` | 10 | PROJECT CHARTER — Аудит и наведение порядка в посевах |  |
| `README.md` | 14 | Посевы (crop targeting) — как работает, что сделано, что проверять |  |
| `STATUS_lead_dedup_2026-06-04.md` | 12 | Посевы — статус и находки по задвоению заявок (4 июня 2026) |  |
| `STATUS_oshibki1_fix_2026-06-06.md` | 5 | STATUS — фикс листа «ошибки1» + межсайтового задвоения посевов (2026-06-06) |  |

## `dev/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `CLAIMS.md` | 2 | CLAIMS.md — реестр «застолблённых» файлов (анти-коллизии двух окон) |  |

## `direct_feed_funnel/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `README.md` | 15 | direct_feed_funnel |  |
| `__init__.py` | 1 | Direct feed funnel builder. |  |
| `build.py` | 3 | Build ClickHouse `fact_direct_feed_funnel` from raw report rows. | run |
| `build_keyed.py` | 34 | Build Direct feed funnel by the agreed physical key. | build, main |
| `build_report_criterion.py` | 8 | Build analytics_report_criterion — denormalised report table for Power BI «Критерий». | build |
| `build_report_feed.py` | 12 | Build analytics_report_feed — denormalised report table for Power BI "Фиды" page. | build |
| `fetch_feed_urls_cookie.py` | 13 | Fetch real Yandex Direct feed URLs through the Direct web UI cookie API. | fetch_login, run, main |
| `pipeline.py` | 4 | Pipeline for Direct feed funnel. | check_yandex_feed_source, run, main |

## `migrations/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `01_init_schema.sql` | 46 | ============================================================================= |  |

## `region_spend/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `CLAUDE.md` | 7 | region_spend — датамарт «расход по регионам показа» |  |
| `build_region_spend.py` | 6 | Build ClickHouse `fact_region_spend` from raw Yandex report rows. | run |
| `build_region_zayavki.py` | 5 | Build ClickHouse `fact_region_zayavki` from CRM leads by geoid. | run |

## `sales_attribution/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `README.md` | 4 | sales_attribution — витрина атрибуции продаж для Power BI |  |
| `__init__.py` | 1 |  |  |
| `build.py` | 10 | sales_attribution/build.py | main |
| `verify.py` | 5 | sales_attribution/verify.py | main |

## `spend/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `__init__.py` | 1 |  |  |
| `build_spend_staging.py` | 18 | spend/build_spend_staging.py — единый проход FDW → UNLOGGED staging. | ensure_staging, drop_staging, staging_exists |

## `sql/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `create_pbi_big_analytics_full_view.sql` | 2 | Compatibility view for the Power BI model table big_analytics_full. |  |
| `v_monthly_kpi_avto.sql` | 4 | VIEW: помесячная KPI-воронка по big_analytics_full |  |

## `star_refactor/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `PBI_REFRESH_OPTIMIZATION_PLAN.md` | 10 | PBI refresh optimization plan |  |
| `audit_pbi_sources.py` | 7 | Metadata-only audit of Power BI source objects in ClickHouse. | ObjectMeta, build_report, main |
| `build_pbi_compat.py` | 31 | Build compatibility tables expected by the existing Power BI semantic model. | drop_bi_views, build_pbi_full, build_pixel_score, build_dim_placement_feed, build_pbi_import_direct_feed_funne |
| `build_star.py` | 22 | Build ClickHouse star/Power BI tables for v6_ch. | build_fact, build_dims, build_ml_korrektirovki_fact, build_vk_ads_fact, run, main |
| `build_star_extensions.py` | 5 | Optional extra star dimensions for the ClickHouse Power BI model. | build_dim_adformat, build_dim_adnetwork, build_dim_source, run |
| `pbi_source_audit.md` | 5 | PBI Source Audit |  |
| `verify_star.py` | 5 | verify_star.py — ШАГ 6 верификация без потерь. | sums, cmp, cmp_grouped |

## `star_refactor/pbi_handoff/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `00_README_NEED_FROM_USER.md` | 6 | Хэндоф star-рефакторинга PBI — что готово и что нужно от пользователя |  |
| `01_tmdl_partitions.md` | 6 | TMDL-фрагменты M-запросов для star-таблиц (Power BI) |  |
| `02_checklist_raspaika.md` | 7 | Чек-лист распайки модели Power BI на звезду |  |
| `03_pipeline_diff.md` | 13 | План интеграции star в пайплайн (ДИФ, НЕ запускать до приёмки модели) |  |
| `FINAL_PLAN.md` | 26 | Финальный план: перевод прода на STAR (big_analytics_v5) |  |
| `find_field_refs.py` | 6 | find_field_refs.py — сканер ссылок на поля факта, которые переезжают в dim. | scan_report, main |
| `remap_back_rowgrain.py` | 13 | remap_back_rowgrain.py — ОБРАТНЫЙ remap (2026-06-07). | atomic_write_json, collect_aliases, remap_node, alias_uses_site_kept, remap_direct_entities, process_scope, it |
| `remap_field_refs.py` | 11 | remap_field_refs.py — переписывает ссылки полей факта big_analytics_full на dim-таблицы | collect_aliases, remap_node, process_query, alias_uses_fact_kept, is_excluded, main |

## `step0_sync_local/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `CLAUDE.md` | 7 | step0_sync_local — Синхронизация локальных копий |  |
| `README.md` | 8 | step0_sync_local — Синхронизация локальных копий |  |
| `STEP.md` | 3 | STEP.md — Шаг 0: Синхронизация локальных копий |  |
| `__init__.py` | 1 |  |  |
| `step0.py` | 3 | Step 0 for v6_ch: ClickHouse-only source preflight. | run |

## `step10_crop_targeting/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `CLAUDE.md` | 6 | step10_crop_targeting — Посевы (crop targeting) |  |
| `README.md` | 8 | step10_crop_targeting — Посевы (crop targeting) |  |
| `SPEC_fetch_api_telegain.md` | 12 | Спецификация: загрузка посевов Telega.in → PostgreSQL |  |
| `fetch_api.py` | 15 | crop_targeting/fetch_api.py — загрузка посевов из Telega.in API | fetch_orders, ensure_table, upsert, main |
| `load_api_leads.py` | 13 | big analytics_v5/crop_targeting/load_api_leads.py | ensure_output_table, run_query, main |
| `load_crop_targeting.py` | 11 | big analytics_v5/crop_targeting/load_crop_targeting.py | read_sheet, clean_header, parse_rows, parse_utm_pairs, filter_for_main, process_main_data, read_utm_mapping, a |
| `load_crop_targeting_leads.py` | 25 | big analytics_v5/crop_targeting/load_crop_targeting_leads.py | get_source_columns, ensure_output_table, run_query, main |
| `load_crop_to_big_analytics.py` | 29 | big analytics_v5/crop_targeting/load_crop_to_big_analytics.py | main |
| `load_telega_in_orders.py` | 25 | big analytics_v5/crop_targeting/load_telega_in_orders.py | ensure_output_table, run_query, collect_errors, main |
| `pipeline.py` | 2 | big analytics_v5/crop_targeting/pipeline.py |  |
| `step10.py` | 46 | Step 10 for v6_ch: add crop/Telega/VK cost overlays. | run |

## `step11_pixel_score/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `ALGORITHM.md` | 8 | step11_pixel_score — алгоритм атрибуции (детали) |  |
| `CLAUDE.md` | 9 | step11_pixel_score — Атрибуция pixel-воронки |  |
| `PLAN_cpl_score.md` | 18 | ПЛАН: новый скор CPL-качества (0.3–3) для big_analytics_pixel_score |  |
| `README.md` | 8 | step11_pixel_score — Атрибуция pixel-воронки |  |
| `step11.py` | 17 | Step 11 for v6_ch: pixel attribution materialization in ClickHouse. | run |

## `step12_proverka_big_analytics/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `CLAUDE.md` | 4 | step12_proverka_big_analytics — Сверка big_analytics vs CSD по грани CRM (ниша Авто) |  |
| `MEMORY.md` | 2 | MEMORY.md — step12_proverka_big_analytics |  |
| `README.md` | 7 | step12_proverka_big_analytics — Сверка bigA с CSD по грани CRM |  |
| `__init__.py` | 1 |  |  |
| `step12.py` | 3 | Step 12 for v6_ch: ClickHouse quality checks. | run |

## `step13_arrival/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `CLAUDE.md` | 11 | step13_arrival — CLAUDE.md |  |
| `README.md` | 7 | step13_arrival — Воронка по дате визита |  |
| `ROLLBACK_mirror_union.md` | 2 | Откат MIRROR+UNION (step13 73-кол зеркало + big_analytics_unified) |  |
| `__init__.py` | 1 |  |  |
| `build_unified.py` | 1 | Build `big_analytics_unified` in ClickHouse. | run |
| `step13.py` | 3 | Step 13 for v6_ch: build arrival mirror table in ClickHouse. | run |

## `step14_minus_snapshot/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `CLAUDE.md` | 5 | CLAUDE.md — step14_minus_snapshot |  |
| `step14.py` | 19 | Step 14 for v6_ch: Yandex Direct negative keywords snapshot in ClickHouse. | load_tokens, enumerate_logins, fetch_sets_sizes, process_login, ensure_schema, replace_delta_view, load_specia |

## `step1_load_raw/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `CLAUDE.md` | 6 | step1_load_raw — RAW UNLOGGED таблицы |  |
| `README.md` | 6 | step1_load_raw — Загрузка RAW UNLOGGED таблиц |  |
| `STEP.md` | 1 | STEP.md — Шаг 1: RAW UNLOGGED таблицы |  |
| `__init__.py` | 1 |  |  |
| `step1.py` | 13 | Step 1 for v6_ch: build RAW tables in ClickHouse from raw_data. | run, get_explain_sql |

## `step2_indexes/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `CLAUDE.md` | 3 | step2_indexes — Индексы на RAW + ANALYZE |  |
| `README.md` | 3 | step2_indexes — Индексы и ANALYZE на RAW-таблицах |  |
| `STEP.md` | 1 | STEP.md — Шаг 2: Индексы + ANALYZE на RAW |  |
| `__init__.py` | 1 |  |  |
| `step2.py` | 1 | Step 2 for v6_ch: ClickHouse RAW maintenance. | run |

## `step3_build_sources/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `CLAUDE.md` | 8 | step3_build_sources — Сборка источниковых таблиц |  |
| `README.md` | 7 | step3_build_sources — Сборка источниковых таблиц |  |
| `STEP.md` | 2 | STEP.md — Шаг 3: Сборка таблиц по источникам |  |
| `__init__.py` | 1 |  |  |
| `step3.py` | 27 | Step 3 for v6_ch: build source marts in ClickHouse. | recreate_source_views, run, get_explain_sql |

## `step4_campaign_status/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `CLAUDE.md` | 8 | step4_campaign_status — Статусы кампаний Яндекс.Директ |  |
| `README.md` | 7 | step4_campaign_status — Статусы кампаний Яндекс.Директ |  |
| `__init__.py` | 1 |  |  |
| `step4.py` | 2 | Step 4 for v6_ch: campaign status from ClickHouse raw_data. | prefetch_statuses, run |

## `step4_campaign_status/check_utm/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `__init__.py` | 1 |  |  |
| `utm_direct_audit.py` | 42 | step7_campaign_status_check_utm/check_utm/utm_direct_audit.py | api_get, metrika_get, load_metrika_counters, find_counter_for_domain, search_counter_by_domain, get_utm_via_me |

## `step5_build_pixel/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `CLAUDE.md` | 5 | step5_build_pixel — Пиксельные данные |  |
| `README.md` | 6 | step5_build_pixel — Сборка пиксельных данных |  |
| `__init__.py` | 1 |  |  |
| `audit_pixels.py` | 2 | Audit pixel names in local_pixel_config vs local_leads_all. |  |
| `audit_pixels_detailed.py` | 2 | Detailed audit: pixel names by source_name AND utm_source. |  |
| `build_pixel.py` | 10 | Step 5 for v6_ch: finalize/check pixel source table. | run |
| `set_pixel_price.py` | 9 | set_pixel_price.py — управление дата-эффективной историей цен пикселей. | set_price, list_prices, main |
| `sync_pixel_config.py` | 7 | pixel/sync_pixel_config.py — синхронизация конфига пикселей из Google Sheets. | sync |

## `step6_build_full/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `CLAUDE.md` | 7 | step6_build_full — Сборка `big_analytics_full` |  |
| `README.md` | 7 | step6_build_full — Сборка `big_analytics_full` |  |
| `STEP.md` | 1 | STEP.md — Шаг 4: big_analytics_full (UNION ALL) |  |
| `__init__.py` | 1 |  |  |
| `step6.py` | 8 | Step 6 for v6_ch: build `big_analytics_full` in ClickHouse. | run, get_explain_sql |

## `step7_finalize/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `CLAUDE.md` | 4 | step7_finalize — Финализация big_analytics_full |  |
| `README.md` | 6 | step7_finalize — Финализация big_analytics_full |  |
| `STEP.md` | 1 | STEP.md — Шаг 5: Финализация |  |
| `__init__.py` | 1 |  |  |
| `step7.py` | 1 | Step 7 for v6_ch: finalize ClickHouse tables. | run |

## `step8_stats/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `CLAUDE.md` | 7 | step8_stats — Финальная статистика + Telegram-отчёт |  |
| `MEMORY.md` | 2 | MEMORY — step8_stats |  |
| `README.md` | 7 | step8_stats — Финальная статистика + Telegram-отчёт |  |
| `STEP.md` | 1 | STEP.md — Шаг 6: Статистика + Telegram-отчёт |  |
| `__init__.py` | 1 |  |  |
| `funnel_drift_snapshot.py` | 30 | step8_stats/funnel_drift_snapshot.py — снимок воронки по (month × источник) + алерт дрейфа. | run |
| `pipeline_log_snapshot.py` | 7 | step8_stats/pipeline_log_snapshot.py — снимок воронки по месяцам в data_pipeline_log. | run |
| `step8.py` | 1 | Step 8 for v6_ch: read-only ClickHouse statistics. | run |

## `step9_direct_history/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `CLAUDE.md` | 6 | step9_direct_history — История изменений Яндекс.Директ |  |
| `README.md` | 7 | step9_direct_history — История изменений Яндекс.Директ |  |
| `step9.py` | 2 | Step 9 for v6_ch: ClickHouse direct history snapshot. | prefetch_history, run |

## `step_cron_night/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `CLAUDE.md` | 2 | CLAUDE.md — step_cron_night |  |
| `README.md` | 2 | step_cron_night — ночной пайплайн |  |
| `__init__.py` | 1 |  |  |
| `build_ml_korrektirovki_night.py` | 5 | build_ml_korrektirovki_night.py — ночное обновление public.fact_ml_korrektirovki. | main |
| `build_spend_daily.py` | 17 | step_cron_night/build_spend_daily.py — дневной job (14:00 Екб / 09:00 UTC): сборка 3 spend-витрин. | main |
| `build_spend_night.py` | 1 | DEPRECATED — переименован в build_spend_daily.py (2026-06-27). |  |
| `metrika_raw_builders.py` | 16 | Build Metrika-derived ClickHouse tables from existing raw_data snapshots. | build_metrika_yandex, build_404_errors, build_check_utm, run_all |
| `metrika_yandex.py` | 1 | v6_ch metrika_yandex builder from ClickHouse raw_data. | run, main |
| `pipeline_night.py` | 5 | pipeline_night.py — ночной пайплайн big_analytics_v5 | run_step, main |
| `revoke_metrika_grants.py` | 12 | revoke_metrika_grants.py — отзыв грантов Яндекс.Метрики для удалённых/остановленных сайтов. | run |

## `step_cron_night/404_errors/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `404_errors.py` | 1 | v6_ch 404 errors builder from raw_data.metrika_yandex_not_found_daily. | run, main |
| `CLAUDE.md` | 7 | CLAUDE.md — 404_errors |  |
| `README.md` | 7 | 404_errors — Сбор 404-ошибок через Яндекс.Метрику |  |
| `recheck_404.py` | 9 | Перепроверка URL из yandex_direct_404_errors реальным HTTP-кодом. | clean_url, is_soft_404, check_url, fetch_urls, main |

## `step_cron_night/direct_account_reviews/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `CLAUDE.md` | 5 | direct_account_reviews — Отзывы Яндекс.Директ |  |
| `README.md` | 7 | direct_account_reviews — Отзывы Яндекс.Директ |  |
| `__init__.py` | 1 |  |  |
| `fetch_direct_stats.py` | 14 | direct_account_reviews/fetch_direct_stats.py | ensure_table, ensure_agency_column, get_logins, get_login_date_from, save_agency, delete_and_insert, fetch_rep |
| `load_reviews.py` | 6 | direct_account_reviews/load_reviews.py | parse_records, load_to_db, main |
| `load_reviews_to_big_analytics.py` | 11 | direct_account_reviews/load_reviews_to_big_analytics.py | main |
| `pipeline.py` | 1 | direct_account_reviews/pipeline.py |  |

## `step_cron_night/korrektirovki/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `CLAUDE.md` | 4 | korrektirovki — Корректировки ставок Яндекс.Директ |  |
| `README.md` | 6 | korrektirovki — Корректировки ставок Яндекс.Директ |  |
| `__init__.py` | 1 |  |  |
| `korrektirovki.py` | 19 | korrektirovki/korrektirovki.py | main |
| `run.py` | 1 | korrektirovki/run.py |  |

## `step_cron_night/report_placement/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `CLAUDE.md` | 12 | report_placement — Документация папки |  |
| `README.md` | 8 | report_placement — Отчёт по площадкам Яндекс.Директ |  |
| `__init__.py` | 1 |  |  |
| `run.py` | 2 | step_cron_night/report_placement/run.py — точка входа: запускает step1 → step2 последовательно. | run |
| `step1_fetch_direct.py` | 39 |  | send_telegram, create_session, ensure_table_exists, get_incremental_date_from, delete_rows_from, load_gsheet_s |
| `step2_build_analytics.py` | 19 |  | build_enrich_direct_sql, build_insert_leads_only_sql, send_telegram, main |

## `step_cron_night/step13_utm_direct_audit/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `CLAUDE.md` | 7 | utm_direct_audit — UTM-аудит Яндекс.Директ |  |
| `README.md` | 9 | step13_utm_direct_audit — UTM-аудит Яндекс.Директ |  |
| `__init__.py` | 1 |  |  |
| `run.py` | 1 | v6_ch UTM audit builder from raw_data.metrika_yandex_utm_daily. | run, main |

## `tests/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `__init__.py` | 1 |  |  |

## `tests/data_check/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `__init__.py` | 1 |  |  |
| `test_funnel.py` | 1 |  | test_valid_funnel_passes, test_kval_gt_korr_flagged, test_prodazhi_without_priezd_flagged, test_dobro_gt_dohod |
| `test_reporter.py` | 1 |  | test_report_shows_critical_count, test_report_shows_stale_warning |

## `tools/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `diagnose_pipeline.py` | 9 | Read-only diagnostics for big_analytics_v5 pipeline performance and coverage. | table_sizes, recent_heavy_steps, login_coverage, missing_login_details, parse_args, main |
| `restore_pbi_star_from_arrival.py` | 15 | Restore the local Power BI star-facing objects from big_analytics_full_arrival. | parse_args, db_params, qcount, main |

## `yandex_direct_checking_report/`

| файл | КБ | назначение | что внутри |
|---|---|---|---|
| `CLAUDE.md` | 5 | yandex_direct_checking_report — CLAUDE.md |  |
| `README.md` | 7 | yandex_direct_checking_report |  |
| `report.py` | 20 | yandex_direct_checking_report/report.py — независимый отчёт сверки расходов Директа | get_date_range, fetch_report_with_token_rotation, parse_tsv_monthly, get_active_accounts, insert_rows, run_com |

