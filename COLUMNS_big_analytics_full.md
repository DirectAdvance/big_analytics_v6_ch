# COLUMNS_big_analytics_full.md — поколоночный словарь главной витрины

<!-- v6-scope-banner -->
> 🧭 **Область в v6_ch (2026-08-15).** Словарь снят с v5 PostgreSQL. В v6 колонок тоже 73, но
> раскладка другая: `ad_analytics.big_analytics_full` — View поверх `fact_big_analytics` + `Dim_*`,
> а PBI-проекция `ad_analytics.pbi_big_analytics_full` отдаёт **43** колонки (текстовые атрибуты
> заменены на `*_key` и вынесены в справочники). Диффы колонок v5↔v6 — `PBI_TABLES.md` §0.3.

> Словарь всех колонок `public.big_analytics_full` (73 колонки). Типы получены
> интроспекцией live-БД `ad_analytics_bi` @ Victory (`information_schema.columns`,
> 2026-06-07). Порядок колонок задан в `step6_build_full/step6.py` (`COLS`) — он же
> определяет порядок ветвей UNION ALL. Источник колонки (какой step её пишет)
> реконструирован из [`step6_build_full/CLAUDE.md`](step6_build_full/CLAUDE.md),
> [`BLOCKS.md`](BLOCKS.md), [`CANON.md`](CANON.md), [`PROJECT_CHARTER.md`](PROJECT_CHARTER.md) §8.

---

## ⚠️ Критичные правила по типам

- **Дробная атрибуция = `numeric`, НЕ int.** Меры воронки (`kol_vo_zayavok, korr, kval,
  priezd, prodazhi` и др.) — `numeric`, потому что пиксель-атрибуция (step11) дробит
  кредит лида между салоном/доменом/кампанией. **Усечение долей до int по строкам — главный
  исторический баг проекта.** Округление — только у итоговой `SUM(...)`. См.
  [`ATTRIBUTION.md`](ATTRIBUTION.md), [`GOLDEN_BASELINE.md`](GOLDEN_BASELINE.md) п.4.
- **`total_cost` — РУБЛИ** (`numeric`, 2 знака; golden Кудерко 25 422 774.00 ₽). В самой витрине
  конвертации в копейки нет. ⚠️ Производные CPL-метрики в [`KNOWN_ISSUES.md`](KNOWN_ISSUES.md) #1
  считаются в копейках — это про расчёт CPL, а не про колонку `total_cost`.
- **`источник IS NOT NULL`** для всех строк; **`"Date" >= '2026-01-01'`**. См. [`CANON.md`](CANON.md).

Легенда «step»: какой шаг/скрипт задаёт значение в `big_analytics_full`. Многие колонки
рождаются в step3 (источниковые витрины) и проходят через UNION ALL step6; постпроцессинг
(salon/manager/проджект/направление) — UPDATE'ы в step6 (Block G2).

---

## Ключи и дата

| Колонка | Тип | step | Смысл |
|---------|-----|------|-------|
| `key3` | text | step3/step6 | Композитный ключ строки (`Date\|CampaignId\|AdGroupId\|Device\|RlAdjustmentId`). Для звонков NULL. |
| `Date` | date | step3 | Дата заявки/расхода. Граница `>= 2026-01-01` (инвариант). Для `_arrival` — дата визита. |
| `День недели` | text | step3 | Текстовый день недели от `Date`. |
| `week_start` | date | step3 | Начало недели (понедельник) от `Date`. |
| `key_pixel_score` | text | step6 | Связь с `pixel_score` (`Date\|domain\|источник\|CampaignId`). |
| `_source_table` | text | step3/6/10/11 | Технический маркер: `direct`/`seo`/`crop_targeting`/`tp8`/`tp9`/`tp10`/`calls`/`social_посевы`/`telegram`/`reviews`/`pixel`. Партиции PBI. Старый `пиксель_атрибуц` выведен из BA6-контракта 2026-08-17. |

## Кампания / группа (Директ)

| Колонка | Тип | step | Смысл |
|---------|-----|------|-------|
| `CampaignId` | bigint | step3 | ID кампании Я.Директа. NULL у звонков/SEO/части посевов. |
| `CampaignName` | text | step3 | Имя кампании. Кириллические lookalike нормализуются (см. CANON). |
| `AdGroupId` | bigint | step3 | ID группы. |
| `AdGroupName` | text | step3 | Имя группы (`'звонки'` у call-строк). |
| `AdNetworkType` | text | step3 | Тип сети (Поиск/РСЯ). |
| `Device` | text | step3 | Устройство. |
| `RlAdjustmentId` | bigint | step3 | ID корректировки ставок (retargeting list). |
| `RlAdjustmentId_total` | text | step3 | Сводная пометка по корректировке. |
| `campaign_code` | text | step1/3 | Код кампании из REGEXP по `CampaignName` (`неверный кодер` = кириллица). |
| `tp` | text | step3 | Тип кампании (tp1–tp11). tp8=Telegram, tp9=Max, tp10=Telegram+Max, tp11=Connected TV. |
| `cpc_cpa` | text | step3 | Модель кампании (cpc/cpa) из имени. |
| `site_quiz` | text | step3 | Признак квиз-сайта из имени кампании. |
| `adgroup_code` | text | step3 | Код группы. |
| `account_login` | text | step3 | Логин аккаунта Директа. |
| `manager_login` | text | step3/6 | Логин менеджера (постпроцесс по салону/домену в step6; `'отзывы'` у reviews). |
| `ag_part1`…`ag_part7` | text | step3 | 7 частей разбора `AdGroupName` (нейминг). `'звонки'` у call-строк. |
| `ag_part1_name` | text | step3/нейминг | Расшифровка `ag_part1` из `local_gsheet_naming`. |
| `campaign_status` | text | step4→step6 | Статус кампании (`Активна`/`Остановлена`/`Архив`) из `campaign_status`. |
| `payment_model` | text | step4→step6 | Модель оплаты (за клики / за конверсии). Часть NULL — известная неполнота. |
| `номер кампании \| название кампании` | text | step6/PBIP | Объединённое поле ID+имя; статусный emoji-префикс для отчёта (🟢/🟡/🔴/⚪) добавляет Power BI semantic model `Dim_Campaign.tmdl`, не `big_analytics_full`. |
| `номер группы \| название группы` | text | step3/6 | Объединённое поле группы. |
| `аккаунт\|сайт` | text | step3/6 | Объединённое поле аккаунт+сайт. |

## Трафик и деньги

| Колонка | Тип | step | Смысл / единица |
|---------|-----|------|-----------------|
| `Impressions` | numeric | step3 | Показы (Директ). |
| `Clicks` | numeric | step3 | Клики (Директ). |
| `total_cost` | numeric | step3/10 | **Расход в рублях** (2 знака). У звонков NULL. Посевы: `price×1.22×1.30`. |

## Воронка (меры — ⚠️ дробные numeric)

| Колонка | Тип | step | Смысл |
|---------|-----|------|-------|
| `kol_vo_zayavok` | numeric | step3 (status_sql) | Количество заявок (обращения). |
| `korr` | numeric | step3 | Корректные заявки. |
| `kval` | numeric | step3 | Квалифицированные. |
| `priezd` | numeric | step3 | Визиты (приезды). |
| `prodazhi` | numeric | step3 | Продажи. |
| `nekorr` | numeric | step3 | Некорректные. |
| `ne_otvechaet` | numeric | step3 (хардкод) | Не отвечает. |
| `filtr` | numeric | step3 (хардкод) | Отфильтровано. |
| `nedozvon` | numeric | step3 (хардкод) | Недозвон. |
| `priedet` | numeric | step3 (хардкод) | Приедет (план визита). |
| `dohod_do_kredita` | bigint | step3 (reason) | Доход до кредита (reason-сторона). |
| `dobro` | bigint | step3 (reason) | Одобрено (кредит). |
| `План заявки` | integer | step6 (gsheet) | План по заявкам (из `local_gsheet_plan_fakt`). |
| `План приезда` | integer | step6 (gsheet) | План по приездам. |
| `priezd_arrival_date` | bigint | step13/6 | Приезды по дате визита (заполняется логикой arrival). |
| `prodazhi_arrival_date` | bigint | step13/6 | Продажи по дате визита. |

> ⚠️ `korr/kval/priezd/prodazhi/kol_vo_zayavok` — **numeric** именно ради дробной
> пиксель-атрибуции. В star-факте они тоже numeric (см. [`STAR_REFACTOR_BRIEF.md`](STAR_REFACTOR_BRIEF.md)):
> приведение к int дробит приезды пикселя 6097 → 1614 — пойманная и исправленная ошибка.

## Справочные измерения (салон / сайт)

| Колонка | Тип | step | Смысл |
|---------|-----|------|-------|
| `марки авто` | text | step3 (brand_map) | Марки авто по ct-кодам. |
| `Название crm` | text | step6 | CRM-система домена (Фаиг/Плекс/Мега/Маркар…). Постпроцесс по салону. |
| `тип_заявки` | text | step3/6 | Тип строки (`звонки`/`отзывы`/…). |
| `статус` | text | step6 (gsheet) | Статус сайта (`Контекст активно` и т.п.). |
| `специалист` | text | step6 (gsheet) | Директолог (колонка называется `специалист`, НЕ `директолог`). |
| `тип_сайта` | text | step6 (gsheet) | Тип сайта. |
| `шаблон` | text | step6 (gsheet) | Шаблон сайта. |
| `салон` | text | step6 (gsheet) | Автосалон. |
| `город` | text | step6 (gsheet) | Город салона. |
| `регион` | text | step6 (gsheet) | Регион. |
| `domain` | text | step3 | Домен сайта (ключ к `local_gsheet_sites`). |
| `direction` | text | step3/6 | Ниша (`Авто` и др.) из `local_gsheet_sites`. |
| `неверный_кодер_new` | text | step3 | Метка некорректной кодировки имени. |
| `fid` | text | step3/corrections | fid-атрибуция (`corrections._patch_fid_attribution`). |
| `проджект` | text | step6 | Проджект (постпроцесс по салону). |
| `id_салона` | text | step6 (gsheet) | ID салона. |
| `менеджер` | text | step6 (gsheet) | Менеджер салона. |
| `источник` | text | step3/6/10/11 | **NEVER NULL** (CANON): `Контекст`/`Пиксель`/`звонки`/`telegram`/`Max`/`VK`/`SEO`/`контекст`. Старые `пиксель`/`пиксель_атрибуц` относятся к v5/истории. |
| `направление` | text | step3/6/11/13 | Бизнес-категория: `Контекст`/`SEO`/`SEO Flow`/`посевы`/`Пиксель`/`отзывы` (CANON). `Пиксель_атрибуц` больше не допустим в BA6 live-данных. |
| `поставщик` | text | step3/10 | Поставщик (для посевов/каналов). |

---

## Канон-значения

Допустимые значения `источник`, `направление`, `_source_table`, нормализация кириллицы
и дата-граница — в [`CANON.md`](CANON.md). Маппинг статусов воронки в меры — в [`FUNNEL.md`](FUNNEL.md).

> Колонки live-БД на момент интроспекции: 73. Если пайплайн добавит/уберёт колонку —
> пересними `SELECT column_name, data_type FROM information_schema.columns WHERE
> table_schema='public' AND table_name='big_analytics_full' ORDER BY ordinal_position`
> (см. [`QUERIES.md`](QUERIES.md)) и обнови этот файл.
