"""Прод-LLM — локальная gemma4-31b-nvfp4 за LiteLLM (2026-10-12).

Ловушка, от которой защищает Config.load_from_env: OLLAMA_API_KEY всегда лежит в .env, и прежняя
безусловная подстановка подменяла бы им ключ локальной модели — LiteLLM отвечал бы 401 на каждый запрос.
"""
from config import Config

import pytest


def _env(monkeypatch, **overrides):
    monkeypatch.setenv('GEMMA4_31B_API_KEY', 'local-key')
    monkeypatch.setenv('GEMMA4_31B_NVFP4_API_KEY', 'nvfp4-key')
    monkeypatch.setenv('OLLAMA_API_KEY', 'cloud-key')
    for name in ('OLLAMA_URL', 'OLLAMA_MODEL'):
        monkeypatch.delenv(name, raising=False)
    for name, value in overrides.items():
        monkeypatch.setenv(name, value)


def test_default_is_local_gemma_with_its_own_key(monkeypatch):
    _env(monkeypatch)
    c = Config.load_from_env()
    assert (c.ollama_url, c.ollama_api_key, c.ollama_model) == (
        'http://172.17.0.1:4000/v1', 'nvfp4-key', 'gemma4-31b-nvfp4')
    assert c.max_concurrent_llm_requests == 7


@pytest.mark.parametrize('url', ['http://172.17.0.1:4000/v1', 'http://127.0.0.1:4000/v1'])
def test_explicit_local_url_keeps_nvfp4_key(monkeypatch, url):
    _env(monkeypatch, OLLAMA_URL=url)
    c = Config.load_from_env()
    assert c.ollama_url == url
    assert c.ollama_api_key == 'nvfp4-key'


def test_missing_nvfp4_key_does_not_use_another_models_key(monkeypatch):
    _env(monkeypatch)
    monkeypatch.delenv('GEMMA4_31B_NVFP4_API_KEY')
    assert Config.load_from_env().ollama_api_key == ''


def test_previous_local_fp8_model_uses_its_key_and_three_slots(monkeypatch):
    _env(monkeypatch, OLLAMA_MODEL='gemma4-31b')
    c = Config.load_from_env()
    assert c.ollama_api_key == 'local-key'
    assert c.max_concurrent_llm_requests == 3


def test_ollama_url_switches_back_to_cloud_with_cloud_key(monkeypatch):
    _env(monkeypatch, OLLAMA_URL='https://ollama.com/v1', OLLAMA_MODEL='gemma4:31b')
    c = Config.load_from_env()
    assert (c.ollama_url, c.ollama_api_key, c.ollama_model) == (
        'https://ollama.com/v1', 'cloud-key', 'gemma4:31b')
    assert c.max_concurrent_llm_requests == 3


def test_cloud_endpoint_does_not_receive_local_key(monkeypatch):
    _env(monkeypatch, OLLAMA_URL='https://ollama.com/v1')
    assert Config.load_from_env().ollama_api_key == 'cloud-key'
