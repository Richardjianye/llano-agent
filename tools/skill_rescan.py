#!/usr/bin/env python3
"""已安装技能的定期重扫。

安装时扫过一遍不等于永远安全：技能装完之后还会被改 —— 员工自己迭代、sync 拉下
上游新版本、hub 里更新。所以在 curator tick 上再扫一遍已经装好的技能，命中就走
`tools/skill_quarantine.py` 隔离掉。

### 只扫「变过的」

每轮把全部技能重扫一遍太贵，也没意义 —— 内容没动，结论不会变（同一个扫描器
版本下）。用 `skills_guard.full_content_hash` 做指纹，只扫**没扫过的**和
**指纹变了的**。扫描器自己升级时指纹不变但结论可能变，所以状态里也记扫描器版本，
版本一变就全量重扫。

### 判据是「变化」，不是绝对结论

⚠️ 这里踩过一次真实的坑，值得先读：

第一版直接拿 `scan_skill` 的 `verdict == "dangerous"` 当自动隔离的开关。真机
实测（2026-08-27）的结果是 **79 个已装技能隔离了 23 个，全是随包发的那些** ——
github-auth、comfyui、claude-code、hermes-agent、computer-use…… 也就是说员工装好
客户端，第一次重扫就把自带技能干掉三分之一，还往网关传了 23 份样本。

根因不是阈值高低，是**判据用错了**：`skills_guard._determine_verdict` 只看内容、
完全不看信任级别，`dangerous` 的含义仅仅是「含至少一条 critical 级别的模式」。
自带技能本来就该含这些 —— 它们要做认证、要 curl、要起子进程。上游从不单凭
verdict 动手，它用的是 `should_allow_install`（trust × verdict 查表），
而且是在安装时、有人在场的情况下。

所以现在的判据见 `quarantine_decision`：首次观察只记基线，只有**内容变了并且
从不危险变成危险**才隔离。单元测试抓不到这个 —— 合成的三五个技能里没有一个
长得像真实的自带技能。

### 只对 dangerous 下手，caution 不动

这是一个刻意的选择，不是偷懒：

产品前提是员工要**自己写技能**来完成部门指标。`caution` 是启发式判断，
上游对 community 来源在安装时按 caution 拦是合理的（那是别人的代码，还没进来）；
但对着员工**已经在用**的自有技能按 caution 隔离，等于三天两头把人家的工作
搬走。拦死了，人只会绕开工具去干活 —— 那就一条记录都拿不到了。

`caution` 照样记进结果里、照样上报（S7-C），只是不动它。要改的话改
`QUARANTINE_VERDICTS`。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

# 一轮重扫之间至少隔多久。tick 点调用很频繁（curator 循环 + CLI 启动），
# 不设闸的话每次启动都要把变过的技能重扫一遍。
RESCAN_MIN_INTERVAL_SECONDS = 6 * 3600

# 哪些结论触发隔离。见模块文档：caution 只记录不动手。
QUARANTINE_VERDICTS = frozenset({"dangerous"})

STATE_SCHEMA_VERSION = 1


def state_path() -> Path:
    return get_hermes_home() / "skill-rescan-state.json"


def skills_root() -> Path:
    return get_hermes_home() / "skills"


@dataclass
class RescanReport:
    scanned: List[str] = field(default_factory=list)
    quarantined: List[Dict[str, Any]] = field(default_factory=list)
    flagged: List[Dict[str, Any]] = field(default_factory=list)
    # 首次观察、只记基线没动它的。第一轮会等于 scanned，之后正常是空的 ——
    # 它要是长期不空，说明有东西在反复「首次出现」，那本身就值得看一眼。
    baselined: List[str] = field(default_factory=list)
    skipped_unchanged: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "scanned": self.scanned,
            "quarantined": self.quarantined,
            "flagged": self.flagged,
            "baselined": self.baselined,
            "skipped_unchanged": self.skipped_unchanged,
        }


def load_state() -> Dict[str, Any]:
    path = state_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # 状态文件坏了当成没扫过 —— 多扫一轮的代价远小于漏扫。
        return {"schema_version": STATE_SCHEMA_VERSION, "last_run": 0, "seen": {}, "scanner_version": ""}
    if not isinstance(raw, dict):
        return {"schema_version": STATE_SCHEMA_VERSION, "last_run": 0, "seen": {}, "scanner_version": ""}
    raw.setdefault("seen", {})
    raw.setdefault("last_run", 0)
    raw.setdefault("scanner_version", "")
    return raw


def save_state(state: Dict[str, Any]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def installed_skill_dirs(root: Optional[Path] = None) -> List[Path]:
    """活动树里的技能目录（含 SKILL.md 的那一层）。"""
    base = root or skills_root()
    if not base.is_dir():
        return []

    from agent.skill_utils import is_excluded_skill_path

    found: List[Path] = []
    for skill_md in base.rglob("SKILL.md"):
        if is_excluded_skill_path(skill_md):
            continue
        found.append(skill_md.parent)
    return sorted(found)


def select_stale(
    skills: Sequence[Path],
    state: Dict[str, Any],
    *,
    current_scanner_version: str,
    hasher,
) -> List[Path]:
    """挑出需要重扫的：没扫过的、指纹变了的；扫描器换版本时全量。

    纯函数（`hasher` 注入），因为「哪些该扫」是这个模块唯一容易悄悄错的判断 ——
    错了的表现是**扫了个寂寞**，日志里一片正常。
    """
    if state.get("scanner_version") != current_scanner_version:
        return list(skills)

    seen = state.get("seen") or {}
    stale: List[Path] = []
    for skill in skills:
        key = str(skill)
        previous = seen_hash(seen.get(key))
        if previous is None or previous != hasher(skill):
            stale.append(skill)
    return stale


def seen_hash(entry: Any) -> Optional[str]:
    """从状态条目里取指纹。

    旧格式是裸的哈希字符串，新格式是 {"hash": ..., "verdict": ...} —— 升级时
    两种都要认。不认旧格式的话，升上来的那一轮会把**全部**技能当成没扫过，
    于是走一遍「首次观察」，把所有人的基线重置成当前状态。那正好会掩盖掉
    「升级前就被人改过」的那些技能。
    """
    if isinstance(entry, dict):
        value = entry.get("hash")
        return value if isinstance(value, str) else None
    return entry if isinstance(entry, str) else None


def seen_verdict(entry: Any) -> Optional[str]:
    if isinstance(entry, dict):
        value = entry.get("verdict")
        return value if isinstance(value, str) else None
    return None


def should_quarantine(verdict: str) -> bool:
    return verdict in QUARANTINE_VERDICTS


def quarantine_decision(
    *, first_seen: bool, previous_verdict: Optional[str], current_verdict: str
) -> bool:
    """要不要隔离。整条策略就这一个函数，好让它能被单独测。

    ## 只在「变了并且变坏了」时下手（2026-08-27 负责人定）

    起因是真机实测：79 个已装技能里隔离了 23 个，**全是随包发的那些**
    （github-auth、comfyui、claude-code、hermes-agent、computer-use…）。
    换句话说员工装好客户端，第一次重扫就把自带技能干掉三分之一。

    根因不在阈值高低，而在**用错了判据**：`skills_guard._determine_verdict`
    只看内容、完全不看信任级别，`dangerous` 的含义仅仅是「含至少一条 critical
    级别的模式」。而自带技能本来就该含这些 —— 它们要做认证、要 curl、
    要起子进程。上游从不单凭 verdict 动手，它用的是 `should_allow_install`
    （trust × verdict 查表），而且是在安装时、有人在场的情况下。

    所以定期重扫的判据必须是**变化**，不是绝对结论：

    - **首次观察**：只记基线，绝不隔离。这个技能是产品发出去时的样子，
      不是新增风险。
    - **变了但仍不危险**：记指纹，不动。
    - **变了并且从不危险变成危险**：隔离。这正是「有人改了已装技能」的形状。
    - **本来就危险、改完还是危险**：不隔离，但照样进 flagged 上报。
      反复没收员工正在迭代的东西，只会把人赶去绕开工具干活 ——
      那就一条记录都拿不到了。
    """
    if first_seen:
        return False
    if not should_quarantine(current_verdict):
        return False
    return not should_quarantine(previous_verdict or "")


def rescan_now(*, root: Optional[Path] = None) -> RescanReport:
    """立刻重扫一轮，不看时间闸。`hermes skills rescan` 与测试走这条。"""
    from tools.skills_guard import SCANNER_VERSION, _finding_dict, full_content_hash, scan_skill

    from tools import skill_quarantine

    report = RescanReport()
    state = load_state()
    skills = installed_skill_dirs(root)
    stale = select_stale(skills, state, current_scanner_version=SCANNER_VERSION, hasher=full_content_hash)
    report.skipped_unchanged = len(skills) - len(stale)

    seen = dict(state.get("seen") or {})
    for skill in stale:
        try:
            result = scan_skill(skill, source="installed")
        except Exception as exc:  # noqa: BLE001 — 一个技能扫崩了不能中断整轮
            logger.debug("skill_rescan: 扫描 %s 失败: %s", skill, exc, exc_info=True)
            continue

        report.scanned.append(str(skill))
        findings = [_finding_dict(f) for f in result.findings]

        previous_entry = seen.get(str(skill))
        first_seen = previous_entry is None
        decision = quarantine_decision(
            first_seen=first_seen,
            previous_verdict=seen_verdict(previous_entry),
            current_verdict=result.verdict,
        )

        if first_seen:
            # 首次观察只记基线。见 quarantine_decision 的说明：真机上不这么做的话，
            # 员工装好客户端第一次重扫就会把自带技能干掉三分之一。
            report.baselined.append(str(skill))

        if decision:
            try:
                record = skill_quarantine.quarantine_skill(
                    skill,
                    reason=f"定期重扫判定 {result.verdict}：{result.summary or '命中威胁模式'}",
                    verdict=result.verdict,
                    findings=findings,
                    scanner_version=SCANNER_VERSION,
                    bundle_hash=full_content_hash(skill) if skill.exists() else "",
                    source=result.source,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("skill_rescan: 隔离 %s 失败: %s", skill, exc)
                continue
            report.quarantined.append({"entry_id": record.entry_id, "skill_name": record.skill_name})
            # 隔离掉的不再记指纹 —— 它已经不在活动树里了，下轮也不该再被挑中。
            seen.pop(str(skill), None)
            continue

        if findings:
            report.flagged.append(
                {
                    "skill": str(skill),
                    "verdict": result.verdict,
                    "finding_count": len(findings),
                    # 「本来就危险、改完还是危险」落在这里而不是隔离区。
                    # 没有这个字段的话，审查的人分不清「一直如此」和「刚变坏」。
                    "was": seen_verdict(previous_entry),
                    "first_seen": first_seen,
                }
            )
        # 指纹与结论一起记：下一轮判断「变坏了没有」靠的就是这个 verdict。
        seen[str(skill)] = {"hash": full_content_hash(skill), "verdict": result.verdict}

    # 活动树里已经没有的条目从状态里清掉，免得状态文件无限长。
    live = {str(s) for s in installed_skill_dirs(root)}
    state["seen"] = {k: v for k, v in seen.items() if k in live}
    state["scanner_version"] = SCANNER_VERSION
    state["last_run"] = int(time.time())
    state["schema_version"] = STATE_SCHEMA_VERSION
    save_state(state)

    return report


def due_for_rescan(state: Dict[str, Any], *, now: float, interval: float = RESCAN_MIN_INTERVAL_SECONDS) -> bool:
    return now - float(state.get("last_run") or 0) >= interval


def maybe_rescan_skills() -> Optional[Dict[str, Any]]:
    """tick 入口。抄 `maybe_pull_skills` 的形状：尽力而为、绝不抛、返回 dict 或 None。

    绝不抛是硬要求 —— 这东西挂在 curator 循环和 CLI 启动上，让它把宿主带崩，
    等于为了安全检查赔掉整个后端。
    """
    try:
        state = load_state()
        if not due_for_rescan(state, now=time.time()):
            return None
        return rescan_now().as_dict()
    except Exception as exc:  # noqa: BLE001
        logger.debug("skill_rescan: maybe_rescan_skills 失败: %s", exc, exc_info=True)
        return None
