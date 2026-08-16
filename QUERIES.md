# QUERIES.md — SQL-шпаргалка

<!-- v6-scope-banner -->
> 🧭 **Область в v6_ch (2026-08-15).** Шпаргалка написана под PostgreSQL v5 (MCP
> `postgres-victory`). Для активного контура v6 использовать MCP `clickhouse-victory`
> (БД `ad_analytics` / `raw_data`) или `config/ch_db.get_client()`; синтаксис ClickHouse,
> кириллические идентификаторы — в обратных кавычках.

> Готовые read-only запросы под **MCP `postgres-victory`** (с мака, auto-approved) или
> `ssh victory '~/venv/bin/python3 ~/pgq.py "..."'`. ⛔ psql на Victory сломан.
> Перед изменениями данных — золотая сверка [`GOLDEN_BASELINE.md`](GOLDEN_BASELINE.md).

---

## 1. Золотая сверка (Кудерко) — главный oracle

Жёсткий инвариант: расход **25 422 774.00**, продажи **47** (по дате заявки, без пикселя,
**БЕЗ датного фильтра**). Воронка ~5069/1752/575 дрейфует ± по UPSERT.
Расход и продажи **обязаны** совпасть. Полная методика — [`GOLDEN_BASELINE.md`](GOLDEN_BASELINE.md).

```sql
SELECT
    ROUND(SUM(total_cost)::numeric, 2)  AS rashod,
    ROUND(SUM(kol_vo_zayavok))          AS obrashcheniya,
    ROUND(SUM(kval))                    AS kvaly,
    ROUND(SUM(priezd))                  AS vizity,
    ROUND(SUM(prodazhi))                AS prodazhi
FROM public.fact_big_analytics          -- (или public.big_analytics_unified — идентично по расходу/продажам)
WHERE "специалист" = 'Кудерко Семен'
  AND "атрибуция" = 'По дате заявки'
  AND _source_table IN ('direct','tp8','tp9','tp10','seo','calls','direct_unmatched','direct_zero');
```

> ⚠️ Читать `public.fact_big_analytics` (лёгкий факт звезды, схема `public` — star
> консолидирована) либо `big_analytics_unified`; фильтровать по `атрибуция`, исключать
> пиксель, суммировать дробные меры (не int), **не добавлять фильтр по `"Date"`**. См. [`ATTRIBUTION.md`](ATTRIBUTION.md).
> tp9/tp10 в этом срезе не влияют на Кудерко (их специалисты — Немытов/Вильцин), но список полный для согласованности.

---

## 2. Срез воронки по проджекту (период)

```sql
SELECT COALESCE(проджект, domain) AS проджект,
       ROUND(SUM(total_cost)::numeric,2) AS rashod,
       ROUND(SUM(kol_vo_zayavok)) AS zayavok,
       ROUND(SUM(korr)) AS korr, ROUND(SUM(kval)) AS kval,
       ROUND(SUM(priezd)) AS priezd, ROUND(SUM(prodazhi)) AS prodazhi
FROM public.big_analytics_full
WHERE "Date" >= CURRENT_DATE - 30
GROUP BY 1 ORDER BY rashod DESC;
```

---

## 3. Атрибуция по дате (заявка vs визит)

```sql
-- сравнить приезды/продажи по двум атрибуциям в unified
SELECT "атрибуция",
       ROUND(SUM(priezd))   AS priezd,
       ROUND(SUM(prodazhi)) AS prodazhi
FROM public.big_analytics_unified
WHERE "Date" BETWEEN '2026-01-01' AND '2026-06-04'
  AND _source_table IN ('direct','tp8','tp9','tp10','seo','calls')
GROUP BY 1;
-- эталон без пикселя: заявка priezd 34157 / prodazhi 2384; визит priezd 42979 / prodazhi 2838
```

---

## 4. Размер / bloat таблиц

```sql
-- топ-15 таблиц по полному размеру + мёртвые кортежи
SELECT relname,
       pg_size_pretty(pg_total_relation_size(relid)) AS total,
       pg_size_pretty(pg_relation_size(relid))       AS heap,
       n_live_tup, n_dead_tup
FROM pg_stat_user_tables
ORDER BY pg_total_relation_size(relid) DESC
LIMIT 15;
```
```sql
-- размер одной таблицы
SELECT pg_size_pretty(pg_total_relation_size('public.big_analytics_full'));
```
Лечение bloat (VACUUM / TRUNCATE+перезаливка, `max_parallel_maintenance_workers=0`) —
[`RUNBOOK.md`](RUNBOOK.md) §2.

---

## 5. Свежесть данных

```sql
SELECT step, MAX(run_at) AS last_ok
FROM data_quality_log
WHERE status='ok'
GROUP BY step ORDER BY last_ok DESC;
```
```sql
-- последние прогоны pipeline
SELECT run_id, run_at, step, status, rows_affected, duration_sec
FROM data_quality_log ORDER BY run_at DESC LIMIT 30;
```

---

## 6. Инварианты (быстрый health-check)

```sql
-- источник никогда не NULL (ожидается 0)
SELECT count(*) FROM public.big_analytics_full WHERE источник IS NULL;
-- дата-граница (ожидается 0)
SELECT count(*) FROM public.big_analytics_full WHERE "Date" < '2026-01-01';
-- нет двойного учёта: пересечение direct ∩ crop по лидам (ожидается 0)
```

---

## 7. Колонки витрины (интроспекция)

```sql
SELECT ordinal_position, column_name, data_type
FROM information_schema.columns
WHERE table_schema='public' AND table_name='big_analytics_full'
ORDER BY ordinal_position;
```
Словарь колонок со смыслом и step-источником — [`COLUMNS_big_analytics_full.md`](COLUMNS_big_analytics_full.md).

---

## 8. Лёгкий факт звезды (сверка new vs old; схема `public` — star консолидирована)

```sql
-- агрегаты лёгкого факта по атрибуции (должны совпасть с unified)
SELECT "атрибуция", count(*),
       ROUND(SUM(total_cost)::numeric,2) AS cost,
       ROUND(SUM(priezd)) AS priezd, ROUND(SUM(prodazhi)) AS prodazhi
FROM public.fact_big_analytics   -- было star.fact_big_analytics; схема star упразднена 2026-06-10
GROUP BY 1;
```
Полная верификация — `star_refactor/verify_star.py` ([`STAR_REFACTOR_BRIEF.md`](STAR_REFACTOR_BRIEF.md)).

---

## 9. Типовые запросы (перенесено из `anton_sql.md`)

### Воронка по салонам за период

```sql
SELECT "салон",
    SUM(kol_vo_zayavok) AS обращения,
    SUM(korr) AS корр,
    SUM(kval) AS квал,
    SUM(priezd) AS визит,
    SUM(prodazhi) AS продажи,
    SUM(total_cost) AS расходы,
    ROUND(SUM(total_cost) / NULLIF(SUM(prodazhi), 0), 0) AS cpo
FROM big_analytics_full
WHERE "Date" BETWEEN '2026-05-01' AND '2026-05-31'
  AND тип_заявки != 'звонки'
GROUP BY "салон"
ORDER BY расходы DESC;
```

### Активные аккаунты Директа

```sql
SELECT DISTINCT login_key
FROM local_gsheet_sites
WHERE status = 'Контекст активно'
  AND login_key IS NOT NULL AND login_key != ''
  AND login_key ~ '^[[:ascii:]]+$';  -- только ASCII
```

### Аномалии воронки (visits > qualified leads)

```sql
SELECT domain, "салон", SUM(priezd) AS визитов, SUM(kval) AS квал
FROM big_analytics_full
WHERE "Date" >= CURRENT_DATE - INTERVAL '30 days'
  AND тип_заявки = 'заявки'
GROUP BY domain, "салон"
HAVING SUM(priezd) > SUM(kval);
```

### Топ расходов без лидов

```sql
SELECT "CampaignName", SUM(total_cost) AS cost, SUM(kol_vo_zayavok) AS leads
FROM big_analytics_direct
WHERE "Date" >= CURRENT_DATE - INTERVAL '7 days'
GROUP BY "CampaignName"
HAVING SUM(kol_vo_zayavok) = 0 AND SUM(total_cost) > 0
ORDER BY cost DESC LIMIT 20;
```

### Статистика data_quality_log последнего запуска

```sql
SELECT step, status, rows_affected, ROUND(duration_sec::numeric, 1) AS sec
FROM data_quality_log
WHERE run_id = (SELECT run_id FROM data_quality_log ORDER BY run_at DESC LIMIT 1)
ORDER BY run_at;
```

---

## 10. Паттерны оптимизации (перенесено из `anton_sql.md`)

### Индексы в pipeline

После CTAS UNION ALL в step6, перед UPDATE'ами создаются временные индексы:
```sql
CREATE INDEX IF NOT EXISTS idx_full_tmp_cmpid  ON big_analytics_full ("CampaignId");
CREATE INDEX IF NOT EXISTS idx_full_tmp_domain ON big_analytics_full (domain);
CREATE INDEX IF NOT EXISTS idx_full_tmp_salon  ON big_analytics_full ("салон");
CREATE INDEX IF NOT EXISTS idx_full_tmp_src    ON big_analytics_full (_source_table);
ANALYZE big_analytics_full;
```

### Оптимизация bulk UPDATE (паттерн _tmp_*_agg)

Три UPDATE по салону → один через UNLOGGED lookup:
```sql
CREATE UNLOGGED TABLE _tmp_salon_aggs AS
SELECT "салон",
    MAX("Название crm") FILTER (WHERE "Название crm" IS NOT NULL) AS crm_name,
    MAX(manager_login) FILTER (WHERE manager_login IS NOT NULL
        AND manager_login NOT IN ('отзывы','посевы')) AS mgr_real,
    MAX(проджект) FILTER (WHERE проджект IS NOT NULL) AS proj
FROM big_analytics_full WHERE "салон" IS NOT NULL GROUP BY "салон";

UPDATE big_analytics_full f
SET "Название crm" = COALESCE(f."Название crm", t.crm_name),
    manager_login  = COALESCE(f.manager_login, t.mgr_real),
    проджект       = COALESCE(f.проджект, t.proj)
FROM _tmp_salon_aggs t
WHERE f."салон" = t."салон"
  AND (f."Название crm" IS NULL OR f.manager_login IS NULL OR f.проджект IS NULL);

DROP TABLE _tmp_salon_aggs;
```

### UPSERT паттерн

```sql
INSERT INTO campaign_status (login, campaign_id, campaign_name, status, updated_at)
VALUES (%s, %s, %s, %s, NOW())
ON CONFLICT (login, campaign_id) DO UPDATE
SET status = EXCLUDED.status, updated_at = EXCLUDED.updated_at;
```

### Batch INSERT

```python
psycopg2.extras.execute_batch(cur, INSERT_SQL, rows, page_size=1000)
```

### UNLOGGED для промежуточных таблиц

```sql
CREATE UNLOGGED TABLE _tmp_ag_parts_lookup AS ...;
-- INSERT в 2-3x быстрее без WAL
-- Дропать после использования
```
