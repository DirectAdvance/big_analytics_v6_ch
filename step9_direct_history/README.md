# step9_direct_history — история Директа в v6_ch

Step 9 строит совместимую таблицу для Power BI как ClickHouse view
`ad_analytics.yandex_direct_history`.

## Что делает

`step9.py` читает `reference_data.direct_campaigns`, превращает текущий snapshot кампаний в события
`campaign_snapshot` и добавляет `директолог`, `domain`, `salon` из `reference_data.gsheet_sites`.
Это не исторический GraphQL-журнал v5: в v6 нет постраничной загрузки direct.yandex.ru, куки,
`DAYS_BACK`, `REQUEST_DELAY` и фонового API-prefetch. `prefetch_history()` оставлен no-op только для
совместимости с корневым `pipeline.py`.

## IN / OUT

**IN:**
- `reference_data.direct_campaigns`
- `reference_data.gsheet_sites`

**OUT:**
- `ad_analytics.yandex_direct_history` (view)

Колонки view: `datetime`, `login`, `campaign_id`, `campaign_name`, `event_type`,
`change_source`, `old_value`, `new_value`, `директолог`, `domain`, `salon`, `updated_at`.

## Запуск

```bash
cd ~/big_analytics_v6_ch && ~/venv-v6/bin/python3 pipeline.py --only-step=9
```

Проверка:

```sql
SELECT max(datetime), count() FROM ad_analytics.yandex_direct_history;
```

## Ограничение

В v6 это snapshot, а не полный журнал действий пользователя. Если отчёту снова понадобится
`userActionLog`, нужен отдельный порт GraphQL-логики на ClickHouse.
