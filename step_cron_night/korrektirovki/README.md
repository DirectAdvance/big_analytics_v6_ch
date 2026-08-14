# korrektirovki — v6 ClickHouse

Активный ночной шаг: `run.py`.

В v6 этот шаг не ходит в Direct API и не пишет в PostgreSQL. Он обновляет совместимую BI-витрину:

- source: `raw_data.yandex_direct_korrektirovki`
- target: `ad_analytics.yandex_direct_korrektirovki` (`VIEW`)

Raw-таблица должна обновляться upstream raw pipeline. Старый v5 API/PG-скрипт перенесён в
`archive/postgres_legacy_2026_07_31/step_cron_night/korrektirovki_pg.py`.

Проверка:

```bash
python3 step_cron_night/pipeline_night.py --only-step 103 --no-tg
```
