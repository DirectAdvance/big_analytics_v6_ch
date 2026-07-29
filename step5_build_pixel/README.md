# step5_build_pixel — Сборка пиксельных данных

Шаг 5 пайплайна. Собирает `big_analytics_pixel` — таблицу для пиксельных лидов (источники не привязаны к конкретной Direct-кампании, домен указан через `utm_source`).

В отличие от `big_analytics_direct`, эта таблица НЕ попадает в `big_analytics_full` напрямую через step6 — атрибуция к кампаниям выполняется отдельно в step11 (`pixel_score`).

## Назначение

- Считает воронку по pixel-доменам (домен = `utm_source`)
- Использует Google Sheets-конфиг для подсчёта `total_cost`
- Разделяет лиды на `pixel_leads` (валидные домены) и `pixel_leads_check` (для аудита)

## Архитектурная схема

```
Google Sheets ──sync_pixel_config──► local_pixel_config (pixel_name, cost_per_lead, cost_total)
                                                │
local_leads_all ──JOIN──────────────────────────┤
                                                ▼
                                       _pixel_leads_raw (промежуточная)
                                                │
                       ┌────────────────────────┤
                       │                        │
                  pixel_leads             pixel_leads_check
                  (валидные)              (остальные, для проверки)
                       │
                       ▼
              big_analytics_pixel
              (агрегаты + воронка + total_cost)
                       │
                       ▼ (НЕ через step6 — через step11)
              big_analytics_pixel_score (атрибуция к кампаниям)
                       │
                       ▼
              big_analytics_full (_source_table='пиксель_атрибуц')
```

## Формула total_cost

```
total_cost = kol_vo_zayavok × COALESCE(cost_per_lead, cost_total, 0)
```

Логика: если в Google Sheets указан раздельный `cost_per_lead` — используем его, иначе общая стоимость `cost_total`, иначе 0.

## Google Sheets конфиг пикселей

| Параметр | Значение |
|----------|----------|
| Spreadsheet ID | `1TIiLbeAL9_th6tYT65X_zHmEZqtiKMBPsypTWRK_mYo` |
| Лист | `Лист1` |
| Диапазон | `A:E` |
| Service account | `.secret/service_account.json` (Mac, walk-up) / `~/.secret/service_account.json` (Victory) / `config/cedar-gearbox-464117-e5-676d6cc8937e.json` (запасной) |

| Колонка | Описание |
|---------|----------|
| A — salon | Салон |
| B — project | Проект |
| C — cost_per_lead | ЦЗ за лид (раздельно) |
| D — cost_total | Общая ЦЗ |
| E — pixel_name | Имя пикселя (= `source_name` в `leads_all`) |

## JOIN-условие

```sql
JOIN local_pixel_config pc ON (
    l.source_name = pc.pixel_name
    OR LOWER(l.utm_source) = LOWER(pc.pixel_name)
)
```

Два условия — потому что `pixel_name` в разных лидах может быть в `source_name` или в `utm_source`.

## Валидация доменов

`pixel_leads` содержит только лиды с `domain IN (SELECT domain FROM local_gsheet_sites)`. Остальные — в `pixel_leads_check` (для аудита).

## Параметры

- `T_LEADS_ALL` = `'local_leads_all'`
- `T_PIXEL_CONFIG` = `'local_pixel_config'`
- `T_PIXEL_PRICE_HISTORY` = `'local_pixel_price_history'`
- `T_GSHEET_SITES` = `'local_gsheet_sites'`
- `T_PIXEL` = `'big_analytics_pixel'`

## Зависимости

- step0 (`local_leads_all`, `local_gsheet_sites`)
- `sync_pixel_config` (запускается параллельно как фон в `pipeline.py`)
- `local_pixel_price_history` (персистентная таблица override-тарифов; управляется `set_pixel_price.py`, НЕ truncate'ится пайплайном)
- Google Sheets API + service account JSON

## Примеры запуска

```bash
# Только step5 в составе pipeline:
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 pipeline.py --only-step=5"

# Только sync конфига пикселей:
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 step5_build_pixel/sync_pixel_config.py"

# Аудит результатов:
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 step5_build_pixel/audit_pixels.py"
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 step5_build_pixel/check_pixel_table.py"
```

## Проверки после запуска

```sql
-- Сколько лидов попало в pixel_leads vs pixel_leads_check
SELECT 'valid' AS t, COUNT(*) FROM pixel_leads
UNION ALL SELECT 'check', COUNT(*) FROM pixel_leads_check;

-- Объёмы по доменам
SELECT domain, COUNT(*), SUM(kol_vo_zayavok), SUM(total_cost)
FROM big_analytics_pixel
GROUP BY domain ORDER BY 4 DESC LIMIT 20;

-- total_cost=0 — проверить cost_per_lead/cost_total в Google Sheets
SELECT domain, COUNT(*) FROM big_analytics_pixel
WHERE total_cost = 0 GROUP BY domain;
```

## Файлы

| Файл | Описание |
|------|----------|
| `build_pixel.py` | Основной шаг 5 (build pixel_leads + big_analytics_pixel) |
| `sync_pixel_config.py` | Sync конфига из Google Sheets |
| `set_pixel_price.py` | CLI-управление дата-эффективной историей цен (`local_pixel_price_history`) |
| `audit_pixels.py` | Аудит общий |
| `audit_pixels_detailed.py` | Аудит детальный |
| `check_pixel_table.py` | Проверка консистентности |
| `__init__.py` | Пустой |
| `CLAUDE.md` | Краткая инструкция для ИИ |
| `README.md` | Этот файл |

## Связи

- **Зависит от:** step0 + sync_pixel_config + step3 (DDL)
- **Используется:** step11_pixel_score (атрибуция через CR-composite weight)
- **НЕ через step6**: атрибуция выполняется step11 после построения `big_analytics_full`
