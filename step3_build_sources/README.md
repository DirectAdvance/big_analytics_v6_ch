# step3_build_sources — Source Store

Step3 собирает ClickHouse-таблицу `ad_analytics.big_analytics_sources` и совместимые view над ней:
`big_analytics_direct`, `big_analytics_seo`, `big_analytics_pixel`,
`big_analytics_crop_targeting`, `big_analytics_reviews`.

## Контракт

- источник истины для step6: `ad_analytics.big_analytics_sources`;
- запись идёт через shadow table `big_analytics_sources_new` и атомарный `swap_shadow`;
- тяжёлые ветки режутся на дневные батчи от `2026-01-01`;
- `big_analytics_pixel` как view есть, но `_build_pixel_sql()` намеренно падает: пиксель строит step5;
- reviews пока добираются bridge-запросом из Victory PostgreSQL, потому что raw для weekly reviews ещё не перенесён полностью.

## Основные Ветки

| `_source_table` | Что строит |
|---|---|
| `direct`, `tp8`, `tp9`, `tp10` | расходы и заявки Директа из `raw_yandex`/`raw_leads` |
| `direct_unmatched`, `direct_zero` | direct-заявки без точной пары в расходах |
| `seo` | SEO-заявки из `raw_leads` |
| `crop_targeting`, `telegram`, `social_посевы`, `vk_ads`, `vk_zero`, `vk_perform` | комплекс/посевы/VK ветки |
| `reviews`, `direct_account_reviews` | отзывы |

Мелкие `Посевы_<domain>` источники не должны появляться. Посевные звонки должны оставаться
канонически `Посевы_Звонки` и доезжают до full через step6 calls-ветку.

Для Direct-расхода домен выбирается так: `raw_leads.domain`, затем `raw_yandex.domain`, затем
fallback из `gsheet_sites` по логину и дате. Это сохраняет расход в срезе сайта; `gsheet_sites`
нужен только когда raw-строка не несёт домен.

## CRM И Воронка

- source type маппится в ключ CRM через `CRM_BY_SOURCE_TYPE`;
- отображаемое имя CRM берётся из `CRM_NAME_BY_SOURCE_TYPE`;
- `rivendell_excel` и `perform_api` показываются как `Ривендел`;
- неизвестный source type логируется, а отсутствующий ключ в `reference_data.crm_status_mapping`
  попадает в `crm_mapping_missing`;
- категории статусов считаются через `_metric_expr()` и `reference_data.crm_status_mapping`;
- `CODE_STATUS_CATEGORY` остаётся точечным мостом для статусов, которые нельзя записать в справочник.

## Проверки

```sql
SELECT _source_table, count(), sum(total_cost)
FROM ad_analytics.big_analytics_sources
GROUP BY _source_table
ORDER BY count() DESC;

SELECT count()
FROM ad_analytics.big_analytics_sources
WHERE `источник` LIKE 'Посевы_%' AND `источник` != 'Посевы_Звонки';
```

Второй запрос должен возвращать `0`.

## Связи

- зависит от step1/step2 raw-таблиц и reference/manual справочников, проверенных step0;
- step6 читает `big_analytics_sources` и отдельно добавляет `big_analytics_calls`;
- step5 после step3 добавляет канонический pixel-слой.
