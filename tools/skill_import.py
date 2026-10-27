"""Admission path for a user-downloaded skill bundle (never an arbitrary path).

2026-09-21 起（决策 #59，导入入口从「浏览技能中心」搬到「技能」页）除了文件夹，还收：
单个 SKILL.md、.zip 压缩包；同名时可以「覆盖更新」（先备份旧版）；frontmatter 里的
name 是中文或大写等不合法标识时，自动生成合法标识并把原名存成 display_name，
而不是抛一句英文报错。所有面向同事的错误信息都是中文。

2026-09-23 起（品牌版）：技能要有**中文展示名**（display_name）和**中文简介**
（description），写在 SKILL.md 的 frontmatter。stock Hermes 不管。

2026-10-16 起（负责人定）：缺中文不再弹框让同事手填 —— 同事不会花这个时间。
安全扫描通过后，由 AI 在同一次导入里补齐这两项并写回隔离区的 SKILL.md，再复扫一次
后安装。AI 失败不阻止导入：照常装上，返回 `metadata_incomplete`，列表先显示原文，
同事可在技能页一键重试 AI 补全。调用方显式传入的展示名/简介仍然优先（旧接口兼容）。
`SkillNeedsChineseMeta` 保留给旧调用方解析，新流程不再抛出。
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import shutil
import stat
import time
import unicodedata
import zipfile
from pathlib import Path
from typing import Any, Optional

MAX_FILES = 64
MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024
MAX_ARCHIVE_BYTES = 6 * 1024 * 1024  # 压缩包本身；解压后另按 MAX_TOTAL_BYTES 卡

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_CJK_RE = re.compile(r"[㐀-䶿一-鿿]")
# 压缩包里操作系统顺手塞的垃圾：不是技能内容，也不该算进文件数
_JUNK_NAMES = {"__MACOSX", ".DS_Store", "Thumbs.db"}


class SkillExists(ValueError):
    """同名技能已经装过了。

    单独一个类型，是为了让界面能问「要用这一份覆盖更新吗」，而不是让同事对着一条
    报错发呆。消息前缀是给界面解析的（SKILL_EXISTS::标识::展示名::说明）；后半句
    保留了英文原文，老的调用方与测试仍然按 "already exists" 匹配。
    """

    def __init__(self, name: str, display_name: str = ""):
        self.name = name
        self.display_name = display_name or name
        super().__init__(
            f"SKILL_EXISTS::{name}::{self.display_name}::"
            f"Skill '{name}' already exists（已经有一个叫「{self.display_name}」的技能了）"
        )


class SkillNeedsChineseMeta(ValueError):
    """品牌版导入：缺中文展示名或中文简介，需要同事补上再装。

    消息前缀是给界面解析的：`SKILL_NEEDS_META::<json>::说明`，json 里带着当前的
    标识、展示名和简介，界面用它们预填输入框。
    """

    def __init__(self, name: str, display_name: str, description: str):
        self.name = name
        self.display_name = display_name
        self.description = description
        payload = json.dumps(
            {"name": name, "display_name": display_name, "description": description},
            ensure_ascii=False,
        )
        super().__init__(
            f"SKILL_NEEDS_META::{payload}::"
            "这个技能还没有中文名称或中文简介，请补上后再添加"
        )


# 与 tools/skill_linter.py 的品牌规则对齐：展示名 ≤20 字；简介沿用新建技能的 60 字上限
# （技能索引只显示前 57 个字左右，再长模型也看不到）。
DISPLAY_NAME_MAX_CHARS = 20
DESCRIPTION_MAX_CHARS = 60


def _has_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


def _slug_for(raw: str) -> tuple[str, bool]:
    """把 frontmatter 里的 name 变成合法标识。返回 (标识, 是否改动过)。

    标识（目录名、/name 命令、hub slug）必须是小写 ASCII；同事从别处拿来的技能常常
    写着中文名。中文名不直接拒收，也不指望有拼音库：留下能留的 ASCII 部分，再拼上原名的
    短哈希，保证不同的中文名不会撞到同一个目录。
    """
    raw = (raw or "").strip()
    if raw and len(raw) <= 64 and _SLUG_RE.match(raw):
        return raw, False
    if raw.isascii():
        lowered = re.sub(r"[^a-z0-9._-]+", "-", raw.lower()).strip("-._")
        if lowered and _SLUG_RE.match(lowered) and len(lowered) <= 64:
            return lowered, True
    ascii_part = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii").lower()
    ascii_part = re.sub(r"[^a-z0-9]+", "-", ascii_part).strip("-")
    base = ascii_part[:40] if len(ascii_part) >= 3 else "skill"
    digest = hashlib.sha1((raw or "skill").encode("utf-8")).hexdigest()[:8]
    return f"{base}-{digest}", True


def _frontmatter_span(text: str) -> Optional[tuple[int, int]]:
    """frontmatter 正文（两条 --- 之间）在 text 里的 [start, end) 区间。"""
    body = text.lstrip("﻿")
    offset = len(text) - len(body)
    if not body.startswith("---"):
        return None
    fence = re.search(r"\n---\s*\n", body[3:])
    if not fence:
        return None
    return offset + 3, offset + 3 + fence.start()


def _read_meta(text: str) -> dict[str, Any]:
    import yaml

    span = _frontmatter_span(text)
    if not span:
        return {}
    try:
        meta = yaml.safe_load(text[span[0]:span[1]])
    except yaml.YAMLError:
        return {}
    return meta if isinstance(meta, dict) else {}


def _rewrite_name(text: str, slug: str, original: str, add_display_name: bool) -> str:
    """只改 name 那一行（必要时补一行 display_name），其余字节原样保留。"""
    span = _frontmatter_span(text)
    if not span:
        return text
    block = text[span[0]:span[1]]
    lines = block.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if re.match(r"^name\s*:", line):
            newline = "\r\n" if line.endswith("\r\n") else "\n"
            replaced = [f"name: {slug}{newline}"]
            if add_display_name:
                replaced.append(f"display_name: {json.dumps(original, ensure_ascii=False)}{newline}")
            lines[index:index + 1] = replaced
            break
    else:
        return text
    return text[:span[0]] + "".join(lines) + text[span[1]:]


def _set_frontmatter_key(text: str, key: str, value: str, *, after: str = "name") -> str:
    """把 frontmatter 里顶层的 `key` 换成 `key: "<value>"`（没有就插在 `after` 后面）。

    只动这一个键：原来的值如果是多行（`>` / `|` 块或折行），连同缩进的续行一起换掉；
    其余行、注释、换行符原样保留。值用 JSON 字符串写，JSON 双引号串也是合法的 YAML，
    不用担心冒号、引号、`#` 之类把 YAML 弄坏。
    """
    span = _frontmatter_span(text)
    if not span:
        return text
    lines = text[span[0]:span[1]].splitlines(keepends=True)
    newline = next((("\r\n" if l.endswith("\r\n") else "\n") for l in lines if l.strip()), "\n")
    rendered = f"{key}: {json.dumps(value, ensure_ascii=False)}{newline}"
    key_re = re.compile(rf"^{re.escape(key)}\s*:")
    for index, line in enumerate(lines):
        if key_re.match(line):
            end = index + 1
            while end < len(lines) and (lines[end][:1] in (" ", "\t") or not lines[end].strip()):
                end += 1
            # 续行后面的空行不属于这个键，留着
            while end > index + 1 and not lines[end - 1].strip():
                end -= 1
            lines[index:end] = [rendered]
            break
    else:
        anchor_re = re.compile(rf"^{re.escape(after)}\s*:")
        position = next((i + 1 for i, l in enumerate(lines) if anchor_re.match(l)), None)
        if position is None:
            position = 1 if lines and not lines[0].strip() else 0
        while position < len(lines) and lines[position][:1] in (" ", "\t"):
            position += 1
        lines.insert(position, rendered)
    return text[:span[0]] + "".join(lines) + text[span[1]:]


def _clean_meta_value(value: Optional[str], *, label: str, limit: int) -> Optional[str]:
    """同事在界面上补填的中文名称 / 简介：去掉换行和首尾空白，校验中文与长度。"""
    if value is None:
        return None
    cleaned = " ".join(str(value).split())
    if not cleaned:
        raise ValueError(f"请填写{label}")
    if not _has_cjk(cleaned):
        raise ValueError(f"{label}要用中文写")
    if len(cleaned) > limit:
        raise ValueError(f"{label}最多 {limit} 个字，现在是 {len(cleaned)} 个字")
    return cleaned


def _files_from_archive(archive: str) -> dict[str, str]:
    """把 base64 的 .zip 拆成 {相对路径: base64}，边拆边卡大小、路径和符号链接。"""
    try:
        raw = base64.b64decode(archive, validate=True)
    except Exception as exc:
        raise ValueError("压缩包读取失败，请重新选择文件") from exc
    if len(raw) > MAX_ARCHIVE_BYTES:
        raise ValueError("压缩包太大：一个技能整体不能超过 4 MB")
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise ValueError("这不是有效的 .zip 压缩包") from exc

    entries: list[tuple[zipfile.ZipInfo, list[str]]] = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        parts = [p for p in info.filename.replace("\\", "/").split("/") if p]
        if not parts or any(p in _JUNK_NAMES or p.startswith("._") for p in parts):
            continue
        if stat.S_ISLNK(info.external_attr >> 16):
            raise ValueError(f"压缩包里有快捷方式（符号链接），出于安全不能添加：{info.filename}")
        entries.append((info, parts))
    if not entries:
        raise ValueError("压缩包里没有文件")
    if len(entries) > MAX_FILES:
        raise ValueError(f"一个技能最多 {MAX_FILES} 个文件，这个压缩包里有 {len(entries)} 个")

    # 压缩包经常在外面多包一层目录：SKILL.md 不在根上、但所有文件共享同一个顶层目录时，去掉它
    if not any(len(p) == 1 and p[0].lower() == "skill.md" for _, p in entries):
        tops = {p[0] for _, p in entries}
        if len(tops) == 1 and any(len(p) == 2 and p[1].lower() == "skill.md" for _, p in entries):
            entries = [(i, p[1:]) for i, p in entries]

    out: dict[str, str] = {}
    total = 0
    for info, parts in entries:
        # 不信头里声明的大小：解压炸弹的头可以骗人，所以按实际读到的字节卡
        with zf.open(info) as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise ValueError(f"单个文件不能超过 1 MB：{info.filename}")
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise ValueError("压缩包太大：一个技能整体不能超过 4 MB")
        out["/".join(parts)] = base64.b64encode(data).decode("ascii")
    return out


def _prepare_overwrite(slug: str, existing_path: Path) -> None:
    """覆盖前的把关与备份：内置 / 技能中心装的技能不许覆盖；旧版整份拷进 .archive。"""
    from tools.skill_usage import _read_bundled_manifest_names, _read_hub_installed_names
    from tools.skills_hub import HubLockFile, _skills_dir

    skills_root = _skills_dir().resolve()
    path = Path(existing_path).resolve()
    if not path.is_relative_to(skills_root):
        raise ValueError(f"「{slug}」在外部技能目录里，不能覆盖；请换一个名字")
    imported = {e["name"] for e in HubLockFile().list_installed() if e.get("source") == "local"}
    if slug in _read_bundled_manifest_names() or (slug in _read_hub_installed_names() and slug not in imported):
        raise ValueError(f"「{slug}」是随包内置的技能，或是从技能中心装的，不能被覆盖；请换一个名字")
    archive_root = skills_root / ".archive"
    archive_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(path, archive_root / f"{slug}.replaced-{time.strftime('%Y%m%d-%H%M%S')}")


def import_skill_bundle(
    name: str,
    files: Optional[dict[str, str]] = None,
    *,
    archive: Optional[str] = None,
    overwrite: bool = False,
    display_name: Optional[str] = None,
    description: Optional[str] = None,
) -> dict[str, Any]:
    from tools.skills_hub import (SkillBundle, _validate_skill_name, _validate_bundle_rel_path,
                                  quarantine_bundle, install_from_quarantine)
    from tools.skill_manager_tool import _find_skill, _validate_frontmatter
    from tools.skills_guard import scan_skill, should_allow_install, format_scan_report

    files = dict(files or {})
    if archive:
        files = _files_from_archive(archive)
    if not files or len(files) > MAX_FILES:
        raise ValueError(f"一个技能需要 1 到 {MAX_FILES} 个文件")

    decoded: dict[str, bytes] = {}
    total = 0
    for path, encoded in files.items():
        path = _validate_bundle_rel_path(path)
        if len(encoded) > 1_400_000:
            raise ValueError("单个文件不能超过 1 MB")
        content = base64.b64decode(encoded, validate=True)
        total += len(content)
        if len(content) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            raise ValueError("技能包太大：单个文件不能超过 1 MB，整体不能超过 4 MB")
        if path.casefold() in {p.casefold() for p in decoded}:
            raise ValueError("技能包里有重复的文件路径")
        decoded[path] = content

    # Windows 上常见 Skill.md / skill.md：统一成 SKILL.md（只在没有精确的 SKILL.md 时）
    if "SKILL.md" not in decoded:
        for key in list(decoded):
            if key.lower() == "skill.md":
                decoded["SKILL.md"] = decoded.pop(key)
                break
    if "SKILL.md" not in decoded:
        raise ValueError("技能包里必须有 SKILL.md")

    try:
        text = decoded["SKILL.md"].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("SKILL.md 不是 UTF-8 文本，请另存为 UTF-8 编码后再添加") from exc
    error = _validate_frontmatter(text, new_skill=False)
    if error:
        raise ValueError(error)

    provided_display = _clean_meta_value(display_name, label="中文名称", limit=DISPLAY_NAME_MAX_CHARS)
    provided_description = _clean_meta_value(description, label="中文简介", limit=DESCRIPTION_MAX_CHARS)

    meta = _read_meta(text)
    given_name = name.strip()
    original_name = str(meta.get("name") or "").strip()
    display_name = str(meta.get("display_name") or "").strip()
    if given_name:
        slug = given_name
    else:
        slug, renamed = _slug_for(original_name or str(meta.get("description") or ""))
        if renamed:
            # 名字不是合法标识（中文、大写……）：换成合法标识，原名存成 display_name
            text = _rewrite_name(text, slug, original_name, add_display_name=not display_name and bool(original_name))
            decoded["SKILL.md"] = text.encode("utf-8")
            display_name = display_name or original_name

    from tools.skill_manager_tool import _is_branded_runtime, _validate_name
    error = _validate_name(slug)
    if error:
        raise ValueError(error)
    slug = _validate_skill_name(slug)

    # CUSTOM（2026-09-23）：品牌版要中文展示名 + 中文简介。同事补填的优先，写回 SKILL.md。
    if provided_display is not None or provided_description is not None:
        if provided_display is not None:
            text = _set_frontmatter_key(text, "display_name", provided_display, after="name")
            display_name = provided_display
        if provided_description is not None:
            text = _set_frontmatter_key(text, "description", provided_description, after="display_name")
        error = _validate_frontmatter(text, new_skill=False)
        if error:
            raise ValueError(f"补填的名称或简介写不进 SKILL.md：{error}")
        decoded["SKILL.md"] = text.encode("utf-8")
        meta = _read_meta(text)
    current_description = " ".join(str(meta.get("description") or "").split())
    needs_chinese = _is_branded_runtime() and not (_has_cjk(display_name) and _has_cjk(current_description))
    shown = display_name or original_name or slug

    existing = _find_skill(slug)
    if existing and not overwrite:
        raise SkillExists(slug, shown)

    bundle = SkillBundle(name=slug, files=decoded, source="local", identifier=f"local/{slug}", trust_level="community")
    quarantine = quarantine_bundle(bundle)
    try:
        result = scan_skill(quarantine, source="local-import")
        allowed, reason = should_allow_install(result)
        if allowed is not True:
            return {"success": False, "error": f"安全扫描没有通过：{reason}", "report": format_scan_report(result)}
        metadata_error = ""
        if needs_chinese:
            # 扫描先过，AI 才读内容；补完只改 frontmatter 两项，再复扫一次装的就是实际文件。
            shown, metadata_error = _complete_chinese_metadata(quarantine, shown)
            if not metadata_error:
                bundle.files["SKILL.md"] = (quarantine / "SKILL.md").read_bytes()
                result = scan_skill(quarantine, source="local-import")
                allowed, reason = should_allow_install(result)
                if allowed is not True:
                    return {"success": False, "error": f"安全扫描没有通过：{reason}", "report": format_scan_report(result)}
        replaced = False
        existing = _find_skill(slug)
        if existing:
            if not overwrite:
                raise SkillExists(slug, shown)
            # 扫描过了才动旧版：扫描不过，旧版原样留着，也不留备份垃圾
            _prepare_overwrite(slug, existing["path"])
            replaced = True
        path = install_from_quarantine(quarantine, slug, "", bundle, result)
        from tools.skill_origin import stamp
        stamp(path, write_origin="user")
        out = {
            "success": True,
            "name": slug,
            "display_name": shown,
            "file_count": len(decoded),
            "verdict": result.verdict,
            "replaced": replaced,
        }
        if needs_chinese:
            out["metadata_completed"] = not metadata_error
            if metadata_error:
                out["metadata_incomplete"] = True
                out["metadata_error"] = metadata_error
        return out
    finally:
        shutil.rmtree(quarantine, ignore_errors=True)


def _complete_chinese_metadata(skill_dir: Path, shown: str) -> tuple[str, str]:
    """Return (展示名, 失败原因)。失败不抛：导入照常完成，列表先显示原文。"""
    from tools.skill_chinese_metadata import ensure_skill_metadata

    try:
        ensure_skill_metadata(skill_dir)
    except Exception as exc:  # 模型未授权、超时、返回不合格 —— 都不该挡住导入
        return shown, str(exc).replace("技能尚未上传", "技能已添加，可稍后在技能页重试 AI 补全")
    meta = _read_meta((skill_dir / "SKILL.md").read_text(encoding="utf-8"))
    return str(meta.get("display_name") or shown), ""
