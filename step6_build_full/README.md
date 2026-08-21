# step6_build_full — `big_analytics_full`

Step6 собирает `ad_analytics.big_analytics_full` в ClickHouse батчами. Он объединяет
`big_analytics_sources` из step3 и отдельно пересобранные звонки `big_analytics_calls`.

## Контракт

- входы: `ad_analytics.big_analytics_sources`, `ad_analytics.raw_calls`, `reference_data.gsheet_sites`;
- временные объекты: `big_analytics_calls_new`, `big_analytics_full_new`;
- публикация: `swap_shadow`;
- дополнительная колонка full: `key_pixel_score = Date|domain|источник|CampaignId`;
- строки `_source_table='pixel'` из `big_analytics_sources` не вставляются в full на этом шаге.

## Звонки

`_rebuild_calls()` вставляет два прохода по каждому дневному окну:

| Проход | Фильтр | Канон |
|---|---|---|
| обычные звонки | `gs.direction='Авто'` и не посевной домен | `источник` = `Контекст`/`SEO`/`SEO Flow` |
| посевные звонки | hard crop-account домен или `direction_main='Посевы'` | `источник='Посевы_Звонки'`, `направление='Комплекс'` |

VK-Авто домены сохраняют приоритет VK над посевами.

## Full

`run()`:

1. пересобирает `ad_analytics.big_analytics_calls`;
2. создаёт `ad_analytics.big_analytics_full_new` по схеме `big_analytics_sources`;
3. вставляет дневные батчи из `big_analytics_sources`, кроме `_source_table='pixel'`;
4. вставляет дневные батчи из `big_analytics_calls`;
5. публикует `ad_analytics.big_analytics_full` через `swap_shadow`.

## Проверки

```sql
SELECT _source_table, count(), sum(total_cost)
FROM ad_analytics.big_analytics_full
GROUP BY _source_table
ORDER BY count() DESC;

SELECT `источник`, count()
FROM ad_analytics.big_analytics_full
WHERE `источник` LIKE 'Посевы_%'
GROUP BY `источник`
ORDER BY count() DESC;
```

Второй запрос не должен показывать мелкие `Посевы_<domain>` источники.

## Связи

- зависит от step3 source store;
- step12/star и Power BI читают уже опубликованный `big_analytics_full`;
- pixel добавляется своим шагом и сравнивается отдельно по заявочной/визитной осям.
