# BA6 golden/Telegram checks proposal

Цель: перед refresh Power BI проверять, что raw/source-данные совпадают с таблицами, которые читает BI, и что ошибки из текущей миграции не могут повториться молча.

BA5-паттерн:
- `data_check/verify_big_analytics.py` возвращает список блоков `PASS/FAIL` и валит pipeline при hard fail.
- `data_check/golden_reward.py` читает durable BI-факт и отправляет короткую Telegram-сводку по тем же блокам.
- В BA6 надо оставить тот же принцип: один источник истины для проверок, Telegram = компактное зеркало verdict/details, не отдельная логика.

## Предлагаемые hard checks

| # | Блок | Что ловит | Raw/source | BI-facing target | Grain / допуск |
|---|---|---|---|---|---|
| 1 | Direct spend conservation | Потеря расхода Direct в любом доменном срезе | `ad_analytics.raw_yandex` | `ad_analytics.big_analytics_direct`, `ad_analytics.fact_big_analytics` | `month, account_login, domain`, допуск 1 руб. |
| 2 | Direct BI spend conservation | Случай, когда staging сходится, но BI-факт потерял расход | `ad_analytics.raw_yandex` | `ad_analytics.pbi_import_big_analytics_full` / `fact_big_analytics` | `month, account_login, domain, атрибуция='По дате заявки'`, допуск 1 руб. |
| 3 | Feed spend/funnel conservation | Ошибка вкладки фидов: raw feed есть, BI-таблица пустая/неполная | `raw_data.direct_feed_report_rows` | `ad_analytics.fact_direct_feed_funnel`, `ad_analytics.pbi_import_fact_direct_feed_funnel` | `date, campaign_id, ad_group_id, placement_feed_key`, cost/clicks/impressions/forms/crm sums, допуск 1 руб./1 event |
| 4 | Feed placements isolated from posevy | Попадание площадок `tp8/tp9/tp10`/посевов в Direct-feed/РСЯ фидовую вкладку | `fact_direct_feed_funnel` + campaign/source fields | `pbi_import_fact_direct_feed_funnel` | `countIf(source LIKE 'Посевы%' OR tp IN ('tp8','tp9','tp10')) = 0` |
| 5 | Posevy sales require real specialist | Продажи `Без специалиста` / пустой специалист | `big_analytics_full`, `big_analytics_full_arrival` | `fact_big_analytics` | обе атрибуции, `prodazhi>0 AND specialist IN ('','Без специалиста') = 0` |
| 6 | Posevy request/visit axes are not collapsed | Ошибка BI, где Вильцин/Немытова показывали `заявка + визит` одной цифрой | `big_analytics_full` и `big_analytics_full_arrival` | `fact_big_analytics` | для `источник LIKE 'Посевы%'`: каждая строка имеет ровно одну `атрибуция`; Telegram показывает отдельно request/visit totals по топ-специалистам |
| 7 | Visit-axis sales floor vs request-axis | Глобально продаж по визитам меньше, чем по заявкам | `fact_big_analytics` | BI-факт | `SUM(prodazhi WHERE атрибуция='По дате визита') >= SUM(prodazhi WHERE атрибуция='По дате заявки')`, отдельно all / no-pixel / posevy |
| 8 | VK Ads domain mapping | Расход VK Ads в `.без домена` | `raw_data.vk_ads_stats_day` + `ad_analytics.gsheet_sites_effective.vk_client_id` | `fact_vk_ads`, `fact_big_analytics` | `month, account_id, domain`; `spent>0 AND empty(domain)=0`, допуск 1 руб. |
| 9 | VK Ads spend conservation | VK raw spend потерян при переходе в BI | `raw_data.vk_ads_stats_day` | `fact_vk_ads`, `pbi_import_big_analytics_full` | `month, account_id`, допуск 1 руб. |
| 10 | Effective gsheet coverage | CH-справочник снова потерял домены/специалистов из BA5/PG mirror | `ad_analytics.gsheet_sites_pg_overlay` | `ad_analytics.gsheet_sites_effective` | проблемные домены из overlay должны быть в effective; `directologist` не пуст для доменов с продажами |

## Proposed soft/warn checks

| # | Блок | Что сообщает в Telegram | Почему не hard fail |
|---|---|---|---|
| W1 | Обращения без воронки дальше | Домены/специалисты с `korr/forms > 0`, но `kval=priezd=prodazhi=0`, cost выше порога | Это может быть реальная операционная проблема CRM/качества, а не ETL-регрессия |
| W2 | Top spend without leads | Домены с большим расходом и нулевыми обращениями за текущий/предыдущий месяц | Уже бывает как бизнес-сигнал; hard fail только после согласования порога |
| W3 | Attribution ratio by source | По каждому `источник`: request sales, visit sales, delta | Не все источники обязаны быть строго монотонны без подтверждения бизнес-правила |

## Telegram format

Один Telegram после `verify`:

```text
BA6 golden BI contracts: PASS/FAIL
run_id=... | max_date=... | refresh=blocked/allowed

Hard checks:
PASS Direct spend raw→direct→BI: missing=0, raw=...
PASS Feed raw→BI: cost Δ=0, rows=...
FAIL Posevy real specialist: 3 slices, top: arrival Посевы_Звонки novgorod-cars.ru sales=1
PASS VK Ads domain: empty_domain_spend=0

Warnings:
WARN Обращения без воронки: 12 domains, top=...
```

Правило: если есть хотя бы один hard fail, pipeline `verify` возвращает `1`, Power BI refresh не запускается. Telegram всё равно отправляется с top-5 проблемами.

## Implementation sketch

1. Добавить `data_check/golden_bi_contracts.py`:
   - `Block(num, title, ok, detail, severity='hard')`;
   - функции `check_direct_spend`, `check_feed_funnel`, `check_posevy_specialist`, `check_vk_ads`, `check_effective_gsheet`, `check_warnings`;
   - все запросы read-only, без API.
2. В `data_check/verify_big_analytics.py`:
   - вызвать `golden_bi_contracts.run(client)` после существующих structural checks;
   - hard-fail blocks добавлять в `failures`;
   - логировать все blocks.
3. Telegram:
   - добавить `build_golden_bi_message(blocks, run_id)` через `notifications.telegram.TelegramMessage`;
   - pipeline step `verify` вызывать с `tg=True` или отправлять сообщение внутри `verify` при `tg=True`;
   - при ошибке отправки Telegram не валить pipeline, только `TG_SEND_FAIL` в лог.
4. Tests:
   - contract tests на наличие raw/source и BI-facing target в SQL;
   - unit tests на hard/warn агрегацию и Telegram truncation top-5;
   - smoke SQL compile через `EXPLAIN`/`LIMIT 0` на Victory перед refresh.

## Что уже частично закрыто текущими правками

- `direct_spend_loss_slices` уже добавлен в BA6 `verify_big_analytics`: raw_yandex vs `big_analytics_direct` по `month/account_login/domain`.
- `sales_without_real_specialist_slices` уже добавлен как hard guard.
- `gsheet_sites_effective` уже создан как BA6-owned справочник: CH `reference_data.gsheet_sites` + PG overlay из `public.gsheet_sites`.
- VK Ads код уже переведён на `vk_client_id` из effective-справочника; нужен golden guard на пустой домен и conservation.

## Открытые решения перед внедрением

- Порог для warning “обращения без воронки”: предлагаю `cost >= 100000` за месяц или `korr/forms >= 3`.
- Блок `visit sales >= request sales`: делать hard только для общего total или также по источникам. Без подтверждения лучше hard для total, warn по источникам.
- Feed conservation: сравнивать весь период с `2026-01-01` или только `[today-35d; today]` как быстрый daily guard. Предлагаю hard на оба: полный total и детальный recent-window.
