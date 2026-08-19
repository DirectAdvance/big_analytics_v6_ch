from decimal import Decimal

from direct_placement_links.build import (
    CubePlacement,
    RawPlacement,
    collapse_placement_links,
    filter_unknown_placements,
    match_raw_placements,
    normalize_placement_link,
    normalized_tokens,
)


def raw(placement: str, spend: str, campaign_id: int = 712408803) -> RawPlacement:
    return RawPlacement(
        client_login="porg-hd3y6pbr",
        manager_key="victoryagency14",
        campaign_id=campaign_id,
        campaign_name="tp9 test",
        placement=placement,
        spend=Decimal(spend),
        period_from="2026-07-08",
        period_to="2026-08-05",
    )


def cube(page_group: str, link: str, spend: str, campaign_id: int = 712408803) -> CubePlacement:
    return CubePlacement(
        client_login="porg-hd3y6pbr",
        campaign_id=campaign_id,
        page_group=page_group,
        link=link,
        spend=Decimal(spend),
    )


def test_normalized_tokens_ignore_word_order_and_punctuation():
    assert normalized_tokens("Бензин | Регион 52 | Нижний Новгород") == normalized_tokens(
        "Регион 52 | Нижний Новгород | Бензин"
    )


def test_normalize_placement_link_keeps_only_real_urls():
    assert normalize_placement_link("5play.org") == "https://5play.org"
    assert normalize_placement_link("http://telegram.me/test_channel/") == "https://t.me/test_channel"
    assert normalize_placement_link("@test_channel") == "https://t.me/test_channel"
    assert normalize_placement_link("24/7 БАЛАШИХА | НОВОСТИ") is None


def test_exact_placement_and_campaign_match_uses_home_page_link():
    matches = match_raw_placements(
        [raw("Нижний Новгород в сети", "2654.47", campaign_id=713014099)],
        [
            cube(
                "Нижний Новгород в сети",
                "https://max.ru/join/kIxjZpJ188Nkm_lXk_kkRMzmjOQtQqQHYSMLMVU2ECA",
                "2654.47",
                campaign_id=713014099,
            )
        ],
    )

    assert matches[0].placement_link == "https://max.ru/join/kIxjZpJ188Nkm_lXk_kkRMzmjOQtQqQHYSMLMVU2ECA"
    assert matches[0].match_status == "exact_name"


def test_spend_fallback_matches_same_words_in_different_order():
    matches = match_raw_placements(
        [raw("Бензин | Регион 52 | Нижний Новгород", "1689.21")],
        [
            cube(
                "Регион 52 | Нижний Новгород | Бензин",
                "https://max.ru/join/UO05bVNGLOKp48dzq2bqHUblkzjNK2FTKlr_-yQU5Es",
                "1689.21",
            )
        ],
    )

    assert matches[0].placement_link == "https://max.ru/join/UO05bVNGLOKp48dzq2bqHUblkzjNK2FTKlr_-yQU5Es"
    assert matches[0].match_status == "same_campaign_spend"


def test_spend_fallback_prefers_text_similarity_when_spend_is_not_unique():
    matches = match_raw_placements(
        [raw("Балахна.Ру - группа балахнинского портала. 21 год с вами!", "241.32")],
        [
            cube("Автосервис Балахна", "https://max.ru/other", "241.32"),
            cube(
                "Балахна.Ру - группа балахнинского портала. Местные новости",
                "https://max.ru/balakhnaru",
                "241.32",
            ),
        ],
    )

    assert matches[0].placement_link == "https://max.ru/balakhnaru"
    assert matches[0].match_status == "same_campaign_spend_text"


def test_collapse_placement_links_prefers_largest_raw_spend_for_duplicate_name():
    matches = match_raw_placements(
        [
            raw("Нижний Новгород в сети", "262.41", campaign_id=712408803),
            raw("Нижний Новгород в сети", "2654.47", campaign_id=713014099),
        ],
        [
            cube(
                "Нижний Новгород в сети",
                "https://max.ru/join/goQUmBXZYO96AlZihpKy4QsmnOnu7hk9kRTXMT7cxMk",
                "262.41",
                campaign_id=712408803,
            ),
            cube(
                "Нижний Новгород в сети",
                "https://max.ru/join/kIxjZpJ188Nkm_lXk_kkRMzmjOQtQqQHYSMLMVU2ECA",
                "2654.47",
                campaign_id=713014099,
            ),
        ],
    )

    links = collapse_placement_links(matches)

    assert links == [("Нижний Новгород в сети", "https://max.ru/join/kIxjZpJ188Nkm_lXk_kkRMzmjOQtQqQHYSMLMVU2ECA")]


def test_filter_unknown_placements_skips_already_linked_names():
    rows = [
        raw("Нижний Новгород в сети", "2654.47"),
        raw("Балахна.Ру - группа балахнинского портала. 21 год с вами!", "241.32"),
    ]

    unknown = filter_unknown_placements(
        rows,
        {"Нижний Новгород в сети": "https://max.ru/join/kIxjZpJ188Nkm_lXk_kkRMzmjOQtQqQHYSMLMVU2ECA"},
    )

    assert [row.placement for row in unknown] == ["Балахна.Ру - группа балахнинского портала. 21 год с вами!"]
