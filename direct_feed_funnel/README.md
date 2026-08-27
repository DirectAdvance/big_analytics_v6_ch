# direct_feed_funnel — v6 ClickHouse

Активный v6-шаг: `direct_feed_funnel.build`, вызывается из корневого `pipeline.py` как step 144.

Он строит физическую `ad_analytics.fact_direct_feed_funnel_light` из
`raw_data.direct_feed_report_rows`, обогащая URL/ключи из `raw_data.direct_cookie_feed_urls`.
Расход берётся из `cost`, а не `total_cost`: это совпадает с BA5 feed report.

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

На 2026-08-27 в ClickHouse есть оба feed-источника. `Dim_PlacementFeed` строится по реальным
`feed_url_key/feed_url/feed_name`; старый placement fallback остаётся только для окружений без
`raw_data.direct_feed_report_rows`. Не подменять это join-ом `campaign_id -> campaign_ids`: у части
кампаний несколько фидов, такой join размножит расход.
