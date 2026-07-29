# DB_TABLES.md — Таблицы на сервере (ad_analytics_bi)

Актуально на 2026-06-17. Схемы сверены с реальной БД Victory (`information_schema.columns`).

---

## Пояснения к терминам

### Принцип локальных копий

Локальные копии (`local_leads_all`, `local_yandex`) — **эталон данных**. Если в `src.*` данных стало меньше, чем в локальной копии — это ошибка на стороне источника. Локальная копия верная.

Строки из локальных копий **никогда не удаляются**. Синхронизация только добавляет новые и обновляет изменённые (UPSERT по `updated_at`).

---

### RAW-таблицы (`raw_yandex`, `raw_leads`, `raw_calls`, `raw_domains`)

Зачем: промежуточный слой между «сырыми локальными копиями» и «финальными аналитическими таблицами».

```
local_leads_all (2 млн строк, все данные с 2026-01-01, индексы)
         │
         ▼  шаг 1: DROP + CREATE UNLOGGED + INSERT (фильтрованно)
raw_leads (только заявки, без blacklist-доменов)
         │
         ▼  шаг 2: индексы + ANALYZE
         │  теперь raw_leads — маленькая, быстрая, готова к тяжёлым JOIN
         ▼
big_analytics_direct, big_analytics_seo, ...
```

UNLOGGED = без записи в WAL-журнал → INSERT в 2-3 раза быстрее.
Пересоздаются каждый запуск — нет «накопленного мусора», всегда чистый срез данных.

---

## Дерево таблиц

```
ad_analytics_bi/
│
├── src/                                              ← FDW (FOREIGN TABLE), только чтение, не трогаем
│   ├── leads                                         ← CRM-заявки и звонки (источник)
│   ├── leads_all                                     ← расширенная версия leads с доп. полями
│   ├── yandex_direct_manager_reports                 ← расходы/клики Яндекс.Директ (источник)
│   ├── domains                                       ← справочник доменов (источник)
│   ├── crm_statuses                                  ← справочник статусов заявок (источник)
│   ├── gsheet_sites                                  ← ВСЕ САЙТЫ (DB-admin обновляет)
│   ├── gsheet_naming                                 ← Нейминг (DB-admin обновляет)
│   ├── gsheet_reestr                                 ← Реестр (DB-admin обновляет)
│   ├── gsheet_plan_fakt                              ← План/факт (DB-admin обновляет)
│   ├── gsheet_vse_klienty                            ← Все клиенты (DB-admin обновляет)
│   ├── gsheet_autosalony_clients                     ← Автосалоны клиенты (DB-admin обновляет)
│   ├── gsheet_priezdi_marcar                         ← Маркар Доезды (DB-admin обновляет)
│   ├── salon_regions                                 ← регионы салонов (DB-admin обновляет)
│   └── telega_in_orders                              ← заказы Telegram (DB-admin обновляет)
│
└── public/                                           ← наши таблицы
    │
    ├── [ЛОКАЛЬНЫЕ КОПИИ]                             ← живут между запусками, LOGGED
    │   ├── local_leads_all                           ← копия src.leads_all (инкрементальная, UPSERT)
    │   │     • row_hash TEXT
    │   │     • id BIGINT, source_type, data_source_id, raw_row_id
    │   │     • created_date DATE, arrival_date DATE
    │   │     • status, deal_type, domain_id BIGINT
    │   │     • source_name, salon, campaign_id BIGINT, campaign_parse_failed BOOLEAN
    │   │     • group_id BIGINT, correction_id BIGINT, is_copy_for_removal BOOLEAN
    │   │     • phone, utm_source, utm_medium, utm_campaign, utm_term, utm_content
    │   │     • yclid, client_id, source_record_id, reason
    │   │     • created_at TIMESTAMP, updated_at TIMESTAMP
    │   │
    │   ├── local_yandex                              ← копия src.yandex_direct_manager_reports (UPSERT)
    │   ├── local_domains                             ← копия src.domains (полная замена)
    │   ├── local_crm_statuses                        ← копия src.crm_statuses (полная замена)
    │   ├── local_gsheet_sites                        ← копия src.gsheet_sites (полная замена)
    │   ├── local_gsheet_naming                       ← копия src.gsheet_naming (полная замена)
    │   ├── local_gsheet_reestr                       ← копия src.gsheet_reestr (полная замена)
    │   ├── local_gsheet_plan_fakt                    ← копия src.gsheet_plan_fakt (полная замена)
    │   ├── local_gsheet_vse_klienty                  ← копия src.gsheet_vse_klienty (полная замена)
    │   ├── local_gsheet_autosalony_clients           ← копия src.gsheet_autosalony_clients (полная замена)
    │   ├── local_gsheet_priezdi_marcar               ← копия src.gsheet_priezdi_marcar (полная замена)
    │   ├── local_gsheet_yandex_direct_id_location    ← справочник гео-таргетинга Директа
    │   │     • id_location INTEGER PK
    │   │     • location TEXT, GeoRegionType TEXT, Область TEXT
    │   │     • distance_km INTEGER, distance_km_agreg INTEGER
    │   │
    │   ├── local_telega_in_orders                    ← копия src.telega_in_orders (заказы Telegram)
    │   │     • id BIGINT PK, uid TEXT, order_id BIGINT
    │   │     • order_project_name, order_comment, channel_id BIGINT
    │   │     • channel_name, channel_link, post_link, placement_format
    │   │     • status, cancel_comment, price NUMERIC, total_price NUMERIC
    │   │     • total_views BIGINT, clicks BIGINT, post_links TEXT
    │   │     • utm_source, utm_medium, utm_campaign, utm_content, utm_term
    │   │     • created_at, completed_at, done_at, run_at TIMESTAMP
    │   │     • raw JSONB, updated_at TIMESTAMP
    │   │
    │   └── local_telega_in_orders_errors             ← ошибки матчинга заказов Telegram
    │         • id BIGINT, order_id BIGINT, order_project_name, post_links
    │         • status, utm_source/medium/campaign/content, utm_content_norm
    │         • effective_domain, site_status, directologist, salon, city, region
    │         • total_price NUMERIC, created_at TIMESTAMP
    │         • error_type, error_detail, checked_at TIMESTAMP DEFAULT now()
    │
    ├── [ПОСЕВЫ]                                      ← живут между запусками, LOGGED
    │   ├── gsheets_crop_targeting_account            ← сырые данные из Google Sheets (финансы)
    │   │     • id SERIAL PK
    │   │     • Специалист, Дата, Гео, Гео2, Сайт TEXT
    │   │     • "Тип закупа", "utm утвержденная", Источник, Канал TEXT
    │   │     • НДС, "Цена закупа без ндс", "сумма входящего ндс" TEXT
    │   │     • "Проценты ак", "Цена продажи клиенту с НДС, руб." TEXT
    │   │     • "Наша комиссия с НДС, руб.", "Наша чистая комиссия (без затрат н" TEXT
    │   │     • total_cost TEXT
    │   │
    │   ├── gsheets_crop_targeting_account_leads      ← посевы + лиды, схлопнутые по (utm, месяц)
    │   │     • id SERIAL PK
    │   │     • Специалист, Дата, Гео, Гео2, Сайт TEXT
    │   │     • "Тип закупа", "utm утвержденная", Источник, Канал TEXT
    │   │     • "сумма входящего ндс", "Цена продажи клиенту с НДС, руб." TEXT
    │   │     • total_cost TEXT
    │   │     • kol_vo_zayavok, korr, kval, priezd, prodazhi INTEGER
    │   │     • nekorr, ne_otvechaet, filtr, nedozvon, priedet INTEGER
    │   │     • dohod_do_kredita, dobro INTEGER
    │   │
    │   ├── gsheets_crop_targeting_account_pravilo_utm ← правила нормализации UTM посевов
    │   │     • id SERIAL PK
    │   │     • UTM TEXT           ← исходная UTM из таблицы
    │   │     • "utm утвержденная" TEXT ← нормализованная
    │   │
    │   ├── crop_targeting_api_telegain_lead          ← посевы через API Telegain (новый источник)
    │   │     • id SERIAL PK
    │   │     • Date DATE, total_cost NUMERIC, CampaignName TEXT, domain TEXT
    │   │     • салон, город, источник, поставщик, специалист TEXT
    │   │     • статус, тип_сайта, шаблон, регион, direction TEXT
    │   │     • kol_vo_zayavok, korr, kval, priezd, prodazhi INTEGER
    │   │     • nekorr, ne_otvechaet, filtr, nedozvon, priedet INTEGER
    │   │     • dohod_do_kredita, dobro INTEGER, utm_campaign TEXT
    │   │
    │   └── leads_crop_attribution                    ← кэш атрибуции «лид → посевы»
    │         • lead_id BIGINT PK
    │         • is_crop BOOLEAN NOT NULL DEFAULT false
    │         • crop_source TEXT, attributed_by TEXT
    │         ⚠ Таблица создана, но LEFT JOIN к ней — no-op (пустая → условие всегда TRUE).
    │           Исключение посевных лидов из директа делают прямые UTM-фильтры в step3.
    │
    ├── [RAW — ВРЕМЕННЫЕ]                             ← UNLOGGED, пересоздаются каждый запуск
    │   │   Зачем: чистый срез данных для JOIN, быстрая вставка без WAL
    │   ├── raw_yandex                                ← из local_yandex
    │   ├── raw_leads                                 ← из local_leads_all (только заявки, без blacklist)
    │   ├── raw_calls                                 ← из local_leads_all (только звонки)
    │   └── raw_domains                               ← из local_domains
    │
    ├── [РЕЗУЛЬТИРУЮЩИЕ ПО ИСТОЧНИКАМ]                ← пересоздаются каждый запуск
    │                                                 Колонки: domain, "специалист", "салон" и др.
    │                                                 "салон" берётся из local_gsheet_sites.salon:
    │                                                   • direct:   JOIN по account_login = login_key
    │                                                   • остальные: JOIN по domain = gs."domain"
    │   ├── big_analytics_direct                      ← Яндекс.Директ (все) + tp8 + лиды директ
    │   │     поставщик = 'Яндекс'
    │   ├── big_analytics_crop_targeting              ← Посевы + лиды посевов
    │   │     поставщик = "Тип закупа" из gsheets_crop_targeting_account_leads
    │   ├── big_analytics_seo                         ← лиды без UTM-меток (не звонки)
    │   │     поставщик = 'Victory'
    │   ├── big_analytics_pixel                       ← Pixel лиды (utm_source LIKE 'victory_%')
    │   │     поставщик = 'Victory'              ← пустая таблица на старте
    │   ├── big_analytics_telegram                    ← utm-посевы telegram + лиды (БЕЗ tp8)
    │   │     поставщик = NULL
    │   └── big_analytics_reviews                     ← данные из аналитики АРП (отзывы)
    │         структура как у big_analytics_unified (73+ колонки)
    │
    ├── big_analytics_full                            ← UNION ALL всех выше + звонки отд. строками
    │
    ├── big_analytics_unified                         ← расширенный UNION + corrections (step3→step8)
    │     структура: 73+ колонки big_analytics_full + доп. поле атрибуция TEXT
    │
    ├── big_analytics_pixel_score                     ← big_analytics_direct с пиксельными оценками
    │     структура как у big_analytics_unified (step11)
    │
    ├── big_analytics_full_arrival                    ← воронка по дате ВИЗИТА (step13)
    │     структура как у big_analytics_unified +
    │     • priezd_arrival_date BIGINT, prodazhi_arrival_date BIGINT
    │
    ├── [DIRECT FEED FUNNEL]                          ← отдельная витрина по фидам Директа
    │   ├── yandex_direct_feeds_report                ← источник расходов/кликов по фидам
    │   │     • date DATE, login_key TEXT, domain TEXT
    │   │     • campaign_id BIGINT, campaign_name TEXT
    │   │     • adgroup_id BIGINT, adgroup_name TEXT, ad_id BIGINT
    │   │     • feed_id BIGINT, feed_name TEXT
    │   │     • feed_url TEXT, feed_url_key TEXT — из cookie/web-api Директа
    │   │     • impressions BIGINT, clicks BIGINT, cost NUMERIC
    │   │     • goal_all_forms, goal_crm_order_created, goal_crm_order_paid
    │   │     • goal_crm_spam_order, goal_crm_order_canceled
    │   │     • updated_at TIMESTAMP
    │   │
    │   ├── yandex_direct_feed_urls                  ← map feed_id → реальный URL фида (fetch_feed_urls_cookie.py)
    │   │     • login_key TEXT, feed_id BIGINT         (PK: login_key + feed_id)
    │   │     • feed_name, feed_url, feed_url_key
    │   │     • source, feed_type, update_status
    │   │     • offers_count BIGINT, listings_count BIGINT
    │   │     • cookie_account TEXT, fetched_at TIMESTAMPTZ
    │   │
    │   ├── direct_feed_spend_keyed                   ← расходы по ключу Date|CampaignId|AdGroupId|feed_key
    │   │     • feed_key3 TEXT, date DATE, login_key TEXT, domain TEXT
    │   │     • campaign_id BIGINT, campaign_name TEXT
    │   │     • adgroup_id BIGINT NULL для tp6/tp7, adgroup_name TEXT
    │   │     • feed_id BIGINT, feed_name TEXT, feed_url TEXT, feed_url_key TEXT
    │   │     • feed_key TEXT, is_tp67 BOOLEAN
    │   │     • impressions, clicks, total_cost
    │   │     • цели CRM из Директа, generated_at TIMESTAMP
    │   │
    │   ├── direct_feed_leads_keyed                   ← лиды с fid из utm_content по тому же ключу
    │   │     • feed_key3 TEXT, lead_id BIGINT, date DATE, domain TEXT
    │   │     • campaign_id BIGINT, adgroup_id BIGINT NULL для tp6/tp7
    │   │     • feed_key TEXT, source_type, salon, status, reason, utm_content
    │   │     • kol_vo_zayavok, korr, kval, priezd, prodazhi и остальные статусы
    │   │     • generated_at TIMESTAMP
    │   │
    │   ├── fact_direct_feed_funnel                   ← итог: расходы + воронка по фиду
    │   │     • feed_key3, date, login_key, domain
    │   │     • campaign_id/campaign_name, adgroup_id/adgroup_name
    │   │     • feed_id/feed_name/feed_url/feed_url_key/feed_key, is_tp67
    │   │     • impressions, clicks, total_cost
    │   │     • attributed_leads, kol_vo_zayavok, korr, kval, priezd, prodazhi
    │   │
    │   └── fact_direct_feed_funnel_quality           ← контроль fid-лидов без строгого матчинга
    │         • date DATE, feed_key TEXT
    │         • total_fid_leads, matched_leads, unmatched_fid_leads
    │         • generated_at TIMESTAMP
    │
    │   ── [экспериментальный build.py path — не основной] ─────────────────
    │   ├── direct_feed_key_aliases                   ← ручные алиасы fid_key → feed_key (CREATE IF NOT EXISTS)
    │   │     • fid_key TEXT NOT NULL, feed_key TEXT NOT NULL (PK: fid_key + feed_key)
    │   │     • note TEXT, created_at TIMESTAMPTZ
    │   │
    │   └── direct_feed_lead_attribution              ← промежуточная Attribution-таблица (build.py)
    │         • lead_id INTEGER PK, lead_date DATE
    │         • parsed_fid_key TEXT, match_method TEXT, match_count INT, is_ambiguous BOOLEAN
    │         • domain, campaign_id/name, adgroup_id/name, feed_id/name/key/join_key
    │         • source_type, salon, status, reason, utm_content
    │         • воронка: kol_vo_zayavok…dobro NUMERIC, generated_at TIMESTAMPTZ
    │         INDEX: feed_id+lead_date (WHERE NOT is_ambiguous), match_method, parsed_fid_key
    │
    ├── [UTM-АУДИТ]                                   ← живут между запусками, LOGGED
    │   ├── check_utm                                 ← UTM-аудит групп Директа (шаг 5)
    │   │     • id INTEGER PK
    │   │     • checked_at TIMESTAMP DEFAULT now()
    │   │     • login, CampaignId BIGINT, CampaignName, group_id BIGINT, group_name TEXT
    │   │     • status TEXT
    │   │     • cls TEXT  ← OK | ДРУГОЙ_UTM | НЕТ_UTM
    │   │     • tracking_params, utm_source_type, domain, counter_id TEXT
    │   │     • "специалист" TEXT
    │   │
    │   ├── check_utm_fuck_direct                     ← история групп с неверными UTM (шаг 5)
    │   │     • id INTEGER PK
    │   │     • date DATE NOT NULL           ← MIN(Date) из big_analytics_direct
    │   │     • login, CampaignId BIGINT, CampaignName, group_id BIGINT, group_name TEXT
    │   │     • tracking_params, "специалист", "домен" TEXT
    │   │     • cost NUMERIC               ← сумма расходов (переименовано из total_cost)
    │   │     • utm_source_type TEXT
    │   │     • UNIQUE (login, CampaignId, group_id)
    │   │     ⚠ cost считается через JOIN (не коррелированный подзапрос)
    │   │
    │   └── check_utm_fuck_direct_old                 ← архив старой структуры (до 2026-06)
    │         • id INTEGER PK
    │         • login, CampaignId BIGINT, CampaignName, group_id BIGINT, group_name TEXT
    │         • tracking_params, "домен" TEXT, "специалист" TEXT
    │         • total_cost NUMERIC, data DATE
    │         • checked_at TIMESTAMP, utm_done BOOLEAN DEFAULT false
    │         • id_name_campaing TEXT, first_bad_check DATE, last_bad_check DATE, bad_days INTEGER
    │
    ├── [СТАТУСЫ КАМПАНИЙ]                            ← живут между запусками
    │   └── campaign_status                           ← статусы кампаний Яндекс.Директ (шаг 4)
    │         • CampaignId BIGINT NOT NULL PK
    │         • account_login TEXT          ← (было: login)
    │         • "статус" TEXT               ← кириллица (было: status)
    │         • "специалист" TEXT
    │         • CampaignName TEXT
    │         • manager_login TEXT
    │         • campaign_status TEXT
    │         • payment_model TEXT
    │         ⚠ Структура изменена: login→account_login, campaign_id→CampaignId,
    │           campaign_name→CampaignName, status→статус; убран updated_at;
    │           добавлены специалист, manager_login, campaign_status, payment_model
    │
    ├── [ИСТОРИЯ ДИРЕКТА]                             ← живёт между запусками, LOGGED
    │   └── yandex_direct_history                     ← история изменений аккаунтов Директа (шаг 9)
    │         • id BIGSERIAL PK (sequence: direct_history_id_seq)
    │         • ulogin TEXT                   ← логин кабинета
    │         • datetime TIMESTAMPTZ          ← дата/время события
    │         • user_login TEXT, user_uid BIGINT
    │         • change_source TEXT, event_type TEXT, category TEXT
    │         • campaign_id BIGINT, campaign_name TEXT
    │         • ad_group_id BIGINT, ad_group_name TEXT
    │         • old_value TEXT, new_value TEXT
    │         • raw_event JSONB               ← сырое событие из API
    │         • "директолог" TEXT, domain TEXT, salon TEXT
    │         • loaded_at TIMESTAMPTZ DEFAULT now()
    │         ⚠ Таблица переименована из direct_history → yandex_direct_history;
    │           структура полностью изменена (нет: login, campaign_id simple, field, detected_at)
    │
    ├── [МЕТРИКА]                                     ← живёт между запусками
    │   └── metrika_yandex                            ← счётчики Метрики, синхронизируются фоново
    │
    ├── [МИНУС-СНАПШОТ]                               ← живут постоянно, LOGGED, append-only
    │   ├── yandex_direct_minus_snapshot              ← снапшоты минус-фраз Яндекс.Директ (шаг 14)
    │   │     • id BIGSERIAL PK
    │   │     • "date" DATE                   ← дата прогона (имя-тип: всегда в кавычках)
    │   │     • login TEXT                    ← логин кабинета
    │   │     • campaign_id BIGINT
    │   │     • campaign_name TEXT
    │   │     • campaign_state TEXT
    │   │     • block TEXT                    ← блок кампании: 'tp2'/'tp4'/'прочее' (вычисляется из campaign_name, маркер BLOCK_COL_2026-06-17)
    │   │     • minus_in_campaign INT         ← минусов на кампанию
    │   │     • minus_in_groups INT           ← минусов на группах (сумма)
    │   │     • minus_in_sets INT             ← минусов через наборы (NegativeKeywordSharedSets)
    │   │     • minus_total INT               ← итого = campaign + groups + sets
    │   │     • has_minus BOOLEAN
    │   │     • check_ok BOOLEAN              ← FALSE при сбое запроса групп или наборов
    │   │     • loaded_at TIMESTAMPTZ DEFAULT now()
    │   │     • UNIQUE("date", login, campaign_id) → ON CONFLICT DO NOTHING
    │   │     • INDEX: "date", campaign_id, login
    │   │     • Retention: хранятся последние 30 дней (RETENTION_DAYS=30 в step14.py)
    │   │
    │   └── v_yandex_direct_minus_delta               ← VIEW: LAG-динамика minus_total по снапшотам
    │         • все колонки yandex_direct_minus_snapshot
    │         • minus_total_prev ← значение предыдущего снапшота
    │         • delta            ← minus_total - minus_total_prev
    │         • dynamics TEXT    ← 'первый замер'/'добавили'/'СНЯЛИ'/'без изменений'
    │         Partition: PARTITION BY login, campaign_id ORDER BY "date"
    │
    ├── [ПИКСЕЛЬ]                                     ← живут постоянно, LOGGED
    │   ├── local_pixel_config                        ← конфигурация пикселей (стоимость лидов)
    │   │     • id SERIAL PK
    │   │     • pixel_name TEXT NOT NULL
    │   │     • salon TEXT, project TEXT
    │   │     • cost_per_lead NUMERIC, cost_total NUMERIC
    │   │
    │   ├── local_pixel_price_history                 ← история изменений цен пикселей
    │   │     • id SERIAL PK
    │   │     • pixel_name TEXT NOT NULL
    │   │     • salon TEXT, project TEXT
    │   │     • cost_per_lead NUMERIC, cost_total NUMERIC
    │   │     • valid_from DATE NOT NULL, valid_to DATE
    │   │     • changed_at TIMESTAMPTZ DEFAULT now()
    │   │     • changed_by TEXT, note TEXT
    │   │
    │   ├── pixel_leads                               ← лиды с атрибуцией пикселей (step11)
    │   │     • id BIGINT, created_date DATE
    │   │     • status, reason, source_type, salon, pixel_name, domain TEXT
    │   │     • cost_total NUMERIC, cost_per_lead NUMERIC
    │   │
    │   ├── pixel_leads_check                         ← контрольная копия pixel_leads для сверки
    │   │     структура идентична pixel_leads
    │   │
    │   ├── pixel_score                               ← атрибутированные оценки по пикселям (step11)
    │   │     • month DATE NOT NULL, "салон" TEXT NOT NULL
    │   │     • domain TEXT NOT NULL, "источник" TEXT, "направление" TEXT
    │   │     • CampaignId BIGINT NOT NULL, CampaignName TEXT
    │   │     • kol_vo_zayavok, korr, kval, priezd, prodazhi NUMERIC
    │   │     • cpl_score NUMERIC DEFAULT 1.0
    │   │     • cpl_avg_квал/визит/продажа, cpl_кам_квал/визит/продажа NUMERIC
    │   │     • score_квал/визит/продажа, w_квал/визит/продажа NUMERIC
    │   │     • status_квал/визит/продажа TEXT
    │   │     • расход, weight NUMERIC
    │   │     • pixel_kol_vo_домена/кампании NUMERIC
    │   │     • pixel_квал_домена, attr_pixel_квал_кампании NUMERIC
    │   │     • pixel_приезд_домена, attr_pixel_приезд_кампании NUMERIC
    │   │     • pixel_продажи_домена, attr_pixel_продажи_кампании NUMERIC
    │   │
    │   └── posev_cost_backfill                       ← подбивка расходов посевов (бэкфилл)
    │         • id SERIAL PK
    │         • category TEXT NOT NULL, domain TEXT, "Дата" DATE
    │         • kanal, salon, gorod, region, postavshik TEXT
    │         • zayavok, prodazh, priezdov NUMERIC, reason TEXT NOT NULL
    │         • backfilled_cost NUMERIC, fixed BOOLEAN NOT NULL DEFAULT false
    │         • comment TEXT, created_at TIMESTAMPTZ DEFAULT now()
    │         • utm_campaign, utm_source, utm_medium, utm_content, campaign_name TEXT
    │         • matched_leads_cnt INTEGER, lead_phones TEXT, lead_statuses TEXT, match_basis TEXT
    │
    ├── [АНАЛИТИКА АРП]                               ← живут между запусками, LOGGED
    │   ├── analytics_report_placement                ← данные по площадкам РСЯ/поиска (step ARP)
    │   │     • row_hash TEXT
    │   │     • date DATE, domain TEXT, "логин" TEXT
    │   │     • ad_network_type, placement, placement_key TEXT
    │   │     • clicks BIGINT, cost NUMERIC
    │   │     • "Все формы", "CRM: Заказ создан", "CRM: Заказ оплачен" BIGINT
    │   │     • "CRM: Спам заказ", "CRM: Заказ отменен" BIGINT
    │   │     • campaign_id BIGINT, campaign_name, campaign_code TEXT
    │   │     • tp, cpc_cpa, site_quiz, ad_group_id BIGINT TEXT
    │   │     • key, key2, "директолог" TEXT
    │   │     • "город", "регион", "салон", "шаблон", "тип_сайта", "статус", "направление" TEXT
    │   │     • updated_at TIMESTAMP
    │   │     • "Название crm", "тип_заявки" TEXT
    │   │     • kol_vo_zayavok, korr, kval, priezd, prodazhi BIGINT
    │   │     • nekorr, ne_otvechaet, filtr, nedozvon, priedet BIGINT
    │   │     • dohod_do_kredita, dobro BIGINT
    │   │     • "номер кампании|название кампании" TEXT
    │   │
    │   └── analytics_proverka_big_analytics          ← сверка BI vs CSD по расходу/воронке
    │         • crm_name TEXT (PK-like)
    │         • month DATE
    │         • csd_spend, bi_spend, bi_direct_spend, bi_tp8_spend NUMERIC
    │         • bi_leads, bi_direct_leads, bi_tp8_leads BIGINT
    │         • bi_korr, bi_visits, bi_sales BIGINT
    │         • diff_spend NUMERIC, bi_domain_count INTEGER
    │         • generated_at TIMESTAMPTZ DEFAULT now()
    │
    ├── [ЗВЁЗДНАЯ СХЕМА]                              ← public.* (схема star упразднена 2026-06-10)
    │   │   Источник: build_star.py → публикуется в public схеме
    │   │
    │   ├── fact_big_analytics                        ← ОСНОВНОЙ ФАКТ (50+ колонок)
    │   │     • CampaignId BIGINT, AdGroupId BIGINT, RlAdjustmentId BIGINT
    │   │     • Date DATE, domain TEXT
    │   │     • total_cost NUMERIC, Clicks INTEGER, Impressions INTEGER
    │   │     • kol_vo_zayavok, korr, kval, priezd, prodazhi NUMERIC
    │   │     • nekorr, ne_otvechaet, nedozvon, filtr, priedet INTEGER
    │   │     • priezd_arrival_date BIGINT, prodazhi_arrival_date BIGINT
    │   │     • dohod_do_kredita BIGINT, dobro BIGINT
    │   │     • "План заявки", "План приезда" INTEGER
    │   │     • атрибуция, _source_table, tp, источник, AdNetworkType TEXT
    │   │     • "аккаунт|сайт", campaign_code, поставщик, Device, fid TEXT
    │   │     • cpc_cpa, направление, site_quiz, "марки авто" TEXT
    │   │     • специалист, тип_сайта, статус, салон, шаблон TEXT
    │   │     • id_салона, город, регион, проджект, менеджер TEXT
    │   │     • "Название crm", тип_заявки, manager_login TEXT
    │   │
    │   ├── fact_adformat_spend                       ← расход по форматам объявлений
    │   │     • row_hash TEXT, date DATE, campaign_id BIGINT, campaign_name TEXT
    │   │     • ad_group_id BIGINT, ad_network_type, ad_format TEXT
    │   │     • impressions, clicks, cost NUMERIC
    │   │     • "Все формы", "CRM: Заказ создан", "CRM: Заказ оплачен" NUMERIC
    │   │     • "CRM: Спам заказ", "CRM: Заказ отменен" NUMERIC
    │   │     • domain, "логин", "директолог", "город", "регион", "салон" TEXT
    │   │     • "шаблон", "тип_сайта", "статус", "направление" TEXT
    │   │     • project_manager, client_id, campaign_code, tp TEXT
    │   │     • cpc_cpa, site_quiz, adgroup_code, adgroup_brand TEXT
    │   │     • updated_at TIMESTAMPTZ
    │   │
    │   ├── fact_region_spend                         ← расход по гео-регионам
    │   │     • row_hash TEXT, date DATE, campaign_id BIGINT, campaign_name TEXT
    │   │     • ad_group_id BIGINT, ad_network_type TEXT
    │   │     • id_location BIGINT, location, Область, GeoRegionType TEXT
    │   │     • distance_km INTEGER, distance_km_agreg INTEGER
    │   │     • impressions, clicks, cost NUMERIC
    │   │     • "Все формы", "CRM: Заказ создан/оплачен/спам/отменен" NUMERIC
    │   │     • domain, "логин", "директолог", "город", "регион", "салон" TEXT
    │   │     • "шаблон", "тип_сайта", "статус", "направление" TEXT
    │   │     • project_manager, client_id, campaign_code, tp, cpc_cpa, site_quiz TEXT
    │   │     • updated_at TIMESTAMPTZ
    │   │
    │   ├── fact_region_zayavki                       ← заявки по гео-регионам
    │   │     • row_hash TEXT, created_date DATE, campaign_id BIGINT, campaign_name TEXT
    │   │     • id_location BIGINT, location, Область, GeoRegionType TEXT
    │   │     • distance_km INTEGER, distance_km_agreg INTEGER
    │   │     • салон TEXT, domain_id BIGINT
    │   │     • kol_vo_zayavok, korr, kval, priezd, prodazhi NUMERIC
    │   │     • nekorr, ne_otvechaet, filtr, nedozvon, priedet NUMERIC
    │   │     • dohod_do_kredita BIGINT, dobro BIGINT
    │   │     • updated_at TIMESTAMPTZ
    │   │
    │   ├── fact_criterion_spend                      ← расход по критериям/ключевикам
    │   │     • row_hash TEXT, date DATE, campaign_id BIGINT, campaign_name TEXT
    │   │     • ad_group_id BIGINT, ad_network_type TEXT
    │   │     • criterion_id BIGINT, criterion, criterion_raw, criterion_type TEXT
    │   │     • impressions, clicks, cost NUMERIC
    │   │     • "Все формы", "CRM: Заказ создан/оплачен/спам/отменен" NUMERIC
    │   │     • domain, "логин", "директолог", "город", "регион", "салон" TEXT
    │   │     • "шаблон", "тип_сайта", "статус", "направление" TEXT
    │   │     • project_manager, client_id, campaign_code, tp, cpc_cpa, site_quiz TEXT
    │   │     • updated_at TIMESTAMPTZ
    │   │
    │   ├── fact_criterion_zayavki                    ← заявки по критериям/ключевикам
    │   │     • row_hash TEXT, created_date DATE, campaign_id BIGINT, campaign_name TEXT
    │   │     • criterion, criterion_type, criterion_raw TEXT
    │   │     • салон TEXT, domain_id BIGINT
    │   │     • kol_vo_zayavok, korr, kval, priezd, prodazhi NUMERIC
    │   │     • nekorr, ne_otvechaet, filtr, nedozvon, priedet NUMERIC
    │   │     • dohod_do_kredita BIGINT, dobro BIGINT
    │   │     • updated_at TIMESTAMPTZ
    │   │
    │   ├── fact_vk_ads                               ← VK Ads: сегмент×оффер×объявление воронка
    │   │     • date DATE, account_id BIGINT, салон TEXT
    │   │     • ad_plan_id/ad_group_id/banner_id BIGINT (+ *_name TEXT)
    │   │     • атрибуция TEXT ('По дате заявки'/'По дате визита')
    │   │     • shows, clicks BIGINT, spent NUMERIC(14,2)  ← реклама, ТОЛЬКО заявка-ось
    │   │     • заявки, записи, квал, визиты, продажи BIGINT ← воронка VK-лидов
    │   │     • build_star.py::build_vk_ads_fact; только платный VK Авто; посевы VK НЕ входят
    │   │
    │   ├── Dim_AdGroup                               ← измерение: группы объявлений
    │   │     • AdGroupId BIGINT PK
    │   │     • AdGroupName TEXT, adgroup_code TEXT
    │   │     • "номер группы|название группы" TEXT
    │   │     • ag_part1…ag_part7 TEXT, ag_part1_name TEXT
    │   │     • неверный_кодер_new TEXT
    │   │     • parent_CampaignId BIGINT
    │   │
    │   ├── Dim_Adjustment                            ← измерение: корректировки ставок
    │   │     • RlAdjustmentId BIGINT PK
    │   │     • RlAdjustmentId_total TEXT
    │   │
    │   ├── Dim_Campaign                              ← измерение: кампании
    │   │     • CampaignId BIGINT PK
    │   │     • CampaignName TEXT, account_login TEXT
    │   │     • статус_кампании TEXT, специалист TEXT
    │   │     • manager_login TEXT, campaign_status TEXT, payment_model TEXT
    │   │     • "номер кампании|название кампании" TEXT
    │   │
    │   ├── Dim_Date                                  ← измерение: даты
    │   │     • Date DATE PK
    │   │     • week_start DATE, "День недели" TEXT
    │   │     • year SMALLINT, month SMALLINT
    │   │     • year_month TEXT, day SMALLINT
    │   │
    │   ├── Dim_Distance                              ← измерение: расстояния (для ARP)
    │   │     • distance_km_agreg INTEGER
    │   │     • distance_label TEXT, distance_sort INTEGER
    │   │
    │   └── Dim_Site                                  ← измерение: сайты/домены
    │         • domain TEXT PK
    │
    ├── [СПРАВОЧНИК СПЕЦИАЛИСТОВ]
    │   └── specialists                               ← справочник специалистов
    │         • id SERIAL PK
    │         • name TEXT
    │
    ├── [ИЗМЕНЕНИЯ ЦЕН / ТЕКСТОВ]
    │   ├── izmeneniye_tsen_text_in_direct            ← изменения цен и текстов в Директе
    │   │     • id SERIAL PK
    │   │     • account CHARACTER VARYING, campaign_id BIGINT, campaign_name TEXT
    │   │     • adgroup_id BIGINT, adgroup_name TEXT, ad_id BIGINT
    │   │     • title, title2, text, url TEXT
    │   │     • price_qualifier CHARACTER VARYING
    │   │     • price NUMERIC, old_price NUMERIC
    │   │     • ad_type, status, state CHARACTER VARYING
    │   │     • created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    │   │
    │   └── izmeneniye_tsen_text_in_fid               ← изменения цен в фиде
    │         • id SERIAL PK
    │         • url TEXT, price NUMERIC, oldprice NUMERIC
    │         • created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    │
    ├── [ЯНДЕКС.ДИРЕКТ — ВСПОМОГАТЕЛЬНЫЕ]
    │   ├── yandex_direct_history                     ← см. блок [ИСТОРИЯ ДИРЕКТА] выше
    │   │
    │   ├── yandex_direct_404_errors                  ← 404-ошибки на страницах Директа (Метрика)
    │   │     • id SERIAL PK
    │   │     • "№ счетчика", counter_name, site TEXT
    │   │     • "специалист", url, page_title TEXT
    │   │     • utm_campaign, "№ кампании", utm_content, "№ группы" TEXT
    │   │     • visit_date DATE, week_start DATE
    │   │     • detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    │   │
    │   ├── yandex_direct_account_reviews             ← аккаунты Директа для проверок
    │   │     • id SERIAL PK (sequence: direct_account_reviews_id_seq)
    │   │     • "город", "салон", "аккаунт", "сайт" TEXT
    │   │     • "агентский аккаунт" TEXT
    │   │
    │   ├── yandex_direct_check_block_cookie          ← статус блокировки куки аккаунтов
    │   │     • login TEXT PK
    │   │     • is_blocked BOOLEAN
    │   │     • cookie_account TEXT, checked_at TIMESTAMP
    │   │
    │   ├── yandex_direct_checking_report             ← сводка расходов по аккаунтам/месяцам
    │   │     • id SERIAL PK
    │   │     • domain TEXT, account_login TEXT NOT NULL, manager_login TEXT
    │   │     • month DATE NOT NULL, cost NUMERIC DEFAULT 0
    │   │     • updated_at TIMESTAMPTZ DEFAULT now()
    │   │
    │   ├── yandex_direct_commission_rates            ← ставки комиссий Яндекс.Директ
    │   │     • id SERIAL PK
    │   │     • platform TEXT NOT NULL, ad_network_type TEXT NOT NULL
    │   │     • rate_percent NUMERIC NOT NULL
    │   │     • valid_from DATE DEFAULT '2026-01-01', valid_to DATE
    │   │     • product_name TEXT, comment TEXT
    │   │     • updated_at TIMESTAMP DEFAULT now()
    │   │
    │   ├── yandex_direct_cookie_analytics_website_pages ← аналитика страниц через куки (старый шаг)
    │   │     • id BIGSERIAL PK
    │   │     • login_key TEXT NOT NULL, domain TEXT
    │   │     • banner_href TEXT NOT NULL
    │   │     • date_from DATE NOT NULL, date_to DATE NOT NULL
    │   │     • sum NUMERIC, clicks BIGINT, agoalnum BIGINT
    │   │     • aconv NUMERIC, agoalcost NUMERIC
    │   │     • goal_all_forms BIGINT, goal_crm_order_created BIGINT, goal_crm_order_paid BIGINT
    │   │     • final_url TEXT, directologist TEXT, template TEXT
    │   │     • salon TEXT, city TEXT, region TEXT, site_type TEXT, page_type TEXT
    │   │     • loaded_at TIMESTAMP DEFAULT now()
    │   │
    │   ├── yandex_direct_korrektirovki               ← корректировки ставок по аккаунтам
    │   │     • id BIGSERIAL PK
    │   │     • ulogin TEXT, campaign_id BIGINT, campaign_name TEXT
    │   │     • ad_group_id BIGINT, level TEXT
    │   │     • modifier_id BIGINT, enabled TEXT, modifier_type TEXT
    │   │     • modifier_name TEXT, bid_percent TEXT, korrektirovki_bid TEXT
    │   │     • audience_id BIGINT
    │   │     • "специалист" TEXT, campaign_status TEXT, status TEXT
    │   │     • loaded_at TIMESTAMPTZ DEFAULT now()
    │   │
    │   ├── yandex_direct_reports_reviews             ← сырые отчёты по показам/кликам (reviews)
    │   │     • id SERIAL PK
    │   │     • login TEXT, Date DATE
    │   │     • CampaignId BIGINT, CampaignName TEXT
    │   │     • AdGroupId BIGINT, AdGroupName TEXT
    │   │     • AdNetworkType TEXT, Device TEXT
    │   │     • Impressions BIGINT, Clicks BIGINT, Cost NUMERIC
    │   │     • RlAdjustmentId BIGINT
    │   │
    │   ├── yandex_direct_return_commission_logins    ← логины для возврата комиссии
    │   │     • id SERIAL PK
    │   │     • manager_login TEXT NOT NULL, account_login TEXT NOT NULL
    │   │     • user_login TEXT
    │   │
    │   └── yandex_direct_return_commission_report    ← отчёт возврата комиссии
    │         • id SERIAL PK
    │         • client_login TEXT, date DATE, ad_network_type TEXT, slot TEXT
    │         • campaign_type TEXT, ad_type TEXT
    │         • cost NUMERIC, cost_with_vat NUMERIC
    │         • manager_login TEXT, user_login TEXT
    │         • rate NUMERIC, commission NUMERIC
    │
    ├── [СЛУЖЕБНЫЕ]                                   ← живут постоянно
    │   ├── data_quality_log                          ← история запусков: время, кол-во строк, ошибки
    │   │
    │   ├── data_pipeline_log                         ← снапшоты воронки по запускам (дрейф)
    │   │     • id SERIAL PK, run_id TEXT, recorded_at TIMESTAMPTZ DEFAULT now()
    │   │     • month DATE, cost NUMERIC, obrashenia BIGINT, cpl_obr NUMERIC
    │   │     • zayavki BIGINT, cpl_zayavki NUMERIC
    │   │     • kval BIGINT, cpl_kval NUMERIC
    │   │     • priezd BIGINT, cpl_priezd NUMERIC
    │   │     • prodazhi BIGINT, cpl_prodazhi NUMERIC
    │   │
    │   └── data_funnel_drift_log                     ← дрейф воронки между запусками
    │         • id SERIAL PK, run_id TEXT NOT NULL
    │         • recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
    │         • month DATE NOT NULL, "источник" TEXT NOT NULL
    │         • cost NUMERIC, zayavki BIGINT, vizity BIGINT, prodazhi BIGINT
    │
    └── [VIEWS]
        ├── arp_fact                                  ← VIEW над analytics_report_placement (подмножество колонок)
        │     • CampaignId BIGINT, AdGroupId BIGINT
        │     • cost NUMERIC, clicks BIGINT
        │     • kol_vo_zayavok, korr, kval, priezd, prodazhi INTEGER
        │     • nekorr, ne_otvechaet, nedozvon, filtr, priedet INTEGER
        │     • dohod_do_kredita, dobro INTEGER
        │     • placement, ad_network_type TEXT, Date DATE, domain TEXT
        │     • тип_заявки TEXT, "CRM: Заказ создан/оплачен/спам/отменен" BIGINT
        │     • date DATE, placement_key, tp, updated_at TEXT
        │     • "Все формы" BIGINT
        │     • Специалист, домен, логин TEXT
        │     • "номер кампании|название кампании" TEXT
        │     • салон, тип_сайта TEXT
        │
        ├── v_funnel_change                           ← VIEW: изменение воронки между прогонами
        │     • curr_run_id, prev_run_id TEXT
        │     • recorded_at TIMESTAMPTZ, month DATE, "источник" TEXT
        │     • cost/zayavki/vizity/prodazhi _curr/_prev (NUMERIC/BIGINT)
        │     • delta_cost NUMERIC, delta_zayavki/vizity/prodazhi BIGINT
        │     • cpl_zayavki/cost_per_visit/cost_per_sale _curr/_prev NUMERIC
        │     • changed TEXT
        │     Источник: data_funnel_drift_log (LAG по run_id)
        │
        └── v_yandex_direct_minus_delta               ← VIEW: LAG-динамика снапшотов минус-фраз
              см. блок [МИНУС-СНАПШОТ] выше
```

---

## Жизненный цикл таблиц

| Группа | Создаётся | Обновляется | Удаляется |
|--------|-----------|-------------|-----------|
| `src.*` (FDW) | DB-admin | DB-admin | никогда (мы не трогаем) |
| `local_*` | при первом запуске | каждый запуск (UPSERT / TRUNCATE+INSERT) | никогда |
| Посевы (`gsheets_*`, `crop_targeting_*`) | при первом запуске crop-пайплайна | каждый запуск crop-пайплайна | никогда |
| `raw_*` | каждый запуск (DROP+CREATE UNLOGGED) | — | следующий запуск |
| `big_analytics_*` | каждый запуск (DROP+CREATE UNLOGGED) | SET LOGGED после сборки | следующий запуск |
| `big_analytics_unified`, `_arrival`, `_pixel_score` | каждый запуск | — | следующий запуск |
| `campaign_status`, `check_utm*`, `yandex_direct_history` | при первом запуске | каждый запуск (UPSERT/INSERT) | никогда |
| `pixel_*`, `posev_cost_backfill` | при первом запуске | пикселевые прогоны | никогда |
| Звёздная схема (`fact_*`, `Dim_*`) | каждый запуск build_star.py | — | следующий build_star |
| `data_*_log`, `data_quality_log` | при первом запуске | INSERT после каждого запуска | никогда |
| Views (`arp_fact`, `v_*`) | один раз (DDL) | — | никогда (пересоздаются только при изменении DDL) |

---

## Telegram — отправка уведомлений

Все сообщения (отчёты, ошибки, статистика) — **строго в Telegram**.

### Архитектура: прямой Bot API

Victory сервер отправляет в Telegram **напрямую** через `api.telegram.org`:

```
Victory Server (103.88.240.90)
        │
        │  POST https://api.telegram.org/bot{TOKEN}/sendMessage
        │  Body: {"chat_id": "336635373", "text": "...", "parse_mode": "HTML"}
        │
        ▼
api.telegram.org → чат 336635373
```

### Функция отправки

```python
# config/tokens.py — загружает из .secret/.env через loader.py:
# _TG = load_auto_bi_analytics_telegram()
# TELEGRAM_BOT_TOKEN = _TG['bot_token']  # TG_AUTO_BI_ANALYTICS_BOT
# TELEGRAM_CHAT_ID   = _TG['chat_id']    # TG_AUTO_BI_ANALYTICS_CHAT, фолбэк на TG_PERSONAL_CHAT если пусто

# Функция отправки (шаги 6 и 7):
def _tg(text: str) -> None:
    try:
        requests.post(
            f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage',
            json={'chat_id': TELEGRAM_CHAT_ID, 'text': text, 'parse_mode': 'HTML'},
            timeout=30,
        )
    except Exception as e:
        logger.warning('Telegram недоступен: %s', e)
```

---

## Технические ограничения API

### Яндекс.Директ

Документация: https://yandex.ru/dev/direct/doc/ru/restrictions

- При настройке таймаутов и потоков — **запас 30–50%** от максимального лимита
- Не использовать предельные значения: если лимит 5 потоков — ставить не более 3

### Яндекс.Метрика

Документация: https://yandex.ru/dev/metrika/

- Аналогично: запас 30–50% по таймаутам и количеству запросов
- Не упираться в лимиты — всегда оставлять буфер
