# step5_build_pixel — Сборка пиксельных данных

<!-- pixel-dedup-2026-08-17 -->
> **PIXEL_DEDUP_2026-08-17.**
> Старый `Пиксель_атрибуц` выведен из BA6-контракта; live-канон пикселя —
> `_source_table='pixel'`, `источник='Пиксель'`, `направление='Пиксель'`.
> Как стало:
>
> | объект / ось | `_source_table` | строк |
> |---|---|---:|
> | `big_analytics_pixel` (source-layer) | `pixel` | 62 049 |
> | `fact_big_analytics` — ось «По дате заявки» | `pixel` | 62 049 |
> | `fact_big_analytics` — ось «По дате визита» | `pixel` | 30 019 |
>
> Визитную ось step13 читает из `big_analytics_pixel_score` напрямую и пишет
> `направление='Пиксель'`. Замер live ClickHouse: 2026-08-20, run_id `ed6bfc6f9c23`.

Шаг 5 пайплайна. Собирает `big_analytics_pixel` — гибридный слой пиксельных лидов:
до `2026-06-03` из `raw_data.leads_all`, с `2026-06-03` из
`reference_data.victory_answers FINAL` (`product='пиксель'`).

В отличие от `big_analytics_direct`, эта таблица НЕ попадает в `big_analytics_full` напрямую через step6 — атрибуция к кампаниям выполняется отдельно в step11 (`pixel_score`).

## Назначение

- До `2026-06-03` повторяет BA5: находит pixel-лиды в `raw_data.leads_all` через
  `local_pixel_config`, цену берёт из `local_pixel_price_history` или baseline-конфига.
- С `2026-06-03` берёт одну строку `victory_answers` как одну засчитанную pixel-заявку,
  а `total_cost` напрямую из `victory_answers.cost`.
- Для строк `victory_answers` матчит raw-лид по `phone + bill_month` и считает
  `korr/kval/priezd/prodazhi` по статусам `raw_data.leads_all`, как остальные BA6 lead-ветки.
- Маппит домен/салон к справочнику `reference_data.gsheet_sites`.

## Архитектурная схема

```
raw_data.leads_all (< 2026-06-03) + local_pixel_config / price_history
reference_data.victory_answers FINAL (>= 2026-06-03) + raw_data.leads_all statuses
              │
              ▼
reference_data.gsheet_sites ──► big_analytics_pixel
              │
              ▼
big_analytics_pixel_score ──► big_analytics_full (_source_table='pixel')
```

## Формула total_cost

```
Date <  2026-06-03: total_cost = kol_vo_zayavok * COALESCE(history.price, config.price)
Date >= 2026-06-03: total_cost = victory_answers.cost
```

После `2026-06-03` стоимость уже итоговая в канонической таблице, поэтому step5 её
не пересчитывает. `Date = coalesce(bill_day, date)`: в старых строках `bill_day`
может быть пустой.

Последний проверенный прогон: `big_analytics_pixel=62 049`, `total_cost=143 061 550.00`,
`kol_vo_zayavok=161 610`, `korr=115 615`, `kval=12 336`, `priezd=8 370`,
`prodazhi=646`, период `2026-01-02..2026-08-19`.

## Источник пикселя

Обязательные условия:
- читать `reference_data.victory_answers FINAL`;
- фильтровать `product = 'пиксель'`;
- использовать `site` как домен, `salon` как салон, `cost` как стоимость строки;
- период пикселя задаётся `bill_month`, дневная ось — `coalesce(bill_day, date)`.

## Параметры

- cutoff = `2026-06-03`
- reference source = `reference_data.victory_answers`
- legacy sources = `raw_data.leads_all`, `ad_analytics.local_pixel_config`,
  `ad_analytics.local_pixel_price_history`
- `T_GSHEET_SITES` = `'reference_data.gsheet_sites'`
- `T_PIXEL` = `'big_analytics_pixel'`

## Зависимости

- `reference_data.victory_answers`
- `raw_data.leads_all`
- `ad_analytics.local_pixel_config`
- `ad_analytics.local_pixel_price_history`
- `reference_data.gsheet_sites`

## Примеры запуска

```bash
# Только step5 в составе pipeline:
ssh victory "cd ~/big_analytics_v6_ch && ~/venv-v6/bin/python3 pipeline.py --only-step=5"

# Аудит результатов:
ssh victory "cd ~/big_analytics_v6_ch && ~/venv-v6/bin/python3 step5_build_pixel/audit_pixels.py"
ssh victory "cd ~/big_analytics_v6_ch && ~/venv-v6/bin/python3 step5_build_pixel/check_pixel_table.py"
```

## Проверки после запуска

```sql
-- reference-часть должна сходиться с каноном по количеству и стоимости
SELECT count(), sum(cost)
FROM reference_data.victory_answers FINAL
WHERE product = 'пиксель' AND coalesce(bill_day, date) >= '2026-06-03'
UNION ALL
SELECT count(), sum(total_cost)
FROM big_analytics_pixel
WHERE Date >= '2026-06-03'
  AND cascade_level = 'victory_answers_from_2026_06_03';

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
| `sync_pixel_config.py` | Legacy sync старого конфига; нужен для периода до `2026-06-03` |
| `set_pixel_price.py` | Legacy CLI старой истории цен; нужна для периода до `2026-06-03` |
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
