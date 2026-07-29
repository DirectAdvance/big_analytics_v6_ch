# MEMORY.md — step12_proverka_big_analytics

## Урок 1 (2026-06-16): Дубли campaign_stats_daily — дедуп по (cid,day)

**Симптом:** Фаиг CSD 571.27M >> BI 557.00M, Δ +2.6% при том что BI ≈ Я.Директ API.

**Причина:** `ad_analytics.campaign_stats_daily` содержит ~2.5% (cid,day)-ключей с дублями
(повторённые одинаковые строки). `SUM(total_spend) GROUP BY campaign_id` суммирует их дважды.
Доказательство: cid 702792661 — csd_raw 929 726 = ×2 от BI 474 624, MAX-per-day = 474 624 = BI до копейки.

**Фикс (v7):** в `_CSD_PERIOD_SQL` — CTE `daily` с `MAX(total_spend) per (campaign_id,day)`,
затем `SUM(spend_day)` снаружи. Возвращаем также `csd_raw` и `dup_keys` для trust-индикатора.
Файл: `step12.py`, функция `_fetch_csd_period`, SQL `_CSD_PERIOD_SQL`.

**Результат после фикса:**
- Фаиг: 571.27M → 557.67M, Δ +2.6% → +0.12% ✅
- Плекс: 1.20%, Маркар: 0.54%, Мега: −0.11%, Иная: 2.17% — все ниже 5% порога

**Анти-паттерн:** нельзя доверять SUM напрямую из campaign_stats_daily без проверки на дубли.
Всегда добавлять CTE с GROUP BY (campaign_id, day) перед агрегацией по campaign_id.

## Урок 2 (2026-06-16): trust-индикатор — структура

Для каждой CRM с |Δ%| > 2% Telegram-сообщение показывает:
- Строка «csd дубли: K (cid,day)-ключей, −Z₽ срезано дедупом» (из `trust_dedup_cut`)
- Строка «Δ по N cid; top-1 cid=X: +Y₽ (Z% Δ); размазано / ⚠ концентрация»
  (порог: top-1 cid > 20% |Δ| → концентрация)

`bi_cid_total` получается через `_fetch_bi_cid_period` (один запрос за весь период после цикла).
`csd_by_cid_total` накапливается в основном цикле.
