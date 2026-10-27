"""Structural + convention linter for SKILL.md files.

The hard *validator* in ``tools/skill_manager_tool.py::_validate_frontmatter``
guards the non-negotiables (fence present, YAML mapping, ``name`` +
``description`` present, description length, non-empty body, size cap) and is a
create/edit BLOCKER. This module is the softer, broader companion: it encodes
the "Skill authoring standards (HARDLINE)" conventions from ``CONTRIBUTING.md``
that today are only caught by a human reviewer — shell-utility references
instead of native tools, a missing author/license/metadata block, a
``name`` that doesn't match its directory, dangling ``references/`` links,
marketing words in the description, ``platforms:`` gating vs POSIX-only
primitives, and forbidden scaffolding files.

Design contract (matches the Hermes "no lazy-reading escape hatches / don't
destroy the feature" posture):

* Findings are **advisory** by default. ``lint_skill`` returns a list of
  :class:`LintFinding`; the caller decides whether any severity blocks. The
  create-path surfaces them as guidance, never as a hard reject (the hard
  rejects already live in the validator).
* Pure functions, no I/O beyond reading the files it is pointed at, so CI, the
  ``skill_manage`` create path, and a contributor running it locally all share
  one implementation.
* Reuses ``agent.skill_utils`` helpers rather than re-parsing frontmatter, so
  BOM handling / platform matching / the 60-char prompt budget stay in one
  place.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.skill_utils import (
    SKILL_PROMPT_DESC_LIMIT,
    parse_frontmatter,
)

# ── Rule data ────────────────────────────────────────────────────────────────

# Shell utilities the agent already has wrapped as first-class tools. Naming
# them in SKILL.md prose steers the model to a raw shell call instead of the
# native tool. Maps the banned token -> the native tool the prose should name.
# (CONTRIBUTING.md "Skill authoring standards" rule 2.)
_SHELL_UTIL_TO_TOOL: Dict[str, str] = {
    "grep": "search_files",
    "rg": "search_files",
    "cat": "read_file",
    "head": "read_file",
    "tail": "read_file",
    "sed": "patch",
    "awk": "patch",
    "find": "search_files (target='files')",
    "ls": "search_files (target='files')",
}

# Marketing words the description must not contain (rule 1).
_MARKETING_WORDS = (
    "powerful",
    "comprehensive",
    "seamless",
    "advanced",
    "cutting-edge",
    "state-of-the-art",
    "revolutionary",
    "robust",
)

# POSIX-only primitives that, if a bundled script uses them, require the skill
# to declare ``platforms:`` (rule 3). Detected in scripts/, not in prose.
_POSIX_PRIMITIVES = (
    "fcntl",
    "termios",
    "os.setsid",  # windows-footgun: ok  (search-pattern string, not a call)
    "signal.SIGKILL",  # windows-footgun: ok  (search-pattern string, not a call)
    "osascript",
    "/proc/",
    "apt-get",
    "systemctl",
)

# Scaffolding files a skill should not ship (skill-creator anti-pattern; keeps
# skills dense). These are noise, not skill content.
_FORBIDDEN_FILES = (
    "README.md",
    "CHANGELOG.md",
    "install.sh",
    ".env",
    ".env.example",
    ".gitignore",
)

# Recommended modern section order (rule 5). We check presence of the load
# bearing ones, not exact ordering, to avoid being a change-detector.
_EXPECTED_SECTIONS = ("When to Use", "When to use")

# CUSTOM-BEGIN: 品牌版技能展示名规则（2026-09-21，负责人定 display_name 方案，见决策 #59）
#
# 同事看的是中文界面：技能列表里应该显示中文名和中文说明，而不是
# `taobao-search-scraping / Scrape Taobao search pages...` —— 生产 hub 里唯一那条技能
# 就是因为展示名是英文被下架的。`name` 仍是 ASCII 标识（目录名、/name、hub slug 都
# 依赖它，见 VALID_NAME_RE），中文名写在 frontmatter 的 `display_name`。
#
# 这一组**只在创建路径、且只在品牌版开启**（lint_content(product_rules=True)）：
# 随包的 88 个技能和 CI 里跑的都是英文技能，开在它们身上只会刷一屏没人会处理的提醒。
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
DISPLAY_NAME_MAX_CHARS = 20
PRODUCT_RULES = frozenset(
    {
        "missing-display-name",
        "display-name-not-chinese",
        "display-name-length",
        "description-not-chinese",
        "missing-publish-sections",
        "personal-data",
    }
)
# CUSTOM-END: 品牌版技能展示名规则

ERROR = "error"
WARNING = "warning"


@dataclass
class LintFinding:
    """A single lint result. ``severity`` is advisory metadata for the caller."""

    severity: str  # ERROR | WARNING
    rule: str
    message: str

    def format(self) -> str:
        badge = "✗" if self.severity == ERROR else "⚠"
        return f"{badge} [{self.rule}] {self.message}"


# ── Individual checks ────────────────────────────────────────────────────────


def _check_name_matches_dir(
    frontmatter: Dict[str, Any], skill_dir: Optional[Path]
) -> List[LintFinding]:
    if skill_dir is None:
        return []
    name = str(frontmatter.get("name", "")).strip()
    if not name:
        return []
    if name != skill_dir.name:
        return [
            LintFinding(
                ERROR,
                "name-dir-mismatch",
                f"frontmatter name '{name}' does not match directory "
                f"'{skill_dir.name}'; they must be identical.",
            )
        ]
    return []


def _check_name_format(frontmatter: Dict[str, Any]) -> List[LintFinding]:
    name = str(frontmatter.get("name", "")).strip()
    if not name:
        return []
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
        return [
            LintFinding(
                ERROR,
                "name-format",
                f"name '{name}' must be lowercase letters, digits, hyphens, "
                f"and underscores only.",
            )
        ]
    return []


def _check_description(frontmatter: Dict[str, Any]) -> List[LintFinding]:
    findings: List[LintFinding] = []
    # Raw description as authored — extract_skill_description() applies the
    # 60-char prompt truncation, so it can never exceed the limit; measure the
    # raw frontmatter value for the length check.
    desc = str(frontmatter.get("description", "")).strip().strip("'\"")
    if not desc:
        return findings
    if len(desc) > SKILL_PROMPT_DESC_LIMIT:
        findings.append(
            LintFinding(
                WARNING,
                "description-length",
                f"description is {len(desc)} chars; the skill index truncates "
                f"past {SKILL_PROMPT_DESC_LIMIT} chars + '...', losing routing "
                f"signal. Keep it to one sentence.",
            )
        )
    lower = desc.lower()
    hits = [w for w in _MARKETING_WORDS if re.search(rf"\b{re.escape(w)}\b", lower)]
    if hits:
        findings.append(
            LintFinding(
                WARNING,
                "description-marketing",
                f"description contains marketing words {hits}; state the "
                f"capability, not adjectives.",
            )
        )
    return findings


# CUSTOM-BEGIN: 品牌版技能展示名规则
def _has_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


def _check_product_display(frontmatter: Dict[str, Any]) -> List[LintFinding]:
    """品牌版：技能要有中文展示名（display_name）和中文说明。

    全部是 WARNING：技能已经建好了，这里是提醒模型「再补一刀」，不是拦截。
    消息里直接给出可照抄的 patch 内容，省得模型再猜格式、白跑一轮。
    """
    findings: List[LintFinding] = []

    raw = frontmatter.get("display_name")
    display_name = "" if raw is None else str(raw).strip()
    if not display_name:
        findings.append(
            LintFinding(
                WARNING,
                "missing-display-name",
                "缺少 display_name（中文展示名）：同事在技能列表里看到的是它，不是英文标识。"
                "请用 skill_manage(action='patch') 在 frontmatter 里补一行，例如 "
                'display_name: "发票助手"（中文、20 字以内、一看就知道这个技能干什么）。',
            )
        )
    elif not _has_cjk(display_name):
        findings.append(
            LintFinding(
                WARNING,
                "display-name-not-chinese",
                f"display_name 需要是中文（现在是 {display_name!r}）。请用 "
                "skill_manage(action='patch') 改成中文名，例如 display_name: \"发票助手\"。",
            )
        )
    elif len(display_name) > DISPLAY_NAME_MAX_CHARS:
        findings.append(
            LintFinding(
                WARNING,
                "display-name-length",
                f"display_name 有 {len(display_name)} 个字，超过 {DISPLAY_NAME_MAX_CHARS} 字，"
                "列表里会被截断；请缩短。",
            )
        )

    desc = str(frontmatter.get("description", "")).strip().strip("'\"")
    if desc and not _has_cjk(desc):
        findings.append(
            LintFinding(
                WARNING,
                "description-not-chinese",
                "description 请改写成中文：同事说的是中文，系统靠这一句判断什么时候该用这个技能。"
                "一句话、触发词在前、60 字以内；里面有冒号（含「Trigger:」）时，整段要用英文双引号括起来。",
            )
        )
    return findings


def _check_product_publish_spec(content: str, body: str) -> List[LintFinding]:
    """品牌版：正文按发布规范分节、不含个人信息（tools/skill_publish_spec.py）。

    同一份规范也是「整理并发布」的机器检查口径 —— 习得时就写对，发布前就不用再整理。
    """
    from tools.skill_publish_spec import SECTIONS, missing_sections

    findings: List[LintFinding] = []
    missing = missing_sections(body)
    if missing:
        findings.append(
            LintFinding(
                WARNING,
                "missing-publish-sections",
                "正文缺少发布规范要求的章节：" + "、".join(missing) + "。规范章节为 "
                + "、".join(f"## {s}" for s in SECTIONS) + "，正文用中文。",
            )
        )
    try:
        from tools.skill_refine import scan_sensitive

        hits = scan_sensitive({"SKILL.md": content})
    except Exception:  # noqa: BLE001 — 检查器是建议，不能因为扫描异常整个失败
        hits = []
    if hits:
        sample = "、".join(f"{h['label']}「{h['text'][:24]}」" for h in hits[:3])
        findings.append(
            LintFinding(
                WARNING,
                "personal-data",
                f"技能里有 {len(hits)} 处疑似个人信息：{sample}。请换成占位符（如 {{手机号}}、{{用户目录}}）。",
            )
        )
    return findings


# CUSTOM-END: 品牌版技能展示名规则


def _check_metadata_block(frontmatter: Dict[str, Any]) -> List[LintFinding]:
    findings: List[LintFinding] = []
    for key in ("version", "author", "license"):
        if key not in frontmatter:
            findings.append(
                LintFinding(
                    WARNING,
                    "missing-metadata",
                    f"frontmatter is missing '{key}'; every peer skill has it.",
                )
            )
    meta = frontmatter.get("metadata")
    hermes_meta = meta.get("hermes") if isinstance(meta, dict) else None
    if not isinstance(hermes_meta, dict):
        findings.append(
            LintFinding(
                WARNING,
                "missing-metadata",
                "frontmatter is missing metadata.hermes.{tags, related_skills}.",
            )
        )
    else:
        if "tags" not in hermes_meta:
            findings.append(
                LintFinding(
                    WARNING, "missing-metadata", "metadata.hermes.tags is missing."
                )
            )
    author = str(frontmatter.get("author", ""))
    if author and author.strip().lower() in ("hermes", "agent", "hermes agent") and (
        author != "Hermes Agent"
    ):
        findings.append(
            LintFinding(
                WARNING,
                "author-caps",
                f"author '{author}' should be 'Hermes Agent' (proper caps) "
                f"or a real contributor name.",
            )
        )
    return findings


def _check_shell_utilities(body: str) -> List[LintFinding]:
    """Flag banned shell utilities named in PROSE (not fenced code blocks)."""
    findings: List[LintFinding] = []
    prose = _strip_code_blocks(body)
    for util, tool in _SHELL_UTIL_TO_TOOL.items():
        # Backtick-wrapped mention in prose, e.g. `grep` — the failure mode
        # CONTRIBUTING rule 2 targets. Bare words in sentences are too noisy.
        if re.search(rf"`{re.escape(util)}`", prose):
            findings.append(
                LintFinding(
                    WARNING,
                    "shell-utility-reference",
                    f"prose references `{util}`; name the native tool "
                    f"`{tool}` instead.",
                )
            )
    return findings


def _check_sections(body: str) -> List[LintFinding]:
    if not any(re.search(rf"^#+\s+{re.escape(s)}", body, re.M) for s in _EXPECTED_SECTIONS):
        return [
            LintFinding(
                WARNING,
                "missing-section",
                "no '## When to Use' section found; skills need explicit "
                "trigger conditions near the top.",
            )
        ]
    return []


def _check_reference_links(body: str, skill_dir: Optional[Path]) -> List[LintFinding]:
    """Flag references/ links in the body that don't resolve on disk."""
    if skill_dir is None:
        return []
    findings: List[LintFinding] = []
    seen: set[str] = set()
    # Only references/, templates/, assets/ are reliably skill-owned. `scripts/`
    # is excluded: dev skills routinely mention repo-root scripts like
    # `scripts/run_tests.sh` that legitimately live outside the skill dir.
    for match in re.finditer(r"(references|templates|assets)/[\w./-]+", body):
        rel = match.group(0)
        if rel in seen:
            continue
        seen.add(rel)
        # Skip obvious placeholders / globs.
        if "*" in rel or rel.endswith("/"):
            continue
        if not (skill_dir / rel).exists():
            findings.append(
                LintFinding(
                    WARNING,
                    "dangling-reference",
                    f"body references '{rel}' but that file does not exist "
                    f"in the skill directory.",
                )
            )
    return findings


def _check_platforms_gating(
    frontmatter: Dict[str, Any], skill_dir: Optional[Path]
) -> List[LintFinding]:
    """If bundled scripts use POSIX-only primitives, require platforms:."""
    if skill_dir is None:
        return []
    if frontmatter.get("platforms"):
        return []  # already gated
    scripts_dir = skill_dir / "scripts"
    if not scripts_dir.is_dir():
        return []
    offenders: Dict[str, List[str]] = {}
    for script in scripts_dir.rglob("*"):
        if not script.is_file() or script.suffix not in (".py", ".sh", ".bash"):
            continue
        try:
            text = script.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        hit = [p for p in _POSIX_PRIMITIVES if p in text]
        if hit:
            offenders[script.name] = hit
    if offenders:
        detail = "; ".join(f"{k}: {v}" for k, v in offenders.items())
        return [
            LintFinding(
                WARNING,
                "platforms-gating",
                f"scripts use POSIX-only primitives ({detail}) but no "
                f"'platforms:' frontmatter is declared. Fix cross-platform or "
                f"gate with platforms: [linux, macos].",
            )
        ]
    return []


def _check_forbidden_files(skill_dir: Optional[Path]) -> List[LintFinding]:
    if skill_dir is None:
        return []
    findings: List[LintFinding] = []
    for fname in _FORBIDDEN_FILES:
        if (skill_dir / fname).exists():
            findings.append(
                LintFinding(
                    WARNING,
                    "forbidden-file",
                    f"skill ships '{fname}'; skills should not include "
                    f"scaffolding/config files.",
                )
            )
    return findings


def _check_platform_list_valid(frontmatter: Dict[str, Any]) -> List[LintFinding]:
    platforms = frontmatter.get("platforms")
    if not platforms:
        return []
    valid = {"linux", "macos", "windows", "darwin"}
    items = platforms if isinstance(platforms, list) else [platforms]
    bad = [p for p in items if str(p).lower() not in valid]
    if bad:
        return [
            LintFinding(
                WARNING,
                "platforms-value",
                f"platforms contains unrecognized value(s) {bad}; expected a "
                f"subset of {sorted(valid)}.",
            )
        ]
    return []


# ── Helpers ──────────────────────────────────────────────────────────────────


def _strip_code_blocks(body: str) -> str:
    """Remove fenced code blocks so prose-only checks don't fire on examples."""
    return re.sub(r"```.*?```", "", body, flags=re.S)


# ── Public API ───────────────────────────────────────────────────────────────


def lint_content(
    content: str, *, skill_dir: Optional[Path] = None, product_rules: bool = False
) -> List[LintFinding]:
    """Lint raw SKILL.md *content*.

    Pass ``skill_dir`` to enable on-disk checks (name/dir match, dangling
    reference links, POSIX-primitive gating, forbidden files). Without it,
    only content-only checks run — useful for the create path, where the file
    is not yet written.

    ``product_rules`` (CUSTOM) turns on the branded-build checks: a Chinese
    ``display_name`` and a Chinese ``description``, plus the company publishing
    standard (Chinese body sections, no personal data). Off by default so the
    bundled (English) skills and upstream CI stay quiet.
    """
    frontmatter, body = parse_frontmatter(content)
    findings: List[LintFinding] = []
    findings += _check_name_format(frontmatter)
    findings += _check_name_matches_dir(frontmatter, skill_dir)
    findings += _check_description(frontmatter)
    findings += _check_metadata_block(frontmatter)
    if product_rules:  # CUSTOM
        findings += _check_product_display(frontmatter)
        findings += _check_product_publish_spec(content, body)
    findings += _check_platform_list_valid(frontmatter)
    findings += _check_shell_utilities(body)
    # CUSTOM: 品牌版用中文章节（适用场景…），英文的 When to Use 提醒会和发布规范打架
    if not product_rules:
        findings += _check_sections(body)
    findings += _check_reference_links(body, skill_dir)
    findings += _check_platforms_gating(frontmatter, skill_dir)
    findings += _check_forbidden_files(skill_dir)
    return findings


def lint_skill(skill_md_path: Path, *, product_rules: bool = False) -> List[LintFinding]:
    """Lint a SKILL.md file on disk, with all on-disk checks enabled."""
    skill_md_path = Path(skill_md_path)
    content = skill_md_path.read_text(encoding="utf-8", errors="ignore")
    return lint_content(
        content, skill_dir=skill_md_path.parent, product_rules=product_rules
    )


def format_findings(findings: List[LintFinding]) -> str:
    """Render findings as a newline-joined human-readable block."""
    return "\n".join(f.format() for f in findings)


def has_errors(findings: List[LintFinding]) -> bool:
    return any(f.severity == ERROR for f in findings)


def _main(argv: Optional[List[str]] = None) -> int:
    """CLI: ``python -m tools.skill_linter <path-to-SKILL.md-or-dir> ...``

    Accepts SKILL.md files or skill directories (recursively linted). Prints
    findings grouped per skill. Exit code 1 if any ERROR-severity finding is
    present (WARNING-only is exit 0), so CI can gate on structural breakage
    without failing on advisory convention nits.
    """
    import sys

    args = list(argv if argv is not None else sys.argv[1:])
    # CUSTOM: --product 开启品牌版规则（中文展示名 / 中文说明），给管理员检查同事交来的技能用
    product_rules = "--product" in args
    args = [a for a in args if a != "--product"]
    if not args:
        print("usage: python -m tools.skill_linter [--product] <SKILL.md | skill-dir> ...")
        return 2

    targets: List[Path] = []
    for arg in args:
        p = Path(arg)
        if p.is_dir():
            targets.extend(sorted(p.rglob("SKILL.md")))
        elif p.name == "SKILL.md" and p.is_file():
            targets.append(p)
        else:
            print(f"skip (not a SKILL.md or dir): {arg}")

    any_error = False
    total = 0
    for skill_md in targets:
        findings = lint_skill(skill_md, product_rules=product_rules)
        if not findings:
            continue
        total += len(findings)
        if has_errors(findings):
            any_error = True
        print(f"\n{skill_md.parent.name} ({skill_md}):")
        print(format_findings(findings))

    if total == 0:
        print(f"All {len(targets)} skill(s) clean.")
    else:
        print(f"\n{total} finding(s) across {len(targets)} skill(s).")
    return 1 if any_error else 0


if __name__ == "__main__":
    import sys

    sys.exit(_main())
