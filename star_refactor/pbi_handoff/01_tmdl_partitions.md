# TMDL-фрагменты M-запросов для star-таблиц (Power BI)

> ⚠️ **2026-06-11: схема `star` УПРАЗДНЕНА → читать из `public`.** Звезда консолидирована в
> `public` 2026-06-10. В M-запросах ниже подставляй `Schema="public"` (НЕ `"star"`), иначе
> refresh падает `key didn't match Schema="star"`. См. [`00_README_NEED_FROM_USER.md`](00_README_NEED_FROM_USER.md).
>
> Готовые M-выражения (partition source) для 4 dim + 2 факта (исходно в схеме `star`, теперь `public`)
> БД `ad_analytics_bi` @ Victory `103.88.240.90`. Режим — **Import** (как у текущей модели).
>
> ВАЖНО: точные имена `server` и `database` бери из СУЩЕСТВУЮЩЕГО M-запроса
> текущей таблицы `big_analytics_full` в твоей модели (там уже прописан хост/порт/креды).
> Ниже placeholder `#"PG.Server"` / `#"PG.Database"` — подставь те же, что в живой модели.
> Креды (bi_analytic / пароль) PBI хранит отдельно — они НЕ в M-запросе.

---

## Как применять (2 пути)

**Путь A (рекомендуется — через Power BI Desktop UI, безопасно):**
1. Power Query → «Новый источник» → PostgreSQL → тот же сервер/БД, что у `big_analytics_full`.
2. В навигаторе раскрыть схему `star` → выбрать таблицу → Load.
3. Переименовать таблицу в редакторе как в колонке «Имя в модели» ниже.
4. Так PBI сам сгенерит корректный M с правильным форматом источника (Npgsql vs ODBC).

**Путь B (правка TMDL напрямую — только если модель уже PBIP и ты понимаешь формат):**
Вставить блоки ниже как `partition` внутрь `table '<Имя>'` в соответствующем `.tmdl`.
Формат M-источника СВЕРИТЬ с `big_analytics_full.tmdl` (PostgreSQL.Database vs Sql.Database).

---

## Таблицы и имена в модели

| Имя в модели (table name) | Источник | Тип |
|---|---|---|
| `Dim_Date`        | `star."Dim_Date"`            | измерение (пометить как таблицу дат) |
| `Dim_Campaign`    | `star."Dim_Campaign"`        | измерение |
| `Dim_AdGroup`     | `star."Dim_AdGroup"`         | измерение |
| `Dim_Site`        | `star."Dim_Site"`            | измерение |
| `big_analytics_full` (переназначить source) | `star.fact_big_analytics` | факт (лёгкий) |
| `arp_fact` (новая, заменяет analytics_report_placement) | `star.arp_fact` | факт ARP |

> `big_analytics_full` в модели НЕ переименовывай — просто перенацель его partition
> на `star.fact_big_analytics`. Все меры `(атрибуция)` / claim останутся рабочими, т.к.
> колонки `атрибуция`, total_cost, priezd, prodazhi и т.д. в факте сохранены 1:1.

---

## M-выражения (partition source)

### Dim_Date
```m
let
    Source = #"PG.Database",
    star = Source{[Schema="star", Item="Dim_Date"]}[Data]
in
    star
```

### Dim_Campaign
```m
let
    Source = #"PG.Database",
    star = Source{[Schema="star", Item="Dim_Campaign"]}[Data]
in
    star
```

### Dim_AdGroup
```m
let
    Source = #"PG.Database",
    star = Source{[Schema="star", Item="Dim_AdGroup"]}[Data]
in
    star
```

### Dim_Site
```m
let
    Source = #"PG.Database",
    star = Source{[Schema="star", Item="Dim_Site"]}[Data]
in
    star
```

### fact_big_analytics (перенацелить partition таблицы `big_analytics_full`)
```m
let
    Source = #"PG.Database",
    star = Source{[Schema="star", Item="fact_big_analytics"]}[Data]
in
    star
```

### arp_fact (заменяет `analytics_report_placement`)
```m
let
    Source = #"PG.Database",
    star = Source{[Schema="star", Item="arp_fact"]}[Data]
in
    star
```

`#"PG.Database"` — это `PostgreSQL.Database("<server>", "<database>")` ровно с теми же
аргументами, что в текущем `big_analytics_full` partition. Скопируй строку Source оттуда.

---

## Колонки таблиц (что прилетит) — сверка с БД (2026-06-06)

### star."Dim_Date" (PK Date, 156 строк)
Date(date), week_start(date), «День недели»(text), year(int16), month(int16),
year_month(text 'YYYY-MM'), day(int16). → **пометить как таблицу дат по Date**.

### star."Dim_Campaign" (PK CampaignId, 15 937 строк)
CampaignId(int64), CampaignName, account_login, статус_кампании, специалист,
manager_login, campaign_status, payment_model, «номер кампании | название кампании».

### star."Dim_AdGroup" (PK AdGroupId, 151 166 строк) — ОБНОВЛЕНА 2026-06-06
AdGroupId(int64), AdGroupName, adgroup_code, «номер группы | название группы»,
ag_part1..7, ag_part1_name, **неверный_кодер_new** (НОВОЕ — флаг кодировки),
parent_CampaignId(int64).

### star."Dim_Site" (PK domain, 4 262 строки)
domain(text), салон, город, регион, тип_сайта, id_салона, направление, шаблон,
site_quiz, проджект, менеджер, специалист, «Название crm», «марки авто», статус_сайта.

### star.fact_big_analytics (3.68M строк, 698 МБ)
КЛЮЧИ: CampaignId(int64), AdGroupId(int64), domain(text), Date(date).
МАРКЕРЫ: атрибуция(text), _source_table(text).
МЕРЫ numeric: total_cost, kol_vo_zayavok, korr, kval, priezd, prodazhi.
МЕРЫ int: Clicks, Impressions, nekorr, ne_otvechaet, nedozvon, filtr, priedet,
«План заявки», «План приезда».
МЕРЫ int64: RlAdjustmentId, priezd_arrival_date, prodazhi_arrival_date,
dohod_do_kredita, dobro.

### star.arp_fact (~8M строк, 1370 МБ)
КЛЮЧИ: CampaignId(int64), AdGroupId(int64), domain(text), Date(date).
АТРИБУТЫ: placement(text), ad_network_type(text), тип_заявки(text).
МЕРЫ: cost(numeric), clicks(int64), korr/kval/priezd/prodazhi(numeric или int —
в arp_fact эти — integer), kol_vo_zayavok/nekorr/ne_otvechaet/nedozvon/filtr/priedet/
dohod_do_kredita/dobro(integer).
