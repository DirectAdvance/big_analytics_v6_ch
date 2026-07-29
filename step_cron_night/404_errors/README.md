# 404_errors — Сбор 404-ошибок через Яндекс.Метрику

Сбор страниц с 404-ошибками со всех сайтов через Метрику API. Инкрементальная загрузка с 7-дневным перекрытием (Метрика может дополнять данные ретроспективно).

Запускается в составе **ночного пайплайна** `pipeline_night.py` (cron 03:00 МСК) **и** в конце дневного `pipeline.py` — намеренное дублирование.

## Назначение

Помогает специалистам обнаружить:
- Битые ссылки на сайте, на которые ведёт реклама
- Удалённые страницы, всё ещё рекламируемые в Директе
- UTM-метки с опечатками или несуществующими ID кампаний

Таблица `yandex_direct_404_errors` показывает: какой счётчик, какой сайт, какой специалист, какой URL, какая кампания/группа, когда был визит.

## Архитектурная схема

```
Google Sheets (ВСЕ САЙТЫ, статус=Контекст активно) ──► sites list + директолог
                                │
                                ▼
                       Матчинг счётчиков Метрики ↔ сайтов
                       (site_clean == sheet_clean OR endswith)
                                │
                                ▼
                       Яндекс.Метрика API
                       ym:pv:pageviews + filter ym:pv:title=@'404'
                                │
                                ▼
                       Парсинг URL, page_title, UTM-параметров
                                │
                                ▼
                       DELETE FROM yandex_direct_404_errors WHERE visit_date >= date_from
                                │
                                ▼
                       INSERT свежие данные
                                │
                                ▼
                       Telegram-сообщение со сводкой
```

## Таблица `public.yandex_direct_404_errors`

| Колонка | Тип | Описание |
|---------|-----|----------|
| `№ счетчика` | TEXT | ID счётчика Метрики |
| `counter_name` | TEXT | Название счётчика |
| `site` | TEXT | Домен сайта |
| `специалист` | TEXT | Из Google Sheets (колонка H) |
| `url` | TEXT | Полный URL 404-страницы |
| `page_title` | TEXT | Заголовок страницы |
| `utm_campaign` | TEXT | UTM-метка кампании |
| `№ кампании` | TEXT | Числовой ID из utm_campaign |
| `utm_content` | TEXT | UTM-контент |
| `№ группы` | TEXT | ID группы из utm_content (regex `g:\d+\|`) |
| `visit_date` | DATE | Дата визита |
| `week_start` | DATE | Понедельник недели |
| `detected_at` | TIMESTAMP | Время записи в БД |

## Инкрементальная логика

```python
if table_not_exists:
    date_from = '2026-01-01'  # полная загрузка с DATE_FROM
else:
    date_from = MAX(visit_date) - timedelta(days=7)  # инкрементальная
```

7-дневный перекрёст — Метрика может ретроспективно дополнить данные за уже прошедшие дни.

```sql
DELETE FROM yandex_direct_404_errors WHERE visit_date >= date_from;
INSERT INTO yandex_direct_404_errors ...;
```

## Источники

### Google Sheets (список активных сайтов)

| Параметр | Значение |
|----------|----------|
| Sheet ID | `1Hw0sfNjb3BHrSs6ARmuPaN7C7t0JSAqV7b6TuUlT3ow` |
| Лист | `ВСЕ САЙТЫ` |
| Фильтр | Статус (col B) = `Контекст активно` |
| Специалист | col H (index 7) |

### Яндекс.Метрика API

- Токен: `victoryagency` (OAuth)
- Метрика: `ym:pv:pageviews`
- Фильтр: `ym:pv:title=@'404'`
- Лимит: 10 000 строк на счётчик за период

## Матчинг счётчиков ↔ сайтов

```python
def match(site_url, sheet_domain):
    sc = site_url.replace('www.', '').replace('m.', '')
    sd = sheet_domain.replace('www.', '').replace('m.', '')
    return sc == sd or sc.endswith('.' + sd)
```

`www.` и `m.` убираются с обеих сторон перед сравнением.

## Параметры

- `date_from` — вычисляется автоматически
- `week_start` = `visit_date - weekday()` (понедельник)

## Зависимости

- step0 (для совпадения с активными сайтами)
- Google API (service account + Sheets API)
- Метрика OAuth токен
- `config/cookies.py:send_tg` для уведомлений

## Примеры запуска

```bash
# В составе ночного пайплайна:
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 step_cron_night/pipeline_night.py"

# Только 404_errors:
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 step_cron_night/404_errors/404_errors.py"

# В дневном пайплайне:
# Запускается автоматически после load_reviews + load_crop, перед normalize_salons
```

## Проверки после запуска

```sql
-- Объём за последнюю неделю
SELECT week_start, COUNT(*), COUNT(DISTINCT site) AS sites
FROM yandex_direct_404_errors
WHERE week_start >= CURRENT_DATE - INTERVAL '4 weeks'
GROUP BY 1 ORDER BY 1 DESC;

-- Топ-страницы 404
SELECT site, url, COUNT(*) AS hits
FROM yandex_direct_404_errors
WHERE visit_date >= CURRENT_DATE - INTERVAL '7 days'
GROUP BY 1,2 ORDER BY 3 DESC LIMIT 30;

-- Активные кампании с 404
SELECT site, "№ кампании", "специалист", COUNT(*)
FROM yandex_direct_404_errors
WHERE "№ кампании" IS NOT NULL AND "№ кампании" != ''
  AND visit_date >= CURRENT_DATE - INTERVAL '7 days'
GROUP BY 1,2,3 ORDER BY 4 DESC LIMIT 30;
```

## Намеренное дублирование

`404_errors.py` запускается в **обоих** пайплайнах:
- `pipeline_night.py` — каждую ночь в 03:00 МСК
- `pipeline.py` (дневной) — в конце, после load_reviews+load_crop

Это нормально: скрипт **инкрементальный** с 7-дневным перекрёстом, повторный запуск только обновит свежие данные. Цель — чтобы 404-таблица была актуальна даже если ночной cron не отработал.

## Файлы

| Файл | Описание |
|------|----------|
| `404_errors.py` | Основной скрипт |
| `CLAUDE.md` | Краткая инструкция |
| `README.md` | Этот файл |
