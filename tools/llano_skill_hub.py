"""公司自有 skill hub 的来源适配器 —— 阶段 11 · S11-E

这是 `PRODUCT_SKILL_SOURCE_IDS` 那个预留闸门等的东西。阶段 6 定下接口时刻意
没编 wire protocol（"替一个还不存在的 hub 编一套协议，写出来就是待返工的"），
协议现在定了，见 audit-gateway/server/services/skill_hub.py 的模块说明。

## 为什么是**新文件**而不是往 skills_hub.py 里加一个类

月度 upstream 同步的冲突量正比于改动的上游代码行数。上游 `create_source_router`
那边只加一行（把这个类塞进 sources 列表），实现全部在自己的文件里 —— 上游改
skills_hub.py 的任何一处都不会撞到这 200 行。

## trust_level 是 `trusted`，不是 `community`

看 tools/skills_guard.INSTALL_POLICY 那张表：

    trusted:    safe=allow  caution=allow  dangerous=block（--force 也越不过）
    community:  safe=allow  caution=block  dangerous=block

标 community 的话，员工自己写的技能会天天被 caution 拦下来 —— 而技能里出现网络
调用、写文件本来就是正常的，这类模式恰恰是 caution 的主要来源。**被误报干掉一个
技能，赔进去的是员工这个月的技能指标**（S7-B 那条判断的同一个道理）。

标 trusted 不是放松：dangerous 照样硬拦，而且 `--force` 明确越不过 trusted 的
dangerous。放开的只有 caution 这一档，而这一档本来就该由人看一眼再决定。

## 所有方法都**不抛异常**

网关连不上、还没登录、hub 上没这个技能 —— 一律返回空/None，让下游来源接着找。
这是上游 `HermesIndexSource` 已经立的规矩（"When the index is unavailable, all
methods return empty / None so downstream sources take over transparently"）。
一次网络抖动不该让 `hermes skills search` 整个报错。
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from tools.skills_hub import SkillBundle, SkillMeta, SkillSource

logger = logging.getLogger(__name__)

SOURCE_ID = "llano-hub"
TRUST_LEVEL = "trusted"

API_PREFIX = "/v1/skill-hub/skills"
TIMEOUT_SECONDS = 20

# 与网关侧 services/skill_hub 的上限对齐。客户端也拦一道，是为了在**发出去之前**
# 就把「这个包太大」说清楚：等网关回 400 的话，员工已经等着上传完了。
MAX_FILES = 64
MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024

TEXT_ENCODING = "utf-8"
BINARY_ENCODING = "base64"


class SkillHubUnavailable(RuntimeError):
    """hub 这条路走不通（没配、没登录、连不上）。

    只有**发布**路径会把它抛给用户看 —— 发布是用户主动按下的动作，失败必须说
    清楚为什么。search / fetch 那几条会把它吞掉，见模块说明。

    `status` 是网关的 HTTP 状态码，连不上时为 None。群聊轮询靠它区分「没资格了」
    （404/403，停止轮询）和「网络抖了」（重试）——两者都表现为读失败，但处理
    完全相反。
    """

    status: Optional[int] = None


def hub_origin() -> str:
    """hub 的地址。没单独配就用网关地址。

    `skill_hub_url` 是阶段 6 预留的字段，留给「hub 哪天独立部署」那种情况。
    在那之前它和网关是同一个服务，逼着运维再填一遍同样的地址只会多一个能填错
    的地方 —— 填错的表现还是「搜不到公司技能」，无人报错。
    """
    from hermes_cli.product_policy import (
        ProductPolicyState,
        load_product_policy_status,
        product_skill_hub_url,
    )

    explicit = (product_skill_hub_url() or "").strip().rstrip("/")
    if explicit:
        return explicit

    status = load_product_policy_status()
    if status.state is ProductPolicyState.INVALID:
        raise SkillHubUnavailable("公司托管配置当前不可读取，技能中心已安全停用")
    policy = status.policy
    return (policy.gateway_origin if policy else "").strip().rstrip("/")


def _api_key() -> str:
    # 与 tools/skill_report.py 同一把：桌面端登录后注入到 sidecar 的环境里。
    return (os.environ.get("LLANO_GATEWAY_API_KEY") or "").strip()


def _request(path: str, *, method: str = "GET", body: Optional[bytes] = None) -> Any:
    origin = hub_origin()
    if not origin:
        raise SkillHubUnavailable("品牌配置里没有网关地址，hub 不可用")

    api_key = _api_key()
    if not api_key:
        raise SkillHubUnavailable("还没登录（或会话已过期），hub 不可用")

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"

    request = Request(f"{origin}{path}", data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310 — 托管网关地址
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        # 网关对机器调用方回的是真 4xx，信封里带 {code, message}。
        # 把 message 捞出来 —— "发布失败：slug 只能用小写字母…" 比 "HTTP 400" 有用得多。
        detail = ""
        try:
            detail = str(json.loads(exc.read().decode("utf-8")).get("message") or "")
        except Exception:  # noqa: BLE001 — 错误响应体本身坏掉不该盖住原始错误
            pass
        failure = SkillHubUnavailable(detail or f"网关返回 HTTP {exc.code}")
        # 状态码留给调用方分辨**失权与抖动**（阶段 27-B）：项目被归档、成员被移除
        # 时网关回 404/403，那是轮询的终止条件，重试没有意义；连不上才该重试。
        # 只分辨不了的时候才当抖动 —— 见下面那一支，status 为 None。
        failure.status = exc.code
        raise failure from exc
    except (URLError, OSError, ValueError) as exc:
        failure = SkillHubUnavailable(f"连不上网关：{exc}")
        failure.status = None
        raise failure from exc


_identity_cache: Dict[Tuple[str, str], Tuple[float, Optional[str]]] = {}
_IDENTITY_RETRY_SECONDS = 60.0


def current_uid() -> Optional[str]:
    """当前登录账号在网关上的 uid，用来标出「我发布的」技能。

    走已有的 usage-identity 接口（网关从凭据解出来，不信客户端）；按网关地址 + 凭据
    缓存，换账号自然失效。失败返回 None —— 只是少一个标记，不能拖垮技能中心，
    失败结果也缓存一分钟，免得每个技能卡片都去撞一次网关。
    """
    try:
        key = (hub_origin(), _api_key())
    except SkillHubUnavailable:
        return None
    if not all(key):
        return None
    cached = _identity_cache.get(key)
    if cached and (cached[1] is not None or time.monotonic() - cached[0] < _IDENTITY_RETRY_SECONDS):
        return cached[1]
    uid: Optional[str] = None
    try:
        payload = _request("/v1/skill-hub/usage-identity")
        value = payload.get("uid") if isinstance(payload, dict) else None
        uid = value if isinstance(value, str) and value else None
    except Exception as exc:  # noqa: BLE001 — 标记是锦上添花，任何失败都不该挡住列表
        logger.debug("llano-hub: 读取当前账号失败（%s）", exc)
    _identity_cache[key] = (time.monotonic(), uid)
    return uid


def my_skills() -> List[Dict[str, Any]]:
    """作者自己首发的技能（含已下架）。发布与否是作者主动来看的，失败要报出来。"""
    payload = _request("/v1/skill-hub/mine")
    skills = payload.get("skills") if isinstance(payload, dict) else None
    return [s for s in skills if isinstance(s, dict)] if isinstance(skills, list) else []


COMPANY_PICK_GROUP = "公司精选"
UNGROUPED = "未分组"


def _read_hub_metadata_cache(home):
    try:
        data = json.loads((home / "skills" / ".hub-display-metadata.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _hub_content_matches(path, baseline):
    from tools.skills_guard import content_hash
    try:
        return bool(baseline) and path.is_dir() and content_hash(path) == baseline
    except OSError:
        return False


def hub_display_metadata() -> Dict[str, Dict[str, Any]]:
    """Read server-owned display values once per list, keyed by exact directory.

    No LLM, no network, no changes to skill bodies. Old locks already carry
    the server display_name/description even before the provenance marker.
    """
    from pathlib import Path
    from hermes_constants import get_hermes_home
    from tools.skills_hub import HubLockFile
    from tools.skills_guard import content_hash
    from tools.skill_chinese_metadata import has_chinese
    home = Path(get_hermes_home())
    out = {}
    def accept(path, name, description):
        if isinstance(name, str) and isinstance(description, str) and all(has_chinese(value) for value in (name, description)):
            out[str(Path(path).resolve())] = {"name": name, "description": description,
                "metadata_source": "llano-hub", "metadata_schema_version": 1}
    cached = _read_hub_metadata_cache(home)
    for entry in HubLockFile().list_installed():
        if entry.get("source") != "llano-hub":
            continue
        meta = entry.get("metadata") or {}
        relative = entry.get("install_path")
        if not isinstance(relative, str) or not relative:
            continue
        target = (home / "skills" / relative).resolve()
        if not target.is_relative_to((home / "skills").resolve()):
            continue
        if _hub_content_matches(target, entry.get("content_hash")):
            fresh = cached.get(str(target), {})
            if not isinstance(fresh, dict):
                fresh = {}
            if fresh.get("content_hash") == entry["content_hash"] and fresh.get("bundle_hash") == meta.get("bundle_hash"):
                meta = {**meta, **fresh}
            accept(target, meta.get("display_name"), meta.get("description"))
    for receipt in (home / "skills" / ".hub-published").glob("*.json"):
        try:
            meta = json.loads(receipt.read_text(encoding="utf-8"))
            target = Path(meta.get("path") or "").resolve()
            if (meta.get("metadata_source") == "llano-hub" and target.is_relative_to((home / "skills").resolve())
                    and _hub_content_matches(target, meta.get("content_hash"))):
                accept(target, meta.get("name"), meta.get("description"))
        except (ValueError, TypeError, OSError):
            continue
    return out


def refresh_hub_display_metadata(metrics):
    """Reuse the existing batch metrics response to repair old download locks.

    Both server bundle and local content baselines must still match. This only
    refreshes display metadata; bodies, versions and install hashes stay intact.
    """
    from pathlib import Path
    from hermes_constants import get_hermes_home
    from tools.skills_hub import HubLockFile
    from tools.skills_guard import content_hash
    from tools.skill_chinese_metadata import has_chinese
    home = Path(get_hermes_home());lock = HubLockFile();data = lock.load();cache = _read_hub_metadata_cache(home);changed = False
    for entry in data.get("installed", {}).values():
        if entry.get("source") != "llano-hub":
            continue
        slug = str(entry.get("identifier") or "").removeprefix("llano-hub/")
        current = entry.get("metadata") or {}
        server = (metrics.get(slug) or {}).get("display_metadata") or {}
        relative = entry.get("install_path")
        if not isinstance(relative, str) or not relative:
            continue
        target = (home / "skills" / relative).resolve()
        if (not target.is_relative_to((home / "skills").resolve()) or not target.is_dir()
                or not _hub_content_matches(target, entry.get("content_hash"))
                or not server.get("bundle_hash") or server["bundle_hash"] != current.get("bundle_hash")
                or server.get("metadata_source") != "llano-hub"
                or not has_chinese(server.get("display_name")) or not has_chinese(server.get("description"))):
            continue
        updated = {**server, "content_hash": entry["content_hash"]}
        if cache.get(str(target)) != updated:
            cache[str(target)] = updated;changed = True
    if changed:
        try:
            import tempfile
            destination = home / "skills" / ".hub-display-metadata.json"
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent, delete=False) as stream:
                stream.write(json.dumps(cache, ensure_ascii=False, sort_keys=True)); temporary = stream.name
            os.replace(temporary, destination)
        except (OSError, ImportError):
            logger.warning("llano-hub: 展示元数据缓存暂未保存，下次列表可重试")


def _group_of(entry: Dict[str, Any]) -> str:
    """这个技能在界面上归到哪一组。

    技能库放开可见性之后（2026-09-03），一个人看到的是全公司的技能 —— 几百条
    平铺出来跟看不到没什么区别。分组是唯一让它可读的东西，所以组名要在这里就
    定好，而不是让每个界面自己去拼：桌面端、后台、CLI 三处拼出三种叫法的话，
    同一个技能在三个地方属于三个组。

    没有部门名时退到部门 code，再退到「未分组」——**不编一个像样的假名字**，
    否则分不清是数据没配还是本来就没归属。
    """
    if str(entry.get("scope") or "") == "company":
        return COMPANY_PICK_GROUP
    name = str(entry.get("scope_dept_name") or "").strip()
    if name:
        return name
    code = str(entry.get("owner_dept_code") or "").strip()
    return code or UNGROUPED


def _to_meta(entry: Dict[str, Any]) -> SkillMeta:
    slug = str(entry.get("slug") or "")
    owner = str(entry.get("owner_username") or "")
    dept = str(entry.get("owner_dept_code") or "")
    return SkillMeta(
        name=str(entry.get("name") or slug),
        description=str(entry.get("description") or ""),
        source=SOURCE_ID,
        identifier=f"llano-hub/{slug}",
        trust_level=TRUST_LEVEL,
        tags=[t for t in ("公司内部", dept) if t],
        extra={
            # provider 这个键是上游 `--source <provider>` 过滤看的（见
            # skills_hub._filter_results_by_provider）。填上之后
            # `hermes skills search --source LLANO Agent` 能直接筛出公司自己的技能。
            "provider": "LLANO Agent",
            "metadata_source": entry.get("metadata_source"),
            "metadata_schema_version": entry.get("metadata_schema_version"),
            "version": entry.get("version"),
            "owner": owner,
            "dept": dept,
            # 界面分组用的组名。与 dept 分开：dept 是发布者当时的部门 code（指标口径），
            # group 是「这个技能现在展示在哪一组」，晋级之后两者会分叉。
            "group": _group_of(entry),
            "install_count": entry.get("install_count"),
            "usage_count": entry.get("usage_count"),
            "updated_at": entry.get("updated_at"),
            # 技能中心每行显示「谁、什么时候发布的」（2026-09-24）。时间取最新版本的发布时间，
            # 老网关不回这个字段时为空，界面就不显示那半句。
            "published_at": entry.get("published_at") or "",
            # 「我发布的」标记：首发人就是当前账号。网关已带 owner_uid，不必改接口。
            "mine": bool(entry.get("owner_uid")) and entry.get("owner_uid") == current_uid(),
        },
    )


def decode_files(files: List[Dict[str, Any]]) -> Dict[str, Any]:
    """把 wire 上的编码信封还原成 SkillBundle.files 要的 {路径: 内容}。

    utf-8 出 str、base64 出 bytes —— 正是 `SkillBundle.files` 声明的
    `Dict[str, Union[str, bytes]]`，下游安装代码按类型分别写盘。
    """
    out: Dict[str, Any] = {}
    for entry in files:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path") or "").strip()
        if not path:
            continue
        content = entry.get("content")
        if not isinstance(content, str):
            continue
        if (entry.get("encoding") or TEXT_ENCODING) == BINARY_ENCODING:
            try:
                out[path] = base64.b64decode(content, validate=True)
            except (binascii.Error, ValueError):
                # 单个文件解不开就跳过它，而不是丢掉整个技能包。
                # 缺一个 asset 装出来的技能可能还能用；一个都装不上肯定不能用。
                logger.warning("llano-hub: %s 的 base64 解不开，已跳过这个文件", path)
                continue
        else:
            out[path] = content
    return out


class LlanoHubSource(SkillSource):
    """公司自有技能库。"""

    def source_id(self) -> str:
        return SOURCE_ID

    def trust_level_for(self, identifier: str) -> str:
        return TRUST_LEVEL

    def search(self, query: str, limit: int = 10) -> List[SkillMeta]:
        try:
            params = urlencode({"q": query or "", "limit": max(1, min(int(limit or 10), 100))})
            payload = _request(f"{API_PREFIX}?{params}")
        except SkillHubUnavailable as exc:
            logger.debug("llano-hub: 搜索跳过（%s）", exc)
            return []

        skills = payload.get("skills") if isinstance(payload, dict) else None
        if not isinstance(skills, list):
            return []
        return [_to_meta(s) for s in skills if isinstance(s, dict)]

    def inspect(self, identifier: str) -> Optional[SkillMeta]:
        identifier = identifier.removeprefix("llano-hub/")
        try:
            payload = _request(f"{API_PREFIX}/{quote(str(identifier), safe='')}")
        except SkillHubUnavailable as exc:
            logger.debug("llano-hub: inspect %s 跳过（%s）", identifier, exc)
            return None
        return _to_meta(payload) if isinstance(payload, dict) else None

    def fetch(self, identifier: str) -> Optional[SkillBundle]:
        identifier = identifier.removeprefix("llano-hub/")
        try:
            payload = _request(f"{API_PREFIX}/{quote(str(identifier), safe='')}/bundle")
        except SkillHubUnavailable as exc:
            logger.debug("llano-hub: fetch %s 跳过（%s）", identifier, exc)
            return None
        return bundle_from_payload(payload, identifier)


def fetch_for_update(slug: str) -> Optional[SkillBundle]:
    """把已装的公司技能换成新版本时取包：带 purpose=update，网关不计下载量。

    与 `LlanoHubSource.fetch` 相反，失败要抛出 —— 这是员工点下的「更新」，得说清为什么没成。
    """
    payload = _request(f"{API_PREFIX}/{quote(str(slug), safe='')}/bundle?purpose=update")
    return bundle_from_payload(payload, slug)


def bundle_from_payload(payload: Any, identifier: str) -> Optional[SkillBundle]:
    if not isinstance(payload, dict):
        return None
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        return None

    decoded = decode_files(files)
    if not decoded:
        return None

    return SkillBundle(
        name=str(payload.get("slug") or identifier),
        files=decoded,
        source=SOURCE_ID,
        identifier=f"llano-hub/{identifier}",
        trust_level=TRUST_LEVEL,
        metadata={
            "display_name": payload.get("name"),
            "metadata_source": payload.get("metadata_source"),
            "metadata_schema_version": payload.get("metadata_schema_version"),
            "description": payload.get("description"),
            "version": payload.get("version"),
            "bundle_hash": payload.get("bundle_hash"),
            "owner": payload.get("owner_username"),
            "dept": payload.get("owner_dept_code"),
            "changelog": payload.get("changelog"),
            # source_url_for_bundle 优先读这个键，做的是「这份东西哪来的」的
            # 溯源展示。指到 hub 上这个技能自己的地址。
            "source_url": f"{hub_origin()}{API_PREFIX}/{quote(str(identifier), safe='')}",
        },
    )
