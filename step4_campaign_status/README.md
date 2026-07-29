# step4_campaign_status — Статусы кампаний Яндекс.Директ

Шаг 4 пайплайна. Получает актуальные статусы кампаний через **неофициальный Grid API Яндекс.Директа** (использует куки из браузера) и обогащает `big_analytics_direct` колонкой `campaign_status`.

Содержит подпапку `check_utm/` — отдельный аудит UTM-параметров (запускается из ночного пайплайна `step13_utm_direct_audit`).

## Назначение

- **`campaign_status`** — справочник статусов кампаний: 'Активна' / 'Остановлена' / 'Архив'
- **`payment_model`** — тип оплаты: 'за клики' или 'за конверсии'
- В Power BI status визуализируется emoji-префиксом в `"номер кампании | название кампании"` (🟢/🟡/⚪) — формируется в step6

## Двухфазная архитектура

### Фаза A: prefetch_statuses (фоновый поток)

Запускается из `pipeline.py` сразу после `step0`, параллельно с шагами 1-3. К моменту запуска основного step4 — результаты уже готовы.

```
pipeline.py
   step0 ──► thread_prefetch.start() ─┐
   step1                              │ (фон, 3 потока, ~6 мин)
   step2                              │
   step3                              │
   step4 ──► prefetch_thread.join() ◄─┘
         ──► UPDATE big_analytics_direct
```

### Фаза B: run() (основной step4)

1. Ждёт фоновый поток через `join()`
2. Строит `campaign_status` из prefetch + `big_analytics_direct`
3. `ALTER TABLE big_analytics_direct ADD COLUMN campaign_status + payment_model`
4. UPDATE через JOIN на `campaign_status`
5. Постпроцессинговые UPDATE: campaign_status для звонков, направление='Контекст' для звонков, заполнение Название crm/manager_login/проджект по салону

## Архитектурная схема

```
local_yandex (за 60 дней) ──► активные кампании по manager_login
                              │
                              ├── 3 группы по manager_login
                              │   │
                              │   └── 3 потока × Grid API + куки
                              │       (один manager_login = одна куки = один поток)
                              │
                              ▼
                       _campaign_statuses_prefetch
                              │
                              ├── failed_accounts (manager='unknown' или нет куки)
                              │     │
                              │     └── retry перебором всех 3 сессий
                              │
                              ▼
                       campaign_status (справочник)
                              │
                              ▼
                       big_analytics_direct.campaign_status (UPDATE)
                              │
                              ▼
                       step6: 🟢🟡⚪ префикс в CampaignName
```

## Куки

Файл: `cookies.json` (локально + на Victory в корне `big_analytics_v5/`).

Получение:
```bash
curl -s -H "X-API-Key: $COOKIES_API_KEY" \
    http://192.168.0.202:8765/cookies -o cookies.json
scp cookies.json victory:~/big_analytics_v5/cookies.json
```

Тест валидности — `config/cookies.py:check_cookies_alive()` через 2 известных аккаунта:
- `victorylotsofads1` → `e-20074351`
- `victoryagency-direct1618440` → `acbu-spb-436222-ns89`
- `victoryagency14` → `porg-7uhutcdh`

При **протухших куках**: `_build_sessions()` возвращает `[]` → `send_tg_cookies_dead()` + return, **старые данные сохраняются**.

## Параметры

| Параметр | Значение | Описание |
|----------|----------|----------|
| `ACTIVE_DAYS` | `60` | "Активна" = есть данные за последние N дней в local_yandex |
| `PAGE_LIMIT` | `200` | Page size Grid API |
| `GRID_URL` | `https://direct.yandex.ru/web-api/grid/api` | Эндпоинт |

## Зависимости

- step3 (`big_analytics_direct` с account_login + CampaignId)
- step0 (`local_yandex` для определения активных)
- `cookies.json` (валидные)
- Telegram-токен для уведомлений (`config/tokens.py`)
- HTTP-прокси для Telegram на Victory (РФ-блок api.telegram.org)

## Примеры запуска

```bash
# В составе полного пайплайна (рекомендуется):
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 pipeline.py"

# Только step4 (быстро, использует prefetch из последнего полного запуска):
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 pipeline.py --only-step=4"

# fast_pipeline.py — пропускает Фазу A, только UPDATE из существующей campaign_status:
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 fast_pipeline.py"
```

## Проверки после запуска

```sql
-- Сколько кампаний по статусам
SELECT campaign_status, payment_model, COUNT(*)
FROM campaign_status GROUP BY campaign_status, payment_model;

-- big_analytics_direct.campaign_status проставлен
SELECT campaign_status, COUNT(*) FROM big_analytics_direct
GROUP BY campaign_status;

-- payment_model заполнен (баг 19.05.2026 — 1506 NULL)
SELECT COUNT(*) FROM campaign_status WHERE payment_model IS NULL;
```

## Подпапка `check_utm/`

**Файл:** `check_utm/utm_direct_audit.py`

UTM-аудит групп Яндекс.Директа. Проверяет `TrackingParams` через **официальный** OAuth API + Метрику.

| | Grid API (step4) | OAuth API (check_utm) |
|---|---|---|
| Авторизация | Куки | OAuth-токен |
| Эндпоинт | `direct.yandex.ru/web-api/grid` | `api.direct.yandex.com/json/v5` |
| Что делает | Статусы кампаний | UTM-теги групп объявлений |
| Запуск | Из `pipeline.py` (фон) | Из `step_cron_night/step13_utm_direct_audit/run.py` (cron 03:00 МСК) |

Результат: таблицы `check_utm`, `check_utm_fuck_direct`.

## История фиксов

| Дата | Фикс |
|------|------|
| Апрель 2026 | `manager_login.split('@')[0]` — обрезание `@yandex.ru` суффикса |
| Май 2026 | step9 стартует после step4 prefetch (исключение CSRF на одних куках) |
| Май 2026 | `BiddingStrategy/` — отдельный модуль для prefetch `payment_model` |
| 13.05.2026 | Фикс emoji-префикса в CampaignName (был NULL для 253k direct-строк) |

## Связи

- **Зависит от:** step3 (`big_analytics_direct`), step0 (`local_yandex`)
- **Использует:** step6 (emoji-префикс в CampaignName + `cs.campaign_status` + `cs.payment_model`)
- **Связан с step9**: оба используют куки. step9 стартует после step4 prefetch завершён.

## Файлы

| Файл | Описание |
|------|----------|
| `step4.py` | Основной скрипт (двухфазная архитектура) |
| `check_utm/utm_direct_audit.py` | UTM-аудит групп (отдельный запуск через cron) |
| `check_utm/__init__.py` | Пустой |
| `CLAUDE.md` | Краткая инструкция для ИИ |
| `README.md` | Этот файл |
