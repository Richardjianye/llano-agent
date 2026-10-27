import io
import zipfile
import pytest
from tools import skillhub_cn as hub


def archive(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as out:
        for path, content in files.items():
            out.writestr(path, content)
    return stream.getvalue()


def test_scoped_identity_preserves_namespace():
    first = hub._meta({'slug':'demo', 'namespace':{'handle':'one'}})
    second = hub._meta({'slug':'demo', 'namespace':{'handle':'two'}})
    assert first.identifier != second.identifier
    assert hub.coordinates(first.identifier) == ('one', 'demo')


def test_archive_preserves_support_files_and_bytes():
    files = {'root/SKILL.md': b'# Demo', 'root/assets/test.bin': bytes(range(256))}
    unpacked = hub.unpack_skill_zip(archive(files))
    assert unpacked == {k.removeprefix('root/'):v for k,v in files.items()}


@pytest.mark.parametrize('path', ['../escape', '/tmp/escape', 'root/../../escape'])
def test_archive_traversal_is_rejected(path):
    with pytest.raises(ValueError):
        hub.unpack_skill_zip(archive({'SKILL.md':'Demo', path:'unsafe'}))


def test_scoped_resolution_never_falls_back_to_other_sources():
    from hermes_cli.skills_hub import _resolve_source_meta_and_bundle
    class Other:
        def source_id(self): return 'github'
        def inspect(self, identifier): raise AssertionError('Wrong registry called')
        def fetch(self, identifier): raise AssertionError('Wrong registry called')
    assert _resolve_source_meta_and_bundle('skillhub/one/demo', [Other()]) == (None, None, None)


def test_recommendation_pages_share_snapshot_until_expiry(monkeypatch):
    monkeypatch.setattr(hub, '_RECOMMENDATION_CACHE', None)
    now = [1000]
    monkeypatch.setattr(hub.time, 'monotonic', lambda: now[0])
    calls = []
    def fetch(path):
        calls.append(path)
        return {'skills': [{'slug': f'skill-{i}', 'namespace': {'handle': 'test'}} for i in range(30)]}
    monkeypatch.setattr(hub, '_json', fetch)
    source = hub.SkillHubCnSource()
    first, total = source.recommended(0, 12)
    second, _ = source.recommended(12, 12)
    assert total == 30
    assert len(calls) == 1
    assert not {s.identifier for s in first} & {s.identifier for s in second}
    now[0] += 301
    source.recommended(24, 12)
    assert len(calls) == 2
