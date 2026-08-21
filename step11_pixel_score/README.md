# step11_pixel_score — Pixel Attribution

Step11 материализует атрибутированный pixel-слой в ClickHouse и пересобирает
`ad_analytics.big_analytics_full` с каноническими pixel-строками.

## Live-Контроль

Замер после accepted run `ed6bfc6f9c23`:

| объект / ось | `_source_table` | строк |
|---|---|---:|
| `big_analytics_pixel_score` | `pixel` | 62 049 |
| `big_analytics_full` — по дате заявки | `pixel` | 62 049 |
| `big_analytics_full_arrival` — по дате визита | `pixel` | 30 019 |

Старый `Пиксель_атрибуц` выведен из BA6-контракта.

## Что Делает

1. Создаёт `big_analytics_pixel_score_new` по схеме `big_analytics_full`.
2. Дневными батчами атрибутирует pixel из `big_analytics_sources` к кампаниям
   `direct/crop_targeting/tp8/tp9/tp10`.
3. Публикует `big_analytics_pixel_score` через `swap_shadow`.
4. Создаёт `big_analytics_full_new`, переносит все non-pixel строки из текущего full.
5. Доливает канонический raw-pixel из `big_analytics_sources`.
6. Публикует `big_analytics_full` через `swap_shadow`.

## Формула

Кампания участвует, если за месяц имеет `cost > 0`, `clicks > 0` и положительный weighted score:

```text
score = (zayavki + 3*korr + 10*kval + 30*priezd + 100*prodazhi) / clicks
weight = score / sum(score) по (month, салон, domain)
```

Pixel-метрики умножаются на `weight`. Если у домена нет подходящих кампаний, остаток пишется с
`CampaignId=0` и `weight=100%`.

## Маркеры

- `_source_table='pixel'`
- `источник='Пиксель'`
- `направление='Пиксель'`
- `тип_заявки='Пиксель'`
- `direction='Авто'`
- `key_pixel_score = Date|domain|pixel|CampaignId`

## Проверки

```sql
SELECT count(), sum(total_cost), sum(kol_vo_zayavok)
FROM ad_analytics.big_analytics_pixel_score;

SELECT count(), sum(total_cost), sum(kol_vo_zayavok)
FROM ad_analytics.big_analytics_full
WHERE _source_table = 'pixel';
```

Подробнее по формуле: [`ALGORITHM.md`](ALGORITHM.md).
