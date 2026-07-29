# direct_feed_funnel

Воронка Яндекс.Директа по фидам.

Папка: `/home/semen_vi/big_analytics_v5/direct_feed_funnel/`

## Что делает

Пайплайн собирает физические таблицы для анализа фидов:

1. Проверяет источник `public.yandex_direct_feeds_report`.
2. Строит ключованные таблицы расходов и лидов.
3. Джойнит расходы с лидами по согласованному ключу.
4. Создаёт итоговую таблицу `public.fact_direct_feed_funnel` и таблицу качества матчинга.

Важно: загрузчик из Yandex API для `public.yandex_direct_feeds_report` пока не находится внутри этой папки.
Текущий `pipeline.py` валидирует уже загруженную таблицу и не даёт строить витрину, если источник пустой или устарел.

## Запуск

Полный рекомендуемый порядок перед обновлением Power BI:

```bash
cd /home/semen_vi/big_analytics_v5
/home/semen_vi/venv/bin/python3 -m direct_feed_funnel.fetch_feed_urls_cookie --all-logins --apply
/home/semen_vi/venv/bin/python3 -m direct_feed_funnel.pipeline
```

`fetch_feed_urls_cookie` нужен для новых/изменённых фидов: он обновляет реальные URL в
`public.yandex_direct_feeds_report`. Сам `direct_feed_funnel.pipeline` URL не получает,
а только использует уже заполненные `feed_url` и `feed_url_key`.

### Как сейчас заполняется `feed_url_key`

Приоритет источников в `build_keyed.py` такой:

1. `yandex_direct_feeds_report.feed_url_key` — точное значение из cookie/Grid API.
2. Ручные safe-mapping'и по `feed_name`:
   - `Y` → `yandex.xml`
   - `Каталог-модель` → `yandex-catalog-model.xml`
   - `Кастом-нейм` → `yandex-catalog-model-design-custom-name.xml`
3. Если `feed_name` уже оканчивается на `.xml`, берётся нормализованное имя файла.
4. Fallback по `public.direct_global_feed_rules`, если canonical feed key виден внутри `feed_name`.

`name.xml` и прочие двусмысленные/короткие имена специально НЕ маппятся вручную.
Если ни один шаг не сработал, `feed_url_key` остаётся пустым.

Важно: основной `pipeline_powerbi.py` большого проекта на текущий момент не запускает
`fetch_feed_urls_cookie` автоматически. Если появились новые фиды, сначала запустить
cookie-обогащение, затем пересобрать фидовую витрину и скопировать таблицу на localhost
для Power BI.

```bash
cd /home/semen_vi/big_analytics_v5
/home/semen_vi/venv/bin/python3 -m direct_feed_funnel.pipeline
```

Проверить источник без пересборки витрин:

```bash
cd /home/semen_vi/big_analytics_v5
/home/semen_vi/venv/bin/python3 -m direct_feed_funnel.pipeline --no-build
```

Допустимый лаг источника по умолчанию: 3 дня. Изменить:

```bash
/home/semen_vi/venv/bin/python3 -m direct_feed_funnel.pipeline --max-source-lag-days 7
```

Запустить только сборку без проверки источника:

```bash
/home/semen_vi/venv/bin/python3 -m direct_feed_funnel.pipeline --skip-source-check
```

Обогатить источник реальными URL фидов через cookie/web-api Директа:

```bash
cd /home/semen_vi/big_analytics_v5
/home/semen_vi/venv/bin/python3 -m direct_feed_funnel.fetch_feed_urls_cookie --login stavspeed26-515715-lk1f --apply
```

Для всех логинов из `public.yandex_direct_feeds_report`:

```bash
/home/semen_vi/venv/bin/python3 -m direct_feed_funnel.fetch_feed_urls_cookie --all-logins --apply
```

## Источники

### Расходы

`public.yandex_direct_feeds_report`

Используемые поля:

- `date`
- `login_key`
- `domain`
- `campaign_id`, `campaign_name`
- `adgroup_id`, `adgroup_name`
- `feed_id`, `feed_name`
- `feed_url`, `feed_url_key` — nullable-поля, заполняются через cookie/web-api Директа, потому что официальный `feeds.get` не отдаёт source URL. `feed_url_key` хранит последнюю часть URL с расширением, например `credit-page-01-a.xml`
- `impressions`, `clicks`, `cost`
- цели CRM из Директа: `goal_all_forms`, `goal_crm_order_created`, `goal_crm_order_paid`, `goal_crm_spam_order`, `goal_crm_order_canceled`

В проекте расходы считаются по `total_cost`; в этой витрине `total_cost = sum(cost)` из `yandex_direct_feeds_report`.

⚠️ Пустой `feed_url_key` не означает «фида нет». Чаще это означает, что расход по фиду есть,
но URL/имя файла ещё не удалось восстановить автоматически. Проверять сначала
`feed_id`/`feed_name`/`feed_key`, а не только `feed_url_key`.

### Лиды

`public.leads_all`

Используемые поля:

- `id`
- `created_date`
- `campaign_id`
- `group_id`
- `domain_id`
- `utm_content`
- `source_type`, `salon`, `status`, `reason`

Фид берётся из `utm_content` по шаблону:

```text
fid:<feed_key>
```

Пример:

```text
g:5717742524|fid:dostup-k-rasprodazhe-01-a
```

## Ключ соединения

Обычные кампании:

```text
Date|CampaignId|AdGroupId|feed_key
```

Кампании `tp6` / `tp7`:

```text
Date|CampaignId|feed_key
```

Причина: для `tp6` / `tp7` в проекте групповой уровень не считается надёжной частью ключа.

Нормализация `feed_key`:

- нижний регистр;
- если `feed_url_key` заполнен, используется он;
- обрезается путь до файла, если в имени есть URL/path;
- убирается `.xml`;
- убирается префикс `фид `;
- для расходной стороны также убирается префикс `new `.

Физический ключ в таблицах называется `feed_key3`.

Пример:

```text
2026-03-09|707286927|5717742524|dostup-k-rasprodazhe-01-a
```

## Итоговые таблицы

### `public.direct_feed_spend_keyed`

Расходы по ключу фида.

Основные поля:

- `feed_key3`
- `date`
- `login_key`
- `domain`
- `campaign_id`, `campaign_name`
- `adgroup_id`, `adgroup_name`
- `feed_id`, `feed_name`, `feed_url`, `feed_url_key`, `feed_key`
- `is_tp67`
- `impressions`, `clicks`, `total_cost`
- цели CRM из Директа
- `generated_at`

Индексы:

- `idx_direct_feed_spend_keyed_key`
- `idx_direct_feed_spend_keyed_date`
- `idx_direct_feed_spend_keyed_feed`

### `public.yandex_direct_feed_urls`

Map-таблица реальных URL фидов, полученных через internal Direct Grid API по cookie.

Ключ:

- `login_key`
- `feed_id`

Основные поля:

- `feed_name`
- `feed_url` — полный URL, например `https://stavspeed26.ru/dostup-k-rasprodazhe-01-b.xml`
- `feed_url_key` — последняя часть URL с `.xml`, например `dostup-k-rasprodazhe-01-b.xml`
- `source`, `feed_type`, `update_status`
- `offers_count`, `listings_count`
- `cookie_account`, `fetched_at`

### `public.direct_feed_leads_keyed`

Лиды с `fid` из `utm_content`, приведённые к тому же ключу.

Основные поля:

- `feed_key3`
- `lead_id`
- `date`
- `domain`
- `campaign_id`
- `adgroup_id`
- `feed_key`
- `source_type`, `salon`, `status`, `reason`, `utm_content`
- показатели воронки из `config.status_sql`: `kol_vo_zayavok`, `korr`, `kval`, `priezd`, `prodazhi`, и др.
- `generated_at`

Индексы:

- `idx_direct_feed_leads_keyed_key`
- `idx_direct_feed_leads_keyed_lead`
- `idx_direct_feed_leads_keyed_date`

### `public.fact_direct_feed_funnel`

Финальная витрина: расходы + воронка по фиду.

Поля расходов:

- `impressions`
- `clicks`
- `total_cost`
- цели CRM из Директа

Поля воронки:

- `attributed_leads`
- `kol_vo_zayavok`
- `korr`
- `kval`
- `priezd`
- `prodazhi`
- `nekorr`
- `ne_otvechaet`
- `filtr`
- `nedozvon`
- `priedet`
- `dohod_do_kredita`
- `dobro`

Индексы:

- `idx_fact_direct_feed_funnel_key`
- `idx_fact_direct_feed_funnel_date`
- `idx_fact_direct_feed_funnel_feed`

### `public.analytics_report_feed`

Денормализованная отчётная таблица для страницы «Фиды» в Power BI.

Строится в `build_report_feed.py` (DROP + CREATE AS SELECT): источник — `fact_direct_feed_funnel`
с LEFT JOIN по `local_gsheet_sites` (специалист, регион, тип сайта) + `Dim_Campaign` + `Dim_AdGroup`.
Grain: date | domain | feed_key (совпадает с `fact_direct_feed_funnel`).
VIEW `arf_fact` — passthrough SELECT * FROM analytics_report_feed (пересоздаётся каждый раз вместе с таблицей).

**Исключения доменов (VICTORY_CRM_DOMAIN_EXCLUDE_2026-07-26):**
`victory-crm.ru` исключён из витрины как внутренний/тестовый домен (0 расхода, отсутствует
в `local_gsheet_sites`). Фильтр: `WHERE f.domain <> 'victory-crm.ru'` в `_CREATE_SQL`.

Для пересборки только этой таблицы (без rebuild `build_keyed`):
```bash
cd ~/big_analytics_v5
~/venv/bin/python3 -c "
import logging; logging.basicConfig(level=logging.INFO)
from config.db import init_pool, close_pool
from direct_feed_funnel import build_report_feed
init_pool()
stats = build_report_feed.build()
print(stats)
close_pool()
"
```

### `public.fact_direct_feed_funnel_quality`

Контроль качества матчинга лидов с `fid`.

Поля:

- `date`
- `feed_key`
- `total_fid_leads`
- `matched_leads`
- `unmatched_fid_leads`
- `generated_at`

Эта таблица нужна, чтобы видеть лиды, где `fid` есть, но к строке расхода по строгому ключу они не привязались.

## Текущий результат проверки

Последний проверенный источник:

```text
public.yandex_direct_feeds_report
rows=562967
date=2026-01-01..2026-06-24
clicks=4088183
cost=101939737.73
```

Последняя сборка ключованной витрины:

```text
spend_rows=562967
lead_rows=21742
fact_rows=562967
attributed_leads=2385
unmatched_fid_leads=19357
```

Большое число `unmatched_fid_leads` ожидаемо до отдельной доработки исторических UTM: у части лидов `fid` есть, но старые метки/подстановки не дают надёжно сопоставить их с расходом по строгому ключу.

## Файлы

- `pipeline.py` - оркестратор: проверка источника + сборка витрины.
- `build_keyed.py` - основная физическая сборка таблиц.
- `build.py` - ранняя экспериментальная версия через view; не использовать как основной запуск.

## Состояние key-2 (composite fallback) — актуально на 2026-07-27

Key-2 используется в `fallback_order_match` CTE (`build_keyed.py`) как fallback для лидов,
где `external_id_crm` не сматчился (key-1 промах).

**Состав ключа key-2 (ПОСЛЕ фиксов 2026-07-24 / 2026-07-26 / 2026-07-27):**
```
order_date | order_domain | yclid | phone_last10 | campaign_id_resolved
```
где `campaign_id_resolved = campaign_id` (или номер кампании из начала `utm_campaign`, если `campaign_id` пустой).

**Активные guard'ы (все три обязательны — иначе ложные атрибуции):**

1. **Campaign guard + Adgroup guard (FEED_CAMPAIGN_ADGROUP_GUARD_2026-07-26):**
   ```sql
   AND EXISTS (
       SELECT 1 FROM public.yandex_direct_feeds_report fr
       WHERE fr.campaign_id = lb.campaign_id
         AND (lb.group_id IS NULL OR fr.adgroup_id = lb.group_id)
   )
   ```
   Кампания лида должна присутствовать в feeds_report; если известна группа объявлений (`group_id IS NOT NULL`) —
   она тоже должна быть там. Без этого guard'а 47.4% key-2 матчей были ложными (кампании вне фидов).

2. **UTM_CONTENT adgroup fallback (UTM_CONTENT_ADGROUP_FALLBACK_2026-07-27):**
   ```sql
   AND (lb.group_id IS NOT NULL OR s.utm_content = lb.utm_content)
   ```
   Когда `group_id IS NULL` (adgroup в `leads_all` не заполнен), campaign guard схлопывается до
   уровня кампании, что недостаточно для ЕПК-кампаний с разными типами adgroup. Дополнительная
   верификация через `utm_content`: оба поля (`shadow_orders.public.orders.utm_content` и
   `leads_all.utm_content`) кодируют `g:adgroup_id|geoname:...` идентично — совпадение
   utm_content доказывает правильный adgroup без явного group_id.
   `utm_content` добавлен в `_shadow_orders_fid` temp-таблицу (колонка + SELECT + INSERT + payload).

3. **Source type guard (SOURCE_TYPE_SITE_GUARD_2026-07-25):**
   `AND source_type = 'site'` в `_load_shadow_orders_temp`.
   Практического эффекта нет (100% заказов уже site), но делает инвариант явным.

**Что НЕ работает для key-2 (выяснено на попытках):**
- Domain guard (`order_domain = domain`): убивает 98% матчей из-за subdomain vs root domain
  (`auto.dealer.ru` в entry_point vs `dealer.ru` в `local_domains`).
- Phone guard: убивает все матчи — нормализации телефонов в разных CRM систематически расходятся.

## Что нельзя менять без проверки

- Нельзя ослаблять ключ соединения до одного `feed_key`: это создаст ложные совпадения между кампаниями/датами.
- Нельзя использовать `cost` как финальное имя расхода в витрине Power BI: в проекте расход называется `total_cost`.
- Нельзя строить витрину из пустого или старого `yandex_direct_feeds_report`; сначала должен пройти source-check.
