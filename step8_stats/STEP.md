# STEP.md — Шаг 8: ClickHouse-статистика (step8)

## Что делает

1. Считает строки в ClickHouse-таблицах `ad_analytics.*` из списка `TABLES` (`step8.py`) —
   пропускает отсутствующие через `table_exists()`.
2. Логирует агрегаты `big_analytics_full` (`sum(total_cost)`, `kol_vo_zayavok`, `korr`, `kval`,
   `priezd`, `prodazhi`).
3. Ничего не отправляет в Telegram и не пишет в БД сам — только `logger.info(...)`.

Длительность/статус шага в `ad_analytics.data_quality_log` пишет общий `run_step()` из
`pipeline.py` (одинаково для всех шагов, не специфично для step8).

## Пример лога

```
INFO pipeline.step8:   big_analytics_full: 2634521 строк
INFO pipeline.step8:   full metrics: cost=45678900.0 z=98432 korr=... kval=... priezd=8901 prodazhi=1234
INFO pipeline.step8: Шаг 8 v6_ch завершён за 4.1 сек
```

Никакого HTTP-шлюза / Telegram-gateway в `step8.py` нет — единственный отправитель Telegram в
этой папке — `funnel_drift_snapshot.py` (standalone, не вызывается из `pipeline.py`), см. `CLAUDE.md`.
