# Финальный план: перевод прода на STAR (big_analytics_v5)

> ⚠️ **2026-06-11: схема `star` УПРАЗДНЕНА — звезда консолидирована в `public` 2026-06-10.**
> Везде ниже `star.fact_big_analytics` / `star.arp_fact` физически = `public.*`. Цифры в плашке
> ниже (25 422 774.027 / priezd 2384 / prodazhi 2838) — PoC-замер момента сборки звезды
> 2026-06-07 (ДРУГОЙ срез), НЕ текущий golden Кудерко. Текущий жёсткий инвариант:
> расход **25 422 774.00** / продажи **47** — см. [`../../GOLDEN_BASELINE.md`](../../GOLDEN_BASELINE.md).
>
> Составлен 2026-06-07. Модель STAR собрана и сверена с эталоном
> (Кудерко 25 422 774.027 / 47 ✅; star == unified == big_analytics_full байт-в-байт).
> Это ПЛАН. Выполнять блоками, каждый — после приёмки предыдущего.

---

## Архитектура прода (выяснено 2026-06-07)

```
Большая аналитика_admin/   ← АВТОРИНГ: .Report (byPath) + .SemanticModel (полная модель)
        │  публикуется в воркспейс «Victory Analytics»
        ▼
  Датасет «Большая аналитика_v00»  (semanticmodelid = aa304ada-7a9a-4a7c-9720-a44a75fd7075)
        ▲
        │  byConnection (тонкий отчёт, своей модели НЕТ)
Большая аналитика_user/    ← ОТЧЁТ ДЛЯ ЮЗЕРОВ: только .Report, 41 страница
```

- **admin** = источник модели (его SemanticModel → публикуемый датасет). 48 страниц.
- **user** = тонкий отчёт, читает тот же датасет. 41 страница (36 со слайсером направление+атрибуция).
- **RLS-ролей в модели НЕТ** — admin/user различаются только отчётами.
- **STAR** = форк admin + звёздные правки. Паритет: 48 = 48 страниц, STAR — чистый superset admin
  (добавлены Dim_Date/Campaign/AdGroup/Site + arp_fact; у admin лишь старые `.bak`-файлы-мусор).

Эталон для сверки после катовера: Кудерко 25 422 774.027 / 47; глобальные 34157 / 2384 / 854.7М.
Mutagen: Mac↔LXC101, **НЕ Victory**. Код на Victory — только вручную (scp). Прогон пайплайна — на Victory.

---

## БЛОК A — Привести admin в STAR-форму (promote STAR → admin)

Поскольку STAR = admin + звезда (паритет подтверждён), продвижение = **замена папок**.

1. **Бэкап admin** (вне PBI, файлы могут быть root-owned после PBI):
   ```
   tar -czf "_backup_admin_$(date +%Y%m%d_%H%M).tar.gz" "Большая аналитика_admin"
   ```
2. **Заменить контент admin содержимым STAR** (имена папок совпадают):
   - `Большая аналитика_admin/Большая аналитика_v00.SemanticModel`  ← из STAR
   - `Большая аналитика_admin/Большая аналитика_v00.Report`         ← из STAR
   - Сохранить admin-специфику, если есть (проверить: её нет — RLS нет, страницы совпадают).
   - Удалить старые `.bak_*` tmdl-файлы (мусор, мешают парсеру).
3. **Открыть admin.pbip в PBI Desktop** → модель грузится без ошибок (звезда) → Refresh.
   - Сверить: Кудерко 47, слайсер направления полный, страницы «Анализ страниц»/«Площадки РСЯ».

> Альтернатива (если не хочешь wholesale): переприменить точечно те же правки на admin —
> partition→star.fact, +4 Dim, +arp_fact, 8 связей, меры, report visualInteractions ×37.
> Дольше и рискованнее ручной синхронизации. Рекомендуется ЗАМЕНА (п.2).

---

## БЛОК B — Починить user.Report (тонкий отчёт)

user читает тот же датасет → после публикации звезды (Блок D) автоматически получит звёздную модель.
Но его СТРАНИЦЫ нуждаются в тех же report-level правках:

1. **Бэкап user.Report** (tar).
2. **visualInteractions NoFilter** (направление ⊥ атрибуция) на **36 страницах** —
   тем же скриптом, что прогнан на STAR (источник=атрибуция-слайсер, цель=направление-слайсер).
3. **Снять висячие page-фильтры** на big_analytics_full[направление]/[специалист], если есть
   (как делали на STAR — niche-фикс).
4. **Проверить ссылки** визуалов user на поля, которые звезда могла убрать/переименовать.
   Звезда сохраняет big_analytics_full[направление],[атрибуция] и все меры → ломаться не должно.
   ARP-поля затронет Фаза 2 (см. Блок E).
5. **Republish user.Report** в воркспейс (Блок D).

> ⚠️ Публиковать звезду в ТОТ ЖЕ датасет (overwrite, semanticmodelid aa304ada сохраняется) —
> тогда `byConnection` в user.definition.pbir менять НЕ нужно. Если создать новый датасет —
> id поменяется, и connectionString в user.Report надо обновить.

---

## БЛОК C — Изменения в пайплайнах

### C1. Врезать build_star в pipeline.py (и fast_pipeline.py)
**Где:** сразу после `build_unified` (он после step11, `pipeline.py:681`).
```python
import subprocess, sys, os
log_step(conn, run_id, 'build_star', 'start')
subprocess.run([sys.executable, 'star_refactor/build_star.py'],
               check=True, cwd=os.path.dirname(__file__))
log_step(conn, run_id, 'build_star', 'ok')
```
(build_star.py идемпотентен, открывает своё соединение — subprocess проще всего.)
→ `star.*` пересобираются каждый прогон автоматически.

### C2. Обновить `_ALL_TABLES` в refresh_powerbi.py (строки 193–203)
```python
_ALL_TABLES = [
    'big_analytics_full',   # в модели = star.fact_big_analytics
    'Dim_Date', 'Dim_Campaign', 'Dim_AdGroup', 'Dim_Site',
    'arp_fact',
    'direct_history', 'check_utm_fuck_direct',
    'yandex_direct_korrektirovki', 'yandex_direct_404_errors',
    'yandex_direct_return_commission_report',
    'pixel_score', 'yandex_direct_cookie_analytics_website_pages',
    # УБРАНО: big_analytics_full_arrival (визита теперь внутри star.fact / колонка `атрибуция`)
    # НЕ включаем: analytics_report_placement (excludeFromModelRefresh, 13 ГБ не тянем)
]
```

### C3. Деплой на Victory (ОБЯЗАТЕЛЬНО — Mutagen туда не доезжает)
```
scp pipeline.py fast_pipeline.py refresh_powerbi.py victory:~/big_analytics_v5/
ssh victory 'grep -c build_star ~/big_analytics_v5/pipeline.py'   # маркер-проверка
```
(где запускается refresh_powerbi.py — уточнить: cron LXC101 или Victory; задеплоить туда же.)

### C4. Уже сделано (не трогать)
VACUUM-guard после corrections (диск-защита); build_star.py: row-grain колонки, scope
niche Авто ∪ отзывы/звонки, LZ4, Dim_Site=только domain.

---

## БЛОК D — Прогон + публикация + верификация

1. **Прогон pipeline на Victory** (nohup): `ssh victory "cd ~/big_analytics_v5 && nohup ~/venv/bin/python3 pipeline.py &"`
   → после C1 пересоберёт и star.*.
2. **Опубликовать admin.pbip** из PBI Desktop в «Victory Analytics» → **overwrite** датасета
   «Большая аналитика_v00» (id aa304ada сохраняется). Задать datasource credentials (gateway → star.*).
3. **Опубликовать user.Report** (после Блока B).
4. **refresh_powerbi.py** (с новым _ALL_TABLES) → дождаться Completed.
5. **Верификация эталона**: Кудерко 25 422 774.027 / 47; глобальные 34157/2384/854.7М;
   страницы «Анализ страниц», «Площадки РСЯ», слайсер направления (полный список) — и в admin, и в user.
6. **Мониторинг первого scheduled refresh** (gateway видит star.*, токен валиден).

---

## БЛОК E — Фаза 2: экономия −18 ГБ (после приёмки прода на STAR)

### E1. ARP-катовер (−13 ГБ)
1. Добавить 6 conversion-колонок в `star.arp_fact` (патч build_star.py):
   `Все формы`, `CRM: Заказ создан/оплачен/отменён/Спам`, `placement_key` (+~50–100 МБ).
2. Перенацелить визуалы страницы «Площадки РСЯ» на `arp_fact`: 2 сводные → arp_fact,
   6 слайсеров → Dim_*. **И в admin, И в user** (user: 9 файлов ссылаются на ARP).
3. Убрать `analytics_report_placement` из модели + `DROP TABLE public.analytics_report_placement`.

### E2. unified-дубль (−5.4 ГБ)
- Прекратить материализацию `big_analytics_unified` в step13/build_unified.
- Строить star напрямую из `big_analytics_full ∪ big_analytics_full_arrival` внутри build_star.
- `DROP TABLE public.big_analytics_unified`.

**НЕЛЬЗЯ дропать** `big_analytics_full` — читают CPL-сервис/дашборд/алёрты.
Детали ДИФ 3b/3c — в `03_pipeline_diff.md`.

---

## БЛОК F — Хвост: бэкфилл (~1727 визитов, вторично)
step0 скипнул синк; глубокая причина — заявки <2026 отсутствуют в источниках
(local_leads_all, raw_calls), а не только не досинкались. Решать отдельно, влияние малое.

---

## 🔒 Drop-safety верификация (2026-06-07, read-only проверки)

> Главное правило пользователя: **НЕЛЬЗЯ дропать таблицу, на которую ещё ссылается Power BI.**
> Ниже — доказательства для двух кандидатов. Источники: grep кода `work/big_analytics_v5/`
> + `work/leads_api_perform/`; grep PBIR `.Report/.../visuals/*/visual.json` + TMDL
> `.SemanticModel/.../tables/*.tmdl` ОБОИХ финальных отчётов (admin=STAR и user);
> SQL `pg_depend` на Victory (views/matviews/rules). Размеры — из живой БД.

### Размеры (живая БД, regclass)
| Таблица | Размер | star-замена |
|---------|--------|-------------|
| `public.analytics_report_placement` | **13 GB** | `star.arp_fact` (1447 MB) |
| `public.big_analytics_unified` | **5400 MB** | `star.fact_big_analytics` (2319 MB) |
| `public.big_analytics_full` | 5106 MB | НЕ дропать (CPL-сервис/алёрты) |
| `public.big_analytics_full_arrival` | 21 MB | источник unified/star |

### (a) `analytics_report_placement` (13 ГБ)

**Потребители — КОД (`work/`):**
- ПИШУТ: `step_cron_night/report_placement/step1_fetch_direct.py`, `step2_build_analytics.py`
  (отдельный cron суббота 00:00 МСК; TRUNCATE/UPSERT, **вне `pipeline.py`**).
- ЧИТАЮТ: `star_refactor/build_star.py` (T_ARP — строит из неё `star.arp_fact`),
  `star_refactor/verify_star.py` (сверка). Прочее — только `.md`-доки.
- `leads_api_perform/` — **НЕ ссылается** (0 совпадений).

**Потребители — БД:** `pg_depend` → **0 views / matviews / rules**, 0 входящих FK.

**Потребители — PBI:**
- STAR (=admin будущий): **8 визуалов на странице «Я.Директ_Площадки_РCЯ_без_tp8»**
  (`pages/728b39759452e783b8c6/visuals/`: 2e6c54, 3aa1b8, 7a2fbe, 86c826, a9a160,
  afa270, b2718f, bed7ef) ссылаются на таблицу. Поля: меры CPL/CR, `cost`,
  `kol_vo_zayavok`, `korr/kval/priezd/prodazhi/priedet/nekorr`, **`Все формы`,
  `CRM: Заказ создан/оплачен/отменён`, `CRM: Спам заказ`**, `placement`,
  **`placement_key`**, `ad_network_type` + измерения `домен/логин/салон/тип_сайта/
  директолог/номер кампании|название кампании`.
- USER: те же 8 визуалов на той же странице `728b...` ссылаются на таблицу.
- В TMDL STAR-модели партиция `analytics_report_placement` всё ещё читает
  `[Schema="public", Item="analytics_report_placement"]` (помечена
  `excludeFromModelRefresh`, но **схема/партиция живая** → визуалы рабочие).
- На `arp_fact` визуалами ссылается **0 файлов** (arp_fact в модели только ради
  4 dim-связей — подтверждает заметку пользователя).

**Чего не хватает в `star.arp_fact` (живая БД, 21 колонка сейчас):**
есть `placement, ad_network_type, domain, cost, clicks, kol_vo_zayavok, korr, kval,
priezd, prodazhi, nekorr, ne_otvechaet, nedozvon, filtr, priedet, dohod_do_kredita,
dobro, тип_заявки`. **НЕТ ровно 6:** `Все формы`, `CRM: Заказ создан`,
`CRM: Заказ оплачен`, `CRM: Заказ отменён`, `CRM: Спам заказ`, `placement_key`.
⚠️ Также визуалам нужны измерения `логин / салон / тип_сайта / директолог` —
их нет ни в arp_fact, ни гарантированно в Dim_* → проверить при репойнте (риск ниже).

**ВЫВОД (a):** дропать **НЕЛЬЗЯ** до выполнения цепочки:
1) +6 колонок (+ при необходимости измерения логин/салон/тип_сайта/директолог) в `star.arp_fact`;
2) репойнт 8 визуалов «Площадки РСЯ» на `arp_fact`/`Dim_*` **в admin И user**;
3) убрать таблицу `analytics_report_placement` из модели;
4) публикация обоих отчётов; **только потом** `DROP`.
Cron `report_placement` (суббота) тоже должен перестать материализовать ARP
**или** оставаться источником для arp_fact — см. примечание в Этапе 1.

### (b) `big_analytics_unified` (5.4 ГБ)

**Потребители — КОД (`work/`):**
- ПИШЕТ: `step13_arrival/build_unified.py` (CTAS full ∪ arrival), вызывается из
  `pipeline.py:766` и `fast_pipeline.py:602`.
- ЧИТАЮТ: `star_refactor/build_star.py` (T_UNI — источник истины для dim и факта),
  `star_refactor/verify_star.py` (OLD-эталон сверки). Прочее — `.md` (QUERIES/GOLDEN/ATTRIBUTION).
- `leads_api_perform/` — **НЕ ссылается** (CPL-сервис читает `big_analytics_full`).

**Потребители — БД:** `pg_depend` → **0 views / matviews / rules**, 0 входящих FK.

**Потребители — PBI:**
- STAR (=admin): партиция модельной таблицы `big_analytics_full` уже репойнтнута на
  `[Schema="star", Item="fact_big_analytics"]`. **Ни одна партиция не читает unified.**
  `big_analytics_full_arrival` читает `public.big_analytics_full_arrival` (не unified).
- USER: **0 ссылок** на `big_analytics_unified` (тонкий отчёт, своей модели нет).

**ВЫВОД (b):** после катовера ни модель, ни отчёты, ни `leads_api_perform` не читают unified.
Дроп **БЕЗОПАСЕН** при условии: build_star собирает star **напрямую** из
`full ∪ arrival` (этап E2), и сверка оракула после E2 не разошлась ни на йоту.
Если оракул разойдётся → ОТКАТ E2, оставляем unified (build_star из unified),
Фаза 2 = только ARP −13 ГБ (решение пользователя №5).

### Итоговая таблица drop-safety
| Таблица | Код вне PBI | БД (views/FK) | PBI admin=STAR | PBI user | Можно дропать? |
|---------|-------------|---------------|----------------|----------|----------------|
| `analytics_report_placement` | пишет cron report_placement; читают build_star/verify_star | 0 / 0 | **8 визуалов** (стр. Площадки РСЯ) | **8 визуалов** | **НЕТ** — только после +6 кол. в arp_fact, репойнта визуалов admin+user, публикации |
| `big_analytics_unified` | пишет build_unified (pipeline); читают build_star/verify_star | 0 / 0 | 0 (full→star.fact) | 0 | **ДА** — после E2 (star из full∪arrival) при совпадении оракула; иначе откат |

---

## Решения (согласовано с пользователем 2026-06-07)
1. **Промотирование admin = ЗАМЕНА ПАПОК** (STAR → admin целиком).
2. **`refresh_powerbi.py` и ВСЁ — на Victory.** Любой деплой кода — scp на Victory.
3. **Фаза 2 — урезана до −5.4 ГБ** (только unified). См. п.6–7 ниже.
4. **Публикацию в Service делает пользователь** из Desktop — ПОСЛЕ того как с моей стороны
   всё готово и проверено после прогона пайплайна.
5. **Звезда важнее 5 ГБ:** если E2 (источник star → full∪arrival) сдвинет оракул хоть на йоту
   — ОТКАТ E2, unified остаётся, Фаза 2 = 0 ГБ диска (но катовер на star всё равно состоится).
6. **ARP-дроп ОТЛОЖЕН** (находка: `build_star` строит `arp_fact` ИЗ `analytics_report_placement`;
   дроп требует полного рерайта источника на upstream — ДИФ 3b, отдельная задача).
   `analytics_report_placement` ОСТАЁТСЯ в БД как источник arp_fact (она excludeFromModelRefresh).
7. **Репойнт 8 визуалов «Площадки РСЯ» на `arp_fact` — ДЕЛАЕМ** (ускорение PBI) + убрать
   тяжёлую таблицу ИЗ МОДЕЛИ PBI (но НЕ из БД). Нужны: +6 conversion-колонок и измерения
   визуалов в arp_fact (источник — та же analytics_report_placement, которая остаётся).

---

## ПОРЯДОК ВЫКАТКИ (единый цикл, Фаза 2 включена)

> Принцип: все правки кода/модели/отчётов + прогон + проверка данных — моя сторона.
> Публикация — пользователь в конце. **DROP тяжёлых таблиц — в самом конце, ПОСЛЕ публикации**
> (иначе плановый refresh старого датасета сломается на отсутствующей таблице).

**Этап 1 — код пайплайна (моя сторона):**
- C1: врезать `build_star` после `build_unified` в pipeline.py + fast_pipeline.py.
- E1.1: патч build_star.py — **+6 колонок в `star.arp_fact`**: `Все формы`,
  `CRM: Заказ создан`, `CRM: Заказ оплачен`, `CRM: Заказ отменён`, `CRM: Спам заказ`,
  `placement_key` (источник — `public.analytics_report_placement`, который cron
  report_placement продолжает писать). ⚠️ Дополнительно проверить, нужны ли визуалам
  измерения `логин / салон / тип_сайта / директолог` напрямую из arp_fact (их нет ни
  в arp_fact, ни гарантированно в Dim_*) — если да, добавить и их (+малый вес).
- E2: патч build_star.py — строить star из `full ∪ arrival` напрямую; перестать
  материализовать `big_analytics_unified` в step13/build_unified.
- C2: обновить `_ALL_TABLES` (arp_fact, Dim_*, без arrival/ARP).
- C3: **scp pipeline.py fast_pipeline.py refresh_powerbi.py build_star.py → Victory**, маркер-проверка.
- Примечание по cron `report_placement` (суббота): он остаётся источником `arp_fact`
  → пока **НЕ трогаем**; после полного перехода визуалов на arp_fact и DROP ARP
  решить отдельно (либо писать сразу в arp_fact, либо хранить промежуточную лёгкую ARP).

**Этап 2 — прогон на Victory + проверка данных (моя сторона):**
- Прогон `pipeline.py` на Victory (nohup) → star.* (вкл. arp_fact с 6 кол.) свежие; unified не материализуется.
- Верификация: оракул Кудерко 25 422 774.027 / 47; arp_fact — 6 колонок присутствуют и
  конверсии сходятся (Все формы/CRM/placement_key) с прежней analytics_report_placement;
  star == прежний результат (без регрессии).

**Этап 3 — файлы отчётов (моя сторона). КРИТИЧНО для drop-safety:**
- A: бэкап admin → замена `.SemanticModel`/`.Report` из STAR + чистка `.bak`-мусора.
- E1.2 (admin=STAR): перенацелить **все 8 визуалов** страницы «Я.Директ_Площадки_РCЯ»
  (`pages/728b39759452e783b8c6/visuals/` 2e6c54,3aa1b8,7a2fbe,86c826,a9a160,afa270,b2718f,bed7ef)
  на `arp_fact` + `Dim_*`; убрать таблицу `analytics_report_placement` из модели
  (включая её партицию `[Schema="public",Item="analytics_report_placement"]`).
- B (user): visualInteractions ×36 + снять висячие page-фильтры + **ARP-репойнт тех же
  8 визуалов** страницы `728b...` на arp_fact/Dim_* (user читает датасет → структура
  полей должна совпасть с admin-моделью).
- ⚠️ Проверка drop-gate: `grep -rl analytics_report_placement` по `.Report/definition`
  ОБОИХ отчётов И по `.SemanticModel/.../tables` STAR должен дать **0 файлов**
  (кроме бэкапов) — это разрешающее условие на DROP ARP в Этапе 5.
- Проверка: оба отчёта парсятся, нет ссылок на удалённые таблицы/поля.

**Этап 4 — ПУБЛИКАЦИЯ (твоя сторона, после моего OK):**
- Открыть admin.pbip → Refresh (локально сверить Кудерко 47) → **Publish** в «Victory Analytics»
  (overwrite датасета, id aa304ada сохраняется) → задать datasource credentials (gateway → star.*).
- Открыть user → Refresh → **Publish**.
- Запустить `refresh_powerbi.py` на Victory → Completed → сверить дашборд с эталоном (34157/2384/854.7М).

**Этап 5 — освобождение диска (ТОЛЬКО после успешной публикации обоих отчётов):**

> 🔴 КРИТИЧНО (находка 2026-06-07): обе public-таблицы — это ИСТОЧНИКИ, из которых
> `build_star.py` пересобирает звезду КАЖДЫЙ прогон (`T_UNI=big_analytics_unified`,
> `T_ARP=analytics_report_placement`). Дроп источника = следующая пересборка звезды падает.
> Поэтому drop-gate должен включать ещё и «build_star больше НЕ читает таблицу».

Drop-gate (ВСЕ обязаны выполняться, иначе НЕ дропать):
1. Публикация admin+user прошла, refresh_powerbi.py = Completed, оракул сошёлся.
2. `grep <таблица>` по PBIR+TMDL обоих отчётов = 0 (Этап 3 drop-gate).
3. `pg_depend` по таблице = 0 views/rules (на 2026-06-07 уже 0).
4. **`grep <таблица> star_refactor/build_star.py` = 0** — build_star её НЕ читает как источник.

- `DROP TABLE public.big_analytics_unified` (−5.4 ГБ) — выполнимо: E2 переводит ВСЕ
  чтения `{T_UNI}` (fact + 4 Dim_*) на `full ∪ arrival`. После E2 gate №4 = 0 → дроп безопасен.
  Откат E2 при расхождении оракула (решение №5) → unified НЕ дропать.

- `DROP TABLE public.analytics_report_placement` (13 ГБ) — **ОТЛОЖЕНО (решение №6).**
  build_star строит `arp_fact` из неё → gate №4 НЕ выполнится без рерайта источника на
  upstream (ДИФ 3b). В этом цикле НЕ дропаем; таблица остаётся в БД как источник arp_fact.
  ARP-дроп — отдельной задачей позже (полный ДИФ 3b). Итого диск в этом цикле: **−5.4 ГБ**.

**Этап 6 — хвост:** бэкфилл (~1727 визитов) — когда дойдут руки.

---

## Что делаю Я и что делаешь ТЫ
- **Я (файлы + Victory):** Этапы 1–3 + 5 (правки pipeline/build_star/refresh, scp, прогон, проверка
  данных, замена папок admin, репойнт визуалов admin+user, DROP таблиц).
- **Ты (PBI Desktop):** Этап 4 — открыть admin/user, **Publish**, datasource credentials,
  `sudo chown` когда файлы станут root-owned после сохранения из PBI.
