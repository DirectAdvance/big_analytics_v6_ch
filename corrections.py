"""
corrections.py — ручные корректировки данных перед сборкой big_analytics_full

Применяется в pipeline.py между шагом 3 (build_sources) и шагом 4 (build_full).
Вызов: corrections.apply(conn)

Порядок применения:
  0a → 0b → 0d → 0c → 1 → 2 → 3 → 5 → 4 → 4б → 6 → 7 → 8

══════════════════════════════════════════════════════════════════════════════
ПРАВИЛО 0a: CampaignName DISTINCT ON — распространение последнего кода кампании
══════════════════════════════════════════════════════════════════════════════
Для каждого CampaignId берёт campaign_code/tp/cpc_cpa/site_quiz
из строки с максимальной Date и обновляет все строки того же CampaignId,
у которых код NULL или 'неверный кодер'. Исправляет случаи смены
CampaignName в Директе — старые строки получают актуальный код.

══════════════════════════════════════════════════════════════════════════════
ПРАВИЛО 0b: Нормализация campaign_code и adgroup_code (из v3 yd_normalized)
══════════════════════════════════════════════════════════════════════════════
campaign_code : kviz→quiz, Kviz→Quiz, CYR_С→c, tp8_cpa_site→tp8_cpc_site
adgroup_code  : ct00173→ct0173, ct09_ag→ct009_ag, ag0011→ag011,
                _ct0018_ag→_ct018_ag, _n000_n000_→_n000_, _off_→_aoff_,
                ([^_])(aon|aoff)→\1_\2, _g0$→_g00, (_g\\d\\d)\\S+→\1

══════════════════════════════════════════════════════════════════════════════
ПРАВИЛО 0d: Миграция устаревших ct-кодов площадок
══════════════════════════════════════════════════════════════════════════════
До пересчёта ag_parts заменяет старые ct-коды на актуальные из листа «Нейминг»
в AdGroupName (до разделения ' — ') и уже извлечённом adgroup_code.
Применяется к big_analytics_direct и big_analytics_crop_targeting.

══════════════════════════════════════════════════════════════════════════════
ПРАВИЛО 0c: Глобальный пересчёт ag_part1..7 и неверный_кодер_new
══════════════════════════════════════════════════════════════════════════════
После 0b/0d обновляет ag_part1..7 для ВСЕХ строк с валидным adgroup_code
(не только для _ADGROUP_FIX_ACCOUNTS), JOIN с local_gsheet_naming.

══════════════════════════════════════════════════════════════════════════════
ПРАВИЛО 1: Кудерко Семен — переназначение директолога до 10.04.2026
══════════════════════════════════════════════════════════════════════════════
Таблица : public.big_analytics_direct
Условие : "Date" < '2026-04-10'
          account_login IN (список ниже)
Действие: SET директолог = 'Кудерко Семен'

Аккаунты:
  e-20086621, e-20086622, e-20084860, e-20084861, e-20086619,
  porg-gcegsszl, e-20086660, e-20086659, porg-h27zek57, e-20086658,
  e-20077075, e-20086657, porg-edmpebhr, e-20086620, e-20084857,
  e-20086661, e-20086623, e-20084859, e-20084858, e-20076544,
  e-20076545, porg-kkhtgf2u, e-20074366, porg-mgrauofh, e-20078432,
  e-20077077, e-20077078, e-20078433, e-20078430, e-20077079,
  e-20078431, e-20077076, e-20078429, porg-7yibjfp4, e-20076541,
  e-20074364, porg-pzm4243t, porg-riga5gvo, porg-sblzprjm, porg-xagqvz3v,
  porg-gbj6e3ji, porg-3q2n22ux, porg-x7iyctbh, porg-qruhft2a, porg-53t6ygdz,
  porg-qeyeclqv, porg-iljlldjs, porg-wlta5kmb, porg-cs34qdr7, porg-cz2jqzbo,
  e-20074363, e-20076540, porg-uguxrece, porg-jnbd47au, porg-klfzrvhu,
  porg-v6ao2xka, porg-jelgic43, e-20076539, e-20074365, porg-nen5jouv,
  porg-vvxm6gma, porg-rcg54tv4, porg-bczfmt3d, porg-tr47xrja, porg-5v4n6spu,
  porg-p4uskpj6, porg-wgyzlarl

══════════════════════════════════════════════════════════════════════════════
ПРАВИЛО 2: Исправление AdGroupName — опечатки в кодировке групп (из v3)
══════════════════════════════════════════════════════════════════════════════
Таблица : public.big_analytics_direct
Источник: big_analytics_full_v3.py (account-specific UPDATE перед извлечением adgroup_code)

Аккаунты и замены:
  e-20076545, e-20074366 : _g011_g  → _ag011_g
  e-20076545             : _off_    → _aoff_  (только если нет _aoff_ в строке)
  cars-yekaterinburg-541349-lrqf : _n000_n000_ → _n000_
  porg-akiqrh6u          : ct0054aoff/aon, ct0076aoff/aon → добавляет _ перед aoff/aon
  e-20085128/130/132/135,
  e-20086083             : 'Новая группа' → 'ct0021_aon_n000_r0121_ct001_ag011_g00 — 3. baic u5 plus'
  porg-p7pymm3m          : 'Новая группа' → 'ct0000_aon_n000_r0017_ct001_ag011_g00 — общая'

══════════════════════════════════════════════════════════════════════════════
ПРАВИЛО 3: Переизвлечение adgroup_code из исправленного AdGroupName
══════════════════════════════════════════════════════════════════════════════
После Rule 2 перевычисляет adgroup_code для затронутых аккаунтов,
где он был NULL или 'неверный кодер'.

══════════════════════════════════════════════════════════════════════════════
ПРАВИЛО 4: Пересчёт ag_part1..7 и неверный_кодер_new (для конкретных аккаунтов)
══════════════════════════════════════════════════════════════════════════════
После Rule 3 обновляет ag_part1..7 и "неверный_кодер_new" через JOIN
с local_gsheet_naming для строк с исправленным adgroup_code.

══════════════════════════════════════════════════════════════════════════════
ПРАВИЛО 4б: Пересчёт ag_part1..7 для big_analytics_crop_targeting
══════════════════════════════════════════════════════════════════════════════
После Rule 5 (которое исправляет adgroup_code в crop_targeting: ct018_... → ct0000_...)
пересчитывает ag_part1..7 и "неверный_кодер_new" для big_analytics_crop_targeting.
Без этого шага crop_targeting получает правильный adgroup_code но старые ag_parts → неверный_кодер.
Rule 4 (_recompute_ag_parts) обновляет только big_analytics_direct, поэтому
для crop_targeting требуется отдельный проход.

══════════════════════════════════════════════════════════════════════════════
ПРАВИЛО 5: Прямые маппинги adgroup_code по аккаунту (раздел 5б из v3)
══════════════════════════════════════════════════════════════════════════════

══════════════════════════════════════════════════════════════════════════════
ПРАВИЛО 6: Фильтр валидности campaign_code/tp/cpc_cpa
══════════════════════════════════════════════════════════════════════════════
campaign_code не совпадающий с ^tp\\d+_(cpc|cpa)_(site|quiz)$ → 'неверный кодер'
Каскад: tp и cpc_cpa тоже устанавливаются в 'неверный кодер'.

══════════════════════════════════════════════════════════════════════════════
ПРАВИЛО 7: Заполнение big_analytics_pixel (pixel-лиды)
══════════════════════════════════════════════════════════════════════════════
Пересоздаёт T_PIXEL с реальными данными (убирает WHERE 1=0 заглушку из step3).

══════════════════════════════════════════════════════════════════════════════
ПРАВИЛО 8: Классификация utm_source для unmatched-лидов
══════════════════════════════════════════════════════════════════════════════
В v5 victory_*/telegram лиды исключаются из leads_direct на уровне step3
и попадают в отдельные таблицы (big_analytics_pixel, big_analytics_telegram).
Правило применяется как дополнительная страховка: обновляет строки
big_analytics_direct с источник IS NULL через JOIN с raw_leads по key3.
"""

import logging

from config.settings import (
    T_GSHEET_NAMING, T_PIXEL, T_RAW_LEADS, T_GSHEET_SITES,
)
from config.status_sql import load_status_sql

# Глобальные алиасы салонов: неверное написание → правильное
SALON_ALIASES: dict[str, str] = {
    'Центр Авто Казань': 'Казань Центр Авто',
    'АЦ Кит-Авто':      'Кит-Авто',
    'АЦ на Жукова':     'Автоцентр на Жукова',
    'АвтоПарк Южный':   'Автопарк Южный',
    'М-Авто':           'М-авто',
    'Южный обход':      'Южный Обход',
}

# Домены без записи в local_gsheet_sites → (салон, город, регион)
DOMAIN_SITE_MAP: dict[str, tuple[str, str, str]] = {
    'autotorg-ekb.ru': ('Кит-Авто',   'Екатеринбург', 'Свердловская область'),
    'buymashina-e.ru': ('АвтоМаркет', 'Екатеринбург', 'Свердловская область'),
    'probeg-ek.ru':    ('АвтоМаркет', 'Екатеринбург', 'Свердловская область'),
}

# Обратная совместимость
DOMAIN_SALON_MAP: dict[str, str] = {k: v[0] for k, v in DOMAIN_SITE_MAP.items()}

# Компонентные таблицы, собираемые step3 (до сборки big_analytics_full в step6)
COMPONENT_TABLES: list[str] = [
    'big_analytics_direct',
    'big_analytics_seo',
    'big_analytics_pixel',
    'big_analytics_reviews',
    'big_analytics_crop_targeting',
]

logger = logging.getLogger('corrections')

# Кириллическая 'с' — опечатка в cpc/cpa полях Яндекс
_CYR_S = '\u0441'


# ── Единый источник истины: word-sort ключ матчинга салонов ─────────────
#
# ПРАВИЛО ПРОЕКТА: матчинг названий салонов выполняется ТОЛЬКО через этот ключ.
# Прямое exact-сравнение имени салона (LOWER(TRIM(a)) = LOWER(TRIM(b))) ЗАПРЕЩЕНО —
# оно ломается при перестановке слов.
#
# Ключ детерминирован (НЕ fuzzy): lower -> trim -> пунктуация в пробел ->
# схлоп пробелов -> слова -> сорт по алфавиту -> склейка.
# Любая перестановка слов даёт ОДИН ключ -> канонизируется автоматически.
# Ручные SALON_ALIASES — только для НЕ-перестановочных случаев (опечатки/синонимы),
# где набор слов разный.
#
# Коллизии (два разных салона -> один ключ) проверены = 0 на local_gsheet_sites
# и local_gsheet_autosalony_clients (2026-06-10). При добавлении салонов —
# перепроверять, что авто-канонизация не схлопывает два разных салона.

def salon_match_key(col_sql: str) -> str:
    """Возвращает SQL-выражение word-sort ключа для столбца с именем салона.

    col_sql — SQL-текст столбца (напр. 'f."салон"', 'd.салон', 'gs.salon').
    Использовать в JOIN/GROUP BY вместо exact LOWER(TRIM(...)) сравнения.
    """
    return (
        "array_to_string("
        "ARRAY(SELECT t.w FROM unnest(string_to_array("
        "regexp_replace("
        f"regexp_replace(LOWER(TRIM({col_sql})), '[^[:alnum:][:space:]]', ' ', 'g'),"
        r" '\s+', ' ', 'g'"
        ")"
        ", ' ')) AS t(w) WHERE t.w <> '' ORDER BY t.w)"
        ", ' ')"
    )


# ── Правило 0a ────────────────────────────────────────────────────────────────

def _rule0a_campaign_distinct(conn) -> int:
    """Распространяет последний валидный campaign_code на все строки того же CampaignId."""
    with conn.cursor() as cur:
        cur.execute("""
            WITH latest AS (
                SELECT DISTINCT ON ("CampaignId")
                    "CampaignId", campaign_code, tp, cpc_cpa, site_quiz
                FROM public.big_analytics_direct
                WHERE "CampaignId" IS NOT NULL
                  AND campaign_code IS NOT NULL
                  AND campaign_code != 'неверный кодер'
                ORDER BY "CampaignId", "Date" DESC NULLS LAST
            )
            UPDATE public.big_analytics_direct d
            SET campaign_code = l.campaign_code,
                tp            = l.tp,
                cpc_cpa       = l.cpc_cpa,
                site_quiz     = l.site_quiz
            FROM latest l
            WHERE d."CampaignId" = l."CampaignId"
              AND d."CampaignId" IS NOT NULL
              AND (d.campaign_code IS NULL
                OR d.campaign_code = 'неверный кодер'
                OR d.campaign_code != l.campaign_code)
        """)
        n = cur.rowcount
    conn.commit()
    logger.info('[corrections] Правило 0a (CampaignName DISTINCT ON): %d строк', n)
    return n


# ── Правило 0b ────────────────────────────────────────────────────────────────

def _rule0b_normalize_codes(conn) -> int:
    """Нормализует campaign_code и adgroup_code по цепочкам замен из v3."""
    total = 0
    with conn.cursor() as cur:
        # campaign_code: kviz→quiz, Kviz→Quiz, кириллическая с→c, tp8_cpa_site→tp8_cpc_site
        cur.execute(
            """
            UPDATE public.big_analytics_direct
            SET campaign_code = REPLACE(REPLACE(REPLACE(REPLACE(campaign_code,
                'kviz', 'quiz'),
                'Kviz', 'Quiz'),
                %s, 'cpc'),
                'tp8_cpa_site', 'tp8_cpc_site')
            WHERE campaign_code IS NOT NULL
              AND campaign_code != 'неверный кодер'
              AND (campaign_code LIKE '%%kviz%%'
                OR campaign_code LIKE '%%Kviz%%'
                OR campaign_code LIKE %s
                OR campaign_code LIKE '%%tp8_cpa_site%%')
            """,
            ('cp' + _CYR_S, '%cp' + _CYR_S + '%'),
        )
        total += cur.rowcount

        # adgroup_code: нормализация опечаток и лишних символов (из v3 yd_normalized)
        cur.execute(r"""
            UPDATE public.big_analytics_direct
            SET adgroup_code = REGEXP_REPLACE(REGEXP_REPLACE(REGEXP_REPLACE(
                REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(adgroup_code,
                    'ct00173',     'ct0173'),
                    'ct09_ag',     'ct009_ag'),
                    'ag0011',      'ag011'),
                    '_ct0018_ag',  '_ct018_ag'),
                    '_n000_n000_', '_n000_'),
                    '_off_',       '_aoff_'),
                '([^_])(aon|aoff)', $$\1_\2$$, 'g'),
                '_g0$', '_g00'),
                '(_g[0-9]{2})\S+', $$\1$$)
            WHERE adgroup_code IS NOT NULL
              AND adgroup_code != 'неверный кодер'
        """)
        total += cur.rowcount

    conn.commit()
    logger.info('[corrections] Правило 0b (нормализация кодов): %d строк', total)
    return total


# ── Общий хелпер пересчёта ag_parts ──────────────────────────────────────────

_AG_CODE_PATTERN = r'^ct[0-9]+_(aoff|aon)_n[0-9]+_r[0-9]+_ct[0-9]+_ag[0-9]+_g[0-9]+$'


def _recompute_ag_parts(conn, account_filter=None) -> int:
    """Пересчитывает ag_part1..7 и неверный_кодер_new через JOIN с local_gsheet_naming.

    account_filter=None → обновить все строки (Rule 0c).
    account_filter=list → обновить только указанные аккаунты (Rule 4).

    Оптимизация: вместо UPDATE-by-ctid с 7 LEFT JOIN на 2.7M строк —
    сначала строим lookup для УНИКАЛЬНЫХ adgroup_code (~15k значений),
    затем UPDATE через JOIN по adgroup_code.
    Для tp6/tp7 — отдельный быстрый UPDATE без JOIN'ов.
    """
    nm = T_GSHEET_NAMING
    pat = _AG_CODE_PATTERN

    if account_filter is not None:
        acc_filter_sql = 'AND d2.account_login = ANY(%s)'
        upd_filter_sql = 'AND d.account_login = ANY(%s)'
        params_lookup = (account_filter,)
        params_update = (account_filter,)
        params_tp67   = (account_filter,)
    else:
        acc_filter_sql = ''
        upd_filter_sql = ''
        params_lookup = ()
        params_update = ()
        params_tp67   = ()

    total = 0
    with conn.cursor() as cur:
        # 1. Lookup для уникальных adgroup_code (НЕ для tp6/tp7)
        cur.execute(f"""
            DROP TABLE IF EXISTS _tmp_ag_parts_lookup;
            CREATE UNLOGGED TABLE _tmp_ag_parts_lookup AS
            WITH unique_codes AS (
                SELECT DISTINCT d2.adgroup_code
                FROM public.big_analytics_direct d2
                WHERE d2.adgroup_code IS NOT NULL
                  AND d2.adgroup_code != 'неверный кодер'
                  {acc_filter_sql}
            )
            SELECT
                u.adgroup_code,
                CASE WHEN u.adgroup_code ~ '{pat}'
                     THEN COALESCE(SPLIT_PART(u.adgroup_code,'_',1)||' - '||n1.name,
                                   SPLIT_PART(u.adgroup_code,'_',1))
                     ELSE 'неверный кодер' END AS ag_part1,
                CASE WHEN u.adgroup_code ~ '{pat}'
                     THEN COALESCE(SPLIT_PART(u.adgroup_code,'_',2)||' - '||n2.name,
                                   SPLIT_PART(u.adgroup_code,'_',2))
                     ELSE 'неверный кодер' END AS ag_part2,
                CASE WHEN u.adgroup_code ~ '{pat}'
                     THEN COALESCE(SPLIT_PART(u.adgroup_code,'_',3)||' - '||n3.name,
                                   SPLIT_PART(u.adgroup_code,'_',3))
                     ELSE 'неверный кодер' END AS ag_part3,
                CASE WHEN u.adgroup_code ~ '{pat}'
                     THEN COALESCE(SPLIT_PART(u.adgroup_code,'_',4)||' - '||n4.name,
                                   SPLIT_PART(u.adgroup_code,'_',4))
                     ELSE 'неверный кодер' END AS ag_part4,
                CASE WHEN u.adgroup_code ~ '{pat}'
                     THEN COALESCE(SPLIT_PART(u.adgroup_code,'_',5)||' - '||n5.name,
                                   SPLIT_PART(u.adgroup_code,'_',5))
                     ELSE 'неверный кодер' END AS ag_part5,
                CASE WHEN u.adgroup_code ~ '{pat}'
                     THEN COALESCE(SPLIT_PART(u.adgroup_code,'_',6)||' - '||n6.name,
                                   SPLIT_PART(u.adgroup_code,'_',6))
                     ELSE 'неверный кодер' END AS ag_part6,
                CASE WHEN u.adgroup_code ~ '{pat}'
                     THEN COALESCE(SPLIT_PART(u.adgroup_code,'_',7)||' - '||n7.name,
                                   SPLIT_PART(u.adgroup_code,'_',7))
                     ELSE 'неверный кодер' END AS ag_part7,
                CASE
                    WHEN u.adgroup_code !~ '{pat}' THEN 'неверный кодер'
                    WHEN n1.name IS NOT NULL
                     AND n2.name IS NOT NULL
                     AND n3.name IS NOT NULL
                     AND n4.name IS NOT NULL
                     AND n5.name IS NOT NULL
                     AND n6.name IS NOT NULL
                     AND n7.name IS NOT NULL                  THEN 'верный кодер'
                    ELSE 'неверный кодер'
                END AS verdict
            FROM unique_codes u
            LEFT JOIN {nm} n1 ON n1.type = 'ag_part1' AND LOWER(SPLIT_PART(u.adgroup_code,'_',1)) = n1.code
            LEFT JOIN {nm} n2 ON n2.type = 'ag_part2' AND LOWER(SPLIT_PART(u.adgroup_code,'_',2)) = n2.code
            LEFT JOIN {nm} n3 ON n3.type = 'ag_part3' AND LOWER(SPLIT_PART(u.adgroup_code,'_',3)) = n3.code
            LEFT JOIN {nm} n4 ON n4.type = 'ag_part4' AND LOWER(SPLIT_PART(u.adgroup_code,'_',4)) = n4.code
            LEFT JOIN {nm} n5 ON n5.type = 'ag_part5' AND LOWER(SPLIT_PART(u.adgroup_code,'_',5)) = n5.code
            LEFT JOIN {nm} n6 ON n6.type = 'ag_part6' AND LOWER(SPLIT_PART(u.adgroup_code,'_',6)) = n6.code
            LEFT JOIN {nm} n7 ON n7.type = 'ag_part7' AND LOWER(SPLIT_PART(u.adgroup_code,'_',7)) = n7.code;
            CREATE INDEX ON _tmp_ag_parts_lookup (adgroup_code);
            ANALYZE _tmp_ag_parts_lookup;
        """, params_lookup)

        # 2. UPDATE строк где tp NOT IN tp6/tp7 — JOIN по adgroup_code
        cur.execute(f"""
            UPDATE public.big_analytics_direct d
            SET ag_part1 = l.ag_part1,
                ag_part2 = l.ag_part2,
                ag_part3 = l.ag_part3,
                ag_part4 = l.ag_part4,
                ag_part5 = l.ag_part5,
                ag_part6 = l.ag_part6,
                ag_part7 = l.ag_part7,
                "неверный_кодер_new" = l.verdict
            FROM _tmp_ag_parts_lookup l
            WHERE d.adgroup_code = l.adgroup_code
              AND d.adgroup_code IS NOT NULL
              AND d.adgroup_code != 'неверный кодер'
              AND (d.tp NOT IN ('tp6','tp7') OR d.tp IS NULL)
              {upd_filter_sql}
        """, params_update)
        total += cur.rowcount

        # 3. UPDATE строк где tp IN tp6/tp7 — все ag_parts = 'MK/TK', verdict = NULL
        # adgroup_code заменяется на 'MK/TK' если был NULL/пуст/'неверный кодер'
        cur.execute(f"""
            UPDATE public.big_analytics_direct d
            SET adgroup_code = CASE
                    WHEN d.adgroup_code IS NULL OR d.adgroup_code = '' OR d.adgroup_code = 'неверный кодер'
                        THEN 'MK/TK'
                    ELSE d.adgroup_code
                END,
                ag_part1 = 'MK/TK', ag_part2 = 'MK/TK', ag_part3 = 'MK/TK',
                ag_part4 = 'MK/TK', ag_part5 = 'MK/TK', ag_part6 = 'MK/TK',
                ag_part7 = 'MK/TK',
                "неверный_кодер_new" = NULL
            WHERE d.tp IN ('tp6','tp7')
              AND d.adgroup_code IS NOT NULL
              AND d.adgroup_code != 'неверный кодер'
              {upd_filter_sql}
        """, params_tp67)
        total += cur.rowcount

        cur.execute('DROP TABLE IF EXISTS _tmp_ag_parts_lookup')
    conn.commit()
    return total


# ── Правило 0d ────────────────────────────────────────────────────────────────

# Маппинг устаревших ct-кодов площадок на актуальные (из листа «Нейминг» Google Sheets).
# Ключ = старый код, значение = новый код.
_CT_MIGRATION_MAP: dict[str, str] = {
    # UAZ / Moskvich
    'ct1345': 'ct0258',    # UAZ Patriot
    'ct1340': 'ct0254',    # Moskvich 6
    'ct1338': 'ct0253',    # Moskvich 3
    # Xcite / Solaris / Skoda / Renault / OMODA / Nissan / MG
    'ct1324': 'ct0247',    # Xcite X-Cross 7
    'ct1285': 'ct0241',    # Volkswagen Polo
    'ct1278': 'ct0240',    # Volkswagen Jetta
    'ct1206': 'ct0235',    # Tank 400
    'ct1205': 'ct0234',    # Tank 300
    'ct1204': 'ct0232',    # SWM G05 Pro
    'ct1172': 'ct0224',    # Solaris KRS
    'ct1170': 'ct0222',    # Solaris HC
    'ct1160': 'ct0219',    # Skoda Rapid
    'ct1158': 'ct0218',    # Skoda Octavia
    'ct1156': 'ct0217',    # Skoda Kodiaq
    'ct1139': 'ct0214',    # Renault Sandero
    'ct1128': 'ct0211',    # Renault Duster
    'ct1124': 'ct0210',    # Renault Arkana
    'ct1076': 'ct0282',    # Opel Antara
    'ct1073': 'ct0205',    # OMODA C5
    'ct1061': 'ct0201',    # Nissan Qashqai
    'ct1011': 'ct0196',    # MG 5
    'ct1004': 'ct0313',    # Mercedes-Benz SL-Класс AMG
    # Lada / KIA / KAIYI / Jetta / Jetour / Jaecoo / JAC
    'ct0885': 'ct0185',    # Lada (ВАЗ) Largus
    'ct0870': 'ct0178',    # KIA XCeed
    'ct0861': 'ct0175',    # KIA Sorento
    'ct0860': 'ct0174',    # KIA Seltos
    'ct0856': 'ct0171',    # KIA Picanto
    'ct0845': 'ct0168',    # KIA K5
    'ct0839': 'ct0166',    # KIA Ceed
    'ct0836': 'ct0158',    # KAIYI X7 Kunlun
    'ct0832': 'ct0153',    # Jetta VS7
    'ct0831': 'ct0152',    # Jetta VS5
    'ct0830': 'ct0151',    # Jetta VA3
    'ct0829': 'ct0149',    # Jetour X90 Plus
    'ct0827': 'ct0147',    # Jetour X70
    'ct0806': 'ct0142',    # Jaecoo J8
    'ct0805': 'ct0141',    # Jaecoo J7
    'ct0799': 'ct0131',    # JAC J7
    # Hyundai / Honda / Hawtai / Haval
    'ct0772': 'ct0129',    # Hyundai Tucson
    'ct0769': 'ct0126',    # Hyundai Solaris
    'ct0726': 'ct0285',    # Honda Odyssey
    'ct0722': 'ct0285',    # Honda HR-V
    'ct0715': 'ct0285',    # Honda Crosstour
    'ct0710': 'ct0000',    # Hawtai Laville
    'ct0708': 'ct0119',    # Haval Jolion
    'ct0707': 'ct0118',    # Haval H9
    'ct0706': 'ct0117',    # Haval H7
    'ct0704': 'ct0116',    # Haval H5
    'ct0703': 'ct0115',    # Haval H3
    'ct0700': 'ct0113',    # Haval F7
    # Geely / GAC / FAW / EXEED / Dongfeng / Dodge / Denza
    'ct0673': 'ct0106',    # Geely Preface
    'ct0672': 'ct0105',    # Geely Okavango
    'ct0671': 'ct0104',    # Geely Monjaro
    'ct0659': 'ct0101',    # Geely Coolray
    'ct0657': 'ct0100',    # Geely Cityray
    'ct0652': 'ct0099',    # Geely Atlas Pro
    'ct0646': 'ct0091',    # GAC GS3
    'ct0599': 'ct0085',    # FAW Bestune T90
    'ct0597': 'ct0083',    # FAW Bestune T55
    'ct0596': 'ct0081',    # FAW Bestune Pony
    'ct0593': 'ct0082',    # FAW Bestune B70
    'ct0590': 'ct0078',    # EXEED RX
    'ct0589': 'ct0077',    # EXEED LX
    'ct0576': 'ct0072',    # Dongfeng DFSK ix7
    'ct0575': 'ct0071',    # Dongfeng DFSK ix5
    'ct0566': 'ct0000',    # Dodge Charger
    'ct0565': 'ct0000',    # Dodge Challenger
    'ct0562': 'ct0000',    # Dodge Avenger
    'ct0561': 'ct0000',    # Denza Z9
    # Chevrolet / Chery / Changan / Belgee / BAIC / Audi / Alfa Romeo / Acura
    'ct0523': 'ct0062',    # Chevrolet Spark
    'ct0512': 'ct0059',    # Chevrolet Epica
    'ct0507': 'ct0059',    # Chevrolet Camaro
    'ct0503': 'ct0044',    # Chery Very (A13)
    'ct0500': 'ct0055',    # Chery Tiggo 8 Pro Max
    'ct0493': 'ct0050',    # Chery Tiggo 7 Pro Max
    'ct0492': 'ct0049',    # Chery Tiggo 7 Pro
    'ct0486': 'ct0046',    # Chery Tiggo 4
    'ct0461': 'ct0043',    # Changan UNI-V
    'ct0460': 'ct0042',    # Changan UNI-T
    'ct0453': 'ct0029',    # Changan Hunter
    'ct0452': 'ct0029',    # Changan Eado Plus
    'ct0449': 'ct0029',    # Changan Deepal S09
    'ct0443': 'ct0034',    # Changan CS75Plus
    'ct0441': 'ct0033',    # Changan CS75
    'ct0437': 'ct0031',    # Changan CS35Plus
    'ct0375': 'ct0028',    # Belgee X70
    'ct0373': 'ct0023',    # BAIC X55
    'ct0371': 'ct0021',    # BAIC U5 Plus
    'ct0357': 'ct0312',    # Audi RS 6
    'ct0356': 'ct0312',    # Audi RS 5
    'ct0354': 'ct0312',    # Audi RS 3
    'ct0327': 'ct0000',    # Alfa Romeo Giulia
    'ct0325': 'ct0000',    # Aito M5
    'ct0324': 'ct0000',    # Acura RDX
    # Audi (модели без отдельной записи → бренд)
    'ct0355': 'ct0312',    # Audi RS 4
    'ct0353': 'ct0312',    # Audi R8
    # BAIC
    'ct0374': 'ct0027',    # Belgee X50
    'ct0372': 'ct0022',    # BAIC X35
    'ct0370': 'ct0020',    # BAIC BJ40
    # Changan (дополнение)
    'ct0458': 'ct0040',    # Changan UNI-K
    'ct0456': 'ct0039',    # Changan Lamore
    'ct0451': 'ct0029',    # Changan Eado
    'ct0450': 'ct0029',    # Changan Deepal S7
    'ct0448': 'ct0029',    # Changan Deepal S07
    'ct0445': 'ct0036',    # Changan CS95
    'ct0440': 'ct0032',    # Changan CS55PLUS
    'ct0438': 'ct0032',    # Changan CS55
    'ct0433': 'ct0030',    # Changan Alsvin
    # Chery (дополнение)
    'ct0502': 'ct0058',    # Chery Tiggo 9
    'ct0498': 'ct0054',    # Chery Tiggo 8 Pro
    'ct0495': 'ct0052',    # Chery Tiggo 7L
    'ct0467': 'ct0045',    # Chery Arrizo 8
    # Chevrolet (дополнение)
    'ct0522': 'ct0000',    # Chevrolet Rezzo
    'ct0509': 'ct0060',    # Chevrolet Cobalt
    'ct0508': 'ct0000',    # Chevrolet Captiva
    # Datsun
    'ct0560': 'ct0065',    # Datsun on-DO
    # Dodge (дополнение)
    'ct0567': 'ct0000',    # Dodge Durango
    'ct0564': 'ct0000',    # Dodge Caravan
    'ct0563': 'ct0000',    # Dodge Caliber
    # Dongfeng (дополнение)
    'ct0581': 'ct0075',    # Dongfeng Shine Max
    'ct0572': 'ct0066',    # Dongfeng 580
    # EXEED (дополнение)
    'ct0592': 'ct0080',    # EXEED VX
    'ct0591': 'ct0079',    # EXEED TXL
    # FAW (дополнение)
    'ct0600': 'ct0086',    # FAW Bestune T99
    'ct0598': 'ct0084',    # FAW Bestune T77
    # Geely (дополнение)
    'ct0674': 'ct0107',    # Geely Tugella
    'ct0660': 'ct0102',    # Geely Emgrand
    'ct0651': 'ct0098',    # Geely Atlas
    # Haval (дополнение)
    'ct0709': 'ct0120',    # Haval M6
    'ct0701': 'ct0114',    # Haval F7x
    'ct0699': 'ct0112',    # Haval Dargo
    # Honda → бренд
    'ct0731': 'ct0285',    # Honda Vezel
    'ct0719': 'ct0285',    # Honda Fit Shuttle
    'ct0718': 'ct0285',    # Honda Fit
    'ct0714': 'ct0285',    # Honda CR-V
    # Hyundai (дополнение)
    'ct0744': 'ct0122',    # Hyundai Creta
    # JAC (дополнение)
    'ct0800': 'ct0133',    # JAC JS6
    # Jetour (дополнение)
    'ct0828': 'ct0148',    # Jetour X70 Plus
    'ct0825': 'ct0145',    # Jetour T2
    'ct0824': 'ct0144',    # Jetour Dashing
    # KAIYI (дополнение)
    'ct0835': 'ct0157',    # KAIYI X3 Pro
    'ct0834': 'ct0156',    # KAIYI X3
    # KIA (дополнение)
    'ct0866': 'ct0177',    # Kia Sportage (China)
    'ct0862': 'ct0176',    # Kia Soul
    'ct0859': 'ct0173',    # Kia Rio
    'ct0841': 'ct0167',    # Kia Cerato
    # Lada (дополнение)
    'ct0890': 'ct0190',    # Lada (ВАЗ) XRAY
    # Mercedes-Benz (дополнение)
    'ct1000': 'ct0313',    # Mercedes-Benz Maybach S-Класс
    # Nissan (дополнение)
    'ct1072': 'ct0203',    # Nissan X-Trail
    'ct1069': 'ct0202',    # Nissan Terrano
    # OMODA (дополнение)
    'ct1074': 'ct0207',    # OMODA S5
    # Renault (дополнение)
    'ct1136': 'ct0213',    # Renault Logan
    'ct1132': 'ct0212',    # Renault Kaptur
    # Skoda (дополнение)
    'ct1155': 'ct0216',    # Skoda Karoq
    # Solaris (дополнение)
    'ct1171': 'ct0223',    # Solaris HS
}


def _rule0d_migrate_ct_codes(conn) -> int:
    """Переводит устаревшие ct-коды площадок на актуальные (один UPDATE на колонку).

    Все ct-коды ровно 6 символов (ct + 4 цифры), поэтому LEFT(col, 7) = 'ctXXXX_'.
    Вместо N отдельных UPDATE-запросов в цикле — один UPDATE через VALUES JOIN.
    400 запросов → 4 (2 таблицы × 2 колонки).
    """
    # Строим VALUES один раз; значения — ASCII-константы, SQL-инъекция невозможна
    vals = ', '.join(
        f"('{old}_', '{new}', '{new}_')"
        for old, new in _CT_MIGRATION_MAP.items()
    )
    total = 0
    with conn.cursor() as cur:
        for tbl in ('big_analytics_direct', 'big_analytics_crop_targeting'):
            # adgroup_code: заменяем первый сегмент (ctXXXX)
            # SUBSTRING FROM 7 → оставляем всё начиная с '_' после ct-кода
            cur.execute(f"""
                UPDATE public.{tbl} t
                SET adgroup_code = v.new_code || SUBSTRING(t.adgroup_code FROM 7)
                FROM (VALUES {vals}) AS v(old_prefix, new_code, new_prefix)
                WHERE LEFT(t.adgroup_code, 7) = v.old_prefix
                  AND t.adgroup_code IS NOT NULL
                  AND t.adgroup_code != 'неверный кодер'
            """)
            total += cur.rowcount

            # AdGroupName: заменяем первый сегмент в начале строки
            # SUBSTRING FROM 8 → всё после 'ctXXXX_' (7 символов)
            cur.execute(f"""
                UPDATE public.{tbl} t
                SET "AdGroupName" = v.new_prefix || SUBSTRING(t."AdGroupName" FROM 8)
                FROM (VALUES {vals}) AS v(old_prefix, new_code, new_prefix)
                WHERE LEFT(t."AdGroupName", 7) = v.old_prefix
            """)
            total += cur.rowcount
    conn.commit()
    logger.info('[corrections] Правило 0d (ct migrate): %d строк', total)
    return total


# ── Правило 0c ────────────────────────────────────────────────────────────────

def _rule0c_global_ag_parts(conn) -> int:
    """Глобальный пересчёт ag_parts для всех строк (без фильтра по аккаунту)."""
    n = _recompute_ag_parts(conn, account_filter=None)
    logger.info('[corrections] Правило 0c (global ag_parts): %d строк', n)
    return n


# ── Правило 1 ─────────────────────────────────────────────────────────────────

_KUDЕРКО_DATE   = '2026-04-10'
_KUDЕРКО_NAME   = 'Кудерко Семен'
_KUDЕРКО_LOGINS = (
    'e-20086621', 'e-20086622', 'e-20084860', 'e-20084861', 'e-20086619',
    'porg-gcegsszl', 'e-20086660', 'e-20086659', 'porg-h27zek57', 'e-20086658',
    'e-20077075', 'e-20086657', 'porg-edmpebhr', 'e-20086620', 'e-20084857',
    'e-20086661', 'e-20086623', 'e-20084859', 'e-20084858', 'e-20076544',
    'e-20076545', 'porg-kkhtgf2u', 'e-20074366', 'porg-mgrauofh', 'e-20078432',
    'e-20077077', 'e-20077078', 'e-20078433', 'e-20078430', 'e-20077079',
    'e-20078431', 'e-20077076', 'e-20078429', 'porg-7yibjfp4', 'e-20076541',
    'e-20074364', 'porg-pzm4243t', 'porg-riga5gvo', 'porg-sblzprjm', 'porg-xagqvz3v',
    'porg-gbj6e3ji', 'porg-3q2n22ux', 'porg-x7iyctbh', 'porg-qruhft2a', 'porg-53t6ygdz',
    'porg-qeyeclqv', 'porg-iljlldjs', 'porg-wlta5kmb', 'porg-cs34qdr7', 'porg-cz2jqzbo',
    'e-20074363', 'e-20076540', 'porg-uguxrece', 'porg-jnbd47au', 'porg-klfzrvhu',
    'porg-v6ao2xka', 'porg-jelgic43', 'e-20076539', 'e-20074365', 'porg-nen5jouv',
    'porg-vvxm6gma', 'porg-rcg54tv4', 'porg-bczfmt3d', 'porg-tr47xrja', 'porg-5v4n6spu',
    'porg-p4uskpj6', 'porg-wgyzlarl',
)


def _rule1_kudерко(conn) -> int:
    """Переназначает директолога на 'Кудерко Семен' до 10.04.2026."""
    total = 0
    logins = list(_KUDЕРКО_LOGINS)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE public.big_analytics_direct
            SET    "специалист" = %s
            WHERE  "Date" < %s
              AND  account_login = ANY(%s)
            """,
            (_KUDЕРКО_NAME, _KUDЕРКО_DATE, logins),
        )
        total += cur.rowcount
        cur.execute(
            """
            UPDATE public.big_analytics_crop_targeting
            SET    "специалист" = %s
            WHERE  "Date" < %s
              AND  account_login = ANY(%s)
            """,
            (_KUDЕРКО_NAME, _KUDЕРКО_DATE, logins),
        )
        total += cur.rowcount
    conn.commit()
    logger.info('[corrections] Правило 1 (Кудерко Семен): %d строк', total)
    return total


# ── Правило 1б ────────────────────────────────────────────────────────────────

_СЕРГЕЕВ_DATE   = '2026-04-21'
_СЕРГЕЕВ_NAME   = 'Сергеев Алексей'
_СЕРГЕЕВ_LOGINS = (
    'porg-tde4jof6', 'kazan-ca-532199-z761', 'e-20074360', 'porg-wzisnv32',
    'porg-rmkn7sz4', 'porg-2xphfcul', 'e-20074359', 'porg-fuko7yzw', 'e-20074361',
)


def _rule1b_сергеев(conn) -> int:
    """Переназначает директолога на 'Сергеев Алексей' до 21.04.2026 (включая 20-е)."""
    total = 0
    logins = list(_СЕРГЕЕВ_LOGINS)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE public.big_analytics_direct
            SET    "специалист" = %s
            WHERE  "Date" < %s
              AND  account_login = ANY(%s)
            """,
            (_СЕРГЕЕВ_NAME, _СЕРГЕЕВ_DATE, logins),
        )
        total += cur.rowcount
        cur.execute(
            """
            UPDATE public.big_analytics_crop_targeting
            SET    "специалист" = %s
            WHERE  "Date" < %s
              AND  account_login = ANY(%s)
            """,
            (_СЕРГЕЕВ_NAME, _СЕРГЕЕВ_DATE, logins),
        )
        total += cur.rowcount
    conn.commit()
    logger.info('[corrections] Правило 1б (Сергеев Алексей): %d строк', total)
    return total


# ── Правило 1в ────────────────────────────────────────────────────────────────
# porg-o2lqtxk5: geely-nobosibirsk.ru не имеет directologist в local_gsheet_sites,
# но второй домен (geely-nskauto.ru) того же аккаунта подтверждает специалиста.

_ПИТЕРКИНА_LOGIN = 'porg-o2lqtxk5'
_ПИТЕРКИНА_NAME  = 'Питеркина Дарья'


def _rule1v_питеркина(conn) -> int:
    """Заполняет специалиста для аккаунта porg-o2lqtxk5 где NULL."""
    total = 0
    with conn.cursor() as cur:
        for tbl in ('big_analytics_direct', 'big_analytics_crop_targeting'):
            cur.execute(
                f"""
                UPDATE public.{tbl}
                SET    "специалист" = %s
                WHERE  account_login = %s
                  AND  ("специалист" IS NULL OR TRIM("специалист") = '')
                """,
                (_ПИТЕРКИНА_NAME, _ПИТЕРКИНА_LOGIN),
            )
            total += cur.rowcount
    conn.commit()
    logger.info('[corrections] Правило 1в (Питеркина Дарья): %d строк', total)
    return total


# ── Правило 1г ────────────────────────────────────────────────────────────────
# Питеркина Дарья по дате — аналог _rule1_кудерко.
# 28 аккаунтов до 2026-06-19 переназначаются на Питеркину (любой текущий
# специалист, не только NULL).

_ПИТЕРКИНА_DATE   = '2026-06-19'
_ПИТЕРКИНА_LOGINS = (
    'direct175', 'e-20074386', 'e-20074391', 'e-20075581', 'e-20076024',
    'e-20076025', 'e-20076032', 'e-20076035', 'e-20077735', 'e-20080927',
    'e-20086590', 'porg-3kybbaqw', 'porg-52ddldh4', 'porg-a7ysf76k',
    'porg-asnsozgg', 'porg-cy3l6otz', 'porg-de56ixiq', 'porg-dnprpowd',
    'porg-efrpw7tl', 'porg-g5el6elk', 'porg-hwoltj3u', 'porg-nqw6yxoc',
    'porg-o2lqtxk5', 'porg-orfyrlvm', 'porg-ounlaznf', 'porg-p6eyociq',
    'porg-rrq4agov', 'porg-ze76vrem',
)


def _rule1g_питеркина_bydate(conn) -> int:
    """Переназначает специалиста на 'Питеркина Дарья' для 28 аккаунтов до 2026-06-19."""
    total = 0
    logins = list(_ПИТЕРКИНА_LOGINS)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE public.big_analytics_direct
            SET    "специалист" = %s
            WHERE  "Date" < %s
              AND  account_login = ANY(%s)
            """,
            (_ПИТЕРКИНА_NAME, _ПИТЕРКИНА_DATE, logins),
        )
        total += cur.rowcount
        cur.execute(
            """
            UPDATE public.big_analytics_crop_targeting
            SET    "специалист" = %s
            WHERE  "Date" < %s
              AND  account_login = ANY(%s)
            """,
            (_ПИТЕРКИНА_NAME, _ПИТЕРКИНА_DATE, logins),
        )
        total += cur.rowcount
    conn.commit()
    logger.info('[corrections] Правило 1г (Питеркина Дарья bydate): %d строк', total)
    return total


# ── Правило 2 ─────────────────────────────────────────────────────────────────

# Аккаунты затронутые правилами 2-4 (для точечного UPDATE в правиле 3)
_ADGROUP_FIX_ACCOUNTS = [
    'e-20076545', 'e-20074366',
    'cars-yekaterinburg-541349-lrqf',
    'porg-akiqrh6u',
    'e-20085128', 'e-20085130', 'e-20085132', 'e-20085135', 'e-20086083',
    'porg-p7pymm3m',
    # Правило 5 (5б из v3):
    'byautos-34-533635-yj8u', 'porg-eezad6ih',
    'porg-mduvg6db', 'e-20084938', 'porg-p7bjp76w',
    'e-20077730', 'newcar-siberiya-526203-n5f1',
    # Правило 2 (CampaignName/AdGroupName fixes):
    'porg-g3gqnefi',
]


def _rule2_fix_adgroup_name(conn) -> int:
    """Исправляет опечатки в AdGroupName для конкретных аккаунтов (перенесено из v3)."""
    total = 0
    with conn.cursor() as cur:

        # e-20076545, e-20074366: пропущен 'a' в _ag011_g
        cur.execute("""
            UPDATE public.big_analytics_direct
            SET "AdGroupName" = REPLACE("AdGroupName", '_g011_g', '_ag011_g')
            WHERE account_login = ANY(%s)
              AND "AdGroupName" LIKE '%%_g011_g%%'
        """, (['e-20076545', 'e-20074366'],))
        total += cur.rowcount

        # e-20076545: _off_ → _aoff_ (guard: не трогаем строки где _aoff_ уже есть)
        cur.execute("""
            UPDATE public.big_analytics_direct
            SET "AdGroupName" = REPLACE("AdGroupName", '_off_', '_aoff_')
            WHERE account_login = %s
              AND "AdGroupName" LIKE '%%_off_%%'
              AND "AdGroupName" NOT LIKE '%%_aoff_%%'
        """, ('e-20076545',))
        total += cur.rowcount

        # cars-yekaterinburg-541349-lrqf: задвоенный _n000_n000_ → _n000_
        cur.execute("""
            UPDATE public.big_analytics_direct
            SET "AdGroupName" = REPLACE("AdGroupName", '_n000_n000_', '_n000_')
            WHERE account_login = %s
              AND "AdGroupName" LIKE '%%_n000_n000_%%'
        """, ('cars-yekaterinburg-541349-lrqf',))
        total += cur.rowcount

        # porg-akiqrh6u: пропущены underscores перед aoff/aon
        cur.execute("""
            UPDATE public.big_analytics_direct
            SET "AdGroupName" = REPLACE(REPLACE(REPLACE(REPLACE("AdGroupName",
                'ct0054aoff', 'ct0054_aoff'),
                'ct0054aon',  'ct0054_aon'),
                'ct0076aoff', 'ct0076_aoff'),
                'ct0076aon',  'ct0076_aon')
            WHERE account_login = %s
              AND ("AdGroupName" LIKE '%%ct0054aoff%%'
                OR "AdGroupName" LIKE '%%ct0054aon%%'
                OR "AdGroupName" LIKE '%%ct0076aoff%%'
                OR "AdGroupName" LIKE '%%ct0076aon%%')
        """, ('porg-akiqrh6u',))
        total += cur.rowcount

        # e-20085128/130/132/135, e-20086083: 'Новая группа' → правильное название
        cur.execute("""
            UPDATE public.big_analytics_direct
            SET "AdGroupName" = %s
            WHERE account_login = ANY(%s)
              AND "AdGroupName" = 'Новая группа'
        """, (
            'ct0021_aon_n000_r0121_ct001_ag011_g00 — 3. baic u5 plus',
            ['e-20085128', 'e-20085130', 'e-20085132', 'e-20085135', 'e-20086083'],
        ))
        total += cur.rowcount

        # porg-p7pymm3m: 'Новая группа' → правильное название
        cur.execute("""
            UPDATE public.big_analytics_direct
            SET "AdGroupName" = %s
            WHERE account_login = %s
              AND "AdGroupName" = 'Новая группа'
        """, (
            'ct0000_aon_n000_r0017_ct001_ag011_g00 — общая',
            'porg-p7pymm3m',
        ))
        total += cur.rowcount


        # porg-g3gqnefi: ЕПК — переименовываем CampaignName под нейминг + задаём campaign_code
        # CampaignId 707572731: «Единая перфоманс-кампания №2 от 25-02-2026»
        cur.execute("""
            UPDATE public.big_analytics_direct
            SET "CampaignName" = %s,
                campaign_code   = 'tp8_cpc_site',
                tp              = 'tp8',
                cpc_cpa         = 'cpc',
                site_quiz       = 'site'
            WHERE account_login = %s
              AND "CampaignId" = 707572731
        """, (
            'tp8_cpc_site — Telegram - Haval - Автотаргетинг - Волгоградская область — haval-vlg_тест ТГ неавтошка',
            'porg-g3gqnefi',
        ))
        total += cur.rowcount

        # porg-g3gqnefi: ЕПК — группа «Целевая аудитория» → adgroup_code в AdGroupName
        cur.execute("""
            UPDATE public.big_analytics_direct
            SET "AdGroupName" = %s
            WHERE account_login = %s
              AND "CampaignId" = 707572731
              AND "AdGroupName" = 'Целевая аудитория'
        """, (
            'ct0111_aon_n000_r0086_ct018_ag001_g00',
            'porg-g3gqnefi',
        ))
        total += cur.rowcount

        # porg-g3gqnefi: Товарная кампания — переименовываем CampaignName под нейминг + задаём campaign_code
        # CampaignId 707568038: «Товарная кампания haval-vlg.ru от 25.02.26»
        # tp7 = МК/ТК — adgroup_code задаём прямо здесь (из части CampaignName до ' — ')
        cur.execute("""
            UPDATE public.big_analytics_direct
            SET "CampaignName" = %s,
                campaign_code   = 'tp7_cpc_site',
                tp              = 'tp7',
                cpc_cpa         = 'cpc',
                site_quiz       = 'site',
                adgroup_code    = 'ct0111_aon_n000_r0086_ct010_ag001_g00'
            WHERE account_login = %s
              AND "CampaignId" = 707568038
        """, (
            'tp7_cpc_site_ct0111_aon_n000_r00 ф86_ct010_ag001_g00 — тест епк мод неавтошка',
            'porg-g3gqnefi',
        ))
        total += cur.rowcount

    conn.commit()
    logger.info('[corrections] Правило 2 (AdGroupName fixes): %d строк', total)
    return total


# ── Правило 3 ─────────────────────────────────────────────────────────────────

def _rule3_reextract_adgroup_code(conn) -> int:
    """Переизвлекает adgroup_code из исправленного AdGroupName для затронутых аккаунтов."""
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE public.big_analytics_direct
            SET adgroup_code = (REGEXP_MATCH(
                SPLIT_PART("AdGroupName", ' — ', 1),
                '(ct\\d+_(aoff|aon)_n\\d+_r\\d+_ct\\d+_ag\\d+_g\\d+)'
            ))[1]
            WHERE account_login = ANY(%s)
              AND (adgroup_code IS NULL OR adgroup_code = 'неверный кодер')
              AND "AdGroupName" IS NOT NULL
        """, (_ADGROUP_FIX_ACCOUNTS,))
        n = cur.rowcount
    conn.commit()
    logger.info('[corrections] Правило 3 (adgroup_code re-extract): %d строк', n)
    return n


# ── Правило 4 ─────────────────────────────────────────────────────────────────

def _rule4_recompute_ag_parts(conn) -> int:
    """Пересчитывает ag_part1..7 для строк с исправленным adgroup_code (конкретные аккаунты)."""
    n = _recompute_ag_parts(conn, account_filter=_ADGROUP_FIX_ACCOUNTS)
    logger.info('[corrections] Правило 4 (ag_parts fix accounts): %d строк', n)
    return n


# ── Правило 5 ─────────────────────────────────────────────────────────────────
# Прямые маппинги adgroup_code по аккаунту (раздел 5б из v3).
# Для byautos/porg-eezad6ih: AdGroupName-префикс → adgroup_code (нестандартные имена групп).
# Для остальных: замена неправильного кода на правильный.

_BYAUTOS_MAP = (
    ('avitobu_aon_n000_volgogrado_tgo_a25-54_genders',    'ct0009_aoff_n000_r0086_ct001_ag011_g00'),
    ('avtobu_aon_n000_volgogrado_tgo_a25-54_genders',     'ct0014_aoff_n000_r0086_ct001_ag011_g00'),
    ('avtoprobeg_aon_n000_volgogrado_tgo_a25-54_genders', 'ct0014_aoff_n000_r0086_ct001_ag011_g00'),
    ('avtosalon_aon_n000_volgogrado_tgo_a25-54_genders',  'ct0013_aoff_n000_r0086_ct001_ag011_g00'),
    ('buybu_aon_n000_volgogrado_tgo_a25-54_genders',      'ct0014_aoff_n000_r0086_ct001_ag011_g00'),
    ('chevrolet_aon_n000_volgogrado_cat_age_genders',     'ct0058_aon_n000_r0086_ct001_ag011_g00'),
    ('chinacarbu_aon_n000_volgogrado_tgo_a25-54_genders', 'ct0001_aoff_n000_r0086_ct001_ag011_g00'),
    ('drom_aon_n000_volgogrado_tgo_a25-54_genders',       'ct0010_aoff_n000_r0086_ct001_ag011_g00'),
    ('hyundai_aon_n000_volgogrado_cat_age_genders',       'ct0121_aon_n000_r0086_ct001_ag011_g00'),
    ('kia_aon_n000_volgogrado_cat_age_genders',           'ct0164_aon_n000_r0086_ct001_ag011_g00'),
    ('kia-ceed_aon_n000_volgogrado_tgo_a25-54_genders',   'ct0166_aon_n000_r0086_ct001_ag011_g00'),
    ('kia-rio_aon_n000_volgogrado_tgo_a25-54_genders',    'ct0173_aon_n000_r0086_ct001_ag011_g00'),
    ('kia-seltos_aon_n000_volgogrado_tgo_a25-54_genders', 'ct0174_aon_n000_r0086_ct001_ag011_g00'),
    ('kia-soul_aon_n000_volgogrado_tgo_a25-54_genders',   'ct0176_aon_n000_r0086_ct001_ag011_g00'),
    ('lada-granta_aon_n000_volgogrado_tgo_a25-54_genders','ct0183_aon_n000_r0086_ct001_ag011_g00'),
    ('lada-vesta_aon_n000_volgogrado_ct_a25-54_genders',  'ct0189_aon_n000_r0086_ct001_ag011_g00'),
    ('lada-vesta_aon_n000_volgogrado_tgo_a25-54_genders', 'ct0189_aon_n000_r0086_ct001_ag011_g00'),
    ('mazda_aon_n000_volgogrado_cat_age_genders',         'ct0283_aon_n000_r0086_ct001_ag011_g00'),
    ('mitsubishi_aon_n000_volgogrado_cat_age_genders',    'ct0264_aon_n000_r0086_ct001_ag011_g00'),
    ('nissan_aon_n000_volgogrado_cat_age_genders',        'ct0199_aon_n000_r0086_ct001_ag011_g00'),
    ('notype_aon_n000_volgogrado_cat_a25-54_genders',     'ct0014_aon_n000_r0086_ct010_ag011_g00'),
    ('notype_aon_n000_volgogrado_fid_a25-54_genders',     'ct0014_aon_n000_r0086_ct010_ag011_g00'),
    ('opel_aon_n000_volgogrado_cat_age_genders',          'ct0282_aon_n000_r0086_ct001_ag011_g00'),
    ('renault_aon_n000_volgogrado_cat_age_genders',       'ct0209_aon_n000_r0086_ct001_ag011_g00'),
    ('skoda_aon_n000_volgogrado_cat_age_genders',         'ct0215_aon_n000_r0086_ct001_ag011_g00'),
    ('suzuki_aon_n000_volgogrado_cat_age_genders',        'ct0277_aon_n000_r0086_ct001_ag011_g00'),
    ('volkswagen_aon_n000_volgogrado_cat_age_genders',    'ct0238_aon_n000_r0086_ct001_ag011_g00'),
)

_PORG_EEZAD6IH_MAP = (
    ('buy-auto_aon_n000_hantobl_tgo_age_genders',               'ct0014_aoff_n000_r0125_ct001_ag011_g00'),
    ('chery_aon_n000_hantobl_tgo_age_genders',                  'ct0044_aon_n000_r0125_ct001_ag011_g00'),
    ('geely_aon_n000_hantobl_tgo_age_genders',                  'ct0097_aon_n000_r0125_ct001_ag011_g00'),
    ('geely-atlas-new_aon_n000_hantobl_tgo_age_genders',        'ct0098_aon_n000_r0125_ct001_ag011_g00'),
    ('geely-atlas-pro_aon_n000_hantobl_tgo_age_genders',        'ct0099_aon_n000_r0125_ct001_ag011_g00'),
    ('geely-coolray-new_aon_n000_hantobl_tgo_age_genders',      'ct0101_aon_n000_r0125_ct001_ag011_g00'),
    ('geely-emgrand_aon_n000_hantobl_tgo_age_genders',          'ct0102_aon_n000_r0125_ct001_ag011_g00'),
    ('geely-tugella_aon_n000_hantobl_tgo_age_genders',          'ct0107_aon_n000_r0125_ct001_ag011_g00'),
    ('haval_aon_n000_hantobl_tgo_age_genders',                  'ct0111_aon_n000_r0125_ct001_ag011_g00'),
    ('haval-dargo_aon_n000_hantobl_tgo_age_genders',            'ct0112_aon_n000_r0125_ct001_ag011_g00'),
    ('haval-f7_aon_n000_hantobl_tgo_age_genders',               'ct0113_aon_n000_r0125_ct001_ag011_g00'),
    ('haval-h3_aon_n000_hantobl_tgo_age_genders',               'ct0115_aon_n000_r0125_ct001_ag011_g00'),
    ('haval-jolion-new_aon_n000_hantobl_tgo_age_genders',       'ct0119_aon_n000_r0125_ct001_ag011_g00'),
    ('haval-m6_aon_n000_hantobl_tgo_age_genders',               'ct0120_aon_n000_r0125_ct001_ag011_g00'),
    ('hyundai_aon_n000_hantobl_tgo_age_genders',                'ct0121_aon_n000_r0125_ct001_ag011_g00'),
    ('hyundai-creta_aon_n000_hantobl_tgo_age_genders',          'ct0122_aon_n000_r0125_ct001_ag011_g00'),
    ('hyundai-solaris_aon_n000_hantobl_tgo_age_genders',        'ct0126_aon_n000_r0125_ct001_ag011_g00'),
    ('hyundai-tucson_aon_n000_hantobl_tgo_age_genders',         'ct0129_aon_n000_r0125_ct001_ag011_g00'),
    ('kia_aon_n000_hantobl_tgo_age_genders',                    'ct0164_aon_n000_r0125_ct001_ag011_g00'),
    ('kia-ceed-sw_aon_n000_hantobl_tgo_age_genders',            'ct0166_aon_n000_r0125_ct001_ag011_g00'),
    ('kia-rio-x_aon_n000_hantobl_tgo_age_genders',              'ct0173_aon_n000_r0125_ct001_ag011_g00'),
    ('lada_aon_n000_hantobl_tgo_age_genders',                   'ct0181_aon_n000_r0125_ct001_ag011_g00'),
    ('lada-granta_aon_n000_hantobl_tgo_age_genders',            'ct0183_aon_n000_r0125_ct001_ag011_g00'),
    ('lada-granta-cross_aon_n000_hantobl_tgo_age_genders',      'ct0183_aon_n000_r0125_ct001_ag011_g00'),
    ('lada-granta-drive-active_aon_n000_hantobl_tgo_age_genders','ct0183_aon_n000_r0125_ct001_ag011_g00'),
    ('lada-granta-hatchback_aon_n000_hantobl_tgo_age_genders',  'ct0183_aon_n000_r0125_ct001_ag011_g00'),
    ('lada-granta-liftback_aon_n000_hantobl_tgo_age_genders',   'ct0183_aon_n000_r0125_ct001_ag011_g00'),
    ('lada-largus_aon_n000_hantobl_tgo_age_genders',            'ct0185_aon_n000_r0125_ct001_ag011_g00'),
    ('lada-niva-legend_aon_n000_hantobl_tgo_age_genders',       'ct0186_aon_n000_r0125_ct001_ag011_g00'),
    ('lada-niva-travel_aon_n000_hantobl_tgo_age_genders',       'ct0188_aon_n000_r0125_ct001_ag011_g00'),
    ('lada-vesta_aon_n000_hantobl_tgo_age_genders',             'ct0189_aon_n000_r0125_ct001_ag011_g00'),
    ('lada-vesta-cross-new_aon_n000_hantobl_tgo_age_genders',   'ct0189_aon_n000_r0125_ct001_ag011_g00'),
    ('lada-vesta-sw_aon_n000_hantobl_tgo_age_genders',          'ct0189_aon_n000_r0125_ct001_ag011_g00'),
    ('lada-vesta-sw-cross-new_aon_n000_hantobl_tgo_age_genders','ct0189_aon_n000_r0125_ct001_ag011_g00'),
    ('lada-xray_aon_n000_hantobl_tgo_age_genders',              'ct0190_aon_n000_r0125_ct001_ag011_g00'),
    ('lada-xray-cross_aon_n000_hantobl_tgo_age_genders',        'ct0190_aon_n000_r0125_ct001_ag011_g00'),
    ('nissan_aon_n000_hantobl_tgo_age_genders',                 'ct0199_aon_n000_r0125_ct001_ag011_g00'),
    ('nissan-x-trail_aon_n000_hantobl_tgo_age_genders',         'ct0203_aon_n000_r0125_ct001_ag011_g00'),
    ('nissan-x-trail_aon_n000_hantobl_tgo_age_genders Nissan X-Trail', 'ct0203_aon_n000_r0125_ct001_ag011_g00'),
    ('notype_aon_n000_hantobl_tg_age_genders',                  'ct0000_aon_n000_r0125_ct001_ag011_g00'),
    ('renault_aon_n000_hantobl_tgo_age_genders',                'ct0209_aon_n000_r0125_ct001_ag011_g00'),
    ('renault-duster_aon_n000_hantobl_tgo_age_genders',         'ct0211_aon_n000_r0125_ct001_ag011_g00'),
    ('renault-sandero_aon_n000_hantobl_tgo_age_genders',        'ct0214_aon_n000_r0125_ct001_ag011_g00'),
    ('volkswagen_aon_n000_hantobl_tgo_age_genders',             'ct0238_aon_n000_r0125_ct001_ag011_g00'),
    ('volkswagen-polo_aon_n000_hantobl_tgo_age_genders',        'ct0241_aon_n000_r0125_ct001_ag011_g00'),
)

# Прямые замены неправильного adgroup_code: (account_login, old_code, new_code)
_ADGROUP_CODE_REPLACEMENTS = (
    ('porg-mduvg6db',               'ct018_aoff_n000_r0105_ct001_ag001_g00', 'ct0000_aoff_n000_r0105_ct018_ag001_g00'),
    ('e-20084938',                  'ct018_aoff_n000_r0088_ct001_ag001_g00', 'ct0000_aoff_n000_r0088_ct018_ag001_g00'),
    ('porg-p7bjp76w',               'ct018_aoff_n000_r0074_ct001_ag001_g00', 'ct0000_aoff_n000_r0074_ct018_ag001_g00'),
    ('e-20077730',                  'ct0000_aon_n000_r0107_ct001_ag011_g00', 'ct0000_aon_n000_r0105_ct001_ag011_g00'),
    ('newcar-siberiya-526203-n5f1', 'ct0000_aon_n000_r0123_ct001_ag011_g00', 'ct0000_aon_n000_r0105_ct001_ag011_g00'),
)


def _rule5_direct_adgroup_code_maps(conn) -> int:
    """Прямые маппинги adgroup_code из 5б v3: нестандартные имена групп и замены кодов."""
    total = 0
    with conn.cursor() as cur:
        # byautos-34-533635-yj8u: AdGroupName-префикс → adgroup_code
        cur.execute("""
            UPDATE public.big_analytics_direct t
            SET adgroup_code = v.new_code
            FROM (VALUES %s) AS v(old_name, new_code)
            WHERE t.account_login = 'byautos-34-533635-yj8u'
              AND SPLIT_PART(t."AdGroupName", ' — ', 1) = v.old_name
              AND (t.adgroup_code IS NULL OR t.adgroup_code = 'неверный кодер')
        """ % ','.join(f"('{o}','{n}')" for o, n in _BYAUTOS_MAP))
        total += cur.rowcount

        # porg-eezad6ih: AdGroupName-префикс → adgroup_code
        cur.execute("""
            UPDATE public.big_analytics_direct t
            SET adgroup_code = v.new_code
            FROM (VALUES %s) AS v(old_name, new_code)
            WHERE t.account_login = 'porg-eezad6ih'
              AND SPLIT_PART(t."AdGroupName", ' — ', 1) = v.old_name
              AND (t.adgroup_code IS NULL OR t.adgroup_code = 'неверный кодер')
        """ % ','.join(f"('{o}','{n}')" for o, n in _PORG_EEZAD6IH_MAP))
        total += cur.rowcount

        # Прямые замены adgroup_code + AdGroupName (direct + crop_targeting для tp8-строк)
        for account, old_code, new_code in _ADGROUP_CODE_REPLACEMENTS:
            for tbl in ('big_analytics_direct', 'big_analytics_crop_targeting'):
                cur.execute(f"""
                    UPDATE public.{tbl}
                    SET adgroup_code = %s,
                        "AdGroupName" = CASE WHEN "AdGroupName" = %s THEN %s ELSE "AdGroupName" END
                    WHERE account_login = %s AND adgroup_code = %s
                """, (new_code, old_code, new_code, account, old_code))
                total += cur.rowcount

    conn.commit()
    logger.info('[corrections] Правило 5 (direct adgroup_code maps): %d строк', total)
    return total


# ── Правило 4б: пересчёт ag_parts для big_analytics_crop_targeting ────────────

def _rule4b_recompute_ag_parts_crop(conn) -> int:
    """Пересчитывает ag_part1..7 и неверный_кодер_new для big_analytics_crop_targeting.

    Аналог _recompute_ag_parts, но применяется к big_analytics_crop_targeting.
    Вызывается после rule5 (_rule5_direct_adgroup_code_maps), которое исправляет
    adgroup_code в crop_targeting (ct018_... → ct0000_...) — без этого пересчёта
    crop_targeting получает исправленный adgroup_code но старые ag_parts → неверный_кодер.
    """
    nm = T_GSHEET_NAMING
    pat = _AG_CODE_PATTERN
    total = 0
    with conn.cursor() as cur:
        # 1. Lookup для уникальных adgroup_code из crop_targeting
        cur.execute(f"""
            DROP TABLE IF EXISTS _tmp_ag_parts_crop_lookup;
            CREATE UNLOGGED TABLE _tmp_ag_parts_crop_lookup AS
            WITH unique_codes AS (
                SELECT DISTINCT adgroup_code
                FROM public.big_analytics_crop_targeting
                WHERE adgroup_code IS NOT NULL
                  AND adgroup_code != 'неверный кодер'
            )
            SELECT
                u.adgroup_code,
                CASE WHEN u.adgroup_code ~ '{pat}'
                     THEN COALESCE(SPLIT_PART(u.adgroup_code,'_',1)||' - '||n1.name,
                                   SPLIT_PART(u.adgroup_code,'_',1))
                     ELSE 'неверный кодер' END AS ag_part1,
                CASE WHEN u.adgroup_code ~ '{pat}'
                     THEN COALESCE(SPLIT_PART(u.adgroup_code,'_',2)||' - '||n2.name,
                                   SPLIT_PART(u.adgroup_code,'_',2))
                     ELSE 'неверный кодер' END AS ag_part2,
                CASE WHEN u.adgroup_code ~ '{pat}'
                     THEN COALESCE(SPLIT_PART(u.adgroup_code,'_',3)||' - '||n3.name,
                                   SPLIT_PART(u.adgroup_code,'_',3))
                     ELSE 'неверный кодер' END AS ag_part3,
                CASE WHEN u.adgroup_code ~ '{pat}'
                     THEN COALESCE(SPLIT_PART(u.adgroup_code,'_',4)||' - '||n4.name,
                                   SPLIT_PART(u.adgroup_code,'_',4))
                     ELSE 'неверный кодер' END AS ag_part4,
                CASE WHEN u.adgroup_code ~ '{pat}'
                     THEN COALESCE(SPLIT_PART(u.adgroup_code,'_',5)||' - '||n5.name,
                                   SPLIT_PART(u.adgroup_code,'_',5))
                     ELSE 'неверный кодер' END AS ag_part5,
                CASE WHEN u.adgroup_code ~ '{pat}'
                     THEN COALESCE(SPLIT_PART(u.adgroup_code,'_',6)||' - '||n6.name,
                                   SPLIT_PART(u.adgroup_code,'_',6))
                     ELSE 'неверный кодер' END AS ag_part6,
                CASE WHEN u.adgroup_code ~ '{pat}'
                     THEN COALESCE(SPLIT_PART(u.adgroup_code,'_',7)||' - '||n7.name,
                                   SPLIT_PART(u.adgroup_code,'_',7))
                     ELSE 'неверный кодер' END AS ag_part7,
                CASE
                    WHEN u.adgroup_code !~ '{pat}' THEN 'неверный кодер'
                    WHEN n1.name IS NOT NULL
                     AND n2.name IS NOT NULL
                     AND n3.name IS NOT NULL
                     AND n4.name IS NOT NULL
                     AND n5.name IS NOT NULL
                     AND n6.name IS NOT NULL
                     AND n7.name IS NOT NULL                  THEN 'верный кодер'
                    ELSE 'неверный кодер'
                END AS verdict
            FROM unique_codes u
            LEFT JOIN {nm} n1 ON n1.type = 'ag_part1' AND LOWER(SPLIT_PART(u.adgroup_code,'_',1)) = n1.code
            LEFT JOIN {nm} n2 ON n2.type = 'ag_part2' AND LOWER(SPLIT_PART(u.adgroup_code,'_',2)) = n2.code
            LEFT JOIN {nm} n3 ON n3.type = 'ag_part3' AND LOWER(SPLIT_PART(u.adgroup_code,'_',3)) = n3.code
            LEFT JOIN {nm} n4 ON n4.type = 'ag_part4' AND LOWER(SPLIT_PART(u.adgroup_code,'_',4)) = n4.code
            LEFT JOIN {nm} n5 ON n5.type = 'ag_part5' AND LOWER(SPLIT_PART(u.adgroup_code,'_',5)) = n5.code
            LEFT JOIN {nm} n6 ON n6.type = 'ag_part6' AND LOWER(SPLIT_PART(u.adgroup_code,'_',6)) = n6.code
            LEFT JOIN {nm} n7 ON n7.type = 'ag_part7' AND LOWER(SPLIT_PART(u.adgroup_code,'_',7)) = n7.code;
            CREATE INDEX ON _tmp_ag_parts_crop_lookup (adgroup_code);
            ANALYZE _tmp_ag_parts_crop_lookup;
        """)

        # 2. UPDATE строк crop_targeting — JOIN по adgroup_code
        cur.execute(f"""
            UPDATE public.big_analytics_crop_targeting d
            SET ag_part1 = l.ag_part1,
                ag_part2 = l.ag_part2,
                ag_part3 = l.ag_part3,
                ag_part4 = l.ag_part4,
                ag_part5 = l.ag_part5,
                ag_part6 = l.ag_part6,
                ag_part7 = l.ag_part7,
                "неверный_кодер_new" = l.verdict
            FROM _tmp_ag_parts_crop_lookup l
            WHERE d.adgroup_code = l.adgroup_code
              AND d.adgroup_code IS NOT NULL
              AND d.adgroup_code != 'неверный кодер'
        """)
        total += cur.rowcount

        cur.execute('DROP TABLE IF EXISTS _tmp_ag_parts_crop_lookup')
    conn.commit()
    logger.info('[corrections] Правило 4б (ag_parts crop_targeting): %d строк', total)
    return total


# ── Правило 6 ─────────────────────────────────────────────────────────────────

def _rule6_validity_filter(conn) -> int:
    """Устанавливает 'неверный кодер' для campaign_code/tp/cpc_cpa не прошедших валидацию."""
    total = 0
    with conn.cursor() as cur:
        # Строки с невалидным campaign_code → весь набор кодов = 'неверный кодер'
        cur.execute("""
            UPDATE public.big_analytics_direct
            SET campaign_code = 'неверный кодер',
                tp            = 'неверный кодер',
                cpc_cpa       = 'неверный кодер'
            WHERE campaign_code IS NOT NULL
              AND campaign_code != 'неверный кодер'
              AND campaign_code !~* '^tp[0-9]+_(cpc|cpa)_(site|quiz)$'
        """)
        total += cur.rowcount

        # Строки с уже установленным 'неверный кодер' в campaign_code но незакаскадированным tp/cpc_cpa
        cur.execute("""
            UPDATE public.big_analytics_direct
            SET tp      = 'неверный кодер',
                cpc_cpa = 'неверный кодер'
            WHERE campaign_code = 'неверный кодер'
              AND (tp != 'неверный кодер' OR cpc_cpa != 'неверный кодер')
              AND tp IS NOT NULL
              AND cpc_cpa IS NOT NULL
        """)
        total += cur.rowcount

    conn.commit()
    logger.info('[corrections] Правило 6 (validity filter): %d строк', total)
    return total


# ── Правило 7 ─────────────────────────────────────────────────────────────────

def _rule7_fill_pixel(conn) -> int:
    """Пересоздаёт T_PIXEL с реальными данными (убирает WHERE 1=0 заглушку из step3)."""
    from step3_build_sources.step3 import _build_pixel_sql

    st = load_status_sql(conn)
    sql = _build_pixel_sql(st['status_cases'], st['priezd_statuses_sql'])
    # Убираем заглушку — включаем реальные данные
    sql = sql.replace('WHERE 1=0;', 'WHERE gs."domain" IS NOT NULL;')

    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(f'SELECT COUNT(*) FROM {T_PIXEL}')
        n = cur.fetchone()[0]
    logger.info('[corrections] Правило 7 (pixel fill): %d строк в %s', n, T_PIXEL)
    return n


# ── Правило 8 ─────────────────────────────────────────────────────────────────

# Маппинг utm_source → источник (агрегированный)
_UTM_PIXEL_SOURCES = (
    'victory_irtysh_pixel', 'victory_carstart_vdl', 'victory_carstart_pixel',
    'victory_novo', 'victory_optimum_pixel', 'victory_pb', 'victory_pixel',
    'victory_skavto_vdl', 'victory_pixel_kt', 'victory_pixel_hm', 'victory_pxl',
    'victory_skavto_pixel', 'victory_urbancar_vdl', 'victory_vdl', 'victory_vershina_pixel',
)


def _rule8_utm_classify(conn) -> int:
    """Классифицирует utm_source для unmatched-лидов в big_analytics_direct.

    В v5 victory_*/telegram лиды исключаются из leads_direct и попадают
    в big_analytics_pixel/telegram, поэтому данное правило обновит ~0 строк.
    Реализовано как страховка на случай граничных случаев.
    """
    pixel_list = ', '.join(f"'{s}'" for s in _UTM_PIXEL_SOURCES)
    total = 0
    with conn.cursor() as cur:
        # pixel-источники: JOIN с raw_leads по key3 для получения utm_source
        cur.execute(f"""
            UPDATE public.big_analytics_direct d
            SET источник   = r.new_source,
                направление = r.new_direction
            FROM (
                SELECT
                    rl.key3,
                    CASE WHEN rl.utm_source IN ({pixel_list})
                              THEN 'Пиксель'  -- KOMPLEKS_REFACTOR_REDO_2026-07-09
                         WHEN rl.utm_source IN ('stories_tg','telegram')
                          AND rl.utm_medium = 'posev'
                              THEN 'telegram'
                         ELSE NULL
                    END AS new_source,
                    -- Вариант B: все пиксель-utm → единое направление 'Пиксель'.
                    -- Раньше 14 из 15 веток копировали техническое имя utm_source
                    -- (victory_pxl, victory_vdl и т.п.) прямо в бизнес-колонку
                    -- «направление» — это затекание технического имени в бизнес-данные.
                    -- Эталон для пиксель-лидов = 'Пиксель' (как уже было для victory_irtysh_pixel).
                    CASE
                        WHEN rl.utm_source IN ({pixel_list})           THEN 'Пиксель'  -- KOMPLEKS_REFACTOR_REDO_2026-07-09
                        WHEN rl.utm_source IN ('stories_tg','telegram')
                         AND rl.utm_medium = 'posev'                   THEN 'posev'
                        ELSE NULL
                    END AS new_direction
                FROM {T_RAW_LEADS} rl
                WHERE rl.key3 IS NOT NULL
                  AND (
                    rl.utm_source IN ({pixel_list})
                    OR (rl.utm_source IN ('stories_tg','telegram') AND rl.utm_medium = 'posev')
                  )
                GROUP BY rl.key3, rl.utm_source, rl.utm_medium
            ) r
            WHERE d.key3 = r.key3
              AND d.источник IS NULL
              AND r.new_source IS NOT NULL
        """)
        total += cur.rowcount

    conn.commit()
    logger.info('[corrections] Правило 8 (utm classify): %d строк', total)
    return total


# ── Нормализация салонов (вызывается после сборки big_analytics_full) ─────────

def _rule_fix_wrong_domains(conn) -> int:
    """Исправляет строки где domain взят из лида чужого аккаунта вместо gsheet-домена."""
    DOMAIN_FIXES = [
        # (account_login,    wrong_domain,               correct_domain)
        ('porg-vf3dnoy5',  'topcars-msk.ru',            'new-cars-msk.ru'),
        ('e-20077590',     'newauto-msk.ru',             'topcars-msk.ru'),
        ('e-20077590',     'new-cars-msk.ru',            'topcars-msk.ru'),
        ('porg-jgcggtud',  'driveauto-nvg.ru',           'driveavto-nvg.ru'),
        ('e-20077729',     'topcars-msk.ru',             'newauto-msk.ru'),
        ('e-20080928',     'tenet-77.ru',                'geelyauto-msk.ru'),
        ('e-20080928',     'newauto-msk.ru',             'geelyauto-msk.ru'),
        ('porg-3kybbaqw',  'tenet-stavropol.ru',         'tenet-autokrd.ru'),
        ('porg-bnw3mip5',  'old_drive-auto-134.ru',      'drive-auto-134.ru'),
        ('e-20085690',     'topcars-msk.ru',             'tenet-77.ru'),
        ('e-20078823',     'ladapro-26.ru',              'carsclad-vlg.ru'),
    ]
    total = 0
    with conn.cursor() as cur:
        for login, wrong, correct in DOMAIN_FIXES:
            cur.execute(
                'UPDATE public.big_analytics_direct SET domain = %s '
                'WHERE account_login = %s AND domain = %s',
                (correct, login, wrong),
            )
            total += cur.rowcount
    conn.commit()
    logger.info('[corrections] fix_wrong_domains: %d строк', total)
    return total


def normalize_salons(conn, tables=None) -> int:
    """Применяет SALON_ALIASES + DOMAIN_SALON_MAP к колонке "салон".

    tables — список таблиц public.*; по умолчанию ['big_analytics_full'].
    """
    if tables is None:
        tables = ['big_analytics_full']
    total = 0
    canon_total = 0
    salon_key_f = salon_match_key('f."салон"')
    with conn.cursor() as cur:
        for table in tables:
            # 1. Ручные алиасы (НЕ-перестановочные опечатки/синонимы).
            for wrong, correct in SALON_ALIASES.items():
                cur.execute(
                    f'UPDATE public.{table} SET "салон" = %s WHERE "салон" = %s',
                    (correct, wrong),
                )
                total += cur.rowcount
            # 2. Домены без записи в gsheet_sites -> салон по карте.
            for domain, salon in DOMAIN_SALON_MAP.items():
                cur.execute(
                    f'UPDATE public.{table} SET "салон" = %s'
                    ' WHERE domain = %s AND ("салон" IS NULL OR "салон" = \'\')',
                    (salon, domain),
                )
                total += cur.rowcount
            # 3. WORD-SORT АВТО-КАНОНИЗАЦИЯ — единый источник истины.
            # Любое написание салона, чей word-sort ключ совпал с эталоном
            # local_gsheet_sites.salon (но exact-написание отличается — напр.
            # перестановка слов 'Центр Авто Казань' -> 'Казань Центр Авто'),
            # приводится к эталонному написанию. Это канонизирует ЗНАЧЕНИЕ
            # ДО всех downstream exact-джоинов (step6 CRM, fill_missing_regions,
            # reviews-loader) — они начинают матчиться сами, без правки каждого.
            # Защита от схлопывания: канон-цель ДОЛЖНА быть единственной на ключ
            # (HAVING count(DISTINCT)=1) — два разных салона с одним ключом
            # (коллизия) НЕ канонизируются (проверено: коллизий 0).
            cur.execute(f"""
                UPDATE public.{table} f
                SET "салон" = canon.canon_salon
                FROM (
                    SELECT wkey, MIN(salon) AS canon_salon
                    FROM (
                        SELECT DISTINCT TRIM(salon) AS salon,
                               {salon_match_key('salon')} AS wkey
                        FROM public.{T_GSHEET_SITES}
                        WHERE salon IS NOT NULL AND TRIM(salon) <> ''
                    ) s
                    GROUP BY wkey
                    HAVING count(*) = 1
                ) canon
                WHERE f."салон" IS NOT NULL AND TRIM(f."салон") <> ''
                  AND {salon_key_f} = canon.wkey
                  AND LOWER(TRIM(f."салон")) <> LOWER(TRIM(canon.canon_salon))
            """)
            canon_total += cur.rowcount
            total += cur.rowcount
    conn.commit()
    logger.info(
        '[corrections] normalize_salons %s: %d строк (из них word-sort канонизация %d)',
        tables, total, canon_total,
    )
    return total


# ── Правило: Перформ РФ → направление Перформ ──────────────────────────────

def _rule_perform_direction(conn) -> int:
    """Перформ-строки → направление='Перформ' во всех component-таблицах.

    PERFORM_SALON_FIX_2026-07-10: раньше матчили только по "салон"='Перформ РФ'. После правки 5
    (step3._build_common_ctes) ~525 Перформ-строк получают РЕАЛЬНЫЙ салон (Южный Драйв и т.п.) и
    перестают совпадать с 'Перформ РФ' → выпадали бы из Перформ-фильтра. Добавлен "id_салона"='avto_0415'
    (клиент Перформа) — ловит их независимо от значения "салон".
    """
    total = 0
    for t in COMPONENT_TABLES:
        with conn.cursor() as cur:
            cur.execute(f"""
                UPDATE public.{t}
                SET направление = 'Перформ'
                WHERE ("салон" = 'Перформ РФ' OR "id_салона" = 'avto_0415')
                  AND (направление IS NULL OR направление <> 'Перформ')
            """)
            total += cur.rowcount
    conn.commit()
    logger.info('[corrections] _rule_perform_direction: %d строк', total)
    return total


def fill_missing_regions(conn) -> int:
    """Заполняет пустой регион/город в big_analytics_full.

    1. По матчингу (салон, город) → регион из local_gsheet_sites.
    2. По DOMAIN_SITE_MAP для доменов вообще отсутствующих в local_gsheet_sites.
    """
    total = 0
    with conn.cursor() as cur:
        # 1. Матчинг по (салон, город) → регион
        cur.execute("""
            UPDATE public.big_analytics_full f
            SET "регион" = gs.region
            FROM public.local_gsheet_sites gs
            WHERE f."салон" = gs.salon
              AND f."город" = gs.city
              AND (f."регион" IS NULL OR f."регион" = '')
              AND gs.region IS NOT NULL AND gs.region != ''
        """)
        total += cur.rowcount

        # 2. Домены без записи в local_gsheet_sites
        for domain, (salon, city, region) in DOMAIN_SITE_MAP.items():
            cur.execute("""
                UPDATE public.big_analytics_full
                SET "город"  = COALESCE(NULLIF("город",  ''), %s),
                    "регион" = COALESCE(NULLIF("регион", ''), %s)
                WHERE domain = %s
                  AND ("город" IS NULL OR "город" = ''
                       OR "регион" IS NULL OR "регион" = '')
            """, (city, region, domain))
            total += cur.rowcount

    conn.commit()
    logger.info('[corrections] fill_missing_regions: %d строк', total)
    return total


def _fix_missing_managers(conn) -> int:
    """
    Ручной патч доменов, чьи sales_manager отсутствует в local_gsheet_sites.
    lotos91.ru → avto_0358 (АвтоЛидер) — менеджер тот же что у Лидер (avto_0083).
    """
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE public.big_analytics_direct
            SET менеджер = 'Михаил Яковлев'
            WHERE domain = 'lotos91.ru'
              AND (менеджер IS NULL OR менеджер = '')
        """)
        n = cur.rowcount
    conn.commit()
    logger.info('[corrections] _fix_missing_managers: %d строк', n)
    return n


# Аккаунты, для которых login_key отсутствует в local_gsheet_sites,
# но домен уже размечен под другим login_key. Заполняем поля копированием
# из существующей строки того же домена в big_analytics_direct.
_ACCOUNT_DOMAIN_BACKFILL = [
    ('porg-rtowqong', 'autopark-102.ru'),
]


def _fix_account_domain_backfill(conn) -> int:
    """
    Копирует салон/город/регион/менеджер/проджект/id_салона/статус/тип_сайта/
    шаблон/специалист из эталонной строки (домен уже размечен под другим
    login_key) в строки с NULL для пары (account_login, domain).

    Применяется к big_analytics_direct и big_analytics_crop_targeting.
    """
    total = 0
    for account, domain in _ACCOUNT_DOMAIN_BACKFILL:
        for table in ('big_analytics_direct', 'big_analytics_crop_targeting'):
            with conn.cursor() as cur:
                cur.execute(f"""
                    UPDATE public.{table} f
                    SET "статус"     = COALESCE(NULLIF(f."статус",''),     src."статус"),
                        "салон"      = COALESCE(NULLIF(f."салон",''),      src."салон"),
                        "город"      = COALESCE(NULLIF(f."город",''),      src."город"),
                        "регион"     = COALESCE(NULLIF(f."регион",''),     src."регион"),
                        direction    = COALESCE(NULLIF(f.direction,''),    src.direction),
                        менеджер     = COALESCE(NULLIF(f.менеджер,''),     src.менеджер),
                        проджект     = COALESCE(NULLIF(f.проджект,''),     src.проджект),
                        id_салона    = COALESCE(f.id_салона,                src.id_салона),
                        "тип_сайта"  = COALESCE(NULLIF(f."тип_сайта",''),  src."тип_сайта"),
                        "шаблон"     = COALESCE(NULLIF(f."шаблон",''),     src."шаблон"),
                        "специалист" = COALESCE(NULLIF(f."специалист",''), src."специалист")
                    FROM (
                        SELECT "статус", "салон", "город", "регион", direction, менеджер,
                               проджект, id_салона, "тип_сайта", "шаблон", "специалист"
                        FROM public.big_analytics_direct
                        WHERE LOWER(TRIM(domain)) = %s
                          AND account_login <> %s
                          AND "город" IS NOT NULL AND "город" <> ''
                        LIMIT 1
                    ) src
                    WHERE f.account_login = %s
                      AND LOWER(TRIM(f.domain)) = %s
                      AND (f."город" IS NULL OR f."город" = '')
                """, (domain, account, account, domain))
                total += cur.rowcount
    conn.commit()
    logger.info('[corrections] _fix_account_domain_backfill: %d строк', total)
    return total


# UTM-кампании, отсутствующие в gsheets_crop_targeting_account.
# Заполняем салон/город хардкодом — корректировка в коде вместо Google Sheet.
_CROP_UTM_BACKFILL = [
    ('volgograd_novostnoy',  'carnew-vlg.ru', 'Автоцентр на Жукова', 'Волгоград'),
    ('chp_volgograd',        'carnew-vlg.ru', 'Автоцентр на Жукова', 'Волгоград'),
    ('informator_volgograd', 'carnew-vlg.ru', 'Автоцентр на Жукова', 'Волгоград'),
]


def _fix_crop_missing_utms(conn) -> int:
    """
    Заполняет салон/город для строк big_analytics_crop_targeting
    (_source_table='crop_targeting'), у которых эти поля NULL и для которых
    UTM-кампания отсутствует в gsheets_crop_targeting_account.

    Маппинг хардкодом по utm_campaign из API источника. Применяется
    к big_analytics_crop_targeting (далее в big_analytics_full переезжает
    через load_crop_to_big_analytics.py).

    Сопоставление по `"CampaignName"` (для api строк там лежит channel_link)
    не годится — UTM-кампания живёт только в crop_targeting_api_telegain_lead.
    Поэтому фильтруем строки только по domain + NULL салон.
    """
    total = 0
    with conn.cursor() as cur:
        for utm, domain, salon, gorod in _CROP_UTM_BACKFILL:
            for table in ('big_analytics_crop_targeting', 'big_analytics_full'):
                cur.execute(f"""
                    UPDATE public.{table}
                    SET "салон" = %s,
                        "город" = %s
                    WHERE _source_table = 'crop_targeting'
                      AND LOWER(TRIM(domain)) = %s
                      AND ("салон" IS NULL OR "салон" = '')
                """, (salon, gorod, domain))
                total += cur.rowcount
    conn.commit()
    logger.info('[corrections] _fix_crop_missing_utms: %d строк', total)
    return total


# ── Правило 9: дедуп crmf/mauto «Лидер» по phone (период перекрытия CRM) ─────

# PATCH-CRMF-LIDER-DEDUP-2026-06-15
# С 29.05.2026 салон «Лидер» переехал с CRM crmf → mauto.
# В период перехода 29.05–10.06.2026 один и тот же клиент мог получить заявки
# одновременно в обеих CRM (yclid=NULL у crmf → штатный phone+yclid дедуп в
# leads_deduped CTE НЕ срабатывает, т.к. crmf-строки уходят в ветку
# «phone IS NULL OR yclid IS NULL»).
#
# Решение (ревью director 2026-06-15, Вариант 2):
#   1. Ставим is_copy_for_removal=TRUE для crmf-дублей в raw_leads ДО step3.
#   2. Обе ветки leads_deduped (step3) фильтруют WHERE is_copy_for_removal IS NOT TRUE.
#   3. leads_base (step13) тоже фильтрует WHERE is_copy_for_removal IS NOT TRUE.
# Это гарантирует что дедуп доходит до big_analytics_direct (шаг 3, ось заявок)
# и до big_analytics_full_arrival (шаг 13, ось визитов) одинаково.
#
# Вызов: run_dedup_crmf_lider() вызывается из pipeline.py ПОСЛЕ step2
# (индексы на raw_leads готовы) и ДО step3. НЕ вызывается из apply() —
# apply() выполняется ПОСЛЕ step3, когда big_analytics_direct уже собран.
#
# Критерий дубля: phone совпадает между crmf_excel и mauto_excel
# для salon='Лидер' И created_date >= '2026-05-29'.
# Приоритет: оставляем mauto_excel (новая CRM), исключаем crmf_excel-копию.
# «Купил» среди дублей = 0 (проверено director) → продажи не теряются.
#
# Область влияния: ТОЛЬКО raw_leads, salon='Лидер', source_type='crmf_excel',
# created_date >= 2026-05-29. Расход (raw_yandex/big_analytics_direct) не затрагивается.

_CRMF_LIDER_DEDUP_DATE = '2026-05-29'


def run_dedup_crmf_lider(conn) -> int:
    """Ставит is_copy_for_removal=TRUE для crmf-дублей «Лидер» по phone.

    Вызывается из pipeline.py ПОСЛЕ step2 (индексы на raw_leads готовы) и ДО
    step3 — чтобы leads_deduped (step3) и leads_base (step13) получили флаг
    ДО сборки big_analytics_direct и big_analytics_full_arrival.

    Дубль: phone совпадает между crmf_excel и mauto_excel для salon='Лидер'
    за период >= 2026-05-29 (дата начала переезда CRM).
    Оставляем mauto_excel (приоритетная CRM), исключаем crmf_excel-копию.

    Idempotent: повторный запуск не изменит уже помеченные строки.
    """
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE public.raw_leads rl
            SET is_copy_for_removal = TRUE
            WHERE rl.source_type = 'crmf_excel'
              AND rl.salon = 'Лидер'
              AND rl.created_date >= %s
              AND rl.phone IS NOT NULL AND rl.phone != ''
              AND COALESCE(rl.is_copy_for_removal, FALSE) = FALSE
              AND EXISTS (
                  SELECT 1 FROM public.raw_leads mauto
                  WHERE mauto.source_type = 'mauto_excel'
                    AND mauto.salon = 'Лидер'
                    AND mauto.phone = rl.phone
                    AND mauto.created_date >= %s
              )
        """, (_CRMF_LIDER_DEDUP_DATE, _CRMF_LIDER_DEDUP_DATE))
        n = cur.rowcount
    conn.commit()
    logger.info('[corrections] run_dedup_crmf_lider (crmf/mauto Лидер): %d строк is_copy_for_removal=TRUE', n)
    return n


def _rule_fill_specialist_fallback(conn) -> int:
    """SPEC_FALLBACK_DIRECTION_2026-07-03

    Для строк с пустым/NULL «специалист» в COMPONENT_TABLES заполняет fallback
    в три шага:
      1. directologist из local_gsheet_sites по домену (если заполнен)
      2. direction из local_gsheet_sites по домену (если directologist пустой)
      3. 'Без специалиста' — если ни directologist, ни direction нет,
         либо домен вообще отсутствует в local_gsheet_sites.

    Цель: исключить NULL-специалист строки из «слепого пятна» при PBI-фильтре
    специалист ≠ 'Медиа-Актив'. Строки получают ненулевое значение ('Авто' или
    'Без специалиста') → аналитик может явно включить/исключить их в слайсере.

    Правило идёт последним в волне-3 (после fill_account_domain_backfill) —
    когда все domain уже заполнены предыдущими правилами.
    """
    total = 0
    for t in COMPONENT_TABLES:
        with conn.cursor() as cur:
            # Шаг 1+2: берём directologist (приоритет) или direction_main по домену
            cur.execute(f"""
                UPDATE public.{t} ba
                SET "специалист" = COALESCE(          -- SPEC_FALLBACK_DIRECTION_2026-07-03
                    NULLIF(TRIM(gs.directologist),   ''),
                    NULLIF(TRIM(gs.direction_main),  ''),
                    'Без специалиста'
                )
                FROM (
                    SELECT DISTINCT ON (LOWER(TRIM(domain)))
                        LOWER(TRIM(domain)) AS domain,
                        directologist,
                        direction_main
                    FROM public.local_gsheet_sites
                    ORDER BY LOWER(TRIM(domain)),
                             NULLIF(TRIM(directologist), '') NULLS LAST
                ) gs
                WHERE (ba."специалист" IS NULL OR TRIM(ba."специалист") = '')
                  AND LOWER(TRIM(ba.domain)) = gs.domain
            """)
            n1 = cur.rowcount
            # Шаг 3: домен не найден в gsheet_sites (domain=NULL или нет записи)
            cur.execute(f"""
                UPDATE public.{t}                     -- SPEC_FALLBACK_DIRECTION_2026-07-03
                SET "специалист" = 'Без специалиста'
                WHERE "специалист" IS NULL OR TRIM("специалист") = ''
            """)
            n2 = cur.rowcount
            total += n1 + n2
    conn.commit()
    logger.info('[corrections] _rule_fill_specialist_fallback: %d строк', total)
    return total


def apply_spec_fallback_v3(conn, tables: list) -> int:
    """SPEC_FALLBACK_V3_2026-07-03

    Расширение v2: применяет fallback специалиста к таблицам, которые создаются
    или дополняются ПОСЛЕ corrections.apply(), поэтому не покрываются v2:

      - big_analytics_full: звонки (step6 UNION), пиксель (step5 пересоздаёт
        big_analytics_pixel после corrections), пиксель_атрибуц (step11 доливает),
        crop_targeting строки (step10 INSERT после corrections)
      - big_analytics_full_arrival: direct/crop по дате визита (step13_rebuild
        строит независимо от big_analytics_direct, специалист NULL)

    Каскад приоритетов:
      1. directologist из local_gsheet_sites по домену (реальный специалист)
      2. direction_main из local_gsheet_sites (канал: SEO/Контекст/Посевы/...)
      3. 'Звонки' — для campaign_code='звонки' без связки в gsheet_sites
      4. 'Без специалиста' — последний resort

    Идемпотентно: только NULL/пустые строки. Заполненные (включая 'Кудерко Семен')
    не трогаются. Не создаёт 'Кудерко Семен'. Не меняет cost/атрибуцию.
    """
    total = 0
    for t in tables:
        with conn.cursor() as cur:
            # Шаг 1+2: directologist (приоритет) или direction_main по домену
            cur.execute(f"""
                UPDATE public.{t} ba          -- SPEC_FALLBACK_V3_2026-07-03
                SET "специалист" = COALESCE(
                    NULLIF(TRIM(gs.directologist), ''),
                    NULLIF(TRIM(gs.direction_main), ''),
                    CASE WHEN ba.campaign_code = 'звонки' THEN 'Звонки'
                         ELSE 'Без специалиста' END
                )
                FROM (
                    SELECT DISTINCT ON (LOWER(TRIM(domain)))
                        LOWER(TRIM(domain)) AS domain,
                        directologist,
                        direction_main
                    FROM public.local_gsheet_sites
                    ORDER BY LOWER(TRIM(domain)),
                             NULLIF(TRIM(directologist), '') NULLS LAST
                ) gs
                WHERE (ba."специалист" IS NULL OR TRIM(ba."специалист") = '')
                  AND LOWER(TRIM(ba.domain)) = gs.domain
            """)
            n1 = cur.rowcount
            # Шаг 3: домен не найден в gsheet_sites (или domain IS NULL)
            cur.execute(f"""
                UPDATE public.{t}              -- SPEC_FALLBACK_V3_2026-07-03
                SET "специалист" = CASE
                    WHEN campaign_code = 'звонки' THEN 'Звонки'
                    ELSE 'Без специалиста'
                END
                WHERE "специалист" IS NULL OR TRIM("специалист") = ''
            """)
            n2 = cur.rowcount
            total += n1 + n2
    conn.commit()
    logger.info('[corrections] apply_spec_fallback_v3 %s: %d строк', tables, total)
    return total


# ── Точка входа ───────────────────────────────────────────────────────────────

def _interim_vacuum(conn, tables=('big_analytics_direct', 'big_analytics_crop_targeting')) -> None:
    """Промежуточный VACUUM по ходу corrections — не даёт UNLOGGED-таблицам распухать.

    Контекст bloat (2026-06-08): step3 делает TRUNCATE+INSERT (чистый файл),
    далее corrections гоняет ~19 правил с массовыми UPDATE по 3+ млн строк
    (rule0b/0c/4 переписывают всю таблицу). Каждый UPDATE плодит dead-tuples;
    без промежуточного VACUUM файл direct рос до ~19 ГБ за один прогон и
    переполнял диск на step6 (could not extend file / Check free disk space).
    Финальный VACUUM в pipeline.py реклеймит dead-tuples, НО обычный VACUUM не
    возвращает место ОС (файл остаётся на high-water mark) — поэтому реклеймить
    надо ПО ХОДУ, пока high-water mark ещё низкий, чтобы dead-tuples
    переиспользовались следующими UPDATE, а не наращивали файл.

    VACUUM нельзя выполнять внутри транзакции → отдельное autocommit-соединение
    из пула config.db (тот же DST-пул, что использует pipeline для финального
    VACUUM). Параллелизм отключён (PARALLEL 0 + max_parallel_maintenance_workers=0):
    /dev/shm на этом VPS мал. Это НЕ FULL — данные/строки не меняются, оракул и
    звезда не затрагиваются.
    """
    import config.db as _db
    vac = None
    try:
        vac = _db.get_conn()
        # get_conn() пингует SELECT 1 без commit → соединение остаётся "idle in
        # transaction". set_session (=.autocommit=True) внутри открытой транзакции
        # запрещён psycopg2 ("set_session cannot be used inside a transaction") →
        # VACUUM молча падал, а в finally .autocommit=False кидал ВТОРОЕ то же
        # исключение и пробивал наружу из apply() → rule1_кудерко и последующие
        # правила НЕ выполнялись (golden-FAIL, расход 10.1M вместо 25.4M).
        # rollback() закрывает ping-tx — ровно как в pipeline.py L497 для его VACUUM.
        vac.rollback()
        vac.autocommit = True
        with vac.cursor() as cur:
            cur.execute('SET max_parallel_maintenance_workers = 0')
            cur.execute("SET maintenance_work_mem = '256MB'")
            for t in tables:
                cur.execute('SELECT to_regclass(%s)', (f'public.{t}',))
                if cur.fetchone()[0] is not None:
                    cur.execute(f'VACUUM (ANALYZE, PARALLEL 0) public.{t}')
        logger.info('[corrections] промежуточный VACUUM: %s', ', '.join(tables))
    except Exception as e:  # VACUUM не критичен — прогон продолжается
        logger.warning('[corrections] промежуточный VACUUM не удался (не критично): %s', e)
    finally:
        if vac is not None:
            # guard: .autocommit=False тоже set_session → не дать ему пробить
            # наружу из finally (иначе apply() прервётся уже на этом этапе).
            try:
                vac.autocommit = False
            except Exception as _ae:
                logger.warning('[corrections] _interim_vacuum: сброс autocommit не удался (не критично): %s', _ae)
            _db.put_conn(vac)


def apply(conn) -> dict:
    """Применяет все корректировки. Вызывается из pipeline между шагами 3 и 4."""
    total = 0
    total += _rule0a_campaign_distinct(conn)
    total += _rule0b_normalize_codes(conn)
    total += _rule0d_migrate_ct_codes(conn)
    total += _rule0c_global_ag_parts(conn)
    # VACUUM после ранних массовых UPDATE (rule0b normalize_codes + rule0c global
    # ag_parts переписывают всю таблицу) — реклеймит dead-tuples до того, как
    # файл успеет вырасти; последующие правила переиспользуют освобождённое место.
    _interim_vacuum(conn)
    total += _rule1_kudерко(conn)
    total += _rule1b_сергеев(conn)
    total += _rule1v_питеркина(conn)
    total += _rule1g_питеркина_bydate(conn)
    total += _rule2_fix_adgroup_name(conn)
    total += _rule3_reextract_adgroup_code(conn)
    total += _rule5_direct_adgroup_code_maps(conn)
    total += _rule4_recompute_ag_parts(conn)
    total += _rule4b_recompute_ag_parts_crop(conn)
    # VACUUM после recompute_ag_parts (ещё один full-table UPDATE) — финальная
    # подчистка перед остальными лёгкими правилами и сборкой full в step6.
    _interim_vacuum(conn)
    total += _rule6_validity_filter(conn)
    total += _rule7_fill_pixel(conn)
    total += _rule8_utm_classify(conn)
    total += _rule_fix_wrong_domains(conn)
    total += normalize_salons(conn, tables=COMPONENT_TABLES)
    total += _rule_perform_direction(conn)   # ← Перформ РФ → направление Перформ
    total += _fix_missing_managers(conn)
    total += _fix_account_domain_backfill(conn)
    total += _rule_fill_specialist_fallback(conn)  # SPEC_FALLBACK_DIRECTION_2026-07-03
    # VACUUM_WAVE3_2026-07-02: третий interim_vacuum после волны 3 (rule6 +
    # normalize_salons + perform_direction + фиксы). Волна 3 делает 14+ операций
    # без vacuum → 5-7 GB dead tuples к моменту step6. Этот вызов реклеймит их
    # сразу, пока step6 не начал читать big_analytics_direct.
    _interim_vacuum(conn)
    # rule9 (crmf/mauto Лидер дедуп) вынесен из apply(): вызывается pipeline.py
    # ПОСЛЕ step2 и ДО step3, чтобы флаг is_copy_for_removal был виден
    # leads_deduped (step3) и leads_base (step13) при сборке таблиц.
    return {'rows': total}
