import asyncio
import pytest
from ai_integration import AITunnelClient

@pytest.mark.parametrize('text,expected', [
    ('URL страницы: https://ekoprom.com.ru/contacts\nООО фирма Экопром\n', 'Фирма Экопром, ООО'),
    ('URL страницы: https://other.ru/contacts\nООО фирма Экопром\n', 'Экопром, ООО'),
    ('URL страницы: https://ekoprom.com.ru/contacts\nКонтакты\n', 'Экопром, ООО'),
    ('URL страницы: https://ekoprom.com.ru/contacts\nПоставщик ООО фирма Экопром\n', 'Экопром, ООО'),
])
def test_own_name_requires_exact_visible_name_and_company_page(text, expected):
    client = AITunnelClient.__new__(AITunnelClient)
    client.model = 'unit-test'
    async def request(data):
        return {'choices': [{'message': {'content': '{"company":{"Наименование компании":"Экопром, ООО"}}'}}]}, {}
    client._make_request_with_retry = request
    data, _ = asyncio.run(client.extract_company_info(text, 'control'))
    assert data['company']['Наименование компании'] == expected
