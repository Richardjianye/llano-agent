"""已安装公司技能的更新：判定、单个/全部更新、本机修改保护（2026-10-16，P1）

设计见 docs/design/skill-flow-back-2026-10-16.md 第 1 节。三条约束：

- **判定不取包。** 技能列表本来就批量调 `/v1/skill-hub/metrics`，网关在里面带了最新版
  的 bundle_hash；拿它和安装登记里记的 bundle_hash 比（服务端算的跟服务端算的比），
  不碰本地目录指纹那套算法。以前「全部更新」靠逐个取包比较，每点一次就给每个已装
  技能涨一次下载量。
- **换版本不计下载。** 真要换版本时取包带 `purpose=update`。
- **本机改过的不静默覆盖。** 本地目录指纹 ≠ 安装时的指纹，说明员工（或后台复盘）改过
  它。单个更新要员工二选一：备份后用新版本，或保留自己的（这一版不再提示）；
  「全部更新」只更新没改过的，改过的列出来逐个处理。

更新由员工手动确认（负责人 10-16 定），这里不做自动更新。新版本在新会话里生效，
不动运行中会话的提示词快照。
"""
from __future__ import annotations

import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

SOURCE_ID = "llano-hub"
LOCAL_CHANGES_PREFIX = "SKILL_LOCALLY_MODIFIED::"


class LocalChanges(ValueError):
    """本机改过这个技能；界面据前缀问员工「备份后更新」还是「保留我的」。"""

    def __init__(self, name: str):
        self.name = name
        super().__init__(f"{LOCAL_CHANGES_PREFIX}{name}::这个技能在本机改过，更新前请确认是否先备份你的修改")


def _slug(entry: Dict[str, Any]) -> str:
    return str(entry.get("identifier") or "").removeprefix(f"{SOURCE_ID}/")


def company_entries() -> List[Dict[str, Any]]:
    from tools.skills_hub import HubLockFile
    return [entry for entry in HubLockFile().list_installed() if entry.get("source") == SOURCE_ID]


def fetch_metrics(slugs: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    """批量取指标（每批 100）。网关不可用时抛出，由调用方决定是静默还是报错。"""
    from tools.llano_skill_hub import _request
    unique = [slug for slug in dict.fromkeys(slugs) if slug]
    metrics: Dict[str, Dict[str, Any]] = {}
    for offset in range(0, len(unique), 100):
        response = _request("/v1/skill-hub/metrics", method="POST",
                            body=json.dumps({"slugs": unique[offset:offset + 100]}).encode())
        metrics.update((response or {}).get("metrics") or {})
    return metrics


def _install_dir(entry: Dict[str, Any]) -> Optional[Path]:
    from tools.skills_hub import _resolve_lock_install_path
    try:
        return _resolve_lock_install_path(entry.get("install_path", ""), entry.get("name", ""))
    except (ValueError, OSError):
        return None


def _locally_modified(entry: Dict[str, Any]) -> bool:
    from tools.skills_guard import content_hash
    directory = _install_dir(entry)
    baseline = entry.get("content_hash")
    if not directory or not directory.is_dir() or not baseline:
        return False
    try:
        return content_hash(directory) != baseline
    except OSError:
        return False


def update_state(entry: Dict[str, Any], metric: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """一个已装公司技能相对网关最新版的状态；网关没给数据（看不到/连不上）时返回 None。"""
    if not metric:
        return None
    meta = entry.get("metadata") or {}
    if metric.get("status", "listed") != "listed":
        return {"delisted": True, "delisted_reason": metric.get("delisted_reason") or "",
                "update_available": False, "locally_modified": False}
    latest_hash = str((metric.get("display_metadata") or {}).get("bundle_hash") or "")
    installed_hash = str(meta.get("bundle_hash") or "")
    latest_version = metric.get("version")
    installed_version = meta.get("version")
    if latest_hash and installed_hash:
        newer = latest_hash != installed_hash
    else:
        # 早期安装登记可能没记 bundle_hash：退回比版本号；两个都没有就不猜。
        newer = isinstance(latest_version, int) and isinstance(installed_version, int) and latest_version > installed_version
    ignored = bool(latest_hash) and latest_hash == meta.get("ignored_bundle_hash")
    available = bool(newer and not ignored)
    return {
        "delisted": False,
        "update_available": available,
        "locally_modified": _locally_modified(entry) if available else False,
        "installed_version": installed_version,
        "latest_version": latest_version,
        "latest_bundle_hash": latest_hash,
        "changelog": metric.get("changelog") or "",
        "published_at": metric.get("published_at") or "",
        "publisher_username": metric.get("publisher_username") or "",
    }


def update_states(metrics: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """{安装名: 状态}，供技能列表逐行标「有新版本」「已下架」。"""
    out = {}
    for entry in company_entries():
        state = update_state(entry, metrics.get(_slug(entry)))
        if state is not None:
            out[entry["name"]] = state
    return out


def _backup(name: str, directory: Path) -> Path:
    from tools.skills_hub import _skills_dir
    archive = _skills_dir() / ".archive"
    archive.mkdir(parents=True, exist_ok=True)
    target = archive / f"{name}.before-update-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copytree(directory, target)
    return target


def update_skill(name: str, *, backup_local_changes: bool = False) -> Dict[str, Any]:
    """把一个已装公司技能换成网关上的最新版。

    本机改过且没确认备份时抛 `LocalChanges`，什么都不动。扫描/安装策略与首次安装一致，
    不过就保留旧版不动。
    """
    from tools.llano_skill_hub import fetch_for_update
    from tools.skills_guard import scan_skill, should_allow_install
    from tools.skills_hub import HubLockFile, install_from_quarantine, quarantine_bundle

    entry = HubLockFile().get_installed(name)
    if not entry or entry.get("source") != SOURCE_ID:
        raise ValueError(f"「{name}」不是从公司技能库安装的技能")
    entry = {**entry, "name": name}  # get_installed 不带 name 键；路径校验要用它
    directory = _install_dir(entry)
    if directory is None:
        raise ValueError(f"「{name}」的安装位置不正常，请重新安装")
    modified = _locally_modified(entry)
    if modified and not backup_local_changes:
        raise LocalChanges(name)

    bundle = fetch_for_update(_slug(entry))
    if bundle is None:
        raise ValueError(f"公司技能库里取不到「{name}」的新版本")
    if bundle.metadata.get("bundle_hash") and bundle.metadata.get("bundle_hash") == (entry.get("metadata") or {}).get("bundle_hash"):
        return {"name": name, "updated": False, "version": bundle.metadata.get("version"), "backup": None}
    bundle.name = name

    quarantine = quarantine_bundle(bundle)
    try:
        result = scan_skill(quarantine, source=bundle.identifier)
        allowed, reason = should_allow_install(result)
        if allowed is not True:
            raise ValueError(f"新版本没有通过安全扫描，已保留旧版：{reason}")
        backup = _backup(name, directory) if modified else None
        parent = str(Path(entry.get("install_path", "")).parent)
        install_from_quarantine(quarantine, name, "" if parent == "." else parent, bundle, result)
    finally:
        shutil.rmtree(quarantine, ignore_errors=True)
    try:
        from agent.prompt_builder import clear_skills_system_prompt_cache
        clear_skills_system_prompt_cache(clear_snapshot=True)
    except Exception:  # noqa: BLE001 — 缓存另有目录签名兜底
        logger.debug("技能索引缓存清理失败，下次按目录签名刷新")
    return {"name": name, "updated": True, "version": bundle.metadata.get("version"),
            "backup": str(backup) if backup else None}


def keep_local_version(name: str, bundle_hash: str) -> None:
    """「保留我的版本」：这一版不再提示；作者再发新版时照常提示。"""
    from tools.skills_hub import HubLockFile
    if not bundle_hash or len(bundle_hash) > 128:
        raise ValueError("版本标识无效")
    lock = HubLockFile()
    data = lock.load()
    entry = data.get("installed", {}).get(name)
    if not entry or entry.get("source") != SOURCE_ID:
        raise ValueError(f"「{name}」不是从公司技能库安装的技能")
    entry.setdefault("metadata", {})["ignored_bundle_hash"] = bundle_hash
    lock.save(data)


def update_all() -> Dict[str, Any]:
    """更新所有有新版本、且本机没改过的公司技能；改过的列出来让员工逐个决定。"""
    entries = company_entries()
    metrics = fetch_metrics(_slug(entry) for entry in entries)
    updated: List[Dict[str, Any]] = []
    modified: List[str] = []
    failed: List[Dict[str, str]] = []
    for entry in entries:
        state = update_state(entry, metrics.get(_slug(entry)))
        if not state or not state["update_available"]:
            continue
        if state["locally_modified"]:
            modified.append(entry["name"])
            continue
        try:
            updated.append(update_skill(entry["name"]))
        except Exception as exc:  # noqa: BLE001 — 一个失败不挡其余
            failed.append({"name": entry["name"], "error": str(exc)})
    return {"updated": updated, "locally_modified": modified, "failed": failed}


def pending_update_count() -> int:
    """侧栏小红点用：有新版本的已装公司技能数。只调一次批量指标，不取包；网关不可用记 0。"""
    entries = company_entries()
    if not entries:
        return 0
    try:
        metrics = fetch_metrics(_slug(entry) for entry in entries)
    except Exception as exc:  # noqa: BLE001 — 小红点不该因为网络抖动报错
        logger.debug("公司技能更新计数跳过：%s", exc)
        return 0
    return sum(1 for entry in entries
               if (state := update_state(entry, metrics.get(_slug(entry)))) and state["update_available"])
