#!/usr/bin/env python3
"""把技能安全事件与样本送回网关 —— 阶段 7 · S7-C（上行）。

产品前提：技能来源不封禁（自有 skill hub 是给部门技能指标用的）。安全靠扫描
兜底：命中就隔离，样本传回网关供人工审查；员工自助恢复也要留一条记录。

本期只做**上行**。网关的复核结论怎么回到客户端，下一期再说 —— 所以这里没有
任何「等回执」的路径，发出去、拿到 2xx、删掉，就结束。

发布前扫描命中也走这条上行链路：先发布，再上报提醒和实际发布包的样本，
供管理员事后检查并线下联系发布者。上报故障不撤销已完成的发布。

### 为什么中间要垫一个磁盘队列

隔离发生在后台 tick 里，恢复发生在员工点按钮的那一刻。两个时刻都不保证网络
通：笔记本在地铁上、网关在发版、VPN 断了。直接发就意味着**这些时刻的记录会
悄悄消失**，而这套东西的全部价值就是那条记录。

所以事件先落盘（本地、同步、便宜），发送是另一件事，失败就下一轮再来。

### 隔离与恢复在「写不进队列」时的取舍是相反的

- **隔离**：入队失败照样隔离。隔离是安全动作本身，为了记不上就把危险技能
  留在活动树里，是拿真风险换审计完整性。
- **恢复**：入队失败就**不恢复**。这里记录**就是**安全动作 —— 我们不拦人越过
  扫描结论，代价是这个动作必须留下痕迹；痕迹写不下来，动作就不该发生。

两边都写成 fail-open 或都写成 fail-closed 会各错一半，所以刻意不对称。
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

REPORT_SCHEMA_VERSION = 1

# 样本体积上限。审查的人要看的是代码，不是附带的模型权重或截图。
MAX_SAMPLE_FILES = 40
MAX_SAMPLE_FILE_BYTES = 64 * 1024
MAX_SAMPLE_TOTAL_BYTES = 512 * 1024

# 队列上限。网关长期不可达时不能把员工的磁盘吃光；超了丢**最旧的**，
# 因为最近发生的事更可能是正在排查的那件。
MAX_PENDING_EVENTS = 200

# 一条事件最多试几次。到顶挪进 failed/ 而不是删掉 —— 删掉等于自己把留痕擦了。
MAX_ATTEMPTS = 8

UPLOAD_PATH = "/v1/skill-reports"


def reports_root() -> Path:
    return get_hermes_home() / "skill-reports"


def pending_dir() -> Path:
    return reports_root() / "pending"


def failed_dir() -> Path:
    return reports_root() / "failed"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── 样本采集 ──────────────────────────────────────────────────────────────

def _is_probably_text(data: bytes) -> bool:
    """能不能当文本发。审查的人要读代码，二进制读不了也占地方。"""
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def collect_sample(
    payload_dir: Path,
    *,
    max_files: int = MAX_SAMPLE_FILES,
    max_file_bytes: int = MAX_SAMPLE_FILE_BYTES,
    max_total_bytes: int = MAX_SAMPLE_TOTAL_BYTES,
) -> Dict[str, Any]:
    """把隔离载荷收成可上传的样本。

    **超限的文件不是丢掉，是降级成元数据**（路径 + 大小 + sha256）。审查的人看到
    「有个 12MB 的 .bin 没传上来」是有用的信息；看不到它存在过则不是 —— 而恰恰是
    被刻意藏进大文件、二进制文件里的东西最值得知道它在。
    """
    import hashlib

    payload_dir = Path(payload_dir)
    files: List[Dict[str, Any]] = []
    total = 0
    truncated_reason = ""

    # 排序保证同一份技能每次收出来的样本一致，网关侧才能按内容去重。
    candidates = sorted(p for p in payload_dir.rglob("*") if p.is_file())

    for path in candidates:
        if len(files) >= max_files:
            truncated_reason = "file_count"
            break
        try:
            raw = path.read_bytes()
        except OSError as exc:
            files.append({"path": _rel(path, payload_dir), "error": str(exc), "included": False})
            continue

        entry: Dict[str, Any] = {
            "path": _rel(path, payload_dir),
            "size_bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "included": False,
        }

        if len(raw) > max_file_bytes:
            entry["omitted"] = "too_large"
        elif not _is_probably_text(raw):
            entry["omitted"] = "binary"
        elif total + len(raw) > max_total_bytes:
            entry["omitted"] = "budget_exhausted"
            truncated_reason = truncated_reason or "total_bytes"
        else:
            entry["content"] = raw.decode("utf-8")
            entry["included"] = True
            total += len(raw)

        files.append(entry)

    return {
        "files": files,
        "file_count": len(candidates),
        "included_count": sum(1 for f in files if f.get("included")),
        "included_bytes": total,
        "truncated": bool(truncated_reason),
        "truncated_reason": truncated_reason,
    }


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


# ── 队列 ──────────────────────────────────────────────────────────────────

def _device_label() -> str:
    """机器标签。**仅供人工定位**，不是身份凭据。

    上报的真实身份由网关从 API Key 解出来（谁的号、哪个部门），客户端说自己是谁
    没有意义 —— 它跑在自己的机器上。这个字段只是让审查的人知道「去哪台机器上看」。
    """
    try:
        from tools.skills_sync_client import stable_device_id

        return stable_device_id()
    except Exception:  # noqa: BLE001
        import socket

        try:
            return socket.gethostname()
        except Exception:  # noqa: BLE001
            return ""


def reporting_enabled() -> bool:
    """只有品牌构建上报。原版 Hermes 没有网关，攒一堆发不出去的事件只是占磁盘。"""
    try:
        from hermes_cli.product_policy import load_product_policy

        policy = load_product_policy()
    except Exception:  # noqa: BLE001
        return False
    return bool(policy and policy.gateway_origin)


def enqueue(event: Dict[str, Any]) -> Optional[Path]:
    """把一条事件原子地写进待发队列，返回落盘路径；未启用上报时返回 None。

    先写 `.tmp` 再 rename：flush 会边遍历边发，读到一个写了一半的 JSON 会被当成
    坏事件挪进 failed/ —— 那是**把一条本来好好的记录弄丢**，正是这套东西要防的事。
    """
    if not reporting_enabled():
        return None

    payload = dict(event)
    payload.setdefault("event_id", uuid.uuid4().hex)
    payload.setdefault("reported_at", _now())
    payload.setdefault("schema_version", REPORT_SCHEMA_VERSION)
    payload.setdefault("device", _device_label())

    directory = pending_dir()
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / f"{payload['reported_at'].replace(':', '')}-{payload['event_id']}.json"
    tmp = final.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(tmp, final)

    _prune_pending()
    return final


def list_pending() -> List[Path]:
    directory = pending_dir()
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir() if p.suffix == ".json")


def _prune_pending(limit: Optional[int] = None) -> None:
    # 刻意在函数体里读模块常量，而不是写成默认参数值：默认参数在 import 时就绑死了，
    # 之后改 MAX_PENDING_EVENTS 不会有任何效果 —— 上限失效是**静默**的，
    # 表现只是磁盘慢慢被吃掉。
    limit = MAX_PENDING_EVENTS if limit is None else limit
    pending = list_pending()
    for path in pending[: max(0, len(pending) - limit)]:
        try:
            path.unlink()
        except OSError:
            pass


# ── 事件 ──────────────────────────────────────────────────────────────────

def build_quarantine_event(record: Any, payload_dir: Path) -> Dict[str, Any]:
    """隔离事件：结论 + 命中项 + 样本本体。审查的人要靠它判断是不是误报。"""
    return {
        "event": "skill_quarantined",
        "entry_id": getattr(record, "entry_id", ""),
        "skill_name": getattr(record, "skill_name", ""),
        "verdict": getattr(record, "verdict", ""),
        "reason": getattr(record, "reason", ""),
        "findings": list(getattr(record, "findings", []) or []),
        "scanner_version": getattr(record, "scanner_version", ""),
        "bundle_hash": getattr(record, "bundle_hash", ""),
        "source": getattr(record, "source", ""),
        "origin_relpath": getattr(record, "origin_relpath", ""),
        "quarantined_at": getattr(record, "quarantined_at", ""),
        "sample": collect_sample(payload_dir),
    }


def build_restore_event(record: Any, *, restored_at: str) -> Dict[str, Any]:
    """恢复事件：不带样本 —— 同一个 bundle_hash 的样本隔离时已经传过了。

    网关侧靠 bundle_hash 把两条串起来。重复传一份几百 KB 的样本，换不到任何
    审查信息。
    """
    return {
        "event": "skill_quarantine_restored",
        "entry_id": getattr(record, "entry_id", ""),
        "skill_name": getattr(record, "skill_name", ""),
        "verdict": getattr(record, "verdict", ""),
        "finding_count": len(getattr(record, "findings", []) or []),
        "scanner_version": getattr(record, "scanner_version", ""),
        "bundle_hash": getattr(record, "bundle_hash", ""),
        "source": getattr(record, "source", ""),
        "quarantined_at": getattr(record, "quarantined_at", ""),
        "restored_at": restored_at,
    }


def build_publish_warning_event(result: Any, payload_dir: Path, *,
                                skill_name: str, bundle_hash: str) -> Dict[str, Any]:
    """发布已完成，但扫描有命中：留样本供管理员事后核查。"""
    from dataclasses import asdict
    from tools.skills_guard import SCANNER_VERSION

    return {
        "event": "skill_publish_warning",
        "skill_name": skill_name,
        "verdict": result.verdict,
        "reason": "发布前扫描命中，技能包已在网关，待人工核查",
        "findings": [asdict(finding) for finding in result.findings],
        "scanner_version": SCANNER_VERSION,
        "bundle_hash": bundle_hash,
        "source": "skill-hub-publish",
        "published_at": _now(),
        "sample": collect_sample(payload_dir),
    }


# ── 发送 ──────────────────────────────────────────────────────────────────

PostJson = Callable[[str, Dict[str, str], bytes], int]


def _post_json(url: str, headers: Dict[str, str], body: bytes) -> int:
    from urllib.request import Request, urlopen

    request = Request(url, data=body, headers=headers, method="POST")
    with urlopen(request, timeout=20) as response:  # noqa: S310 — 托管网关地址
        return int(response.status)


def upload_endpoint() -> str:
    from hermes_cli.product_policy import load_product_policy

    policy = load_product_policy()
    origin = (policy.gateway_origin if policy else "").strip().rstrip("/")
    if not origin:
        raise RuntimeError("品牌配置里没有 gateway_origin，无处上报")
    return f"{origin}{UPLOAD_PATH}"


def _api_key() -> str:
    return (os.environ.get("LLANO_GATEWAY_API_KEY") or "").strip()


def flush(*, post_json: PostJson = _post_json, max_events: int = 50) -> Dict[str, Any]:
    """把待发队列送出去。返回本轮的账目，供 CLI 与测试查看。

    单条失败不影响后面几条：一条事件的载荷坏了（磁盘位翻转、手工编辑过）不该
    把整个队列堵死。
    """
    result = {"sent": 0, "failed": 0, "skipped": 0, "gave_up": 0}

    if not reporting_enabled():
        result["skipped"] = len(list_pending())
        return result

    api_key = _api_key()
    if not api_key:
        # 还没登录 / 会话已过期。留在队列里等下一轮，不算失败也不消耗重试次数。
        result["skipped"] = len(list_pending())
        return result

    endpoint = upload_endpoint()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    for path in list_pending()[:max_events]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("skill_report: 事件载荷不可读，挪进 failed/: %s (%s)", path.name, exc)
            _give_up(path)
            result["gave_up"] += 1
            continue

        try:
            status = post_json(endpoint, headers, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        except Exception as exc:  # noqa: BLE001 — 网络不通是常态，不是异常路径
            logger.debug("skill_report: 上报 %s 失败: %s", path.name, exc)
            if _record_attempt(path) >= MAX_ATTEMPTS:
                _give_up(path)
                result["gave_up"] += 1
            else:
                result["failed"] += 1
            continue

        if 200 <= status < 300:
            try:
                path.unlink()
            except OSError:
                pass
            result["sent"] += 1
            continue

        # 4xx 重试多少次都是同一个 4xx（除了 401/429），但客户端分不清哪种，
        # 统一走重试计数：到顶进 failed/，人能看见，也没在无限重发。
        logger.debug("skill_report: 上报 %s 被拒: HTTP %s", path.name, status)
        if _record_attempt(path) >= MAX_ATTEMPTS:
            _give_up(path)
            result["gave_up"] += 1
        else:
            result["failed"] += 1

    return result


def _attempt_path(path: Path) -> Path:
    return path.with_suffix(".attempts")


def _record_attempt(path: Path) -> int:
    marker = _attempt_path(path)
    try:
        count = int(marker.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        count = 0
    count += 1
    try:
        marker.write_text(str(count), encoding="utf-8")
    except OSError:
        pass
    return count


def _give_up(path: Path) -> None:
    """挪进 failed/ 而不是删掉。发不出去的记录**依然是记录** —— 现场排查时
    这几个文件就是唯一的证据。"""
    target_dir = failed_dir()
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        os.replace(path, target_dir / path.name)
    except OSError:
        return
    try:
        _attempt_path(path).unlink()
    except OSError:
        pass


def maybe_flush_reports() -> Optional[Dict[str, Any]]:
    """tick 入口。跟 `maybe_rescan_skills` 一样：尽力而为、绝不抛。"""
    try:
        if not list_pending():
            return None
        return flush()
    except Exception as exc:  # noqa: BLE001
        logger.debug("skill_report: maybe_flush_reports 失败: %s", exc, exc_info=True)
        return None


def flush_in_background() -> None:
    """发布路径只负责入队，并把当前 profile 作用域带到发送线程。"""
    from contextvars import copy_context
    import threading

    context = copy_context()
    threading.Thread(
        target=lambda: context.run(maybe_flush_reports),
        name="skill-report-flush",
        daemon=True,
    ).start()
