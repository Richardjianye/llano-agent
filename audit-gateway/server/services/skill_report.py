"""技能安全事件上报的载荷解析与规范化 —— 阶段 7 · S7-C（上行）

客户端发上来的一切都是**不可信输入**。它跑在员工自己的机器上，谁都能给它发
一个手写的 JSON。所以这里只做三件事：看得懂就收下、看不懂就明确拒绝、
身份一律不听它的。

### 身份为什么不从载荷里取

user_uid / dept_id 由网关从 API Key 解出来（见 utils/gateway_auth.py）。
载荷里就算带了这些字段也一概忽略 —— 否则任意一把有效 Key 都能把事件记到
别人名下，而这张表的用途恰恰是「谁越过了扫描结论」。

### 样本去重键为什么是服务端算的

客户端上报的 bundle_hash 可能是空的（隔离时没算出来），空值当去重键会把互不
相干的样本合成一条。这里按「文件路径 + 各自 sha256」重算一个指纹，
既不依赖客户端诚实，也不依赖那个字段存在。

注意这个指纹**不用于安全判定**，只用于去重：客户端完全可以上报一份与本地不符
的内容。真正的判定发生在人工审查那一步，看的是这份内容本身。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# 一条事件的 JSON 上限。客户端的样本预算是 512KB，留三倍余量给转义与元数据。
# 超了直接拒，而不是截断：截断之后库里那一行看上去是好的，读到的却是半截代码。
MAX_EVENT_BYTES = 2 * 1024 * 1024

QUARANTINED = "skill_quarantined"
RESTORED = "skill_quarantine_restored"
PUBLISH_WARNING = "skill_publish_warning"
KNOWN_EVENTS = frozenset({QUARANTINED, RESTORED, PUBLISH_WARNING})

# 与 model/database.py 的列宽对齐。超长截断而不是拒收：一个过长的技能名
# 不值得把整条留痕丢掉，而这些字段都不参与判定。
_MAX_LENS = {
    "event_id": 64,
    "event": 32,
    "device": 128,
    "skill_name": 255,
    "entry_id": 64,
    "verdict": 16,
    "reason": 512,
    "source": 64,
    "origin_relpath": 512,
    "scanner_version": 32,
    "bundle_hash": 64,
    "occurred_at": 24,
    "reported_at": 24,
}


class SkillReportRejected(ValueError):
    """载荷不可接收。调用方转成 4xx。"""


@dataclass
class ParsedSample:
    sample_hash: str
    bundle_hash: str
    file_count: int
    included_count: int
    included_bytes: int
    truncated: bool
    truncated_reason: str
    files_json: str


@dataclass
class ParsedEvent:
    event_id: str
    event: str
    skill_name: str
    entry_id: str = ""
    verdict: str = ""
    reason: str = ""
    source: str = ""
    origin_relpath: str = ""
    scanner_version: str = ""
    bundle_hash: str = ""
    finding_count: int = 0
    findings_json: Optional[str] = None
    device: str = ""
    occurred_at: str = ""
    reported_at: str = ""
    schema_version: int = 1
    sample: Optional[ParsedSample] = None
    _unused: List[str] = field(default_factory=list)


def _text(raw: Any, key: str) -> str:
    value = "" if raw is None else str(raw)
    limit = _MAX_LENS.get(key)
    return value[:limit] if limit else value


def sample_fingerprint(files: List[Dict[str, Any]]) -> str:
    """按「路径 + 各自 sha256」算指纹。

    只用路径和哈希、不用内容本身：超限的文件没有内容上来，但它的哈希在。
    换成对内容取哈希的话，同一份技能在「样本收全了」和「有文件超限」两种情况下
    会算出两个指纹，去重就失效了 —— 而那正是同一份技能。
    """
    parts = sorted(
        f"{str(entry.get('path') or '')}:{str(entry.get('sha256') or '')}"
        for entry in files
    )
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _parse_sample(raw: Any, bundle_hash: str) -> Optional[ParsedSample]:
    if not isinstance(raw, dict):
        return None
    files = raw.get("files")
    if not isinstance(files, list):
        raise SkillReportRejected("sample.files 必须是数组")
    clean = [entry for entry in files if isinstance(entry, dict)]

    return ParsedSample(
        sample_hash=sample_fingerprint(clean),
        bundle_hash=bundle_hash,
        file_count=_int(raw.get("file_count")),
        included_count=_int(raw.get("included_count")),
        included_bytes=_int(raw.get("included_bytes")),
        truncated=bool(raw.get("truncated")),
        truncated_reason=_text(raw.get("truncated_reason"), "truncated_reason")[:32],
        files_json=json.dumps(clean, ensure_ascii=False),
    )


def _int(raw: Any) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def parse_event(payload: Any, *, raw_bytes: Optional[int] = None) -> ParsedEvent:
    """把客户端载荷解析成一条待入库的事件，或明确拒绝。"""
    if raw_bytes is not None and raw_bytes > MAX_EVENT_BYTES:
        raise SkillReportRejected(f"载荷过大：{raw_bytes} 字节，上限 {MAX_EVENT_BYTES}")

    if not isinstance(payload, dict):
        raise SkillReportRejected("载荷必须是 JSON 对象")

    event = _text(payload.get("event"), "event")
    if event not in KNOWN_EVENTS:
        # 认不出来的事件类型直接拒。收下一个我们看不懂的东西，等于在表里放一行
        # 谁也不知道该怎么读的记录。
        raise SkillReportRejected(f"未知事件类型：{event or '(空)'}")

    event_id = _text(payload.get("event_id"), "event_id").strip()
    if not event_id:
        raise SkillReportRejected("缺少 event_id（幂等键）")

    skill_name = _text(payload.get("skill_name"), "skill_name").strip()
    if not skill_name:
        raise SkillReportRejected("缺少 skill_name")

    findings = payload.get("findings")
    findings_json = None
    finding_count = _int(payload.get("finding_count"))
    if isinstance(findings, list):
        findings_json = json.dumps(findings, ensure_ascii=False)
        finding_count = len(findings)

    bundle_hash = _text(payload.get("bundle_hash"), "bundle_hash")

    sample = None
    if event in (QUARANTINED, PUBLISH_WARNING):
        sample = _parse_sample(payload.get("sample"), bundle_hash)
    if event == PUBLISH_WARNING and sample is None:
        raise SkillReportRejected("发布扫描提醒缺少技能样本")

    return ParsedEvent(
        event_id=event_id,
        event=event,
        skill_name=skill_name,
        entry_id=_text(payload.get("entry_id"), "entry_id"),
        verdict=_text(payload.get("verdict"), "verdict"),
        reason=_text(payload.get("reason"), "reason"),
        source=_text(payload.get("source"), "source"),
        origin_relpath=_text(payload.get("origin_relpath"), "origin_relpath"),
        scanner_version=_text(payload.get("scanner_version"), "scanner_version"),
        bundle_hash=bundle_hash,
        finding_count=finding_count,
        findings_json=findings_json,
        device=_text(payload.get("device"), "device"),
        occurred_at=_text(
            payload.get("quarantined_at") if event == QUARANTINED else
            payload.get("published_at") if event == PUBLISH_WARNING else
            payload.get("restored_at"), "occurred_at"
        ),
        reported_at=_text(payload.get("reported_at"), "reported_at"),
        schema_version=_int(payload.get("schema_version")) or 1,
        sample=sample,
    )
