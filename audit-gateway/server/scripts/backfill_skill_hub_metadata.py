"""Offline planning by default; applying/rollback require explicit flags.

python scripts/backfill_skill_hub_metadata.py --snapshot snapshot.json \
  --proposals chinese-proposals.json --plan reviewed-plan.json
python scripts/backfill_skill_hub_metadata.py --plan reviewed-plan.json --apply
python scripts/backfill_skill_hub_metadata.py --plan reviewed-plan.json --rollback

The selected server database configuration is used only for explicit apply or
rollback. Planning has no database/network/model dependency.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.skill_hub_metadata_backfill import prepare_plan, persist_plan, apply_plan


def main():
    parser = argparse.ArgumentParser(description='技能中文元数据离线审阅计划及 CAS 回填')
    parser.add_argument('--snapshot');parser.add_argument('--proposals');parser.add_argument('--plan', required=True)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--apply', action='store_true');actions.add_argument('--rollback', action='store_true')
    args = parser.parse_args()
    if not (args.apply or args.rollback):
        if not args.snapshot or not args.proposals:
            parser.error('默认仅生成计划，需要 --snapshot 和 --proposals')
        plan = prepare_plan(json.loads(Path(args.snapshot).read_text()), json.loads(Path(args.proposals).read_text()))
        persist_plan(plan, args.plan)
        print(json.dumps({'dry_run': True, 'planned': len(plan['entries']), 'plan': args.plan}))
        return
    async def execute():
        from db import init_async_session
        factory = await init_async_session()
        async with factory() as session:
            print(json.dumps(await apply_plan(session, args.plan, rollback=args.rollback), ensure_ascii=False))
    asyncio.run(execute())
if __name__ == '__main__':
    main()
