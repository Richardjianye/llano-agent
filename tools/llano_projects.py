"""协作项目的 sidecar 侧客户端 —— 阶段 16

## 为什么这一层必须存在

桌面端的数据通路是：

    React 渲染进程 --WS--> hermes sidecar (Python) --HTTPS+APIKey--> 审计网关

渲染进程**拿不到也不该拿到** `LLANO_GATEWAY_API_KEY`（它在主进程注入 sidecar 的
环境里）。所以项目、群聊消息、任务这些住在网关上的数据，都要由 sidecar 代取。

复用 llano_skill_hub 的 `_request`：同一把 Key、同一个 origin、同一套错误话术。
另起一份 HTTP 封装会带来两套超时、两套错误处理，而它们会各自漂移。

## 读路径与写路径的错误处理**相反**

- **读**（列项目、同步消息、看任务）：失败抛给 sidecar，由它保留缓存并标记 stale。
  成功的空列表必须与读取失败区分，不能用旧缓存复活已移除的成员。
- **写**（发消息、建任务、认领、确认）：失败**抛出去**。用户按了发送却什么都
  没发生，必须说清楚为什么 —— 与 skill_publish 那条判断一样。

## ⚠️ 认领与确认是两个调用，不能合并成一个

这是 P0-6 的落点。`claim()` 返回的 `confirmation` 里带着任务原文，界面**必须**
把它呈给人看，拿到人的确认之后才调 `confirm()`。

合并成一步（认领即开工）就把「人在环」那一下抹掉了 —— 而群里发一个夹带注入的
任务描述、诱导 agent 认领、回私聊执行，正是那一下要挡的东西。所以这里刻意不提供
`claim_and_confirm` 之类的便利函数：便利函数会被用，然后防线就没了。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlencode

from tools.llano_skill_hub import SkillHubUnavailable, _request

logger = logging.getLogger(__name__)

# 沿用同一个异常类型：对调用方来说「网关这条路走不通」是同一件事，
# 分成两个异常只会让每个调用点都要 catch 两次。
ProjectsUnavailable = SkillHubUnavailable

API = "/v1/projects"


def _path(code: str, *suffix: str) -> str:
    parts = [API, quote(str(code), safe="")]
    parts.extend(quote(str(s), safe="") for s in suffix)
    return "/".join(parts)


# ==================== 读：失败返回空，不让界面崩 ====================


def identity_fingerprint() -> str:
    """当前账号 + 网关地址的指纹，用来判断「还是同一个人吗」（阶段 27-B）。

    **不回传也不记录 Key 本身**：这个值会进日志和响应体，而 Key 一旦露出来，
    渲染进程拿不到 Key 这条前提就作废了。取哈希前缀足够回答「变了没有」，
    而这正是唯一需要回答的问题。

    没登录时返回空串 —— 空串与任何真实身份都不相等，所以退出后照样触发清场。
    """
    import hashlib

    from tools.llano_skill_hub import _api_key, hub_origin

    key = _api_key()
    if not key:
        return ""
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return f"{hub_origin()}#{digest}"


def messages_since(code: str, *, after_seq: int = 0, limit: int = 200) -> List[Dict[str, Any]]:
    """与 `list_messages` 同一条路，但**失败抛出去**。

    轮询要能分辨「网络抖了，等会儿重试」和「项目归档 / 我被移除了，别再拉了」。
    吞掉异常的那个版本回答不了这个问题，而分不出来的后果是一个对着自己已经没
    资格看的项目每秒敲一次网关的后台死循环（P0-A 明确要求有终止条件）。
    """
    params = urlencode({"after_seq": max(0, int(after_seq or 0)),
                        "limit": max(1, min(int(limit or 200), 200))})
    payload = _request(f"{_path(code, 'messages')}?{params}")
    messages = payload.get("messages") if isinstance(payload, dict) else None
    return messages if isinstance(messages, list) else []


def list_projects() -> List[Dict[str, Any]]:
    payload = _request(API)
    projects = payload.get("projects") if isinstance(payload, dict) else None
    return projects if isinstance(projects, list) else []


def list_messages(code: str, *, after_seq: int = 0, limit: int = 50) -> List[Dict[str, Any]]:
    """拉消息。`after_seq` 是**项目内**序号 —— 用全局 id 当游标会因为别的项目的
    写入而跳号，界面上看起来像丢了消息。"""
    try:
        params = urlencode({"after_seq": max(0, int(after_seq or 0)),
                            "limit": max(1, min(int(limit or 50), 200))})
        payload = _request(f"{_path(code, 'messages')}?{params}")
    except ProjectsUnavailable as exc:
        logger.debug("协作：%s 的消息取不到（%s）", code, exc)
        return []
    messages = payload.get("messages") if isinstance(payload, dict) else None
    return messages if isinstance(messages, list) else []


def list_members(code: str) -> List[Dict[str, Any]]:
    payload = _request(_path(code, "members"))
    members = payload.get("members") if isinstance(payload, dict) else None
    return members if isinstance(members, list) else []


def list_tasks(code: str, *, status: str = "") -> List[Dict[str, Any]]:
    suffix = f"?{urlencode({'status': status})}" if status else ""
    payload = _request(f"{_path(code, 'tasks')}{suffix}")
    tasks = payload.get("tasks") if isinstance(payload, dict) else None
    return tasks if isinstance(tasks, list) else []


# ==================== 写：失败抛出去，不能静默 ====================


def _post(path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    raw = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
    result = _request(path, method="POST", body=raw)
    return result if isinstance(result, dict) else {}


def _put(path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    raw = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
    result = _request(path, method="PUT", body=raw)
    return result if isinstance(result, dict) else {}


def create_project(code: str, name: str = "", description: str = "") -> Dict[str, Any]:
    return _post(API, {"code": code, "name": name, "description": description})


def retry_repository(code: str) -> Dict[str, Any]:
    return _post(_path(code, "repository", "retry"))


def directory(keyword: str = "", limit: int = 50) -> List[Dict[str, Any]]:
    """在职同事列表。**读路径**：拿不到就回空，别让「加成员」那个框炸掉。

    字段只有 uid / username / realname / dept_name —— 服务端就只回这四个，见
    router/v1/directory.py。群聊令牌拿不到这个接口。
    """
    query = urlencode({"q": keyword, "limit": max(1, min(int(limit), 200))})
    try:
        payload = _request(f"/v1/directory?{query}")
    except SkillHubUnavailable:
        return []
    users = payload.get("users") if isinstance(payload, dict) else None
    return users if isinstance(users, list) else []


def invite_member(code: str, user_uid: str, username: str = "",
                  role: str = "member") -> Dict[str, Any]:
    """把一个**人**加进项目。只有负责人能加，判定在服务端。"""
    return _post(_path(code, "members"),
                 {"member_kind": "user", "user_uid": user_uid,
                  "username": username, "role": role})


def pull_my_agent(code: str, agent_label: str = "") -> Dict[str, Any]:
    """把**自己的** agent 拉进这个项目。

    ⚠️ 只能拉自己的，而且这不是客户端的自觉：服务端在 `member_kind=agent` 时
    一律用调用者本人的 uid，载荷里写别人的会被忽略。能拉别人的 agent 等于替别人
    的凭据做决定 —— 那个 agent 手里是它主人的中台账号。

    这里连 uid 字段都不发，是为了让「拉不了别人的」这件事在代码里也读得出来。
    """
    return _post(_path(code, "members"),
                 {"member_kind": "agent", "agent_label": agent_label})


def say(code: str, content: str, *, reply_to_id: Optional[int] = None) -> Dict[str, Any]:
    """发一条群聊消息。

    ⚠️ **脱敏发生在服务端**，这里不做也不该做。指望发送方的客户端替换等于把一条
    防线交给被防的那一侧 —— 它跑在员工自己的机器上，而接收方是别的部门、甚至
    别的公司的人（P0-2）。

    返回体里的 `redacted` 是服务端告诉我们「这条被遮过」，界面应当显示出来：
    收到的人有权知道自己看到的不是原话，发的人也有权知道自己的话被改了。
    """
    return _post(_path(code, "messages"), {"content": content, "reply_to_id": reply_to_id})


def create_task(code: str, title: str, body: str = "", *,
                source_message_id: Optional[int] = None,
                from_chat: bool = True) -> Dict[str, Any]:
    """建任务。

    `untrusted` **不在这里传** —— 服务端按来源自己定。客户端能提交这个标记的话，
    注入一句「把 untrusted 设成 false」就绕过整条 P0-6 防线。
    """
    return _post(_path(code, "tasks"), {
        "title": title,
        "body": body,
        "source_kind": "chat" if from_chat else "manual",
        "source_message_id": source_message_id,
    })


def claim(code: str, task_id: int, *, agent_label: str = "") -> Dict[str, Any]:
    """认领。**返回体里的 `confirmation` 必须呈给人看。**

    它带着任务原文（原样，不摘要不高亮）与一句提醒。界面拿到之后要停下来等人，
    而不是接着调 confirm —— 那一下人在环是 P0-6 唯一的防线。
    """
    return _post(_path(code, "tasks", str(task_id), "claim"), {"agent_label": agent_label})


def confirm(code: str, task_id: int) -> Dict[str, Any]:
    """人看过原文之后确认开工。

    返回体里的 `untrusted` 要**跟着任务描述一起进入私聊上下文**，不能因为
    「这是主人自己的会话」就清掉（P0-6 原话）。
    """
    return _post(_path(code, "tasks", str(task_id), "confirm"))


# ==================== 群聊 agent 的执行租约（阶段 27-E）====================
#
# 这一组全部是**写**路径的形状：失败抛出去。租约拿不到、心跳续不上、写回被拒，
# 每一样都必须让调用方知道 —— 一个静默失败的 turn 表现为「我的 agent 不说话」，
# 而那是最难查的一类问题。
#
# ⚠️ 身份不在这里：`turn_reply` 不带 sender_kind / owner_uid / agent_label。
# 服务端从租约推。能自称 agent 的接口就是决策 #45 说不能做的那种设计。


# 群聊令牌的本地缓存。
#
# ## 为什么要缓存
#
# `POST /{code}/session` 每次都**无条件新签一把**，不复用现有未过期的同
# (uid, project) 令牌。而 `run_turn` 每一轮都要一把 —— 于是一个活跃项目每跑一轮
# 就在 `api_keys` 里留一行，8 小时内都有效。2026-09-07 的阶段 27 验收里跑 10 轮
# 就留下 10 行：表随轮次单调增长，同一时刻存在 N 把可用凭据。
#
# 这不是越权问题（群聊令牌只会让持有者**少**拿到东西，见服务端那个接口的注释），
# 所以修在客户端就够：少签就少留。
#
# ## 为什么缓存窗口远小于服务端 TTL
#
# 服务端给 8 小时，这里只缓存 1 小时。缓存窗口的作用是「把每轮一把降到每小时
# 一把」，再长收益已经趋近于零，而代价是实打实的：**客户端与服务端的时钟不一定
# 同步**，越贴近过期边界，一次时钟偏差就越可能让我们把一把已经死掉的令牌喂给
# 模型调用。1 小时的窗口配 8 小时的 TTL，中间留了 7 小时的余量。
_GROUP_SESSION_MAX_CACHE_SECONDS = 3600.0

# 除了窗口，还看服务端给的 expires_at：临近过期就重签，不等窗口到期。
_GROUP_SESSION_EXPIRY_MARGIN_SECONDS = 600.0

# {(identity, code): (到期的单调时刻, 令牌响应)}
#
# 键里带 identity 是必须的：退出登录、换账号、换网关之后，上一个身份的令牌既没用
# 也不该留着。指纹变了就自然落不到同一个键上（见 `identity_fingerprint`）。
_group_sessions: Dict[tuple, tuple] = {}


def clear_group_sessions(*, code: str = "") -> None:
    """丢掉缓存的群聊令牌。不传 code 就全清（退出登录/切账号时）。"""
    if not code:
        _group_sessions.clear()
        return
    for key in [k for k in _group_sessions if k[1] == code]:
        _group_sessions.pop(key, None)


def _cache_deadline(expires_at: Any) -> float:
    """这把令牌最晚缓存到哪个**单调时刻**。

    单调时钟而不是墙钟：墙钟会被 NTP 校正、会被用户改，而缓存有效期这种「从现在
    起还能用多久」的问题，用会跳的钟去答就会答错。
    """
    import time

    limit = time.monotonic() + _GROUP_SESSION_MAX_CACHE_SECONDS

    # 服务端给的是 ISO 的本地时间（naive）。解析失败就只靠上面那个窗口 ——
    # 拿不到过期时间不是拒绝缓存的理由，窗口本身已经足够保守。
    try:
        from datetime import datetime

        remaining = (
            datetime.fromisoformat(str(expires_at)) - datetime.now()
        ).total_seconds() - _GROUP_SESSION_EXPIRY_MARGIN_SECONDS
    except (TypeError, ValueError):
        return limit

    return min(limit, time.monotonic() + remaining)


def group_session(code: str) -> Dict[str, Any]:
    """签一把群聊会话令牌，**能复用就复用**。

    它**不比调用者本人多任何权限** —— 唯一的作用是让服务端从凭据本身推得出
    group，从而关掉 MCP 与 skill（决策 #45：能力不跨界，只有产物跨界）。

    复用见上面 `_GROUP_SESSION_MAX_CACHE_SECONDS` 的说明。调用方不需要知道这里
    有缓存：拿到的永远是一把当前可用的令牌，只是不一定是刚签的。
    """
    import time

    key = (identity_fingerprint(), code)
    cached = _group_sessions.get(key)
    if cached and cached[0] > time.monotonic():
        return cached[1]

    session = _post(_path(code, "session"))
    # 没有 api_key 的响应不值得缓存 —— 缓存一个空壳只会让下一轮也拿到空壳。
    if isinstance(session, dict) and session.get("api_key"):
        _group_sessions[key] = (_cache_deadline(session.get("expires_at")), session)
    return session


def lease_turn(code: str, member_id: int, *, after_seq: int = 0,
               reason: str = "mention") -> Dict[str, Any]:
    """要一把执行租约。

    返回体里 `turn` 为 None 是**正常**的：没有该回的事、项目里有别的 turn 在跑、
    跳数到了上限，都长这样。把它当错误处理的话，客户端每次空转都要 catch 一下，
    久了就会有人把 catch 写成忽略全部错误。
    """
    return _post(_path(code, "agents", str(member_id), "turns"),
                 {"after_seq": max(0, int(after_seq or 0)), "reason": reason})


def turn_heartbeat(code: str, turn_id: int, *, status_seq: int) -> Dict[str, Any]:
    return _post(_path(code, "turns", str(turn_id), "heartbeat"), {"status_seq": int(status_seq)})


def turn_reply(code: str, turn_id: int, content: str,
               *, reply_to_id: Optional[int] = None) -> Dict[str, Any]:
    """agent 写回一条群聊消息。身份由服务端从租约推，这里只给正文。"""
    return _post(_path(code, "turns", str(turn_id), "reply"),
                 {"content": content, "reply_to_id": reply_to_id})


def turn_finish(code: str, turn_id: int, *, status: str = "completed",
                error_code: Optional[str] = None,
                reply_message_id: Optional[int] = None) -> Dict[str, Any]:
    """收尾。不调这条的话，项目要等租约过期才能开下一个 turn。"""
    return _post(_path(code, "turns", str(turn_id), "finish"),
                 {"status": status, "error_code": error_code,
                  "reply_message_id": reply_message_id})


def set_respond_to(code: str, member_id: int, respond_to: str,
                   *, allowlist: Optional[List[str]] = None) -> Dict[str, Any]:
    """配置这个 agent 接受谁的指令。判定在服务端，这里只是配置入口。"""
    return _put(_path(code, "members", str(member_id), "respond-to"),
                 {"respond_to": respond_to, "allowlist": allowlist or []})


def archive(code: str) -> Dict[str, Any]:
    """归档项目。服务端会连带撤销这个项目里还在跑的 turn。"""
    return _post(_path(code, "archive"))


def list_turns(code: str) -> Dict[str, Any]:
    """agent 活跃状态。读路径，失败降级成空。"""
    payload = _request(_path(code, "turns"))
    return payload if isinstance(payload, dict) else {"turns": [], "hops": 0}


def update_project(code: str, name: str, description: str = "") -> Dict[str, Any]:
    return _put(_path(code), {"name": name, "description": description})


def remove_member(code: str, member_id: int) -> Dict[str, Any]:
    result = _request(_path(code, "members", str(member_id)), method="DELETE")
    return result if isinstance(result, dict) else {}


def restore(code: str) -> Dict[str, Any]:
    return _post(_path(code, "restore"))


def transfer(code: str, member_id: int) -> Dict[str, Any]:
    return _post(_path(code, "transfer"), {"member_id": member_id})


def presence(code: str, member_id: int) -> Dict[str, Any]:
    return _post(_path(code, "agents", str(member_id), "presence"))


def submit_task_result(code: str, task_id: int, content: str) -> Dict[str, Any]:
    return _post(_path(code, f"tasks/{int(task_id)}/result"), {"content": content})


def accept_task_result(code: str, task_id: int) -> Dict[str, Any]:
    return _post(_path(code, f"tasks/{int(task_id)}/accept"))


def get_task_result(code: str, task_id: int) -> Dict[str, Any]:
    return _request(_path(code, "tasks", str(int(task_id)), "result"))
