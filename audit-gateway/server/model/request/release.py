"""桌面端版本发布请求体

发布接口收的是**离线签名机的产物原样**：`sign_release.py` 输出的 JSON 里就是
`{"manifest": {...}, "signature": "..."}` 两个键，直接贴进来即可。

这里刻意不逐字段建模。清单的字段规则只有一份真源
（services/release_manifest.normalize_manifest），在 pydantic 里再抄一遍
就有了第二份会漂的规则，而两份规则不一致时的表现是「后台说合法、客户端验不过」。
"""

from typing import Any, Dict

from pydantic import BaseModel, Field


class ReleasePublish(BaseModel):
    manifest: Dict[str, Any] = Field(..., description="签名机产出的清单原文")
    signature: str = Field(..., min_length=1, max_length=256, description="Ed25519 签名（base64）")


class ReleaseStatusUpdate(BaseModel):
    status: int = Field(..., ge=0, le=1, description="1 上架 / 0 下架")
