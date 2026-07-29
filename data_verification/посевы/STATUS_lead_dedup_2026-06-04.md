# Посевы — статус и находки по задвоению заявок (4 июня 2026)

> Рабочий журнал сессии. Что сделано, что подтверждено, что осталось, и как доделать.
> Связанные доки: [README.md](README.md) · [AUDIT_PLAN.md](AUDIT_PLAN.md) · [PROJECT_CHARTER.md](PROJECT_CHARTER.md)

---

## TL;DR (главное)

| Что | Статус |
|---|---|
| **Расход до мая** = 9 213 650 ₽ (раздувание +12% от `gsheets_nearest` убрано) | ✅ применено и проверено на проде |
| **Расход май** = 2 181 280 ₽ (только из `local_telega_in_orders`) | ✅ применено и проверено |
| **Задвоение заявок (Вариант A)** — апрель `kol_vo`=741, должно 444 | ⚠️ КОД ГОТОВ ЛОКАЛЬНО, на прод НЕ применён |

**Деньги корректны.** Не доведён до конца только дедуп счётчика заявок (Вариант A) — апрельские строки crop_targeting завышены на ~297 заявок.

---

## 1. Что ТОЧНО подтверждено на проде (read-only запросы)

### Расход crop_targeting в big_analytics_full по месяцам (после прогонов B1+C1):
| Месяц | kol_vo_zayavok | total_cost |
|---|---|---|
| 2026-01 | 765 | 3 014 878 |
| 2026-02 | 384 | 1 801 818 |
| 2026-03 | 498 | 2 215 229 |
| 2026-04 | **741** ⚠️ | 2 181 725 |
| 2026-05 | 1149 | 2 181 280 |

- Расход (cost) — корректен, = реестр (до мая 9 213 650) + telega (май 2 181 280). Дублей расхода нет.
- **kol_vo апреля = 741** — это с задвоением. Должно стать **444** после Варианта A.

### Майский расход 2 181 280 — почему не 2 334 687 (разобрано, всё верно)
- Колонка расхода = `total_price` из `local_telega_in_orders` (уже финальная цена с НДС/наценкой Telega.in, ≈×1.416 от `price`). Никаких наших множителей.
- Дата = `effective_date`: `utm_content` (DDMMYYYY) или fallback `completed_at/done_at/created_at`. Фильтр `status='complete'`.
- `2 334 686.67` = сумма по `created_at` БЕЗ фильтра статуса. Разница `153 406.43` = **13 отменённых заказов** (`status='cancel'`: «бот не выпустил пост», «отменено заказчиком» и т.п.). Их деньги Telega.in возвращает → правомерно исключены.
- **Вердикт: 2 181 280 ₽ — правильно.**

---

## 2. Задвоение заявок — ПОДТВЕРЖДЕНО (Вариант A нужен)

### Суть бага
Майский посевной лид (`created_date >= 2026-05-01`) считается ДВАЖДЫ:
1. **gsheets-путь**: привязывается nearest-prior к АПРЕЛЬСКОМУ размещению (т.к. `posev_leads_raw` без верхней границы даты) → попадает в апрельскую строку crop_targeting (дата < мая → проходит фильтр в `load_crop_to_big_analytics.py`).
2. **telega-путь**: тот же лид матчится к майскому telega-заказу → майская строка crop_targeting.

### Цифры (anton_sql, точная проверка)
- **297 майских лидов задвоены** (есть и в апрельском gsheets, и в майском telega).
- Апрель `kol_vo` = 741, должно ~444 (−297).
- Итого crop_targeting = 3537, должно ~3240.
- **Расход НЕ задвоен** (cost берётся из размещения один раз).
- Примеры задвоенных lead_id: 16096673, 16013532 (`bashdtp_bash.dtp.official`, newauto-102.ru), 16046949 (`avtorynok_vlg`, carnew-vlg.ru).
- Топ каналов: `region116_max` (59), `bashkiriya_online_max` (32), `yug_24_...krasnodara` (26), `bashdtp_..._max` (15).
- **26 уникальных** майских лидов (домен `avtoworld-kuban.ru`, краснодарские VK/Max, которых НЕТ в telega) — терять нельзя.

### Ключ матчинга telega (из load_telega_in_orders.py)
`utm_campaign + домен (LOWER(TRIM(local_domains.name)) = effective_domain) + месяц лида в окне ±1` от даты заказа.

---

## 3. Вариант A — ЧТО СДЕЛАНО В КОДЕ (локально готово)

Выбран **Вариант A** (точный NOT EXISTS, НЕ грубый дата-бордюр `< мая` — тот терял бы 26 уникальных).

### Изменения в 3 файлах (`work/big_analytics_v5/step10_crop_targeting/`):

1. **`load_telega_in_orders.py`** — добавлена колонка `utm_campaign` в выход `crop_targeting_api_telegain_lead`:
   - DDL: `utm_campaign TEXT`
   - SELECT: `d.utm_campaign AS utm_campaign`
   - INSERT-список: `+ utm_campaign`
   - (downstream `load_crop_to_big_analytics.py` не ломается — там явный список колонок, не `SELECT *`)

2. **`load_crop_targeting_leads.py`** — в `posev_leads_raw`:
   - `LEFT JOIN public.local_domains ld ON ld.id = l.domain_id` (домен лида)
   - условие исключения:
   ```sql
   AND NOT (
       l.created_date >= '2026-05-01'
       AND EXISTS (
           SELECT 1 FROM public.crop_targeting_api_telegain_lead t
           WHERE t.utm_campaign = l.utm_campaign
             AND LOWER(TRIM(t.domain)) = LOWER(TRIM(ld.name))
             AND DATE_TRUNC('month', t."Date")::date BETWEEN
                     (DATE_TRUNC('month', l.created_date) - INTERVAL '1 month')::date
                 AND (DATE_TRUNC('month', l.created_date) + INTERVAL '1 month')::date
       )
   )
   ```

3. **`pipeline.py`** — порядок шагов изменён: `load_telega_in_orders` (ШАГ 2) ТЕПЕРЬ ДО `load_crop_targeting_leads` (ШАГ 3), чтобы при NOT EXISTS таблица telega была свежей.

Все 3 файла локально содержат правки и компилируются (py_compile OK). **Проверено локальным grep.**

---

## 4. Статус деплоя на Victory (НЕ завершён)

| Файл | Залит на прод? |
|---|---|
| `pipeline.py` | ✅ да (через `printf '%s' '<b64>' | base64 -d`, подтверждён новый порядок ШАГ2=telega) |
| `load_crop_targeting_leads.py` | 🟡 декод через printf прошёл, маркер не подтверждён из-за проблем захвата вывода |
| `load_telega_in_orders.py` | ❌ деплой прерван пользователем (был на printf-методе) |

### ⚠️ Инфраструктурная проблема сессии (важно для следующего раза)
- **SSH работает** (вход по паролю из `.secret/.env` → `PASS`, юзер `semen_vi@103.88.240.90`).
- **`scp` ПЕРЕСТАЛ переносить файлы** молча (после множества подключений). Раньше в сессии работал.
- **Захват вывода SSH-команд через `expect` нестабилен**: простые команды (`cat file`, одиночный `printf...|base64 -d > file; grep`) ловятся, а составные (`cd; a; b; c`, бэктики `` `cmd` ``, скобочные группы `{ }`) — часто отдают только spawn-строку без результата.
- **Рабочий способ залить файл:** `printf '%s' '<base64>' | base64 -d > path` (НЕ `echo $B64` — тот пишет криво).
- **Рабочий способ выполнить запрос с кириллицей:** записать .py локально → base64 → `echo <b64> | base64 -d > /tmp/q.py && ... python3 /tmp/q.py` (избегает экранирования кириллицы в SQL).
- **Рабочий способ прочитать результат:** либо `cat файл` одной командой, либо `-re {.+} {exp_continue}` catch-all драйв.
- `nohup ... &` ВНУТРИ ssh-скобок `{}` НЕ запускал процесс — использовать двойные кавычки и foreground-обёртку, держащую соединение.

---

## 5. ЧТО ОСТАЛОСЬ СДЕЛАТЬ (чек-лист для доведения Варианта A)

1. [ ] Дозалить `load_telega_in_orders.py` (printf-метод) — добавляет колонку `utm_campaign`.
2. [ ] Подтвердить, что все 3 файла на проде содержат правки + `py_compile` OK. Маркеры:
   - `load_telega_in_orders.py`: `grep -c 'utm_campaign     TEXT'` = 1
   - `load_crop_targeting_leads.py`: `grep -c 'crop_targeting_api_telegain_lead t'` = 1
   - `pipeline.py`: ШАГ 2 = load_telega_in_orders, ШАГ 3 = load_crop_targeting_leads
3. [ ] Прогнать `step10_crop_targeting/pipeline.py` (двойные кавычки, foreground-обёртка держит коннект ~5-10 мин, лог в файл).
4. [ ] Проверить лог: «Пайплайн посевов завершён», без `Traceback`. ВНИМАНИЕ: новый порядок — ШАГ2 telega (создаёт колонку utm_campaign), ШАГ3 leads (использует её в NOT EXISTS). Если порядок не тот — NOT EXISTS упадёт «column t.utm_campaign does not exist».
5. [ ] Проверить данные (q.py base64-метод):
   ```sql
   SELECT to_char(date_trunc('month',"Date"),'YYYY-MM'), sum(kol_vo_zayavok), round(sum(total_cost))
   FROM big_analytics_full WHERE "направление"='посевы' AND _source_table='crop_targeting'
   GROUP BY 1 ORDER BY 1;
   ```
   Ожидаем: **апрель kol_vo ≈ 444** (было 741), итого crop_targeting ≈ 3240. Расход без изменений (до мая 9 213 650, май 2 181 280).
6. [ ] Если апрель стал 444 → ✅ Вариант A применён. Обновить статусы в CHARTER/AUDIT_PLAN.
7. [ ] Закоммитить правки в репозиторий big_analytics_v5 (по явной команде пользователя).

### Команда прогона (когда деплой подтверждён):
```bash
# foreground на сервере, лог в файл (двойные кавычки, НЕ скобки/nohup):
ssh semen_vi@103.88.240.90 "cd ~/big_analytics_v5 && ~/venv/bin/python3 step10_crop_targeting/pipeline.py > /tmp/cf.log 2>&1"
# читать лог: ssh ... "cat /tmp/cf.log"
```

---

## 6. social_посевы vs crop_targeting (попутно разобрано)

| | crop_targeting | social_посевы |
|---|---|---|
| Что | посевы **с расходом** | VK/Max/storis заявки **без расхода** (cost=NULL) |
| Когда | канал есть в реестре gsheets / telega | utm_source ∈ (max,vk,vk_groups,vk_storis,telegram_storis)+posev, канала НЕТ в реестре |

`social_посевы` = «заявки есть, расход не заведён» (индикатор недозаполнения реестра VK/Max, пункт P6).
Из-за cost=NULL занижают CPL по Max/VK в 1.8× при группировке (пункт P7) — в отчётах фильтровать `cost IS NOT NULL`.
Разделение оставить (полезный сигнал). Корневое решение — завести VK/Max-расход.

---

## 7. Прочие открытые проблемы заявок (не блокеры)

- **64 лида теряются полностью** из витрины: `utm_source` = домен дилера (driveavto-kazan.ru и т.п.), правила в `pravilo_utm` нет, не telegram/VK/Max → не попадают никуда. Список и детали — в [missing_utm_pravilo.txt](missing_utm_pravilo.txt) (помечены `[ТЕРЯЕТСЯ]`). Решение: дозаполнить справочник ИЛИ доработать step3 под domain-источники.
- **255 лидов (до мая) без правила в pravilo_utm** — telegram/Max/VK видны через telegram/social_посевы, но без привязки к расходу. Список в [missing_utm_pravilo.txt](missing_utm_pravilo.txt).
- **Битые UTM** (дата в utm_campaign: `chp_24_kumertau=25042026` и др.) — поправить разметку на каналах.

---

_Создано: 4 июня 2026, по итогам сессии. Деньги верны; Вариант A (дедуп заявок) — код готов, деплой не завершён._
