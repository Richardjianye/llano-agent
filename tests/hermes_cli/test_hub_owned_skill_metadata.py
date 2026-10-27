import json
from contextlib import nullcontext
from pathlib import Path


def test_hub_metadata_uses_exact_directory_frontmatter_alias_and_content_baseline(monkeypatch,_isolate_hermes_home):
    from hermes_constants import get_hermes_home
    from tools.skills_guard import content_hash
    from tools.skills_tool import _find_all_skills
    from hermes_cli.web_routers import skills as routes
    from tools.llano_skill_hub import hub_display_metadata
    home=Path(get_hermes_home());directory=home/'skills'/'different-directory'
    directory.mkdir(parents=True)
    definition=directory/'SKILL.md'
    definition.write_text('---\nname: named-alias\ndescription: English description\n---\nBody\n')
    receipt=home/'skills'/'.hub-published'/'named-alias.json';receipt.parent.mkdir()
    receipt.write_text(json.dumps({'path':str(directory),'metadata_source':'llano-hub',
        'name':'公司技能标题','description':'服务端统一维护的介绍','content_hash':content_hash(directory)}))
    monkeypatch.setattr(routes,'_profile_scope',lambda profile:nullcontext())
    monkeypatch.setattr(routes,'_config_profile_scope',lambda profile:nullcontext())
    monkeypatch.setattr('hermes_cli.product_policy.load_product_policy',lambda:object())
    rows=_find_all_skills(skip_disabled=True)
    row=next(x for x in routes._localized(rows,title_field='display_name') if x['name']=='named-alias')
    assert row['display_name']=='公司技能标题' and row['description']=='服务端统一维护的介绍'
    assert row['provenance']=='hub' and 'skill_dir' not in row
    assert str(directory.resolve()) in hub_display_metadata()
    # Local content edits invalidate the old server receipt. Chinese edits win.
    definition.write_text('---\nname: named-alias\ndisplay_name: 用户自定中文\ndescription: 用户新用途\n---\nDifferent body\n')
    assert not hub_display_metadata()
    from tools.skills_tool import _SKILLS_CACHE
    _SKILLS_CACHE.clear()
    rows=_find_all_skills(skip_disabled=True)
    row=next(x for x in routes._localized(rows,title_field='display_name') if x['name']=='named-alias')
    assert row['display_name']=='用户自定中文' and row['description']=='用户新用途'


def test_external_same_name_does_not_receive_different_directory_hub_metadata(monkeypatch,_isolate_hermes_home,tmp_path):
    from hermes_constants import get_hermes_home
    from hermes_cli.web_routers import skills as routes
    home=Path(get_hermes_home());external=tmp_path/'external';directory=external/'same-name'
    directory.mkdir(parents=True);(directory/'SKILL.md').write_text('---\nname: same-name\ndescription: English external\n---\nExternal body\n')
    monkeypatch.setattr('agent.skill_utils.get_all_skills_dirs',lambda:[external])
    monkeypatch.setattr('tools.llano_skill_hub.hub_display_metadata',lambda:{str(home/'skills'/'same-name'):{'name':'别的目录','description':'不能套用'}})
    monkeypatch.setattr(routes,'_profile_scope',lambda profile:nullcontext())
    monkeypatch.setattr(routes,'_config_profile_scope',lambda profile:nullcontext())
    monkeypatch.setattr('hermes_cli.product_policy.load_product_policy',lambda:None)
    row=routes._localized([{'name':'same-name','description':'English external'}],title_field='display_name')[0]
    assert row['description']=='English external' and row.get('display_name')!='别的目录'
    assert 'skill_dir' not in row


def test_existing_metrics_response_refreshes_old_lock_only_when_both_hashes_match(monkeypatch,_isolate_hermes_home):
    from hermes_constants import get_hermes_home
    from tools.skills_guard import content_hash
    from tools.skills_hub import HubLockFile
    from tools.llano_skill_hub import refresh_hub_display_metadata,hub_display_metadata
    home=Path(get_hermes_home());directory=home/'skills'/'downloaded'
    directory.mkdir(parents=True);definition=directory/'SKILL.md';original='---\nname: downloaded\ndescription: English\n---\nContent\n';definition.write_text(original)
    lock=HubLockFile();lock.record_install(name='downloaded',source='llano-hub',identifier='llano-hub/downloaded',
        trust_level='trusted',scan_verdict='safe',skill_hash=content_hash(directory),install_path='downloaded',
        files=['SKILL.md'],metadata={'display_name':'English','description':'English','bundle_hash':'server-bundle'})
    metrics={'downloaded':{'display_metadata':{'display_name':'下载技能','description':'服务端汉化介绍',
        'bundle_hash':'server-bundle','metadata_source':'llano-hub','metadata_schema_version':1}}}
    refresh_hub_display_metadata(metrics)
    assert lock.get_installed('downloaded')['metadata']['display_name']=='English'
    assert definition.read_text()==original
    assert hub_display_metadata()[str(directory.resolve())]['name']=='下载技能'
    before=lock.path.read_text();refresh_hub_display_metadata(metrics);assert lock.path.read_text()==before
    metrics['downloaded']['display_metadata']['bundle_hash']='new-server-bundle'
    metrics['downloaded']['display_metadata']['display_name']='不同包用途'
    refresh_hub_display_metadata(metrics);assert lock.path.read_text()==before
    metrics['downloaded']['display_metadata']['bundle_hash']='server-bundle'
    definition.write_text(original+'Local edit')
    refresh_hub_display_metadata(metrics);assert lock.path.read_text()==before


def test_alias_installed_slug_in_existing_metrics_batch(monkeypatch,_isolate_hermes_home):
    from hermes_constants import get_hermes_home
    from tools.skills_hub import HubLockFile
    from tools.skills_guard import content_hash
    from tools.skill_hub_usage_sync import local_skill_metrics
    from tools.llano_skill_hub import hub_display_metadata
    home=Path(get_hermes_home());directory=home/'skills'/'download-slug';directory.mkdir(parents=True)
    (directory/'SKILL.md').write_text('---\nname: different-alias\ndescription: English\n---\nBody')
    HubLockFile().record_install(name='download-slug',source='llano-hub',identifier='llano-hub/download-slug',
        trust_level='trusted',scan_verdict='safe',skill_hash=content_hash(directory),install_path='download-slug',
        files=['SKILL.md'],metadata={'display_name':'English','description':'English','bundle_hash':'hash'})
    monkeypatch.setattr('hermes_cli.product_policy.load_product_policy',lambda:object())
    seen=[]
    def batch(path,**kwargs):
        seen.append(json.loads(kwargs['body']))
        return {'metrics':{'download-slug':{'display_metadata':{'display_name':'服务端标题','description':'服务端用途',
            'metadata_source':'llano-hub','bundle_hash':'hash'}}}}
    monkeypatch.setattr('tools.llano_skill_hub._request',batch)
    local_skill_metrics([{'name':'different-alias'}])
    assert seen==[{'slugs':['download-slug']}]
    assert hub_display_metadata()[str(directory.resolve())]['name']=='服务端标题'


def test_cache_write_failure_and_corrupt_values_do_not_break_list(monkeypatch,_isolate_hermes_home):
    from hermes_constants import get_hermes_home
    from tools.llano_skill_hub import hub_display_metadata,refresh_hub_display_metadata
    from tools.skills_hub import HubLockFile
    from tools.skills_guard import content_hash
    home=Path(get_hermes_home());directory=home/'skills'/'downloaded';directory.mkdir(parents=True)
    (directory/'SKILL.md').write_text('English')
    HubLockFile().record_install(name='downloaded',source='llano-hub',identifier='llano-hub/downloaded',
        trust_level='trusted',scan_verdict='safe',skill_hash=content_hash(directory),install_path='downloaded',files=['SKILL.md'],
        metadata={'display_name':'旧中文标题','description':'旧中文用途','bundle_hash':'hash'})
    (home/'skills'/'.hub-display-metadata.json').write_text(json.dumps({str(directory):['broken']}))
    assert hub_display_metadata()[str(directory)]['name']=='旧中文标题'
    def fail(*a,**kw):raise OSError('read-only')
    monkeypatch.setattr('os.replace',fail)
    refresh_hub_display_metadata({'downloaded':{'display_metadata':{'display_name':'新中文标题','description':'新中文用途',
        'metadata_source':'llano-hub','bundle_hash':'hash'}}})
    assert hub_display_metadata()[str(directory)]['name']=='旧中文标题'
    monkeypatch.setattr('tools.skills_guard.content_hash',fail)
    assert not hub_display_metadata()
