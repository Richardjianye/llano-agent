"""Title wire-format compatibility uses the resolved auxiliary model."""
from copy import deepcopy

import pytest

from agent.auxiliary_client import _build_call_kwargs
from agent.title_generator import _TITLE_RESPONSE_FORMAT, _extract_title_text


@pytest.mark.parametrize('provider,model', [
    ('custom', 'deepseek-v4.1-flash'),
    ('deepseek', 'deepseek-chat'),
    ('openrouter', 'deepseek/deepseek-v3'),
])
def test_deepseek_title_uses_json_mode_without_mutating_shared_schema(provider, model):
    source = {'response_format': deepcopy(_TITLE_RESPONSE_FORMAT)}
    before = deepcopy(source)
    kwargs = _build_call_kwargs(provider, model,
        [{'role': 'user', 'content': 'Name this conversation as JSON'}],
        task='title_generation', extra_body=source)
    assert kwargs['extra_body']['response_format'] == {'type': 'json_object'}
    assert source == before
    assert _extract_title_text('{"title":"库存销售分析"}') == '库存销售分析'


@pytest.mark.parametrize('task,model', [
    ('title_generation', 'gpt-5.6-luna'),
    ('compression', 'deepseek-v4.1-flash'),
    ('skill_metadata', 'deepseek-v4.1-flash'),
])
def test_other_resolved_models_and_tasks_keep_strict_schema(task, model):
    kwargs = _build_call_kwargs('custom', model,
        [{'role': 'user', 'content': 'Return JSON'}],
        task=task, extra_body={'response_format': _TITLE_RESPONSE_FORMAT})
    assert kwargs['extra_body']['response_format'] == _TITLE_RESPONSE_FORMAT


@pytest.mark.parametrize('response_format', [{'type':'text'}, {'type':'json_object'}])
def test_title_keeps_explicit_non_schema_response_format(response_format):
    kwargs = _build_call_kwargs('custom', 'deepseek-v4.1-flash', [],
        task='title_generation', extra_body={'response_format': response_format})
    assert kwargs['extra_body']['response_format'] == response_format
