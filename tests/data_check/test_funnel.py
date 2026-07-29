import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from data_check.checks.funnel import check_invariants


def test_valid_funnel_passes():
    row = {'проджект': 'Test', 'korr': 10, 'kval': 8,
           'priezd': 5, 'prodazhi': 3, 'dohod_do_kredita': 4, 'dobro': 2}
    issues = check_invariants(row)
    assert issues == []


def test_kval_gt_korr_flagged():
    row = {'проджект': 'Test', 'korr': 5, 'kval': 8,
           'priezd': 3, 'prodazhi': 2, 'dohod_do_kredita': 2, 'dobro': 1}
    issues = check_invariants(row)
    assert any('kval' in i and 'korr' in i for i in issues)


def test_prodazhi_without_priezd_flagged():
    row = {'проджект': 'Test', 'korr': 10, 'kval': 8,
           'priezd': 0, 'prodazhi': 3, 'dohod_do_kredita': 2, 'dobro': 1}
    issues = check_invariants(row)
    assert any('prodazhi' in i and 'priezd' in i for i in issues)


def test_dobro_gt_dohod_flagged():
    row = {'проджект': 'Test', 'korr': 10, 'kval': 8,
           'priezd': 5, 'prodazhi': 3, 'dohod_do_kredita': 2, 'dobro': 5}
    issues = check_invariants(row)
    assert any('dobro' in i and 'dohod' in i for i in issues)
