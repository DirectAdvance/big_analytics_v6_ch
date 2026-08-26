from star_refactor.build_pbi_compat import _vk_ads_pbi_sql
from star_refactor.build_star import _vk_ads_sql


def test_vk_ads_fact_keeps_domain_grain_for_leads_and_visits():
    sql = _vk_ads_sql("*", "", "", "", "")

    assert "domain," in sql
    assert "AS site_key" in sql
    assert "GROUP BY date, domain, site_key, ad_group_id, banner_id" in sql
    assert "INNER JOIN banner_dim bd ON bd.banner_id = za.banner_id" in sql
    assert "INNER JOIN banner_dim bd ON bd.banner_id = va.banner_id" in sql
    assert "LEFT JOIN site_dim sd ON sd.site_key = za.site_key" in sql
    assert "LEFT JOIN site_dim sd ON sd.site_key = va.site_key" in sql


def test_vk_ads_pbi_view_exports_site_key_and_domain():
    sql = _vk_ads_pbi_sql()

    assert "{" not in sql
    assert 'AS site_key' in sql
    assert 'f.domain AS domain' in sql
