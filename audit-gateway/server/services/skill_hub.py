"""自有 skill hub 的载荷解析与规范化 —— 阶段 11 · S11-B

## wire protocol 长这样，以及为什么

发布（POST /v1/skill-hub/skills）：

    {
      "schema_version": 1,
      "slug": "lingxing-daily-report",
      "name": "领星日报",
      "description": "拉当日订单并出汇总",
      "changelog": "加了退款口径",
      "files": [
        {"path": "SKILL.md",         "encoding": "utf-8",  "content": "..."},
        {"path": "assets/logo.png",  "encoding": "base64", "content": "..."}
      ]
    }

### files 为什么是**数组**不是 `{path: content}` 字典

字典更顺手，但同一个 path 出现两次时 json 解析会**静默留下最后一个** ——
而「同名两份内容」正是想藏东西的人会试的第一件事：审查的人读到前一份，
装到磁盘上的是后一份。数组能把重复路径显式撞出来，直接拒。

### 为什么每个文件带 encoding 信封

技能里有 `assets/` 这类二进制。tools/skill_report.py 的样本收集**直接丢掉**
二进制（那是取证用的，审查的人要读代码，二进制读不了也占地方），但 hub 是
**分发**通道：丢掉一个 png 意味着分发出去的技能是坏的，而且坏得很安静 ——
装上了，运行时才发现引用的图不在。所以这里必须能原样往返，base64 兜二进制。

## 路径校验是**重写**的，不是 import 的

真正的路径校验在客户端 `tools/skills_hub._normalize_bundle_path`，装到磁盘那一步
会再跑一遍。网关是独立服务，跨仓 import 不了，所以这里照着重写一份。

规则必须**不弱于**上游那份，特别是这两条容易漏的：

- **拒绝任何一段里带冒号**。Windows 上冒号要么是盘符（`C:`），要么是 NTFS 的
  Alternate Data Stream：一个叫 `run.py:payload` 的成员会把字节写进**看不见的流**
  里，而 `rglob` 式的复核根本枚举不到它。
- **拒绝 `..`**，包括规范化之后才露出来的。

在网关也拦一遍，是因为这里是**扇出点**：一个坏路径从这里发出去要在几十台机器上
各拦一次，而其中任何一台的客户端版本落后一点就漏了。

## 身份一律不听载荷

owner_uid / publisher_uid / dept_* 全部由调用方（router）从 API Key 解出来传进
这一层。载荷里带了这些字段一概忽略：客户端跑在员工自己的机器上，它说自己是谁
没有意义，而这两张表的用途之一恰恰是部门技能指标统计。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Dict, List

SCHEMA_VERSION = 1

# 整个 JSON 请求体的上限。base64 把二进制撑成约 4/3，再加 JSON 转义，
# 给 4MB 的解码后预算留到 8MB。
MAX_PAYLOAD_BYTES = 8 * 1024 * 1024
# 一个技能包的文件数。技能是「一份 SKILL.md 加几个附件」，超过这个数的
# 多半不是技能而是有人在拿 hub 当网盘。
MAX_FILES = 64
# 单文件与总量，都按**解码后**的原始字节算。
MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024

# slug 是安装标识，也是技能落到磁盘上的目录名，所以字符集要比显示名严格得多。
# 首尾必须是字母数字：`-foo` 在命令行上会被当成选项，`foo-` 纯粹是手滑。
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")

TEXT_ENCODING = "utf-8"
BINARY_ENCODING = "base64"
KNOWN_ENCODINGS = frozenset({TEXT_ENCODING, BINARY_ENCODING})

LISTED = "listed"
DELISTED = "delisted"

# ==================== 空间分层（阶段 21）====================
#
# 只有两级。**没有 personal** —— 个人技能留在客户端本地，不上传列表：那会让
# 「员工装了哪些技能」变成网关上的数据，而这件事没有非做不可的理由。客户端发起的
# 是「发布到部门空间」的请求。
SCOPE_DEPT = "dept"
SCOPE_COMPANY = "company"
KNOWN_SCOPES = frozenset({SCOPE_DEPT, SCOPE_COMPANY})


def can_browse_all(role: str, dept_code: str | None) -> bool:
    """负责人 10-12：经理及以上、管理部门全员、技术部门可跨部门浏览。

    这里只扩大已上架技能的读取范围，不扩大晋级、编辑、下架等写权限。
    部门编码来自认证结果，不接受客户端声明；名称改动不影响授权。
    """
    from model.database import role_level
    return role_level(role) >= 40 or dept_code in {"exec", "digital-ops"}


# 可见性规则（公司空间 ∪ 本部门及所有上级空间）只在 db/selects/skill_hub._visible_to
# 一处实现。09-03 放开成全员可见、09-23 收回，都只改了那一处。


def can_promote(role: str, caller_dept_id: Optional[int], skill_dept_id: Optional[int],
                *, is_admin: bool = False) -> bool:
    """能不能把这个技能发到公司空间。

    主管只能晋级**自己部门**的技能。这是 manager 拿到管理端权限之后必须补上的
    数据范围 —— 上一轮给 skill_hub:view 时特意没给 manager，理由正是「这两个权限点
    没有数据范围概念，给了就是全公司可见」。现在负责人要主管做筛选，那就得把范围
    真正做出来，而不是在权限名单里加个词了事。
    """
    if is_admin:
        return True
    if caller_dept_id is None or skill_dept_id is None:
        return False
    return int(caller_dept_id) == int(skill_dept_id)

# 与 model/database.py 的列宽对齐。这几个字段超长**截断**而不是拒收：
# 一个过长的说明不值得把整次发布丢掉，它们也不参与任何判定。
# slug 不在这里 —— 它是标识，截断会变成另一个技能。
_MAX_LENS = {
    "name": 255,
    "description": 1024,
    "changelog": 1024,
}


class SkillHubRejected(ValueError):
    """载荷不可接收。调用方转成真 4xx（见 router/v1/skill_hub.py 的说明）。"""


@dataclass
class ParsedFile:
    path: str
    encoding: str
    content: str
    raw_bytes: int
    sha256: str


@dataclass
class ParsedPublish:
    slug: str
    name: str
    description: str
    changelog: str
    files: List[ParsedFile]
    bundle_hash: str
    file_count: int
    total_bytes: int

    def files_json(self) -> str:
        """存库的那份。顺序已经在 parse 里排好，重算 bundle_hash 时才对得上。"""
        return json.dumps(
            [
                {"path": f.path, "encoding": f.encoding, "content": f.content}
                for f in self.files
            ],
            ensure_ascii=False,
        )


def _text(value: Any, field: str, *, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise SkillHubRejected(f"{field} 必须是字符串")
    out = value.strip()
    if required and not out:
        raise SkillHubRejected(f"{field} 不能为空")
    limit = _MAX_LENS.get(field)
    return out[:limit] if limit else out


def publish_metadata(value: Any, field: str) -> str:
    """新发布及新版必须有中文展示信息；只约束写入，不影响存量安装。"""
    label = {"name": "中文标题", "description": "中文描述"}[field]
    if not isinstance(value, str) or not value.strip():
        raise SkillHubRejected(f"请补填{label}（{field}）后重新发布")
    text = value.strip()
    if len(text) > _MAX_LENS[field]:
        raise SkillHubRejected(f"{label}最多 {_MAX_LENS[field]} 个字符，请缩短后重新发布")
    from services.skill_hub_metadata import has_chinese
    if not has_chinese(text):
        raise SkillHubRejected(f"{label}必须包含中文，请说明技能用途后重新发布")
    return text


def normalize_slug(value: Any) -> str:
    """安装标识的规范化。**不做大小写折叠之外的补救**。

    容错到「把 `My Skill` 改成 `my-skill`」听上去友好，实际是把发布者以为的名字
    和真正落盘的名字悄悄岔开：他在 hub 上搜自己刚发的技能会搜不到。
    所以只折叠大小写和首尾空白，剩下的不合规就直接报错，让人自己改。
    """
    raw = value if isinstance(value, str) else ""
    slug = raw.strip().lower()
    if not slug:
        raise SkillHubRejected("slug 不能为空")
    if len(slug) > 64:
        raise SkillHubRejected("slug 超过 64 个字符")
    if not SLUG_RE.match(slug):
        raise SkillHubRejected(
            f"slug 只能用小写字母、数字和中划线，且首尾必须是字母或数字：{slug!r}"
        )
    return slug


def normalize_bundle_path(path_value: Any) -> str:
    """技能包内部路径的校验。规则对齐 tools/skills_hub._normalize_bundle_path。

    这一层拦下的东西在客户端安装时还会再拦一次。两道都要留：网关是扇出点，
    客户端是最后一米，而客户端版本是会落后的。
    """
    if not isinstance(path_value, str):
        raise SkillHubRejected("文件路径必须是字符串")

    raw = path_value.strip()
    if not raw:
        raise SkillHubRejected("文件路径不能为空")

    # 反斜杠先归一成正斜杠，否则 `a\..\..\b` 在 PurePosixPath 眼里是**一整段**
    # 文件名，`..` 检查会直接漏过去。
    normalized = raw.replace("\\", "/")
    path = PurePosixPath(normalized)
    parts = [p for p in path.parts if p not in {"", "."}]

    if normalized.startswith("/") or path.is_absolute():
        raise SkillHubRejected(f"文件路径不能是绝对路径：{path_value!r}")
    if not parts:
        raise SkillHubRejected(f"文件路径为空：{path_value!r}")
    if any(p == ".." for p in parts):
        raise SkillHubRejected(f"文件路径不能往上跳：{path_value!r}")
    # 冒号：Windows 的盘符或 NTFS Alternate Data Stream。
    # `run.py:payload` 会把字节写进看不见的流里，rglob 式的复核枚举不到。
    if any(":" in p for p in parts):
        raise SkillHubRejected(f"文件路径里不能有冒号：{path_value!r}")

    return "/".join(parts)


def _decode_one(entry: Any, index: int) -> ParsedFile:
    if not isinstance(entry, dict):
        raise SkillHubRejected(f"files[{index}] 必须是对象")

    path = normalize_bundle_path(entry.get("path"))
    encoding = entry.get("encoding") or TEXT_ENCODING
    if encoding not in KNOWN_ENCODINGS:
        raise SkillHubRejected(
            f"{path}：不认识的 encoding {encoding!r}，只支持 utf-8 / base64"
        )

    content = entry.get("content")
    if not isinstance(content, str):
        raise SkillHubRejected(f"{path}：content 必须是字符串")

    if encoding == TEXT_ENCODING:
        raw = content.encode("utf-8")
    else:
        try:
            # validate=True：默认的宽松模式会**静默丢掉**非 base64 字符，于是
            # 一份被改坏的载荷能解出一份看似正常、内容却不同的文件。
            raw = base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise SkillHubRejected(f"{path}：base64 解不开（{exc}）")

    if len(raw) > MAX_FILE_BYTES:
        raise SkillHubRejected(
            f"{path}：单文件 {len(raw)} 字节，超过上限 {MAX_FILE_BYTES}"
        )

    return ParsedFile(
        path=path,
        encoding=encoding,
        content=content,
        raw_bytes=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def compute_bundle_hash(files: List[ParsedFile]) -> str:
    """内容指纹 = 排序后的「路径 + 各自 sha256」再哈希一遍。

    服务端算，不听客户端上报的任何哈希 —— 同 skill_security_samples 的理由。
    它只用来回答「这一版跟上一版是不是同一份东西」，不用于任何安全判定。

    算的是**解码后**的字节：同一份内容用 utf-8 发和用 base64 发必须得到同一个
    指纹，否则换个发法就能凭空多出一版。
    """
    digest = hashlib.sha256()
    for f in sorted(files, key=lambda x: x.path):
        digest.update(f.path.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(f.sha256.encode("ascii"))
        digest.update(b"\x00")
    return digest.hexdigest()


def parse_publish(payload: Any, *, raw_bytes: int, require_chinese_metadata: bool = True) -> ParsedPublish:
    """把发布载荷解成可入库的形状。看得懂就收下，看不懂就明确拒绝。"""
    if raw_bytes > MAX_PAYLOAD_BYTES:
        raise SkillHubRejected(
            f"载荷 {raw_bytes} 字节，超过上限 {MAX_PAYLOAD_BYTES}"
        )
    if not isinstance(payload, dict):
        raise SkillHubRejected("载荷必须是 JSON 对象")

    version = payload.get("schema_version", SCHEMA_VERSION)
    if not isinstance(version, int) or version != SCHEMA_VERSION:
        raise SkillHubRejected(
            f"不支持的 schema_version {version!r}，本网关只认 {SCHEMA_VERSION}"
        )

    slug = normalize_slug(payload.get("slug"))
    if require_chinese_metadata:
        name = publish_metadata(payload.get("name"), "name")
        description = publish_metadata(payload.get("description"), "description")
    else:
        name = _text(payload.get("name", ""), "name")
        description = _text(payload.get("description", ""), "description")
        for field in ("name", "description"):
            if len(payload.get(field) or "") > _MAX_LENS[field]:
                raise SkillHubRejected(f"{field} 超过长度上限")
    changelog = _text(payload.get("changelog"), "changelog")

    raw_files = payload.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise SkillHubRejected("files 必须是非空数组")
    if len(raw_files) > MAX_FILES:
        raise SkillHubRejected(f"文件数 {len(raw_files)} 超过上限 {MAX_FILES}")

    files: List[ParsedFile] = []
    seen: Dict[str, int] = {}
    total = 0
    for i, entry in enumerate(raw_files):
        parsed = _decode_one(entry, i)
        if parsed.path in seen:
            # 同名两份内容：审查的人读到前一份，装到磁盘上的是后一份。
            raise SkillHubRejected(
                f"文件路径重复：{parsed.path}（files[{seen[parsed.path]}] 与 files[{i}]）"
            )
        seen[parsed.path] = i
        total += parsed.raw_bytes
        if total > MAX_TOTAL_BYTES:
            raise SkillHubRejected(
                f"技能包解码后超过 {MAX_TOTAL_BYTES} 字节上限"
            )
        files.append(parsed)

    # SKILL.md 是技能的定义本身，没有它装下去也是个空壳。
    # 大小写不敏感地找：Windows 上发布的包很容易是 Skill.md。
    if not any(f.path.lower() == "skill.md" for f in files):
        raise SkillHubRejected("技能包里必须有 SKILL.md")

    files.sort(key=lambda f: f.path)

    return ParsedPublish(
        slug=slug,
        name=name,
        description=description,
        changelog=changelog,
        files=files,
        bundle_hash=compute_bundle_hash(files),
        file_count=len(files),
        total_bytes=total,
    )
