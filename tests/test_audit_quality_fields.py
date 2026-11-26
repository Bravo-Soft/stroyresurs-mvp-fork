from copy import deepcopy
from product_quality import normalize_product_fields
from main import count_numeric_characteristics


def test_move_lifetime_and_storage_without_changing_values():
    data={'product':{'specifications':{'Срок службы':'30 лет','Срок хранения':'24 месяца','Напряжение':'10 кВ'},'colors':[]}}
    normalize_product_fields(data)
    p=data['product']
    assert p['specifications']=={'Напряжение':'10 кВ'}
    assert '30 лет' in p['exploitation'] and '24 месяца' in p['storage']
    once=deepcopy(data);normalize_product_fields(data);assert data==once


def test_duplicate_color_only_when_same_value_and_unqualified():
    data={'product':{'specifications':{'Цвет':'черный','Цвет крышки':'красный'},'colors':['черный']}}
    normalize_product_fields(data)
    assert data['product']['specifications']=={'Цвет крышки':'красный'}


def test_different_temperature_signs_never_deduplicated():
    data={'product':{'specifications':{'Температура окружающей среды':'+5 C'},'exploitation':'Температура окружающей среды: -5 C'}}
    normalize_product_fields(data)
    assert '+5 C' in data['product']['exploitation'] and '-5 C' in data['product']['exploitation']


def test_unknown_additional_info_stays_hidden_and_explicit_environment_moves():
    data={'product':{'additional_info':{'text':'Температура окружающей среды, C: от -30 до +40\nПатент № 92933'},'specifications':{}}}
    normalize_product_fields(data)
    assert data['product']['additional_info']['text']=='Патент № 92933'
    assert '-30' in data['product']['exploitation']


def test_storage_in_exploitation_moves_without_temperature_invention():
    data={'product':{'specifications':{},'exploitation':'Срок хранения: 12 месяцев при +5 C\nТемпература эксплуатации: -5 C'}}
    normalize_product_fields(data)
    assert data['product']['storage']=='- Срок хранения: 12 месяцев при +5 C'
    assert data['product']['exploitation']=='Температура эксплуатации: -5 C'


def test_material_codes_do_not_supply_missing_physical_characteristics():
    assert count_numeric_characteristics({'specifications':{'Диаметр':'16 мм','Металл':'65г/70','Покрытие':'01/02/11'}})==1
    assert count_numeric_characteristics({'specifications':{'Диаметр волокна':'13 мкм','Линейная плотность':'1260 текс','Замасливатель':'78'}})==2
    assert count_numeric_characteristics({'specifications':{'Размеры, мм':'360x170x150','Материал':'Сталь 30Х13'}})==3


def test_real_coating_thickness_and_material_fraction_stay_physical():
    assert count_numeric_characteristics({'specifications':{'Толщина покрытия':'20 мкм','Содержание стекла':'40 %','Плотность':'1500 кг/м^3'}})==3


def test_physical_parameter_with_normative_method_is_still_physical():
    product={'specifications':{'Ажур-15П-76':{
      'Ширина (по ГОСТ 6943.17-94), см':'100±2',
      'Поверхностная плотность, г/м^2':'15±1,5',
      'Разрывная нагрузка, Н':'50',
      'ГОСТ':'ГОСТ 6943.17-94'}}}
    assert count_numeric_characteristics(product)==3


def test_money_columns_do_not_count_as_physical_properties():
    assert count_numeric_characteristics({'specifications':{'До 10 шт, руб.':'1200','От 10 до 25 шт':'1000 ₽','Толщина':'0.8 мм'}})==1
    assert count_numeric_characteristics({'specifications':{'Номинальный ток рубильника':'100 А','Мощность':'10 кВт','Длина':'300 мм'}})==3


def test_thread_component_codes_are_not_physical_values():
    assert count_numeric_characteristics({'specifications':{'Нити в основе':'БС-6-6,8х1х2(130)-76','Нити в утке':'БС7-9,2х1(50)-76','Ширина':'100 см'}})==1
    assert count_numeric_characteristics({'specifications':{'Количество нитей в основе':'40 шт','Нити в утке':'30 шт'}})==2


def test_identity_labels_are_not_material_strength_classes():
    assert count_numeric_characteristics({'specifications':{'Марка изделия':'БД-4ИМ','Марка полотна':'15П-76','Марка нити':'БС7-9,2','Класс бетона':'B25','Марка прочности':'М200'}})==2


def test_document_reference_and_notes_are_not_physical_properties():
    assert count_numeric_characteristics({'specifications':{'НТД':'ТУ 6-48-91-92','НД':'ГОСТ 8-2010','Примечание':'МС-ВП-А9/А-1100','Группа горючести':'Г1','Объём':'3 м^3'}})==2


def test_modulus_is_not_mistaken_for_gost_substring():
    assert count_numeric_characteristics({'specifications':{'Модуль упругости':'95 ГПа','ГОСТ':'ГОСТ 6943.17-94','Плотность':'2580 кг/м^3'}})==2


def test_qualitative_sections_and_anonymous_cells_are_not_numeric_properties():
    assert count_numeric_characteristics({'specifications': {
        'Конструкция': '2 камеры, версия 2024', 'Устройство': '3 блока',
        'Принцип работы': 'Этап 1 и этап 2', 'Строка 1': '100', 'Столбец 2': '500',
        'Прочность конструкции': '200 МПа', 'Количество строк матрицы': '16'}}) == 2
