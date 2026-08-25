"""
brand_map.py — маппинг ct-кодов групп объявлений → марки авто
Используется в шаге 3 для построения колонки "марки авто" в big_analytics_direct.
"""

AG_PART1_BRAND_MAP: dict = {
    'ct0019': 'BAIC',       'ct0020': 'BAIC',       'ct0021': 'BAIC',
    'ct0022': 'BAIC',       'ct0023': 'BAIC',       'ct0024': 'BAIC',
    'ct0025': 'BAIC',       'ct0026': 'Belgee',     'ct0027': 'Belgee',
    'ct0028': 'Belgee',     'ct0029': 'Changan',    'ct0030': 'Changan',
    'ct0031': 'Changan',    'ct0032': 'Changan',    'ct0033': 'Changan',
    'ct0034': 'Changan',    'ct0035': 'Changan',    'ct0036': 'Changan',
    'ct0037': 'Changan',    'ct0038': 'Changan',    'ct0039': 'Changan',
    'ct0040': 'Changan',    'ct0041': 'Changan',    'ct0042': 'Changan',
    'ct0043': 'Changan',    'ct0044': 'Chery',      'ct0045': 'Chery',
    'ct0046': 'Chery',      'ct0047': 'Chery',      'ct0048': 'Chery',
    'ct0049': 'Chery',      'ct0050': 'Chery',      'ct0051': 'Chery',
    'ct0052': 'Chery',      'ct0053': 'Chery',      'ct0054': 'Chery',
    'ct0055': 'Chery',      'ct0056': 'Chery',      'ct0057': 'Chery',
    'ct0058': 'Chery',      'ct0059': 'Chevrolet',  'ct0060': 'Chevrolet',
    'ct0061': 'Chevrolet',  'ct0062': 'Chevrolet',  'ct0063': 'Datsun',
    'ct0064': 'Datsun',     'ct0065': 'Datsun',     'ct0066': 'Dongfeng',
    'ct0067': 'Dongfeng',   'ct0068': 'Dongfeng',   'ct0069': 'Dongfeng',
    'ct0070': 'Dongfeng',   'ct0071': 'Dongfeng',   'ct0072': 'Dongfeng',
    'ct0073': 'Dongfeng',   'ct0074': 'Dongfeng',   'ct0075': 'Dongfeng',
    'ct0076': 'EXEED',      'ct0077': 'EXEED',      'ct0078': 'EXEED',
    'ct0079': 'EXEED',      'ct0080': 'EXEED',      'ct0081': 'Faw',
    'ct0082': 'Faw',        'ct0083': 'Faw',        'ct0084': 'Faw',
    'ct0085': 'Faw',        'ct0086': 'Faw',        'ct0087': 'Faw',
    'ct0088': 'Faw',        'ct0089': 'GAC',        'ct0090': 'GAC',
    'ct0091': 'GAC',        'ct0092': 'GAC',        'ct0093': 'GAC',
    'ct0094': 'GAC',        'ct0095': 'GAC',        'ct0096': 'GAC',
    'ct0097': 'Geely',      'ct0098': 'Geely',      'ct0099': 'Geely',
    'ct0100': 'Geely',      'ct0101': 'Geely',      'ct0102': 'Geely',
    'ct0103': 'Geely',      'ct0104': 'Geely',      'ct0105': 'Geely',
    'ct0106': 'Geely',      'ct0107': 'Geely',      'ct0108': 'Great',
    'ct0109': 'Great',      'ct0110': 'Great',      'ct0111': 'Haval',
    'ct0112': 'Haval',      'ct0113': 'Haval',      'ct0114': 'Haval',
    'ct0115': 'Haval',      'ct0116': 'Haval',      'ct0117': 'Haval',
    'ct0118': 'Haval',      'ct0119': 'Haval',      'ct0120': 'Haval',
    'ct0121': 'Hyundai',    'ct0122': 'Hyundai',    'ct0123': 'Hyundai',
    'ct0124': 'Hyundai',    'ct0125': 'Hyundai',    'ct0126': 'Hyundai',
    'ct0127': 'Hyundai',    'ct0128': 'Hyundai',    'ct0129': 'Hyundai',
    'ct0130': 'Jac',        'ct0131': 'Jac',        'ct0132': 'Jac',
    'ct0133': 'Jac',        'ct0134': 'Jac',        'ct0135': 'Jac',
    'ct0136': 'Jac',        'ct0137': 'Jac',        'ct0138': 'Jac',
    'ct0139': 'Jac',        'ct0140': 'Jaecoo',     'ct0141': 'Jaecoo',
    'ct0142': 'Jaecoo',     'ct0143': 'Jetour',     'ct0144': 'Jetour',
    'ct0145': 'Jetour',     'ct0146': 'Jetour',     'ct0147': 'Jetour',
    'ct0148': 'Jetour',     'ct0149': 'Jetour',     'ct0150': 'Jetta',
    'ct0151': 'Jetta',      'ct0152': 'Jetta',      'ct0153': 'Jetta',
    'ct0154': 'KAIYI',      'ct0155': 'KAIYI',      'ct0156': 'KAIYI',
    'ct0157': 'KAIYI',      'ct0158': 'KAIYI',      'ct0159': 'KGM',
    'ct0160': 'KGM',        'ct0161': 'KGM',        'ct0162': 'KGM',
    'ct0163': 'KGM',        'ct0164': 'KIA',        'ct0165': 'KIA',
    'ct0166': 'KIA',        'ct0167': 'KIA',        'ct0168': 'KIA',
    'ct0169': 'KIA',        'ct0170': 'KIA',        'ct0171': 'KIA',
    'ct0172': 'KIA',        'ct0173': 'KIA',        'ct0174': 'KIA',
    'ct0175': 'KIA',        'ct0176': 'KIA',        'ct0177': 'KIA',
    'ct0178': 'KIA',        'ct0179': 'KNEWSTAR',   'ct0180': 'KNEWSTAR',
    'ct0181': 'Lada',       'ct0182': 'Lada',       'ct0183': 'Lada',
    'ct0184': 'Lada',       'ct0185': 'Lada',       'ct0186': 'Lada',
    'ct0187': 'Lada',       'ct0188': 'Lada',       'ct0189': 'Lada',
    'ct0190': 'Lada',       'ct0191': 'LIVAN',      'ct0192': 'LIVAN',
    'ct0193': 'LIVAN',      'ct0194': 'LIVAN',      'ct0195': 'MG',
    'ct0196': 'MG',         'ct0197': 'MG',         'ct0198': 'MG',
    'ct0199': 'Nissan',     'ct0200': 'Nissan',     'ct0201': 'Nissan',
    'ct0202': 'Nissan',     'ct0203': 'Nissan',     'ct0204': 'Omoda',
    'ct0205': 'Omoda',      'ct0206': 'Omoda',      'ct0207': 'Omoda',
    'ct0208': 'Omoda',      'ct0209': 'Renault',    'ct0210': 'Renault',
    'ct0211': 'Renault',    'ct0212': 'Renault',    'ct0213': 'Renault',
    'ct0214': 'Renault',    'ct0215': 'Skoda',      'ct0216': 'Skoda',
    'ct0217': 'Skoda',      'ct0218': 'Skoda',      'ct0219': 'Skoda',
    'ct0220': 'Skoda',      'ct0221': 'Solaris',    'ct0222': 'Solaris',
    'ct0223': 'Solaris',    'ct0224': 'Solaris',    'ct0225': 'Solaris',
    'ct0226': 'SOUEAST',    'ct0227': 'SOUEAST',    'ct0228': 'SOUEAST',
    'ct0229': 'SWM',        'ct0230': 'SWM',        'ct0231': 'SWM',
    'ct0232': 'SWM',        'ct0233': 'Tank',       'ct0234': 'Tank',
    'ct0235': 'Tank',       'ct0236': 'Tank',       'ct0237': 'Tank',
    'ct0238': 'Volkswagen', 'ct0239': 'Volkswagen', 'ct0240': 'Volkswagen',
    'ct0241': 'Volkswagen', 'ct0242': 'Volkswagen', 'ct0243': 'Volkswagen',
    'ct0244': 'Volkswagen', 'ct0245': 'Volkswagen', 'ct0246': 'XCITE',
    'ct0247': 'XCITE',      'ct0248': 'XCITE',      'ct0249': 'Zotye',
    'ct0250': 'Zotye',      'ct0251': 'Zotye',      'ct0252': 'Moskvich',
    'ct0253': 'Moskvich',   'ct0254': 'Moskvich',   'ct0255': 'Moskvich',
    'ct0256': 'UAZ',        'ct0257': 'UAZ',        'ct0258': 'UAZ',
    'ct0259': 'Ford',       'ct0260': 'Ford',       'ct0261': 'Ford',
    'ct0262': 'Ford',       'ct0263': 'Kia',        'ct0264': 'Mitsubishi',
    'ct0265': 'Mitsubishi', 'ct0266': 'Mitsubishi', 'ct0267': 'Mitsubishi',
    'ct0268': 'Mitsubishi', 'ct0269': 'Nissan',     'ct0270': 'Skoda',
    'ct0271': 'Toyota',     'ct0272': 'Volkswagen', 'ct0273': 'KIA',
    'ct0274': 'Kia',        'ct0275': 'Lada',       'ct0276': 'Volvo',
    'ct0277': 'Suzuki',     'ct0278': 'Subaru',     'ct0279': 'SsangYong',
    'ct0280': 'SEAT',       'ct0281': 'Peugeot',    'ct0282': 'Opel',
    'ct0283': 'Mazda',      'ct0284': 'Lifan',      'ct0285': 'Honda',
    'ct0286': 'Daewoo',     'ct0287': 'Nissan',     'ct0288': 'Nissan',
    'ct0289': 'Haima',      'ct0290': 'Haima',      'ct0291': 'Haima',
    'ct0292': 'Geely',      'ct0293': 'GAC',        'ct0294': 'MG',
    'ct0295': 'MG',         'ct0296': 'MG',         'ct0297': 'MG',
    'ct0298': 'MG',         'ct0299': 'Haval',      'ct0300': 'Tenet',
    'ct0301': 'Tenet',      'ct0302': 'Tenet',      'ct0303': 'Tenet',
    'ct0304': 'Jaecoo',     'ct0305': 'BAIC',       'ct0306': 'Belgee',
    'ct0307': 'Jetour',     'ct0308': 'Citroen',    'ct0309': 'BMW',
    'ct0310': 'Fiat',       'ct0311': 'Mini',       'ct0312': 'Audi',
    'ct0313': 'Mercedes-Benz', 'ct0314': 'Lexus',
}


def build_brand_case_sql() -> str:
    """SQL CASE WHEN для поля 'марки авто'."""
    rows = '\n'.join(
        f"        WHEN '{code}' THEN '{brand}'"
        for code, brand in AG_PART1_BRAND_MAP.items()
    )
    return (
        "    CASE LOWER(SPLIT_PART(adgroup_code, '_', 1))\n"
        f"{rows}\n"
        "        ELSE ''\n"
        "    END"
    )


def build_brand_multiif_sql(adgroup_code_expr: str) -> str:
    """ClickHouse expression for field `марки авто` from the first ct-code."""
    code = f"lowerUTF8(splitByChar('_', ifNull({adgroup_code_expr}, ''))[1])"
    parts = []
    for raw_code, brand in AG_PART1_BRAND_MAP.items():
        parts.append(f"{code} = '{raw_code}', '{brand}'")
    return f"multiIf({', '.join(parts)}, '')"
