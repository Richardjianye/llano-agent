"""Gitea API 的最小适配层：只负责幂等创建私有空仓库。"""

from dataclasses import dataclass
from typing import Optional

import httpx


class GiteaError(RuntimeError):
    pass


@dataclass(frozen=True)
class GiteaRepository:
    id: int
    name: str
    html_url: str


class GiteaClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        owner: str,
        timeout: float = 15.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        if not base_url.strip() or not token.strip() or not owner.strip():
            raise GiteaError("Gitea 地址、令牌或仓库所有者未配置")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.owner = owner
        self.timeout = timeout
        self.transport = transport

    @staticmethod
    def _repository(payload: dict) -> GiteaRepository:
        try:
            return GiteaRepository(
                id=int(payload["id"]),
                name=str(payload["name"]),
                html_url=str(payload["html_url"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise GiteaError("Gitea 返回了不完整的仓库信息") from exc

    async def ensure_private_repository(
        self, *, name: str, description: str = ""
    ) -> GiteaRepository:
        headers = {"Authorization": f"token {self.token}"}
        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=self.timeout,
            transport=self.transport,
        ) as client:
            try:
                created = await client.post(
                    "/api/v1/user/repos",
                    json={
                        "name": name,
                        "description": description[:255],
                        "private": True,
                        "auto_init": False,
                    },
                )
                if created.status_code == 201:
                    return self._repository(created.json())

                # 重试、双 worker 或「远端成功/本地提交前进程退出」都会走到这里。
                if created.status_code in {409, 422}:
                    existing = await client.get(
                        f"/api/v1/repos/{self.owner}/{name}"
                    )
                    if existing.status_code == 200:
                        return self._repository(existing.json())

                raise GiteaError(f"Gitea 建仓返回 HTTP {created.status_code}")
            except httpx.TimeoutException as exc:
                raise GiteaError("连接 Gitea 超时") from exc
            except httpx.HTTPError as exc:
                raise GiteaError("无法连接 Gitea") from exc
