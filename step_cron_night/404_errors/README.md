# 404_errors — v6 ClickHouse

Активные ночные шаги:

- `404_errors.py` строит `ad_analytics.yandex_direct_404_errors` из `raw_data.metrika_yandex_not_found_daily`.
- `recheck_404.py` перепроверяет URL реальным HTTP-кодом и удаляет из ClickHouse только явно живые URL.

Оба шага работают без PostgreSQL и входят в `step_cron_night/pipeline_night.py`.

Проверки:

```bash
python3 step_cron_night/pipeline_night.py --only-step 104 --no-tg
python3 step_cron_night/pipeline_night.py --only-step 105 --no-tg
```

`recheck_404.py` использует `DELETE ... WHERE url IN (...)` с `mutations_sync=1`, чтобы дождаться CH mutation
перед итоговым count.
