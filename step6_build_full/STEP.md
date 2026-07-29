# STEP.md — Шаг 4: big_analytics_full (UNION ALL)

## Что делает

Собирает `big_analytics_full` = UNION ALL всех source-таблиц + звонки inline.

```
big_analytics_full =
  big_analytics_direct          (расходы + лиды директ + tp8)
  UNION ALL
  big_analytics_crop_targeting  (посевы + лиды посевов)
  UNION ALL
  big_analytics_seo             (лиды без UTM)
  UNION ALL
  big_analytics_pixel           (pixel лиды)
  UNION ALL
  big_analytics_telegram        (telegram utm-посевы)
  UNION ALL
  raw_calls → GROUP BY домен+дата  (звонки)
```

## Важно

- **SELECT * запрещён** — все ветки перечисляют колонки явно (иначе при расхождении DDL данные тихо сместятся)
- Звонки строятся inline в этом шаге — своей таблицы у них нет
- Таблица создаётся как `UNLOGGED` → SET LOGGED в шаге 5
