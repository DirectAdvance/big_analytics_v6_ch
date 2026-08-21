# direct_feed_funnel — v6 ClickHouse

Активный v6-шаг: `direct_feed_funnel.build`, вызывается из корневого `pipeline.py` как step 144.

Он строит физическую `ad_analytics.fact_direct_feed_funnel_light` из `ad_analytics.direct_spend_staging`
дневными батчами без PostgreSQL и без старого keyed pipeline.
Это агрегат по площадкам РСЯ/Direct placement, а не BA5-витрина товарных фидов.

`ad_analytics.fact_direct_feed_funnel` оставлен как compatibility view для PBI и соседних витрин.
Тяжелый текстовый `placement_feed_key` вынесен из физического факта: в light-таблице хранится
`placement_feed_key_hash`, а view восстанавливает прежнюю колонку через `Dim_PlacementFeed`.
Step 144 сам обновляет `Dim_PlacementFeed` перед созданием view, поэтому `--only-step=144` не зависит
от предварительного запуска step 146.
`domain` и `account_login` пока оставлены в light-факте как низкорисковый компромисс: они намного
меньше по размеру и не зависят от полноты справочников при сверке данных.

Старые v5 helper-скрипты перенесены в `archive/postgres_legacy_2026_07_31/`
(`direct_feed_funnel_build_keyed.py`, `direct_feed_funnel_build_report_feed.py`,
`direct_feed_funnel_fetch_feed_urls_cookie.py`).

На 2026-08-20 в ClickHouse есть `raw_data.direct_cookie_feed_urls` (42 096 строк), но нет
расходного `raw_data.direct_feed_report_rows`. Поэтому `Dim_PlacementFeed` содержит 35 441
placement-ключ и 0 заполненных `feed_name/feed_url`. Не подменять это join-ом
`campaign_id -> campaign_ids`: у части кампаний несколько фидов, такой join размножит расход.
