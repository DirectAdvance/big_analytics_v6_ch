# Спецификация: загрузка посевов Telega.in → PostgreSQL

**Файл:** `step10_crop_targeting/fetch_api.py`  
**Таблица-назначение:** `public.crop_targeting_api_telegain` (БД `ad_analytics_bi`, Victory VPS)  
**Назначение:** забрать сырые данные по размещениям (посевам) из API Telega.in и сохранить в PostgreSQL для дальнейшей аналитики.

---

## 1. Место в пайплайне

```
crop_targeting/pipeline.py
  Шаг 1. load_crop_targeting.py       → gsheets_crop_targeting_account (Google Sheets)
  Шаг 2. load_crop_targeting_leads.py → gsheets_crop_targeting_account_leads (до мая 2026)
  Шаг 3. load_api_leads.py            → crop_targeting_api_telegain_lead  ← использует результат fetch_api.py
  Шаг 4. load_crop_to_big_analytics.py → big_analytics_full
```

**fetch_api.py** — это шаг, который заполняет таблицу `crop_targeting_api_telegain`.  
`load_api_leads.py` читает её и строит аналитическую таблицу `crop_targeting_api_telegain_lead`.

Запускать **после** полного пайплайна big_analytics_v5 (шаги 0–7).  
Текущий запуск из `pipeline_monday.py` (раз в неделю) или вручную:

```bash
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 crop_targeting/pipeline.py"
```

---

## 2. Что делает скрипт

1. Подключается к БД, создаёт таблицу `crop_targeting_api_telegain` если нет (включая миграции схемы).
2. Загружает список заказов (`/api/public/v1/order/index`) → словарь `{order_id: {project_name, comment, status}}`.
3. Постранично читает размещения (`/api/public/v1/order_channel/index`, 100 записей/страница).
4. На каждой странице:
   - Пропускает записи где `status != 'complete'`.
   - Пропускает записи где `end_time < 2026-05-01` (захардкожена константа `API_DATE_FROM`).
   - Парсит UTM-метки из ссылок поста (`statistic.all_links`, затем `order_links`); при `tglink.io` — резолвит редирект.
   - UPSERT строк в таблицу (ON CONFLICT (id) DO UPDATE).
5. Останавливается когда вся страница старее cutoff-даты (API отдаёт данные в обратном хронологическом порядке).

---

## 3. API Telega.in

**Base URL:** берётся из секретов (`api_base`)  
**Аутентификация:** два заголовка:
- `Partner-Authorization: <partner_key>`
- `Authorization: <api_key>`

**Эндпоинты:**

| Эндпоинт | Метод | Параметры | Назначение |
|----------|-------|-----------|-----------|
| `/api/public/v1/order/index` | GET | — | Список заказов (Orders) |
| `/api/public/v1/order_channel/index` | GET | `page`, `per_page=100` | Список размещений по каналам |

**Rate limit:** при `HTTP 429` — читать `Retry-After` заголовок, ждать (max 120 сек), retry до 5 раз.  
**Пагинация:** `resp['count']` — общее число записей; прекратить когда `page * 100 >= count` или все записи старее cutoff.

**SSL:** API имеет проблемы с сертификатом — используется кастомный адаптер с `verify=False` и `SECLEVEL=1`.

---

## 4. Схема таблицы `public.crop_targeting_api_telegain`

| Колонка | Тип | Описание |
|---------|-----|----------|
| `id` | INTEGER PK | ID размещения (order_channel.id) |
| `uid` | TEXT | UUID размещения |
| `channel_id` | INTEGER | ID канала |
| `post_link` | TEXT | Ссылка на пост |
| `customer_id` | INTEGER | ID клиента |
| `channel_name` | TEXT | Название канала |
| `channel_link` | TEXT | Ссылка на канал (t.me/...) |
| `placement_format` | TEXT | Формат размещения |
| `status` | TEXT | Статус (всегда `'complete'` в загруженных данных) |
| `cancel_comment` | TEXT | Комментарий отмены |
| `user_price` | NUMERIC | Цена без НДС |
| `user_price_currency` | TEXT | Валюта |
| `order_id` | INTEGER | ID заказа (FK → orders) |
| `order_text` | TEXT | Текст заказа |
| `order_links` | TEXT | Первая ссылка из заказа (после резолва редиректа) |
| `statistic` | JSONB | Статистика поста (просмотры, реакции и т.д.) |
| `created_at` | TIMESTAMPTZ | Дата создания |
| `completed_at` | TIMESTAMPTZ | Дата выполнения |
| `done_at` | TIMESTAMPTZ | Дата приёмки |
| `run_at` | TIMESTAMPTZ | Дата запуска |
| `start_time` | TIMESTAMPTZ | Начало размещения |
| `end_time` | TIMESTAMPTZ | Конец размещения (**основной фильтр дат**) |
| `can_compaint` | BOOLEAN | |
| `can_cancel_compaint` | BOOLEAN | |
| `can_cancel` | BOOLEAN | |
| `order_project_name` | TEXT | Название проекта (из orders) |
| `order_comment` | TEXT | Комментарий к заказу (из orders) |
| `order_status` | TEXT | Статус заказа (из orders) |
| `utm_source` | TEXT | Распарсено из ссылок поста |
| `utm_medium` | TEXT | Распарсено из ссылок поста |
| `utm_campaign` | TEXT | Распарсено из ссылок поста |
| `utm_content` | TEXT | Распарсено из ссылок поста |
| `utm_term` | TEXT | Распарсено из ссылок поста |
| `domain` | TEXT | Домен из первой ссылки |
| `fetched_at` | TIMESTAMPTZ DEFAULT NOW() | Дата загрузки |

**PK:** `id`  
**UPSERT:** при конфликте по `id` обновляются: `status`, `statistic`, `utm_*`, `order_*`, `user_price`, `fetched_at`.  
**НЕ обновляются:** `created_at`, `start_time`, `end_time`, `channel_*`, `customer_id`.

---

## 5. Бизнес-правила фильтрации

1. **status = 'complete'** — незавершённые размещения не сохраняются.
2. **end_time >= 2026-05-01** — загружаем только данные начиная с мая 2026.  
   Константа `API_DATE_FROM = date(2026, 5, 1)` — при необходимости сдвинуть дату менять здесь.
3. **Записи без UTM** — сохраняются (для аудита). Следующий шаг (`load_api_leads.py`) фильтрует их сам: берёт только строки где `utm_content ~ '^[0-9]{8}$'`.

---

## 6. UTM-парсинг

Алгоритм поиска UTM в записи (в порядке приоритета):
1. `statistic.all_links` — ссылки из тела поста
2. `order_links` — ссылки из описания заказа

Для каждой ссылки:
- Если домен `tglink.io` → резолвится редирект (HEAD-запрос → GET с `stream=True`)
- Парсится query string: `utm_source`, `utm_medium`, `utm_campaign`, `utm_content`, `utm_term`
- Берётся первая ссылка где хоть один UTM-параметр не NULL

Редиректы кэшируются в памяти (`_redirect_cache`) в рамках одного запуска.  
`domain` = `netloc` из первой ссылки `order_links` (после резолва).

---

## 7. Секреты и конфигурация

Скрипт читает секреты через `loader.py` из `.secret/`:

```python
from loader import load_telega  # noqa
_TG = load_telega()
API_BASE    = _TG['api_base']      # базовый URL API
PARTNER_KEY = _TG['partner_key']   # заголовок Partner-Authorization
API_KEY     = _TG['api_key']       # заголовок Authorization
```

Подключение к БД через `config/settings.py`:
```python
from config.settings import DB_DST
conn = psycopg2.connect(**DB_DST)
```

На сервере Victory:
- `.secret/.env` содержит `DB_VICTORY_HOST=localhost` и credentials БД
- `.secret/` содержит токены Telega.in (`load_telega()`)
- `.secret/loader.py` — общий загрузчик

---

## 8. Миграция схемы (автоматическая)

Скрипт при каждом запуске проверяет и применяет:
1. Если таблица без колонки `uid` → пересоздаёт (`DROP + CREATE`).
2. Если `order_links` имеет тип `JSONB` → конвертирует в `TEXT`.
3. Если есть колонка `domen` → переименовывает в `domain`.
4. Если нет колонки `domain` → добавляет `ALTER TABLE ADD COLUMN`.

---

## 9. Зависимые таблицы (downstream)

После загрузки `crop_targeting_api_telegain` следующий шаг читает её:

**`load_api_leads.py`** → `crop_targeting_api_telegain_lead`  
Трансформации:
- `utm_content` (DDMMYYYY) → `"Date"` DATE
- `user_price * 1.22 * 1.30` → `total_cost` (цена с НДС 22% и агентской комиссией 30%)
- `channel_link` → `"CampaignName"`
- `domain` → lookup салон/город/специалист из `local_gsheet_sites`
- JOIN лидов из `local_leads_all` по `utm_campaign × месяц`

Фильтр в `load_api_leads.py`: только строки где `utm_content ~ '^[0-9]{8}$'` и `utm_campaign IS NOT NULL`.

---

## 10. Как запустить

```bash
# На сервере Victory (103.88.240.90)
cd ~/big_analytics_v5

# Только fetch (сырые данные из API):
~/venv/bin/python3 crop_targeting/fetch_api.py

# Полный пайплайн посевов (все 4 шага):
~/venv/bin/python3 crop_targeting/pipeline.py

# В рамках понедельного полного пайплайна (автоматически):
~/venv/bin/python3 pipeline_monday.py
```

Локальная разработка: файлы синхронизируются через Mutagen (`HomeServer_PythonProject/ ↔ /opt/scripts/`).  
Деплой изменений на Victory: `scp` или через Mutagen автоматически.

---

## 11. Логирование

Скрипт пишет в stdout через `logging`:
```
2026-05-14 10:00:00 [INFO] === fetch_api_telegain start ===
2026-05-14 10:00:01 [INFO] Загружаем заказы...
2026-05-14 10:00:02 [INFO] Заказов: 42
2026-05-14 10:00:03 [INFO]   Страница 1: +87 строк | итого: 87
...
2026-05-14 10:00:10 [INFO] crop_targeting Telega.in API: загружено 312 строк (с 01.05.2026)
2026-05-14 10:00:10 [INFO] === Готово ===
```

Ошибки API: retry 4 раза с экспоненциальным ожиданием (10, 20, 40, 60 сек).  
429 Rate Limit: ожидание по `Retry-After`, retry до 5 раз.

---

## 12. Задача для дата-инженера

Перенести запуск этого скрипта (`fetch_api.py`) в управляемый пайплайн.

**Входные данные:**
- Telega.in API (credentials через `.secret/loader.py → load_telega()`)
- Параметр `API_DATE_FROM = date(2026, 5, 1)` — нижняя граница дат

**Выход:**
- Таблица `public.crop_targeting_api_telegain` в БД `ad_analytics_bi` на Victory VPS (PostgreSQL)

**Расписание:** раз в неделю (понедельник), вместе с остальным пайплайном посевов.

**Порядок запуска посевов:**
```
fetch_api.py              ← этот скрипт (сырые данные из API)
load_crop_targeting.py    ← Google Sheets данные
load_crop_targeting_leads.py
load_api_leads.py         ← зависит от fetch_api.py
load_crop_to_big_analytics.py
```

**Что НЕ нужно трогать:**
- Схему таблицы (менять только через миграции внутри скрипта)
- Логику UTM-парсинга и резолва редиректов
- `API_DATE_FROM` (без согласования)
