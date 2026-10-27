"""会话附件的抽取与落盘（决策 #8「含上传文件/图片记录」）

## 为什么要有这一层

多模态请求里的图片/文件基本都是 base64 内联的：

    {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0K..."}}

一张手机截图 base64 之后 2~5 MB。原样落进 `conversation_logs.request_messages`
会出两个问题：

1. 那一列直接被撑爆（且 `CONTENT_MAX_CHARS` 截断时会把后面的文字一起吃掉，
   审计员打开详情只看到半串 base64，最想看的文字反而没了）；
2. 想按关键词检索会话内容时，索引和 LIKE 都被这堆 base64 拖死。

所以这里做一次「拆信封」：附件本体抽出去单独存，原文里换成
`[[attachment:短码]]` 占位符，详情页再按短码把两边对上。

## 三条边界

1. **只抽发送方向。** 接收方向只存不改（决策 #9），模型回复里的内容原样保留。
2. **远程 URL 不抓取。** 客户端可能传 `https://...` 而不是 base64。网关主动去
   下载用户给的任意地址是个实打实的 SSRF 面（内网元数据服务、局域网设备），
   为审计完整性开这个口子不划算 —— 只记 URL，标 `stored=False`。
3. **落盘失败不影响对话。** 磁盘满、权限不对都只记 `store_error`，
   元数据照样入库。审计员看到「他发过一个 3 MB 的 xlsx，但网关没存下来」，
   仍然比什么都没有强得多。

## 抽取出来的东西不回流给上游

`extract_attachments` 返回的是**审计副本**，占位符只存在于这份副本里。
发给上游的仍是原始 messages —— 否则模型收到的是 `[[attachment:ab12cd]]`，
用户会以为图片没上传成功。调用方（router/v1/chat.py）务必分清这两份数据。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

# data URL 的形状：data:<mime>[;charset=...][;base64],<数据>
_DATA_URL_RE = re.compile(r"^data:([^;,]*)(;[^,]*)?,(.*)$", re.DOTALL)

# 短码长度：sha256 取前 12 位。审计场景下的碰撞概率可以忽略，
# 而占位符要短到不干扰阅读——正文里塞 64 个十六进制字符没人愿意看。
MARKER_LEN = 12

# 常见 MIME → 扩展名。落盘文件名带上扩展名，审计员双击就能打开，
# 不用先猜这是什么格式。认不出来的一律 .bin。
_EXT_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/svg+xml": ".svg",
    "application/pdf": ".pdf",
    "application/json": ".json",
    "application/zip": ".zip",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "text/markdown": ".md",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/mp4": ".m4a",
}

KIND_IMAGE = "image"
KIND_AUDIO = "audio"
KIND_FILE = "file"

# 只有不会携带脚本的栅格图片允许在管理后台同源内联展示。
# SVG/HTML/PDF 等即便扩展名看起来正常，也可能包含主动内容；它们必须下载。
SAFE_INLINE_MIME_TYPES = frozenset({
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/gif",
    "image/webp",
    "image/bmp",
})


def is_safe_inline_mime(mime: Optional[str]) -> bool:
    """附件能否在管理后台源下安全地 inline 展示"""
    return (mime or "").lower().strip() in SAFE_INLINE_MIME_TYPES


@dataclass
class ExtractedAttachment:
    """一个从消息里抽出来的附件

    `data` 只在内存里活到落盘那一刻，绝不进数据库、绝不进日志。
    """

    marker: str
    kind: str = KIND_FILE
    mime_type: Optional[str] = None
    filename: Optional[str] = None
    size_bytes: int = 0
    sha256: Optional[str] = None
    origin_url: Optional[str] = None
    data: Optional[bytes] = field(default=None, repr=False)

    # 落盘结果，由 store_attachments 回填
    storage_path: Optional[str] = None
    stored: bool = False
    store_error: Optional[str] = None

    @property
    def is_remote(self) -> bool:
        """远程 URL 形态：只有链接没有字节"""
        return self.data is None and bool(self.origin_url)


def _ext_for(mime: Optional[str], filename: Optional[str]) -> str:
    if filename and "." in filename:
        ext = os.path.splitext(filename)[1]
        # 挡住 `.tar.gz.` 这类畸形值和超长扩展名，别让它进文件路径
        if 1 < len(ext) <= 10 and ext[1:].isalnum():
            return ext.lower()
    return _EXT_BY_MIME.get((mime or "").lower().strip(), ".bin")


def _kind_for(mime: Optional[str], part_type: Optional[str]) -> str:
    mime = (mime or "").lower()
    if mime.startswith("image/") or part_type == "image_url":
        return KIND_IMAGE
    if mime.startswith("audio/") or part_type == "input_audio":
        return KIND_AUDIO
    return KIND_FILE


def _decode_data_url(url: str) -> Optional[Tuple[str, bytes]]:
    """解 data URL，返回 (mime, 原始字节)。不是 data URL 或解不开返回 None

    容错要宽：客户端拼串时漏个 padding、换行没去掉都很常见，
    这里用 validate=False 让 base64 忽略非法字符，实在解不开才放弃。
    """
    match = _DATA_URL_RE.match(url.strip())
    if not match:
        return None

    mime = (match.group(1) or "application/octet-stream").strip()
    params = (match.group(2) or "").lower()
    payload = match.group(3)

    if "base64" not in params:
        # 非 base64 的 data URL（百分号编码的纯文本）几乎不会出现在多模态请求里，
        # 按 UTF-8 收下就行
        return mime, payload.encode("utf-8", errors="replace")

    try:
        # 去掉换行等空白后再解，padding 不足的补齐
        cleaned = re.sub(r"\s+", "", payload)
        cleaned += "=" * (-len(cleaned) % 4)
        return mime, base64.b64decode(cleaned, validate=False)
    except (binascii.Error, ValueError) as exc:
        logger.warning(f"附件 data URL 解码失败，按远程链接记录: {exc}")
        return None


def _marker_of(payload: bytes | str) -> str:
    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    return hashlib.sha256(raw).hexdigest()[:MARKER_LEN]


def _placeholder(att: ExtractedAttachment) -> str:
    """正文里留下的占位符

    带上人可读的描述而不是光秃秃一个 id：审计员扫列表时不点开详情
    也能知道「这里有张 240KB 的 png」。
    """
    size = f"{att.size_bytes}B" if att.size_bytes else "远程"
    name = att.filename or att.kind
    return f"[[attachment:{att.marker} {name} {att.mime_type or '未知类型'} {size}]]"


# ==================== 抽取 ====================


def extract_attachments(
    messages: List[dict],
) -> Tuple[List[dict], List[ExtractedAttachment]]:
    """把 messages 拆成「审计副本 + 附件列表」

    审计副本里所有内联附件都被换成占位符，可以安全地 JSON 序列化入库。
    **原 messages 不会被修改**，发给上游的仍然是原件。

    支持三种 OpenAI 兼容的多模态形态：

    | part.type | 数据位置 |
    | --------- | -------- |
    | `image_url` | `image_url.url`（data URL 或 https 链接） |
    | `input_audio` | `input_audio.data`（裸 base64）+ `.format` |
    | `file` | `file.file_data`（data URL）+ `file.filename` |
    """
    if not messages:
        return messages or [], []

    attachments: List[ExtractedAttachment] = []
    result: List[dict] = []

    for message in messages:
        if not isinstance(message, dict):
            result.append(message)
            continue

        content = message.get("content")
        if not isinstance(content, list):
            # 纯文本消息不可能带附件，原样放行
            result.append(message)
            continue

        item = dict(message)
        parts: List[Any] = []
        touched = False

        for part in content:
            if not isinstance(part, dict):
                parts.append(part)
                continue

            extracted = _extract_part(part)
            if extracted is None:
                parts.append(part)
                continue

            attachments.append(extracted)
            touched = True
            # 换成一个纯文本片段：审计副本要能被关键词检索，
            # 保留原结构反而让 LIKE 查询扫到一堆无意义的 JSON 键名
            parts.append({"type": "text", "text": _placeholder(extracted)})

        if touched:
            item["content"] = parts
            result.append(item)
        else:
            result.append(message)

    return result, attachments


def _extract_part(part: Dict[str, Any]) -> Optional[ExtractedAttachment]:
    """从单个 content part 里抽附件，认不出来返回 None"""
    part_type = part.get("type")

    # ---- image_url：最常见的一种 ----
    if part_type == "image_url" or "image_url" in part:
        holder = part.get("image_url")
        url = holder.get("url") if isinstance(holder, dict) else holder
        if not isinstance(url, str) or not url:
            return None
        return _from_url(url, part_type="image_url")

    # ---- input_audio：裸 base64，没有 data: 前缀 ----
    if part_type == "input_audio" or "input_audio" in part:
        holder = part.get("input_audio")
        if not isinstance(holder, dict):
            return None
        raw = holder.get("data")
        if not isinstance(raw, str) or not raw:
            return None
        fmt = str(holder.get("format") or "wav").lower()
        try:
            data = base64.b64decode(raw + "=" * (-len(raw) % 4), validate=False)
        except (binascii.Error, ValueError):
            return None
        mime = f"audio/{'mpeg' if fmt == 'mp3' else fmt}"
        marker = _marker_of(data)
        return ExtractedAttachment(
            marker=marker,
            kind=KIND_AUDIO,
            mime_type=mime,
            filename=f"{marker}{_ext_for(mime, None)}",
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            data=data,
        )

    # ---- file：带文件名的附件 ----
    if part_type == "file" or "file" in part:
        holder = part.get("file")
        if not isinstance(holder, dict):
            return None
        filename = holder.get("filename")
        raw = holder.get("file_data") or holder.get("data") or holder.get("file_url")
        if not isinstance(raw, str) or not raw:
            # 只给了 file_id（上游文件接口的引用）——没有内容可存，但要留痕
            file_id = holder.get("file_id")
            if not file_id:
                return None
            return ExtractedAttachment(
                marker=_marker_of(str(file_id)),
                kind=KIND_FILE,
                filename=filename if isinstance(filename, str) else str(file_id),
                origin_url=f"file_id:{file_id}",
            )
        return _from_url(
            raw,
            part_type="file",
            filename=filename if isinstance(filename, str) else None,
        )

    return None


def _from_url(
    url: str, part_type: str, filename: Optional[str] = None
) -> Optional[ExtractedAttachment]:
    decoded = _decode_data_url(url)

    if decoded is None:
        # 远程链接：只记不抓（见模块开头第 2 条边界）
        return ExtractedAttachment(
            marker=_marker_of(url),
            kind=_kind_for(None, part_type),
            filename=filename or os.path.basename(url.split("?")[0]) or None,
            # URL 可能很长（预签名地址带一堆参数），截断到列宽以内
            origin_url=url[:1024],
        )

    mime, data = decoded
    marker = _marker_of(data)
    return ExtractedAttachment(
        marker=marker,
        kind=_kind_for(mime, part_type),
        mime_type=mime,
        filename=filename or f"{marker}{_ext_for(mime, filename)}",
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        data=data,
    )


# ==================== 落盘 ====================


def _safe_name(att: ExtractedAttachment) -> str:
    """落盘文件名：哈希打头 + 干净的扩展名

    **不直接用客户端给的文件名**：那是用户可控的字符串，
    `../../etc/passwd` 或者一个 300 字符的中文名都能把落盘搞出事。
    原始文件名照样存进数据库（展示用），但不参与拼路径。
    """
    ext = _ext_for(att.mime_type, att.filename)
    return f"{(att.sha256 or att.marker)}{ext}"


def store_attachments(
    attachments: List[ExtractedAttachment],
    root: str,
    day: Optional[date] = None,
    max_bytes: int = 0,
    enabled: bool = True,
) -> None:
    """把附件本体写到磁盘，结果回填进对象

    目录结构 `<root>/<YYYY-MM>/<sha256前2位>/<sha256>.<ext>`：
    按月分目录方便按留存期整目录删（合规动作里要写死留存期限）；
    再按哈希前缀打散，避免单目录几十万文件把 ls 和备份拖死。

    去重：同哈希的文件已存在就不重写，但每次请求仍各记一行 —— 审计要的是
    「谁在什么时候发的」，不是「这个文件存在过」。

    整个函数不抛异常：存不下来是可以接受的降级，让用户的对话请求失败不行。
    """
    if not attachments:
        return

    for att in attachments:
        if att.data is None:
            # 远程链接，本来就没有字节可写
            continue

        if not enabled:
            att.store_error = "配置未开启附件落盘"
            continue

        if max_bytes and att.size_bytes > max_bytes:
            # 只记元数据。审计员看到「3 MB 的 xlsx 未留存」也是有效信息
            att.store_error = f"超过单文件上限（{att.size_bytes} > {max_bytes}）"
            continue

        try:
            bucket = (day or date.today()).strftime("%Y-%m")
            shard = (att.sha256 or att.marker)[:2]
            rel = os.path.join(bucket, shard, _safe_name(att))
            abs_path = os.path.join(root, rel)

            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            if not os.path.exists(abs_path):
                # 先写临时文件再改名：中途崩溃不会留下半个文件被当成完整附件
                tmp = f"{abs_path}.tmp"
                with open(tmp, "wb") as fp:
                    fp.write(att.data)
                os.replace(tmp, abs_path)

            att.storage_path = rel.replace(os.sep, "/")
            att.stored = True
        except OSError as exc:
            att.store_error = str(exc)[:255]
            logger.error(f"附件落盘失败 marker={att.marker}: {exc}")
        finally:
            # 无论成败都把字节丢掉，别让它跟着 UsageFacts 在内存里飘
            att.data = None


def resolve_storage_path(root: str, storage_path: str) -> Optional[str]:
    """把库里的相对路径还原成绝对路径，并确认没跑出 root

    下载接口拿的是数据库里的字符串，理论上可信；但只要有一处写入没走
    `store_attachments`，这里就是最后一道防线。多这四行比事后追责便宜。
    """
    if not storage_path:
        return None

    abs_root = os.path.abspath(root)
    abs_path = os.path.abspath(os.path.join(abs_root, storage_path))

    if not abs_path.startswith(abs_root + os.sep):
        logger.error(f"附件路径越界，已拒绝: {storage_path}")
        return None

    return abs_path if os.path.isfile(abs_path) else None
