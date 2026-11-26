from data_island import has_unloaded_product_tabs


def test_legacy_merged_label_does_not_confirm_two_different_targets():
    html = '<a data-toggle="tab" href="#a">Характеристики</a><a data-toggle="tab" href="#b">Характеристики</a><div id="a"></div><div id="b"></div><section data-tab="Характеристики"><h2>Характеристики</h2><p>Давление16бар</p></section>'
    assert has_unloaded_product_tabs(html)


def test_one_legacy_merged_target_remains_completed():
    html = '<a data-toggle="tab" href="#a">Характеристики</a><div id="a"></div><section data-tab="Характеристики"><h2>Характеристики</h2><p>Давление16бар</p></section>'
    assert not has_unloaded_product_tabs(html)
