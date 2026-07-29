# report_placement — Отчёт по площадкам Яндекс.Директ

Выгрузка данных Яндекс.Директ по **площадкам** (placements — конкретные сайты РСЯ + поиск) и объединение с лидами из `raw_leads`. Запускается **еженедельно** по cron каждую субботу в 00:00 МСК.

## Назначение

Стандартный отчёт Директа по кампаниям не показывает, на каких именно сайтах сети показывались объявления. `report_placement` тянет агрегацию по площадкам и матчит её с лидами — даёт ответ "сколько лидов пришло с avito.ru, drive2.ru и т.д.".

## Архитектурная схема

```
metrika_yandex (login_key, 5 целей)
        │
        ▼
step1_fetch_direct.py ──► Direct Reports API (по неделям, 15 воркеров)
                                │
                                ▼
                       public.analytics_report_placement
                       (UPSERT-инкремент: Direct-строки; лид-колонки = NULL/0)
                                │
                       step2_build_analytics.py ──◄── local_leads_all (step0)
                                │
                                ├──► A: сброс лид-данных Direct-строк (окно 61д)
                                ├──► B: UPDATE Direct-строк из local_leads_all по key2
                                ├──► C: DELETE старых leads-only строк (окно 61д)
                                └──► D: INSERT новых leads-only строк (utm_source без Direct)
                                                │
                                                ▼
                                       public.analytics_report_placement
                                       (обновляется на месте)
```

## Шаги

### `step1_fetch_direct.py`

Загружает данные из Яндекс.Директ Reports API по площадкам.

- **Источник аккаунтов:** `public.metrika_yandex` (login_key + 5 целей)
- **Период:** недельные батчи от `DATE_FROM` до вчера
- **Токены:** все 5 (`OAUTH_TOKEN_1`…`OAUTH_TOKEN_5`) + кэш `token_cache.json`; параллельно 15 воркеров (5 токенов × 3/токен)
- **Результат:** `public.analytics_report_placement` (UPSERT-инкремент напрямую)
- **Лимит зависания:** 40 попыток (~20 мин) на report через `wait_report`

Маппинг целей:
| Колонка БД | Название в Direct |
|------------|-------------------|
| `all_forms` | `Все формы` |
| `crm_order_created` | `CRM: Заказ создан` |
| `crm_order_paid` | `CRM: Заказ оплачен` |
| `crm_spam_order` | `CRM: Спам заказ` |
| `crm_order_canceled` | `CRM: Заказ отменен` |

### `step2_build_analytics.py`

Обогащает `analytics_report_placement` лидами из `local_leads_all` (инкремент 61 день, этапы A/B/C/D).

- **Статусы:** динамически через `build_leads_agg_sql(conn)` → `local_crm_statuses`
- **Результат:** обновляет `public.analytics_report_placement` на месте (A/B/C/D)
- **`statement_timeout = 600 сек`** (тяжёлый JOIN)

#### Ключ JOIN — `key2`

```
key2 = date | campaign_id | group_id | placement_key
```

`raw_leads` не имеет колонки `key2` — вычисляется на лету:

```sql
created_date::text
|| '|' || COALESCE(campaign_id::text, '0')
|| '|' || CASE WHEN utm_campaign ~* 'tp[67]' THEN '0'
               ELSE COALESCE(group_id::text, '0') END
|| '|' || CASE WHEN campaign_id IS NOT NULL AND placement = 'none' THEN 'yandex'
               ELSE LOWER(REGEXP_REPLACE(
                   COALESCE((regexp_match(utm_source, '(?:^|[^a-z])s:(.+)$'))[1], utm_source, ''),
                   '^(www\.|m\.)', ''))
          END
```

Особенности:
- `s:` стрипается через `regexp_match` (в любом месте строки, не только в начале)
- tp6/tp7 (МК/ТК): `group_id = '0'` принудительно
- `none` → `yandex` для Direct-трафика без конкретного плейсмента

#### Инкремент: логика 2 типов строк

- **Direct-строки (Этап B):** UPDATE из `local_leads_all` по `key2` (логин IS NOT NULL)
- **leads-only строки (Этап D):** INSERT площадок из `local_leads_all.utm_source`, которых **нет** в Direct; Direct-поля = NULL

#### Исключения в Части 2 (utm_source)

| Паттерн | Тип |
|---------|-----|
| `victory_` (substring) | внутренние |
| `victory` (exact) | внутренние |
| `seo` (exact) | органика |
| `_vdl` (substring) | внутренние |
| `vk_groups` (exact) | соцсети |
| `vk` (exact) | соцсети |
| `_pixel` (substring) | пиксели |

## Cron на Victory

```cron
# Площадки Direct — каждую субботу в 00:00 МСК (21:00 UTC пятница)
0 21 * * 5  cd ~/big_analytics_v5 && ~/venv/bin/python3 step_cron_night/report_placement/run.py >> /tmp/report_placement.log 2>&1
```

## Зависимости

- `metrika_yandex` (для login_key) — заполняется `metrika_yandex.py`
- `local_leads_all` (step0 — синк из ad_analytics, LOGGED)
- `local_crm_statuses` (для `build_leads_agg_sql`)
- OAuth токены Direct API
- 600s `statement_timeout`

## Примеры запуска

```bash
# Полный пайплайн (вручную):
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 step_cron_night/report_placement/run.py"

# Только step1 (fetch Direct API):
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 step_cron_night/report_placement/step1_fetch_direct.py"

# Только step2 (build analytics, требует свежий step1):
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 step_cron_night/report_placement/step2_build_analytics.py"
```

## Проверки после запуска

```sql
-- Объёмы (Direct-строки / leads-only)
SELECT CASE WHEN логин IS NOT NULL THEN 'direct' ELSE 'leads_only' END AS тип,
       COUNT(*) AS строк
FROM analytics_report_placement
GROUP BY 1;

-- Топ-площадки по расходам за неделю
SELECT placement_key, COUNT(*) AS rows,
       SUM(cost) AS spend_rub,
       SUM(kol_vo_zayavok) AS leads
FROM analytics_report_placement
WHERE date >= CURRENT_DATE - INTERVAL '7 days' AND placement_key NOT IN ('yandex','none')
GROUP BY 1 ORDER BY 3 DESC LIMIT 30;
```

## История фиксов (см. CLAUDE.md)

- 2026-04-20: Создание модуля
- 2026-04-23: Двойной REGEXP_REPLACE для `s:` (19,504 → 38,558 строк лидов, +98%)
- 2026-04-23 (2): `s:` через `regexp_match`, none→yandex, tp6/tp7 → group_id='0'
- 2026-04-28: Колонка `номер кампании|название кампании`
- 2026-05-15: Перенесена папка `report_placement/` → `step_cron_night/report_placement/`, обновлён sys.path
- 2026-06-01: Рефакторинг: step1 пишет напрямую в `analytics_report_placement` (UPSERT), step2 — 4-этапный инкремент A/B/C/D (см. CLAUDE.md)
- 2026-06-20: LEADSRC: лиды с `raw_leads` → `local_leads_all`; параллелизация step1 (15 воркеров, кэш `token_cache.json`)

## Связи

- **Зависит от:** `metrika_yandex` (login_key), `raw_leads`, `local_crm_statuses`
- **Используется:** Power BI страница "Площадки"

## Файлы

| Файл | Описание |
|------|----------|
| `run.py` | Точка входа (step1 → step2) |
| `step1_fetch_direct.py` | Direct Reports API → analytics_report_placement (UPSERT) |
| `step2_build_analytics.py` | Обогащение ARP лидами из local_leads_all (A/B/C/D) |
| `__init__.py` | Пустой |
| `CLAUDE.md` | Документация папки + история изменений |
| `README.md` | Этот файл |
