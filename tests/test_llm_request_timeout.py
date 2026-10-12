"""Параметры HTTP-запроса не должны сокращать таймаут генерации LiteLLM."""
import asyncio
import json

import pytest

import ai_integration
from ai_integration import AITunnelClient


@pytest.mark.parametrize('operation, reply', [
    ('reasoning', {'step_1_page_classification': {
        'is_product_page': False, 'tech_params_count': 0, 'has_enough_params': False}}),
    ('formatter', {'product': {'product_name': 'Тестовый товар'}}),
    ('single', {'error': 'Trash_418#'}),
    ('distributor', {'suppliers': []}),
    ('company', {'company': {'Наименование компании': 'Тест'}}),
])
def test_extraction_uses_gateway_timeout_without_payload_override(monkeypatch, operation, reply):
    requests = []
    client_timeouts = []

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def json(self):
            return {
                'choices': [{'message': {'content': json.dumps(reply, ensure_ascii=False)}}],
                'usage': {'prompt_tokens': 3, 'completion_tokens': 2},
            }

    class Session:
        def __init__(self, *, timeout):
            client_timeouts.append(timeout.total)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def post(self, url, *, headers, json):
            requests.append(json)
            return Response()

    monkeypatch.setattr(ai_integration.aiohttp, 'ClientSession', Session)

    async def scenario():
        # Без конструктора: тесту не нужны сеть, ключи и загрузка токенайзера.
        client = object.__new__(AITunnelClient)
        client.api_url = 'http://local-test.invalid/v1/chat/completions'
        client.headers = {'Authorization': 'Bearer test-key'}
        client.model = 'gemma4-31b-nvfp4'
        client.semaphore = asyncio.Semaphore(7)
        client.max_retries = 0
        client.rate_limiter = None
        client.processing_tracker = None
        client.performance_monitor = None
        client.encoding = None

        if operation == 'reasoning':
            return await client._sgr_reasoning('Текст', 'Тест', 'https://example.com')
        if operation == 'formatter':
            return await client._sgr_formatter({}, 'https://example.com', 'Тест')
        if operation == 'single':
            return await client._sgr_single('Текст', 'Тест', 'https://example.com')
        if operation == 'distributor':
            return await client.extract_distributor_info('Текст', '1', 'Тест')
        return await client.extract_company_info('Текст', '1')

    result = asyncio.run(scenario())
    assert result[0] is not None
    assert result[1] == {'input_tokens': 3, 'output_tokens': 2}
    assert len(requests) == 1
    assert 'timeout' not in requests[0]
    assert requests[0]['model'] == 'gemma4-31b-nvfp4'
    assert client_timeouts == [900]
