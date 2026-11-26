"""Регрессии фактических отказов партии 24."""
import pytest
from site_profiles.control_transport import browser_address
from site_profiles.markdown_validation import canonical

def test_browser_prefers_reachable_ipv4_over_sorted_ipv6():
    assert browser_address(['2a00:f940:2:2:1:1:0:306','31.31.197.91']) == '31.31.197.91'

def test_browser_ipv6_literal_has_brackets():
    assert browser_address(['2a00:f940:2:2:1:1:0:306']) == '[2a00:f940:2:2:1:1:0:306]'

@pytest.mark.parametrize('source,result', [
    ('½ дюйма','1/2 дюйма'), ('1½ дюйма','1 1/2 дюйма'),
    ('температурный подъем (∆t) 25 C˚','температурный подъем (∆t) 25 C'),
    ('батарейки 1,5v × 2шт','батарейки 1,5v х 2шт')])
def test_equivalent_source_not_false_loss(source,result):
    assert canonical(source)==canonical(result)

@pytest.mark.parametrize('source,result', [
    ('½ дюйма','1/3 дюйма'), ('1½ дюйма','11/2 дюйма'),
    ('температурный подъем (∆t) 25 C˚','температурный подъем (∆t) 25'),
    ('батарейки 1,5v × 2шт','батарейки 1,5v х 3шт')])
def test_changed_value_or_unit_still_differs(source,result):
    assert canonical(source)!=canonical(result)
