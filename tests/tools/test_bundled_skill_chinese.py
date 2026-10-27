"""Cold-home package metadata is complete, offline, and does not overwrite edits."""
import hashlib
from pathlib import Path
import shutil

import pytest

from agent.skill_utils import parse_frontmatter
from tools.bundled_skill_chinese import apply_bundled_metadata, bundled_catalog
from tools.skill_chinese_metadata import has_chinese

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def installed_catalog(tmp_path, monkeypatch):
    monkeypatch.setattr('hermes_constants.get_hermes_home', lambda: tmp_path)
    entries = bundled_catalog()['skills']
    for relative in entries:
        source = ROOT / 'skills' / relative / 'SKILL.md'
        if not source.exists():
            source = ROOT / 'apps/desktop/resources/default-skills' / relative / 'SKILL.md'
        destination = tmp_path / 'skills' / relative / 'SKILL.md'
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    return tmp_path, entries


def test_catalog_covers_actual_package_and_resolves_all_cold_home_entries_offline(installed_catalog, monkeypatch):
    home, entries = installed_catalog
    from tools import skill_chinese_metadata
    monkeypatch.setattr(skill_chinese_metadata, '_translate', lambda *_: pytest.fail('Must not call AI'))
    expected = {str(p.parent.relative_to(ROOT / 'skills')) for p in (ROOT / 'skills').rglob('SKILL.md')}
    expected.remove('research/research-paper-writing')
    expected.update(str(p.parent.relative_to(ROOT / 'apps/desktop/resources/default-skills'))
                    for p in (ROOT / 'apps/desktop/resources/default-skills').rglob('SKILL.md'))
    assert set(entries) == expected
    for relative, entry in entries.items():
        path = home / 'skills' / relative / 'SKILL.md'
        before = path.read_bytes()
        meta, body = parse_frontmatter(before.decode())
        assert meta['name'] == entry['name']
        assert hashlib.sha256(before).hexdigest() == entry['source_sha256']
        assert hashlib.sha256(body.encode()).hexdigest() == entry['source_body_sha256']
        row = {'name': entry['name'], 'category': entry['category'], 'description': entry['source_description'],
               'skill_dir': str(path.parent)}
        result = apply_bundled_metadata(row, title_field='display_name')
        assert result['name'] == row['name']
        assert result['display_name'] == entry['display_name']
        # Already-Chinese descriptions are user-visible and stay verbatim.
        assert result['description'] == (entry['source_description'] if has_chinese(entry['source_description']) else entry['description'])
        assert result['metadata_pending'] is False
        assert len(entry['display_name']) <= 20
        assert len(entry['description']) <= 60
        assert path.read_bytes() == before
        assert row.get('display_name') is None


def test_preserves_chinese_user_edits_and_rejects_edited_body_or_unrelated_source(installed_catalog):
    home, entries = installed_catalog
    relative = 'productivity/pdf'
    entry = entries[relative]
    path = home / 'skills' / relative / 'SKILL.md'
    original = path.read_text()
    from tools.skill_import import _set_frontmatter_key
    edited = _set_frontmatter_key(_set_frontmatter_key(original, 'display_name', '我的文档助手'), 'description', '我自定义的中文介绍。')
    path.write_text(edited)
    row = {'name': entry['name'], 'category': entry['category'], 'display_name': '我的文档助手',
           'description': '我自定义的中文介绍。', 'skill_dir': str(path.parent)}
    assert apply_bundled_metadata(row, title_field='display_name')['display_name'] == row['display_name']
    assert apply_bundled_metadata(row, title_field='display_name')['description'] == row['description']
    assert apply_bundled_metadata({**row, 'provenance': 'hub'}, title_field='display_name') is None
    assert apply_bundled_metadata({**row, 'provenance': 'imported'}, title_field='display_name') is None
    assert apply_bundled_metadata({**row, 'provenance': 'agent'}, title_field='display_name') is None
    assert apply_bundled_metadata({**row, 'skill_dir': str(home / 'external' / 'pdf')}, title_field='display_name') is None
    path.write_text(edited + '\nUser changed instructions.\n')
    assert apply_bundled_metadata(row, title_field='display_name') is None
    path.write_text(_set_frontmatter_key(original, 'description', 'Different user-written English description.'))
    assert apply_bundled_metadata({**row, 'description': 'Different user-written English description.'}, title_field='display_name') is None


def test_real_skill_scanner_and_localized_rows_accept_every_installed_bundle(installed_catalog, monkeypatch):
    home, entries = installed_catalog
    import hermes_cli.product_policy as policy
    from tools import skills_tool, skill_chinese_metadata
    monkeypatch.setattr(policy, 'load_product_policy', lambda: object())
    monkeypatch.setattr(skill_chinese_metadata, 'get_hermes_home', lambda: home)
    monkeypatch.setattr(skill_chinese_metadata, '_translate', lambda *_: pytest.fail('List route must not call AI'))
    monkeypatch.setattr(skills_tool, '_skills_dir', lambda: home / 'skills')
    monkeypatch.setattr('agent.skill_utils.get_external_skills_dirs', lambda: [])
    monkeypatch.setattr(skills_tool, 'skill_matches_platform', lambda _: True)
    monkeypatch.setattr(skills_tool, 'skill_matches_environment', lambda _: True)
    monkeypatch.setattr(skills_tool, '_SKILLS_CACHE', {})
    rows = skills_tool._find_all_skills(skip_disabled=True)
    by_name = {row['name']: row for row in rows}
    assert set(by_name) == {entry['name'] for entry in entries.values()}
    for row in skill_chinese_metadata.localize_rows(rows, title_field='display_name'):
        assert has_chinese(row['display_name']), row['name']
        assert has_chinese(row['description']), row['name']
        assert row.get('metadata_pending') is False, row['name']
    for name in ['baoyu-infographic', 'inspecting-hermes-desktop-dom']:
        result = apply_bundled_metadata(by_name[name], title_field='display_name')
        assert result and has_chinese(result['description']), name
