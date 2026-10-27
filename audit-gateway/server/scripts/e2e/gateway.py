"""网关客户端：登录拿 JWT，之后统一带 Authorization。

从 `e2e_audit_check.py` 原样抽出，行为不变，只把原先直接调用模块级 `warn()`
改成走注入的 Reporter —— 抽取的目的就是让对账逻辑不再依赖某个脚本的全局状态。
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, List, Optional

try:
    import httpx
except ImportError:  # pragma: no cover
    sys.exit("缺少 httpx，请先 pip install -r requirements.txt")

from .reporter import Reporter


class Gateway:
    """薄封装：登录拿 JWT，之后统一带 Authorization"""

    def __init__(self, base_url: str, reporter: Optional[Reporter] = None):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.AsyncClient(timeout=180, follow_redirects=True)
        self.token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.reporter = reporter or Reporter()

    async def close(self) -> None:
        if self.token:
            try:
                await self.client.post(
                    f"{self.base_url}/admin/user/logout",
                    headers=self._headers(),
                    json={"refresh_token": self.refresh_token},
                )
            except httpx.HTTPError as exc:
                self.reporter.warn("E2E 登录令牌自动撤销失败", str(exc))
        self.token = None
        self.refresh_token = None
        await self.client.aclose()

    def _headers(self, key: Optional[str] = None) -> Dict[str, str]:
        token = key or self.token
        return {"Authorization": f"Bearer {token}"} if token else {}

    async def login(self, username: str, password: str) -> Dict[str, Any]:
        """登录并取回当前账号信息

        /login 只返回两个 token，**不返回用户信息** —— 角色和部门要另外调
        /info 才拿得到。第一版脚本直接从登录响应里读 username/permission，
        于是打印出一串 None，还误报「当前账号没有 audit:view」，
        而实际上那是总裁账号、审计接口全都跑通了。
        误报比不报更糟，这里补上第二次调用。
        """
        resp = await self.client.post(
            f"{self.base_url}/admin/user/login",
            json={"username": username, "password": password},
        )
        body = resp.json()
        if body.get("code") != 0:
            sys.exit(f"登录失败：{body.get('message')}")

        self.token = body["data"]["access_token"]
        self.refresh_token = body["data"]["refresh_token"]
        return await self.admin_get("/admin/user/info")

    async def admin_get(self, path: str, **params) -> Dict[str, Any]:
        """管理端接口，统一 {code,message,data} 信封"""
        resp = await self.client.get(
            f"{self.base_url}{path}",
            headers=self._headers(),
            params={k: v for k, v in params.items() if v is not None},
        )
        body = resp.json()
        if body.get("code") != 0:
            raise RuntimeError(f"{path} 返回 {body.get('code')}：{body.get('message')}")
        return body["data"]

    async def raw_get(self, path: str) -> "httpx.Response":
        """文件流接口（附件、导出），不套信封"""
        return await self.client.get(
            f"{self.base_url}{path}", headers=self._headers()
        )

    async def redaction_policy(self) -> Dict[str, Any]:
        resp = await self.client.get(
            f"{self.base_url}/v1/redaction-policy",
            headers=self._headers(),
        )
        resp.raise_for_status()
        return resp.json()

    async def chat(
        self,
        messages: List[dict],
        model: str,
        stream: bool = False,
        abort_after: Optional[int] = None,
        audit_messages: Optional[List[dict]] = None,
        policy_version: Optional[str] = None,
    ) -> Dict[str, Any]:
        """模拟客户端发对话请求 —— 走的是和真实客户端完全相同的入口

        `abort_after=N` 表示收到 N 个内容块之后**主动断开**，
        用来复现用户点「停止生成」。这条路径上曾经会丢账，必须能测。
        """
        payload = {"model": model, "messages": messages, "stream": stream}
        if audit_messages is not None:
            payload["_llano_audit"] = {
                "policy_version": policy_version or "e2e-unknown",
                "messages": audit_messages,
            }

        if not stream:
            resp = await self.client.post(
                f"{self.base_url}/v1/chat/completions",
                headers=self._headers(),
                json=payload,
            )
            return resp.json()

        # 流式：把 SSE 收完，顺便确认最后一个 chunk 带 usage
        collected: List[str] = []
        usage = None
        aborted = False

        async with self.client.stream(
            "POST",
            f"{self.base_url}/v1/chat/completions",
            headers=self._headers(),
            json=payload,
        ) as resp:
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if chunk.get("usage"):
                    usage = chunk["usage"]
                for choice in chunk.get("choices") or []:
                    piece = (choice.get("delta") or {}).get("content")
                    if piece:
                        collected.append(piece)

                if abort_after is not None and len(collected) >= abort_after:
                    # 直接跳出并关掉连接 —— 这正是「停止生成」在协议层的样子
                    aborted = True
                    break

        return {"text": "".join(collected), "usage": usage, "aborted": aborted}
