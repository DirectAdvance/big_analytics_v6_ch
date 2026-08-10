# RUNBOOK.md — v6_ch operations

Операционка для активного `big_analytics_v6_ch` ClickHouse-контура. Production v5 на
Victory (`~/big_analytics_v5`, PostgreSQL) — отдельный проект. v6 запускается на том же Victory
VPS, но из отдельной папки `~/big_analytics_v6_ch` и отдельного Python `~/venv-v6`.

## 1. Подключение

| Что | Как |
|---|---|
| ClickHouse v6 | `config.ch_db.get_client()` / `~/venv-v6/bin/python3` из `~/big_analytics_v6_ch` на Victory |
| Секреты | `.secret/loader.py`, ключ `victory_clickhouse` |
| v5 для read-only сверки | `data_check/compare/run.py` сам открывает read-only PostgreSQL-сессию |
| Dashboard launch | LXC101 `work.service` → `ssh victory` → `server=victory_v6` |

Проверка подключения:

```bash
ssh victory "cd ~/big_analytics_v6_ch && ~/venv-v6/bin/python3 config/ch_db.py"
```

## 2. Запуск пайплайнов

```bash
# Основной дневной ручной прогон.
~/venv-v6/bin/python3 -u ~/big_analytics_v6_ch/pipeline.py

# Дневной прогон вместе с maintenance steps 2/7.
~/venv-v6/bin/python3 -u ~/big_analytics_v6_ch/pipeline.py --include-maintenance

# Продолжить с шага.
~/venv-v6/bin/python3 -u ~/big_analytics_v6_ch/pipeline.py --from-step=12

# Один шаг.
~/venv-v6/bin/python3 -u ~/big_analytics_v6_ch/pipeline.py --only-step=145

# Ночной ClickHouse pipeline.
~/venv-v6/bin/python3 -u ~/big_analytics_v6_ch/step_cron_night/pipeline_night.py
```

Долгий локальный запуск вести через `nohup` с логом:

```bash
cd ~/big_analytics_v6_ch
nohup ~/venv-v6/bin/python3 -u ~/big_analytics_v6_ch/pipeline.py > logs/pipeline_full_$(date +%Y%m%d_%H%M%S).log 2>&1 &
```

## 3. Мониторинг локального прогона

```bash
ps -axo pid,etime,command | rg 'big_analytics_v6_ch|pipeline.py' | rg -v 'rg '
tail -40 logs/<pipeline-log>.log
```

Паспорт шагов в ClickHouse:

```bash
.venv/bin/python3 - <<'PY'
from config.ch_db import get_client
client = get_client()
rows = client.query("""
    SELECT run_id, step, status, duration_sec, details, run_at
    FROM ad_analytics.data_quality_log
    ORDER BY run_at DESC
    LIMIT 30
""").result_rows
for row in rows:
    print(row)
PY
```

## 4. Проверки после прогона

```bash
~/venv-v6/bin/python3 data_check/verify_big_analytics.py --full
~/venv-v6/bin/python3 data_check/compare/run.py
~/venv-v6/bin/python3 data_check/compare/run.py --json > logs/compare_v5_v6_$(date +%Y%m%d_%H%M%S).json
```

`verify_big_analytics.py` проверяет v6 ClickHouse golden/invariants.
`data_check/compare/run.py` сверяет durable mart v5 `public.fact_big_analytics`
с v6 `ad_analytics.fact_big_analytics` read-only.

## 5. Что считать нормальным

- `pipeline.py` без флагов пропускает maintenance steps `2` и `7`; это не сбой.
- Step14 `minus_snapshot` не входит в дневной прогон без `--include-nightly`.
- Victory — только launch host: держать код, `~/venv-v6` и логи. Не писать локальные дампы,
  parquet/csv-кэши или raw-слои на Victory; такие данные должны жить в Yandex Cloud ClickHouse.
- v5 staging/raw таблицы после cleanup могут быть пустыми; для v5↔v6 сверки использовать
  durable `fact_big_analytics`, а не transient `big_analytics_full`.
- Golden v6 имеет отдельный допуск и причины в `GOLDEN_BASELINE.md`; v5-допуск не переносить
  вслепую.

## 6. Типовые реакции

| Симптом | Что делать |
|---|---|
| `ClickHouse preflight failed` на step0 | Проверить отсутствующий/пустой `raw_data.*` или manual input из сообщения ошибки. |
| `verify_big_analytics` FAIL | Читать точный блок FAIL в логе, затем сверить `KNOWN_ISSUES.md` и `GOLDEN_BASELINE.md`. |
| `data_check/compare/run.py` FAIL | Сохранить stdout/json в `logs/`, разложить по totals и drilldown; обновить отчёт сверки. |
| `MEMORY_LIMIT_EXCEEDED` на `Dim_AdGroup` | Известный риск star step 145; не ретраить вслепую, сначала смотреть `STATE.md`/`KNOWN_ISSUES.md`. |
| Power BI refresh нужен | `refresh_powerbi.py` существует, но v6 publishing/refresh проверять отдельно по `PBI_TABLES.md`; не запускать v5 `pipeline_powerbi.py`. |

## 7. Документы после инцидента или сверки

Обновлять только активные релевантные MD:

- `STATE.md` — короткий handoff текущего состояния.
- `RAW_DIFF_FINDINGS.md` — свежая raw-сверка v5↔v6.
- `KNOWN_ISSUES.md` — новые открытые/закрытые расхождения.
- `GOLDEN_BASELINE.md` — только если фактически менялся golden-допуск/интерпретация.
- `PIPELINES.md` / `README.md` — только если менялся способ запуска или состав шагов.
