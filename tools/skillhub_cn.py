"""Public SkillHub catalog adapter. Never uses company authentication."""
from __future__ import annotations

import io
import json
import re
import stat
import ssl
import hashlib
import time
import threading
import zipfile
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from tools.skills_hub import SkillSource, SkillMeta, SkillBundle, _validate_bundle_rel_path

API_ORIGIN = "https://api.skillhub.cn"
WEB_ORIGIN = "https://www.skillhub.cn"
MAX_DOWNLOAD = 8 * 1024 * 1024
MAX_UNPACKED = 4 * 1024 * 1024
_RECOMMENDATION_CACHE = None
_RECOMMENDATION_LOCK = threading.Lock()


def _read(path: str, maximum: int = MAX_DOWNLOAD) -> bytes:
    # Only fixed, adapter-owned API paths. No gateway credential or arbitrary URL.
    req = Request(API_ORIGIN + path, headers={"Accept": "application/json", "User-Agent": "llano-skill-catalog/1"})
    import truststore
    with urlopen(req, timeout=15, context=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)) as response:
        raw = response.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("SkillHub response exceeds size limit")
    return raw


def _json(path: str) -> dict:
    data = json.loads(_read(path))
    if not isinstance(data, dict):
        raise ValueError("Invalid SkillHub response")
    return data


def coordinates(identifier: str) -> tuple[str, str]:
    parts = identifier.split("/")
    if len(parts) == 2 and parts[0] == "skillhub":
        namespace, slug = "", parts[1]
    elif len(parts) == 3 and parts[0] == "skillhub":
        namespace, slug = parts[1], parts[2]
    else:
        raise ValueError("Invalid SkillHub identifier")
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", slug) or (namespace and not re.fullmatch(r"[a-zA-Z0-9_-]+", namespace)):
        raise ValueError("Invalid SkillHub coordinate")
    return namespace, slug


def _meta(item: dict, namespace: dict | None = None) -> SkillMeta:
    slug = str(item.get("slug") or "")
    ns = (namespace or item.get("namespace") or {}).get("handle") or ""
    ident = f"skillhub/{ns}/{slug}" if ns else f"skillhub/{slug}"
    coordinates(ident)
    return SkillMeta(name=str(item.get("displayName") or item.get("name") or slug),
        description=str(item.get("summary_zh") or item.get("description_zh") or item.get("summary") or item.get("description") or ""),
        source="skillhub", identifier=ident, trust_level="community",
        extra={"detail_url": f"{WEB_ORIGIN}/skills/{quote(slug)}" + ("?" + urlencode({"namespace": ns}) if ns else ""),
               "version": item.get("version"),
               "download_count": item.get("downloadCount", item.get("downloads"))})


def unpack_skill_zip(raw: bytes) -> dict[str, bytes]:
    """Validate every member before extraction; never write unscanned files."""
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = [m for m in archive.infolist() if not m.is_dir()]
        if not members or len(members) > 64 or sum(m.file_size for m in members) > MAX_UNPACKED:
            raise ValueError("Skill package exceeds file/size limit")
        files = {}
        for member in members:
            path = _validate_bundle_rel_path(member.filename)
            if stat.S_ISLNK(member.external_attr >> 16) or member.file_size > 1024 * 1024:
                raise ValueError("Unsafe skill archive member")
            if path.casefold() in {p.casefold() for p in files}:
                raise ValueError("Duplicate skill archive path")
            files[path] = archive.read(member)
    if "SKILL.md" not in files:
        roots = [p[:-len("SKILL.md")] for p in files if p.endswith("/SKILL.md")]
        if len(roots) != 1 or not all(p.startswith(roots[0]) for p in files):
            raise ValueError("Archive must contain one complete skill")
        files = {p[len(roots[0]):]: content for p, content in files.items()}
    return files


class SkillHubCnSource(SkillSource):
    def source_id(self):
        return "skillhub"

    def trust_level_for(self, identifier):
        return "community"

    def recommended(self, offset=0, limit=12):
        # The showcase API returns one complete recommendation snapshot, not
        # server-side pages. Cache it and serve the requested page locally.
        global _RECOMMENDATION_CACHE
        with _RECOMMENDATION_LOCK:
            if _RECOMMENDATION_CACHE is None or time.monotonic() >= _RECOMMENDATION_CACHE[0]:
                data = _json("/api/v1/showcase/recommended")
                items = data.get("skills")
                if not isinstance(items, list):
                    raise ValueError("SkillHub recommendation list is missing")
                _RECOMMENDATION_CACHE = (time.monotonic() + 300, items)
            items = _RECOMMENDATION_CACHE[1]
        return [_meta(i) for i in items[offset:offset + limit]], len(items)

    def packages(self, page=1, limit=6):
        data = _json("/api/v1/skillsets?" + urlencode({"page": page, "pageSize": limit}))
        items = data.get("skillSets")
        if not isinstance(items, list):
            raise ValueError("SkillHub package list is missing")
        return {"packages": [{"slug": i["slug"], "name": i.get("displayName") or i["slug"],
            "description": i.get("summary") or "", "scene": i.get("scene") or "",
            "skill_count": i.get("skillCount", len(i.get("skills") or [])),
            "detail_url": WEB_ORIGIN + "/skillspackage/" + quote(i["slug"], safe="")}
            for i in items], "total": int(data.get("total") or len(items))}

    def search(self, query, limit=10):
        if not query.strip():
            return self.recommended(limit=limit)[0]
        data = _json("/api/skills?" + urlencode({"page": 1, "pageSize": limit, "keyword": query}))
        if data.get("code") != 0 or not isinstance(data.get("data", {}).get("skills"), list):
            raise ValueError("Invalid SkillHub search response")
        return [_meta(i) for i in data["data"]["skills"]]

    def inspect(self, identifier):
        if not identifier.startswith("skillhub/"):
            return None
        namespace, slug = coordinates(identifier)
        data = _json("/api/v1/skills/" + quote(slug, safe="") + "?" + urlencode({"namespace": namespace}))
        item = data["skill"]
        meta = _meta(item, data.get("namespace"))
        meta.extra["version"] = (data.get("latestVersion") or {}).get("version")
        return meta

    def fetch(self, identifier):
        if not identifier.startswith("skillhub/"):
            return None
        namespace, slug = coordinates(identifier)
        meta = self.inspect(identifier)
        # A source-qualified local name prevents a company skill from being overwritten.
        name = "skillhub-" + (namespace + "-" if namespace else "") + slug
        if len(name) > 100:
            name = name[:80] + "-" + hashlib.sha256(identifier.encode()).hexdigest()[:12]
        params = {"slug": slug, "namespace": namespace}
        if meta.extra.get("version"):
            params["version"] = str(meta.extra["version"])
        files = unpack_skill_zip(_read("/api/v1/download?" + urlencode(params)))
        return SkillBundle(name=name, files=files, source="skillhub", identifier=identifier,
            trust_level="community", metadata={"source_url": meta.extra["detail_url"], "display_name": meta.name,
                                               "description": meta.description})
