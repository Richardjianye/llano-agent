"""把本地技能发布到公司自有 skill hub —— 阶段 11 · S11-F

产品前提：hub 是给各部门下技能指标用的 —— 每月做多少 skill、总结多少工作经验。
所以「发布」这个动作必须是员工在对话里随口一句就能完成的（"把这个技能发到公司
技能库"），而不是一条要背下来的命令。因此它挂在 `skill_manage` 的 publish 动作上，
跟 create / patch / delete 同一个入口。

## 打包在客户端做，不在网关做

网关只收一份已经拆好的文件清单。让它去拉员工机器上的目录是不可能的（网关根本
够不到），而让客户端上传一个 zip 则意味着网关要解压——解压是一整类路径穿越
（zip slip）的来源，而我们已经有一套逐文件校验的协议了。

## 二进制走 base64，不丢

tools/skill_report.py 的样本收集**直接丢掉**二进制，那是对的——它是取证通道，
审查的人要读代码。这里是**分发**通道：丢掉一个 png，装出来的技能是坏的，
而且坏得很安静。两条通道对同一个问题给出相反的答案，是因为它们的用途相反。

## 跳过什么

`.git/`、`__pycache__/`、`.DS_Store` 这些是本地噪音，发出去只会让每个装它的人
多一份垃圾；`node_modules/` 更是能把一个 20KB 的技能撑到几十兆直接顶穿上限。
**跳过的东西会在返回里列出来**——静默跳过的话，发布者不会知道他以为发出去的
那个文件其实没发。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from tools.llano_skill_hub import (API_PREFIX, MAX_FILE_BYTES, MAX_FILES,
                                   MAX_TOTAL_BYTES, SkillHubUnavailable,
                                   _request, hub_origin)

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# 目录级跳过。node_modules 单独列出来不是洁癖：一个装了依赖的技能目录能有几十兆，
# 不跳的话发布必定顶穿上限，而报出来的错是「超过 4MB」，跟真正的原因隔了一层。
SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", "node_modules", ".venv", "venv", ".idea", ".vscode",
})
# 文件级跳过。
# .llano-origin.json 是来源标记（P0-4）。发到 hub 上只是噪音，而且会把
# 「这个技能产生于哪个项目」带给全公司 —— 它是给离职取舍用的，不是给全员看的。
SKIP_FILES = frozenset({".DS_Store", "Thumbs.db", ".gitignore", ".llano-origin.json"})
SKIP_SUFFIXES = frozenset({".pyc", ".pyo", ".swp", ".log"})

TEXT_ENCODING = "utf-8"
BINARY_ENCODING = "base64"


def _is_text(data: bytes) -> bool:
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _should_skip(rel: Path) -> Optional[str]:
    if any(part in SKIP_DIRS for part in rel.parts[:-1]) or rel.parts[0] in SKIP_DIRS:
        return "开发目录"
    if rel.name in SKIP_FILES:
        return "本地噪音文件"
    if rel.suffix in SKIP_SUFFIXES:
        return "构建产物"
    return None


def pack_skill(skill_dir: Path) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """把一个技能目录收成 wire protocol 的 files 数组。

    返回 (files, skipped)。skipped 里每条带原因，调用方要把它显示给发布者 ——
    静默跳过等于让他以为发出去的东西比实际多。

    排序保证同一份内容每次收出来一样，网关侧的内容指纹才对得上（否则每次发布
    都因为文件顺序不同而算出不同的指纹……其实不会，网关会重排，但让两边一致
    能省掉一整类「为什么本地算的 hash 和网关不一样」的排查）。
    """
    skill_dir = Path(skill_dir)
    files: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []
    total = 0

    for path in sorted(p for p in skill_dir.rglob("*") if p.is_file()):
        rel = path.relative_to(skill_dir)
        rel_posix = rel.as_posix()

        reason = _should_skip(rel)
        if reason:
            skipped.append({"path": rel_posix, "reason": reason})
            continue

        if len(files) >= MAX_FILES:
            skipped.append({"path": rel_posix, "reason": f"文件数已达上限 {MAX_FILES}"})
            continue

        try:
            raw = path.read_bytes()
        except OSError as exc:
            skipped.append({"path": rel_posix, "reason": f"读不了：{exc}"})
            continue

        if len(raw) > MAX_FILE_BYTES:
            skipped.append({
                "path": rel_posix,
                "reason": f"{len(raw)} 字节，超过单文件上限 {MAX_FILE_BYTES}",
            })
            continue

        if total + len(raw) > MAX_TOTAL_BYTES:
            skipped.append({"path": rel_posix, "reason": f"技能包已达总量上限 {MAX_TOTAL_BYTES}"})
            continue

        total += len(raw)
        if _is_text(raw):
            files.append({
                "path": rel_posix,
                "encoding": TEXT_ENCODING,
                "content": raw.decode("utf-8"),
            })
        else:
            files.append({
                "path": rel_posix,
                "encoding": BINARY_ENCODING,
                "content": base64.b64encode(raw).decode("ascii"),
            })

    return files, skipped


def _read_frontmatter(skill_md: Path) -> Dict[str, str]:
    """从 SKILL.md 的 YAML frontmatter 里取 name / description。

    复用技能索引的 YAML 解析器，支持多行说明，只取字符串元数据。
    取不到时返回空，由发布前 AI 补齐与网关校验处理。
    """
    try:
        text = skill_md.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return {}
    from agent.skill_utils import parse_frontmatter

    meta, _ = parse_frontmatter(text)
    return {key: value.strip() for key, value in meta.items()
            if key in {"name", "description", "display_name"} and isinstance(value, str)}


def validate_publish_metadata(name: Any, description: Any) -> None:
    """上传前提示补填；网关仍独立校验，直接 API 无法绕过。"""
    for value, field, label, limit in (
        (name, "display_name", "中文标题", 255),
        (description, "description", "中文描述", 1024),
    ):
        if not isinstance(value, str) or not re.search(r"[\u3400-\u9fff\U00020000-\U000323af]", value):
            raise SkillHubUnavailable(f"请在 SKILL.md 中补填{label}（{field}），保存后重新发布")
        if len(value.strip()) > limit:
            raise SkillHubUnavailable(f"{label}最多 {limit} 个字符，请缩短后重新发布")


def build_payload(
    skill_dir: Path,
    *,
    slug: str,
    changelog: str = "",
) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
    """组装发布载荷。返回 (payload, skipped)。"""
    files, skipped = pack_skill(skill_dir)
    if not any(f["path"].lower() == "skill.md" for f in files):
        # 本地就拦一道。等网关回 400 的话，员工已经等着上传完了，
        # 而这个错误在打包那一刻就完全可知。
        raise SkillHubUnavailable(
            f"{slug} 目录里没有 SKILL.md，这不是一个能发布的技能"
        )

    meta = _read_frontmatter(skill_dir / "SKILL.md")
    name = meta.get("display_name") or meta.get("name") or slug
    description = meta.get("description", "")
    return (
        {
            "schema_version": SCHEMA_VERSION,
            "slug": slug,
            # CUSTOM: hub 的 name 是展示名（slug 才是标识），优先用中文 display_name，
            # 这样公司技能库里同事看到的就是中文名；安装走 slug，不受影响。
            "name": name,
            "description": description,
            "changelog": changelog or "",
            "files": files,
        },
        skipped,
    )


def publish_bundle(payload: Dict[str, Any]) -> Dict[str, Any]:
    """扫描实际发布包，命中只提醒并留样，随后发布到网关。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    import tempfile
    from tools.skills_guard import scan_skill
    from tools.llano_skill_hub import decode_files
    from tools.skills_hub import _validate_bundle_rel_path
    with tempfile.TemporaryDirectory(prefix="skill-publish-") as directory:
        try:
            root = Path(directory)
            for name, content in decode_files(payload["files"]).items():
                target = root / _validate_bundle_rel_path(name)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
            result = scan_skill(root, source="agent-created")
        except SkillHubUnavailable:
            raise
        except Exception as exc:
            raise SkillHubUnavailable(f"技能扫描失败，未上传：{exc}") from exc

        result.skill_name = str(payload.get("slug") or result.skill_name)
        response = _request(API_PREFIX, method="POST", body=body)
        if result.findings:
            _report_publish_warning(result, root, response)
        return response


def _report_publish_warning(scan_result: Any, root: Path, response: Dict[str, Any]) -> None:
    """上报失败不改变已完成的发布；队列会继续重试。"""
    from tools import skill_report

    response["scan_warning"] = {
        "verdict": scan_result.verdict,
        "finding_count": len(scan_result.findings),
        "report_status": "unavailable",
    }
    try:
        event = skill_report.build_publish_warning_event(
            scan_result, root,
            skill_name=scan_result.skill_name,
            bundle_hash=str(response.get("bundle_hash") or ""),
        )
        queued = skill_report.enqueue(event)
        if queued is not None:
            response["scan_warning"]["report_status"] = "queued"
            skill_report.flush_in_background()
    except Exception:
        logger.exception("技能已发布，但扫描提醒上报失败：%s", scan_result.skill_name)

# 网关返回的 scope。用字面量而不是 import 服务端常量 —— 客户端够不到那份代码。
SCOPE_DEPT = "dept"
SCOPE_COMPANY = "company"


def _visibility(scope: Optional[str]) -> str:
    """把网关返回的 scope 翻成一句给员工看的话。

    发布落在**本部门**的技能空间（阶段 21），不是全公司 —— 之前这里写死了
    「全公司可见」，那在分层之后就是一句假话。员工照着它去跟别的部门说
    「我发了个技能你搜一下」，对方搜不到，而两边都不知道为什么。

    已经被主管晋级到公司空间的技能，追加新版本仍然停在公司空间（晋级是技能级
    的，不是版本级的），所以这里要按网关返回的实际 scope 说，不能一律说部门。

    scope 缺失说明网关是阶段 21 之前的版本。那时候**不编**范围：说错方向的
    代价比少说一句大得多。
    """
    if scope == SCOPE_COMPANY:
        return "已在公司技能空间，全公司可见"
    if scope == SCOPE_DEPT:
        return "已发到本部门技能空间；本部门及下级部门同事、经理及以上、管理部门和技术部门可查看；要全员可见，请主管晋级到公司空间"
    return "已进入公司技能库"


def published_receipt(local_name: str) -> Optional[Dict[str, Any]]:
    """本机技能对应的发布回执（含实际发布用的 slug）；没发布过返回 None。

    回执按发布 slug 存放。2026-10-16 起「整理并发布」允许换一个 slug（原 slug 被同事占用时），
    所以先按本机名推出的 slug 找，找不到再按回执里记的 local_name / 目录名匹配。
    """
    from hermes_constants import get_hermes_home
    directory = Path(get_hermes_home()) / "skills" / ".hub-published"
    guess = directory / (local_name.strip().lower().replace("_", "-") + ".json")
    candidates = [guess] + sorted(p for p in directory.glob("*.json") if p != guess) if directory.is_dir() else []
    for path in candidates:
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(receipt, dict):
            continue
        owner = receipt.get("local_name") or Path(str(receipt.get("path") or "")).name
        if path == guess or owner == local_name:
            receipt.setdefault("slug", path.stem)
            return receipt
    return None


def refresh_receipt_hash(slug: str, skill_dir: Path) -> None:
    """本机技能内容换过之后（例如用整理版替换了本机原版），把回执的指纹对齐到现在的内容。"""
    from hermes_constants import get_hermes_home
    from tools.skills_guard import content_hash
    receipt = Path(get_hermes_home()) / "skills" / ".hub-published" / (slug + ".json")
    try:
        data = json.loads(receipt.read_text(encoding="utf-8"))
        data["content_hash"] = content_hash(skill_dir)
        receipt.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except (OSError, ValueError):
        logger.debug("发布回执未更新：%s", slug)


def publish_skill_action(name: str, *, changelog: str = "", source_dir: Optional[Path] = None,
                         slug: Optional[str] = None) -> Dict[str, Any]:
    """`skill_manage(action="publish")` 的落点。返回 skill_manage 那套结果字典。

    发布是用户主动按下的动作，所以这里**不吞异常** —— 与 LlanoHubSource 那几个
    读路径相反。搜索时静默让路是对的（一次抖动不该让搜索报错），发布时静默失败
    是灾难：员工以为自己交了这个月的技能指标，hub 上什么都没有。
    """
    from tools.skill_manager_tool import _find_skill

    found = _find_skill(name)
    if not found:
        return {"success": False, "error": f"本地没有叫 {name} 的技能"}

    skill_dir = Path(found["path"]).parent if Path(found["path"]).is_file() else Path(found["path"])
    # CUSTOM（2026-10-16）：「整理并发布」从临时草稿目录打包，必要时换一个 slug；回执仍记本机技能。
    slug = (slug or name).strip().lower().replace("_", "-")

    try:
        payload, skipped = build_payload(source_dir or skill_dir, slug=slug, changelog=changelog)
        result = publish_bundle(payload)
    except (SkillHubUnavailable, ValueError, OSError) as exc:
        return {"success": False, "error": f"发布失败：{exc}"}

    created = bool(result.get("created"))
    # 调用同步只识别真正发布过的本机技能，不发送私人技能名称。
    from hermes_constants import get_hermes_home
    from hermes_cli.product_policy import load_product_policy
    policy = load_product_policy()
    if policy and policy.sync_skill_usage:
        from tools.skills_guard import content_hash
        receipt = Path(get_hermes_home()) / "skills" / ".hub-published" / (slug + ".json")
        try:
            receipt.parent.mkdir(parents=True, exist_ok=True)
            receipt.write_text(json.dumps({"slug": result.get("slug", slug), "local_name": skill_dir.name,
                "path": str(skill_dir.resolve()), "name": result.get("name"),
                "description": result.get("description"), "metadata_source": "llano-hub",
                "bundle_hash": result.get("bundle_hash"),
                "metadata_schema_version": 1,
                "content_hash": content_hash(skill_dir)}, ensure_ascii=False), encoding="utf-8")
        except OSError:
            logger.debug("技能已发布，调用同步回执未能保存")
    version = result.get("version")
    display_name = result.get("name") or payload["name"]
    out: Dict[str, Any] = {
        "success": True,
        "published": created,
        "slug": result.get("slug", slug),
        "version": version,
        "scope": result.get("scope"),
        "file_count": len(payload["files"]),
        # 「内容没变」必须说成「内容没变」，不能说成「发布成功」。
        # 员工按了发布却什么都没发生，他有权知道是哪一种。
        "message": (
            f"已发布「{display_name}」v{version}，{_visibility(result.get('scope'))}"
            if created
            else f"「{display_name}」的内容与已发布的 v{version} 完全相同，没有新增版本"
        ),
    }
    if result.get("metadata_repaired"):
        out["metadata_repaired"] = True
        out["message"] = "AI 已补齐中文标题和介绍；" + out["message"]
    if skipped:
        # 静默跳过的话，发布者不会知道他以为发出去的那个文件其实没发。
        out["skipped"] = skipped
        out["message"] += f"（跳过 {len(skipped)} 个文件，见 skipped）"
    warning = result.get("scan_warning")
    if warning:
        out["scan_warning"] = warning
        out["message"] += f"；发布前扫描命中 {warning['finding_count']} 项（{warning['verdict']}）"
        if warning.get("report_status") == "queued":
            out["message"] += "，样本已在本机排队上报，供管理员事后核查"
        else:
            out["message"] += "，样本未能入队，请联系管理员检查本机上报配置"
    return out
