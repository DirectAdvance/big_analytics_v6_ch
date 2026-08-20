# step5_build_pixel — Сборка пиксельных данных

<!-- pixel-dedup-2026-08-17 -->
> **PIXEL_DEDUP_2026-08-17.**
> Старый `Пиксель_атрибуц` выведен из BA6-контракта; live-канон пикселя —
> `_source_table='pixel'`, `источник='Пиксель'`, `направление='Пиксель'`.
> Как стало:
>
> | объект / ось | `_source_table` | строк |
> |---|---|---:|
> | `big_analytics_pixel_score` (физическая таблица) | `pixel` | 243 278 |
> | `big_analytics_full` — ось «По дате заявки» | `pixel` | 31 464 |
> | `big_analytics_full_arrival` — ось «По дате визита» | `pixel` | 85 160 |
>
> Визитную ось step13 читает из `big_analytics_pixel_score` напрямую и пишет
> `направление='Пиксель'`. Замер live ClickHouse: 2026-08-17.

Шаг 5 пайплайна. Собирает `big_analytics_pixel` — таблицу для пиксельных лидов из
канонического источника `reference_data.victory_pixel_answers FINAL` (`product='пиксель'`).

В отличие от `big_analytics_direct`, эта таблица НЕ попадает в `big_analytics_full` напрямую через step6 — атрибуция к кампаниям выполняется отдельно в step11 (`pixel_score`).

## Назначение

- Берёт одну строку `victory_pixel_answers` как одну засчитанную pixel-заявку.
- Берёт `total_cost` напрямую из `victory_pixel_answers.cost`.
- Маппит домен/салон к справочнику `reference_data.gsheet_sites`.

## Архитектурная схема

```
reference_data.victory_pixel_answers FINAL
              │ product='пиксель'
              ▼
reference_data.gsheet_sites ──► big_analytics_pixel
              (агрегаты + воронка + total_cost)
                       │
                       ▼ (НЕ через step6 — через step11)
              big_analytics_pixel_score (атрибуция к кампаниям)
                       │
                       ▼
              big_analytics_full (_source_table='pixel')
```

## Формула total_cost

```
total_cost = victory_pixel_answers.cost
```

Стоимость уже итоговая в канонической таблице, поэтому step5 её не пересчитывает.
`Date = coalesce(bill_day, date)`: в старых месяцах `bill_day` может быть пустой.

## Источник пикселя

Обязательные условия:
- читать `reference_data.victory_pixel_answers FINAL`;
- фильтровать `product = 'пиксель'`;
- использовать `site` как домен, `salon` как салон, `cost` как стоимость строки;
- период пикселя задаётся `bill_month`, дневная ось — `coalesce(bill_day, date)`.

## Параметры

- `T_PIXEL_SOURCE` = `'reference_data.victory_pixel_answers'`
- `T_GSHEET_SITES` = `'reference_data.gsheet_sites'`
- `T_PIXEL` = `'big_analytics_pixel'`

## Зависимости

- `reference_data.victory_pixel_answers`
- `reference_data.gsheet_sites`

## Примеры запуска

```bash
# Только step5 в составе pipeline:
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 pipeline.py --only-step=5"

# Аудит результатов:
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 step5_build_pixel/audit_pixels.py"
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 step5_build_pixel/check_pixel_table.py"
```

## Проверки после запуска

```sql
-- `big_analytics_pixel` должен сходиться с каноном по количеству и стоимости
SELECT count(), sum(cost)
FROM reference_data.victory_pixel_answers FINAL
WHERE product = 'пиксель' AND bill_month >= '2026-01'
UNION ALL
SELECT sum(kol_vo_zayavok), sum(total_cost)
FROM big_analytics_pixel;

-- Объёмы по доменам
SELECT domain, COUNT(*), SUM(kol_vo_zayavok), SUM(total_cost)
FROM big_analytics_pixel
GROUP BY domain ORDER BY 4 DESC LIMIT 20;

-- строки без site остаются в итогах по салону, но не участвуют в доменной атрибуции step11
SELECT count(), sum(total_cost)
FROM big_analytics_pixel
WHERE domain IS NULL;
```

## Файлы

| Файл | Описание |
|------|----------|
| `build_pixel.py` | Основной шаг 5 (build pixel_leads + big_analytics_pixel) |
| `sync_pixel_config.py` | Legacy sync старого конфига; step5 больше его не читает |
| `set_pixel_price.py` | Legacy CLI старой истории цен; step5 больше её не читает |
| `audit_pixels.py` | Legacy аудит старого конфига |
| `audit_pixels_detailed.py` | Legacy детальный аудит старого конфига |
| `check_pixel_table.py` | Проверка консистентности |
| `__init__.py` | Пустой |
| `CLAUDE.md` | Краткая инструкция для ИИ |
| `README.md` | Этот файл |

## Связи

- **Зависит от:** step0 + sync_pixel_config + step3 (DDL)
- **Используется:** step11_pixel_score (атрибуция через CR-composite weight)
- **НЕ через step6**: атрибуция выполняется step11 после построения `big_analytics_full`
