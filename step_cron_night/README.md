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

## Weekly-only night steps

Not run by a plain `pipeline_night.py` (excluded via `WEEKLY_DEFAULT_STEPS`, mirrors
`pipeline.py`'s `NIGHTLY_DEFAULT_STEPS`/`--include-nightly`). Reached with `--only-step` on
their own weekly cron line, or with `--include-weekly` for a manual full run.

| # | Step | Result |
|---|------|--------|
| 107 | `night_direct_account_reviews` | `ad_analytics.yandex_direct_account_reviews`, `ad_analytics.yandex_direct_reports_reviews` |

## Commands

```bash
python3 step_cron_night/pipeline_night.py
python3 step_cron_night/pipeline_night.py --list-steps
python3 step_cron_night/pipeline_night.py --only-step 114
python3 step_cron_night/pipeline_night.py --only-step 107   # weekly: direct_account_reviews
```

## Cron

На Victory включён с 2026-08-17:

```cron
10 18 * * * cd ~/big_analytics_v6_ch && /usr/bin/flock -n /tmp/ba6_night.lock ~/venv-v6/bin/python3 step_cron_night/pipeline_night.py >> /tmp/ba6_night.log 2>&1
```

Это 23:10 Екб: после дневных прогонов и до БА5 night в 21:00 UTC. Проверенный ручной прогон перед
постановкой в cron: PASS за 14м15с.

`CHECK_UTM_BATCH_DAYS=7` по умолчанию. Уменьшать можно для отладки, увеличивать только после проверки
памяти ClickHouse.

**Weekly `direct_account_reviews` (шаг 107) — финальная строка, ставит main session после
ревью:**

```cron
0 19 * * 0 cd ~/big_analytics_v6_ch && /usr/bin/flock -n /tmp/ba6_night.lock ~/venv-v6/bin/python3 step_cron_night/pipeline_night.py --only-step 107 >> /tmp/ba6_night_reviews.log 2>&1 || echo "$(date -u +\%FT\%TZ) ba6_night_reviews SKIPPED_OR_FAILED (lock busy or pipeline_night.py exited non-zero — check this file's content above and /tmp/ba6_night.log for the daily job)" >> /tmp/ba6_night_reviews.log
```

Воскресенье 19:00 UTC (был понедельник 02:00 МСК в BA5) — после того как ежедневный ночной
прогон `10 18 * * *` успевает закончиться (замер 14м15с, конец ~18:24 UTC, 36 минут запаса), и
до BA5 night в 21:00 UTC. Замер живого прогона шага 107 — **1ч22м** (director rework
2026-08-24, не ~40 мин, как считалось раньше при постановке слота): старт 19:00 UTC, конец
~20:22 UTC, 38 минут запаса до BA5 21:00 UTC — слот подтверждён верным при новой длительности.

Тот же `flock`-файл `/tmp/ba6_night.lock`, что и у ежедневной строки: `pipeline_night.py`
внутри уже берёт общий `pipeline_mutex.acquire("ba6_night", ...)` независимо от `--only-step`,
поэтому weekly и daily прогоны не должны драться за один и тот же ClickHouse-инстанс (2 vCPU) —
разный `flock`-файл тут был бы дырой, а не защитой. Коллизия (daily всё ещё держит
`/tmp/ba6_night.lock`) означала бы, что weekly молча не запускается: внешний `flock -n` не
пишет ни в лог, ни в Telegram при отказе взять лок (в отличие от внутреннего
`pipeline_mutex`, который уведомляет). `|| echo ... >> log` выше делает сам факт пропуска
видимым в `/tmp/ba6_night_reviews.log`, независимо от того, был это lock-busy или реальный
сбой `pipeline_night.py` — с F9 (staleness warning теперь доходит до Telegram-сводки дневного
прогона, см. `step0_sync_local/step0.py::_check_reviews_freshness`) многонедельная незамеченная
протухшая `yandex_direct_reports_reviews` обнаружится за дни, а не месяцы, даже если этот лог
никто не откроет вручную.

## Telegram

Уведомления идут через `config.tokens` в тот же чат бота `@analitika_auto_powerbi_bot`: старт,
ошибка шага, финальная сводка.

## Legacy not ported

`direct_account_reviews` — CH-native с 2026-08-24 (шаг 107 выше), больше не PostgreSQL-мост;
старая версия осталась только в `archive/postgres_legacy_2026_07_31/step_cron_night/` для
справки (Sheets/API-логика, не запускать — пишет в `public.*` на Victory PG, которого больше не
читает пайплайн).

`report_placement/run.py` — единственный оставшийся неперенесённый weekly v5 job, лежит в
`archive/postgres_legacy_2026_07_31/step_cron_night/`, не подключён как CH live-fetch. Не
запускать из v6 ночного пайплайна, пока не переписан с PostgreSQL на `raw_data`/`ad_analytics`.
