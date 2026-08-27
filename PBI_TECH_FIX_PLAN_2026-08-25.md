# BA6 PBI техправки 2026-08-25

## Ограничение запуска

Второй прогон pipeline не запускать, пока Семён не опубликует Power BI Service с текущими PBIP-правками. Текущий ручной прогон `3914534` упал на шаге 146 из-за уже задеплоенного `f.\`специалист\`` в `pbi_big_analytics_full`; локальная правка готова, но повторный прогон ждёт публикации BI.

## План и статус

1. **Общая / Я.Директ / Я.Директ проверки / Посевы: верхние панели**
   - Привести подписи кнопок к одному виду.
   - Статус: локально в PBIP уже заменены технические `кодер группы_ct1/ct2/a-n-g/r` на русские `Кодер группы ...`; старые строки в JSON не находятся.

2. **Срезы: тип кампании, статус кампаний, тип оплаты**
   - Английские значения показывать по-русски.
   - Статус: локально в PBIP старые `AD_WORK/SEARCH/ACCEPTED/DRAFT/MODERATION/REJECTED` в JSON не находятся; BA6 PBI builder уже мапит статус/оплату/тип кампании на русские значения.

3. **Я.Директ / расстояние**
   - Причина: не было relationship `fact_region_spend.distance_km_agreg -> Dim_Distance.distance_km_agreg`, поэтому расходы повторялись по интервалам.
   - Статус: relationship добавлен в admin semantic model.

4. **Я.Директ / ключевые слова**
   - Причина: строки матриц брались из `Dim_AdNetworkType`, `dim_criterion`, `Dim_Site`, что даёт пустые строки и тяжёлые запросы.
   - Правка: добавить calculated columns в `fact_criterion_spend` и переключить строки визуалов на fact-поля.
   - Статус: сделано в `admin_ch` и `user_ch`; старые row-ссылки в проблемных матрицах очищены.

5. **Я.Директ / формат**
   - Причина: формат показывался как `IMAGE/TEXT/SMART_SINGLE/...`, а строки брались из Dim.
   - Правка: русский mapping форматов в PBIP и BA6 `Dim_AdFormat`, строки визуалов через `fact_adformat_spend`.
   - Статус: сделано локально; SQL `Dim_AdFormat` проходит `EXPLAIN SYNTAX`.

6. **Я.Директ / минус-фразы**
   - Причина: верхняя матрица тянула `Dim_Campaign.CampaignName` вместо snapshot-поля.
   - Правка: `yandex_direct_minus_snapshot[campaign_name] = RELATED(Dim_Campaign[CampaignName])`, визуал переведён на snapshot.
   - Статус: сделано в обоих reports.

7. **Я.Директ / корректировки**
   - Причина: BA6 строковая раскладка отличалась от BA5: `Dim_Adjustment`/`account_login` вместо `big_analytics_full`-полей.
   - Правка: добавить `RlAdjustmentId_total`, `номер корректировки | логин`, `номер кампании | название кампании` в `big_analytics_full`; визуалы вернуть к BA5-составу строк.
   - Статус: сделано локально.

8. **VK Ads фильтры**
   - Причина: spend-строки `fact_vk_ads` имели NULL в `салон/регион/тип_сайта/специалист`, а заявки имели значения; фильтр по типу сайта/региону отрезал расход.
   - Правка: в `build_star.py` добавить `salon_by_acc` через `reference_data.vk_ads_agency_clients.domain -> reference_data.gsheet_sites.domain` и заполнять классификацию spend-строк; также вернуть `ad_plan_name/ad_group_name/banner_name`, на которые уже ссылается PBIP.
   - Статус: локальный SQL проходит `EXPLAIN SYNTAX`; нужен следующий pipeline после публикации BI.

9. **Шаг 146 / PBI full**
   - Причина падения текущего прогона: `pbi_big_analytics_full` ссылался на `f.\`специалист\``, но `fact_big_analytics` такой колонки не содержит.
   - Правка: брать `специалист` из `Dim_Site` через `site_key`.
   - Статус: локальный `pbi_full` проходит `EXPLAIN SYNTAX`.

10. **Две пустые строки в кодере групп**
    - Причина: страницы с `Dim_AdGroup.ag_part*` показывают пустые значения как отдельные строки; при соседних матрицах/уровнях это выглядит как две строки “нет данных”.
    - Страницы с риском: `Я.Директ Кодер группы A/N/G`, `Я.Директ Кодер группы CT1`, `Я.Директ Кодер группы CT2/AG`, `Я.Директ Кодер группы R`, `Я.Директ Кодер группы`, `Я.Директ марки авто`, `Я.Директ проверки ошибки в кодере`, `Общая Домен`.
    - Правка: нормализовать пустые `Dim_AdGroup.adgroup_code`, `марки авто`, `ag_part1..ag_part7`, `ag_part1_name` в `Не указано`.
    - Статус: сделано в PBIP Power Query и BA6 `_dim_adgroup_pbi_sql()`; SQL проходит `EXPLAIN SYNTAX`.

## Деплой runtime-кода

На Victory без запуска pipeline доставлены runtime `.py`:

- `spend/dated_site_join.py`
- `star_refactor/build_pbi_compat.py`
- `star_refactor/build_star.py`
- `step13_arrival/step13.py`
- `step3_build_sources/step3.py`

Проверено: md5 Mac==Victory, локальный и удалённый `py_compile`, marker grep (`salon_by_acc`, `dsite.\`специалист\``, `Не указано`). Pipeline после деплоя не запускался.

## Power BI refresh error 17:23

После падения старого step146 в ClickHouse отсутствовали `bi_*` views, поэтому Power BI Desktop
показал “Запросы заблокированы” (`datetime/cost/ulogin/domain`). Исправлено без второго полного
pipeline:

- PBIP queries `direct_history`, `check_utm_fuck_direct`, `yandex_direct_korrektirovki`,
  `pixel_score`, `fact_region_spend` переведены на существующие базовые CH-объекты и сами
  добавляют compatibility-поля.
- `build_pbi_compat.py`: `bi_fact_vk_ads` берет `ad_plan_name/ad_group_name/banner_name` из
  `Dim_VkAdPlan/Dim_VkAdGroup/Dim_VkBanner`, а не из еще не пересчитанного `fact_vk_ads`.
- `bi_fact_region_spend_star` получил `domain`/`updated_at` compatibility-поля.
- Запущен только `star_refactor/build_pbi_compat.py`: `bi_views_created=47`, полный pipeline не
  запускался.

## Проверки

- PBIP JSON: все `.json` в `Большая аналитика_admin_ch` и `Большая аналитика_user_ch` парсятся.
- Python: `py_compile star_refactor/build_star.py star_refactor/build_pbi_compat.py` проходит.
- ClickHouse syntax: `pbi_full`, `region_spend`, `adformat_spend`, `criterion_spend`, `vk_pbi`, `Dim_AdFormat`, `Dim_AdGroup`, `vk_ads` проходят `EXPLAIN SYNTAX`.
- ClickHouse live: все 41 источника из admin TMDL проходят `DESCRIBE`, `missing=0`.
- Не проверено до публикации и нового pipeline: фактическая загрузка новых VK/PBI columns в ClickHouse и визуальная проверка в Power BI Service.
