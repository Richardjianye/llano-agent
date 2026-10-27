"""静态产物的私有文件存储、包校验和逐资源访问判断。"""

import io
import json
import mimetypes
import re
import shutil
import stat
import uuid
import zipfile
from pathlib import Path, PurePosixPath

from config import get_settings
from fastapi import HTTPException
from fastapi.responses import FileResponse

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_UNPACKED_BYTES = 40 * 1024 * 1024
MAX_FILES = 200
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_PATH_LENGTH = 220
SAFE_SUFFIXES = frozenset({
    ".html", ".htm", ".css", ".js", ".mjs", ".json", ".txt", ".md", ".csv",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".avif",
    ".woff", ".woff2", ".ttf", ".otf", ".pdf", ".mp4", ".webm", ".mp3",
})
_UID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PUBLIC_CSP = (
    "sandbox allow-scripts; default-src 'self' data: blob:; "
    "script-src 'self' 'unsafe-inline' 'unsafe-eval' blob:; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
    "media-src 'self' data: blob:; font-src 'self' data:; "
    "connect-src 'none'; form-action 'none'; base-uri 'none'; "
    "frame-ancestors 'self'"
)
_RESTRICTED_CSP = (
    "sandbox allow-same-origin; default-src 'self' data: blob:; "
    "script-src 'none'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; media-src 'self' data: blob:; "
    "font-src 'self' data:; connect-src 'none'; form-action 'none'; "
    "base-uri 'none'; frame-ancestors 'self'"
)


def storage_root() -> Path:
    configured = get_settings().artifact_storage_path
    if not configured:
        raise HTTPException(503, "静态产物存储尚未配置")
    root = Path(configured).expanduser().resolve()
    server_root = Path(__file__).resolve().parents[1]
    public_roots = (server_root / "static", server_root.parent / "client" / "dist")
    if any(root == public or root.is_relative_to(public) or public.is_relative_to(root)
           for public in public_roots):
        raise HTTPException(503, "静态产物存储不能与公开文件目录重叠")
    return root


def normalized_path(name: str) -> str:
    """只收普通相对路径；磁盘读取还要和数据库清单双重匹配。"""
    if (not name or len(name) > MAX_PATH_LENGTH or "\\" in name or "\x00" in name
            or name.startswith("/") or name.endswith("/")):
        raise HTTPException(400, "网站包内含无效路径")
    path = PurePosixPath(name)
    if (any(part in {"", ".", ".."} for part in name.split("/"))
            or path.suffix.lower() not in SAFE_SUFFIXES
            or any(part.startswith(".") for part in path.parts)):
        raise HTTPException(400, "网站包内含不允许的文件路径或类型")
    return path.as_posix()


def unpack_upload(filename: str, content: bytes) -> dict[str, bytes]:
    if not content or len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, "文件为空或超过 20 MB")
    if filename.lower().endswith((".html", ".htm")):
        if len(content) > MAX_FILE_BYTES:
            raise HTTPException(400, "单页超过 10 MB")
        return {"index.html": content}
    if not filename.lower().endswith(".zip"):
        raise HTTPException(400, "仅支持 HTML 文件或 ZIP 网站包")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            files: dict[str, bytes] = {}
            folded_paths: set[str] = set()
            total = 0
            if len(entries) > MAX_FILES + 20:
                raise HTTPException(400, "网站包文件数量超过限制")
            for item in entries:
                if item.is_dir():
                    continue
                mode = (item.external_attr >> 16) & 0xFFFF
                if stat.S_IFMT(mode) not in (0, stat.S_IFREG):
                    raise HTTPException(400, "网站包不允许链接或特殊文件")
                name = normalized_path(item.filename)
                if name.casefold() in folded_paths:
                    raise HTTPException(400, "网站包内存在重复路径")
                folded_paths.add(name.casefold())
                if item.flag_bits & 0x1:
                    raise HTTPException(400, "不支持加密网站包")
                total += item.file_size
                if item.file_size > MAX_FILE_BYTES or total > MAX_UNPACKED_BYTES:
                    raise HTTPException(400, "网站包解压后超出大小限制")
                if len(files) >= MAX_FILES:
                    raise HTTPException(400, "网站包文件数量超过限制")
                data = archive.read(item)
                if len(data) != item.file_size:
                    raise HTTPException(400, "网站包文件长度不匹配")
                files[name] = data
    except (zipfile.BadZipFile, RuntimeError, EOFError) as exc:
        raise HTTPException(400, "ZIP 网站包损坏或不受支持") from exc
    if "index.html" not in files:
        raise HTTPException(400, "网站包根目录必须包含 index.html")
    if len(json.dumps(sorted(files), ensure_ascii=False).encode("utf-8")) > 60000:
        raise HTTPException(400, "网站包文件路径清单超过限制")
    return files


def write_version(root: Path, site_id: str, version: int, files: dict[str, bytes]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    site_root = root / site_id
    site_root.mkdir(exist_ok=True)
    staging = site_root / (".staging-" + uuid.uuid4().hex)
    destination = site_root / str(version)
    if destination.exists():
        raise FileExistsError("网站版本目录已存在")
    try:
        staging.mkdir()
        for name, data in files.items():
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        staging.rename(destination)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def discard_version(root: Path, site_id: str, version: int) -> None:
    shutil.rmtree(root / site_id / str(version), ignore_errors=True)


def allowed(site, user) -> bool:
    if site.status != "published" or site.published_version is None:
        return False
    if user and user.uid == site.owner_uid:
        return True
    if site.visibility == "public":
        return True
    if not user:
        return False
    if site.visibility == "users":
        return user.uid in json.loads(site.allowed_users)
    if site.visibility == "depts":
        return bool(user.dept_code and user.dept_code in json.loads(site.allowed_depts))
    return False


def file_response(root: Path, site_id: str, version: int, manifest: list[str], name: str,
                  *, public: bool = False):
    try:
        path = normalized_path(name)
    except HTTPException:
        raise HTTPException(404, "资源不存在") from None
    if path not in manifest:
        raise HTTPException(404, "资源不存在")
    candidate = root / site_id / str(version) / path
    base = (root / site_id / str(version)).resolve()
    try:
        candidate.resolve().relative_to(base)
    except ValueError:
        raise HTTPException(404, "资源不存在") from None
    if not candidate.is_file():
        raise HTTPException(404, "资源不存在")
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    if path.endswith((".html", ".htm")):
        mime = "text/html; charset=utf-8"
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
               "Content-Security-Policy": _PUBLIC_CSP if public else _RESTRICTED_CSP,
               "Referrer-Policy": "no-referrer",
               "X-Frame-Options": "SAMEORIGIN"}
    return FileResponse(candidate, media_type=mime, headers=headers)


def check_acl_shape(visibility: str, users: list[str], depts: list[str]) -> None:
    if visibility not in {"private", "users", "depts", "public"}:
        raise HTTPException(400, "访问范围无效")
    if len(users) > 100 or len(depts) > 100 or len(users) != len(set(users)) or len(depts) != len(set(depts)):
        raise HTTPException(400, "访问名单重复或超过限制")
    if any(not _UID.fullmatch(item) for item in users + depts):
        raise HTTPException(400, "访问名单包含无效标识")
    if visibility == "users" and not users:
        raise HTTPException(400, "指定用户范围不能为空")
    if visibility == "depts" and not depts:
        raise HTTPException(400, "指定部门范围不能为空")
