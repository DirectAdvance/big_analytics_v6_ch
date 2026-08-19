# step12_proverka_big_analytics — ClickHouse quality gate

Step 12 — быстрый fail-fast перед тяжёлым хвостом пайплайна. Он проверяет уже собранные
`ad_analytics.big_analytics_full` и `ad_analytics.big_analytics_sources`.

## Проверки

| Проверка | Что ловит |
|---|---|
| `missing:*` / `empty:*` | нет или пустые `big_analytics_full` / `big_analytics_sources` |
| `full_before_2026` | строки до `2026-01-01` |
| `full_null_source` | пустой `источник` |
| `full_funnel_korr_lt_kval` | `korr < kval` |
| `full_funnel_kval_lt_priezd` | `kval < priezd` |
| `full_funnel_priezd_lt_prodazhi` | `priezd < prodazhi` |
| `full_credit_lt_approved` | `dohod_do_kredita < dobro` |
| `direct_crop_key_overlap` | пересечение direct/crop по `key3` |
| `direct_crop_universe_overlap` | пересечение direct/crop на уровне UTM-предикатов |

`direct_crop_universe_overlap` важнее простой проверки результата: она ловит общую причину
задвоения, включая `direct_zero`, где `key3` вырожден и не годится как уникальный маркер лида.

## Запуск

```bash
cd ~/big_analytics_v6_ch && ~/venv-v6/bin/python3 pipeline.py --only-step=12
```

Прямой запуск модуля:

```bash
cd ~/big_analytics_v6_ch && ~/venv-v6/bin/python3 -m step12_proverka_big_analytics.step12
```

## Что больше не делает

Старый PostgreSQL/CSD-отчёт (`campaign_stats_daily`, `analytics_proverka_big_analytics`,
Telegram-рассылка с CRM-дельтами) относится к v5 legacy и в активном v6 step12 не выполняется.
