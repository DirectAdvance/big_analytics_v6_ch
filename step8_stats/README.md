# step8_stats — Финальная статистика + Telegram-отчёт

Шаг 8 (последний) пайплайна. Собирает статистику по всем выполненным шагам, формирует итоговый Telegram-отчёт и записывает снимок воронки в `data_pipeline_log`.

## Назначение

| Действие | Файл |
|----------|------|
| Собрать длительности шагов из `data_quality_log` | `step8.py` |
| Посчитать объёмы таблиц (full, direct, seo, pixel, crop, reviews) | `step8.py` |
| Отправить отчёт в Telegram | `step8.py` |
| Записать снимок воронки в `data_pipeline_log` | `pipeline_log_snapshot.py` |

## Архитектурная схема

```
data_quality_log              ─┐
big_analytics_*               ─┼─► step8.run()
yandex_direct_manager_reports ─┤        │   (FDW; step8.py:507-513, маркер RECON_FIX_2026-06-19)
fact_big_analytics            ─┤        │   (step8.py:519+, RECON_FIX_2026-06-19)
campaign_status               ─┘        │
                          ├──► Telegram (через SOCKS5-прокси)
                          │
                          └──► data_pipeline_log (snapshot воронки)
                                                       │
                                                       ▼
                                        Дашборд → /api/pipeline-delta
                                        Виджет "Дельта пайплайна"
```

## Telegram-отчёт

```
🟢 big_analytics_v5 pipeline complete

⏱ Длительность шагов:
  Синхронизация: 12.3s
  Загрузка RAW: 8.1s
  Индексы: 5.4s
  Источники: 124.5s
  Корректировки: 67.2s
  Статусы кампаний: 6m 19s
  big_analytics_full: 89.6s
  ...
  Статистика: 4.1s
Всего: 28m 12s

📊 Объёмы:
  big_analytics_full: 2,634,521 строк
  big_analytics_direct: 712,345
  big_analytics_seo: 14,567
  big_analytics_crop_targeting: 16,234
  ...

📈 Воронка (direction='Авто', !пиксель_атрибуц):
  Расход: 12,345,678 ₽
  Обращения: 45,678
  Заявки: 12,345
  ...
```

Сообщение > 4096 символов автоматически режется на чанки.

## STEP_LABELS

Все имена шагов имеют человекочитаемые лейблы:

| Имя шага | Лейбл |
|----------|-------|
| `step0` | Синхронизация |
| `step1` | Загрузка RAW |
| `step2` | Индексы |
| `step3` | Источники |
| `corrections` | Корректировки |
| `sync_pixel_config` | Конфиг пикселей |
| `step5` | Пиксели (build_pixel) |
| `step4` | Статусы кампаний |
| `step6` | big_analytics_full |
| `step7` | Финализация |
| `step9` | История Директа |
| `step10` | Посевы Telega.in |
| `load_reviews` | Загрузка отзывов |
| `load_crop` | Загрузка посевов |
| `404_errors` | 404 ошибки |
| `normalize_salons` | Нормализация салонов |
| `cleanup_old_dates` | Очистка старых дат |
| `step11` | Атрибуция пикселя (score) |
| `step11_pixel_score` | Атрибуция пикселя (score) |
| `step8` | Статистика |

Если шаг отсутствует в `STEP_LABELS` — в отчёте появится сырое имя (`step_NN:`).

## Self-tracking

step8 не может писать свою собственную длительность в `data_quality_log` до формирования отчёта (запись делается после `run_step`). Решение — synthetic entry в `stats['step_durations']`:

```python
durations = list(stats.get('step_durations', []))
durations.append(('step8', time.perf_counter() - t0))
stats['step_durations'] = durations
```

## Telegram-прокси

На Victory `api.telegram.org` заблокирован (РФ). Используется `TELEGRAM_PROXY_VARIANTS` из `config/tokens.py`.

Стратегия отправки в `send_telegram()` — ротация цепочки прокси Amsterdam→DE→NL→FR→direct
(маркер `TG_PROXY_CHAIN_ROTATION_2026-06-17`). Итерирует список вариантов, при успехе возвращает True.

Splitting: `_split_chunks(text, 4096)` — режет по `\n`, чтобы не разорвать слова.

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
  AND (направление IS NULL OR направление <> 'Пиксель_атрибуц')
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

- Все предыдущие шаги (записи в `data_quality_log`)
- `requests` + прокси для Telegram
- Доступ к 6 таблицам `big_analytics_*`

## Примеры запуска

```bash
# Только step8 (для тестирования отчёта):
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 pipeline.py --only-step=8"

# В составе полного пайплайна:
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 pipeline.py"

# Только снимок воронки:
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 step8_stats/pipeline_log_snapshot.py"
```

## Проверки после запуска

```sql
-- Все шаги попали в лог
SELECT step_name, duration_sec, status FROM data_quality_log
WHERE run_id = (SELECT MAX(run_id) FROM data_quality_log)
ORDER BY started_at;

-- Снимки воронки растут (по run_at)
SELECT run_at, COUNT(*) AS months FROM data_pipeline_log
GROUP BY 1 ORDER BY 1 DESC LIMIT 10;
```

## История фиксов

| Дата | Фикс |
|------|------|
| Май 2026 | step8 вынесен в отдельную константу `STEP8_INFO`, запускается последним |
| Май 2026 | `sync_pixel_config` логируется через явный `log_step()` |
| 2026-05-20 | Добавлен `pipeline_log_snapshot.py` для виджета "Дельта пайплайна" |

## Связи

- **Зависит от:** ВСЕ шаги (через `data_quality_log`)
- **Последний шаг** пайплайна

## Файлы

| Файл | Описание |
|------|----------|
| `step8.py` | Основной скрипт (статистика + Telegram) |
| `pipeline_log_snapshot.py` | Снимок воронки в `data_pipeline_log` |
| `funnel_drift_snapshot.py` | Снимок по (month × источник) в `data_funnel_drift_log` + алерт дрейфа |
| `__init__.py` | Пустой |
| `CLAUDE.md` | Краткая инструкция для ИИ |
| `README.md` | Этот файл |
