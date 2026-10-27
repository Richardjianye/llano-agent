import json
import threading
import time

import pytest

from tools import local_skill_metadata as local
from tools import skill_chinese_metadata as chinese


def wait_until(predicate):
    deadline = time.monotonic() + 5
    while not predicate():
        if time.monotonic() > deadline:
            pytest.fail('metadata worker did not finish')
        time.sleep(.01)


@pytest.fixture
def skill(tmp_path, monkeypatch, _isolate_hermes_home):
    from hermes_constants import get_hermes_home
    home = get_hermes_home()
    directory = home / 'skills' / 'learned-report'
    directory.mkdir(parents=True)
    text = '---\nname: learned-report\ndescription: Produce reports\n---\n\nRead the table and total sales.\n'
    (directory / 'SKILL.md').write_text(text)
    row = {'name': 'learned-report', 'description': 'Produce reports', 'provenance': 'agent',
           'skill_dir': str(directory), 'usage': 32}
    import hermes_cli.product_policy as policy
    monkeypatch.setattr(policy, 'load_product_policy', lambda: object())
    return home, directory, text, row


def proposal(rows):
    return {r['id']: {'name': '销售报表汇总', 'description': '读取销售表格并汇总销售金额'} for r in rows}


def test_automatic_completion_is_nonblocking_deduplicated_persistent_and_read_only(skill, monkeypatch):
    home, directory, text, row = skill
    entered, release = threading.Event(), threading.Event()
    calls = []
    def translate(rows):
        from hermes_constants import get_hermes_home
        calls.append((rows, get_hermes_home()))
        entered.set()
        assert release.wait(5)
        return proposal(rows)
    monkeypatch.setattr(chinese, '_translate', translate)
    try:
        assert local.schedule_metadata([row]) == {row['name']}
        assert entered.wait(5)
        for _ in range(4):
            assert local.schedule_metadata([row]) == {row['name']}
        assert len(calls) == 1
        release.set()
        wait_until(lambda: bool(local.display_metadata(row)))
        localized = chinese.localize_rows([row], title_field='display_name')[0]
        assert localized['display_name'] == '销售报表汇总'
        assert not localized['metadata_incomplete'] and localized['usage'] == 32
        assert calls[0][1] == home
        assert (directory / 'SKILL.md').read_text() == text
        assert local.schedule_metadata([row]) == set()
        # A fresh cache read, not an in-memory translation, supplies the result.
        assert json.loads(local._cache_path(home).read_text())
        assert local.display_metadata(row)['name'] == '销售报表汇总'
    finally:
        release.set()
        wait_until(lambda: not any(k[0] == str(home.resolve()) for k in local._pending))


def test_changed_content_and_other_profile_never_reuse_a_translation(skill, monkeypatch):
    home, directory, text, row = skill
    monkeypatch.setattr(chinese, '_translate', proposal)
    local.schedule_metadata([row])
    wait_until(lambda: bool(local.display_metadata(row)))
    (directory / 'SKILL.md').write_text(text + '\nNew operation.\n')
    assert local.display_metadata(row) == {}
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    token = set_hermes_home_override(str(home / 'profiles' / 'other'))
    try:
        assert local.display_metadata(row) == {}
        assert local.schedule_metadata([row]) == set()
    finally:
        reset_hermes_home_override(token)


def test_bundle_company_and_external_files_are_not_translated(skill, monkeypatch, tmp_path):
    _, _, _, row = skill
    def forbidden(rows):
        pytest.fail('authoritative/external skills must never be translated locally')
    monkeypatch.setattr(chinese, '_translate', forbidden)
    assert local.schedule_metadata([{**row, 'provenance': 'bundled'}, {**row, 'provenance': 'hub'},
                                    {**row, 'skill_dir': str(tmp_path / 'outside')}]) == set()


def test_failed_generation_cools_down_and_explicit_retry_recovers(skill, monkeypatch):
    home, _, _, row = skill
    calls = []
    def fail(rows):
        calls.append(rows)
        raise ValueError('invalid AI response')
    monkeypatch.setattr(chinese, '_translate', fail)
    local.schedule_metadata([row])
    wait_until(lambda: any(k[0] == str(home.resolve()) for k in local._failed_until))
    for _ in range(3):
        assert local.schedule_metadata([row]) == set()
    assert len(calls) == 1 and local.display_metadata(row) == {}
    monkeypatch.setattr(chinese, '_translate', proposal)
    local.schedule_metadata([row], retry=True)
    wait_until(lambda: bool(local.display_metadata(row)))


def test_concurrent_edit_while_model_runs_discards_old_proposal(skill, monkeypatch):
    home, directory, text, row = skill
    def translate(rows):
        (directory / 'SKILL.md').write_text(text + '\nChanged concurrently.\n')
        return proposal(rows)
    monkeypatch.setattr(chinese, '_translate', translate)
    local.schedule_metadata([row])
    wait_until(lambda: not any(k[0] == str(home.resolve()) for k in local._pending))
    assert local.display_metadata(row) == {}
    assert json.loads(local._cache_path(home).read_text()) == {}


def test_worker_exercises_real_auxiliary_client_and_http_protocol(skill, monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from agent import auxiliary_client as aux
    _, directory, original, row = skill
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append(data)
            inputs = json.loads(data['messages'][-1]['content'])
            content = json.dumps([{'id': r['id'], 'name': '销售报表汇总',
                                   'description': '读取销售表格并汇总销售金额'} for r in inputs], ensure_ascii=False)
            response = json.dumps({'id': 'fixture', 'object': 'chat.completion', 'created': 0,
                'model': 'metadata-fixture', 'choices': [{'index': 0, 'finish_reason': 'stop',
                'message': {'role': 'assistant', 'content': content}}],
                'usage': {'prompt_tokens': 10, 'completion_tokens': 10, 'total_tokens': 20}}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(response)))
            self.end_headers()
            self.wfile.write(response)
        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    real_call = aux.call_llm
    monkeypatch.setattr(aux, 'call_llm', lambda **kwargs: real_call(**kwargs, provider='custom',
        model='metadata-fixture', base_url=f'http://127.0.0.1:{server.server_port}/v1', api_key='local-fixture'))
    try:
        local.schedule_metadata([row])
        wait_until(lambda: bool(local.display_metadata(row)))
        assert len(calls) == 1
        assert not calls[0].get('tools')
        assert json.loads(calls[0]['messages'][-1]['content'])[0]['content'] == original
        assert (directory / 'SKILL.md').read_text() == original
    finally:
        server.shutdown()
        server.server_close()


def test_externally_installed_skills_can_be_completed_but_company_ones_cannot():
    from tools.local_skill_metadata import _completable

    assert _completable({"provenance": "agent"}) and _completable({"provenance": "imported"})
    assert _completable({"provenance": "hub", "hub_source": "skillhub"})
    assert not _completable({"provenance": "hub", "hub_source": "llano-hub"}), "公司技能中文由服务端负责"
    assert not _completable({"provenance": "hub"}), "来源不明不擅自补"
    assert not _completable({"provenance": "bundled"}), "随包技能中文随安装包"
