"""附件抽取与落盘的纯逻辑测试

重点覆盖三件「错了不会报错、只会悄悄丢数据」的事：

1. **抽取不能改动原 messages**：改了的话上游收到的是占位符，
   用户会以为图片没上传成功 —— 而且这个 bug 只在多模态请求上出现，
   日常文本对话完全测不出来。
2. **审计副本必须显著变短**：抽取的全部意义就是别让 base64 撑爆正文列。
   要是哪天有人改坏了匹配规则，这条断言会立刻红。
3. **落盘失败必须降级而不是抛异常**：附件存不下来可以接受，
   让用户的对话请求跟着失败不行。
"""

from __future__ import annotations

import base64
import json
import os

import pytest
from services.attachments import (KIND_AUDIO, KIND_FILE, KIND_IMAGE,
                                  extract_attachments, is_safe_inline_mime,
                                  resolve_storage_path, store_attachments)


def data_url(mime: str, payload: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(payload).decode()}"


PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 2000
XLSX = b"PK\x03\x04" + b"y" * 500


def multimodal_messages() -> list:
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "看看这张图，还有这份表"},
                {"type": "image_url", "image_url": {"url": data_url("image/png", PNG)}},
                {
                    "type": "file",
                    "file": {
                        "filename": "季度报表.xlsx",
                        "file_data": data_url(
                            "application/vnd.openxmlformats-officedocument"
                            ".spreadsheetml.sheet",
                            XLSX,
                        ),
                    },
                },
            ],
        },
        {"role": "assistant", "content": "好的"},
    ]


# ==================== 抽取 ====================


def test_原始消息不被修改():
    """发给上游的必须是原件

    这是整个模块最重要的一条：抽取产出的是**审计副本**。
    如果原 messages 被就地改成占位符，模型收到的就是
    `[[attachment:ab12cd]]`，用户会以为图片没上传成功。
    """
    messages = multimodal_messages()
    before = json.dumps(messages, ensure_ascii=False)

    extract_attachments(messages)

    assert json.dumps(messages, ensure_ascii=False) == before


def test_审计副本显著变短():
    """抽取的全部意义：别让 base64 撑爆 conversation_logs 的正文列"""
    messages = multimodal_messages()
    audit, _ = extract_attachments(messages)

    original = len(json.dumps(messages, ensure_ascii=False))
    stripped = len(json.dumps(audit, ensure_ascii=False))

    assert stripped * 4 < original, "占位符替换没生效，base64 还留在正文里"
    assert "base64" not in json.dumps(audit, ensure_ascii=False)


def test_抽取图片与文件():
    _, attachments = extract_attachments(multimodal_messages())

    assert len(attachments) == 2

    image, file = attachments
    assert image.kind == KIND_IMAGE
    assert image.mime_type == "image/png"
    assert image.size_bytes == len(PNG)
    assert image.filename.endswith(".png")

    assert file.kind == KIND_FILE
    # 客户端给的原始文件名要原样保留（展示用），但不参与拼路径
    assert file.filename == "季度报表.xlsx"
    assert file.size_bytes == len(XLSX)


def test_占位符里带得上人可读信息():
    """审计员扫列表时不点开详情也该知道「这里有张图」"""
    audit, attachments = extract_attachments(multimodal_messages())
    text = json.dumps(audit, ensure_ascii=False)

    for att in attachments:
        assert f"[[attachment:{att.marker}" in text
    assert "季度报表.xlsx" in text


def test_相同内容短码一致():
    """短码取自内容哈希：同一张图反复发送，磁盘上只占一份"""
    _, first = extract_attachments(multimodal_messages())
    _, second = extract_attachments(multimodal_messages())

    assert [a.marker for a in first] == [a.marker for a in second]
    assert first[0].sha256 == second[0].sha256


def test_远程链接只记不抓():
    """网关主动下载用户给的任意地址是个 SSRF 面，不为审计完整性开这个口子"""
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "https://x.test/a.jpg?sig=1"}}
            ],
        }
    ]
    _, attachments = extract_attachments(messages)

    assert len(attachments) == 1
    att = attachments[0]
    assert att.is_remote
    assert att.origin_url.startswith("https://x.test/")
    assert att.data is None
    assert att.sha256 is None


def test_抽取音频():
    raw = base64.b64encode(b"RIFFxxxx").decode()
    messages = [
        {
            "role": "user",
            "content": [{"type": "input_audio", "input_audio": {"data": raw, "format": "wav"}}],
        }
    ]
    _, attachments = extract_attachments(messages)

    assert attachments[0].kind == KIND_AUDIO
    assert attachments[0].mime_type == "audio/wav"


def test_纯文本消息原样放行():
    messages = [{"role": "user", "content": "你好"}]
    audit, attachments = extract_attachments(messages)

    assert attachments == []
    assert audit == messages


def test_畸形输入不抛异常():
    """客户端什么都可能传。抽取炸了会让整条对话请求失败，代价太大"""
    messages = [
        None,
        {"role": "user", "content": None},
        {"role": "user", "content": [None, 42, {"type": "image_url"}]},
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": ""}}]},
        {"role": "user", "content": [{"type": "file", "file": {}}]},
    ]
    audit, attachments = extract_attachments(messages)

    assert attachments == []
    assert len(audit) == len(messages)


def test_坏掉的base64退化成远程记录():
    """解不开就当链接记一笔，总好过整条附件凭空消失"""
    messages = [
        {
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,!!!"}}],
        }
    ]
    _, attachments = extract_attachments(messages)

    # base64 容错很宽，解得开就是空字节；解不开则退化成远程记录。
    # 无论走哪条路，都必须留下一行记录 —— 这才是断言的重点
    assert len(attachments) == 1


# ==================== 落盘 ====================


def test_落盘与去重(tmp_path):
    _, attachments = extract_attachments(multimodal_messages())
    store_attachments(attachments, root=str(tmp_path))

    assert all(a.stored for a in attachments)
    for att in attachments:
        assert os.path.isfile(os.path.join(str(tmp_path), att.storage_path))
        # 落盘名用哈希而不是客户端给的文件名：那是用户可控字符串
        assert att.sha256 in att.storage_path

    # 再存一遍同样的内容不该多出文件
    files_before = sum(len(f) for _, _, f in os.walk(str(tmp_path)))
    _, again = extract_attachments(multimodal_messages())
    store_attachments(again, root=str(tmp_path))
    files_after = sum(len(f) for _, _, f in os.walk(str(tmp_path)))

    assert files_before == files_after


def test_超过上限只记元数据(tmp_path):
    """审计员看到「3MB 的 xlsx 未留存」，仍比什么都没有强"""
    _, attachments = extract_attachments(multimodal_messages())
    store_attachments(attachments, root=str(tmp_path), max_bytes=600)

    image, file = attachments
    assert not image.stored and "超过单文件上限" in image.store_error
    assert file.stored  # 小的那个照存不误
    # 关键：没存下来的那个，元数据仍然是全的
    assert image.size_bytes == len(PNG) and image.sha256


def test_关掉落盘开关仍留元数据(tmp_path):
    _, attachments = extract_attachments(multimodal_messages())
    store_attachments(attachments, root=str(tmp_path), enabled=False)

    assert not any(a.stored for a in attachments)
    assert all(a.sha256 and a.size_bytes for a in attachments)


def test_落盘失败不抛异常(tmp_path):
    """磁盘满、权限不对都只降级。让对话请求跟着失败是不可接受的"""
    _, attachments = extract_attachments(multimodal_messages())
    blocked = tmp_path / "blocked"
    blocked.write_text("我是个文件，不是目录")

    store_attachments(attachments, root=str(blocked))

    assert not any(a.stored for a in attachments)
    assert all(a.store_error for a in attachments)


def test_字节在落盘后被丢弃(tmp_path):
    """别让几 MB 的 bytes 跟着 UsageFacts 在内存里飘"""
    _, attachments = extract_attachments(multimodal_messages())
    store_attachments(attachments, root=str(tmp_path))

    assert all(a.data is None for a in attachments)


# ==================== 路径越界 ====================


@pytest.mark.parametrize(
    "evil",
    ["../../../etc/passwd", "/etc/passwd", "2026-08/../../../etc/passwd"],
)
def test_拒绝越界路径(tmp_path, evil):
    """库里的路径理论上可信，但只要有一处写入没走 store_attachments，
    这里就是最后一道防线"""
    assert resolve_storage_path(str(tmp_path), evil) is None


def test_正常路径能还原(tmp_path):
    _, attachments = extract_attachments(multimodal_messages())
    store_attachments(attachments, root=str(tmp_path))

    resolved = resolve_storage_path(str(tmp_path), attachments[0].storage_path)
    assert resolved and os.path.isfile(resolved)


def test_文件不存在返回None(tmp_path):
    assert resolve_storage_path(str(tmp_path), "2026-08/ab/nope.png") is None


@pytest.mark.parametrize("mime", ["image/png", "image/jpeg", "image/webp"])
def test_只有安全栅格图片允许同源预览(mime):
    assert is_safe_inline_mime(mime)


@pytest.mark.parametrize(
    "mime",
    ["image/svg+xml", "text/html", "application/pdf", "application/octet-stream"],
)
def test_主动内容必须下载而非同源预览(mime):
    assert not is_safe_inline_mime(mime)
