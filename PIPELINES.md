# PIPELINES.md — v6_ch ClickHouse pipelines

Этот файл описывает активный контур `big_analytics_v6_ch`. v6 запускается на Victory из
`~/big_analytics_v6_ch` через `~/venv-v6`; данные и тяжёлые кэши живут в Yandex Cloud ClickHouse.
Victory `~/big_analytics_v5`, `fast_pipeline.py`, `pipeline_powerbi.py`, PostgreSQL `UNLOGGED`,
FDW и `VACUUM` относятся к v5/legacy и не являются инструкциями для v6.

## Команды запуска

```bash
# Дневной ручной пайплайн v6_ch.
~/venv-v6/bin/python3 -u ~/big_analytics_v6_ch/pipeline.py

# То же, но с maintenance steps 2 и 7.
~/venv-v6/bin/python3 -u ~/big_analytics_v6_ch/pipeline.py --include-maintenance

# С конкретного шага до конца.
~/venv-v6/bin/python3 -u ~/big_analytics_v6_ch/pipeline.py --from-step=12

# Один шаг.
~/venv-v6/bin/python3 -u ~/big_analytics_v6_ch/pipeline.py --only-step=145

# Low-memory/debug: пропустить тяжёлый PBI/star/feed/spend хвост.
~/venv-v6/bin/python3 -u ~/big_analytics_v6_ch/pipeline.py --skip-heavy-pbi

# Ночной ClickHouse pipeline.
~/venv-v6/bin/python3 -u ~/big_analytics_v6_ch/step_cron_night/pipeline_night.py
~/venv-v6/bin/python3 -u ~/big_analytics_v6_ch/step_cron_night/pipeline_night.py --list-steps
~/venv-v6/bin/python3 -u ~/big_analytics_v6_ch/step_cron_night/pipeline_night.py --only-step=114
```

## Важные флаги дневного pipeline.py

| Флаг | Поведение |
|---|---|
| без флагов | Выполняет все обычные шаги, кроме maintenance `{2, 7}` и nightly step `14`. |
| `--include-maintenance` | Добавляет step2 `OPTIMIZE RAW` и step7 `OPTIMIZE full/sources/calls`. |
| `--include-nightly` | Добавляет step14 minus snapshot в дневной прогон. По умолчанию выключено. |
| `--skip-heavy-pbi` | Пропускает steps `{140,141,142,143,1431,1432,144,145,1451,146,147,148}`. |
| `--no-parallel-safe` | Отключает safe background-запуск step14, если step14 включён. |

По умолчанию `pipeline.py` пишет паспорт шагов в
`ad_analytics.data_quality_log` (`run_id`, `step`, `status`, `duration_sec`, `details`).

<a id="steps-map"></a>
## Карта шагов pipeline.py

| Шаг | Модуль | Label в логе | Что делает |
|---:|---|---|---|
| 0 | `step0_sync_local.step0` | `step0` | ClickHouse preflight: проверяет обязательные `raw_data.*` и manual inputs в `ad_analytics.*`. |
| 1 | `step1_load_raw.step1` | `step1` | Пересоздаёт `ad_analytics.raw_*` из `raw_data.*`. |
| 2 | `step2_indexes.step2` | `step2` | Maintenance: `OPTIMIZE TABLE ... FINAL` для RAW-слоя. Только с `--include-maintenance`. |
| 4 | `step4_campaign_status.step4` | `step4` | Строит `campaign_status` из `raw_data.direct_campaigns`. |
| 3 | `step3_build_sources.step3` | `step3` | Собирает source-слой direct/seo/pixel/calls и связанные промежуточные таблицы. |
| 31 | `corrections` | `corrections` | Применяет портированные правила коррекций v5 к v6 ClickHouse-слою. |
| 5 | `step5_build_pixel.build_pixel` | `step5` | Собирает pixel-воронку. |
| 6 | `step6_build_full.step6` | `step6` | Собирает `big_analytics_full`. |
| 7 | `step7_finalize.step7` | `step7` | Maintenance: `OPTIMIZE` для `big_analytics_sources/calls/full`. Только с `--include-maintenance`. |
| 9 | `step9_direct_history.step9` | `step9` | История Директа из `raw_data.direct_campaigns`. |
| 10 | `step10_crop_targeting.step10` | `step10` | Посевы Telega/VK/MAX и связанные локальные CH-таблицы. |
| 11 | `step11_pixel_score.step11` | `step11` | Pixel score и доливка `_source_table='пиксель_атрибуц'`. |
| 115 | `spec_fallback` | `spec_fallback` | Каскад `специалист` по домену без окна дат после step10/step11. |
| 12 | `step12_proverka_big_analytics.step12` | `step12` | Проверка CRM-маппингов. |
| 13 | `step13_arrival.step13` | `step13` | Воронка по дате визита → `big_analytics_full_arrival`. |
| 131 | `step13_arrival.build_unified` | `build_unified` | UNION заявочной и визитной осей → `big_analytics_unified`. |
| 139 | `direct_placement_links.build` | `direct_placement_links` | Справочник placement links для Direct/посевных разрезов. |
| 140 | `spend.build_direct_spend_staging` | `direct_spend_staging` | Staging расходов Директа для spend-витрин. |
| 141 | `region_spend.build_region_spend` | `region_spend` | `fact_region_spend`. |
| 142 | `adformat_spend.build_adformat_spend` | `adformat_spend` | `fact_adformat_spend`. |
| 143 | `criterion_spend.build_criterion_spend` | `criterion_spend` | `fact_criterion_spend`. |
| 144 | `direct_feed_funnel.build` | `direct_feed_funnel` | `fact_direct_feed_funnel`. |
| 1431 | `region_spend.build_region_zayavki` | `region_zayavki` | Заявочная воронка по регионам. |
| 1432 | `criterion_spend.build_criterion_zayavki` | `criterion_zayavki` | Заявочная воронка по критериям. |
| 145 | `star_refactor.build_star` | `build_star` | `fact_big_analytics`, `Dim_*`, `arp_fact`, `fact_vk_ads`. |
| 1451 | `star_refactor.build_star_extensions` | `build_star_extensions` | Расширения star-слоя. |
| 148 | `star_refactor.cleanup_wide_intermediates` | `cleanup_wide_intermediates` | Очистка wide-промежуточных таблиц. |
| 146 | `star_refactor.build_pbi_compat` | `build_pbi_compat` | PBI compatibility layer. |
| 147 | `spend.cleanup_direct_spend_staging` | `direct_spend_staging_cleanup` | Удаляет spend staging. |
| 14 | `step14_minus_snapshot.step14` | `step14` | Nightly/default-off: снапшот минус-фраз. |
| 8 | `step8_stats.step8` | `step8` | Финальная статистика. |
| 900 | `data_check.verify_big_analytics` | `verify` | Golden/invariant gate v6. |

## Ночной pipeline_night.py

Ночной пайплайн v6_ch работает с ClickHouse и пишет в тот же
`ad_analytics.data_quality_log`.

| Step | Модуль | Label |
|---:|---|---|
| 101 | `step_cron_night.metrika_yandex` | `night_metrika_yandex` |
| 102 | `step_cron_night.step13_utm_direct_audit.run` | `night_check_utm` |
| 103 | `step_cron_night.korrektirovki.run` | `night_korrektirovki` |
| 104 | `step_cron_night.404_errors.404_errors` | `night_404_errors` |
| 105 | `step_cron_night.404_errors.recheck_404` | `night_recheck_404` |
| 106 | `step_cron_night.build_ml_korrektirovki_night` | `night_ml_korrektirovki` |
| 114 | `step14_minus_snapshot.step14` | `night_minus_snapshot` |

Legacy PG jobs `direct_account_reviews`, `report_placement`, old `build_spend_daily`
and `revoke_metrika_grants` лежат в `archive/postgres_legacy_2026_07_31/` и не входят
в активный v6 night, пока не портированы на `raw_data`/`ad_analytics`.

## Проверки после прогона

```bash
.venv/bin/python3 data_check/verify_big_analytics.py --full
.venv/bin/python3 data_check/compare/run.py
```

Для сверки с v5 `data_check/compare/run.py` читает v5 PostgreSQL только read-only,
а v6 ClickHouse только SELECT.
