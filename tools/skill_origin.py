"""技能的来源标记 —— P0-4：项目记忆与个人记忆从一开始就分开存

## 为什么必须**现在**做，而不是等群聊界面

设计决议原话：「这是离职处理的前置条件，**事后拆不出来**。项目上下文（需求、决议、
任务历史）属于项目资产，个人 agent 的偏好与私有记忆属于个人。两者混在一个存储里，
离职当天只剩两个都错的选项：连人带项目知识一起带走，或者把个人数据强留在公司。」

配套那条同样是「事后补不了」：「**skill 生成时必须打来源标记**（产生于哪个项目、
哪次会话）。没有它，离职当天面对一堆 markdown 只剩『全审』或『全弃』两个选项。」

## 「今天所有标记都写 personal」不是白做

客户端还没有项目上下文（群聊界面没做），所以现在生成的每一条标记都是 `personal`。
这看上去像空转，但差别是实质的：

- 有标记：离职当天知道这 200 条是个人的、那 15 条产生于 Q4 项目，可以**按项目分组
  取舍**
- 没标记：离职当天面对 215 个 markdown 文件，只能全审或全弃

而项目上下文接上来的那天，**要改的只有 `current_project()` 一个函数** —— 判定与
写入路径都在位了。反过来（先做界面、事后补标记）是不可能的：那 215 个文件已经
没有出处了。

## 分组语义是**取舍**，不是批准，且**默认不进**项目库

「一个人的 skill 库大部分是个人工作习惯，默认全收会污染项目知识。」所以
`group_for_offboarding` 把 personal 与 project 分开列，不做任何「建议收录」的判断
—— 那是人的活。

## 标记存在技能目录里的 sidecar，不存中央索引

中央索引会漂：技能被改名、被移动、被手工删掉之后，索引里那一行指向空气，而
「这条到底还在不在」没法从索引本身判断。sidecar 跟着目录走，目录没了标记也就没了，
不会留下假信息。

⚠️ 它必须排除在**发布到 skill hub** 之外（见 tools/skill_publish.SKIP_FILES）——
   发出去只是噪音，而且会把「这个技能产生于哪个项目」带给全公司。
"""

from __future__ import annotations

import contextvars
import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

ORIGIN_FILENAME = ".llano-origin.json"
SCHEMA_VERSION = 1

PERSONAL = "personal"
PROJECT = "project"


@dataclass(frozen=True)
class ProjectContext:
    """当前这一轮对话属于哪个项目。"""

    project_code: str
    project_name: str = ""
    session_id: str = ""


# 默认 None = 个人上下文。项目群聊接上来时，由跑这一轮的代码 set 进去。
_project_ctx: contextvars.ContextVar[Optional[ProjectContext]] = contextvars.ContextVar(
    "llano_project_context", default=None
)


def set_current_project(ctx: Optional[ProjectContext]) -> contextvars.Token:
    """绑定当前轮次的项目上下文。调用方必须在 finally 里 reset。

    与上游 skill_provenance.set_current_write_origin 同一个形状 —— 那条区分的是
    「后台自省 vs 前台」，这条区分的是「项目 vs 个人」。两者正交：一个后台自省
    产生的技能同样可能产生在项目上下文里。
    """
    return _project_ctx.set(ctx)


def reset_current_project(token: contextvars.Token) -> None:
    _project_ctx.reset(token)


def current_project() -> Optional[ProjectContext]:
    """当前轮次的项目上下文，个人上下文时为 None。

    ⚠️ **项目群聊接上来时，要改的就是这一个函数的输入** —— 判定与写入路径都在位了。
    """
    return _project_ctx.get()


def build_record(*, session_id: str = "", write_origin: str = "") -> Dict[str, Any]:
    """组装一条来源标记。

    `write_origin` 是上游那条正交信息（前台 / 后台自省）。一起记下来，因为离职
    取舍时「这条是 agent 自己攒的还是人让它写的」同样是判断依据。
    """
    ctx = current_project()
    record: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "scope": PROJECT if ctx else PERSONAL,
        # UTC 原文。写它的是员工的机器，时区不受我们控制 —— 与
        # client_releases.released_at、skill_security_events.occurred_at 同一处理。
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "session_id": session_id or (ctx.session_id if ctx else ""),
        "write_origin": write_origin or "",
    }
    if ctx:
        record["project_code"] = ctx.project_code
        record["project_name"] = ctx.project_name
    return record


def stamp(skill_dir: Path, *, session_id: str = "", write_origin: str = "") -> Optional[Path]:
    """给一个技能目录打来源标记。**已有标记不覆盖。**

    改一条已存在的技能不该改写它的出处：一条产生于 Q4 项目的技能，后来在私聊里
    被顺手改过一次，它仍然产生于那个项目。覆盖的话，离职当天那条会被算成个人的。

    写失败**不抛** —— 标记没打上是要修的问题，但不该让创建技能这件事失败。
    失败会记 warning，因为静默失败等于回到「没有标记」那个状态，而那正是要避免的。
    """
    path = Path(skill_dir) / ORIGIN_FILENAME
    if path.exists():
        return path

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(build_record(session_id=session_id, write_origin=write_origin),
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path
    except OSError as exc:
        logger.warning("技能来源标记写不进去（%s）：%s —— 这条技能离职时会缺出处",
                       skill_dir, exc)
        return None


def read(skill_dir: Path) -> Optional[Dict[str, Any]]:
    """读一个技能的来源标记。没有或坏了返回 None。

    没有标记 ≠ 个人。**它是「不知道」** —— 阶段 13 之前创建的技能全都没有标记，
    把它们算成个人的，等于替管理员做了一个他没做的判断。分组函数据此单列一档。
    """
    path = Path(skill_dir) / ORIGIN_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def group_for_offboarding(skills_root: Path) -> Dict[str, Any]:
    """按来源把技能分组，供离职取舍。

    三档，**「不知道」单列**：

    - `personal`：明确产生于个人上下文
    - `by_project`：按项目分组
    - `unknown`：没有标记（阶段 13 之前建的，或标记写失败的）

    把 unknown 并进 personal 是最省事的写法，也是错的：那等于替管理员断言
    「这些不是项目资产」，而他根本没看过。少数几条不确定的让人扫一眼，
    比一个悄悄做掉的默认判断好。

    语义是**取舍**不是批准，默认不进项目库 —— 所以这里只分组，不给任何
    「建议收录」的结论。一个人的 skill 库大部分是个人工作习惯，默认全收会污染
    项目知识。
    """
    root = Path(skills_root)
    personal: List[Dict[str, Any]] = []
    unknown: List[Dict[str, Any]] = []
    by_project: Dict[str, List[Dict[str, Any]]] = {}

    if not root.exists():
        return {"personal": [], "by_project": {}, "unknown": [], "total": 0}

    for skill_md in sorted(root.rglob("SKILL.md")):
        skill_dir = skill_md.parent
        entry = {
            "name": skill_dir.name,
            "path": str(skill_dir.relative_to(root)),
        }
        record = read(skill_dir)
        if record is None:
            unknown.append(entry)
            continue

        entry["created_at"] = record.get("created_at", "")
        entry["write_origin"] = record.get("write_origin", "")
        if record.get("scope") == PROJECT and record.get("project_code"):
            entry["session_id"] = record.get("session_id", "")
            by_project.setdefault(str(record["project_code"]), []).append(entry)
        else:
            personal.append(entry)

    return {
        "personal": personal,
        "by_project": by_project,
        "unknown": unknown,
        "total": len(personal) + len(unknown) + sum(len(v) for v in by_project.values()),
    }
