# STATUS — фикс листа «ошибки1» + межсайтового задвоения посевов (2026-06-06)

Продолжение root-cause сессии `a7404552` (запись памяти `oshibki1-tvoy_stvrp-rootcause-2026-06-05`).

## ШАГ 1 — фикс кода `load_crop_targeting_leads.py` (LOCAL + deployed)

Файл: `work/big_analytics_v5/step10_crop_targeting/load_crop_targeting_leads.py`
Фикс **уже присутствовал** в рабочем дереве (uncommitted; репо `big_analytics_v5` имеет только init-коммит).

Суть фикса v2 (межсайтовое задвоение `tvoy_stvrp`/havalcar):
- `posev_leads_attributed` (строки 257–260): домен добавлен в **условие JOIN**
  `LOWER(TRIM(p."Сайт")) = LOWER(TRIM(lr.lead_domain))`, а не только в ORDER BY.
- `orphan_leads` (284–285): NOT EXISTS зеркалит то же доменное условие → лид без размещения
  на СВОЁМ домене становится orphan на своём домене, не приклеивается к чужому.
- `orphan_agg` GROUP BY `lead_domain` → orphan-строки ложатся на свой домен.
- `py_compile` — OK. md5 = `668f509de5b0c1c525347c07e006b28a`.

Эффект: 13–18 autopark/autostorage лидов `tvoy_stvrp` больше не клеятся к havalcar 15.01
(раньше havalcar получал `kol_vo_zayavok=114` при расходе 6210₽). **Применится к данным только
после прогона** `step10_crop_targeting/pipeline.py`.

## ШАГ 2 — перегенерация листа «ошибки1» (gid=22873707)

Таблица `1qsGRWYLOztpUnh5fHN6Uka8NyLmpVEKZZVn-jod1vCo`.
Бэкап старого листа (525 строк) → `/tmp/oshibki1_backup_v2.json`. Скрипт → `/tmp/regen_oshibki1.py`.

**Новый 2-условный критерий «реально потерянного» лида** (universe: posev-лиды
`utm_medium='posev'`, `created_date >= 2026-01-01`, 2797 шт.):
- **C1** — НЕТ complete-заказа Telega.in по 5-ключу
  (домен + utm_campaign + `lpad(utm_content,8,'0')` + utm_source + utm_medium;
  при пустом utm_content — по 4 полям без content).
- **C2** — И НЕТ покрытия в `big_analytics_full` по **домену+месяцу** через посев-пути
  (`_source_table IN (crop_targeting, tp8, social_посевы, calls, seo, telegram)`).
  (BAF не содержит колонок utm — покрытие только по domain+month.)

### Результат

| Метрика | Значение |
|---------|----------|
| Всего posev-лидов | 2797 |
| C1 fail (нет заказа Telega.in) | 1576 |
| C2 fail (нет покрытия в BAF) | **1** |
| **РЕАЛЬНО ПОТЕРЯНО (C1 ∩ C2)** | **0** |
| Лидов с пустым utm_content | 1984 |

Прошлый 1-условный критерий давал ~414 «потерянных» (106 комбо) — **почти все ложные**:
янв-апр лиды атрибуцируются gsheets-путём в `big_analytics_full`, а заказы Telega.in API
существуют только с мая. Добавление C2 схлопнуло их до 0.

Лист перезаписан (clear+put): шапка с пояснением + блок агрегата (пуст — 0 потерянных,
дедуплицирован) + лид-блок (пуст) + блок «АНОМАЛИЯ C2» (1 лид). Телефоны маскированы
(6 цифр + `***`). Лист1 и другие листы не тронуты.

### Лиды «требуют решения по форме UTM»

**Грабля:** в первой версии скрипта все 366 «потерянных» имели `domain_id IS NULL` —
у них **съехала форма UTM**: домен попал в `utm_source` (`utm_source='driveavto-kazan.ru'`).
Без домена C2 (покрытие по домену) никогда не матчит → ложно «потеряны».
**Фикс резолва домена:** `COALESCE(local_domains по domain_id, local_domains по utm_source
если LIKE '%.%')`. 17 distinct таких доменов, все есть в `local_domains`. После fallback
все 366 получили покрытие в BAF → не потеряны.

Эти лиды (домен в utm_source ИЛИ пустой utm_content) — кандидаты на исправление формы UTM
на стороне разметки. Сложный маппинг не вводился (по решению задачи).

### Аномалия C2 (1 лид, проходит C1 → не потерян)

`lead_id=16480121`, `ladaauto-vlg.ru`, 2026-02-01, `telegram/vlg_n1`, пустой content —
домен без посев-покрытия в `big_analytics_full` за февраль, но complete-заказ Telega.in
есть (4-ключ при пустом content) → не потерян. Записан в блок «АНОМАЛИЯ C2» листа для
ручной проверки покрытия домена в витрине.

## ШАГ 3 — деплой на Victory

`scp` (expect, пароль через env `VPASS` — в пароле символ `$`) →
`semen_vi@103.88.240.90:~/big_analytics_v5/step10_crop_targeting/load_crop_targeting_leads.py`.

md5 локальный == md5 Victory = `668f509de5b0c1c525347c07e006b28a` ✅

**Пайплайн посевов НЕ запускался.** Для применения фикса к данным нужен прогон:
```bash
ssh victory "cd ~/big_analytics_v5 && ~/venv/bin/python3 step10_crop_targeting/pipeline.py"
```
Данные пересоберутся автоматически при следующем плановом прогоне.
