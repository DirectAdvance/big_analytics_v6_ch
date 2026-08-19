"""ClickHouse v6 corrections hook — порт правил v5 `corrections.py`.

v5 применял ~20 правил цепочкой массовых PostgreSQL UPDATE по пяти компонентным
таблицам. ClickHouse таких UPDATE не любит, поэтому здесь ВСЕ правила выражены
как SQL-выражения и применяются ОДНОЙ пересборкой теневой таблицы
`ad_analytics.big_analytics_sources` (шесть вложенных стадий вместо шести
последовательных UPDATE). Порядок стадий воспроизводит порядок правил v5 —
он содержательный, а не косметический:

    S1  `_rule0a` → `_rule0b` → `_rule0d` → `_rule2`   (коды кампаний и групп)
    S2  `_rule3` → `_rule5`                            (adgroup_code из имени группы)
    S3  `_rule0c`/`_rule4`/`_rule4b` + `_rule6`        (ag_part1..7, верный/неверный кодер)
    S4  `_rule_fix_wrong_domains` → `normalize_salons` (домен, затем салон)
    S5  `fill_missing_regions`, `_fix_missing_managers`,
        `_fix_account_domain_backfill`, `_fix_crop_missing_utms`,
        `_rule1_*` + `apply_spec_fallback_v3`          (регион/город/менеджер/специалист)

Почему именно такой порядок (ломается при перестановке):
  * `_rule0b` («kviz»→«quiz») обязан идти ДО `_rule6`: без него фильтр валидности
    пометил бы 553 087 строк живых кампаний `tp1_cpc_kviz` как «неверный кодер».
  * ag_parts (S3) считаются от ФИНАЛЬНОГО `adgroup_code` (после S1+S2) и от
    ДО-`_rule6` значения `tp` — иначе ветка tp6/tp7 («MK/TK») не сработала бы,
    потому что `_rule6` уже переписал бы `tp` в «неверный кодер».
  * `normalize_salons` (S4) идёт ДО `fill_missing_regions` (S5): регион ищется
    по паре (салон, город), а салон к этому моменту уже канонизирован.
  * `apply_spec_fallback_v3` (S5) идёт ПОСЛЕ правил специалиста по аккаунту:
    fallback трогает только пустые значения и не должен затирать «Кудерко Семен».

Область применения — `ad_analytics.big_analytics_sources` (direct / tp8 / tp9 /
tp10 / seo / crop_targeting / direct_unmatched / direct_zero). Скоуп каждого
правила задан фильтром `_source_table`, повторяющим таблицу-цель v5:
`big_analytics_direct` → `_source_table = 'direct'` (в v5 строки tp8/tp9/tp10 к
моменту corrections уже переехали в crop через `_move_tp8_to_crop`),
`big_analytics_crop_targeting` → `tp8/tp9/tp10/crop_targeting`.

НЕ покрыто (и в v5 покрыто отдельными вызовами уже ПОСЛЕ `apply()`):
звонки (`big_analytics_calls`, step6), пиксельная атрибуция (step11) и
стоимостной оверлей посевов (step10) — эти строки появляются позже пересборки.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config.ch_db import get_client
from config.ch_utils import count_rows, swap_shadow, table_exists

logger = logging.getLogger("corrections")

SOURCE_TABLE = "ad_analytics.big_analytics_sources"
SHADOW_TABLE = "ad_analytics.big_analytics_sources_new"

# Скоупы — прямой перевод «таблица v5 → _source_table v6».
# Каждая стадия пересборки читает предыдущую под алиасом `s`, поэтому скоуп
# всегда квалифицирован: без алиаса ссылка была бы неоднозначной в стадии с JOIN.
SCOPE_DIRECT = "s.`_source_table` = 'direct'"
SCOPE_CROP = "s.`_source_table` IN ('tp8', 'tp9', 'tp10', 'crop_targeting')"
SCOPE_DIRECT_CROP = "s.`_source_table` IN ('direct', 'tp8', 'tp9', 'tp10', 'crop_targeting')"

# Кириллическая «с» — опечатка в cpc/cpa полях Яндекса (v5 corrections.py:162).
_CYR_S = "с"
_INVALID_CODE = "неверный кодер"

CORRECTIONS_QUERY_SETTINGS = {
    "max_execution_time": 900,
    "max_memory_usage": 4_000_000_000,
    "max_bytes_before_external_group_by": 1_000_000_000,
    "max_bytes_before_external_sort": 1_000_000_000,
}


def _lit(value: str) -> str:
    """SQL-литерал строки."""
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _sql_list(items) -> str:
    return ", ".join(_lit(item) for item in items)


def _transform(expr: str, mapping, default: str) -> str:
    """`transform(expr, [ключи], [значения], default)` — CH-аналог VALUES-джойна v5."""
    keys = ", ".join(_lit(key) for key, _ in mapping)
    values = ", ".join(_lit(value) for _, value in mapping)
    return f"transform({expr}, [{keys}], [{values}], {default})"


# ══════════════════════════════════════════════════════════════════════════════
# Правила специалиста по аккаунту и дате (v5 rules 1, 1б, 1в, 1г, 1д)
# ══════════════════════════════════════════════════════════════════════════════

_KUDERKO_DATE = "2026-04-10"
_KUDERKO_NAME = "Кудерко Семен"
_KUDERKO_LOGINS = (
    "e-20086621", "e-20086622", "e-20084860", "e-20084861", "e-20086619",
    "porg-gcegsszl", "e-20086660", "e-20086659", "porg-h27zek57", "e-20086658",
    "e-20077075", "e-20086657", "porg-edmpebhr", "e-20086620", "e-20084857",
    "e-20086661", "e-20086623", "e-20084859", "e-20084858", "e-20076544",
    "e-20076545", "porg-kkhtgf2u", "e-20074366", "porg-mgrauofh", "e-20078432",
    "e-20077077", "e-20077078", "e-20078433", "e-20078430", "e-20077079",
    "e-20078431", "e-20077076", "e-20078429", "porg-7yibjfp4", "e-20076541",
    "e-20074364", "porg-pzm4243t", "porg-riga5gvo", "porg-sblzprjm", "porg-xagqvz3v",
    "porg-gbj6e3ji", "porg-3q2n22ux", "porg-x7iyctbh", "porg-qruhft2a", "porg-53t6ygdz",
    "porg-qeyeclqv", "porg-iljlldjs", "porg-wlta5kmb", "porg-cs34qdr7", "porg-cz2jqzbo",
    "e-20074363", "e-20076540", "porg-uguxrece", "porg-jnbd47au", "porg-klfzrvhu",
    "porg-v6ao2xka", "porg-jelgic43", "e-20076539", "e-20074365", "porg-nen5jouv",
    "porg-vvxm6gma", "porg-rcg54tv4", "porg-bczfmt3d", "porg-tr47xrja", "porg-5v4n6spu",
    "porg-p4uskpj6", "porg-wgyzlarl",
)

_SERGEEV_DATE = "2026-04-21"
_SERGEEV_NAME = "Сергеев Алексей"
_SERGEEV_LOGINS = (
    "porg-tde4jof6", "kazan-ca-532199-z761", "e-20074360", "porg-wzisnv32",
    "porg-rmkn7sz4", "porg-2xphfcul", "e-20074359", "porg-fuko7yzw", "e-20074361",
)

_PITERKINA_LOGIN = "porg-o2lqtxk5"
_PITERKINA_DATE = "2026-06-19"
_PITERKINA_NAME = "Питеркина Дарья"
_PITERKINA_LOGINS = (
    "direct175", "e-20074386", "e-20074391", "e-20075581", "e-20076024",
    "e-20076025", "e-20076032", "e-20076035", "e-20077735", "e-20080927",
    "e-20086590", "porg-3kybbaqw", "porg-52ddldh4", "porg-a7ysf76k",
    "porg-asnsozgg", "porg-cy3l6otz", "porg-de56ixiq", "porg-dnprpowd",
    "porg-efrpw7tl", "porg-g5el6elk", "porg-hwoltj3u", "porg-nqw6yxoc",
    "porg-o2lqtxk5", "porg-orfyrlvm", "porg-ounlaznf", "porg-p6eyociq",
    "porg-rrq4agov", "porg-ze76vrem",
)

_CHEPELEV_DATE = "2026-07-17"
_CHEPELEV_NAME = "Чепелев Никита"
# CHEPELEV_LOGIN_ONLY_2026-08-06: матч ТОЛЬКО по account_login — дословный паритет с
# v5 `_rule1d_чепелев` (v5 corrections.py:890-930, `account_login = ANY(...)`). Сайты
# приведены в комментарии как происхождение списка, но в предикат НЕ входят: правило
# описывает владение АККАУНТОМ, а не парой (сайт, аккаунт), и все 4 правила специалиста
# обязаны иметь одну и ту же форму. Пара (домен, логин) сделала бы выражение зависимым
# от того, передал ли конкретный вызов домен, — а колонка `domain` на визитной и
# пиксельной осях резолвится другим алиасом (см. 6 вызовов ниже).
#   tenet-park-msk.ru, autopark-moscow.ru, tenet-auto-ufa.ru, protenet-kras.ru,
#   exeed-moreauto.ru, ural-auto-cars.site, multicars-kuban.site
_CHEPELEV_LOGINS = (
    "porg-4776zkhx", "porg-vwnkfsr6", "porg-rjykrcf7", "porg-iythq6m5",
    "direct781", "porg-gclfs2bh", "porg-n7g7whoa",
)

SPECIALIST_DATE_RULES = (
    (_KUDERKO_NAME, _KUDERKO_DATE, _KUDERKO_LOGINS),
    (_SERGEEV_NAME, _SERGEEV_DATE, _SERGEEV_LOGINS),
    (_PITERKINA_NAME, _PITERKINA_DATE, _PITERKINA_LOGINS),
    (_CHEPELEV_NAME, _CHEPELEV_DATE, _CHEPELEV_LOGINS),
)


def specialist_correction_expr(
    date_expr: str,
    account_expr: str,
    specialist_expr: str,
) -> str:
    """Return CH expression matching v5 account-based specialist corrections.

    Выражение зависит ТОЛЬКО от (дата, логин аккаунта, текущий специалист), поэтому
    все вызовы — заявочная ось, визитная ось, пиксельная атрибуция — считают одно и
    то же правило. Необязательных аргументов нет намеренно: забытый аргумент раньше
    молча менял правило на конкретной оси.
    """
    branches: list[str] = []
    account_key = f"lowerUTF8(trim(ifNull({account_expr}, '')))"
    for name, date_barrier, logins in SPECIALIST_DATE_RULES:
        branches.append(
            f"{date_expr} < toDate('{date_barrier}') "
            f"AND ({account_key} IN ({_sql_list(logins)})), {_lit(name)}"
        )
    branches.append(
        f"{account_key} = {_lit(_PITERKINA_LOGIN)} "
        f"AND empty(trim(ifNull({specialist_expr}, ''))), {_lit(_PITERKINA_NAME)}"
    )
    branches.append(specialist_expr)
    return f"multiIf({', '.join(branches)})"


# ══════════════════════════════════════════════════════════════════════════════
# Правило 0d: миграция устаревших ct-кодов площадок (v5 corrections.py:429-616)
# ══════════════════════════════════════════════════════════════════════════════

_CT_MIGRATION_MAP: dict[str, str] = {
    "ct1345": "ct0258", "ct1340": "ct0254", "ct1338": "ct0253", "ct1324": "ct0247",
    "ct1285": "ct0241", "ct1278": "ct0240", "ct1206": "ct0235", "ct1205": "ct0234",
    "ct1204": "ct0232", "ct1172": "ct0224", "ct1170": "ct0222", "ct1160": "ct0219",
    "ct1158": "ct0218", "ct1156": "ct0217", "ct1139": "ct0214", "ct1128": "ct0211",
    "ct1124": "ct0210", "ct1076": "ct0282", "ct1073": "ct0205", "ct1061": "ct0201",
    "ct1011": "ct0196", "ct1004": "ct0313", "ct0885": "ct0185", "ct0870": "ct0178",
    "ct0861": "ct0175", "ct0860": "ct0174", "ct0856": "ct0171", "ct0845": "ct0168",
    "ct0839": "ct0166", "ct0836": "ct0158", "ct0832": "ct0153", "ct0831": "ct0152",
    "ct0830": "ct0151", "ct0829": "ct0149", "ct0827": "ct0147", "ct0806": "ct0142",
    "ct0805": "ct0141", "ct0799": "ct0131", "ct0772": "ct0129", "ct0769": "ct0126",
    "ct0726": "ct0285", "ct0722": "ct0285", "ct0715": "ct0285", "ct0710": "ct0000",
    "ct0708": "ct0119", "ct0707": "ct0118", "ct0706": "ct0117", "ct0704": "ct0116",
    "ct0703": "ct0115", "ct0700": "ct0113", "ct0673": "ct0106", "ct0672": "ct0105",
    "ct0671": "ct0104", "ct0659": "ct0101", "ct0657": "ct0100", "ct0652": "ct0099",
    "ct0646": "ct0091", "ct0599": "ct0085", "ct0597": "ct0083", "ct0596": "ct0081",
    "ct0593": "ct0082", "ct0590": "ct0078", "ct0589": "ct0077", "ct0576": "ct0072",
    "ct0575": "ct0071", "ct0566": "ct0000", "ct0565": "ct0000", "ct0562": "ct0000",
    "ct0561": "ct0000", "ct0523": "ct0062", "ct0512": "ct0059", "ct0507": "ct0059",
    "ct0503": "ct0044", "ct0500": "ct0055", "ct0493": "ct0050", "ct0492": "ct0049",
    "ct0486": "ct0046", "ct0461": "ct0043", "ct0460": "ct0042", "ct0453": "ct0029",
    "ct0452": "ct0029", "ct0449": "ct0029", "ct0443": "ct0034", "ct0441": "ct0033",
    "ct0437": "ct0031", "ct0375": "ct0028", "ct0373": "ct0023", "ct0371": "ct0021",
    "ct0357": "ct0312", "ct0356": "ct0312", "ct0354": "ct0312", "ct0327": "ct0000",
    "ct0325": "ct0000", "ct0324": "ct0000", "ct0355": "ct0312", "ct0353": "ct0312",
    "ct0374": "ct0027", "ct0372": "ct0022", "ct0370": "ct0020", "ct0458": "ct0040",
    "ct0456": "ct0039", "ct0451": "ct0029", "ct0450": "ct0029", "ct0448": "ct0029",
    "ct0445": "ct0036", "ct0440": "ct0032", "ct0438": "ct0032", "ct0433": "ct0030",
    "ct0502": "ct0058", "ct0498": "ct0054", "ct0495": "ct0052", "ct0467": "ct0045",
    "ct0522": "ct0000", "ct0509": "ct0060", "ct0508": "ct0000", "ct0560": "ct0065",
    "ct0567": "ct0000", "ct0564": "ct0000", "ct0563": "ct0000", "ct0581": "ct0075",
    "ct0572": "ct0066", "ct0592": "ct0080", "ct0591": "ct0079", "ct0600": "ct0086",
    "ct0598": "ct0084", "ct0674": "ct0107", "ct0660": "ct0102", "ct0651": "ct0098",
    "ct0709": "ct0120", "ct0701": "ct0114", "ct0699": "ct0112", "ct0731": "ct0285",
    "ct0719": "ct0285", "ct0718": "ct0285", "ct0714": "ct0285", "ct0744": "ct0122",
    "ct0800": "ct0133", "ct0828": "ct0148", "ct0825": "ct0145", "ct0824": "ct0144",
    "ct0835": "ct0157", "ct0834": "ct0156", "ct0866": "ct0177", "ct0862": "ct0176",
    "ct0859": "ct0173", "ct0841": "ct0167", "ct0890": "ct0190", "ct1000": "ct0313",
    "ct1072": "ct0203", "ct1069": "ct0202", "ct1074": "ct0207", "ct1136": "ct0213",
    "ct1132": "ct0212", "ct1155": "ct0216", "ct1171": "ct0223",
}

# Все ct-коды ровно 6 символов → префикс с разделителем = 7 символов.
_CT_CODE_MAP = tuple((f"{old}_", new) for old, new in _CT_MIGRATION_MAP.items())
_CT_NAME_MAP = tuple((f"{old}_", f"{new}_") for old, new in _CT_MIGRATION_MAP.items())


# ══════════════════════════════════════════════════════════════════════════════
# Правила 2/3/5: имена групп и прямые маппинги adgroup_code
# ══════════════════════════════════════════════════════════════════════════════

_ADGROUP_FIX_ACCOUNTS = (
    "e-20076545", "e-20074366",
    "cars-yekaterinburg-541349-lrqf",
    "porg-akiqrh6u",
    "e-20085128", "e-20085130", "e-20085132", "e-20085135", "e-20086083",
    "porg-p7pymm3m",
    "byautos-34-533635-yj8u", "porg-eezad6ih",
    "porg-mduvg6db", "e-20084938", "porg-p7bjp76w",
    "e-20077730", "newcar-siberiya-526203-n5f1",
    "porg-g3gqnefi",
)

_NEW_GROUP_ACCOUNTS = ("e-20085128", "e-20085130", "e-20085132", "e-20085135", "e-20086083")
_NEW_GROUP_NAME = "ct0021_aon_n000_r0121_ct001_ag011_g00 — 3. baic u5 plus"
_P7PYMM3M_GROUP_NAME = "ct0000_aon_n000_r0017_ct001_ag011_g00 — общая"

# porg-g3gqnefi: ЕПК и товарная кампания — переименование под нейминг + коды.
_G3GQNEFI_EPK_ID = 707572731
_G3GQNEFI_EPK_NAME = (
    "tp8_cpc_site — Telegram - Haval - Автотаргетинг - Волгоградская область "
    "— haval-vlg_тест ТГ неавтошка"
)
_G3GQNEFI_EPK_GROUP = "ct0111_aon_n000_r0086_ct018_ag001_g00"
_G3GQNEFI_TK_ID = 707568038
_G3GQNEFI_TK_NAME = "tp7_cpc_site_ct0111_aon_n000_r00 ф86_ct010_ag001_g00 — тест епк мод неавтошка"
_G3GQNEFI_TK_GROUP = "ct0111_aon_n000_r0086_ct010_ag001_g00"

_BYAUTOS_MAP = (
    ("avitobu_aon_n000_volgogrado_tgo_a25-54_genders", "ct0009_aoff_n000_r0086_ct001_ag011_g00"),
    ("avtobu_aon_n000_volgogrado_tgo_a25-54_genders", "ct0014_aoff_n000_r0086_ct001_ag011_g00"),
    ("avtoprobeg_aon_n000_volgogrado_tgo_a25-54_genders", "ct0014_aoff_n000_r0086_ct001_ag011_g00"),
    ("avtosalon_aon_n000_volgogrado_tgo_a25-54_genders", "ct0013_aoff_n000_r0086_ct001_ag011_g00"),
    ("buybu_aon_n000_volgogrado_tgo_a25-54_genders", "ct0014_aoff_n000_r0086_ct001_ag011_g00"),
    ("chevrolet_aon_n000_volgogrado_cat_age_genders", "ct0058_aon_n000_r0086_ct001_ag011_g00"),
    ("chinacarbu_aon_n000_volgogrado_tgo_a25-54_genders", "ct0001_aoff_n000_r0086_ct001_ag011_g00"),
    ("drom_aon_n000_volgogrado_tgo_a25-54_genders", "ct0010_aoff_n000_r0086_ct001_ag011_g00"),
    ("hyundai_aon_n000_volgogrado_cat_age_genders", "ct0121_aon_n000_r0086_ct001_ag011_g00"),
    ("kia_aon_n000_volgogrado_cat_age_genders", "ct0164_aon_n000_r0086_ct001_ag011_g00"),
    ("kia-ceed_aon_n000_volgogrado_tgo_a25-54_genders", "ct0166_aon_n000_r0086_ct001_ag011_g00"),
    ("kia-rio_aon_n000_volgogrado_tgo_a25-54_genders", "ct0173_aon_n000_r0086_ct001_ag011_g00"),
    ("kia-seltos_aon_n000_volgogrado_tgo_a25-54_genders", "ct0174_aon_n000_r0086_ct001_ag011_g00"),
    ("kia-soul_aon_n000_volgogrado_tgo_a25-54_genders", "ct0176_aon_n000_r0086_ct001_ag011_g00"),
    ("lada-granta_aon_n000_volgogrado_tgo_a25-54_genders", "ct0183_aon_n000_r0086_ct001_ag011_g00"),
    ("lada-vesta_aon_n000_volgogrado_ct_a25-54_genders", "ct0189_aon_n000_r0086_ct001_ag011_g00"),
    ("lada-vesta_aon_n000_volgogrado_tgo_a25-54_genders", "ct0189_aon_n000_r0086_ct001_ag011_g00"),
    ("mazda_aon_n000_volgogrado_cat_age_genders", "ct0283_aon_n000_r0086_ct001_ag011_g00"),
    ("mitsubishi_aon_n000_volgogrado_cat_age_genders", "ct0264_aon_n000_r0086_ct001_ag011_g00"),
    ("nissan_aon_n000_volgogrado_cat_age_genders", "ct0199_aon_n000_r0086_ct001_ag011_g00"),
    ("notype_aon_n000_volgogrado_cat_a25-54_genders", "ct0014_aon_n000_r0086_ct010_ag011_g00"),
    ("notype_aon_n000_volgogrado_fid_a25-54_genders", "ct0014_aon_n000_r0086_ct010_ag011_g00"),
    ("opel_aon_n000_volgogrado_cat_age_genders", "ct0282_aon_n000_r0086_ct001_ag011_g00"),
    ("renault_aon_n000_volgogrado_cat_age_genders", "ct0209_aon_n000_r0086_ct001_ag011_g00"),
    ("skoda_aon_n000_volgogrado_cat_age_genders", "ct0215_aon_n000_r0086_ct001_ag011_g00"),
    ("suzuki_aon_n000_volgogrado_cat_age_genders", "ct0277_aon_n000_r0086_ct001_ag011_g00"),
    ("volkswagen_aon_n000_volgogrado_cat_age_genders", "ct0238_aon_n000_r0086_ct001_ag011_g00"),
)

_PORG_EEZAD6IH_MAP = (
    ("buy-auto_aon_n000_hantobl_tgo_age_genders", "ct0014_aoff_n000_r0125_ct001_ag011_g00"),
    ("chery_aon_n000_hantobl_tgo_age_genders", "ct0044_aon_n000_r0125_ct001_ag011_g00"),
    ("geely_aon_n000_hantobl_tgo_age_genders", "ct0097_aon_n000_r0125_ct001_ag011_g00"),
    ("geely-atlas-new_aon_n000_hantobl_tgo_age_genders", "ct0098_aon_n000_r0125_ct001_ag011_g00"),
    ("geely-atlas-pro_aon_n000_hantobl_tgo_age_genders", "ct0099_aon_n000_r0125_ct001_ag011_g00"),
    ("geely-coolray-new_aon_n000_hantobl_tgo_age_genders", "ct0101_aon_n000_r0125_ct001_ag011_g00"),
    ("geely-emgrand_aon_n000_hantobl_tgo_age_genders", "ct0102_aon_n000_r0125_ct001_ag011_g00"),
    ("geely-tugella_aon_n000_hantobl_tgo_age_genders", "ct0107_aon_n000_r0125_ct001_ag011_g00"),
    ("haval_aon_n000_hantobl_tgo_age_genders", "ct0111_aon_n000_r0125_ct001_ag011_g00"),
    ("haval-dargo_aon_n000_hantobl_tgo_age_genders", "ct0112_aon_n000_r0125_ct001_ag011_g00"),
    ("haval-f7_aon_n000_hantobl_tgo_age_genders", "ct0113_aon_n000_r0125_ct001_ag011_g00"),
    ("haval-h3_aon_n000_hantobl_tgo_age_genders", "ct0115_aon_n000_r0125_ct001_ag011_g00"),
    ("haval-jolion-new_aon_n000_hantobl_tgo_age_genders", "ct0119_aon_n000_r0125_ct001_ag011_g00"),
    ("haval-m6_aon_n000_hantobl_tgo_age_genders", "ct0120_aon_n000_r0125_ct001_ag011_g00"),
    ("hyundai_aon_n000_hantobl_tgo_age_genders", "ct0121_aon_n000_r0125_ct001_ag011_g00"),
    ("hyundai-creta_aon_n000_hantobl_tgo_age_genders", "ct0122_aon_n000_r0125_ct001_ag011_g00"),
    ("hyundai-solaris_aon_n000_hantobl_tgo_age_genders", "ct0126_aon_n000_r0125_ct001_ag011_g00"),
    ("hyundai-tucson_aon_n000_hantobl_tgo_age_genders", "ct0129_aon_n000_r0125_ct001_ag011_g00"),
    ("kia_aon_n000_hantobl_tgo_age_genders", "ct0164_aon_n000_r0125_ct001_ag011_g00"),
    ("kia-ceed-sw_aon_n000_hantobl_tgo_age_genders", "ct0166_aon_n000_r0125_ct001_ag011_g00"),
    ("kia-rio-x_aon_n000_hantobl_tgo_age_genders", "ct0173_aon_n000_r0125_ct001_ag011_g00"),
    ("lada_aon_n000_hantobl_tgo_age_genders", "ct0181_aon_n000_r0125_ct001_ag011_g00"),
    ("lada-granta_aon_n000_hantobl_tgo_age_genders", "ct0183_aon_n000_r0125_ct001_ag011_g00"),
    ("lada-granta-cross_aon_n000_hantobl_tgo_age_genders", "ct0183_aon_n000_r0125_ct001_ag011_g00"),
    ("lada-granta-drive-active_aon_n000_hantobl_tgo_age_genders", "ct0183_aon_n000_r0125_ct001_ag011_g00"),
    ("lada-granta-hatchback_aon_n000_hantobl_tgo_age_genders", "ct0183_aon_n000_r0125_ct001_ag011_g00"),
    ("lada-granta-liftback_aon_n000_hantobl_tgo_age_genders", "ct0183_aon_n000_r0125_ct001_ag011_g00"),
    ("lada-largus_aon_n000_hantobl_tgo_age_genders", "ct0185_aon_n000_r0125_ct001_ag011_g00"),
    ("lada-niva-legend_aon_n000_hantobl_tgo_age_genders", "ct0186_aon_n000_r0125_ct001_ag011_g00"),
    ("lada-niva-travel_aon_n000_hantobl_tgo_age_genders", "ct0188_aon_n000_r0125_ct001_ag011_g00"),
    ("lada-vesta_aon_n000_hantobl_tgo_age_genders", "ct0189_aon_n000_r0125_ct001_ag011_g00"),
    ("lada-vesta-cross-new_aon_n000_hantobl_tgo_age_genders", "ct0189_aon_n000_r0125_ct001_ag011_g00"),
    ("lada-vesta-sw_aon_n000_hantobl_tgo_age_genders", "ct0189_aon_n000_r0125_ct001_ag011_g00"),
    ("lada-vesta-sw-cross-new_aon_n000_hantobl_tgo_age_genders", "ct0189_aon_n000_r0125_ct001_ag011_g00"),
    ("lada-xray_aon_n000_hantobl_tgo_age_genders", "ct0190_aon_n000_r0125_ct001_ag011_g00"),
    ("lada-xray-cross_aon_n000_hantobl_tgo_age_genders", "ct0190_aon_n000_r0125_ct001_ag011_g00"),
    ("nissan_aon_n000_hantobl_tgo_age_genders", "ct0199_aon_n000_r0125_ct001_ag011_g00"),
    ("nissan-x-trail_aon_n000_hantobl_tgo_age_genders", "ct0203_aon_n000_r0125_ct001_ag011_g00"),
    ("nissan-x-trail_aon_n000_hantobl_tgo_age_genders Nissan X-Trail", "ct0203_aon_n000_r0125_ct001_ag011_g00"),
    ("notype_aon_n000_hantobl_tg_age_genders", "ct0000_aon_n000_r0125_ct001_ag011_g00"),
    ("renault_aon_n000_hantobl_tgo_age_genders", "ct0209_aon_n000_r0125_ct001_ag011_g00"),
    ("renault-duster_aon_n000_hantobl_tgo_age_genders", "ct0211_aon_n000_r0125_ct001_ag011_g00"),
    ("renault-sandero_aon_n000_hantobl_tgo_age_genders", "ct0214_aon_n000_r0125_ct001_ag011_g00"),
    ("volkswagen_aon_n000_hantobl_tgo_age_genders", "ct0238_aon_n000_r0125_ct001_ag011_g00"),
    ("volkswagen-polo_aon_n000_hantobl_tgo_age_genders", "ct0241_aon_n000_r0125_ct001_ag011_g00"),
)

# (account_login, старый код, новый код) — direct + crop.
_ADGROUP_CODE_REPLACEMENTS = (
    ("porg-mduvg6db", "ct018_aoff_n000_r0105_ct001_ag001_g00", "ct0000_aoff_n000_r0105_ct018_ag001_g00"),
    ("e-20084938", "ct018_aoff_n000_r0088_ct001_ag001_g00", "ct0000_aoff_n000_r0088_ct018_ag001_g00"),
    ("porg-p7bjp76w", "ct018_aoff_n000_r0074_ct001_ag001_g00", "ct0000_aoff_n000_r0074_ct018_ag001_g00"),
    ("e-20077730", "ct0000_aon_n000_r0107_ct001_ag011_g00", "ct0000_aon_n000_r0105_ct001_ag011_g00"),
    ("newcar-siberiya-526203-n5f1", "ct0000_aon_n000_r0123_ct001_ag011_g00", "ct0000_aon_n000_r0105_ct001_ag011_g00"),
)

_AG_CODE_PATTERN = "^ct[0-9]+_(aoff|aon)_n[0-9]+_r[0-9]+_ct[0-9]+_ag[0-9]+_g[0-9]+$"
_AG_CODE_EXTRACT = "(ct[0-9]+_(aoff|aon)_n[0-9]+_r[0-9]+_ct[0-9]+_ag[0-9]+_g[0-9]+)"
_CAMPAIGN_CODE_PATTERN = "^tp[0-9]+_(cpc|cpa)_(site|quiz)$"


# ══════════════════════════════════════════════════════════════════════════════
# Правила меток: домены, салоны, регионы, менеджеры
# ══════════════════════════════════════════════════════════════════════════════

# (account_login, неверный домен, верный домен) — v5 `_rule_fix_wrong_domains`.
_DOMAIN_FIXES = (
    ("porg-vf3dnoy5", "topcars-msk.ru", "new-cars-msk.ru"),
    ("e-20077590", "newauto-msk.ru", "topcars-msk.ru"),
    ("e-20077590", "new-cars-msk.ru", "topcars-msk.ru"),
    ("porg-jgcggtud", "driveauto-nvg.ru", "driveavto-nvg.ru"),
    ("e-20077729", "topcars-msk.ru", "newauto-msk.ru"),
    ("e-20080928", "tenet-77.ru", "geelyauto-msk.ru"),
    ("e-20080928", "newauto-msk.ru", "geelyauto-msk.ru"),
    ("porg-3kybbaqw", "tenet-stavropol.ru", "tenet-autokrd.ru"),
    ("porg-bnw3mip5", "old_drive-auto-134.ru", "drive-auto-134.ru"),
    ("e-20085690", "topcars-msk.ru", "tenet-77.ru"),
    ("e-20078823", "ladapro-26.ru", "carsclad-vlg.ru"),
)

# Глобальные алиасы салонов: неверное написание → правильное.
SALON_ALIASES: dict[str, str] = {
    "Центр Авто Казань": "Казань Центр Авто",
    "АЦ Кит-Авто": "Кит-Авто",
    "АЦ на Жукова": "Автоцентр на Жукова",
    "АвтоПарк Южный": "Автопарк Южный",
    "М-Авто": "М-авто",
    "Южный обход": "Южный Обход",
}

# Домены без записи в gsheet_sites → (салон, город, регион).
DOMAIN_SITE_MAP: dict[str, tuple[str, str, str]] = {
    "autotorg-ekb.ru": ("Кит-Авто", "Екатеринбург", "Свердловская область"),
    "buymashina-e.ru": ("АвтоМаркет", "Екатеринбург", "Свердловская область"),
    "probeg-ek.ru": ("АвтоМаркет", "Екатеринбург", "Свердловская область"),
}
DOMAIN_SALON_MAP: dict[str, str] = {key: value[0] for key, value in DOMAIN_SITE_MAP.items()}

_MISSING_MANAGERS = (("lotos91.ru", "Михаил Яковлев"),)

# Аккаунты без login_key в gsheet_sites, чей домен размечен под другим login_key.
_ACCOUNT_DOMAIN_BACKFILL = (("porg-rtowqong", "autopark-102.ru"),)
_BACKFILL_FIELDS = (
    "статус", "салон", "город", "регион", "direction",
    "менеджер", "проджект", "id_салона", "тип_сайта", "шаблон", "специалист",
)

# UTM-кампании, отсутствующие в gsheets_crop_targeting_account → салон/город.
_CROP_UTM_BACKFILL = (
    ("carnew-vlg.ru", "Автоцентр на Жукова", "Волгоград"),
)


def _word_sort_key(col_sql: str) -> str:
    """Word-sort ключ матчинга салонов — CH-двойник v5 `salon_match_key`.

    Детерминирован (НЕ fuzzy): lowerUTF8 → trim → пунктуация в пробел → схлоп
    пробелов → слова → сортировка → склейка. Любая перестановка слов даёт ОДИН
    ключ. `lowerUTF8`, а не `lower`: `lower` в ClickHouse — ASCII-only и кириллицу
    не трогает, из-за чего 'Центр Авто' и 'центр авто' дали бы разные ключи.
    """
    cleaned = (
        f"replaceRegexpAll(replaceRegexpAll(lowerUTF8(trim(ifNull({col_sql}, ''))), "
        "'[^\\\\p{L}\\\\p{N}\\\\s]', ' '), '\\\\s+', ' ')"
    )
    return f"arrayStringConcat(arraySort(arrayFilter(x -> x != '', splitByChar(' ', {cleaned}))), ' ')"


# ══════════════════════════════════════════════════════════════════════════════
# Стадии пересборки
# ══════════════════════════════════════════════════════════════════════════════


def _scoped(scope: str, corrected: str, original: str) -> str:
    """Применить выражение только внутри скоупа `_source_table` (иначе — как было)."""
    return f"if({scope}, {corrected}, {original})"


def _stage1_codes() -> str:
    """S1: правила 0a, 0b, 0d, 2 — коды кампаний, имена и коды групп."""
    # ── AdGroupName: 0d (миграция ct) → 2 (опечатки по аккаунтам) ──────────────
    # ct-код ровно 6 символов, поэтому префикс с разделителем — первые 7,
    # а остаток имени начинается с 8-го (v5: SUBSTRING(name FROM 8)).
    name_prefix = "left(ifNull(s.`AdGroupName`, ''), 7)"
    name_lookup = _transform(name_prefix, _CT_NAME_MAP, "''")
    name_migrated = (
        f"if({name_lookup} != '', concat({name_lookup}, "
        "substring(ifNull(s.`AdGroupName`, ''), 8)), s.`AdGroupName`)"
    )
    name_migrated = _scoped(SCOPE_DIRECT_CROP, name_migrated, "s.`AdGroupName`")
    acc = "lowerUTF8(trim(ifNull(s.account_login, '')))"
    name_fixed = f"""multiIf(
        {acc} IN ('e-20076545', 'e-20074366') AND position(NAME_SRC, '_g011_g') > 0,
            replaceAll(NAME_SRC, '_g011_g', '_ag011_g'),
        {acc} = 'e-20076545' AND position(NAME_SRC, '_off_') > 0 AND position(NAME_SRC, '_aoff_') = 0,
            replaceAll(NAME_SRC, '_off_', '_aoff_'),
        {acc} = 'cars-yekaterinburg-541349-lrqf' AND position(NAME_SRC, '_n000_n000_') > 0,
            replaceAll(NAME_SRC, '_n000_n000_', '_n000_'),
        {acc} = 'porg-akiqrh6u',
            replaceAll(replaceAll(replaceAll(replaceAll(NAME_SRC,
                'ct0054aoff', 'ct0054_aoff'), 'ct0054aon', 'ct0054_aon'),
                'ct0076aoff', 'ct0076_aoff'), 'ct0076aon', 'ct0076_aon'),
        {acc} IN ({_sql_list(_NEW_GROUP_ACCOUNTS)}) AND NAME_SRC = 'Новая группа', {_lit(_NEW_GROUP_NAME)},
        {acc} = 'porg-p7pymm3m' AND NAME_SRC = 'Новая группа', {_lit(_P7PYMM3M_GROUP_NAME)},
        {acc} = 'porg-g3gqnefi' AND s.`CampaignId` = {_G3GQNEFI_EPK_ID} AND NAME_SRC = 'Целевая аудитория',
            {_lit(_G3GQNEFI_EPK_GROUP)},
        NAME_SRC)""".replace("NAME_SRC", f"ifNull({name_migrated}, '')")
    ad_group_name = _scoped(SCOPE_DIRECT, name_fixed, name_migrated)

    # ── CampaignName: правило 2 для porg-g3gqnefi ──────────────────────────────
    campaign_name = _scoped(
        SCOPE_DIRECT,
        f"""multiIf(
        {acc} = 'porg-g3gqnefi' AND s.`CampaignId` = {_G3GQNEFI_EPK_ID}, {_lit(_G3GQNEFI_EPK_NAME)},
        {acc} = 'porg-g3gqnefi' AND s.`CampaignId` = {_G3GQNEFI_TK_ID}, {_lit(_G3GQNEFI_TK_NAME)},
        s.`CampaignName`)""",
        "s.`CampaignName`",
    )

    # ── campaign_code / tp / cpc_cpa / site_quiz: 0a → 0b → 2 ──────────────────
    # 0a: последний валидный код кампании распространяется на все строки CampaignId.
    has_latest = "s.`CampaignId` IS NOT NULL AND ifNull(lc.campaign_code, '') != ''"
    code_0a = f"if({has_latest}, lc.campaign_code, s.campaign_code)"
    tp_0a = f"if({has_latest}, lc.tp, s.tp)"
    cpc_0a = f"if({has_latest}, lc.cpc_cpa, s.cpc_cpa)"
    quiz_0a = f"if({has_latest}, lc.site_quiz, s.site_quiz)"
    # 0b: kviz→quiz, Kviz→Quiz, кириллическая с→c, tp8_cpa_site→tp8_cpc_site.
    code_0b = (
        f"if(ifNull(CODE_SRC, '') != '' AND CODE_SRC != {_lit(_INVALID_CODE)}, "
        "replaceAll(replaceAll(replaceAll(replaceAll(CODE_SRC, 'kviz', 'quiz'), 'Kviz', 'Quiz'), "
        f"{_lit('cp' + _CYR_S)}, 'cpc'), 'tp8_cpa_site', 'tp8_cpc_site'), CODE_SRC)"
    ).replace("CODE_SRC", code_0a)
    g3_epk = f"{acc} = 'porg-g3gqnefi' AND s.`CampaignId` = {_G3GQNEFI_EPK_ID}"
    g3_tk = f"{acc} = 'porg-g3gqnefi' AND s.`CampaignId` = {_G3GQNEFI_TK_ID}"
    campaign_code = _scoped(
        SCOPE_DIRECT,
        f"multiIf({g3_epk}, 'tp8_cpc_site', {g3_tk}, 'tp7_cpc_site', {code_0b})",
        "s.campaign_code",
    )
    tp_col = _scoped(SCOPE_DIRECT, f"multiIf({g3_epk}, 'tp8', {g3_tk}, 'tp7', {tp_0a})", "s.tp")
    cpc_cpa = _scoped(SCOPE_DIRECT, f"multiIf({g3_epk} OR {g3_tk}, 'cpc', {cpc_0a})", "s.cpc_cpa")
    site_quiz = _scoped(SCOPE_DIRECT, f"multiIf({g3_epk} OR {g3_tk}, 'site', {quiz_0a})", "s.site_quiz")

    # ── adgroup_code: 0b (нормализация опечаток) → 0d (миграция ct) ────────────
    code_norm = (
        r"replaceRegexpOne(replaceRegexpOne(replaceRegexpAll("
        r"replaceAll(replaceAll(replaceAll(replaceAll(replaceAll(replaceAll(s.adgroup_code,"
        r" 'ct00173', 'ct0173'), 'ct09_ag', 'ct009_ag'), 'ag0011', 'ag011'),"
        r" '_ct0018_ag', '_ct018_ag'), '_n000_n000_', '_n000_'), '_off_', '_aoff_'),"
        r" '([^_])(aon|aoff)', '\\1_\\2'), '_g0$', '_g00'), '(_g[0-9]{2})\\S+', '\\1')"
    )
    guard = f"ifNull(s.adgroup_code, '') != '' AND s.adgroup_code != {_lit(_INVALID_CODE)}"
    ag_0b = _scoped(SCOPE_DIRECT, f"if({guard}, {code_norm}, s.adgroup_code)", "s.adgroup_code")
    ct_lookup = _transform(f"left(ifNull({ag_0b}, ''), 7)", _CT_CODE_MAP, "''")
    ag_0d = (
        f"if({guard} AND {ct_lookup} != '', concat({ct_lookup}, substring(ifNull({ag_0b}, ''), 7)), {ag_0b})"
    )
    adgroup_code = _scoped(SCOPE_DIRECT_CROP, ag_0d, ag_0b)

    return f"""
    {ad_group_name} AS `AdGroupName`,
    {campaign_name} AS `CampaignName`,
    {campaign_code} AS campaign_code,
    {tp_col} AS tp,
    {cpc_cpa} AS cpc_cpa,
    {site_quiz} AS site_quiz,
    {adgroup_code} AS adgroup_code
"""


def _stage2_reextract() -> str:
    """S2: правило 3 — переизвлечение adgroup_code из исправленного имени группы.

    Отдельная стадия, а не часть S1: правило читает `AdGroupName` уже ПОСЛЕ
    правил 0d и 2, а внутри одного `REPLACE` выражения видят исходные колонки.
    v5 пишет NULL, если regexp не совпал (`REGEXP_MATCH` → NULL) — повторяем.
    """
    acc = "lowerUTF8(trim(ifNull(s.account_login, '')))"
    name_head = "splitByString(' — ', ifNull(s.`AdGroupName`, ''))[1]"
    empty_code = f"(ifNull(s.adgroup_code, '') = '' OR s.adgroup_code = {_lit(_INVALID_CODE)})"
    extracted = f"extract({name_head}, {_lit(_AG_CODE_EXTRACT)})"
    rule3 = (
        f"if({acc} IN ({_sql_list(_ADGROUP_FIX_ACCOUNTS)}) AND {empty_code} "
        f"AND ifNull(s.`AdGroupName`, '') != '', nullIf({extracted}, ''), s.adgroup_code)"
    )
    return f"{_scoped(SCOPE_DIRECT, rule3, 's.adgroup_code')} AS adgroup_code"


def _stage3_adgroup_maps() -> str:
    """S3: правило 5 — прямые маппинги adgroup_code по аккаунту."""
    acc = "lowerUTF8(trim(ifNull(s.account_login, '')))"
    name_head = "splitByString(' — ', ifNull(s.`AdGroupName`, ''))[1]"
    empty_code = f"(ifNull(s.adgroup_code, '') = '' OR s.adgroup_code = {_lit(_INVALID_CODE)})"

    # 5а/5б: нестандартные имена групп двух аккаунтов → код по префиксу имени.
    byautos = _transform(name_head, _BYAUTOS_MAP, "''")
    eezad = _transform(name_head, _PORG_EEZAD6IH_MAP, "''")
    by_name = f"""multiIf(
        {acc} = 'byautos-34-533635-yj8u' AND {empty_code} AND {byautos} != '', {byautos},
        {acc} = 'porg-eezad6ih' AND {empty_code} AND {eezad} != '', {eezad},
        s.adgroup_code)"""
    direct_code = _scoped(SCOPE_DIRECT, by_name, "s.adgroup_code")

    # 5в: прямые замены кода по паре (аккаунт, старый код) — direct + crop.
    replacements = " ".join(
        f"{acc} = {_lit(account)} AND ifNull(s.adgroup_code, '') = {_lit(old)}, {_lit(new)},"
        for account, old, new in _ADGROUP_CODE_REPLACEMENTS
    )
    adgroup_code = _scoped(
        SCOPE_DIRECT_CROP, f"multiIf({replacements} {direct_code})", direct_code
    )

    # Имя группы, совпадающее со старым кодом, переписывается вместе с кодом.
    name_replacements = " ".join(
        f"{acc} = {_lit(account)} AND ifNull(s.adgroup_code, '') = {_lit(old)} "
        f"AND ifNull(s.`AdGroupName`, '') = {_lit(old)}, {_lit(new)},"
        for account, old, new in _ADGROUP_CODE_REPLACEMENTS
    )
    ad_group_name = _scoped(
        SCOPE_DIRECT_CROP, f"multiIf({name_replacements} s.`AdGroupName`)", "s.`AdGroupName`"
    )

    return f"""
    {adgroup_code} AS adgroup_code,
    {ad_group_name} AS `AdGroupName`
"""


def _naming_joins() -> str:
    """7 LEFT JOIN к `reference_data.gsheet_naming` — по одному на ag_part1..7.

    ⚠️ Уникальность пары (type, code) в справочнике НЕ гарантирована ни ключом, ни
    проверкой: это гугл-таблица. Появится дубль — LEFT JOIN размножит строку витрины,
    и это НЕ тихая порча: гейт `_invariants` сверяет rows/расход/воронку до и после
    пересборки и уронит шаг (fail-closed). На 2026-08-06 дублей 0.
    """
    return "\n".join(
        f"LEFT JOIN reference_data.gsheet_naming n{idx} "
        f"ON n{idx}.type = 'ag_part{idx}' "
        f"AND lowerUTF8(splitByChar('_', ifNull(s.adgroup_code, ''))[{idx}]) = lowerUTF8(ifNull(n{idx}.code, ''))"
        for idx in range(1, 8)
    )


def _stage4_ag_parts() -> str:
    """S4: ag_part1..7 + «неверный_кодер_new» (правила 0c/4/4б) и правило 6.

    Правило 6 живёт здесь же намеренно: оно читает ИСХОДНЫЙ `tp` этой стадии
    (после S1, до каскада «неверный кодер»), ровно как в v5, где 0c считался
    раньше rule6. Если поменять местами — ветка tp6/tp7 («MK/TK») перестанет
    срабатывать, потому что `tp` уже был бы переписан.
    """
    code = "ifNull(s.adgroup_code, '')"
    valid = f"match({code}, {_lit(_AG_CODE_PATTERN)})"
    tp67 = "ifNull(s.tp, '') IN ('tp6', 'tp7')"
    guard = f"({code} != '' AND {code} != {_lit(_INVALID_CODE)})"
    scope = f"({SCOPE_DIRECT_CROP})"

    parts = []
    for idx in range(1, 8):
        piece = f"splitByChar('_', {code})[{idx}]"
        # ifNull вокруг имени обязателен: без него concat наследует Nullable от
        # `gsheet_naming.name`, и колонка ag_partN сменила бы тип String → Nullable(String).
        named = (
            f"if(ifNull(n{idx}.name, '') != '', concat({piece}, ' - ', ifNull(n{idx}.name, '')), {piece})"
        )
        value = f"multiIf({tp67}, 'MK/TK', {valid}, {named}, {_lit(_INVALID_CODE)})"
        parts.append(f"if({scope} AND {guard}, {value}, s.ag_part{idx}) AS ag_part{idx}")

    all_named = " AND ".join(f"ifNull(n{idx}.name, '') != ''" for idx in range(1, 8))
    verdict = (
        f"multiIf({tp67}, CAST(NULL, 'Nullable(String)'), "
        f"NOT {valid}, {_lit(_INVALID_CODE)}, "
        f"{all_named}, 'верный кодер', {_lit(_INVALID_CODE)})"
    )
    parts.append(f"if({scope} AND {guard}, {verdict}, s.`неверный_кодер_new`) AS `неверный_кодер_new`")
    # tp6/tp7 с пустым кодом получают 'MK/TK' (v5 _recompute_ag_parts, шаг 3).
    parts.append(
        f"if({scope} AND {guard} AND {tp67} AND {code} = '', 'MK/TK', s.adgroup_code) AS adgroup_code"
    )

    # ── Правило 6: фильтр валидности campaign_code (+ каскад на tp/cpc_cpa) ────
    cc = "ifNull(s.campaign_code, '')"
    invalid_cc = (
        f"({cc} != '' AND {cc} != {_lit(_INVALID_CODE)} "
        f"AND NOT match(lowerUTF8({cc}), {_lit(_CAMPAIGN_CODE_PATTERN)}))"
    )
    cascade = f"({cc} = {_lit(_INVALID_CODE)})"
    direct_scope = f"({SCOPE_DIRECT})"
    parts.append(
        f"if({direct_scope} AND {invalid_cc}, {_lit(_INVALID_CODE)}, s.campaign_code) AS campaign_code"
    )
    parts.append(
        f"if({direct_scope} AND ({invalid_cc} OR {cascade}), {_lit(_INVALID_CODE)}, s.tp) AS tp"
    )
    parts.append(
        f"if({direct_scope} AND ({invalid_cc} OR {cascade}), {_lit(_INVALID_CODE)}, s.cpc_cpa) AS cpc_cpa"
    )
    return ",\n    ".join(parts)


def _salon_canon_cte() -> str:
    """Эталонные написания салонов: word-sort ключ → каноническое имя.

    `HAVING count() = 1` — защита от схлопывания: если два РАЗНЫХ салона дают
    один ключ (коллизия), канонизация для этого ключа выключается.
    """
    return f"""
salon_canon AS
(
    SELECT wkey, min(salon) AS canon_salon
    FROM
    (
        SELECT DISTINCT trim(salon) AS salon, {_word_sort_key('salon')} AS wkey
        FROM reference_data.gsheet_sites
        WHERE ifNull(salon, '') != '' AND trim(salon) != ''
    )
    GROUP BY wkey
    HAVING count() = 1
)"""


def _stage5_domain_salon() -> str:
    """S5: `_rule_fix_wrong_domains` → `normalize_salons`."""
    acc = "lowerUTF8(trim(ifNull(s.account_login, '')))"
    fixes = " ".join(
        f"{acc} = {_lit(account)} AND ifNull(s.domain, '') = {_lit(wrong)}, {_lit(correct)},"
        for account, wrong, correct in _DOMAIN_FIXES
    )
    domain = _scoped(SCOPE_DIRECT, f"multiIf({fixes} s.domain)", "s.domain")

    aliases = " ".join(
        f"ifNull(s.`салон`, '') = {_lit(wrong)}, {_lit(correct)},"
        for wrong, correct in SALON_ALIASES.items()
    )
    domain_salon = " ".join(
        f"ifNull(trim(s.`салон`), '') = '' AND lowerUTF8(trim(ifNull(s.domain, ''))) = {_lit(domain_key)}, {_lit(salon)},"
        for domain_key, salon in DOMAIN_SALON_MAP.items()
    )
    canon = (
        "ifNull(trim(s.`салон`), '') != '' AND ifNull(sc.canon_salon, '') != '' "
        "AND lowerUTF8(trim(s.`салон`)) != lowerUTF8(trim(sc.canon_salon)), sc.canon_salon,"
    )
    salon = f"multiIf({aliases} {domain_salon} {canon} s.`салон`)"
    return f"""
    {domain} AS domain,
    {salon} AS `салон`
"""


def _region_cte() -> str:
    return """
salon_city_region AS
(
    SELECT
        lowerUTF8(trim(ifNull(salon, ''))) AS salon_key,
        lowerUTF8(trim(ifNull(city, ''))) AS city_key,
        min(region) AS region_value
    FROM reference_data.gsheet_sites
    WHERE ifNull(region, '') != '' AND ifNull(salon, '') != ''
    GROUP BY salon_key, city_key
)"""


def _specialist_cte() -> str:
    """directologist/direction_main по домену — приоритет строке с непустым directologist."""
    # argMax по КОРТЕЖУ: v5 `DISTINCT ON (domain) ORDER BY directologist NULLS LAST`
    # берёт ОБА поля из ОДНОЙ строки. Два независимых argMax смешали бы строки.
    return """
gs_specialist AS
(
    SELECT domain_key, best.1 AS directologist, best.2 AS direction_main
    FROM
    (
        SELECT
            domain_key,
            argMax((directologist, direction_main), has_dir) AS best
        FROM
        (
            SELECT
                lowerUTF8(trim(ifNull(domain, ''))) AS domain_key,
                ifNull(directologist, '') AS directologist,
                ifNull(direction_main, '') AS direction_main,
                if(trim(ifNull(directologist, '')) != '', 1, 0) AS has_dir
            FROM reference_data.gsheet_sites
            WHERE ifNull(domain, '') != ''
        )
        GROUP BY domain_key
    )
)"""


def _backfill_cte() -> str:
    """Донорские значения для `_fix_account_domain_backfill` (v5: LIMIT 1 по домену)."""
    domains = _sql_list(domain for _, domain in _ACCOUNT_DOMAIN_BACKFILL)
    accounts = _sql_list(account for account, _ in _ACCOUNT_DOMAIN_BACKFILL)
    fields = ",\n        ".join(
        f"any(`{field}`) AS donor_{idx}" for idx, field in enumerate(_BACKFILL_FIELDS)
    )
    return f"""
backfill_donor AS
(
    SELECT
        lowerUTF8(trim(ifNull(domain, ''))) AS domain_key,
        {fields}
    FROM {SOURCE_TABLE}
    WHERE `_source_table` = 'direct'
      AND lowerUTF8(trim(ifNull(domain, ''))) IN ({domains})
      AND lowerUTF8(trim(ifNull(account_login, ''))) NOT IN ({accounts})
      AND ifNull(trim(`город`), '') != ''
    GROUP BY domain_key
)"""


def _stage6_labels() -> str:
    """S6: регион/город, менеджер, backfill аккаунта, посевные utm, специалист."""
    domain_key = "lowerUTF8(trim(ifNull(s.domain, '')))"
    acc = "lowerUTF8(trim(ifNull(s.account_login, '')))"

    backfill_pair = " OR ".join(
        f"({acc} = {_lit(account)} AND {domain_key} = {_lit(domain)})"
        for account, domain in _ACCOUNT_DOMAIN_BACKFILL
    )
    backfill_on = f"(({backfill_pair}) AND ifNull(trim(s.`город`), '') = '')"

    def _with_backfill(field: str, idx: int, expr: str) -> str:
        donor = f"bd.donor_{idx}"
        return f"if({backfill_on} AND ifNull(trim({donor}), '') != '', coalesce(nullIf(trim({expr}), ''), {donor}), {expr})"

    # ── fill_missing_regions: (салон, город) → регион + DOMAIN_SITE_MAP ────────
    region_by_salon = (
        "if(ifNull(trim(s.`регион`), '') = '' AND ifNull(scr.region_value, '') != '', scr.region_value, s.`регион`)"
    )
    map_city = " ".join(
        f"{domain_key} = {_lit(domain)}, {_lit(city)},"
        for domain, (_, city, _region) in DOMAIN_SITE_MAP.items()
    )
    map_region = " ".join(
        f"{domain_key} = {_lit(domain)}, {_lit(region)},"
        for domain, (_salon, _city, region) in DOMAIN_SITE_MAP.items()
    )
    city_expr = f"if(ifNull(trim(s.`город`), '') = '', multiIf({map_city} s.`город`), s.`город`)"
    region_expr = (
        f"if(ifNull(trim(REGION_SRC), '') = '', multiIf({map_region} REGION_SRC), REGION_SRC)"
    ).replace("REGION_SRC", region_by_salon)

    # ── _fix_crop_missing_utms: посевные домены без записи в gsheet-аккаунтах ──
    crop_salon = " ".join(
        f"{domain_key} = {_lit(domain)}, {_lit(salon)},"
        for domain, salon, _city in _CROP_UTM_BACKFILL
    )
    crop_city = " ".join(
        f"{domain_key} = {_lit(domain)}, {_lit(city)},"
        for domain, _salon, city in _CROP_UTM_BACKFILL
    )
    crop_on = "s.`_source_table` = 'crop_targeting' AND ifNull(trim(s.`салон`), '') = ''"
    salon_expr = f"if({crop_on}, multiIf({crop_salon} s.`салон`), s.`салон`)"
    city_expr = f"if({crop_on} AND ifNull(trim(s.`город`), '') = '', multiIf({crop_city} {city_expr}), {city_expr})"

    # ── _fix_missing_managers ─────────────────────────────────────────────────
    managers = " ".join(
        f"{domain_key} = {_lit(domain)}, {_lit(manager)},"
        for domain, manager in _MISSING_MANAGERS
    )
    manager_expr = (
        f"if(s.`_source_table` = 'direct' AND ifNull(trim(s.`менеджер`), '') = '', "
        f"multiIf({managers} s.`менеджер`), s.`менеджер`)"
    )

    perform_direction_expr = (
        "CAST(if("
        "ifNull(s.`салон`, '') = 'Перформ РФ' OR ifNull(s.`id_салона`, '') = 'avto_0415', "
        "'Перформ', "
        "s.`направление`"
        "), 'String')"
    )

    # ── специалист: правила по аккаунту (v5 rules 1..1д) + fallback v3 ────────
    account_rules = specialist_correction_expr("s.`Date`", "s.account_login", "s.`специалист`")
    fallback = (
        f"if(ifNull(trim({account_rules}), '') != '', {account_rules}, "
        "coalesce("
        "nullIf(trim(ifNull(gsp.directologist, '')), ''), "
        "nullIf(trim(ifNull(gsp.direction_main, '')), ''), "
        "if(ifNull(s.campaign_code, '') = 'звонки', 'Звонки', 'Без специалиста')))"
    )

    exprs = {
        "салон": salon_expr,
        "город": city_expr,
        "регион": region_expr,
        "менеджер": manager_expr,
        "специалист": fallback,
        "направление": perform_direction_expr,
    }
    parts = []
    for idx, field in enumerate(_BACKFILL_FIELDS):
        base = exprs.pop(field, f"s.`{field}`")
        parts.append(f"{_with_backfill(field, idx, base)} AS `{field}`")
    for field, expr in exprs.items():
        parts.append(f"{expr} AS `{field}`")
    return ",\n    ".join(parts)


def _latest_campaign_cte() -> str:
    """Правило 0a: последний валидный код кампании на CampaignId.

    `argMax` берётся по КОРТЕЖУ, а не по каждой колонке отдельно: v5 `DISTINCT ON`
    выбирает ОДНУ строку и тянет из неё все четыре поля, а четыре независимых
    argMax при равных датах могли бы смешать поля разных строк.
    """
    return f"""
latest_campaign AS
(
    SELECT
        `CampaignId`,
        best.1 AS campaign_code,
        best.2 AS tp,
        best.3 AS cpc_cpa,
        best.4 AS site_quiz
    FROM
    (
        SELECT
            `CampaignId`,
            argMax((campaign_code, tp, cpc_cpa, site_quiz), `Date`) AS best
        FROM {SOURCE_TABLE}
        WHERE `_source_table` = 'direct'
          AND `CampaignId` IS NOT NULL
          AND ifNull(campaign_code, '') != ''
          AND campaign_code != {_lit(_INVALID_CODE)}
        GROUP BY `CampaignId`
    )
)"""


def build_corrections_sql(target_table: str) -> str:
    """CREATE TABLE <target> — исходная таблица, пропущенная через все стадии."""
    return f"""
CREATE TABLE {target_table}
ENGINE = MergeTree
PARTITION BY toYYYYMM(ifNull(`Date`, toDate('2026-01-01')))
ORDER BY (ifNull(`Date`, toDate('2026-01-01')), ifNull(`CampaignId`, 0), ifNull(key3, ''))
AS
WITH
{_latest_campaign_cte().strip()},
{_salon_canon_cte().strip()},
{_region_cte().strip()},
{_specialist_cte().strip()},
{_backfill_cte().strip()}
SELECT s.* REPLACE (
    {_stage6_labels()}
)
FROM
(
    SELECT s.* REPLACE (
        {_stage5_domain_salon().strip()}
    )
    FROM
    (
        SELECT s.* REPLACE (
            {_stage4_ag_parts()}
        )
        FROM
        (
            SELECT s.* REPLACE (
                {_stage3_adgroup_maps().strip()}
            )
            FROM
            (
                SELECT s.* REPLACE (
                    {_stage2_reextract()}
                )
                FROM
                (
                    SELECT s.* REPLACE (
                        {_stage1_codes().strip()}
                    )
                    FROM {SOURCE_TABLE} s
                    LEFT JOIN latest_campaign lc ON lc.`CampaignId` = s.`CampaignId`
                ) s
            ) s
        ) s
        {_naming_joins()}
    ) s
    LEFT JOIN salon_canon sc ON sc.wkey = {_word_sort_key('s.`салон`')}
) s
LEFT JOIN salon_city_region scr
  ON scr.salon_key = lowerUTF8(trim(ifNull(s.`салон`, '')))
 AND scr.city_key = lowerUTF8(trim(ifNull(s.`город`, '')))
LEFT JOIN gs_specialist gsp ON gsp.domain_key = lowerUTF8(trim(ifNull(s.domain, '')))
LEFT JOIN backfill_donor bd ON bd.domain_key = lowerUTF8(trim(ifNull(s.domain, '')))
"""


# ══════════════════════════════════════════════════════════════════════════════
# Гейт неизменности денег и воронки
# ══════════════════════════════════════════════════════════════════════════════

_INVARIANT_SQL = """
SELECT
    count(),
    round(sum(total_cost), 2),
    sum(`Impressions`),
    sum(`Clicks`),
    sum(kol_vo_zayavok),
    sum(korr),
    sum(kval),
    sum(priezd),
    sum(prodazhi),
    sum(dohod_do_kredita),
    sum(dobro)
FROM {table}
"""

_INVARIANT_NAMES = (
    "rows", "total_cost", "impressions", "clicks",
    "kol_vo_zayavok", "korr", "kval", "priezd", "prodazhi", "dohod_do_kredita", "dobro",
)


def _schema(client, table: str) -> list[tuple[str, str]]:
    """Имена и типы колонок — пересборка обязана сохранить их до буквы.

    Выражение правила легко меняет тип (например `concat` с Nullable-именем из
    справочника превращает `String` в `Nullable(String)`), а `EXCHANGE TABLES`
    подменит таблицу молча — расхождение схемы всплыло бы уже в step6/звезде.
    """
    rows = client.query(f"DESCRIBE TABLE {table}", settings=CORRECTIONS_QUERY_SETTINGS).result_rows
    return [(row[0], row[1]) for row in rows]


def _invariants(client, table: str) -> dict:
    row = client.query(_INVARIANT_SQL.format(table=table), settings=CORRECTIONS_QUERY_SETTINGS).result_rows[0]
    return dict(zip(_INVARIANT_NAMES, (str(value) for value in row)))


_SUMMARY_SQL = """
SELECT
    countIf(campaign_code = 'неверный кодер') AS invalid_campaign_code,
    countIf(`неверный_кодер_new` = 'верный кодер') AS valid_adgroup_verdict,
    countIf(position(ifNull(ag_part1, ''), ' - ') > 0) AS named_ag_part1,
    countIf(ifNull(trim(`специалист`), '') = '') AS empty_specialist,
    countIf(ifNull(trim(`салон`), '') = '') AS empty_salon,
    countIf(ifNull(trim(`регион`), '') = '') AS empty_region,
    countIf(ifNull(trim(`менеджер`), '') = '') AS empty_manager
FROM {table}
"""

_SUMMARY_NAMES = (
    "invalid_campaign_code", "valid_adgroup_verdict", "named_ag_part1",
    "empty_specialist", "empty_salon", "empty_region", "empty_manager",
)


def _summary(client, table: str) -> dict:
    row = client.query(_SUMMARY_SQL.format(table=table), settings=CORRECTIONS_QUERY_SETTINGS).result_rows[0]
    return dict(zip(_SUMMARY_NAMES, (int(value) for value in row)))


def apply(conn=None, run_id: str | None = None) -> dict:  # noqa: A001, ARG001
    """Пересобрать `big_analytics_sources` со всеми правилами corrections."""
    logger.info("corrections v6_ch: пересборка %s правилами v5", SOURCE_TABLE)
    client = get_client()
    t0 = time.perf_counter()

    if not table_exists(client, "ad_analytics", "big_analytics_sources"):
        # Раньше здесь стоял тихий пропуск с итогом OK: после сборки звезды
        # `cleanup_wide_intermediates` дропает таблицу, и одиночный запуск
        # corrections рапортовал успех, не применив НИ ОДНОГО правила.
        raise RuntimeError(
            f"{SOURCE_TABLE} отсутствует — corrections применять не к чему. "
            "Таблицу строит step3; после сборки звезды её дропает "
            "star_refactor/cleanup_wide_intermediates.py, поэтому запускать "
            "corrections в одиночку имеет смысл только сразу после step3."
        )

    before_inv = _invariants(client, SOURCE_TABLE)
    before_sum = _summary(client, SOURCE_TABLE)
    before_schema = _schema(client, SOURCE_TABLE)

    client.command(f"DROP TABLE IF EXISTS {SHADOW_TABLE} SYNC")
    client.command(build_corrections_sql(SHADOW_TABLE), settings=CORRECTIONS_QUERY_SETTINGS)

    after_schema = _schema(client, SHADOW_TABLE)
    if before_schema != after_schema:
        drift = [pair for pair in zip(before_schema, after_schema) if pair[0] != pair[1]]
        client.command(f"DROP TABLE IF EXISTS {SHADOW_TABLE} SYNC")
        raise RuntimeError(f"corrections изменили схему big_analytics_sources: {drift[:10]}")

    after_inv = _invariants(client, SHADOW_TABLE)
    after_sum = _summary(client, SHADOW_TABLE)
    if before_inv != after_inv:
        drift = {key: (before_inv[key], after_inv[key]) for key in before_inv if before_inv[key] != after_inv[key]}
        client.command(f"DROP TABLE IF EXISTS {SHADOW_TABLE} SYNC")
        raise RuntimeError(
            "corrections изменили деньги/воронку — правила обязаны трогать только "
            f"метки: {drift}"
        )

    swap_shadow(client, SOURCE_TABLE, SHADOW_TABLE)

    changes = ", ".join(
        f"{key}:{before_sum[key]:,}->{after_sum[key]:,}"
        for key in _SUMMARY_NAMES
        if before_sum[key] != after_sum[key]
    ) or "нет изменений меток"
    rows = count_rows(client, SOURCE_TABLE)
    details = f"big_analytics_sources={rows:,}, {changes}"
    logger.info("corrections v6_ch завершены за %.1f сек: %s", time.perf_counter() - t0, details)
    return {"rows": rows, "details": details}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print(apply())
