"""Catalog discovery never invokes a private adapter; installed catalog rows stay visible."""
from starlette.testclient import TestClient


def test_private_source_is_excluded_but_installed_company_result_remains(tmp_path, monkeypatch, _isolate_hermes_home):
    from tools import skills_hub
    from tools.skills_hub import SkillMeta
    import hermes_cli.web_server as web
    from hermes_constants import get_hermes_home
    home = get_hermes_home()
    (home / 'config.yaml').write_text('{}\n')
    calls = []

    class Source:
        def __init__(self, sid):
            self.sid = sid
        def source_id(self):
            return self.sid
        def search(self, query, limit=20):
            calls.append(self.sid)
            assert self.sid != 'local', 'private source must not be invoked'
            return [SkillMeta(name='公司报表', description='整理公司业务报表', source=self.sid,
                              identifier='llano-hub/company-report', trust_level='trusted')]

    monkeypatch.setattr(skills_hub, 'create_source_router', lambda: [Source('local'), Source('llano-hub')])
    installed = {'llano-hub/company-report': {'name': 'company-report'}}
    monkeypatch.setattr(web, '_installed_hub_identifiers', lambda profile=None: installed)
    with TestClient(web.app) as client:
        client.headers[web._SESSION_HEADER_NAME] = web._SESSION_TOKEN
        inventory = client.get('/api/skills/hub/sources?metadata_only=true').json()
        assert [s['id'] for s in inventory['sources']] == ['llano-hub']
        assert inventory['installed'] == installed
        results = client.get('/api/skills/hub/search?q=report').json()
        assert [r['identifier'] for r in results['results']] == ['llano-hub/company-report']
        assert results['installed'] == installed
        assert client.get('/api/skills/hub/search?q=report&source=local').json()['results'] == []
    assert calls == ['llano-hub']
