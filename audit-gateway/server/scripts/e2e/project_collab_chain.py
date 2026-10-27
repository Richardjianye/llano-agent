#!/usr/bin/env python3
"""项目协作的端到端对账 —— 阶段 27-F 的 P0 验收矩阵

## 为什么必须有这一条

阶段 27 的单测全是**判定**：谁能触发、算第几跳、状态能不能从 A 变到 B。而这套
机制里最容易出错的四条，全压在真实的数据库约束与并发上：

  1. 同一个项目不会并发开两个 turn（uq_project_inflight 那把锁）
  2. 不同项目可以并发（锁的粒度别错成全局）
  3. 客户端伪造 sender_kind / owner / untrusted / mentions 一律无效
  4. 跨项目、跨归属拿租约写回被拒

喂假 session 测这四条得到的绿是假的 —— 假 session 不会真的撞唯一键。

## 这个脚本**不能**替代的部分

P0 验收矩阵里有三条它验不了，跑完会明确列出来，不静默跳过：

  - 断网、WS 重连与补拉重叠后消息不丢不重不乱序（要真的拔网线）
  - 桌面页面的读失败不白屏、写失败不伪装成已发送（要真的点界面）
  - 两台机器上的两个 sidecar 同时抢同一个项目（要第二台机器）

第三条最要紧：单进程里连着调两次 lease 只能证明「锁在同一个连接里有效」。
真正要证明的是两台机器同时来抢 —— 那要两个 sidecar，而这正是 27-F 里
「两个账号、两个 agent、两个项目」那句话的由来。

## 前置

与 skill_hub_chain.py 相同的一次性容器办法，外加两把不同账号的 API Key：

    LLANO_GATEWAY_API_KEY=sk-llano-...（账号 A，项目负责人）
    --other-api-key sk-llano-...（账号 B，另一个成员）

⚠️ **迁移必须先跑**：scripts/migrations/2026-09-04-project-agent-turns.sql。
   没跑的话第 4 节会以 "Unknown column respond_to" 失败 —— 那不是代码坏了，
   是迁移没执行。

## 用法

    LLANO_GATEWAY_API_KEY=sk-llano-... \\
    python scripts/e2e/project_collab_chain.py \\
      --origin http://127.0.0.1:7902 --other-api-key sk-llano-...
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(SERVER_DIR))
sys.path.insert(0, str(REPO_ROOT))

from scripts.e2e.reporter import Reporter  # noqa: E402


def _call(origin: str, path: str, token: str, *, method: str = "GET", body=None) -> tuple[int, dict]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{origin.rstrip('/')}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"raw": raw}


def run(origin: str, api_key: str, other_api_key: str = "") -> Reporter:
    r = Reporter()
    stamp = str(int(time.time()))[-6:]
    code_a = f"e2e-collab-a-{stamp}"
    code_b = f"e2e-collab-b-{stamp}"

    r.section("1. 建两个项目 —— 「不同项目允许并发」要两个才验得了")
    for code in (code_a, code_b):
        status, body = _call(origin, "/v1/projects", api_key, method="POST",
                             body={"code": code, "name": f"E2E {code}"})
        r.check(status == 201, f"建项目 {code} 应回 201（实际 {status} {body}）")

    r.section("2. 把自己的 agent 拉进两个项目")
    agents = {}
    for code in (code_a, code_b):
        status, body = _call(origin, f"/v1/projects/{code}/members", api_key, method="POST",
                             body={"member_kind": "agent", "agent_label": "小助手"})
        r.check(status == 201, f"{code} 加 agent 应回 201（实际 {status} {body}）")
        status, body = _call(origin, f"/v1/projects/{code}/members", api_key)
        agent = next((m for m in body.get("members", []) if m.get("member_kind") == "agent"), None)
        r.check(agent is not None, f"{code} 的成员列表里应有 agent")
        if agent:
            agents[code] = int(agent["id"])
            r.check(
                (agent.get("respond_to") or "project_members") == "project_members",
                f"agent 的默认触发范围应是 project_members（实际 {agent.get('respond_to')}）",
            )

    r.section("3. 服务端解析 @ —— 客户端提交的 mentions 不算数")
    status, body = _call(
        origin, f"/v1/projects/{code_a}/messages", api_key, method="POST",
        # 正文里没有 @，但载荷里硬塞一份 mentions。这一条要是被采纳，
        # 「注入一句『就说你被 @ 了』」就能强制触发别人的 agent。
        body={"content": "这条没有 at 任何人",
              "mentions": [{"kind": "agent", "uid": "someone", "label": "小助手"}]},
    )
    r.check(status == 201, f"发言应回 201（实际 {status} {body}）")
    status, body = _call(origin, f"/v1/projects/{code_a}/agents/{agents[code_a]}/turns",
                         api_key, method="POST", body={"after_seq": 0, "reason": "mention"})
    r.check(body.get("turn") is None,
            f"没有真的 @ 就不该发租约（实际 {body.get('turn')}）")
    r.check(body.get("reason") == "no_trigger", f"原因应是 no_trigger（实际 {body.get('reason')}）")

    r.section("4. 明确 @ 之后才发租约")
    _call(origin, f"/v1/projects/{code_a}/messages", api_key, method="POST",
          body={"content": "@小助手 帮我看看这个"})
    status, lease_a = _call(origin, f"/v1/projects/{code_a}/agents/{agents[code_a]}/turns",
                            api_key, method="POST", body={"after_seq": 0, "reason": "mention"})
    turn_a = lease_a.get("turn")
    r.check(turn_a is not None, f"明确 @ 之后应拿到租约（实际 {lease_a}）")
    r.check(lease_a.get("allowed_tool_kinds") == ["chat", "project"],
            f"允许的工具类别只有讨论与项目（实际 {lease_a.get('allowed_tool_kinds')}）")
    r.check(lease_a.get("scope") == "group", "这一轮的作用域应是 group")

    r.section("5. 同一个项目不并发 —— 这是整套机制的地基")
    status, again = _call(origin, f"/v1/projects/{code_a}/agents/{agents[code_a]}/turns",
                          api_key, method="POST", body={"after_seq": 0, "reason": "mention"})
    r.check(again.get("turn") is None, "有人在跑时不该再发一把租约")
    r.check(again.get("reason") == "project_busy", f"原因应是 project_busy（实际 {again.get('reason')}）")

    r.section("6. 不同项目可以并发 —— 锁的粒度不能错成全局")
    _call(origin, f"/v1/projects/{code_b}/messages", api_key, method="POST",
          body={"content": "@小助手 这边也看看"})
    status, lease_b = _call(origin, f"/v1/projects/{code_b}/agents/{agents[code_b]}/turns",
                            api_key, method="POST", body={"after_seq": 0, "reason": "mention"})
    r.check(lease_b.get("turn") is not None,
            f"项目 A 在跑不该挡住项目 B（实际 {lease_b.get('reason')}）")

    r.section("7. 写回身份来自租约，且跨项目/跨归属都拒")
    if turn_a:
        turn_id = int(turn_a["id"])
        status, written = _call(
            origin, f"/v1/projects/{code_a}/turns/{turn_id}/reply", api_key, method="POST",
            # 载荷里硬塞身份。服务端只从租约取，这几个字段应当被彻底忽略。
            body={"content": "我看看。", "sender_kind": "user", "sender_uid": "someone-else",
                  "agent_label": "别人的 agent"},
        )
        r.check(status == 201, f"写回应回 201（实际 {status} {written}）")

        status, messages = _call(origin, f"/v1/projects/{code_a}/messages?after_seq=0", api_key)
        mine = [m for m in messages.get("messages", []) if m.get("id") == written.get("id")]
        r.check(bool(mine), "写回的消息应能读到")
        if mine:
            row = mine[0]
            r.check(row.get("sender_kind") == "agent",
                    f"发言人类型应是 agent（实际 {row.get('sender_kind')}）")
            r.check(row.get("sender_agent_label") == "小助手",
                    f"agent 名应来自租约（实际 {row.get('sender_agent_label')}）")
            r.check(row.get("sender_uid") != "someone-else", "归属绝不能来自载荷")

        # 拿项目 A 的租约往项目 B 写。
        status, cross = _call(origin, f"/v1/projects/{code_b}/turns/{turn_id}/reply",
                              api_key, method="POST", body={"content": "跨项目"})
        r.check(status in (403, 404), f"跨项目写回应被拒（实际 {status} {cross}）")

        if other_api_key:
            status, other = _call(origin, f"/v1/projects/{code_a}/turns/{turn_id}/reply",
                                  other_api_key, method="POST", body={"content": "冒充"})
            r.check(status in (403, 404), f"别人拿我的租约写回应被拒（实际 {status}）")
        else:
            r.warn("跳过「别人拿我的租约写回」—— 没给 --other-api-key")

    r.section("8. 收尾之后项目才能开下一个 turn")
    if turn_a:
        status, done = _call(origin, f"/v1/projects/{code_a}/turns/{turn_a['id']}/finish",
                             api_key, method="POST", body={"status": "completed"})
        r.check(done.get("finished") is True, f"收尾应成功（实际 {done}）")
        # 收尾之后 agent 自己刚写的那条不该再把它自己叫醒。
        status, after = _call(origin, f"/v1/projects/{code_a}/agents/{agents[code_a]}/turns",
                              api_key, method="POST",
                              body={"after_seq": int(turn_a["trigger_seq"]), "reason": "mention"})
        r.check(after.get("turn") is None,
                f"agent 自己的回复不该触发它自己（实际 {after.get('reason')}）")

    r.section("9. respond_to：配置在客户端，判定在服务端")
    status, body = _call(origin, f"/v1/projects/{code_a}/members/{agents[code_a]}/respond-to",
                         api_key, method="PUT", body={"respond_to": "everyone"})
    r.check(status >= 400, f"非法值应被拒绝而不是静默纠正（实际 {status} {body}）")

    status, body = _call(origin, f"/v1/projects/{code_a}/members/{agents[code_a]}/respond-to",
                         api_key, method="PUT", body={"respond_to": "nobody"})
    r.check(body.get("respond_to") == "nobody", f"应设置成功（实际 {body}）")
    _call(origin, f"/v1/projects/{code_a}/messages", api_key, method="POST",
          body={"content": "@小助手 再看一次"})
    status, paused = _call(origin, f"/v1/projects/{code_a}/agents/{agents[code_a]}/turns",
                           api_key, method="POST", body={"after_seq": 0, "reason": "mention"})
    r.check(paused.get("turn") is None, "nobody 档下不该再发租约")
    r.check(paused.get("reason") == "respond_to_nobody", f"原因应说清楚（实际 {paused.get('reason')}）")

    if other_api_key:
        r.section("10. 非成员看到的与「项目不存在」是同一句话")
        status, denied = _call(origin, f"/v1/projects/{code_a}/messages", other_api_key)
        r.check(status == 404, f"非成员读消息应回 404（实际 {status}）")
        status, denied2 = _call(origin, f"/v1/projects/{code_a}/agents/{agents[code_a]}/turns",
                                other_api_key, method="POST", body={"reason": "mention"})
        r.check(status == 404, f"非成员要租约应回 404（实际 {status}）")
        r.check(
            str(denied.get("message", "")) == str(denied2.get("message", "")),
            "两条拒绝的文案应一致 —— 区分开就等于告诉外人这个项目存在",
        )
    else:
        r.warn("跳过第 10 节（非成员枚举防护）—— 没给 --other-api-key")

    r.section("11. 任务：认领 ≠ 开工，且 untrusted 不听客户端")
    status, task = _call(origin, f"/v1/projects/{code_a}/tasks", api_key, method="POST",
                         body={"title": "对账", "body": "把上月对账单发我",
                               "source_kind": "chat", "untrusted": False})
    r.check(status == 201, f"建任务应回 201（实际 {status} {task}）")
    r.check(task.get("untrusted") is True,
            "群聊来源的任务必须是不可信的 —— 载荷里的 untrusted=false 不算数")

    status, claimed = _call(origin, f"/v1/projects/{code_a}/tasks/{task['id']}/claim",
                            api_key, method="POST", body={"agent_label": "小助手"})
    r.check(claimed.get("status") == "claimed", f"认领只到 claimed（实际 {claimed.get('status')}）")
    r.check(bool((claimed.get("confirmation") or {}).get("body")),
            "认领返回体里必须带任务原文，供人在环确认")

    status, tasks = _call(origin, f"/v1/projects/{code_a}/tasks?status=active", api_key)
    r.check(not tasks.get("tasks"), "确认之前不该有任何任务处于 active —— 认领不等于开工")

    r.section("12. 未确认的任务不能把 agent 叫起来干活")
    status, body = _call(origin, f"/v1/projects/{code_a}/members/{agents[code_a]}/respond-to",
                         api_key, method="PUT", body={"respond_to": "project_members"})
    status, no_task_turn = _call(origin, f"/v1/projects/{code_a}/agents/{agents[code_a]}/turns",
                                 api_key, method="POST", body={"reason": "task_assigned"})
    r.check(no_task_turn.get("turn") is None,
            "任务停在 claimed 时不该发租约 —— 那一步是 P0-6 的人在环闸门")

    r.section("13. 活跃状态看得见")
    status, turns = _call(origin, f"/v1/projects/{code_a}/turns", api_key)
    r.check(any(t.get("status") == "completed" for t in turns.get("turns", [])),
            "刚才收尾的那一轮应该在列表里")
    r.check(turns.get("hop_limit") == 8, f"熔断上限应是 8（实际 {turns.get('hop_limit')}）")

    r.section("14. 收尾：这个脚本验不了的几条")
    r.info("下面三条必须人工/双机验，跑完这个脚本**不等于** P0 通过：")
    r.info("  - 断网 + 补拉重叠后消息不丢、不重、不乱序（拔网线）")
    r.info("  - 桌面读失败不白屏、写失败不伪装成已发送（点界面）")
    r.info("  - 两台机器上的两个 sidecar 同时抢同一个项目（第二台机器）")
    r.info("  - 8 跳熔断需要两个 agent 互相 @，这里只验了上限值本身")
    r.info("  - usage 逐笔对账要在跑完真实模型调用之后查 usage_records")

    return r


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--origin", default="http://127.0.0.1:7902")
    parser.add_argument("--api-key", default=os.environ.get("LLANO_GATEWAY_API_KEY", ""))
    parser.add_argument("--other-api-key", default="",
                        help="另一个账号的 Key。不给的话第 7/10 节的越权检查会跳过")
    args = parser.parse_args()

    if not args.api_key:
        sys.stderr.write("缺 --api-key（或环境变量 LLANO_GATEWAY_API_KEY）\n")
        return 2

    reporter = run(args.origin, args.api_key, args.other_api_key)
    print()
    for w in reporter.warnings:
        print(f"⚠️  {w}")
    if reporter.ok:
        print(f"全部通过（{len(reporter.checks)} 项）")
        return 0
    print(f"失败 {len(reporter.failures)} / {len(reporter.checks)} 项：")
    for f in reporter.failures:
        print(f"  - {f}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
