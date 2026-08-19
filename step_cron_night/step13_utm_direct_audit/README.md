# step13_utm_direct_audit — v6 ClickHouse

Активный ночной шаг: `run.py`.

В v6 UTM-аудит не ходит в Direct/Metrika API. Он строит таблицы из ClickHouse raw snapshots:

- source: `raw_data.metrika_yandex_utm_daily`
- source: `reference_data.metrika_yandex_counters`
- source: `reference_data.gsheet_sites`
- target: `ad_analytics.check_utm`
- target: `ad_analytics.check_utm_fuck_direct`

По умолчанию чанки недельные:

```bash
CHECK_UTM_BATCH_DAYS=7
```

Это снижает число insert-select запросов с дневных ~212 до недельных ~31 на период с 2026-01-01.

Проверка:

```bash
python3 step_cron_night/pipeline_night.py --only-step 102 --no-tg
```
