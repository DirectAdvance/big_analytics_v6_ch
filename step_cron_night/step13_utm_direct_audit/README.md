# step13_utm_direct_audit — UTM-аудит Яндекс.Директ

Проверяет `TrackingParams` всех групп объявлений по всем активным аккаунтам через **официальный** Direct API + Метрику. Самый долгий шаг ночного пайплайна (~2 часа).

## Назначение

UTM-метки в группах объявлений — это шаблон, по которому Директ формирует параметры в кликах. Если они не совпадают со стандартом — лиды не атрибутируются к кампаниям, не попадают в BI-аналитику.

Этот скрипт ежедневно проверяет TrackingParams всех групп Директа и фиксирует:
- Группы без UTM
- Группы с UTM не по шаблону
- Группы без трафика (Метрика не вернула данных за 30 дней)
- Группы с правильным UTM

И отдельно ведёт **историю проблем** в `check_utm_fuck_direct` — там видно когда проблема появилась впервые, в каком объёме расходов сидит, исправил ли её специалист.

## Архитектурная схема

```
big_analytics_direct (account_login + CampaignId)
         │
         ▼
JOIN local_gsheet_sites (status='Контекст активно')
         │
         ▼
Active accounts (~100-200)
         │
         ├──► tp1-tp5: Direct API v5 (AdGroups) ──► TrackingParams каждой группы
         │
         └──► tp6/tp7/tp8 (МК/ТК): Метрика API ──► UTM из реального трафика
                                  │
                                  ▼
                       Классификация (cls):
                       ОК / НЕТ_UTM / ДРУГОЙ_UTM / НЕТ_ТРАФИКА
                                  │
                                  ├──► public.check_utm (DROP+CREATE+INSERT)
                                  │
                                  └──► public.check_utm_fuck_direct (UPSERT, хранит историю)
                                  │
                                  ▼
                       Telegram-уведомление (start/end/error)
```

## Время работы

- ~100-200 активных аккаунтов
- 15-20 минут на 1 аккаунт (учитывая API лимиты)
- Полный прогон: **~2 часа**

## Когда запускать

- **Ночной cron в 03:00 МСК** — основной режим
- **Вручную не раньше 03:00 МСК** в тот же день, если уже запускался (квота Метрики 5000/сутки сбрасывается 00:00 GMT = 03:00 МСК)

## Двух-токенная схема Метрики

| Токен | Email | Назначение | Квота |
|-------|-------|------------|-------|
| `METRIKA_TOKEN_AUDIT` (primary) | `victorylotsofads04@yandex.ru` | Только step13 | 5000/сутки |
| `METRIKA_TOKEN` (fallback) | `victorylotsofads1@yandex.ru` | metrika_yandex.py sync + fallback | 5000/сутки |

Логика 429 в `step4_campaign_status/check_utm/utm_direct_audit.py:metrika_get()`:
1. Первый 429 → ждём `Retry-After`, retry
2. Второй 429 → переключение на следующий токен
3. Все выжаты → `_metrika_quota_exhausted=True`, Метрика пропускается до конца запуска

Доступ к счётчикам раздаётся в `metrika_yandex.py` Phase 4 (поэтому он запускается первым в ночном пайплайне).

## Классификация UTM (cls)

| cls | Условие |
|-----|---------|
| `ОК` | TrackingParams соответствует шаблону |
| `НЕТ_UTM` | TrackingParams пустой или отсутствует |
| `ДРУГОЙ_UTM` | UTM есть, но не по шаблону |
| `НЕТ_ТРАФИКА` | Метрика не вернула данных за 30 дней |

Ожидаемый шаблон UTM содержит:
```
utm_source=s:{source}
utm_campaign={id}|{name}
utm_content=g:{gbid}
geoname, geoid, dev, r, cor
```

## Таблица `public.check_utm`

**Дропается и пересоздаётся** каждый запуск.

| Колонка | Тип | Описание |
|---------|-----|----------|
| `id` | SERIAL PK | |
| `checked_at` | TIMESTAMP | |
| `login` | TEXT | account_login |
| `CampaignId` | BIGINT | |
| `CampaignName` | TEXT | |
| `group_id` | BIGINT | |
| `group_name` | TEXT | |
| `status` | TEXT | статус группы из Директа |
| `cls` | TEXT | ОК / НЕТ_UTM / ДРУГОЙ_UTM / НЕТ_ТРАФИКА |
| `tracking_params` | TEXT | текущие TrackingParams |
| `utm_source_type` | TEXT | `direct` / `metrika` |
| `domain` | TEXT | |
| `counter_id` | TEXT | ID счётчика Метрики (для tp6/7/8) |
| `специалист` | TEXT | директолог из local_gsheet_sites |

## Таблица `public.check_utm_fuck_direct`

**НЕ дропается** — хранит историю. UPSERT при каждом запуске.

| Колонка | Тип | Описание |
|---------|-----|----------|
| `login`, `CampaignId`, `CampaignName`, `group_id`, `group_name` | | как в check_utm |
| `tracking_params` | TEXT | плохая метка |
| `домен` | TEXT | |
| `total_cost` | NUMERIC | расходы из big_analytics_direct |
| `data` | DATE | дата первого появления проблемы (MIN Date) |
| `checked_at` | TIMESTAMP | дата последней проверки |
| `utm_done` | BOOLEAN | TRUE = проблема исправлена |
| `специалист` | TEXT | |
| `id_name_campaing` | TEXT | `CampaignId|CampaignName` |

Логика UPSERT:
- Новые `cls IN ('НЕТ_UTM', 'ДРУГОЙ_UTM')` → INSERT с `data = MIN("Date")` из big_analytics_direct
- Существующие `utm_done=FALSE` → UPDATE `checked_at`, `total_cost`, `tracking_params`
- Появился `cls='ОК'` для той же CampaignId/group_id → `utm_done = TRUE`

## Авторизация

- **Direct API:** OAuth-токены `OAUTH_TOKEN_1/2/3` в `config/tokens.py`
- **Метрика:** `METRIKA_TOKEN_AUDIT` + fallback `METRIKA_TOKEN`
- **НЕ использует куки** — только официальные API (поэтому нет конфликта с step4)

## Зависимости

- step3 (`big_analytics_direct`)
- step4 (`campaign_status`)
- `metrika_yandex.py` (раздаёт grants к счётчикам)
- 3 OAuth токена + 2 Метрика токена

## Примеры запуска

```bash
# В составе ночного пайплайна:
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 step_cron_night/pipeline_night.py"

# Только step13 (после metrika_yandex.py):
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 -u step_cron_night/step13_utm_direct_audit/run.py"

# Без фонового режима (увидеть прогресс):
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 -u step_cron_night/step13_utm_direct_audit/run.py 2>&1 | tee /tmp/step13.log"
```

## Проверки после запуска

```sql
-- Все проблемные группы текущего аудита
SELECT login, "CampaignName", group_name, cls, "специалист"
FROM public.check_utm
WHERE cls != 'ОК'
ORDER BY login, "CampaignId";

-- Сводка по специалисту
SELECT "специалист", cls, COUNT(*) FROM public.check_utm
GROUP BY 1, 2 ORDER BY 1, 2;

-- Открытые проблемы (история)
SELECT login, "CampaignName", group_name, домен, total_cost, data, "специалист"
FROM public.check_utm_fuck_direct
WHERE utm_done = FALSE
ORDER BY total_cost DESC NULLS LAST LIMIT 30;

-- Исправленные за последний запуск
SELECT * FROM public.check_utm_fuck_direct
WHERE utm_done = TRUE AND checked_at > NOW() - INTERVAL '1 day';
```

## Почему вне дневного `pipeline.py`

UTM-аудит использует **официальный** Direct API (OAuth). step4 уже использует Grid API (куки) — оба требуют сетевых ресурсов, не имеет смысла гонять и днём, и ночью. Перенесён исключительно в ночной пайплайн (май 2026).

## Файлы

| Файл | Описание |
|------|----------|
| `run.py` | Точка входа (импорт `main()` из основного модуля) |
| `__init__.py` | Пустой |
| `step4_campaign_status/check_utm/utm_direct_audit.py` | Основная логика (~970 строк) — лежит в подпапке step4 |
| `CLAUDE.md` | Краткая инструкция |
| `README.md` | Этот файл |
