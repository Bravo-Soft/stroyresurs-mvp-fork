"""Контракт внедрения v37: один запрос, зафиксированный промпт и совместимость старых моделей."""
import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from ai_integration import AITunnelClient
from gemma_nvfp4_single_prompt import SYSTEM, USER_TEMPLATE


def test_prompt_is_frozen_v37():
    source = Path(__file__).resolve().parents[1] / 'gemma_nvfp4_single_prompt.py'
    assert hashlib.sha256(source.read_bytes()).hexdigest() == (
        '9fad9b841bcec547cc40c0bee9b296790ca15d2a511e4d477115eb8b2a678862')


@pytest.mark.parametrize('reply', [
    {'error': 'Trash_418#'},
    {'product': {'product_name': 'Панель (Завод, ООО)', 'specifications': {
        'nominal_voltage': '220 В', 'Цена': '1200 руб.', 'Длина': '120 мм'}}},
    None,
])
def test_nvfp4_dispatches_one_call_with_v37_and_keeps_result_contract(reply):
    requests = []
    text = '# Панель\n' + 'Данные {с фигурными скобками}\n' * 5000
    page_url = 'https://example.com/catalog/panel/'

    async def scenario():
        client = object.__new__(AITunnelClient)
        client.model = 'gemma4-31b-nvfp4'

        async def request(data):
            requests.append(data)
            result = {'choices': [{'message': {'content': json.dumps(reply)}}]} if reply else None
            return result, {'input_tokens': 15, 'output_tokens': 9}

        async def forbidden(*args, **kwargs):
            pytest.fail('NVFP4 не должна попадать в двухвызовную SGR')

        client._make_request_with_retry = request
        client._sgr_reasoning = client._sgr_formatter = forbidden
        return await client.extract_product_info(
            text, 'Завод, ООО', 'https://example.com', page_url=page_url)

    result, usage = asyncio.run(scenario())
    assert usage == {'input_tokens': 15, 'output_tokens': 9}
    assert requests == [{
        'model': 'gemma4-31b-nvfp4',
        'messages': [
            {'role': 'system', 'content': SYSTEM},
            {'role': 'user', 'content': USER_TEMPLATE.format(
                text=text[:100000], manufacturer='Завод, ООО',
                base_domain='https://example.com', page_url=page_url)},
        ],
        'temperature': 0.0,
        'reasoning_effort': 'none',
        'response_format': {'type': 'json_object'},
        'max_tokens': 8192,
    }]
    if reply is None:
        assert result is None
    elif 'error' in reply:
        assert result == reply
    else:
        assert result['product']['specifications'] == {
            'Номинальное напряжение': '220 В', 'Длина': '120 мм'}


@pytest.mark.parametrize('model', ['gemma4-31b', 'gemma4:31b', 'glm-5.2'])
def test_existing_single_models_keep_their_prompts_and_payload(model):
    requests = []

    async def scenario():
        client = object.__new__(AITunnelClient)
        client.model = model

        async def request(data):
            requests.append(data)
            return {'choices': [{'message': {'content': '{"error":"Trash_418#"}'}}]}, {}

        client._make_request_with_retry = request
        return await client.extract_product_info('Текст', 'Завод', 'https://example.com')

    assert asyncio.run(scenario())[0] == {'error': 'Trash_418#'}
    assert len(requests) == 1
    assert requests[0]['messages'][0]['content'] != SYSTEM
    assert 'max_tokens' not in requests[0]


def test_non_single_model_keeps_reasoning_gate():
    calls = []

    async def scenario():
        client = object.__new__(AITunnelClient)
        client.model = 'deepseek-v4-flash:0731'

        async def reasoning(*args):
            calls.append('reasoning')
            return {'step_1_page_classification': {'is_product_page': False}}, {}, True

        async def single(*args, **kwargs):
            pytest.fail('Deepseek должна сохранить путь SGR')

        client._sgr_reasoning = reasoning
        client._sgr_single = single
        return await client.extract_product_info('Текст', 'Завод', 'https://example.com')

    assert asyncio.run(scenario())[0] == {'error': 'Trash_418#'}
    assert calls == ['reasoning']
