# korrektirovki — Корректировки ставок Яндекс.Директ

Получает все корректировки ставок (bid modifiers) по всем активным аккаунтам через **официальный** API Яндекс.Директ v5. Полный снапшот при каждом запуске.

Запускается в составе **ночного пайплайна** `pipeline_night.py` (cron 03:00 МСК) или вручную.

## Назначение

Корректировки ставок — это процентные модификаторы базовой ставки кампании по:
- Полу и возрасту аудитории
- Типу устройства (мобильные/планшеты/десктоп/Smart TV)
- Регионам
- Аудиториям ретаргетинга
- Видео/Smart-баннерам/SERP-позиции
- Доходу аудитории
- Группе объявлений

Таблица `yandex_direct_korrektirovki` показывает: где включены, на каком уровне, какой процент, кто специалист.

## Архитектурная схема

```
local_gsheet_sites (WHERE status='Контекст активно')
        │
        ▼
Active logins
        │
        ├──► Direct API v5 /bidmodifiers/get (CAMPAIGN level)
        │
        └──► Direct API v5 /bidmodifiers/get (AD_GROUP level)
                                │
                                ▼
                       Разворачивание modifiers (несколько в одном bid_modifier_id)
                                │
                                ▼
                       JOIN с campaign_status (статус кампании)
                       JOIN с local_gsheet_sites.directologist (специалист)
                                │
                                ▼
                       DROP + CREATE + INSERT
                       → public.yandex_direct_korrektirovki
                                │
                                ▼
                       Telegram-уведомление о завершении
```

## Таблица `public.yandex_direct_korrektirovki`

| Колонка | Тип | Описание |
|---------|-----|----------|
| `ulogin` | TEXT | Логин аккаунта |
| `campaign_id` | BIGINT | ID кампании |
| `campaign_name` | TEXT | Название кампании |
| `ad_group_id` | BIGINT | ID группы (NULL если уровень кампании) |
| `level` | TEXT | `CAMPAIGN` / `AD_GROUP` |
| `modifier_id` | BIGINT | ID корректировки из API |
| `enabled` | TEXT | `YES` / `NO` |
| `modifier_type` | TEXT | Тип корректировки |
| `modifier_name` | TEXT | Читаемое название (например `Пол и возраст: Мужчины (25–34)`) |
| `bid_percent` | TEXT | `+20%`, `-50%` |
| `korrektirovki_bid` | TEXT | `modifier_name | bid_percent` (комбо) |
| `audience_id` | BIGINT | ID аудитории (только RETARGETING) |
| `"специалист"` | TEXT | Директолог из `local_gsheet_sites.directologist` |
| `campaign_status` | TEXT | Статус кампании из `campaign_status` |
| `loaded_at` | TIMESTAMPTZ | Время загрузки |

## Типы корректировок (modifier_type)

| Значение | Описание |
|----------|----------|
| `DEMOGRAPHICS_ADJUSTMENT` | Пол и возраст |
| `MOBILE_ADJUSTMENT` | Мобильные устройства |
| `TABLET_ADJUSTMENT` | Планшеты |
| `DESKTOP_ONLY_ADJUSTMENT` | Только десктоп |
| `REGIONAL_ADJUSTMENT` | Регионы |
| `RETARGETING_ADJUSTMENT` | Аудитории / ретаргетинг |
| `VIDEO_ADJUSTMENT` | Видео |
| `SMART_AD_ADJUSTMENT` | Смарт-баннеры |
| `SMART_TV_ADJUSTMENT` | Смарт ТВ |
| `SERP_LAYOUT_ADJUSTMENT` | Позиция в выдаче |
| `INCOME_GRADE_ADJUSTMENT` | Доход аудитории |
| `AD_GROUP_ADJUSTMENT` | Группа |

## Авторизация

OAuth-токены из `config/tokens.py`:
- `OAUTH_TOKEN_1`, `OAUTH_TOKEN_2` — перебором по первому рабочему

Не использует куки. Это **официальный API**, в отличие от Grid API в step4.

## Параметры

- Источник логинов: `local_gsheet_sites` WHERE `status = 'Контекст активно'`
- Уровни: `CAMPAIGN` и `AD_GROUP`
- Стратегия: DROP + CREATE + INSERT (полный снапшот)
- 0.5с задержка между батчами одного логина
- 1.0с между логинами

## Производительность

- ~188 аккаунтов
- ~20-25 минут полного прогона
- Некоторые `porg-*` аккаунты возвращают 0 строк — нет доступа по токенам

## Зависимости

- step0 (`local_gsheet_sites`)
- step4 (`campaign_status` для JOIN)
- `OAUTH_TOKEN_*` в `config/tokens.py`

## Примеры запуска

```bash
# В составе ночного пайплайна:
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 step_cron_night/pipeline_night.py"

# Только корректировки:
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 step_cron_night/korrektirovki/run.py"

# Долгий прогон в фоне:
ssh victory "cd ~/big_analytics_v5 && nohup ~/venv/bin/python3 step_cron_night/korrektirovki/run.py > /tmp/korrektirovki.log 2>&1 &"
ssh victory "tail -f /tmp/korrektirovki.log"
```

## Проверки после запуска

```sql
-- Сколько корректировок по типам
SELECT modifier_type, enabled, COUNT(*)
FROM yandex_direct_korrektirovki GROUP BY 1,2 ORDER BY 3 DESC;

-- По специалисту
SELECT "специалист", COUNT(*) FROM yandex_direct_korrektirovki
WHERE enabled='YES' GROUP BY 1 ORDER BY 2 DESC LIMIT 20;

-- Активные кампании с корректировками RETARGETING
SELECT campaign_name, modifier_name, bid_percent
FROM yandex_direct_korrektirovki
WHERE modifier_type='RETARGETING_ADJUSTMENT' AND enabled='YES'
  AND campaign_status='Активна' LIMIT 20;
```

## Файлы

| Файл | Описание |
|------|----------|
| `run.py` | Точка входа |
| `korrektirovki.py` | Основная логика (API + парсинг + INSERT) |
| `__init__.py` | Пустой |
| `CLAUDE.md` | Краткая инструкция |
| `README.md` | Этот файл |
