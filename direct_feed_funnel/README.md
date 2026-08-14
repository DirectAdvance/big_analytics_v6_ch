# direct_feed_funnel — v6 ClickHouse

Активный v6-шаг: `direct_feed_funnel.build`, вызывается из корневого `pipeline.py` как step 144.

Он строит `ad_analytics.fact_direct_feed_funnel` из `raw_data.yandex_direct_report_rows` недельно/дневными
без PostgreSQL и без старого keyed pipeline.

Старые v5 helper-скрипты (`build_keyed.py`, `build_report_*`, `fetch_feed_urls_cookie.py`, `pipeline.py`)
перенесены в `archive/postgres_legacy_2026_07_31/`.
