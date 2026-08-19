# step8_stats — ClickHouse-статистика (step8) + standalone Postgres drift-снимки

`step8.py` — рядовой шаг `pipeline.py::STEPS` (последний из содержательных, перед `verify`).
Read-only: считает строки в `ad_analytics.*` (ClickHouse) и логирует агрегаты
`big_analytics_full`. **Telegram не отправляет.**

`pipeline_log_snapshot.py` и `funnel_drift_snapshot.py` в `pipeline.py::STEPS` **не вызываются**.
Grep по репозиторию: `funnel_drift_snapshot` — только в
`tests/test_telegram_notifications.py:241`; `pipeline_log_snapshot` — нигде вне своего файла (нет
даже в тестах). Это PostgreSQL-скрипты (`config.db`, `public.big_analytics_unified`) для
legacy-контура, запускаются вручную/standalone.
`funnel_drift_snapshot.py` при этом активно поддерживается — шлёт Telegram-алерт дрейфа.

## Назначение

| Действие | Файл | Вызывается из pipeline.py? |
|----------|------|------|
| Посчитать строки `ad_analytics.*` (ClickHouse) + агрегаты `big_analytics_full` | `step8.py` | да, `STEPS` |
| Записать снимок воронки в `data_pipeline_log` (Postgres) | `pipeline_log_snapshot.py` | нет — standalone |
| Записать снимок + Telegram-алерт дрейфа в `data_funnel_drift_log` (Postgres) | `funnel_drift_snapshot.py` | нет — standalone |

## Архитектурная схема

```
step8.py (ClickHouse, IN только):
ad_analytics.raw_yandex / raw_leads / raw_calls / big_analytics_sources /
big_analytics_calls / big_analytics_full / big_analytics_pixel_score /
big_analytics_full_arrival / big_analytics_unified / fact_big_analytics
        │  count_rows() + table_exists() (config/ch_utils.py)
        ▼
   logger.info(...)   — нет OUT (длительность пишет generic run_step() в pipeline.py)

pipeline_log_snapshot.py / funnel_drift_snapshot.py (Postgres, standalone, НЕ из pipeline.py):
public.big_analytics_unified
        │
        ├──► data_pipeline_log            (pipeline_log_snapshot.py)
        │        │
        │        ▼
        │    Дашборд → /api/pipeline-delta → виджет "Дельта пайплайна"
        │
        └──► data_funnel_drift_log + Telegram-алерт (funnel_drift_snapshot.py,
             через notifications/telegram.py::send_html, SOCKS5-прокси-цепочка)
```

## step8.py — вывод (лог, не Telegram)

`step8.py` больше не отправляет отчёт в Telegram — только логирует построчно (`step8.py:37-43`
построчно по таблицам, `:58-61` агрегаты `big_analytics_full`, `:64` итоговая длительность):

```
INFO pipeline.step8:   big_analytics_full: 2634521 строк
INFO pipeline.step8:   fact_big_analytics: 712345 строк
...
INFO pipeline.step8:   full metrics: cost=12345678.0 z=45678 korr=12345 kval=... priezd=... prodazhi=...
INFO pipeline.step8: Шаг 8 v6_ch завершён за 4.1 сек
```

`STEP_LABELS` и человекочитаемые лейблы шагов в этой папке не существуют — весь пер-шаговый
итоговый отчёт с длительностями по `STEP_LABELS` был частью старой v5-версии step8, в v6_ch
он не перенесён. Длительность самого step8 пишет generic `run_step()` из `pipeline.py`, никакого
self-tracking hack (synthetic entry) в `step8.py` нет.

## Telegram — только `funnel_drift_snapshot.py`

Единственный отправитель Telegram в этой папке — `funnel_drift_snapshot.py::_send_drift_alert()`.
Он строит HTML-текст и отдаёт его целиком в общий `notifications/telegram.py::send_html()`:

```python
send_html(text, bot_token=TELEGRAM_BOT_TOKEN, chat_id=TELEGRAM_CHAT_ID,
          proxy_variants=TELEGRAM_PROXY_VARIANTS, collapse_whitespace=False, timeout=15)
```

- Чанкинг >4096 симв. и ротация прокси (`TELEGRAM_PROXY_VARIANTS`, Amsterdam→DE→NL→FR→direct,
  маркер `TG_PROXY_CHAIN_ROTATION_2026-06-17`) — внутри `send_html()`, локального
  `_split_chunks`/`_send_one`/`send_telegram()` в этой папке больше нет.
- `collapse_whitespace=False` — обязателен: без него санитайзер схлопывает отступы иерархии
  месяц→источник→метрика в один пробел (маркер `WHITESPACE_IS_CONTENT_2026-08-14`).
- Суммы в тексте — через `format_ru_amount()` (NNBSP-разделитель тысяч, запятая-десятичная),
  не голый `f'{val:,}'`.
- `timeout=15` — восстановлен под исходный `requests.post(..., timeout=15)` (после миграции на
  `send_html` таймаут по умолчанию был 10s).

## `pipeline_log_snapshot.py`

Отдельный скрипт. Пишет снимок воронки в `data_pipeline_log`:

```sql
INSERT INTO data_pipeline_log (run_id, month, cost, ...)
SELECT %s AS run_id,
       date_trunc('month', "Date")::date AS month,
       SUM(total_cost), SUM(kol_vo_zayavok), SUM(korr), SUM(kval), SUM(priezd), SUM(prodazhi),
       ...
FROM public.big_analytics_unified           -- SNAPSHOT_ON_UNIFIED_2026-06-20
WHERE direction = 'Авто'
  AND (_source_table IS NULL OR _source_table <> 'pixel')
  AND атрибуция = 'По дате заявки'           -- DELTA_AXIS_FIX_2026-07-10: только заявочная ось
GROUP BY date_trunc('month', "Date")
ON CONFLICT (run_id, month) DO NOTHING
```

Используется виджетом "Дельта пайплайна" в дашборде (`/api/pipeline-delta`).

## Параметры

`config/tokens.py`:
- `TELEGRAM_BOT_TOKEN` — токен бота для отчётов
- `TELEGRAM_CHAT_ID` — чат `336635373` (личный)
- `TELEGRAM_PROXY` — SOCKS5 URL (опционально)

`config/settings.py`:
- `T_DATA_QUALITY_LOG` = `'data_quality_log'`
- `DATE_FROM` — для фильтра воронки

## Зависимости

- `step8.py`: `config/ch_db.py` (ClickHouse client), `config/ch_utils.py` (`count_rows`/`table_exists`)
- `pipeline_log_snapshot.py`/`funnel_drift_snapshot.py`: `config/db.py` (Postgres), `requests` + прокси для Telegram (только funnel_drift)

## Примеры запуска

```bash
# Только step8 (реальный шаг пайплайна):
ssh victory "cd ~/big_analytics_v6_ch && ~/venv-v6/bin/python3 pipeline.py --only-step=8"

# В составе полного пайплайна:
ssh victory "cd ~/big_analytics_v6_ch && ~/venv-v6/bin/python3 pipeline.py"

# Только снимок воронки (standalone, не часть pipeline.py):
ssh victory "cd ~/big_analytics_v6_ch && ~/venv-v6/bin/python3 step8_stats/pipeline_log_snapshot.py"
```

## Проверки после запуска

```sql
-- ClickHouse: все шаги (включая step8) попали в лог за последний run
-- (запись делает generic run_step() из pipeline.py для КАЖДОГО шага, не step8.py сам)
SELECT step, duration_sec, status FROM ad_analytics.data_quality_log
WHERE run_id = (SELECT run_id FROM ad_analytics.data_quality_log ORDER BY run_at DESC LIMIT 1)
ORDER BY run_at;
```

```sql
-- Postgres: снимки воронки растут (по run_at) — только если pipeline_log_snapshot.py запускали вручную
SELECT run_at, COUNT(*) AS months FROM data_pipeline_log
GROUP BY 1 ORDER BY 1 DESC LIMIT 10;
```

## Связи

- **step8.py:** ClickHouse read-only, независим, стоит последним из содержательных шагов `STEPS`.
- **pipeline_log_snapshot.py / funnel_drift_snapshot.py:** standalone, вне `pipeline.py::STEPS`.

## Файлы

| Файл | Описание |
|------|----------|
| `step8.py` | ClickHouse-статистика (row counts + агрегаты), без Telegram |
| `pipeline_log_snapshot.py` | Снимок воронки в `data_pipeline_log` (Postgres, standalone) |
| `funnel_drift_snapshot.py` | Снимок по (month × источник) в `data_funnel_drift_log` + Telegram-алерт дрейфа (Postgres, standalone) |
| `__init__.py` | Пустой |
| `CLAUDE.md` | Краткая инструкция для ИИ |
| `README.md` | Этот файл |
