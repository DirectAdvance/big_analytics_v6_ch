# step_cron_night — v6 ClickHouse night pipeline

Ночной контур v6 запускает тяжёлые диагностические/API-совместимые шаги отдельно от дневного
`pipeline.py`, чтобы не удлинять основной прогон и не дергать Direct API для минус-фраз каждый раз.

## Daily night steps

| # | Step | Result |
|---|------|--------|
| 101 | `night_metrika_yandex` | `ad_analytics.metrika_yandex` |
| 102 | `night_check_utm` | `ad_analytics.check_utm`, `ad_analytics.check_utm_fuck_direct` |
| 103 | `night_korrektirovki` | `ad_analytics.yandex_direct_korrektirovki` view |
| 104 | `night_404_errors` | `ad_analytics.yandex_direct_404_errors` |
| 105 | `night_recheck_404` | cleans live URLs from `yandex_direct_404_errors` |
| 106 | `night_ml_korrektirovki` | `ad_analytics.fact_ml_korrektirovki` |
| 114 | `night_minus_snapshot` | `ad_analytics.yandex_direct_minus_snapshot` |

## Commands

```bash
python3 step_cron_night/pipeline_night.py
python3 step_cron_night/pipeline_night.py --list-steps
python3 step_cron_night/pipeline_night.py --only-step 114
```

`CHECK_UTM_BATCH_DAYS=7` по умолчанию. Уменьшать можно для отладки, увеличивать только после проверки
памяти ClickHouse.

## Telegram

Уведомления идут через `config.tokens` в тот же чат бота `@analitika_auto_powerbi_bot`: старт,
ошибка шага, финальная сводка.

## Legacy not ported

Weekly v5 jobs `direct_account_reviews/pipeline.py` и `report_placement/run.py` перенесены в
`archive/postgres_legacy_2026_07_31/step_cron_night/` и ещё не подключены как CH live-fetch. Их нельзя
запускать из v6 ночного пайплайна, пока они не переписаны с PostgreSQL на `raw_data`/`ad_analytics`.
