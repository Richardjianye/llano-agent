#!/usr/bin/env python3
"""技能隔离区 —— 扫描命中之后把技能挪出活动树，但不删。

产品前提（2026-08-26 负责人定）：技能来源不封禁。自有 skill hub 是给各部门下
技能指标用的，把员工的工作经验转成公司资产，所以下载要放开。安全靠**安装时与
定期扫描**兜底：命中就立即禁用、把样本传回网关供人工审查。

「禁用」在这里是**隔离而不是删除**，而且员工可以自己看原因、自己放回来 ——
被误报干掉一个技能，赔进去的是他这个月的技能指标。放回来这个动作会产生一条
要上报网关的事件（留痕），这是这套东西成立的前提：不是拦住人，是让越过的动作
留下记录。

### 为什么隔离区在 SKILLS_DIR 外面

`agent/skill_utils.py` 有一套 `EXCLUDED_SKILL_DIRS`，把 `.hub` / `.archive`
这些目录从技能发现里剔掉，看上去把隔离区放 `SKILLS_DIR/.quarantine/` 就行了。

但全仓 28 处 `rglob("SKILL.md")` 里只有 21 处紧跟着 `is_excluded_skill_path`。
把「这个技能还生不生效」压在「每一处扫描都记得调排除函数」上，就是在赌剩下
那几处 —— 而赌输的表现是**技能明明显示已隔离、实际还在跑**，没有任何报错。

放在 `HERMES_HOME/skill-quarantine/`（活动树之外）就没有这个依赖：任何以
`SKILLS_DIR` 为根的扫描，无论记不记得排除，都够不到它。
"""

from __future__ import annotations

import json
import logging
import shutil
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

# 隔离记录随包放在被隔离技能旁边，一并挪走、一并恢复。
MANIFEST_NAME = "quarantine.json"
QUARANTINE_SCHEMA_VERSION = 1


def quarantine_root() -> Path:
    """隔离区根目录。刻意在 SKILLS_DIR 之外 —— 见模块文档。"""
    return get_hermes_home() / "skill-quarantine"


def skills_root() -> Path:
    """活动技能树。跟 tools/skills_sync.SKILLS_DIR 指同一处。"""
    return get_hermes_home() / "skills"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class QuarantineRecord:
    """一次隔离的全部事实。员工看的是它，上报网关的也是它。"""

    entry_id: str
    skill_name: str
    quarantined_at: str
    reason: str
    verdict: str
    findings: List[Dict[str, Any]] = field(default_factory=list)
    scanner_version: str = ""
    bundle_hash: str = ""
    source: str = ""
    # 原路径相对 skills_root()，恢复时要放回原处而不是拍到根上 ——
    # 技能是按 category/skill 分层的，放错地方等于换了个技能。
    origin_relpath: str = ""
    restored_at: Optional[str] = None
    schema_version: int = QUARANTINE_SCHEMA_VERSION

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2, sort_keys=True)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "QuarantineRecord":
        known = {f for f in cls.__dataclass_fields__}  # noqa: F821
        return cls(**{k: v for k, v in raw.items() if k in known})


def _entry_dir(entry_id: str) -> Path:
    return quarantine_root() / entry_id


def _payload_dir(entry_id: str) -> Path:
    return _entry_dir(entry_id) / "payload"


def quarantine_skill(
    skill_dir: Path,
    *,
    reason: str,
    verdict: str = "dangerous",
    findings: Optional[List[Dict[str, Any]]] = None,
    scanner_version: str = "",
    bundle_hash: str = "",
    source: str = "",
) -> QuarantineRecord:
    """把一个已安装技能挪进隔离区，返回隔离记录。

    移动而不是复制：留一份在活动树里就等于没隔离。
    """
    skill_dir = Path(skill_dir)
    if not skill_dir.is_dir():
        raise FileNotFoundError(f"技能目录不存在：{skill_dir}")

    try:
        origin_relpath = str(skill_dir.resolve().relative_to(skills_root().resolve()))
    except (OSError, ValueError):
        # 活动树之外的东西不该走这里 —— 隔离一个我们放不回去的目录，
        # 等于把用户的文件搬走之后没有还回去的路。
        raise ValueError(f"拒绝隔离活动技能树之外的目录：{skill_dir}")

    entry_id = f"{_now().replace(':', '').replace('-', '')}-{uuid.uuid4().hex[:8]}"
    record = QuarantineRecord(
        entry_id=entry_id,
        skill_name=skill_dir.name,
        quarantined_at=_now(),
        reason=reason,
        verdict=verdict,
        findings=list(findings or []),
        scanner_version=scanner_version,
        bundle_hash=bundle_hash,
        source=source,
        origin_relpath=origin_relpath,
    )

    entry = _entry_dir(entry_id)
    entry.mkdir(parents=True, exist_ok=False)
    # 先写记录再搬文件：反过来的话，搬完崩在写记录上会留下一份**没有原因、
    # 也不知道该放回哪里**的孤儿载荷。
    (entry / MANIFEST_NAME).write_text(record.to_json(), encoding="utf-8")
    shutil.move(str(skill_dir), str(_payload_dir(entry_id)))

    # 样本上报（阶段 7 · S7-C）。**入队失败不影响隔离** —— 隔离本身就是安全动作，
    # 为了记不上就把判定为危险的技能留在活动树里，是拿真风险换审计完整性。
    try:
        from tools.skill_report import build_quarantine_event, enqueue

        enqueue(build_quarantine_event(record, _payload_dir(entry_id)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("skill_quarantine: 隔离事件入队失败（隔离已生效）: %s", exc)

    return record


def list_quarantined(*, include_restored: bool = False) -> List[QuarantineRecord]:
    """列出隔离区里的条目，最近的在前。员工界面读这个。"""
    root = quarantine_root()
    if not root.is_dir():
        return []

    records: List[QuarantineRecord] = []
    for entry in root.iterdir():
        manifest = entry / MANIFEST_NAME
        if not manifest.is_file():
            continue
        try:
            record = QuarantineRecord.from_dict(json.loads(manifest.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            # 记录坏了不能让整个列表打不开 —— 员工至少要能看见其余条目。
            continue
        if record.restored_at and not include_restored:
            continue
        records.append(record)

    return sorted(records, key=lambda r: r.quarantined_at, reverse=True)


def get_record(entry_id: str) -> Optional[QuarantineRecord]:
    manifest = _entry_dir(entry_id) / MANIFEST_NAME
    if not manifest.is_file():
        return None
    try:
        return QuarantineRecord.from_dict(json.loads(manifest.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return None


def restore_skill(entry_id: str) -> Dict[str, Any]:
    """员工自助恢复。事件先落到本地待发队列，再把技能放回活动树。

    上报是这套东西成立的前提 —— 我们不拦人，但越过扫描结论的动作必须留痕。

    这里只负责**入队**（本地磁盘写，便宜且不看网络），发送由 tools/skill_report.py
    的 flush 另行完成。所以网关不可达时恢复照常可用 —— 卡住恢复的只有「连本地
    队列都写不进去」，那时痕迹本来也留不下来。
    """
    record = get_record(entry_id)
    if record is None:
        raise FileNotFoundError(f"隔离条目不存在：{entry_id}")
    if record.restored_at:
        raise ValueError(f"该条目已恢复过：{entry_id}")

    payload = _payload_dir(entry_id)
    if not payload.is_dir():
        raise FileNotFoundError(f"隔离载荷缺失：{payload}")

    target = skills_root() / record.origin_relpath
    if target.exists():
        # 隔离期间同名技能又被装了一份。直接覆盖会把新的那份悄悄干掉，
        # 而员工以为自己只是「恢复」了一下。
        raise FileExistsError(f"原位置已被占用，先处理再恢复：{target}")

    restored_at = _now()

    # 先入队，再放回去 —— 与 quarantine_skill「先写记录再搬文件」同一个道理，
    # 也与那里的取舍**刻意相反**：隔离时入队失败照样隔离，恢复时入队失败就不恢复。
    #
    # 我们不拦员工越过扫描结论（拦死了人只会绕开工具，一条记录都拿不到），
    # 代价是这个动作必须留下痕迹。痕迹写不下来，动作就不该发生。
    #
    # 反过来（先搬后入队）的失败形态是最坏的那种：技能已经回到活动树里跑着，
    # 而网关那边什么都没有。
    from tools.skill_report import build_restore_event, enqueue

    enqueue(build_restore_event(record, restored_at=restored_at))

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(payload), str(target))

    record.restored_at = restored_at
    (_entry_dir(entry_id) / MANIFEST_NAME).write_text(record.to_json(), encoding="utf-8")

    return {
        "event": "skill_quarantine_restored",
        "entry_id": record.entry_id,
        "skill_name": record.skill_name,
        "verdict": record.verdict,
        "finding_count": len(record.findings),
        "scanner_version": record.scanner_version,
        "bundle_hash": record.bundle_hash,
        "source": record.source,
        "quarantined_at": record.quarantined_at,
        "restored_at": record.restored_at,
    }
