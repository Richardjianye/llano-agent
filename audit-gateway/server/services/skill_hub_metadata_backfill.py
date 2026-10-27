"""Reviewable metadata-only backfill with optimistic concurrency and rollback.

No model calls or production writes happen at import or planning time. Persist
prepare_plan's full original snapshot before applying; callers supply explicit
reviewed Chinese proposals. Bundle files and version pointers never change.
"""
import json
from pathlib import Path
from datetime import datetime
from sqlalchemy import select, update, literal_column, bindparam
from model.database import SkillHubSkill, SkillHubVersion
from services.skill_hub import publish_metadata
from services.skill_hub_metadata import has_chinese, metadata_missing


async def snapshot(session):
    statement = select(SkillHubSkill.id, SkillHubSkill.slug, SkillHubSkill.name,
        SkillHubSkill.description, SkillHubSkill.latest_version_id,
        SkillHubVersion.bundle_hash).join(SkillHubVersion,
        SkillHubVersion.id == SkillHubSkill.latest_version_id)
    return [dict(row) for row in (await session.execute(statement)).mappings()]


def prepare_plan(rows, proposals):
    entries = []
    for row in rows:
        if not metadata_missing(row['name'], row['description']):
            continue
        proposal = proposals.get(row['slug'])
        if not proposal:
            continue
        updated = {field: row[field] if has_chinese(row[field]) else publish_metadata(proposal.get(field), field)
                   for field in ('name', 'description')}
        entries.append({'original': dict(row), 'updated': updated})
    return {'schema_version': 1, 'entries': entries}


def persist_plan(plan, path):
    # Exclusive create prevents accidentally destroying the only rollback copy.
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(plan, stream, ensure_ascii=False, indent=2)
        stream.flush()
        import os
        os.fsync(stream.fileno())


async def apply_plan(session, plan_path, *, rollback=False):
    # Require the persisted review artifact, not a transient in-memory proposal.
    plan = json.loads(Path(plan_path).read_text(encoding='utf-8'))
    if plan.get('schema_version') != 1:
        raise ValueError('不支持的回填计划版本')
    changed, skipped = [], []
    for entry in plan['entries']:
        original, updated = entry['original'], entry['updated']
        expected = updated if rollback else original
        target = original if rollback else updated
        for field in ('name', 'description'):
            if not rollback:
                publish_metadata(target[field], field)
        guards = []
        if session.bind.dialect.name == "mysql":
            guards.extend([literal_column("BINARY name") == bindparam("metadata_expected_name", expected["name"]),
                           literal_column("BINARY description") == bindparam("metadata_expected_description", expected["description"])])
        if original.get("updated_at") is not None:
            timestamp = original["updated_at"]
            if isinstance(timestamp, str):
                timestamp = datetime.fromisoformat(timestamp)
            guards.append(SkillHubSkill.updated_at == timestamp)
        if "latest_version" in original:
            guards.append(SkillHubSkill.latest_version == original["latest_version"])
        result = await session.execute(update(SkillHubSkill).where(
            *guards,
            SkillHubSkill.id == original['id'],
            SkillHubSkill.latest_version_id == original['latest_version_id'],
            SkillHubSkill.name == expected['name'],
            SkillHubSkill.description == expected['description'],
            SkillHubSkill.latest_version_id.in_(select(SkillHubVersion.id).where(
                SkillHubVersion.bundle_hash == original['bundle_hash'])),
        ).values(name=target['name'], description=target['description'], updated_at=SkillHubSkill.updated_at))
        (changed if result.rowcount else skipped).append(original['slug'])
    await session.commit()
    return {'changed': changed, 'skipped': skipped, 'rollback': rollback}
