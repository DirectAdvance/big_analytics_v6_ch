# direct_account_reviews — Отзывы Яндекс.Директ

Загружает данные кампаний отзывов из Яндекс.Директа в `big_analytics_full`. Кампании отзывов — это отдельный вид рекламы с особой логикой расчёта (без воронки заявок, только показы/клики/расходы).

Запускается **еженедельно** ночным cron в понедельник 02:00 МСК и в дневном `pipeline.py` (только перенос в `big_analytics_full`).

## Назначение

Кампании отзывов в Директе:
- Не привязаны к воронке заявок (нет лидов)
- Имеют свой формат отчётности
- В `big_analytics_full` получают специальные маркеры: `manager_login='отзывы'`, `тип_заявки='отзывы'`, etc.

## Архитектурная схема

```
Google Sheets "Power BI" A:E
  (город / салон / аккаунт / сайт / агентский аккаунт)
        │
        ▼
load_reviews.py ──► yandex_direct_account_reviews (справочник)
                                │
Direct Reports API v5 ─────────┤
  Client-Login = аккаунт (porg-*)
  OAuth Token = по агентскому аккаунту (перебором)
                                ▼
fetch_direct_stats.py ──► yandex_direct_reports_reviews (статистика)
                                │
                                ▼
load_reviews_to_big_analytics.py
                                │
                                ▼
                       big_analytics_full
                       (manager_login='отзывы'
                        + тип_заявки='отзывы'
                        + RlAdjustmentId_total='отзывы'
                        + Название crm='отзывы')
```

## Таблицы в БД

| Таблица | Содержимое |
|---------|-----------|
| `yandex_direct_account_reviews` | Справочник: город/салон/аккаунт/сайт/агентский аккаунт (231 строка) |
| `yandex_direct_reports_reviews` | Статистика: расходы по дням/кампаниям (инкрементная, ~3 577 строк) |

## Подшаги

| # | Файл | Что делает | Время |
|---|------|-----------|-------|
| 1 | `load_reviews.py` | Google Sheets → `yandex_direct_account_reviews` | ~30с |
| 2 | `fetch_direct_stats.py` | Direct API → `yandex_direct_reports_reviews` | ~40 мин |
| 3 | `load_reviews_to_big_analytics.py` | INSERT в `big_analytics_full` | ~30с |

Полный пайплайн: ~40 мин (bottleneck — очереди Reports API).

## Маркеры строк в `big_analytics_full`

| Поле | Значение |
|------|----------|
| `_source_table` | `'direct'` (или `'reviews'`) |
| `manager_login` | `'отзывы'` |
| `Название crm` | `'отзывы'` |
| `тип_заявки` | `'отзывы'` |
| `тип_сайта` | `'отзывы'` |
| `шаблон` | `'отзывы'` |
| `RlAdjustmentId_total` | `'отзывы'` |
| `priedet`, `dohod_do_kredita`, `dobro` | `NULL::BIGINT` |
| `direction` | `'Авто'` |

## Два режима запуска

### Полный (ночной)
В понедельник 02:00 МСК запускается `pipeline.py`:
1. Sync справочника из Google Sheets
2. Fetch свежей статистики из Direct API
3. INSERT в big_analytics_full

### Дневной (быстрый)
В `pipeline.py` (корневой) запускается только `load_reviews_to_big_analytics.py` — переносит существующие данные из `yandex_direct_account_reviews` в свежепересозданный `big_analytics_full` (после step6 CTAS).

## Cron на Victory

```cron
# Отзывы — раз в неделю в пн 02:00 МСК (вс 21:00 UTC)
0 21 * * 0  cd ~/big_analytics_v5 && ~/venv/bin/python3 step_cron_night/direct_account_reviews/pipeline.py >> /tmp/reviews_pipeline.log 2>&1
```

## Зависимости

- step0 (`local_*`)
- step6 + step7 (`big_analytics_full` финализирован)
- OAuth токены Direct API
- Google API + service account

## Примеры запуска

```bash
# Полный пайплайн отзывов:
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 step_cron_night/direct_account_reviews/pipeline.py"

# Только перенос (после ежедневного pipeline.py):
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 step_cron_night/direct_account_reviews/load_reviews_to_big_analytics.py"

# Только fetch статистики (для теста):
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 step_cron_night/direct_account_reviews/fetch_direct_stats.py"
```

## Проверки после запуска

```sql
-- Отзывы попали в big_analytics_full
SELECT COUNT(*), SUM(total_cost) FROM big_analytics_full
WHERE manager_login = 'отзывы';

-- По месяцам
SELECT DATE_TRUNC('month', "Date") AS month, COUNT(*), SUM(total_cost)
FROM big_analytics_full WHERE manager_login = 'отзывы'
GROUP BY 1 ORDER BY 1;

-- Справочник
SELECT COUNT(*), MAX(updated_at) FROM yandex_direct_account_reviews;
```

## История фиксов

| Дата | Фикс |
|------|------|
| Апрель 2026 | Переименование `direct_account_reviews` → `yandex_direct_account_reviews` |
| Апрель 2026 | Добавлены маркеры `'отзывы'` в `_build_reviews_sql` (step3) — `Название crm`, `тип_сайта`, `шаблон`, `RlAdjustmentId_total` |
| Май 2026 | Колонки `priedet`, `dohod_do_kredita`, `dobro` как `NULL::BIGINT` в INSERT |
| Май 2026 | `load_reviews.py` переписан: новый формат Google Sheets (одна строка = один аккаунт, A-E = город/салон/аккаунт/сайт/агентский аккаунт) |
| Май 2026 | `fetch_direct_stats.py`: добавлен `REVIEWS_TOKENS` из `load_yandex_direct_reviews()` (Victory `~/.secret/loader.py`), фильтр non-ASCII логинов |
| Май 2026 | Статистика переименована в `yandex_direct_reports_reviews` (отдельно от справочника) |

## Связи

- **Зависит от:** step6 + step7 (`big_analytics_full` готов)
- **Дневной pipeline** запускает только `load_reviews_to_big_analytics.py` (не весь pipeline отзывов)
- **Используется:** PBI страница "Отзывы" + общий BAF фильтр `direction='Авто' AND направление!='пиксель_атрибуц'`

## Файлы

| Файл | Описание |
|------|----------|
| `pipeline.py` | 3 подшага последовательно |
| `load_reviews.py` | Google Sheets → справочник |
| `fetch_direct_stats.py` | Direct API → статистика |
| `load_reviews_to_big_analytics.py` | INSERT в `big_analytics_full` |
| `__init__.py` | Пустой |
| `CLAUDE.md` | Краткая инструкция |
| `README.md` | Этот файл |
