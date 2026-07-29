# step_cron_night — ночной пайплайн

Тяжёлые API-шаги big_analytics_v5, запускаемые автоматически в **03:00 МСК** (00:00 UTC) через cron.
Power BI читает готовые таблицы — данные всегда свежие с ночи.

## Зачем отдельно от pipeline.py

- Эти шаги занимают **2.5–3 часа** и делают тысячи запросов к API
- Ночной запуск не конкурирует с дневным pipeline.py за квоту Метрики
- Квота Метрики (5000 req/day × 3 токена) сбрасывается в **00:00 UTC = 03:00 МСК** — именно поэтому старт в это время
- Дневной pipeline.py работает быстро, читая уже готовые таблицы

## Шаги (порядок важен)

| # | Модуль | Таблица результата | ~Время |
|---|--------|-------------------|--------|
| 1 | `metrika_yandex.py` | `metrika_yandex` (grants счётчиков) | 3 мин |
| 2 | `step13_utm_direct_audit/run.py` | `check_utm`, `check_utm_fuck_direct` | 2 ч |
| 3 | `korrektirovki/run.py` | `yandex_direct_korrektirovki` | 25 мин |
| 4 | `404_errors/404_errors.py` | `yandex_direct_404_errors` | 1 мин |

## Cron на Victory VPS

```cron
# pipeline_night — 03:00 МСК = 00:00 UTC
0 0 * * * cd ~/big_analytics_v5 && ~/venv/bin/python3 step_cron_night/pipeline_night.py >> /tmp/pipeline_night.log 2>&1
```

## Ручной запуск

```bash
# Фоновый запуск (с логом):
ssh victory "cd ~/big_analytics_v5 && nohup ~/venv/bin/python3 step_cron_night/pipeline_night.py > /tmp/pipeline_night.log 2>&1 &"

# Мониторинг:
ssh victory "tail -f /tmp/pipeline_night.log"
```

## Telegram-уведомления

- При старте: сообщение "pipeline_night запущен"
- При завершении: сводка ✅/❌ по каждому шагу + общее время

## Примечание

`step13_utm_direct_audit` теперь запускается отсюда. Его можно убрать из `pipeline.py`
(строка subprocess после crm_mappings_check) — pipeline станет короче на ~2 часа.
