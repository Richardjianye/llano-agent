"""异步同步公司技能的真实使用事件；不上传私人技能内容、提示词或密钥。"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
import uuid
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)
_jobs = queue.Queue(maxsize=256)
_thread = None
_lock = threading.Lock()


@lru_cache(maxsize=16)
def _identity(origin, key):
    import httpx
    with httpx.Client(timeout=8, trust_env=False) as client:
        response = client.get(origin + "/v1/skill-hub/usage-identity", headers={"Authorization": "Bearer " + key})
        response.raise_for_status()
        identity = response.json()
        if identity.get("scope") != "private":
            raise ValueError("协作凭据不同步个人技能调用")
        uid = identity.get("uid")
        if not isinstance(uid, str) or not 1 <= len(uid) <= 36:
            raise ValueError("调用身份响应无效")
        return uid


def _sync(home, origin, key, slug):
    import httpx
    uid = _identity(origin, key)
    directory = Path(home) / "cache" / "skill-use-pending"
    directory.mkdir(parents=True, exist_ok=True)
    event = {"event_id": uuid.uuid4().hex, "slug": slug, "uid": uid, "origin": origin}
    path = directory / (event["event_id"] + ".json")
    from utils import atomic_write_text
    atomic_write_text(path, json.dumps(event))
    pending = []
    for path in sorted(directory.glob("*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            if item.get("uid") == uid and item.get("origin") == origin:
                pending.append((path, item))
        except (ValueError, OSError):
            continue
        if len(pending) >= 100: break
    if not pending: return
    with httpx.Client(timeout=8, trust_env=False) as client:
        response = client.post(origin + "/v1/skill-hub/usage", headers={"Authorization": "Bearer " + key},
            json={"expected_uid": uid, "events": [{"event_id": item["event_id"], "slug": item["slug"]} for _, item in pending]})
        response.raise_for_status()
        result = response.json()
        accepted = set(result.get("accepted") or []) | set(result.get("rejected") or [])
    for path, item in pending:
        if item["event_id"] in accepted:
            path.unlink(missing_ok=True)


def _worker():
    while True:
        job = _jobs.get()
        try:
            _sync(*job)
        except Exception:
            logger.debug("公司技能调用同步未完成，已排队的事件将在后续调用时重试")
        finally:
            _jobs.task_done()


def report_use(skill_name: str) -> None:
    """仅 bump_use 调用；查看、编辑等事件不会触发。认证 UID 在网关再次核对。"""
    global _thread
    try:
        from hermes_cli.product_policy import load_product_policy
        from hermes_cli.config import load_config_readonly
        from hermes_constants import get_hermes_home
        from tools.skills_hub import HubLockFile
        policy = load_product_policy()
        if not policy or not policy.sync_skill_usage:
            return
        config = load_config_readonly()
        if config.get("skills", {}).get("sync_company_usage") is False:
            return
        key = os.environ.get("LLANO_GATEWAY_API_KEY", "").strip()
        if not key: return
        entry = HubLockFile().get_installed(skill_name)
        if entry and entry.get("source") == "llano-hub":
            slug = str(entry.get("identifier", "")).removeprefix("llano-hub/")
        elif entry:
            return  # 不向公司网关发送第三方/未发布的私人技能列表。
        else:
            # 自己发布过的技能才发；使用本地发布回执（含实际发布的 slug），不上传私人名称。
            from tools.skill_publish import published_receipt
            receipt = published_receipt(skill_name)
            if not receipt: return
            slug = str(receipt.get("slug") or "")
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?", slug): return
        _jobs.put_nowait((str(get_hermes_home()), policy.gateway_origin.rstrip("/"), key, slug))
        with _lock:
            if _thread is None or not _thread.is_alive():
                _thread = threading.Thread(target=_worker, name="skill-hub-usage", daemon=True)
                _thread.start()
    except Exception:
        logger.debug("技能调用同步不可用，不影响技能执行")


def local_skill_metrics(rows):
    from hermes_cli.product_policy import load_product_policy
    from hermes_constants import get_hermes_home
    from tools.skills_hub import HubLockFile
    policy = load_product_policy()
    if not policy: return rows
    installed = {entry["name"]: entry for entry in HubLockFile().list_installed()}
    to_query = {}
    for row in rows:
        name = row["name"]
        entry = installed.get(name)
        slug = str((entry or {}).get("identifier") or "").removeprefix("llano-hub/")
        if entry and entry.get("source") == "llano-hub":
            to_query[name] = slug
            row["download_count"] = None
        elif not entry:
            from tools.skill_publish import published_receipt
            receipt = published_receipt(name)
            if receipt and receipt.get("slug"):
                to_query[name] = str(receipt["slug"])
                row["download_count"] = None
            else:
                row["download_count"] = 0  # 尚未发布的本机技能没有网关下载。
        else:
            row["download_count"] = (entry or {}).get("metadata", {}).get("download_count")
    metrics = {}
    try:
        from tools.llano_skill_hub import _request
        # Frontmatter names may differ from install slugs. Refresh each installed
        # hub baseline in the same existing batch, without assigning by alias.
        hub_slugs = [str(entry.get("identifier") or "").removeprefix("llano-hub/")
                     for entry in installed.values() if entry.get("source") == "llano-hub"]
        slugs = list(dict.fromkeys([*to_query.values(), *hub_slugs]))
        for offset in range(0, len(slugs), 100):
            response = _request("/v1/skill-hub/metrics", method="POST",
                body=json.dumps({"slugs": slugs[offset:offset + 100]}).encode())
            metrics.update(response.get("metrics") or {})
    except Exception:
        logger.debug("下载量暂不可读取，保留未知状态")
    from tools.llano_skill_hub import refresh_hub_display_metadata
    refresh_hub_display_metadata(metrics)
    # 同一批指标顺带做公司技能的更新判定（不取包、不计下载）。
    from tools.llano_skill_update import update_states
    updates = update_states(metrics) if metrics else {}
    for row in rows:
        if row["name"] in to_query:
            metric = metrics.get(to_query[row["name"]], {})
            row.update({key: metric[key] for key in ("download_count", "usage_count") if key in metric})
        if row["name"] in updates:
            row["hub_update"] = updates[row["name"]]
    return rows
