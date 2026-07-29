# step9_direct_history — История изменений Яндекс.Директ

Шаг 9 пайплайна. Загружает историю изменений кампаний Директа (статус, стратегия, бюджет, регионы, корректировки ставок и т.д.) через **внутренний GraphQL API** direct.yandex.ru.

Двухфазная архитектура аналогично step4: фоновый prefetch + основной шаг с обогащением.

## Назначение

Таблица `yandex_direct_history` — лог всех изменений кампаний:
- Когда кампания была остановлена/запущена
- Когда менялась стратегия / бюджет
- Кто внёс изменение (`changeSource`, `user.uid/login`)
- Какие регионы добавили/убрали
- Изменения корректировок ставок (но не сами корректировки — это `step_cron_night/korrektirovki/`)

Используется для аналитики истории и аудита: "когда специалист менял стратегию", "корректировки за неделю".

## Архитектурная схема

```
pipeline.py
   step0 ──► history_thread.start() ─┐
   step1                              │
   step2                              │
   step3                              │ (фон, ~10 мин)
   step4 ──► prefetch_statuses        │
   step5                              │
   step6                              │
   step7                              │
   step8                              │
   step9 ──► history_thread.join() ◄──┘
         ──► UPDATE директолог/domain/salon из local_gsheet_sites
```

step9 стартует **после** того как step4 prefetch завершён — исключает CSRF-конфликты на одних куках.

## Логика инкрементальной загрузки

Для каждого активного логина:

```python
# 1. Удалить строки по логинам которые стали неактивными
# 2. Для каждого логина:
if MAX(date) IS NULL:
    period = последние 30 дней
elif MAX(date) == today:
    skip  # актуально
else:
    period = MAX(date) + 1 → today  # инкрементальная
```

## GraphQL API

Эндпоинт: `https://direct.yandex.ru/graphql`

Запрос: `userActionLog`:
- `clientLogin` — логин клиента
- `dateFrom` / `dateTo` — окно (`LocalDateTime`)
- `limit: 200`, `pageToken` — пагинация
- `categories: []` — фильтр типов событий (опционально)
- `changeSources: []` — фильтр источника (опционально)

Куки — те же что в step4 (из `cookies.json`).

## Типы событий

| GraphQL type | Русское |
|--------------|---------|
| CampaignValueChangeEvent | Изменение параметра кампании |
| CampaignStatusChangeEvent | Изменение статуса кампании |
| CampaignStrategyEvent | Изменение стратегии |
| CampaignListChangeEvent | Изменение списков |
| CampaignTimeTargetEvent | Изменение временного таргетинга |
| CampaignNetworkEvent | Изменение настроек сетей |
| CampaignRegionsEvent | Изменение регионов |
| BannersEvent | Изменение объявлений |
| SingleMultiplierEvent | Изменение корректировки ставки |
| DemographyMultipliersEvent | Изменение демографических корректировок |
| AdOptionsEvent | Изменение параметров объявления |
| AgencyChangeEvent | Смена агентства |

## Параметры

| Параметр | Значение |
|----------|----------|
| `DAYS_BACK` | 30 (окно первой загрузки) |
| `PAGE_LIMIT` | 200 (лимит API) |
| `REQUEST_DELAY` | 1.0 sec (пауза между страницами) |
| `LOGIN_DELAY` | 0.5 sec (пауза между логинами) |
| `ACTIVE_STATUS` | 'Контекст активно' |
| `T_DIRECT_HISTORY` | `'yandex_direct_history'` |

## Фильтрация невалидных login

В `_get_active_logins`:
```python
if not all(0x00 <= ord(c) <= 0x7f for c in login):
    logger.warning('Skip non-ASCII login: %r', login)
    continue
```

Иначе `requests` бросит `latin-1 codec can't encode` в HTTP-заголовке `Referer`. Пример проблемного логина: `'Нет'` в `local_gsheet_sites.login_key`.

## Зависимости

- step0 (`local_gsheet_sites`, `local_yandex`)
- `cookies.json` (валидные)
- step4 prefetch завершён (общие куки)

## Примеры запуска

```bash
# В составе pipeline:
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 pipeline.py"

# Только step9:
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 pipeline.py --only-step=9"

# Проверка:
psql -c "SELECT MAX(datetime), COUNT(*) FROM yandex_direct_history;"
psql -c "SELECT login, MAX(datetime), COUNT(*) FROM yandex_direct_history GROUP BY 1 ORDER BY 2 DESC LIMIT 10;"
```

## Проверки после запуска

```sql
-- Общий объём
SELECT COUNT(*), MIN(datetime), MAX(datetime) FROM yandex_direct_history;

-- По типам событий
SELECT event_type, COUNT(*) FROM yandex_direct_history GROUP BY 1 ORDER BY 2 DESC;

-- Активные логины с историей за вчера
SELECT login, COUNT(*) FROM yandex_direct_history
WHERE datetime::date = CURRENT_DATE - 1
GROUP BY 1 ORDER BY 2 DESC LIMIT 20;

-- Обогащение сработало (директолог/domain/salon)
SELECT COUNT(*) FILTER (WHERE директолог IS NULL) AS no_dir,
       COUNT(*) FILTER (WHERE domain IS NULL) AS no_domain,
       COUNT(*) FILTER (WHERE salon IS NULL) AS no_salon
FROM yandex_direct_history;
```

## История фиксов

| Дата | Фикс |
|------|------|
| Апрель 2026 | Переименование `direct_history` → `yandex_direct_history` |
| Апрель 2026 | Фильтр не-ASCII login (предотвращение `latin-1` ошибки) |
| Май 2026 | step9 стартует после step4 prefetch (CSRF-конфликты) |

## Связи

- **Зависит от:** step0, step4 (prefetch завершён)
- **НЕ входит в:** `big_analytics_full` — это отдельная таблица для аналитики истории
- **Используется отдельно:** в Power BI как страница "История изменений", в `corrections.py` иногда для проверки

## Файлы

| Файл | Описание |
|------|----------|
| `step9.py` | Основной шаг |
| `__init__.py` | Пустой |
| `CLAUDE.md` | Краткая инструкция для ИИ |
| `README.md` | Этот файл |
