# RUNBOOK.md — операционка и восстановление

> Что запускать, как чинить инциденты, как проверять. Подключения и режимы запуска —
> из [`CLAUDE.md`](CLAUDE.md) и [`PIPELINES.md`](PIPELINES.md). SQL-шпаргалка — [`QUERIES.md`](QUERIES.md).
> Перед каждой проверкой данных — золотая сверка [`GOLDEN_BASELINE.md`](GOLDEN_BASELINE.md).

---

## 0. Подключение (как ходить в БД)

| Способ | Команда |
|--------|---------|
| SSH на Victory | `ssh victory` |
| SQL с мака (read-only) | MCP `postgres-victory` (auto-approved) |
| SQL на Victory | `ssh victory '~/venv/bin/python3 ~/pgq.py "SELECT ..."'` |
| ⛔ psql на Victory | **СЛОМАН** (пустая обёртка) — НЕ использовать |

Все долгие операции на Victory — через `nohup` (см. `rule_victory_nohup`).

---

## 1. Три режима запуска пайплайна

| Режим | Команда | Когда | ~Время |
|-------|---------|-------|--------|
| **Полный дневной** | `ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 pipeline.py"` | штатный пересбор `big_analytics_*` | 30–40 мин |
| **Быстрый** | `… ~/venv/bin/python3 fast_pipeline.py` | без API Директа/step9 (кэш campaign_status/history) | 5–10 мин |
| **Ночной** | `… ~/venv/bin/python3 step_cron_night/pipeline_night.py` | cron 03:00 МСК: тяжёлые API (UTM-аудит, корректировки, 404, reviews) | ~2.5 ч |
| **С refresh PBI** | `… ~/venv/bin/python3 pipeline_powerbi.py` | сверка расходов → pipeline → триггер refresh PBI | 40–50 мин |
| **Один шаг** | `… ~/venv/bin/python3 pipeline.py --only-step=N` | точечная пересборка | — |
| **С шага N** | `… ~/venv/bin/python3 pipeline.py --from-step=N` | возобновление после сбоя | — |

> ⛔ Incremental refresh PBI **запрещён** (см. [`CLAUDE.md`](CLAUDE.md)).

---

## 2. «Диск заполнен» / bloat / VACUUM

**Симптом:** запись падает с `could not extend file` / `No space left` / `DiskFull` в `/dev/shm`.

**Был инцидент:** `big_analytics_direct` раздулся (bloat) до ~19 ГБ. Причина — таблица
часто пересобирается/обновляется, мёртвые кортежи не вычищались.

**Диагностика (размер + bloat):** см. [`QUERIES.md`](QUERIES.md) §«Размер/bloat».
```sql
SELECT relname, pg_size_pretty(pg_total_relation_size(relid)) AS total, n_dead_tup
FROM pg_stat_user_tables ORDER BY pg_total_relation_size(relid) DESC LIMIT 15;
```

**Лечение:**
1. Освободить место (старые `.log`/временные файлы на Victory).
2. `VACUUM (ANALYZE) public.big_analytics_direct;` — вернуть мёртвое место.
3. Если bloat не уходит — `VACUUM FULL` (блокирует таблицу, нужен запас места ×2) или
   пересоздание через CTAS + `TRUNCATE`+перезаливка (для регулярно пересобираемых таблиц).
4. ⚠️ При VACUUM больших таблиц на Victory ставить `SET max_parallel_maintenance_workers=0`
   (иначе DiskFull в `/dev/shm`) — как сделано в `star_refactor/build_star.py`.
5. Профилактика: `fillfactor`, UNLOGGED для промежуточных RAW (step7 переводит в LOGGED).

---

## 3. Проверка свежести данных

```sql
SELECT run_id, MAX(run_at) AS last_ok
FROM data_quality_log WHERE step='step8' AND status='ok';
```
- Если последний успешный `step8` был **>24 ч назад** → данные/PBI устарели.
- `data_check/run.py` делает эту проверку автоматически (флаг `stale`, см.
  [`data_check/README.md`](data_check/README.md)).
- Дрейф воронки на единицы при «давно не гонялся pipeline» — норма (CRM-лиды доезжают
  через UPSERT, full пересобирается только при прогоне). См. [`GOLDEN_BASELINE.md`](GOLDEN_BASELINE.md) п.4.

---

## 4. Ретриггер refresh Power BI

```bash
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 refresh_powerbi.py"
```
- Обновляет только датасет PBI Service (`_ALL_TABLES`), polling до 60 мин.
- Конфиг (`workspace_id`/`dataset_id`/`client_id/secret`/`tenant_id`) — `~/.secret/.env`
  через `loader.py` (⚠️ не `tokens.json`).
- `pipeline_powerbi.py` делает refresh сам после сверки расходов (стоп при |Σ| > 200 000 ₽).
- Refresh `Failed/Cancelled`/таймаут → Telegram-уведомление; перезапустить `refresh_powerbi.py`.
- **Host-rebind теперь автоматический (2026-06-12):** `refresh_powerbi.py` в начале каждого прогона
  сам чинит host-mismatch облачного датасета (`_ensure_datasource_host()` — идемпотентный ребайнд
  server → `analytics-marketing.ru` через `Default.UpdateDatasources`, no-op если уже канонический).
  После публикации модели из Desktop ручной rebind больше НЕ нужен (см. KNOWN_ISSUES #15).

**✅ Статус refresh (2026-06-11): проходит.** Последний прогон `refresh_powerbi.py` завершился
`статус = Completed` (лог `/tmp/refresh_powerbi_run.log`, 04:36 UTC) — все 13 таблиц обновились.
Перечисленные ниже блокеры **РЕШЕНЫ**, оставлены как исторический разбор для диагностики рецидива.

**🗂 Исторические (РЕШЁННЫЕ) блокеры refresh (2026-06, зона пользователя/`pbip_editor`):**
- ~~`The key didnt match any rows Key=[Schema="star", Item=...]`~~ → РЕШЕНО: звезда консолидирована
  в `public`, модель перепубликована из `public`.
- ~~`DMTS_DatasourceHasNoCredentialError Server='localhost'`~~ → **РЕШЕНО 2026-06-11**:
  параметр `ServerHost` переведён `localhost` → `103.88.240.90` (→ домен для SSL), datasource пересоздан,
  credentials привязаны. Признак `Server='localhost'` больше не воспроизводится.
  Разбор `serviceExceptionJson` из лога даёт точный источник при любом рецидиве. См. [`KNOWN_ISSUES.md`](KNOWN_ISSUES.md) #15.

---

## 5. Чек bloat / размеров таблиц (быстрый)

См. готовые запросы в [`QUERIES.md`](QUERIES.md): размеры таблиц, `n_dead_tup`,
`pg_total_relation_size`. Для разовой проверки после прогона — топ-15 таблиц по размеру.

---

## 6. Типовые инциденты → реакция

| Симптом | Реакция |
|---------|---------|
| Протухли куки Я.Директ (step4/9 без статусов) | `config/cookies.py` self-healing авто-рефрешит с glavpotok; если мёртво → Telegram + стоп |
| `\|Σ расход\|` > 200 000 ₽ | `pipeline_powerbi.py` стоп + retry через 30 мин, PBI не обновляется |
| `big_analytics_full` пустой/мало строк | идёт CTAS-пересборка — дождаться `count > 1M`; иначе один источник пуст → проверить step3 |
| `DEGRADED` в финале пайплайна | основной `fact_big_analytics` собран, но один из `fact_*_spend` не обновился. Проверить `data_quality_log` по шагам `build_region_spend`, `build_adformat_spend`, `build_criterion_spend`; если Power BI читает эти витрины, перезапустить упавший билдер или полный/быстрый пайплайн |
| refresh PBI завис | проверить polling-лог, перезапустить `refresh_powerbi.py` |
| Квота Метрики 429 | 2 токена + backoff (`step13_utm_direct_audit`); подождать окно квоты |
| Колонки витрины «сдвинулись» | проверить порядок в `step6` `COLS` и каждой ветке UNION ALL |
| `RuntimeError: RAW_YANDEX_COST_GUARD` в step1 | FDW вернул строки с нулевым расходом (транзиент). Проверить `SELECT SUM(total_cost) FROM local_yandex` → повторить step1 после восстановления FDW |

Точки отказа целиком — [`PROJECT_CHARTER.md`](PROJECT_CHARTER.md) §7.

---

## 7. Предохранитель step1: нулевой расход в raw_yandex (RAW_YANDEX_COST_GUARD_2026-06-22)

**Симптом.** step1 падает с `RuntimeError: RAW_YANDEX_COST_GUARD_2026-06-22: raw_yandex.total_cost=0`.

**Причина.** После загрузки `raw_yandex` выполняется `SELECT SUM(total_cost)`. Если = 0 — прогон
останавливается до step2. Это защита от транзиентного сбоя FDW: внешняя таблица могла вернуть
строки, но с нулевым расходом — без предохранителя пайплайн отработал бы молча и golden дал
расход = 0 (инцидент 2026-06-22).

**Диагностика:**
```sql
-- Проверить источник (на Victory через pgq.py):
SELECT SUM(total_cost), COUNT(*) FROM public.local_yandex;
-- Если тоже 0 — FDW-сбой, ждать восстановления и перезапустить step0→step1
-- Если local_yandex норм — step1 загрузился неверно, повторить step1
```

**Лечение.** Дождаться восстановления FDW (или перезапустить step0 если local_yandex тоже пустой),
затем повторить step1. Пайплайн с `--from-step=1` подхватит дальше.

---

## 8. Восстановление `campaign_status`/`payment_model` в `Dim_Campaign` (просели до ~906)

**Симптом.** В `public."Dim_Campaign"` `campaign_status`/`payment_model` непусты у ~906 строк
вместо ~5905 → в PBI «слетели» слайсеры статуса кампании / типа оплаты.
**Корень.** Партиал-прогон `pipeline.py --from-step=3` пропускает prefetch (блок `step_num==0`),
campaign_status не строится.
**Лечение (БЕЗ полного pipeline, НЕ трогает locked SRC):**
1. `nohup ~/venv/bin/python3 _warm_campaign_status.py >/tmp/warm.log` — Фаза A `prefetch_statuses()`
   (Grid API по кукам) + Фаза B `step4.run` (строит `campaign_status`). step4 не читает
   `yandex_direct_manager_reports` (это только step0). ~17 мин. ⚠️ проверить, что прошлый warm
   не запущен (двойной warm = двойная запись prefetch).
2. `nohup ~/venv/bin/python3 star_refactor/build_star.py >/tmp/bs.log` — пересобирает `Dim_Campaign`
   FROM `public.campaign_status`. `build_fact` = лёгкая проекция unified, golden НЕ ломается. ~2 мин.
3. Проверить: `count(campaign_status)`/`count(payment_model)` в `Dim_Campaign` ≈ 5905 + golden цел.

## 9. Обход чужого SRC-лока на `ad_analytics.yandex_direct_manager_reports`

Если чужая транзакция держит лок на SRC (его читает только **step0**), а нужно применить фикс
витрины — `pipeline.py --from-step=3` пересобирает всё начиная со step3, читая только DST
(`ad_analytics_bi`, raw_*/local_*), SRC не трогая. `init_src_pool` лишь connect (на локе не виснет).
Перед запуском убедиться, что raw_*/local_* от прошлого прогона целы (exact COUNT, не n_live_tup).
⚠️ При `--from-step=3` cs/pm в Dim_Campaign просядут (см. блок 8) — добить warm+build_star.

---

## 10. После любого восстановления — верифицировать

1. Золотая сверка ([`GOLDEN_BASELINE.md`](GOLDEN_BASELINE.md)): расход и продажи Кудерко
   обязаны совпасть до копейки.
2. Инварианты: `источник IS NULL`=0, `"Date" < '2026-01-01'`=0, воронка вложена.
3. `data_check/run.py --tg` — общий health-check + Telegram-отчёт.

---

## 11. Статус DEGRADED по spend-витринам

`build_region_spend`, `build_adformat_spend`, `build_criterion_spend` пишут отдельные
витрины `fact_region_spend`, `fact_adformat_spend`, `fact_criterion_spend`. Они не входят
в основной `fact_big_analytics`, поэтому их падение не останавливает публикацию основного
факта. При этом прогон больше не считается чисто успешным:

```sql
SELECT step, status, duration_sec, details
FROM data_quality_log
WHERE run_id = '<run_id>'
  AND step IN ('build_region_spend', 'build_adformat_spend',
               'build_criterion_spend', 'pipeline_degraded')
ORDER BY id;
```

Ожидаемая реакция: если PBI использует эти `fact_*_spend`, повторить упавший билдер или
перезапустить `fast_pipeline.py`/`pipeline.py`. Если PBI смотрит только на
`fact_big_analytics`, основной факт свежий, но spend-разрезы остаются старыми/неполными
до следующей успешной сборки.
