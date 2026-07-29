"""
build_star.py — ШАГ 4 рефакторинга big_analytics_v5 под звезду (star schema,
консолидирована в public 2026-06-10 — отдельной схемы star больше нет).

СОЗДАЁТ объекты звезды (fact_big_analytics, Dim_*, arp_fact) в схеме `public`
РЯДОМ со старыми широкими таблицами. НИЧЕГО не дропает,
не трогает big_analytics_full / big_analytics_full_arrival / big_analytics_unified /
analytics_report_placement (только читает их).

Источник истины для dim — ФИНАЛЬНЫЙ big_analytics_unified (значения dim уже прошли
normalize_salons / backfill / corrections — это ровно то, что показывает отчёт), плюс
добивка ключей из сырых справочников (campaign_status, local_gsheet_sites, naming).

Лёгкий факт public.fact_big_analytics = колоночная проекция big_analytics_unified
(full ∪ arrival + атрибуция): ТОЛЬКО ключи + маркеры + меры. Те же строки, та же
атрибуция (По дате заявки / По дате визита), MIRROR+UNION сохранён 1:1 → нулевая потеря.

Запуск (на Victory, через nohup):
    nohup ~/venv/bin/python3 star_refactor/build_star.py > /tmp/build_star.log 2>&1 &
"""
import sys, time, logging
from pathlib import Path

# Портабл-резолвер путей (как в остальных файлах проекта): идём вверх от файла,
# ищем корень проекта (где лежит config/settings.py) и каталог .secret (loader.py).
# Фолбэк на исторические Victory-пути, чтобы не сломать запуск subprocess'ом.
def _resolve_paths():
    here = Path(__file__).resolve()
    proj_root = None
    secret_dir = None
    for parent in here.parents:
        if proj_root is None and (parent / 'config' / 'settings.py').exists():
            proj_root = parent
        cand = parent / '.secret'
        if secret_dir is None and (cand / 'loader.py').exists():
            secret_dir = cand
        if proj_root and secret_dir:
            break
    return proj_root, secret_dir

_PROJ_ROOT, _SECRET_DIR = _resolve_paths()
# Фолбэк на прежние Victory-пути, если walk-up не нашёл (напр. нестандартная раскладка).
for _p in (str(_SECRET_DIR) if _SECRET_DIR else '/home/semen_vi/.secret',
           str(_PROJ_ROOT)   if _PROJ_ROOT   else '/home/semen_vi/big_analytics_v5'):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)

import psycopg2
from loader import load_db
# VK_ADS_FACT_2026-07-10: воронка VK-лидов через тот же маппинг статусов, что и витрина.
from config.status_sql import build_leads_agg_sql

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('build_star')

DB = load_db('victory'); DB['database'] = 'ad_analytics_bi'
WORK_MEM = '1999MB'

# Каноничная нижняя граница периода проекта (config/settings.py: DATE_FROM='2026-01-01').
# Импортируем централизованно, чтобы фильтр звезды совпадал с инвариантом витрины
# ("Date" >= '2026-01-01'). Fallback на литерал, если settings недоступен на Victory.
try:
    from config.settings import DATE_FROM as _DATE_FROM
except Exception:
    _DATE_FROM = '2026-01-01'
DATE_FROM = _DATE_FROM

# ⚠️ E2 ОТКАТАН (Этап 1, 2026-06-07): источник факта остаётся big_analytics_unified.
# Замысел E2 — читать звезду напрямую из big_analytics_full ∪ big_analytics_full_arrival
# (чтобы дропнуть unified, ~5.4 ГБ). НЕ сделано: build_unified.py строит unified
# динамически (список колонок BAF из information_schema + проверка зеркала BFA),
# воспроизвести его статическим CTE байт-в-байт без риска нельзя. Правило проекта
# «ЗВЕЗДА ВАЖНЕЕ 5 ГБ» → не рискуем оракулом, unified остаётся источником.
T_UNI = 'public.big_analytics_unified'
T_ARP = 'public.analytics_report_placement'

# ─────────────────────────────────────────────────────────────────────────────
# СКОУП ЗВЕЗДЫ: только ниша «Авто» (бизнес-правило, июнь 2026).
# В звезду попадают ТОЛЬКО домены, у которых local_gsheet_sites.niche = 'Авто'.
# Остальные ниши (Медицина, Недвижимость, Другое, …), а также домены БЕЗ записи в
# local_gsheet_sites (niche неизвестен) и строки с domain IS NULL — ИСКЛЮЧАЮТСЯ из
# фактов и Dim_Site. Dim_Campaign/Dim_AdGroup/Dim_Date остаются полными (лишние ключи
# без фактов не искажают метрики). Применяется в build_fact / build_arp_fact /
# build_dim_site, чтобы пересборка воспроизводила Авто-срез без ручной подрезки БД.
AUTO_DOMAIN_SET = (
    "SELECT DISTINCT lower(trim(domain)) FROM public.local_gsheet_sites "
    "WHERE niche = 'Авто' AND domain IS NOT NULL AND trim(domain) <> ''"
)
# Предикат «оставить строку»: domain непустой И входит в Авто-множество.
# (domain IS NULL → исключается, т.к. niche неизвестен.)
FACT_AUTO_WHERE = f"domain IS NOT NULL AND domain IN ({AUTO_DOMAIN_SET})"

# ⚠️ СКОУП ФАКТА (2026-06-07): помимо Авто-доменов в факт ВОЗВРАЩЕНЫ строки
# тип_заявки IN ('Отзывы','Звонки) — бизнес-решение пользователя. niche='Авто'
# выкидывал отзывы (3706) и часть звонков (domain вне Авто-множества / NULL).
# Эти строки нужны слайсеру тип_заявки и звонкам. Применяется ТОЛЬКО к факту
# (build_fact), НЕ к Dim_Site/arp_fact — Dim_Site остаётся чистым Авто-скоупом
# (домены отзывов/звонков не должны попадать в сторону «1» связи fact→Dim_Site).
# CAPITALIZE_FIX_2026-07-10: 'отзывы'→'Отзывы', 'звонки'→'Звонки' (step3/step6/step11 updated)
FACT_SCOPE_WHERE = (
    f"({FACT_AUTO_WHERE}) OR \"тип_заявки\" IN ('Отзывы', 'Звонки')"
)


# ─────────────────────────────────────────────────────────────────────────────
# ANTI-REGRESSION GUARD (2026-06-07)
# Контракт колонок Power BI. Защищает от молчаливого падения колонки, на которую
# ссылается модель: тонкий отчёт «Большая аналитика_user» (live-connect, без своей
# модели) резолвит поля как big_analytics_full[X]; если миграция/правда build_fact
# уберёт колонку — Service падает «Поля, которые нужно исправить» на каждой вкладке.
# Поэтому build_star в конце сверяет реальные колонки public.fact_big_analytics / public.arp_fact с
# ожидаемым списком sourceColumn модели и громко падает ДО завершения сборки.
#
# ⚠️ СИНХРОНИЗИРОВАТЬ ПРИ ИЗМЕНЕНИИ МОДЕЛИ big_analytics_full / analytics_report_placement.
# Источник истины — sourceColumn из TMDL admin-модели:
#   .../Большая аналитика_v00.SemanticModel/definition/tables/big_analytics_full.tmdl
#   .../analytics_report_placement.tmdl
# (На Victory этих файлов НЕТ — build_star крутится здесь, файл недоступен — поэтому
#  список захардкожен как актуальный контракт, а не читается с диска.)

# M-партиция модели big_analytics_full переименовывает 2 колонки звезды
# (Table.RenameColumns): модель ждёт «Обращения»/«домен», в public.fact_big_analytics они называются
# kol_vo_zayavok/domain. Здесь маппинг modelSourceColumn -> реальная колонка public.fact_big_analytics.
BAF_MODEL_RENAMES = {
    'Обращения': 'kol_vo_zayavok',
    'домен':     'domain',
}

# ОЖИДАЕМЫЕ sourceColumn модельной таблицы big_analytics_full (45 шт., из TMDL).
# ⚠️ RlAdjustmentId_total СНЯТ из контракта (2026-06-10): он БОЛЬШЕ НЕ sourceColumn
# факта — приходит в PBI JOIN-ом факта с Dim_Adjustment по RlAdjustmentId в M-партиции
# (имя big_analytics_full[RlAdjustmentId_total] сохранено). Оставить здесь = ложное
# падение ANTI-REGRESSION (колонка физически снята с факта). Ключ RlAdjustmentId — в списке.
# MonthKey/меры — НЕ в списке (calculated column / DAX, без sourceColumn).
# RemoveColumns партиции (Impressions, *_arrival_date, _source_table) НЕ в контракте —
# модель их не мапит, build_fact может их держать/убирать свободно.
# ⚠️ 18 dim-колонок СНЯТЫ из контракта (вариант A, 2026-06-10): CampaignName,
# AdGroupName, account_login, adgroup_code, ag_part1..ag_part7, campaign_status,
# payment_model, неверный_кодер_new, week_start, «День недели»,
# «номер группы | название группы», «номер кампании | название кампании».
# Причина: они БОЛЬШЕ НЕ sourceColumn факта — в M-партиции big_analytics_full.tmdl
# они приходят через Table.NestedJoin факта с Dim_Campaign/Dim_AdGroup/Dim_Date
# (имена big_analytics_full[X] сохранены). Guard сверяет контракт с реальными
# колонками public.fact_big_analytics: оставить их здесь = ложное падение ANTI-REGRESSION
# (колонки физически сняты с факта Правкой 1a).
BAF_REQUIRED_COLUMNS = [
    'Date', 'CampaignId', 'AdNetworkType', 'Device', 'total_cost', 'campaign_code',
    'tp', 'cpc_cpa', 'Обращения', 'korr', 'kval', 'priezd', 'prodazhi', 'nekorr',
    'ne_otvechaet', 'filtr', 'nedozvon', 'RlAdjustmentId',
    'fid', 'Clicks', 'источник', 'План заявки', 'План приезда', 'аккаунт|сайт',
    'поставщик', 'домен', 'priedet', 'dohod_do_kredita', 'dobro', 'атрибуция',
    'AdGroupId', 'направление', 'site_quiz', 'марки авто', 'специалист', 'тип_сайта',
    'статус', 'салон', 'шаблон', 'id_салона', 'город', 'регион', 'проджект',
    'менеджер', 'Название crm', 'тип_заявки', 'manager_login',
]

# ОЖИДАЕМЫЕ sourceColumn модельной таблицы analytics_report_placement (25 шт., из TMDL).
# Её M-партиция читает public.arp_fact БЕЗ переименований (только TransformColumnTypes
# на "date") → имена sourceColumn должны существовать в public.arp_fact дословно
# (регистр/спецсимволы важны). «Специалист» в public.arp_fact = алиас от "директолог".
ARP_REQUIRED_COLUMNS = [
    'date', 'домен', 'логин', 'ad_network_type', 'placement', 'placement_key',
    'cost', 'Все формы', 'CRM: Заказ создан', 'CRM: Заказ оплачен', 'CRM: Спам заказ',
    'CRM: Заказ отменен', 'tp', 'Специалист', 'салон', 'тип_сайта', 'updated_at',
    'kol_vo_zayavok', 'korr', 'kval', 'priezd', 'prodazhi', 'nekorr', 'priedet',
    'номер кампании|название кампании',
]

# ОЖИДАЕМЫЕ sourceColumn финальной PBI-view public.pbi_big_analytics_full.
# Это тот же контракт, который сейчас Power Query собирает из fact_big_analytics +
# Dim_Campaign/Dim_AdGroup/Dim_Date/Dim_Adjustment. View нужна, чтобы Service читал
# один готовый SQL-объект, а не повторял JOIN-цепочку в Mashup engine.
PBI_BAF_VIEW_REQUIRED_COLUMNS = BAF_REQUIRED_COLUMNS + [
    'RlAdjustmentId_total',
    'CampaignName',
    'AdGroupName',
    'account_login',
    'adgroup_code',
    'ag_part1',
    'ag_part2',
    'ag_part3',
    'ag_part4',
    'ag_part5',
    'ag_part6',
    'ag_part7',
    'campaign_status',
    'payment_model',
    'неверный_кодер_new',
    'week_start',
    'День недели',
    'номер группы | название группы',
    'номер кампании | название кампании',
]


def _actual_columns(cur, schema, table):
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s", (schema, table))
    return {r[0] for r in cur.fetchall()}


def verify_model_coverage(cur):
    """ANTI-REGRESSION: реальные колонки public.fact_big_analytics / public.arp_fact обязаны покрывать
    sourceColumn моделей big_analytics_full и analytics_report_placement. Падает громко,
    если миграция/правка уронила колонку, нужную Power BI (тонкий user-отчёт ломается)."""
    log.info('[8] verify_model_coverage — anti-regression guard')

    # big_analytics_full: применяем M-ренеймы партиции (модельное имя -> имя в star.fact).
    fact_cols = _actual_columns(cur, 'public', 'fact_big_analytics')
    baf_missing = []
    for model_col in BAF_REQUIRED_COLUMNS:
        real_col = BAF_MODEL_RENAMES.get(model_col, model_col)
        if real_col not in fact_cols:
            baf_missing.append(f'{model_col}->{real_col}' if real_col != model_col else model_col)
    if baf_missing:
        raise RuntimeError(
            'ANTI-REGRESSION: public.fact_big_analytics не содержит колонки, нужные модели '
            f'big_analytics_full: {baf_missing}')

    # analytics_report_placement: партиция читает public.arp_fact без ренеймов → дословно.
    arp_cols = _actual_columns(cur, 'public', 'arp_fact')
    arp_missing = [c for c in ARP_REQUIRED_COLUMNS if c not in arp_cols]
    if arp_missing:
        raise RuntimeError(
            'ANTI-REGRESSION: public.arp_fact не содержит колонки, нужные модели '
            f'analytics_report_placement: {arp_missing}')

    # big_analytics_full PBI-view: партиция модели читает view напрямую без M-join.
    pbi_baf_cols = _actual_columns(cur, 'public', 'pbi_big_analytics_full')
    pbi_baf_missing = [c for c in PBI_BAF_VIEW_REQUIRED_COLUMNS if c not in pbi_baf_cols]
    if pbi_baf_missing:
        raise RuntimeError(
            'ANTI-REGRESSION: public.pbi_big_analytics_full не содержит колонки, нужные '
            f'модели big_analytics_full: {pbi_baf_missing}')

    log.info('  coverage OK: %d колонок fact-contract, %d колонок pbi_big_analytics_full, %d arp_fact',
             len(BAF_REQUIRED_COLUMNS), len(PBI_BAF_VIEW_REQUIRED_COLUMNS), len(ARP_REQUIRED_COLUMNS))


def connect():
    c = psycopg2.connect(**DB)
    c.autocommit = False
    return c


def exec_step(cur, sql, label):
    t = time.perf_counter()
    cur.execute(sql)
    log.info('  %s — %.1fs (rowcount=%s)', label, time.perf_counter() - t, cur.rowcount)


def size_of(cur, rel):
    cur.execute("SELECT pg_size_pretty(pg_total_relation_size(%s)), "
                "(SELECT reltuples::bigint FROM pg_class WHERE oid = %s::regclass)", (rel, rel))
    return cur.fetchone()


# ─────────────────────────────────────────────────────────────────────────────
def build_schema(cur):
    # ⚠️ КОНСОЛИДАЦИЯ star -> public (2026-06-10, решение пользователя): все объекты
    # звезды переехали в схему public. Отдельной схемы star больше нет — CREATE SCHEMA
    # не нужен (public существует всегда). Функция оставлена no-op для идемпотентности
    # main() и обратной совместимости вызова.
    log.info('[0] схема public (звезда консолидирована в public, отдельной star нет)')
    # View зависит от Dim_Date/Dim_Campaign/Dim_AdGroup/Dim_Adjustment/fact_big_analytics.
    # Снимаем её до пересборки dim/fact; ниже build_pbi_big_analytics_full_view создаст заново.
    cur.execute('DROP VIEW IF EXISTS public.pbi_big_analytics_full')


def build_dim_date(cur):
    log.info('[1] Dim_Date')
    cur.execute("DROP TABLE IF EXISTS public.\"Dim_Date\"")
    cur.execute(f"""
        CREATE TABLE public."Dim_Date" AS
        WITH d AS (
            -- UTC_DATE_GUARD_2026-06-26: явный порог инварианта CANON.md.
            -- Строки и week_start до DATE_FROM недопустимы — они создают
            -- "декабрьский" слайс в PBI на недельных вкладках.
            SELECT DISTINCT "Date" AS d FROM {T_UNI}
            WHERE "Date" IS NOT NULL
              AND "Date" >= '2026-01-01'::date
        )
        SELECT
            d                                                   AS "Date",
            GREATEST(
                (d - (EXTRACT(DOW FROM d)::int + 6) % 7)::date,
                '{DATE_FROM}'::date
            )                                                   AS week_start,
            CASE EXTRACT(DOW FROM d)::int
                WHEN 1 THEN 'Понедельник' WHEN 2 THEN 'Вторник' WHEN 3 THEN 'Среда'
                WHEN 4 THEN 'Четверг' WHEN 5 THEN 'Пятница' WHEN 6 THEN 'Суббота'
                ELSE 'Воскресенье' END                          AS "День недели",
            EXTRACT(YEAR  FROM d)::smallint                     AS year,
            EXTRACT(MONTH FROM d)::smallint                     AS month,
            to_char(d, 'YYYY-MM')                               AS year_month,
            EXTRACT(DAY   FROM d)::smallint                     AS day
        FROM d
    """)
    cur.execute('ALTER TABLE public."Dim_Date" ADD PRIMARY KEY ("Date")')


def build_dim_campaign(cur):
    log.info('[2] Dim_Campaign (из campaign_status + добивка из факта)')
    cur.execute("DROP TABLE IF EXISTS public.\"Dim_Campaign\"")
    # База — campaign_status (CampaignId уникален: 0 дублей). Добиваем CampaignId,
    # которые есть в факте, но отсутствуют в campaign_status (61 шт.) — берём имя из факта.
    cur.execute(f"""
        CREATE TABLE public."Dim_Campaign" AS
        WITH from_status AS (
            SELECT
                "CampaignId",
                "CampaignName",
                "account_login",
                "статус"          AS "статус_кампании",
                "специалист",
                "manager_login",
                "campaign_status",
                "payment_model"
            FROM public.campaign_status
        ),
        fact_extra AS (
            SELECT DISTINCT ON (u."CampaignId")
                u."CampaignId",
                u."CampaignName",
                u."account_login",
                NULL::text  AS "статус_кампании",
                u."специалист",
                u."manager_login",
                u."campaign_status",
                u."payment_model"
            FROM {T_UNI} u
            WHERE u."CampaignId" IS NOT NULL
              AND u."CampaignId" NOT IN (SELECT "CampaignId" FROM from_status)
            -- СВЕЖЕЕ ПО ДАТЕ (2026-06-10): для добивочных кампаний (нет в campaign_status)
            -- брать ПОСЛЕДНЕЕ имя по дате, не алфавитно-первое. Date DESC первым; вторичный
            -- CampaignName для воспроизводимости (max(Date) уникально резолвит 1/1 конфликт).
            ORDER BY u."CampaignId", u."Date" DESC NULLS LAST, u."CampaignName" NULLS LAST
        )
        SELECT * FROM from_status
        UNION ALL
        SELECT * FROM fact_extra
    """)
    cur.execute('ALTER TABLE public."Dim_Campaign" ADD PRIMARY KEY ("CampaignId")')
    # довесок: «номер кампании | название» — СВЕЖЕЕ по дате (детерминированно)
    cur.execute("""
        ALTER TABLE public."Dim_Campaign"
        ADD COLUMN "номер кампании | название кампании" text
    """)
    # ⚠️ ДЕТЕРМИНИЗМ «СВЕЖЕЕ ПО ДАТЕ» (2026-06-10): раньше mode() WITHIN GROUP брал
    # самое частое значение, НО при равенстве частот PostgreSQL mode() выбирает
    # ПРОИЗВОЛЬНОЕ из равных -> недетерминированно между пересборками. По решению
    # пользователя берём ПОСЛЕДНЕЕ по дате (свежее) через DISTINCT ON, с вторичным
    # tie-break по самому значению -> воспроизводимо.
    # NULL_NAME_FALLBACK_2026-07-06: первичный приоритет — NOT NULL имя (IS NOT NULL DESC):
    # если у кампании есть хоть одна строка с именем (любой даты), она предпочтётся
    # строке с NULL именем. Это страхует случай «новая кампания: первые прогоны без имени
    # (API не успел) → NULL перекрывал свежей датой». Для уже именованных кампаний
    # порядок не меняется (все строки NOT NULL → вторичная сортировка по Date DESC та же).
    cur.execute(f"""
        UPDATE public."Dim_Campaign" dc
        SET "номер кампании | название кампании" = src.v
        FROM (
            SELECT DISTINCT ON ("CampaignId")
                   "CampaignId",
                   "номер кампании | название кампании" AS v
            FROM {T_UNI}
            WHERE "CampaignId" IS NOT NULL
            ORDER BY "CampaignId",
                     ("номер кампании | название кампании" IS NOT NULL) DESC,
                     "Date" DESC NULLS LAST,
                     "номер кампании | название кампании" NULLS LAST
        ) src
        WHERE dc."CampaignId" = src."CampaignId"
    """)
    # ─── SURROGATE-CAMPAIGN-FIX (2026-06-11) ──────────────────────────────────
    # ⚠️ ПУСТОЙ CampaignName у посевов/не-Директ в PBI: строки unified с
    # "CampaignId" IS NULL (посевы crop_targeting/telegram/social, отзывы пиксель,
    # звонки calls, seo, direct_zero) НЕ джойнятся к Dim_Campaign → весь их расход
    # падает в одну пустую строку CampaignName в матрице PBI. При этом
    # "CampaignName" у них ЗАПОЛНЕН в unified (для посевов = channel_link площадки).
    # ФИКС: для каждого DISTINCT непустого CampaignName с NULL CampaignId генерим
    # СУРРОГАТНЫЙ ОТРИЦАТЕЛЬНЫЙ ключ -abs(hashtext(name)::bigint) и добавляем строкой
    # в Dim_Campaign. Тот же ключ проставляется в факте (build_fact, COALESCE) — связь
    # fact→Dim_Campaign срабатывает, имя площадки видно в визуалах.
    # БЕЗОПАСНОСТЬ (проверено read-only SQL на живых данных 2026-06-11):
    #   • реальные CampaignId положительны (23.5M..710M) → суррогат (строго <0) НЕ
    #     пересекается ни с campaign_status, ни с факт-добивкой (overlap=0/0);
    #   • hashtext::bigint ПЕРЕД abs → -abs(INT_MIN) не переполняет (intmin_cnt=0);
    #   • коллизий hashtext по 736 distinct CampaignName НЕТ (736 distinct sid);
    #   • факт-сторона и dim-сторона дают одинаковый id 63550/63550 (одно выражение).
    # Строки с NULL CampaignId И пустым CampaignName (calls/seo с пустым именем,
    # пиксель) остаются NULL — у них нет имени, суррогат неприменим (by design).
    # account_login/специалист/manager_login/статус оставляем NULL (у не-Директ их нет).
    # ⚠️ SURROGATE_STATUS_NULL_2026-06-24: campaign_status суррогатных строк = NULL
    # (было: 'surrogate'). Ключ связи fact→Dim_Campaign НЕ меняется — он отрицательный
    # bigint ("CampaignId"). campaign_status — отображаемая колонка, не ключ связи.
    # NULL → в слайсере PBI показывается как «(Пусто)», а не лишнее значение «surrogate».
    cur.execute(f"""
        INSERT INTO public."Dim_Campaign"
            ("CampaignId", "CampaignName", "account_login", "статус_кампании",
             "специалист", "manager_login", "campaign_status", "payment_model",
             "номер кампании | название кампании")
        SELECT DISTINCT ON (s.sid)
            s.sid,
            s."CampaignName",
            NULL::text, NULL::text, NULL::text, NULL::text,
            NULL::text,               -- SURROGATE_STATUS_NULL_2026-06-24: было 'surrogate', стало NULL (слайсер PBI)
            NULL::text,
            s."CampaignName"          -- «номер | название» = само имя площадки
        FROM (
            SELECT DISTINCT
                "CampaignName",
                (-abs(hashtext("CampaignName")::bigint))::bigint AS sid
            FROM {T_UNI}
            WHERE "CampaignId" IS NULL
              AND "CampaignName" IS NOT NULL
              AND trim("CampaignName") <> ''
        ) s
        WHERE s.sid NOT IN (SELECT "CampaignId" FROM public."Dim_Campaign")
        ORDER BY s.sid, s."CampaignName"
        ON CONFLICT ("CampaignId") DO NOTHING
    """)


def build_dim_adgroup(cur):
    log.info('[3] Dim_AdGroup (по AdGroupId из unified; СВЕЖЕЕ по дате детерминированно)')
    cur.execute("DROP TABLE IF EXISTS public.\"Dim_AdGroup\"")
    # AdGroupId уникальный ключ; берём DISTINCT ON по AdGroupId. ag_part1-7 и adgroup_code
    # уже посчитаны в источнике (corrections + naming).
    # ⚠️ ДЕТЕРМИНИЗМ «СВЕЖЕЕ ПО ДАТЕ» (2026-06-10, решение пользователя): не-1:1 атрибуты
    # (AdGroupName: 13 групп с >1 значением; adgroup_code: 3377 групп) брать ПОСЛЕДНЕЕ
    # по дате, а не алфавитно-первое. Раньше ORDER BY AdGroupName NULLS LAST давал
    # алфавитный (детерминированный, но НЕ свежий) выбор. Теперь "Date" DESC первым =
    # последнее по дате значение; вторичный tie-break по самим атрибутам делает выбор
    # ВОСПРОИЗВОДИМЫМ при 2 значениях в один последний день (проверено: max(Date)
    # уникально резолвит AdGroupName 13/13; adgroup_code 3297/3377, остальные 80 групп
    # с двумя кодами в один последний день закрывает вторичный ORDER BY adgroup_code).
    cur.execute(f"""
        CREATE TABLE public."Dim_AdGroup" AS
        SELECT DISTINCT ON (u."AdGroupId")
            u."AdGroupId",
            u."AdGroupName",
            u."adgroup_code",
            u."номер группы | название группы",
            u."ag_part1", u."ag_part2", u."ag_part3", u."ag_part4",
            u."ag_part5", u."ag_part6", u."ag_part7",
            u."ag_part1_name",
            -- неверный_кодер_new — AdGroup-level флаг качества кодировки AdGroupName
            -- ('верный кодер'/'неверный кодер'). Функц. зависит от AdGroupId
            -- (0 конфликтов на 151103 групп, проверено 2026-06-06). Это размерность,
            -- НЕ факт → выносим в Dim_AdGroup, в факте отсутствует. 801 строка со
            -- значением при NULL AdGroupId — звонки (calls), значение неприменимо, отбрасываются.
            u."неверный_кодер_new",
            u."CampaignId"      AS "parent_CampaignId"
        FROM {T_UNI} u
        WHERE u."AdGroupId" IS NOT NULL
        -- СВЕЖЕЕ ПО ДАТЕ: Date DESC = последнее значение; затем стабильные вторичные
        -- ключи (AdGroupName, adgroup_code) для воспроизводимости при одинаковой дате.
        ORDER BY u."AdGroupId", u."Date" DESC NULLS LAST,
                 u."AdGroupName" NULLS LAST, u."adgroup_code" NULLS LAST
    """)
    cur.execute('ALTER TABLE public."Dim_AdGroup" ADD PRIMARY KEY ("AdGroupId")')


def build_dim_site(cur):
    log.info('[4] Dim_Site (ТОЛЬКО ключ domain — связь fact↔Dim_Site / direct_history)')
    cur.execute("DROP TABLE IF EXISTS public.\"Dim_Site\"")
    # ⚠️ РЕВИЗИЯ 2026-06-07: 14 row-grain атрибутов (салон/город/регион/тип_сайта/
    # id_салона/направление/шаблон/site_quiz/проджект/менеджер/специалист/Название crm/
    # марки авто/статус) ВОЗВРАЩЕНЫ НА ФАКТ (build_fact) — они НЕ функц.зависят от domain
    # (один domain несёт разные значения в разных строках unified), поэтому mode()-свёртка
    # к 1 значению/домен искажала срезы (Кудерко 24.8М/53 вместо 25.42М/47). Dim_Site
    # теперь — чистая dim-таблица ТОЛЬКО с ключом domain: нужна лишь для связей
    # fact→Dim_Site (1:*) и Dim_Site→direct_history. Никаких атрибутов — все на факте.
    cur.execute(f"""
        CREATE TABLE public."Dim_Site" AS
        SELECT DISTINCT domain
        FROM {T_UNI}
        WHERE {FACT_AUTO_WHERE}   -- скоуп на нишу «Авто»
    """)
    cur.execute('ALTER TABLE public."Dim_Site" ADD PRIMARY KEY (domain)')
    # Добивка Авто-доменов из local_gsheet_sites, которых нет в факте (для полноты
    # стороны «1» связи: домены без фактов, но с историей Директа / справочные).
    cur.execute("""
        INSERT INTO public."Dim_Site" (domain)
        SELECT DISTINCT lower(trim(s.domain))
        FROM public.local_gsheet_sites s
        WHERE lower(trim(s.domain)) NOT IN (SELECT domain FROM public."Dim_Site")
          AND s.domain IS NOT NULL AND trim(s.domain) <> ''
          AND s.niche = 'Авто'   -- добиваем только Авто-домены (скоуп ниши)
        ON CONFLICT (domain) DO NOTHING
    """)


def build_dim_adjustment(cur):
    log.info('[4b] Dim_Adjustment (RlAdjustmentId -> RlAdjustmentId_total, СТРОГО 1:1)')
    cur.execute('DROP TABLE IF EXISTS public."Dim_Adjustment"')
    # ⚠️ ВЫНОС RlAdjustmentId_total С ФАКТА В ИЗМЕРЕНИЕ (2026-06-10, по аналогии с вар. A).
    # RlAdjustmentId_total — текстовый «итог корректировок» уровня RlAdjustmentId,
    # дублировался построчно на факте (~126 МБ на 3.8M строк). СТРОГО функц. зависит
    # от RlAdjustmentId: проверено на big_analytics_unified — 1691 distinct ключ,
    # 0 конфликтов (count(DISTINCT RlAdjustmentId_total) per key = 1 для всех),
    # 320693 строк с RlAdjustmentId IS NULL (у них _total тоже NULL, ключ⟺total).
    # Поэтому здесь НЕ нужна DISTINCT ON/мода — значение однозначно. DISTINCT ON
    # с детерминированным ORDER BY оставлен как страховка на случай будущего дрейфа
    # (если 1:1 когда-то нарушится — берём СВЕЖЕЕ по дате, см. правило ниже).
    cur.execute(f"""
        CREATE TABLE public."Dim_Adjustment" AS
        SELECT DISTINCT ON (u."RlAdjustmentId")
            u."RlAdjustmentId",
            u."RlAdjustmentId_total"
        FROM {T_UNI} u
        WHERE u."RlAdjustmentId" IS NOT NULL
        -- СВЕЖЕЕ ПО ДАТЕ детерминированно: Date DESC (последнее значение), затем
        -- сам _total как стабильный вторичный tie-break (на случай 2 значений в один
        -- последний день — воспроизводимо между пересборками). Сейчас 0 конфликтов,
        -- ветка tie-break не срабатывает, но гарантирует детерминизм при дрейфе.
        ORDER BY u."RlAdjustmentId", u."Date" DESC NULLS LAST, u."RlAdjustmentId_total" NULLS LAST
    """)
    cur.execute('ALTER TABLE public."Dim_Adjustment" ADD PRIMARY KEY ("RlAdjustmentId")')
    # lz4 на текстовый атрибут (вес без потери данных)
    cur.execute('ALTER TABLE public."Dim_Adjustment" '
                'ALTER COLUMN "RlAdjustmentId_total" SET COMPRESSION lz4')


def build_fact(cur):
    log.info('[5] public.fact_big_analytics (лёгкий: проекция unified, оптим. типы)')
    cur.execute("DROP TABLE IF EXISTS public.fact_big_analytics")
    # Оптимизация веса: целочисленные меры -> integer/bigint, total_cost -> numeric(14,2).
    # Column tetris: bigint -> numeric -> integer -> date -> text.
    cur.execute(f"""
        CREATE TABLE public.fact_big_analytics AS
        SELECT
            -- ⚠️ SURROGATE-CAMPAIGN-FIX (2026-06-11): реальный CampaignId берётся как
            -- есть (COALESCE первым аргументом → строки Директа НЕ затрагиваются). Для
            -- не-Директ строк (CampaignId IS NULL, но CampaignName заполнен: посевы/
            -- telegram/отзывы/звонки/seo) подставляем СУРРОГАТНЫЙ ОТРИЦАТЕЛЬНЫЙ ключ —
            -- ровно то же выражение, что в build_dim_campaign → связь fact→Dim_Campaign
            -- срабатывает, имя площадки видно в матрице PBI. Если CampaignName тоже пуст
            -- (часть calls/seo/пиксель) → остаётся NULL (имени нет, by design).
            COALESCE(
                "CampaignId",
                CASE WHEN "CampaignName" IS NOT NULL AND trim("CampaignName") <> ''
                     THEN (-abs(hashtext("CampaignName")::bigint))::bigint
                END
            )::bigint                                         AS "CampaignId",
            "AdGroupId"::bigint                               AS "AdGroupId",
            "RlAdjustmentId"::bigint                          AS "RlAdjustmentId",
            priezd_arrival_date::bigint                       AS priezd_arrival_date,
            prodazhi_arrival_date::bigint                     AS prodazhi_arrival_date,
            dohod_do_kredita::bigint                          AS dohod_do_kredita,
            dobro::bigint                                     AS dobro,
            COALESCE(total_cost,0)::numeric                   AS total_cost,
            -- ⚠️ kol_vo_zayavok/korr/kval/priezd/prodazhi — ДРОБНЫЕ для пиксель-атрибуции
            -- (step11_pixel_score распределяет дробный кредит). ОБЯЗАТЕЛЬНО numeric,
            -- иначе ::integer теряет дробь (priezd пикселя 6097 -> 1614). Потеря данных.
            COALESCE(kol_vo_zayavok,0)::numeric               AS kol_vo_zayavok,
            COALESCE(korr,0)::numeric                         AS korr,
            COALESCE(kval,0)::numeric                         AS kval,
            COALESCE(priezd,0)::numeric                       AS priezd,
            COALESCE(prodazhi,0)::numeric                     AS prodazhi,
            -- целочисленные меры — integer безопасно (0 дробных строк)
            COALESCE("Clicks",0)::integer                     AS "Clicks",
            COALESCE("Impressions",0)::integer                AS "Impressions",
            COALESCE(nekorr,0)::integer                       AS nekorr,
            COALESCE(ne_otvechaet,0)::integer                 AS ne_otvechaet,
            COALESCE(nedozvon,0)::integer                     AS nedozvon,
            COALESCE(filtr,0)::integer                        AS filtr,
            COALESCE(priedet,0)::integer                      AS priedet,
            "План заявки"::integer                            AS "План заявки",
            "План приезда"::integer                           AS "План приезда",
            "Date"::date                                      AS "Date",
            domain::text                                      AS domain,
            "атрибуция"::text                                 AS "атрибуция",
            _source_table::text                               AS _source_table,
            -- 10 degenerate-dim (orphan) текстовых колонок: атрибуты уровня факта,
            -- функционально НЕ зависят целиком от одного из 4 ключей (CampaignId/AdGroupId/
            -- domain/Date), поэтому остаются на факте, а не выносятся в dim. Сохраняются
            -- в лёгком факте, чтобы визуалы/срезы по ним продолжали работать после распайки.
            tp::text                                          AS tp,
            "источник"::text                                  AS "источник",
            "AdNetworkType"::text                             AS "AdNetworkType",
            "аккаунт|сайт"::text                              AS "аккаунт|сайт",
            campaign_code::text                               AS campaign_code,
            "поставщик"::text                                 AS "поставщик",
            "Device"::text                                    AS "Device",
            fid::text                                         AS fid,
            cpc_cpa::text                                     AS cpc_cpa,
            -- ⚠️ RlAdjustmentId_total СНЯТ С ФАКТА В Dim_Adjustment (2026-06-10, ~126 МБ).
            -- СТРОГО 1:1 с ключом RlAdjustmentId (1691 ключ, 0 конфликтов). Ключ
            -- RlAdjustmentId ОСТАЁТСЯ на факте (выше) для связи fact->Dim_Adjustment.
            -- В PBI _total приходит JOIN-ом факта с Dim_Adjustment в M-партиции
            -- big_analytics_full.tmdl (имя big_analytics_full[RlAdjustmentId_total]
            -- СОХРАНЕНО -> column-определение и отчёт не правятся).
            -- ⚠️ 14 ROW-GRAIN текстовых колонок (возврат на факт, 2026-06-07).
            -- Эти атрибуты НЕ функц.зависят от domain: один domain в разных строках
            -- unified несёт РАЗНЫЕ значения (доменов с >1 значением в Авто-скоупе:
            -- направление=800, site_quiz=608, «марки авто»=530, специалист=26,
            -- тип_сайта=23, статус=13, шаблон=5, салон/id_салона=4, проджект=3,
            -- город/регион=2, менеджер/«Название crm»=1). Поэтому держать их в
            -- Dim_Site (1 значение/домен через mode()) НЕЛЬЗЯ — при фильтре по
            -- специалист/направление/салон PBI берёт НЕ ТОТ набор строк (давало
            -- Кудерко 24.8М/53 вместо эталона 25.42М/47). Источник — построчно из
            -- big_analytics_unified, та же строка (тот же SELECT/WHERE) → точное 1:1.
            "направление"::text                               AS "направление",
            site_quiz::text                                   AS site_quiz,
            "марки авто"::text                                AS "марки авто",
            "специалист"::text                                AS "специалист",
            "тип_сайта"::text                                 AS "тип_сайта",
            "статус"::text                                    AS "статус",
            "салон"::text                                     AS "салон",
            "шаблон"::text                                    AS "шаблон",
            "id_салона"::text                                 AS "id_салона",
            "город"::text                                     AS "город",
            "регион"::text                                    AS "регион",
            "проджект"::text                                  AS "проджект",
            "менеджер"::text                                  AS "менеджер",
            "Название crm"::text                              AS "Название crm",
            -- ⚠️ 2 ROW-GRAIN текстовых колонки (добавлены 2026-06-07).
            -- тип_заявки (значения: заявки/звонки/отзывы/пиксель_атрибуц) и manager_login.
            -- ПОСТРОЧНЫЕ, НЕ выводятся из _source_table: источник 'calls' раздваивает
            -- звонки/заявки → тип_заявки нельзя восстановить из _source_table. Берутся
            -- построчно из big_analytics_unified (та же строка, тот же SELECT/WHERE → 1:1).
            -- На них ссылаются 42 визуала (слайсер тип_заявки) + 1 (manager_login).
            "тип_заявки"::text                                AS "тип_заявки",
            manager_login::text                               AS manager_login
            -- ⚠️ 18 dim-КОЛОНОК СНЯТЫ С ФАКТА (вариант A, 2026-06-10).
            -- Ранее (2026-06-07) они были возвращены на факт, потому что тонкий отчёт
            -- «Большая аналитика_user» (live-connect, без своей модели) резолвил их как
            -- big_analytics_full[X] и падал «Поля, которые нужно исправить». Теперь эти
            -- 18 атрибутов приходят в Power BI через JOIN факта с Dim_Campaign /
            -- Dim_AdGroup / Dim_Date в M-партиции big_analytics_full.tmdl (имена
            -- big_analytics_full[X] СОХРАНЕНЫ → отчёт не правится). Снятие физических
            -- колонок с факта = ×2 по весу (топ веса: CampaignName/AdGroupName/
            -- номер кампании/ag_part — функц. зависят от CampaignId/AdGroupId, дублировались
            -- построчно на 3.8M строк). Связи модели НЕ трогаются: 3 нужные rel
            -- (CampaignId→Dim_Campaign, AdGroupId→Dim_AdGroup, Date→Dim_Date) уже есть;
            -- «Модель атрибуции» = disconnected DATATABLE (TREATAS), от снятия колонок
            -- НЕ зависит. Снятые 18: CampaignName, AdGroupName, account_login, adgroup_code,
            -- ag_part1..ag_part7, campaign_status, payment_model, неверный_кодер_new,
            -- week_start, «День недели», «номер группы | название группы»,
            -- «номер кампании | название кампании». _source_table НЕ снят (нужен
            -- verify_star.py GROUP BY + verify_big_analytics блок 12).
        FROM {T_UNI}
        WHERE ({FACT_SCOPE_WHERE})   -- Авто-скоуп + возврат отзывы/звонки (2026-06-07)
          -- ⚠️ ФИЛЬТР ДЕКАБРЯ 2025 (2026-06-07): инвариант витрины "Date" >= '2026-01-01'.
          -- В unified протекли 182 визита декабря 2025 (все «По дате визита», заявка=0),
          -- из-за чего в матрице PBI появился лишний «Декабрь». Оракул («По дате заявки»)
          -- не затрагивается: декабрьские строки только визитные. Граница = DATE_FROM из
          -- config/settings.py (каноничная), убирает РОВНО строки <2026-01-01.
          AND "Date" >= '{DATE_FROM}'::date
    """)
    # SET COMPRESSION lz4 на text-колонки (PG14+) — экономия веса без потери данных.
    for col in ('domain', 'атрибуция', '_source_table', 'tp', 'источник',
                'AdNetworkType', 'аккаунт|сайт', 'campaign_code', 'поставщик',
                'Device', 'fid', 'cpc_cpa',
                # RlAdjustmentId_total СНЯТ с факта (Dim_Adjustment, 2026-06-10) ->
                # исключён из lz4-списка (ALTER COLUMN на несуществующую колонку упал бы).
                # 14 row-grain (возврат 2026-06-07)
                'направление', 'site_quiz', 'марки авто', 'специалист', 'тип_сайта',
                'статус', 'салон', 'шаблон', 'id_салона', 'город', 'регион',
                'проджект', 'менеджер', 'Название crm',
                # 2 row-grain (добавлены 2026-06-07): слайсер тип_заявки + manager_login
                'тип_заявки', 'manager_login'):
                # ⚠️ 18 dim-колонок СНЯТЫ с факта (вариант A, 2026-06-10) — больше нет в
                # проекции, поэтому исключены и из lz4-списка (ALTER COLUMN на
                # несуществующую колонку упал бы). Атрибуты приходят в PBI через JOIN
                # факта с Dim_Campaign/Dim_AdGroup/Dim_Date в M-партиции (см. выше).
        cur.execute(f'ALTER TABLE public.fact_big_analytics '
                    f'ALTER COLUMN "{col}" SET COMPRESSION lz4')
    cur.execute('ALTER TABLE public.fact_big_analytics SET (fillfactor=100)')


def build_arp_fact(cur):
    log.info('[6] public.arp_fact (VIEW поверх ARP: те же conformed dim + 25 кол. для репойнта партиции)')
    # ⚠️ VIEW, а НЕ TABLE (2026-06-09): arp_fact — чистая проекция public.analytics_report_placement
    # (тип-касты, ренеймы директолог→"Специалист"/domain→"домен", COALESCE(x,0), WHERE niche='Авто').
    # Материализация дублировала ~3.45 ГБ диска без выигрыша на чтении (PBI обновляется полным Import;
    # лишние 15-20 сек на VIEW несущественны). VIEW устраняет дубль данных. Поэтому:
    # нет fillfactor/индексов/VACUUM на arp_fact (неприменимо к VIEW).
    # ⚠️ ФИКС 2026-06-09: при ПЕРВОЙ конвертации arp_fact ещё лежит как BASE TABLE
    # (старая материализация 3454МБ), на повторных прогонах — уже VIEW. Postgres
    # различает типы: `DROP VIEW IF EXISTS` на TABLE кидает WrongObjectType (IF EXISTS
    # гасит только «отсутствует», НЕ «другой тип»), и симметрично `DROP TABLE IF EXISTS`
    # на VIEW. Поэтому два DROP подряд НЕ идемпотентны. Решение: определить фактический
    # relkind через pg_class и дропнуть нужным DDL — работает в любом состоянии
    # (TABLE 'r' / VIEW 'v' / отсутствует) и на любых повторных прогонах.
    cur.execute("""
        SELECT c.relkind
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = 'arp_fact'
    """)
    _row = cur.fetchone()
    if _row is not None:
        if _row[0] == 'v':
            cur.execute("DROP VIEW IF EXISTS public.arp_fact")
        else:
            cur.execute("DROP TABLE IF EXISTS public.arp_fact CASCADE")
    # ⚠️ РАСШИРЕНО (Этап 1 миграции на звезду, 2026-06-07): arp_fact теперь отдаёт
    # ВСЕ колонки, которые мапит модельная таблица analytics_report_placement, чтобы
    # её партицию можно было репойнтнуть на public.arp_fact (Этап 3). Точные имена
    # sourceColumn (регистр/спецсимволы важны). Маппинг источника:
    #   директолог -> "Специалист" (в источнике нет колонки "Специалист")
    #   domain     -> "домен"      (в источнике нет колонки "домен"; "domain" тоже оставлен)
    #   date       -> "date"       (lowercase, ОТДЕЛЬНО от существующей "Date")
    # Дубли date/"Date" и домен/domain — ОК: модельные таблицы мапят разные имена.
    # Conversion-колонки CRM/«Все формы» уже bigint в источнике → ::bigint безопасен,
    # дробной/пиксельной атрибуции тут нет (ARP — не пиксель).
    cur.execute(f"""
        CREATE VIEW public.arp_fact AS
        SELECT
            campaign_id::bigint                       AS "CampaignId",
            ad_group_id::bigint                       AS "AdGroupId",
            ROUND(COALESCE(cost,0)::numeric, 2)       AS cost,
            COALESCE(clicks,0)::bigint                AS clicks,
            COALESCE(kol_vo_zayavok,0)::integer       AS kol_vo_zayavok,
            COALESCE(korr,0)::integer                 AS korr,
            COALESCE(kval,0)::integer                 AS kval,
            COALESCE(priezd,0)::integer               AS priezd,
            COALESCE(prodazhi,0)::integer             AS prodazhi,
            COALESCE(nekorr,0)::integer               AS nekorr,
            COALESCE(ne_otvechaet,0)::integer         AS ne_otvechaet,
            COALESCE(nedozvon,0)::integer             AS nedozvon,
            COALESCE(filtr,0)::integer                AS filtr,
            COALESCE(priedet,0)::integer              AS priedet,
            COALESCE(dohod_do_kredita,0)::integer     AS dohod_do_kredita,
            COALESCE(dobro,0)::integer                AS dobro,
            placement::text                           AS placement,
            ad_network_type::text                     AS ad_network_type,
            date::date                                AS "Date",
            domain::text                              AS domain,
            "тип_заявки"::text                        AS "тип_заявки",
            -- ── +25 колонок для репойнта партиции analytics_report_placement ──
            COALESCE("CRM: Заказ оплачен",0)::bigint  AS "CRM: Заказ оплачен",
            COALESCE("CRM: Заказ отменен",0)::bigint  AS "CRM: Заказ отменен",
            COALESCE("CRM: Заказ создан",0)::bigint   AS "CRM: Заказ создан",
            COALESCE("CRM: Спам заказ",0)::bigint     AS "CRM: Спам заказ",
            date::date                                AS "date",
            placement_key::text                       AS placement_key,
            tp::text                                  AS tp,
            updated_at::timestamp                     AS updated_at,
            COALESCE("Все формы",0)::bigint           AS "Все формы",
            "директолог"::text                        AS "Специалист",
            domain::text                              AS "домен",
            "логин"::text                             AS "логин",
            "номер кампании|название кампании"::text  AS "номер кампании|название кампании",
            "салон"::text                             AS "салон",
            "тип_сайта"::text                         AS "тип_сайта"
        FROM {T_ARP}
        WHERE {FACT_AUTO_WHERE}   -- скоуп на нишу «Авто»
    """)
    # ad_network_type/cost/kol_vo_zayavok/korr/kval/placement/priedet/priezd/prodazhi
    # уже присутствуют как основные колонки (выше) — модельная таблица мапит те же
    # sourceColumn-имена на них, отдельно дублировать не нужно.
    # (fillfactor/индексы/VACUUM на arp_fact удалены — неприменимы к VIEW, 2026-06-09)


def build_pbi_big_analytics_full_view(cur):
    log.info('[6b] public.pbi_big_analytics_full (готовая SQL-view для PBI big_analytics_full)')
    cur.execute("""
        SELECT c.relkind
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = 'pbi_big_analytics_full'
    """)
    _row = cur.fetchone()
    if _row is not None:
        if _row[0] == 'v':
            cur.execute('DROP VIEW IF EXISTS public.pbi_big_analytics_full')
        else:
            cur.execute('DROP TABLE IF EXISTS public.pbi_big_analytics_full CASCADE')

    cur.execute(f"""
        CREATE VIEW public.pbi_big_analytics_full AS
        SELECT
            f."Date",
            f."CampaignId",
            f."AdNetworkType",
            f."Device",
            f.total_cost,
            f.campaign_code,
            f.tp,
            f.cpc_cpa,
            f.kol_vo_zayavok AS "Обращения",
            f.korr,
            f.kval,
            f.priezd,
            f.prodazhi,
            f.nekorr,
            f.ne_otvechaet,
            f.filtr,
            f.nedozvon,
            f."RlAdjustmentId",
            COALESCE(adj."RlAdjustmentId_total", '') AS "RlAdjustmentId_total",
            f.fid,
            f."Clicks",
            f."источник",
            f."План заявки",
            f."План приезда",
            f."аккаунт|сайт",
            f."поставщик",
            f.domain AS "домен",
            f.priedet,
            f.dohod_do_kredita,
            f.dobro,
            f."атрибуция",
            f."AdGroupId",
            f."направление",
            f.site_quiz,
            f."марки авто",
            f."специалист",
            f."тип_сайта",
            f."статус",
            f."салон",
            f."шаблон",
            f."id_салона",
            f."город",
            f."регион",
            f."проджект",
            f."менеджер",
            f."Название crm",
            f."тип_заявки",
            f.manager_login,
            COALESCE(dc."CampaignName", '') AS "CampaignName",
            COALESCE(da."AdGroupName", '') AS "AdGroupName",
            COALESCE(dc."account_login", '') AS "account_login",
            COALESCE(da."adgroup_code", '') AS "adgroup_code",
            COALESCE(da."ag_part1", '') AS "ag_part1",
            COALESCE(da."ag_part2", '') AS "ag_part2",
            COALESCE(da."ag_part3", '') AS "ag_part3",
            COALESCE(da."ag_part4", '') AS "ag_part4",
            COALESCE(da."ag_part5", '') AS "ag_part5",
            COALESCE(da."ag_part6", '') AS "ag_part6",
            COALESCE(da."ag_part7", '') AS "ag_part7",
            COALESCE(dc."campaign_status", '') AS "campaign_status",
            COALESCE(dc."payment_model", '') AS "payment_model",
            COALESCE(da."неверный_кодер_new", '') AS "неверный_кодер_new",
            GREATEST(dd.week_start, '{DATE_FROM}'::date) AS week_start,
            COALESCE(dd."День недели", '') AS "День недели",
            COALESCE(da."номер группы | название группы", '') AS "номер группы | название группы",
            COALESCE(dc."номер кампании | название кампании", '') AS "номер кампании | название кампании"
        FROM public.fact_big_analytics f
        LEFT JOIN public."Dim_Campaign" dc
               ON f."CampaignId" = dc."CampaignId"
        LEFT JOIN public."Dim_AdGroup" da
               ON f."AdGroupId" = da."AdGroupId"
        LEFT JOIN public."Dim_Date" dd
               ON f."Date" = dd."Date"
        LEFT JOIN public."Dim_Adjustment" adj
               ON f."RlAdjustmentId" = adj."RlAdjustmentId"
    """)
    cur.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bi_analytic') THEN
                GRANT SELECT ON public.pbi_big_analytics_full TO bi_analytic;
            END IF;
        END $$;
    """)


def build_dim_location(cur):
    # DIM_LOCATION_SHOWREGION_2026-06-17
    # Измерение РЕГИОНА ПОКАЗА (не салона):
    #   ключ: id_location (BIGINT, уникальный, NOT NULL)
    #   атрибуты: location (нас. пункт показа), "Область", "GeoRegionType", distance_km_agreg
    #   НЕТ: город/салон/регион (атрибуты салона — на другой оси, не вкладываются в Область)
    #
    # Источник атрибутов: денормализованные поля фактов (LOCAL JOIN со справочником
    # local_gsheet_yandex_direct_id_location уже сделан при сборке fact_region_spend/zayavki).
    # Состав строк: UNION DISTINCT всех id_location (NOT NULL) из обоих фактов.
    # Незаматченные (Область IS NULL при непустом id_location, ~1.7% заявок) →
    #   location='(Регион не определён)', "Область"='(Регион не определён)', GeoRegionType=NULL.
    # Гарантирует: 0 orphan по непустым id_location, 0 «(Пусто)» в Dim_Location.
    #
    # NULL-правило: строки факта с NULL id_location → (Пусто) в PBI (by design,
    # spend: ~863k строк без геопривязки; zayavki: 0 NULL id_location).
    #
    # FACT_REGION_SPEND_TOREGCLASS_GUARD_2026-07-16: build_spend_daily.py DROP+CTAS
    # fact_region_spend ежедневно в 09:00 UTC (~32 мин). Если build_star попадает в
    # этот gap — UndefinedTable. to_regclass-проверка позволяет деградировать до
    # zayavki-only вместо краша: Dim_Location будет менее богатым (≈1% orphan больше),
    # но build_star и PBI не упадут. При следующем прогоне build_star после 09:32 UTC
    # fact_region_spend снова будет доступна.
    log.info('[5c] Dim_Location (id_location → location/Область/GeoRegionType/distance_km_agreg, show-region)')
    cur.execute("SELECT to_regclass('public.fact_region_spend')::text")
    _has_spend = cur.fetchone()[0] is not None
    if not _has_spend:
        log.warning(
            'FACT_REGION_SPEND_TOREGCLASS_GUARD: public.fact_region_spend НЕ существует '
            '(вероятно, build_spend_daily.py в процессе DROP+CTAS). '
            'Dim_Location строится только из fact_region_zayavki — деградация без краша.'
        )
    cur.execute('DROP TABLE IF EXISTS public."Dim_Location"')
    if _has_spend:
        cur.execute("""
            CREATE TABLE public."Dim_Location" AS
            WITH all_ids AS (
                SELECT DISTINCT id_location FROM public.fact_region_spend
                WHERE id_location IS NOT NULL
                UNION
                SELECT DISTINCT id_location FROM public.fact_region_zayavki
                WHERE id_location IS NOT NULL
            ),
            attrs_spend AS (
                -- Атрибуты показа из spend (богаче — больше строк, лучше покрытие справочника)
                SELECT
                    id_location,
                    mode() WITHIN GROUP (ORDER BY location)          AS location,
                    mode() WITHIN GROUP (ORDER BY "Область")         AS "Область",
                    mode() WITHIN GROUP (ORDER BY "GeoRegionType")   AS "GeoRegionType",
                    mode() WITHIN GROUP (ORDER BY distance_km_agreg) AS distance_km_agreg
                FROM public.fact_region_spend
                WHERE id_location IS NOT NULL
                GROUP BY id_location
            ),
            attrs_zayavki AS (
                -- Orphan id_location: есть в zayavki, но нет в spend → берём атрибуты из zayavki
                SELECT
                    rz.id_location,
                    mode() WITHIN GROUP (ORDER BY rz.location)          AS location,
                    mode() WITHIN GROUP (ORDER BY rz."Область")         AS "Область",
                    mode() WITHIN GROUP (ORDER BY rz."GeoRegionType")   AS "GeoRegionType",
                    mode() WITHIN GROUP (ORDER BY rz.distance_km_agreg) AS distance_km_agreg
                FROM public.fact_region_zayavki rz
                WHERE rz.id_location IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM attrs_spend s WHERE s.id_location = rz.id_location)
                GROUP BY rz.id_location
            ),
            combined AS (
                SELECT
                    a.id_location,
                    COALESCE(s.location,          z.location)          AS location,
                    COALESCE(s."Область",         z."Область")         AS "Область",
                    COALESCE(s."GeoRegionType",   z."GeoRegionType")   AS "GeoRegionType",
                    COALESCE(s.distance_km_agreg, z.distance_km_agreg) AS distance_km_agreg
                FROM all_ids a
                LEFT JOIN attrs_spend    s ON s.id_location = a.id_location
                LEFT JOIN attrs_zayavki  z ON z.id_location = a.id_location
            )
            SELECT
                id_location,
                COALESCE(location,  '(Регион не определён)') AS location,
                COALESCE("Область", '(Регион не определён)') AS "Область",
                "GeoRegionType",
                distance_km_agreg
            FROM combined
        """)
    else:
        # Деградированный вариант: только fact_region_zayavki
        cur.execute("""
            CREATE TABLE public."Dim_Location" AS
            WITH all_ids AS (
                SELECT DISTINCT id_location FROM public.fact_region_zayavki
                WHERE id_location IS NOT NULL
            ),
            attrs AS (
                SELECT
                    id_location,
                    mode() WITHIN GROUP (ORDER BY location)          AS location,
                    mode() WITHIN GROUP (ORDER BY "Область")         AS "Область",
                    mode() WITHIN GROUP (ORDER BY "GeoRegionType")   AS "GeoRegionType",
                    mode() WITHIN GROUP (ORDER BY distance_km_agreg) AS distance_km_agreg
                FROM public.fact_region_zayavki
                WHERE id_location IS NOT NULL
                GROUP BY id_location
            )
            SELECT
                a.id_location,
                COALESCE(at.location,  '(Регион не определён)') AS location,
                COALESCE(at."Область", '(Регион не определён)') AS "Область",
                at."GeoRegionType",
                at.distance_km_agreg
            FROM all_ids a
            LEFT JOIN attrs at ON at.id_location = a.id_location
        """)
    cur.execute('ALTER TABLE public."Dim_Location" ADD PRIMARY KEY (id_location)')
    cur.execute('SELECT COUNT(*) FROM public."Dim_Location"')
    n = cur.fetchone()[0]
    log.info('  Dim_Location built OK: %d строк (show-region, no orphan)', n)


def build_dim_distance(cur):
    # DIM_DISTANCE_ADD_2026-06-17
    log.info('[5b] Dim_Distance (distinct distance_km_agreg из fact_region_spend + fact_region_zayavki)')
    # Таблица строится из UNION двух датамартов региона — тех же источников, что в PBI-модели.
    # distance_km_agreg = INTEGER, верхняя граница бакета (100, 200, ..., 1900) или NULL («не определено»).
    # distance_label — человекочитаемая подпись диапазона для оси/среза PBI (строится здесь, в БД,
    # чтобы PBI мог сортировать и фильтровать без DAX computed column).
    # distance_sort — числовой порядок для сортировки: NULL → 9999 (внизу), 100 → 100, ...
    #
    # ⚠️ ПОКРЫТИЕ: таблица содержит ТОЛЬКО значения, реально присутствующие в фактах.
    # Это гарантирует referential integrity (нет «лишних» строк Dim без строки в факте)
    # и автоматически расширяется при появлении новых бакетов в справочнике.
    #
    # FACT_REGION_SPEND_TOREGCLASS_GUARD_2026-07-16: та же race condition что в
    # build_dim_location. Используем тот же to_regclass-паттерн для graceful degradation.
    cur.execute("SELECT to_regclass('public.fact_region_spend')::text")
    _has_spend_dist = cur.fetchone()[0] is not None
    if not _has_spend_dist:
        log.warning(
            'FACT_REGION_SPEND_TOREGCLASS_GUARD: public.fact_region_spend НЕ существует '
            '— Dim_Distance строится только из fact_region_zayavki.'
        )
    cur.execute("DROP TABLE IF EXISTS public.\"Dim_Distance\"")
    if _has_spend_dist:
        cur.execute("""
            CREATE TABLE public."Dim_Distance" AS
            WITH vals AS (
                SELECT DISTINCT distance_km_agreg
                FROM public.fact_region_spend
                UNION
                SELECT DISTINCT distance_km_agreg
                FROM public.fact_region_zayavki
            )
        SELECT
            distance_km_agreg,
            CASE
                WHEN distance_km_agreg IS NULL  THEN 'не определено'
                WHEN distance_km_agreg <= 100   THEN '0–100 км'
                ELSE
                    (distance_km_agreg - 100)::text || '–' || distance_km_agreg::text || ' км'
            END                                     AS distance_label,
            COALESCE(distance_km_agreg, 9999)       AS distance_sort
        FROM vals
        ORDER BY distance_sort
        """)
    else:
        # Деградированный вариант: только fact_region_zayavki
        cur.execute("""
            CREATE TABLE public."Dim_Distance" AS
            WITH vals AS (
                SELECT DISTINCT distance_km_agreg
                FROM public.fact_region_zayavki
            )
            SELECT
                distance_km_agreg,
                CASE
                    WHEN distance_km_agreg IS NULL  THEN 'не определено'
                    WHEN distance_km_agreg <= 100   THEN '0–100 км'
                    ELSE
                        (distance_km_agreg - 100)::text || '–' || distance_km_agreg::text || ' км'
                END                                     AS distance_label,
                COALESCE(distance_km_agreg, 9999)       AS distance_sort
            FROM vals
            ORDER BY distance_sort
        """)
    # ⚠️ NULL в distance_km_agreg допустим (строки факта без геопривязки).
    # PostgreSQL не разрешает NULL в PRIMARY KEY → используем UNIQUE INDEX WHERE NOT NULL.
    # Строки факта с NULL distance_km_agreg связь не активируют (PBI: бланк) — by design.
    cur.execute(
        'CREATE UNIQUE INDEX ON public."Dim_Distance" (distance_km_agreg) '
        'WHERE distance_km_agreg IS NOT NULL'
    )
    log.info('  Dim_Distance built OK')


def build_vk_ads_fact(conn, cur):
    """VK_ADS_FACT_2026-07-10 — датамарт public.fact_vk_ads для VK Ads отчёта.

    Грань: date × account(салон) × ad_plan(оффер) × ad_group(сегмент) × banner(объявление)
    × ось_атрибуции ('По дате заявки' / 'По дате визита').

    ИСТОЧНИКИ:
      • Рекламные метрики shows/clicks/spent = public.local_vk_ads_stats_day
        (banner×date, только Авто). Несутся ТОЛЬКО осью 'По дате заявки'.
      • Воронка = VK-лиды из public.local_leads_all (utm_source='vkads'), агрегат через
        config/status_sql (build_leads_agg_sql) — те же категории qualified/visit/sale +
        status='Приедет' (записи), что и основная витрина. VK-лиды целочисленные (CRM) →
        дробной пиксель-атрибуции здесь нет, к int ничего не приводим (её и нет).
        Два класса лидов:
          – id-несущие (utm_content='ad_group_id/banner_id'): воронка по кампании/группе.
            banner_id = split_part(utm_content,'/',2), ad_group_id = split_part(...,'/',1).
          – БЕЗ id-utm_content (только utm_campaign='victory'): бакет «не определено» по
            салону (кампании/группы/объявления нет). Не выбрасываются молча.
      • Денормализованные атрибуты (регион/тип_сайта/специалист) — из
        public.local_gsheet_sites. Для account-строк — из АВТОРИТЕТНОЙ строки с
        vk_client_id аккаунта (salon_by_acc), т.к. у салона сотни доменов и site_type/
        directologist варьируются по доменам, детерминирована только vk_client_id-строка.
        специалист=directologist, может быть NULL (напр. «Перформ РФ» — пусто в источнике).
        Для бакет-строк (нет account_id) — только регион по салону (region_by_salon;
        distinct region на салон = 1); тип_сайта/специалист там NULL (не определимы на
        грани салона).

    LEAD-DRIVEN (не spend-driven): ось 'По дате заявки' = FULL OUTER JOIN(stats, лиды) по
    (banner_id, ad_group_id, date). КАЖДЫЙ id-лид попадает в воронку по своей кампании
    (даже если у banner не было spent в день заявки — прежний LEFT JOIN от stats их резал);
    КАЖДАЯ строка stats остаётся (расход сохраняется 1:1). stats уникальна по
    (banner_id, ad_group_id, date) → FULL JOIN не размножает ни расход, ни воронку.

    ДЕДУП РЕКЛАМНЫХ МЕТРИК МЕЖДУ ОСЯМИ: shows/clicks/spent несутся ТОЛЬКО строками оси
    'По дате заявки' (сторона stats). Все прочие ветки (бакет заявка-оси + обе ветки
    визит-оси) имеют shows=clicks=spent=0 → SUM(spent) по всей таблице = SUM(spent) исходной
    stats, не удваивается.

    ВОРОНКА ПО ОСЯМ:
      • 'По дате заявки': id-лиды по created_date (FULL OUTER JOIN stats) + бакет
        «не определено» (лиды без id-utm_content по created_date, салон-грань).
      • 'По дате визита': id-лиды по arrival_date (JOIN banner_dim за атрибутами) + бакет
        «не определено» по arrival_date. Рекламные метрики = 0.

    CTR/CPM/CPL/CPV/CPS НЕ материализуем — это меры PBI (DAX). В таблице только сырьё:
    shows/clicks/spent + заявки/записи/квал/визиты/продажи.

    golden Кудерко НЕ затрагивается: это отдельная таблица (VK вне GOLDEN_SOURCES),
    build_star её не читает при сборке fact_big_analytics.
    """
    log.info('[6b] public.fact_vk_ads (VK Ads: сегмент×оффер×объявление воронка, 2 оси)')
    leads_agg = build_leads_agg_sql(conn)  # bare-колонки: status/reason/source_type/salon
    cur.execute("DROP TABLE IF EXISTS public.fact_vk_ads")
    cur.execute(f"""
        CREATE TABLE public.fact_vk_ads AS
        WITH vk_leads_id AS (
            -- id-несущие VK-лиды: utm_content = 'ad_group_id/banner_id' → воронка по кампании
            SELECT
                split_part(l.utm_content, '/', 1)::bigint AS ad_group_id,
                split_part(l.utm_content, '/', 2)::bigint AS banner_id,
                l.created_date AS created_date,
                l.arrival_date AS arrival_date,
                l.status       AS status,
                l.reason       AS reason,
                l.source_type  AS source_type,
                l.salon        AS salon
            FROM public.local_leads_all l
            WHERE l.utm_source = 'vkads'
              AND l.utm_content ~ '^[0-9]{{5,}}/[0-9]{{5,}}$'
              AND l.is_copy_for_removal IS NOT TRUE
        ),
        vk_leads_noid AS (
            -- VK-лиды БЕЗ id-utm_content (только utm_campaign='victory') → бакет «не определено»
            -- по салону (кампании/группы/объявления нет; salon есть). Не выбрасываем молча.
            SELECT
                l.created_date AS created_date,
                l.arrival_date AS arrival_date,
                l.status       AS status,
                l.reason       AS reason,
                l.source_type  AS source_type,
                l.salon        AS salon
            FROM public.local_leads_all l
            WHERE l.utm_source = 'vkads'
              AND (l.utm_content IS NULL OR l.utm_content !~ '^[0-9]{{5,}}/[0-9]{{5,}}$')
              AND l.is_copy_for_removal IS NOT TRUE
        ),
        zayavka_agg AS (
            SELECT ad_group_id, banner_id, created_date AS date,
        {leads_agg}
            FROM vk_leads_id
            WHERE created_date IS NOT NULL
            GROUP BY ad_group_id, banner_id, created_date
        ),
        visit_agg AS (
            SELECT ad_group_id, banner_id, arrival_date AS date,
        {leads_agg}
            FROM vk_leads_id
            WHERE arrival_date IS NOT NULL
            GROUP BY ad_group_id, banner_id, arrival_date
        ),
        bucket_zayavka_agg AS (
            SELECT salon AS салон, created_date AS date,
        {leads_agg}
            FROM vk_leads_noid
            WHERE created_date IS NOT NULL
            GROUP BY salon, created_date
        ),
        bucket_visit_agg AS (
            SELECT salon AS салон, arrival_date AS date,
        {leads_agg}
            FROM vk_leads_noid
            WHERE arrival_date IS NOT NULL
            GROUP BY salon, arrival_date
        ),
        salon_by_acc AS (
            -- account → салон + денормализованные атрибуты (регион/тип_сайта/специалист)
            -- из АВТОРИТЕТНОЙ строки домена, несущей vk_client_id этого VK-аккаунта.
            -- ⚠️ Атрибуты берём именно из vk_client_id-строки, а НЕ агрегатом по салону:
            -- у салона сотни доменов, site_type/directologist варьируются по доменам
            -- (distinct 3-6 / 5-21), и только регион салон-уровневый. Строка с vk_client_id —
            -- единственная детерминированная точка для VK-аккаунта.
            -- специалист (directologist) может быть пустым в источнике → NULL (напр. «Перформ РФ»).
            SELECT DISTINCT ON (NULLIF(TRIM(vk_client_id), '')::bigint)
                NULLIF(TRIM(vk_client_id), '')::bigint AS account_id,
                NULLIF(TRIM(salon), '')                AS салон,
                NULLIF(TRIM(region), '')               AS регион,
                NULLIF(TRIM(site_type), '')            AS тип_сайта,
                NULLIF(TRIM(directologist), '')        AS специалист
            FROM public.local_gsheet_sites
            WHERE niche = 'Авто' AND vk_client_id ~ '^[0-9]+$'
            ORDER BY NULLIF(TRIM(vk_client_id), '')::bigint, salon
        ),
        region_by_salon AS (
            -- салон → регион для бакет-строк (нет account_id → нет vk_client_id-строки).
            -- Регион салон-уровневый (distinct region на салон = 1) → детерминирован.
            -- тип_сайта/специалист на грани салона НЕ определимы (варьируются по доменам)
            -- → в бакете остаются NULL, регион здесь единственный корректный атрибут.
            SELECT DISTINCT ON (NULLIF(TRIM(salon), ''))
                NULLIF(TRIM(salon), '')  AS салон,
                NULLIF(TRIM(region), '') AS регион
            FROM public.local_gsheet_sites
            WHERE niche = 'Авто' AND NULLIF(TRIM(salon), '') IS NOT NULL
            ORDER BY NULLIF(TRIM(salon), ''), region NULLS LAST
        ),
        banner_dim AS (
            -- атрибуты объявления (стабильны по датам) — для визит-оси и lead-only заявок
            SELECT DISTINCT ON (banner_id)
                banner_id, account_id, ad_plan_id, ad_plan_name,
                ad_group_id, ad_group_name, banner_name
            FROM public.local_vk_ads_stats_day
            ORDER BY banner_id, date DESC
        )
        -- ── Ось «По дате заявки», основная: FULL OUTER JOIN(stats, лиды) ──
        -- lead-driven: КАЖДЫЙ id-лид попадает в воронку (даже если у banner не было spent в
        -- день заявки); КАЖДАЯ строка stats остаётся (расход сохраняется). Атрибуты lead-only
        -- строк добираются из banner_dim (по banner_id). spent несёт только сторона stats.
        SELECT
            COALESCE(s.date, za.date)::date               AS date,
            COALESCE(s.account_id, bd.account_id)::bigint AS account_id,
            sba.салон::text                               AS салон,
            COALESCE(s.ad_plan_id, bd.ad_plan_id)::bigint AS ad_plan_id,
            COALESCE(s.ad_plan_name, bd.ad_plan_name)::text AS ad_plan_name,
            COALESCE(s.ad_group_id, za.ad_group_id, bd.ad_group_id)::bigint AS ad_group_id,
            COALESCE(s.ad_group_name, bd.ad_group_name)::text AS ad_group_name,
            COALESCE(s.banner_id, za.banner_id)::bigint   AS banner_id,
            COALESCE(s.banner_name, bd.banner_name)::text AS banner_name,
            'По дате заявки'::text                        AS "атрибуция",
            COALESCE(s.shows, 0)::bigint                  AS shows,
            COALESCE(s.clicks, 0)::bigint                 AS clicks,
            COALESCE(s.spent, 0)::numeric(14,2)           AS spent,
            COALESCE(za.kol_vo_zayavok, 0)::bigint        AS заявки,
            COALESCE(za.korr, 0)::bigint                  AS заявки_корр,
            COALESCE(za.priedet, 0)::bigint               AS записи,
            COALESCE(za.kval, 0)::bigint                  AS квал,
            COALESCE(za.priezd, 0)::bigint                AS визиты,
            COALESCE(za.prodazhi, 0)::bigint              AS продажи,
            sba.регион::text                              AS регион,
            sba.тип_сайта::text                           AS тип_сайта,
            sba.специалист::text                          AS специалист
        FROM public.local_vk_ads_stats_day s
        FULL OUTER JOIN zayavka_agg za
               ON za.banner_id   = s.banner_id
              AND za.ad_group_id = s.ad_group_id
              AND za.date        = s.date
        LEFT JOIN banner_dim bd    ON bd.banner_id  = COALESCE(s.banner_id, za.banner_id)
        LEFT JOIN salon_by_acc sba ON sba.account_id = COALESCE(s.account_id, bd.account_id)

        UNION ALL

        -- ── Ось «По дате заявки», бакет «не определено»: VK-лиды без id-utm_content ──
        -- грань = салон × дата заявки; кампании/группы/объявления нет; рекламных метрик нет.
        SELECT
            bz.date::date                                 AS date,
            NULL::bigint                                  AS account_id,
            bz.салон::text                                AS салон,
            NULL::bigint                                  AS ad_plan_id,
            'не определено'::text                         AS ad_plan_name,
            NULL::bigint                                  AS ad_group_id,
            NULL::text                                    AS ad_group_name,
            NULL::bigint                                  AS banner_id,
            NULL::text                                    AS banner_name,
            'По дате заявки'::text                        AS "атрибуция",
            0::bigint                                     AS shows,
            0::bigint                                     AS clicks,
            0::numeric(14,2)                              AS spent,
            COALESCE(bz.kol_vo_zayavok, 0)::bigint        AS заявки,
            COALESCE(bz.korr, 0)::bigint                  AS заявки_корр,
            COALESCE(bz.priedet, 0)::bigint               AS записи,
            COALESCE(bz.kval, 0)::bigint                  AS квал,
            COALESCE(bz.priezd, 0)::bigint                AS визиты,
            COALESCE(bz.prodazhi, 0)::bigint              AS продажи,
            rbs.регион::text                              AS регион,
            NULL::text                                    AS тип_сайта,
            NULL::text                                    AS специалист
        FROM bucket_zayavka_agg bz
        LEFT JOIN region_by_salon rbs ON rbs.салон = bz.салон

        UNION ALL

        -- ── Ось «По дате визита», основная: спайн = визит-лиды; рекламные метрики = 0 (дедуп) ──
        SELECT
            va.date::date                         AS date,
            bd.account_id::bigint                 AS account_id,
            sba.салон::text                       AS салон,
            bd.ad_plan_id::bigint                 AS ad_plan_id,
            bd.ad_plan_name::text                 AS ad_plan_name,
            va.ad_group_id::bigint                AS ad_group_id,
            bd.ad_group_name::text                AS ad_group_name,
            va.banner_id::bigint                  AS banner_id,
            bd.banner_name::text                  AS banner_name,
            'По дате визита'::text                AS "атрибуция",
            0::bigint                             AS shows,
            0::bigint                             AS clicks,
            0::numeric(14,2)                      AS spent,
            COALESCE(va.kol_vo_zayavok, 0)::bigint AS заявки,
            COALESCE(va.korr, 0)::bigint          AS заявки_корр,
            COALESCE(va.priedet, 0)::bigint       AS записи,
            COALESCE(va.kval, 0)::bigint          AS квал,
            COALESCE(va.priezd, 0)::bigint        AS визиты,
            COALESCE(va.prodazhi, 0)::bigint      AS продажи,
            sba.регион::text                      AS регион,
            sba.тип_сайта::text                   AS тип_сайта,
            sba.специалист::text                  AS специалист
        FROM visit_agg va
        JOIN banner_dim bd ON bd.banner_id = va.banner_id
        LEFT JOIN salon_by_acc sba ON sba.account_id = bd.account_id

        UNION ALL

        -- ── Ось «По дате визита», бакет «не определено»: VK-лиды без id-utm_content ──
        SELECT
            bv.date::date                                 AS date,
            NULL::bigint                                  AS account_id,
            bv.салон::text                                AS салон,
            NULL::bigint                                  AS ad_plan_id,
            'не определено'::text                         AS ad_plan_name,
            NULL::bigint                                  AS ad_group_id,
            NULL::text                                    AS ad_group_name,
            NULL::bigint                                  AS banner_id,
            NULL::text                                    AS banner_name,
            'По дате визита'::text                        AS "атрибуция",
            0::bigint                                     AS shows,
            0::bigint                                     AS clicks,
            0::numeric(14,2)                              AS spent,
            COALESCE(bv.kol_vo_zayavok, 0)::bigint        AS заявки,
            COALESCE(bv.korr, 0)::bigint                  AS заявки_корр,
            COALESCE(bv.priedet, 0)::bigint               AS записи,
            COALESCE(bv.kval, 0)::bigint                  AS квал,
            COALESCE(bv.priezd, 0)::bigint                AS визиты,
            COALESCE(bv.prodazhi, 0)::bigint              AS продажи,
            rbs.регион::text                              AS регион,
            NULL::text                                    AS тип_сайта,
            NULL::text                                    AS специалист
        FROM bucket_visit_agg bv
        LEFT JOIN region_by_salon rbs ON rbs.салон = bv.салон
    """)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_fact_vk_ads_date ON public.fact_vk_ads (date)')
    cur.execute('ANALYZE public.fact_vk_ads')


def build_ml_korrektirovki_fact(cur):
    """ML_KORREKTIROVKI_FACT_2026-07-27 — датамарт ML-аудиторных корректировок ставок.

    Источники:
      • public.fact_big_analytics (ключ: RlAdjustmentId) — все колонки воронки/расхода/срезов
      • public.yandex_direct_korrektirovki — фильтр korrektirovki_bid ILIKE '%_ml_%'

    JOIN: fact_big_analytics.RlAdjustmentId = yandex_direct_korrektirovki.audience_id.
    Это рабочий, верифицированный в проекте ключ (CLAUDE.md, корректировки PBI-отчёта).

    Гранулярность: как у fact_big_analytics — только строки с RlAdjustmentId, матчащимся
    в ML-аудиторию. DISTINCT ON audience_id в CTE ml_korr гарантирует 1:1 (нет умножения
    строк при одной аудитории в нескольких кампаниях).

    Новые колонки:
      ml_audience_name — modifier_name из korrektirovki (читаемое имя аудитории)
      bid_percent      — процент корректировки ставки (TEXT, напр. '+20%')
      ml_tier          — уровень из имени аудитории: regex '_ml_(\d+p)' → '5p'/'10p'/...;
                         NULL если паттерн не найден (надёжный разбор по заданию)

    TABLE (не VIEW): JOIN присутствует → правило CLAUDE.md §«VIEW вместо TABLE».
    Non-fatal guard: если yandex_direct_korrektirovki отсутствует — создаётся пустая таблица
    (korrektirovki/run.py ещё не запускался / деградация без краша build_star).

    Порядок вызова: ПОСЛЕ build_fact() (fact_big_analytics должна существовать и быть актуальной).
    """
    log.info('[6c] public.fact_ml_korrektirovki (ML-аудитории × воронка)')

    # GUARD: fact_big_analytics должна существовать (build_fact обязан отработать до нас)
    cur.execute("SELECT to_regclass('public.fact_big_analytics')::text")
    if cur.fetchone()[0] is None:
        raise RuntimeError(
            'build_ml_korrektirovki_fact: public.fact_big_analytics не существует — '
            'build_fact() должен отработать до вызова этой функции.'
        )

    # GUARD: yandex_direct_korrektirovki. Если отсутствует — создаём пустую таблицу
    # (деградация без краша: korrektirovki/run.py ещё не запускался на этом инстансе).
    cur.execute("SELECT to_regclass('public.yandex_direct_korrektirovki')::text")
    _korr_exists = cur.fetchone()[0] is not None

    cur.execute('DROP TABLE IF EXISTS public.fact_ml_korrektirovki')

    if not _korr_exists:
        log.warning(
            'ML_KORREKTIROVKI: public.yandex_direct_korrektirovki отсутствует '
            '(korrektirovki/run.py ещё не запускался?) — создаём пустую fact_ml_korrektirovki'
        )
        cur.execute("""
            CREATE TABLE public.fact_ml_korrektirovki AS
            SELECT
                f.*,
                NULL::text  AS ml_audience_name,
                NULL::text  AS bid_percent,
                NULL::text  AS ml_tier
            FROM public.fact_big_analytics f
            WHERE false
        """)
    else:
        cur.execute("""
            CREATE TABLE public.fact_ml_korrektirovki AS
            WITH ml_korr AS (
                -- DISTINCT ON audience_id: гарантирует 1:1 джойн (нет умножения строк).
                -- Одна ML-аудитория может применяться в нескольких кампаниях с разным
                -- bid_percent; берём детерминированно первый по алфавиту bid_percent.
                -- modifier_name — само имя аудитории (не зависит от кампании).
                SELECT DISTINCT ON (audience_id)
                    audience_id,
                    modifier_name,
                    bid_percent,
                    korrektirovki_bid
                FROM public.yandex_direct_korrektirovki
                WHERE korrektirovki_bid ILIKE '%_ml_%'
                ORDER BY audience_id, bid_percent NULLS LAST
            )
            SELECT
                f.*,
                k.modifier_name                                       AS ml_audience_name,
                k.bid_percent,
                (regexp_match(k.modifier_name, '_ml_all_(\\d+p)'))[1] AS ml_tier
            FROM public.fact_big_analytics f
            JOIN ml_korr k ON f."RlAdjustmentId" = k.audience_id
        """)

    cur.execute(
        'CREATE INDEX IF NOT EXISTS idx_fact_ml_korr_date '
        'ON public.fact_ml_korrektirovki USING brin ("Date")'
    )
    cur.execute('ANALYZE public.fact_ml_korrektirovki')
    cur.execute('SELECT COUNT(*) FROM public.fact_ml_korrektirovki')
    n = cur.fetchone()[0]
    log.info('  fact_ml_korrektirovki OK: %d строк', n)


def index_and_analyze(cur):
    log.info('[7] Индексы (BRIN на Date) + VACUUM ANALYZE')
    # INDEX_AUDIT_2026-06-27: удалены мёртвые индексы (idx_scan=0 по pg_stat_user_indexes):
    #   idx_star_fact_dom (domain), idx_star_fact_attr (атрибуция), idx_star_fact_src (_source_table).
    # idx_star_fact_date (BRIN) сохранён — 24 scans.
    stmts = [
        'CREATE INDEX IF NOT EXISTS idx_star_fact_date  ON public.fact_big_analytics USING brin ("Date")',
        # idx_star_arp_* удалены — public.arp_fact теперь VIEW, индексы неприменимы (2026-06-09)
    ]
    for s in stmts:
        cur.execute(s)


def main():
    conn = connect(); cur = conn.cursor()
    cur.execute(f"SET work_mem = '{WORK_MEM}'")
    t0 = time.perf_counter()

    # BUILD_UNIFIED_FRESHNESS_LOG_2026-07-16 (v2 rework): аудит свежести build_unified при каждом запуске build_star.
    # Blocker 1 fix: _step1_freshness (3-ч порог) НЕ поймал бы инцидент:
    # step1=07:21 UTC, standalone build_star=09:43 UTC → 2.4ч < 3.0ч → ложный "OK".
    # Правильный артефакт: последняя успешная запись build_unified в data_quality_log.
    # В нормальном pipeline build_unified завершается за минуты до build_star → age < 1ч.
    # При standalone-запуске поверх устаревшего unified → age >> 3ч → PARTIAL_RUN_RISK.
    # Этот guard НЕ блокирует build_star — только ВИДИМОСТЬ. Блокировка PBI — в BUILD_UNIFIED_GATE
    # (pipeline_powerbi.py).
    import datetime as _dt_bs
    try:
        cur.execute(
            "SELECT MAX(run_at) FROM public.data_quality_log "
            "WHERE step = 'build_unified' AND status = 'ok'"
        )
        _buni_row = cur.fetchone()
        _buni_ts = _buni_row[0] if _buni_row and _buni_row[0] is not None else None
        _now_utc_bs = _dt_bs.datetime.utcnow()
        _buni_age_h = (
            (_now_utc_bs - _buni_ts).total_seconds() / 3600
            if _buni_ts is not None else 9999.0
        )
        _buni_stale = _buni_age_h > 3.0
        _buni_ts_str = _buni_ts.strftime('%Y-%m-%d %H:%M:%S') if _buni_ts else 'NULL'
        if _buni_stale:
            log.error(
                'BUILD_UNIFIED_FRESHNESS_LOG: build_unified STALE (%.1f ч назад, %s UTC) — '
                'build_star строит star из УСТАРЕВШИХ big_analytics_unified данных '
                '(PARTIAL RUN?). Блокировка PBI refresh — в BUILD_UNIFIED_GATE '
                '(pipeline_powerbi.py).',
                _buni_age_h, _buni_ts_str,
            )
        else:
            log.info(
                'BUILD_UNIFIED_FRESHNESS_LOG: build_unified свежий — %s UTC (%.1f ч назад) — OK',
                _buni_ts_str, _buni_age_h,
            )
        # Аудит: пишем в data_quality_log (если таблица существует)
        cur.execute("SELECT to_regclass('public.data_quality_log')")
        if cur.fetchone()[0] is not None:
            cur.execute(
                "INSERT INTO public.data_quality_log "
                "(run_id, run_at, step, status, rows_affected, duration_sec, details) "
                "VALUES (gen_random_uuid()::text, NOW(), %s, %s, 0, 0, %s)",
                (
                    'build_star_unified_freshness_check',
                    'PARTIAL_RUN_RISK' if _buni_stale else 'OK',
                    f'build_unified_last_ok={_buni_ts_str}, age_h={_buni_age_h:.2f}, stale={_buni_stale}',
                ),
            )
            conn.commit()
    except Exception as _s1g_e:
        log.warning('BUILD_UNIFIED_FRESHNESS_LOG: ошибка: %s', _s1g_e)
    conn.rollback()  # очищаем незакоммиченное от проверки перед UNIFIED_GUARD

    # UNIFIED_GUARD_2026-06-27: проверяем COUNT(big_analytics_unified) ДО первого DROP.
    # Если unified пуст или мал — abort; старые Dim_*/fact_big_analytics СОХРАНЕНЫ.
    # Порог 500 000 — защита от каскада (disk fail → unified=0 → star дропает живые данные).
    _MIN_UNIFIED_ROWS = 500_000
    import logging as _g_log
    _ug_logger = _g_log.getLogger(__name__)
    try:
        cur.execute("SELECT COUNT(*) FROM public.big_analytics_unified")
        _uni_cnt = cur.fetchone()[0]
    except Exception as _ug_e:
        conn.close()
        raise RuntimeError(
            f'build_star UNIFIED_GUARD: не удалось прочитать big_analytics_unified: {_ug_e}. '
            f'Star НЕ перестраивается, старые данные СОХРАНЕНЫ.'
        ) from _ug_e
    if _uni_cnt < _MIN_UNIFIED_ROWS:
        conn.close()
        raise RuntimeError(
            f'build_star UNIFIED_GUARD: big_analytics_unified = {_uni_cnt:,} строк '
            f'(порог {_MIN_UNIFIED_ROWS:,}) — star НЕ перестраивается, '
            f'старые Dim_*/fact_big_analytics СОХРАНЕНЫ. '
            f'Запустите build_unified.py и убедитесь что unified корректен.'
        )
    _ug_logger.info(
        'UNIFIED_GUARD: big_analytics_unified = %d строк (порог %d) — OK, строим star',
        _uni_cnt, _MIN_UNIFIED_ROWS
    )

    build_schema(cur);   conn.commit()
    build_dim_date(cur); conn.commit()
    build_dim_campaign(cur); conn.commit()
    build_dim_adgroup(cur);  conn.commit()
    build_dim_site(cur);     conn.commit()
    build_dim_adjustment(cur); conn.commit()
    build_dim_location(cur);   conn.commit()
    build_dim_distance(cur);   conn.commit()
    build_fact(cur);         conn.commit()
    build_arp_fact(cur);     conn.commit()
    build_vk_ads_fact(conn, cur); conn.commit()  # VK_ADS_FACT_2026-07-10
    build_ml_korrektirovki_fact(cur); conn.commit()  # ML_KORREKTIROVKI_FACT_2026-07-27
    build_pbi_big_analytics_full_view(cur); conn.commit()
    index_and_analyze(cur);  conn.commit()

    # Anti-regression: контракт колонок Power BI (падает ДО конца сборки, если уронили
    # колонку, нужную моделям big_analytics_full / analytics_report_placement).
    verify_model_coverage(cur); conn.commit()

    # VACUUM ANALYZE (autocommit). PARALLEL 0 + max_parallel_maintenance_workers=0 —
    # иначе параллельные воркеры падают на shared memory (/dev/shm) DiskFull.
    conn.autocommit = True
    cur.execute('SET max_parallel_maintenance_workers = 0')
    cur.execute("SET maintenance_work_mem = '256MB'")
    # public.arp_fact исключён из VACUUM — это VIEW (VACUUM на view падает), 2026-06-09
    for rel in ('public."Dim_Date"', 'public."Dim_Campaign"', 'public."Dim_AdGroup"',
                'public."Dim_Site"', 'public."Dim_Adjustment"', 'public."Dim_Location"',
                'public."Dim_Distance"', 'public.fact_big_analytics',
                'public.fact_ml_korrektirovki'):  # ML_KORREKTIROVKI_FACT_2026-07-27
        cur.execute(f'VACUUM (ANALYZE, PARALLEL 0) {rel}')
    conn.autocommit = False

    log.info('=== РАЗМЕРЫ / СТРОКИ ===')
    for rel in ('public."Dim_Date"', 'public."Dim_Campaign"', 'public."Dim_AdGroup"',
                'public."Dim_Site"', 'public."Dim_Adjustment"', 'public."Dim_Location"',
                'public."Dim_Distance"', 'public.fact_big_analytics', 'public.arp_fact',
                'public.fact_vk_ads', 'public.fact_ml_korrektirovki',  # ML_KORREKTIROVKI_FACT_2026-07-27
                'public.pbi_big_analytics_full'):
        log.info('  %-32s %s', rel, size_of(cur, rel))

    log.info('ГОТОВО за %.1f сек', time.perf_counter() - t0)
    conn.close()


if __name__ == '__main__':
    main()
