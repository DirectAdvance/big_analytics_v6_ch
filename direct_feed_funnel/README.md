# direct_feed_funnel — v6 ClickHouse

Активный v6-шаг: `direct_feed_funnel.build`, вызывается из корневого `pipeline.py` как step 144.

Он строит физическую `ad_analytics.fact_direct_feed_funnel_light` из `ad_analytics.direct_spend_staging`
дневными батчами без PostgreSQL и без старого keyed pipeline.

`ad_analytics.fact_direct_feed_funnel` оставлен как compatibility view для PBI и соседних витрин.
Тяжелый текстовый `placement_feed_key` вынесен из физического факта: в light-таблице хранится
`placement_feed_key_hash`, а view восстанавливает прежнюю колонку через `Dim_PlacementFeed`.
Step 144 сам обновляет `Dim_PlacementFeed` перед созданием view, поэтому `--only-step=144` не зависит
от предварительного запуска step 146.
`domain` и `account_login` пока оставлены в light-факте как низкорисковый компромисс: они намного
меньше по размеру и не зависят от полноты справочников при сверке данных.

Старые v5 helper-скрипты (`build_keyed.py`, `build_report_*`, `fetch_feed_urls_cookie.py`, `pipeline.py`)
перенесены в `archive/postgres_legacy_2026_07_31/`.
