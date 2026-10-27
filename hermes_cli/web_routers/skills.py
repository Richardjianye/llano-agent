"""Skills dashboard routes (extracted verbatim from web_server.py).

Two routers because the original registration points are far apart and global
route order matters: ``hub_router`` (the skills-hub install/search/scan
endpoints) was registered before the profiles ``router`` include in
web_server, the plain skills CRUD ``router`` after it - each is mounted at
its original registration point.

Handler bodies are byte-identical; web_server-owned helpers are reached via
the late-binding seam in :mod:`hermes_cli.web_deps` so tests that
``monkeypatch.setattr(web_server, "_spawn_hermes_action", ...)`` keep
working.
"""

import asyncio  # noqa: F401 — used by handlers
import logging
from typing import Optional  # noqa: F401

from fastapi import APIRouter, HTTPException  # noqa: F401

from hermes_cli.web_deps import late, LateState
from hermes_cli.web_models import (
    SkillContentUpdate,
    SkillCreate,
    SkillInstallRequest,
    SkillToggle,
    SkillUninstallRequest,
    SkillsUpdateRequest,
)

# Same logger the handlers used before extraction (identical logger object).
_log = logging.getLogger("hermes_cli.web_server")

hub_router = APIRouter()
router = APIRouter()
_PRIVATE_DISCOVERY_SOURCES = frozenset({"local", "installed", "agent"})


def _localized(rows, profile=None, *, title_field="name", repair_local=False, retry=False):
    from tools.skill_chinese_metadata import localize_rows, has_chinese
    from tools.llano_skill_hub import hub_display_metadata
    from pathlib import Path
    with _profile_scope(profile), _config_profile_scope(profile):
        server_metadata = hub_display_metadata() if title_field == "display_name" else {}
        directories = {}
        if title_field == "display_name":
            from agent.skill_utils import get_all_skills_dirs, iter_skill_index_files
            from tools.skills_tool import _parse_frontmatter, skill_matches_platform, skill_matches_environment, MAX_NAME_LENGTH
            # Same resolution and first-name-wins ordering as the skill index.
            # Scan once, including frontmatter aliases; dirname is not identity.
            for root in get_all_skills_dirs():
                for definition in iter_skill_index_files(root, "SKILL.md"):
                    try:
                        meta, _ = _parse_frontmatter(definition.read_text(encoding="utf-8-sig", errors="replace")[:4000])
                        if not skill_matches_platform(meta) or not skill_matches_environment(meta):
                            continue
                        name = meta.get("name", definition.parent.name)[:MAX_NAME_LENGTH]
                        directories.setdefault(name, definition.parent.resolve())
                    except (OSError, TypeError, ValueError):
                        continue
        prepared = []
        for original in rows:
            row = dict(original)
            if title_field == "display_name":
                directory = directories.get(row.get("name"))
                if directory:
                    row["skill_dir"] = str(directory)
                    owned = server_metadata.get(row["skill_dir"])
                    if owned:
                        if not has_chinese(row.get("display_name")):
                            row["display_name"] = owned["name"]
                        if not has_chinese(row.get("description")):
                            row["description"] = owned["description"]
                        row["provenance"] = "hub"
            elif row.get("source") == "llano-hub":
                row["provenance"] = "hub"
            prepared.append(row)
        localized = localize_rows(prepared, title_field=title_field)
        if repair_local:
            from tools.local_skill_metadata import can_complete, schedule_metadata
            missing = [original for original, row in zip(prepared, localized) if row.get("metadata_incomplete")]
            # 打开列表只读：AI 补全由同事点按钮（retry=True）发起，不在每次浏览时花钱。
            pending = schedule_metadata(missing, retry=retry, enqueue=retry)
            completable = {original.get("name") for original in missing if can_complete(original)}
            for row in localized:
                row["metadata_pending"] = row.get("name") in pending
                row["metadata_completable"] = row.get("name") in completable
        for original, row in zip(rows, localized):
            if "skill_dir" not in original:
                row.pop("skill_dir", None)
        return localized

# Late-bound web_server helpers (resolved at call time; cycle-safe,
# monkeypatch-transparent).
_clear_skills_prompt_cache = late("_clear_skills_prompt_cache")
_config_profile_scope = late("_config_profile_scope")
_hub_action_name = late("_hub_action_name")
_installed_hub_identifiers = late("_installed_hub_identifiers")
_profile_cli_args = late("_profile_cli_args")
_profile_scope = late("_profile_scope")
_skill_meta_to_payload = late("_skill_meta_to_payload")
_spawn_hermes_action = late("_spawn_hermes_action")
load_config = late("load_config")

# Live proxies for web_server-owned module state (mutations/monkeypatches
# on web_server remain authoritative; resolved at operation time).
_SKILL_HUB_SOURCE_LABELS = LateState("_SKILL_HUB_SOURCE_LABELS")
# Config read-modify-write serialization for off-loop handlers (see the
# definition in web_server.py). LateState supports ``with``-blocks, so this
# is the live lock object, not a frozen import-time copy.
_CONFIG_MUTATION_LOCK = LateState("_CONFIG_MUTATION_LOCK")


from pydantic import BaseModel, Field


class SkillImportRequest(BaseModel):
    name: str = Field(default="", max_length=100)
    # CUSTOM（2026-09-21）：单个 SKILL.md / 文件夹走 files；.zip 走 archive（base64）；
    # overwrite=True 是同事在「已经有同名技能」的提示上点了「覆盖」之后的重试。
    files: dict[str, str] = Field(default_factory=dict, max_length=64)
    archive: Optional[str] = Field(default=None, max_length=9_000_000)
    overwrite: bool = False
    # CUSTOM（2026-09-23）：品牌版缺中文名称 / 中文简介时，界面弹框让同事补填后带着重试
    display_name: Optional[str] = Field(default=None, max_length=200)
    description: Optional[str] = Field(default=None, max_length=1000)
    profile: Optional[str] = None


@hub_router.post("/api/skills/hub/import")
async def import_downloaded_skill(body: SkillImportRequest, profile: Optional[str] = None):
    def run():
        from tools.skill_import import import_skill_bundle
        with _profile_scope(body.profile or profile):
            return import_skill_bundle(
                body.name, body.files, archive=body.archive, overwrite=body.overwrite,
                display_name=body.display_name, description=body.description,
            )
    try:
        result = await asyncio.to_thread(run)
    except (ValueError, OSError) as exc:
        from tools.skill_import import SkillExists, SkillNeedsChineseMeta
        # 409 让界面能问「要覆盖吗」；428 让界面弹框补中文名称和简介；其余是 400
        if isinstance(exc, SkillExists):
            status = 409
        elif isinstance(exc, SkillNeedsChineseMeta):
            status = 428
        else:
            status = 400
        raise HTTPException(status, str(exc)) from exc
    if not result["success"]:
        raise HTTPException(422, result.get("report") or result["error"])
    # 新会话立即看得到刚导入的技能（与 skill_manage 写入后同一处理）。
    _clear_skills_prompt_cache()
    return result


@hub_router.post("/api/skills/hub/install")
async def install_skill_hub(body: SkillInstallRequest, profile: Optional[str] = None):
    identifier = (body.identifier or "").strip()
    if not identifier:
        raise HTTPException(status_code=400, detail="identifier is required")
    name = _hub_action_name("install", identifier)
    try:
        proc = _spawn_hermes_action(
            _profile_cli_args(body.profile or profile)
            + ["skills", "install", identifier, "--yes"],
            name,
        )
    except HTTPException:
        raise
    except Exception as exc:
        _log.exception("Failed to spawn skills install")
        raise HTTPException(status_code=500, detail=f"Failed to install skill: {exc}")
    return {"ok": True, "pid": proc.pid, "name": name}


@hub_router.post("/api/skills/hub/uninstall")
async def uninstall_skill_hub(body: SkillUninstallRequest, profile: Optional[str] = None):
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    action = _hub_action_name("uninstall", name)
    try:
        proc = _spawn_hermes_action(
            _profile_cli_args(body.profile or profile) + ["skills", "uninstall", name, "--yes"],
            action,
        )
    except HTTPException:
        raise
    except Exception as exc:
        _log.exception("Failed to spawn skills uninstall")
        raise HTTPException(status_code=500, detail=f"Failed to uninstall skill: {exc}")
    return {"ok": True, "pid": proc.pid, "name": action}


@hub_router.post("/api/skills/hub/update")
async def update_skills_hub(
    body: Optional[SkillsUpdateRequest] = None, profile: Optional[str] = None
):
    try:
        effective = (body.profile if body else None) or profile
        proc = _spawn_hermes_action(
            _profile_cli_args(effective) + ["skills", "update"], "skills-update"
        )
    except HTTPException:
        raise
    except Exception as exc:
        _log.exception("Failed to spawn skills update")
        raise HTTPException(status_code=500, detail=f"Failed to update skills: {exc}")
    return {"ok": True, "pid": proc.pid, "name": "skills-update"}


class CompanySkillUpdateRequest(BaseModel):
    """CUSTOM（2026-10-16）：更新一个已装公司技能；本机改过时须显式确认先备份。"""
    name: str = Field(min_length=1, max_length=128)
    backup_local_changes: bool = False
    profile: Optional[str] = None


class CompanySkillKeepRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    bundle_hash: str = Field(min_length=1, max_length=128)
    profile: Optional[str] = None


def _company_update_call(profile, fn):
    """同步执行（结果要回给界面），按资料档案隔离；网关错误转 502，业务拒绝转 4xx。"""
    async def run():
        def work():
            with _profile_scope(profile), _config_profile_scope(profile):
                return fn()
        try:
            return await asyncio.to_thread(work)
        except Exception as exc:
            from tools.llano_skill_hub import SkillHubUnavailable
            from tools.llano_skill_update import LocalChanges
            if isinstance(exc, LocalChanges):
                raise HTTPException(409, str(exc)) from exc
            if isinstance(exc, SkillHubUnavailable):
                raise HTTPException(502, str(exc)) from exc
            if isinstance(exc, (ValueError, OSError)):
                raise HTTPException(400, str(exc)) from exc
            _log.exception("company skill update failed")
            raise HTTPException(500, f"更新失败：{exc}") from exc
    return run()


@hub_router.post("/api/skills/hub/company/update")
async def update_company_skill(body: CompanySkillUpdateRequest, profile: Optional[str] = None):
    from tools.llano_skill_update import update_skill
    return await _company_update_call(
        body.profile or profile,
        lambda: update_skill(body.name, backup_local_changes=body.backup_local_changes),
    )


@hub_router.post("/api/skills/hub/company/keep")
async def keep_local_company_skill(body: CompanySkillKeepRequest, profile: Optional[str] = None):
    from tools.llano_skill_update import keep_local_version

    def keep():
        keep_local_version(body.name, body.bundle_hash)
        return {"ok": True}
    return await _company_update_call(body.profile or profile, keep)


@hub_router.get("/api/skills/hub/company/updates")
async def company_skill_update_count(profile: Optional[str] = None):
    """侧栏「技能与工具」小红点：有新版本的公司技能数（只读、不取包、失败记 0）。"""
    from tools.llano_skill_update import pending_update_count
    return await _company_update_call(profile, lambda: {"count": pending_update_count()})


@hub_router.post("/api/skills/hub/company/update-all")
async def update_all_company_skills(profile: Optional[str] = None):
    from tools.llano_skill_update import update_all
    return await _company_update_call(profile, update_all)


@hub_router.get("/api/skills/hub/sources")
async def list_skills_hub_sources(profile: Optional[str] = None, metadata_only: bool = False):
    """Source metadata and installed provenance.

    Desktop discovery loads catalogs independently with metadata_only=True.
    Legacy dashboard clients retain their existing company/featured payloads.
    """

    def _run():
        from tools.skills_hub import create_source_router

        with _config_profile_scope(profile):
            sources = [src for src in create_source_router() if src.source_id() not in _PRIVATE_DISCOVERY_SOURCES]
        out = []
        index_available = False
        featured = []
        company = []
        for src in sources:
            sid = src.source_id()
            entry = {
                "id": sid,
                "label": _SKILL_HUB_SOURCE_LABELS.get(sid, sid),
            }
            # GitHub exposes a rate-limit flag; the index an availability flag.
            if sid == "github":
                try:
                    entry["rate_limited"] = bool(getattr(src, "is_rate_limited", False))
                except Exception:
                    entry["rate_limited"] = False
            if not metadata_only and sid == "llano-hub":
                try:
                    company = [
                        _skill_meta_to_payload(m)
                        for m in src.search("", limit=100)
                    ]
                except Exception:
                    # Source discovery is an inventory endpoint.  A company-hub
                    # outage must not hide public sources or the provenance of
                    # already-installed skills from the same response.
                    company = []
                    _log.warning(
                        "company skills hub unavailable while listing sources",
                        exc_info=True,
                    )
            if sid == "hermes-index":
                try:
                    # ``metadata_only`` skips catalog rows, not routing
                    # metadata.  Desktop search needs this availability bit to
                    # know the index already covers the public API sources;
                    # defaulting it to false fans every query out to GitHub,
                    # skills.sh, ClawHub, LobeHub, and well-known endpoints.
                    index_available = bool(getattr(src, "is_available", False))
                    entry["available"] = index_available
                    if index_available and not metadata_only:
                        featured = [
                            _skill_meta_to_payload(m)
                            for m in src.search("", limit=12)
                        ]
                except Exception:
                    # Treat a broken index as unavailable so the public source
                    # adapters stay searchable instead of failing the entire
                    # endpoint (the same fallback as an absent index).
                    index_available = False
                    entry["available"] = False
                    featured = []
                    _log.warning(
                        "public skills index unavailable while listing sources",
                        exc_info=True,
                    )
            out.append(entry)
        # Tell the UI which sources are worth searching individually (for its
        # progressive per-source fan-out). Mirror parallel_search_sources: when
        # the centralized index is available it already subsumes the external
        # API sources, so they're redundant — skipping them avoids ~70 GitHub
        # calls per keystroke. Keep this set in sync with that function's
        # ``_api_source_ids``.
        _api_source_ids = frozenset(
            {"github", "skills-sh", "clawhub", "lobehub", "well-known"}
        )
        for entry in out:
            entry["searchable"] = not (index_available and entry["id"] in _api_source_ids)
        return {
            "sources": out,
            "index_available": index_available,
            "featured": _localized(featured, profile),
            "company": _localized(company, profile),
            "installed": _installed_hub_identifiers(profile),
        }

    try:
        return await asyncio.to_thread(_run)
    except HTTPException:
        raise
    except Exception as exc:
        _log.exception("skills hub sources listing failed")
        raise HTTPException(status_code=502, detail=f"Hub sources failed: {exc}")


@hub_router.get("/api/skills/hub/mine")
async def my_published_skills(profile: Optional[str] = None):
    """「我发布的技能」：网关上本人首发的技能 + 本机对应的技能和未发布改动。

    本机对应关系只用本机已有的记录（发布回执、公司技能安装登记、同名技能目录），
    不向网关上报本机技能列表。
    """
    def run():
        from tools.llano_skill_hub import my_skills
        with _profile_scope(profile), _config_profile_scope(profile):
            rows = my_skills()
            return {"skills": [{**row, **_local_publish_state(row.get("slug") or "")} for row in rows]}
    try:
        return await asyncio.to_thread(run)
    except HTTPException:
        raise
    except Exception as exc:
        from tools.llano_skill_hub import SkillHubUnavailable
        if isinstance(exc, SkillHubUnavailable):
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        _log.exception("my published skills failed")
        raise HTTPException(status_code=502, detail=f"读取我发布的技能失败：{exc}") from exc


def _local_publish_state(slug: str) -> dict:
    """本机哪个技能对应这个 slug，以及它在上次发布后有没有改过。"""
    import json
    from pathlib import Path
    from hermes_constants import get_hermes_home
    from tools.skill_manager_tool import _find_skill
    from tools.skills_guard import content_hash
    if not slug:
        return {"local_name": None, "local_changed": False}
    receipt_path = Path(get_hermes_home()) / "skills" / ".hub-published" / f"{slug}.json"
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        receipt = {}
    found = _find_skill(slug) or _find_skill(slug.replace("-", "_"))
    if not found:
        return {"local_name": None, "local_changed": False}
    directory = Path(found["path"])
    directory = directory.parent if directory.is_file() else directory
    changed = False
    baseline = receipt.get("content_hash") if isinstance(receipt, dict) else None
    if baseline:
        try:
            changed = content_hash(directory) != baseline
        except OSError:
            changed = False
    return {"local_name": directory.name, "local_changed": changed}


@hub_router.get("/api/skills/hub/catalog")
async def skill_hub_catalog(section: str = "company", department_id: Optional[int] = None,
                            offset: int = 0, limit: int = 6, profile: Optional[str] = None):
    """Independent discovery sections: a public outage cannot hide company skills."""
    if section not in {"company", "recommended", "packages"}:
        raise HTTPException(400, "Unknown catalog section")
    limit, offset = min(max(limit, 1), 100), max(offset, 0)

    def run():
        from tools.skills_hub import create_source_router
        from tools.llano_skill_hub import _request, _to_meta
        from urllib.parse import urlencode
        with _config_profile_scope(profile):
            sources = {s.source_id(): s for s in create_source_router()}
            sid = "llano-hub" if section == "company" else "skillhub"
            if sid not in sources:
                raise HTTPException(404, "Catalog source is unavailable")
            if section == "company":
                params = {"offset": offset, "limit": limit}
                if department_id is not None:
                    params["department_id"] = department_id
                data = _request("/v1/skill-hub/catalog?" + urlencode(params))
                for dept in data["departments"]:
                    dept["skills"] = _localized([_skill_meta_to_payload(_to_meta(m)) for m in dept["skills"]], profile)
                return data
            if section == "packages":
                result = sources[sid].packages(page=offset // limit + 1, limit=limit)
                result["packages"] = _localized(result["packages"], profile)
                return result
            items, total = sources[sid].recommended(offset=offset, limit=limit)
            return {"skills": _localized([_skill_meta_to_payload(m) for m in items], profile), "total": total}
    try:
        return await asyncio.to_thread(run)
    except HTTPException:
        raise
    except Exception as exc:
        _log.warning("Skill catalog %s failed: %s", section, exc)
        raise HTTPException(502, f"Catalog unavailable: {exc}") from exc


@hub_router.get("/api/skills/hub/search")
async def search_skills_hub(
    q: str = "", source: str = "all", limit: int = 20, profile: Optional[str] = None
):
    """Search the skill hub across all configured sources.

    Network-bound (parallel source search); runs in a thread so the FastAPI
    loop isn't blocked.  Returns structured results the UI installs by
    identifier via POST /api/skills/hub/install, previews via
    /api/skills/hub/preview, and scans via /api/skills/hub/scan.
    """
    query = (q or "").strip()
    if not query:
        return {"results": [], "source_counts": {}, "timed_out": [], "installed": {}}

    def _run():
        from tools.skills_hub import create_source_router, parallel_search_sources

        with _config_profile_scope(profile):
            sources = [src for src in create_source_router() if src.source_id() not in _PRIVATE_DISCOVERY_SOURCES]
        capped = min(max(limit, 1), 50)
        all_results, source_counts, timed_out = parallel_search_sources(
            sources, query=query, source_filter=source or "all", overall_timeout=30
        )

        # Dedupe by identifier, preferring higher trust (mirrors unified_search).
        _rank = {"builtin": 2, "trusted": 1, "community": 0}
        seen = {}
        for r in all_results:
            if r.source in _PRIVATE_DISCOVERY_SOURCES or r.identifier.split("/", 1)[0].split(":", 1)[0] in _PRIVATE_DISCOVERY_SOURCES:
                continue
            if r.identifier not in seen:
                seen[r.identifier] = r
            elif _rank.get(r.trust_level, 0) > _rank.get(seen[r.identifier].trust_level, 0):
                seen[r.identifier] = r
        deduped = list(seen.values())[:capped]

        return {
            "results": _localized([_skill_meta_to_payload(m) for m in deduped], profile),
            "source_counts": source_counts,
            "timed_out": timed_out,
            "installed": _installed_hub_identifiers(profile),
        }

    try:
        return await asyncio.to_thread(_run)
    except HTTPException:
        raise
    except Exception as exc:
        _log.exception("skills hub search failed")
        raise HTTPException(status_code=502, detail=f"Hub search failed: {exc}")


@hub_router.get("/api/skills/hub/preview")
async def preview_skill_hub(identifier: str = "", profile: Optional[str] = None):
    """Fetch a hub skill's SKILL.md content + metadata for in-dashboard reading.

    Resolves the identifier across configured sources (same path the CLI
    installer uses), then returns the rendered SKILL.md text and the file
    manifest WITHOUT installing anything.  This is the 'read the actual skill
    before installing' affordance the Browse-hub tab was missing.

    Scoped to ``profile`` so a non-default profile with different hub taps
    resolves against ITS source router, not the default profile's.
    """
    ident = (identifier or "").strip()
    if not ident:
        raise HTTPException(status_code=400, detail="identifier is required")

    def _run():
        from hermes_cli.skills_hub import _resolve_source_meta_and_bundle
        from tools.skills_hub import create_source_router

        with _config_profile_scope(profile):
            sources = create_source_router()
            meta, bundle, _src = _resolve_source_meta_and_bundle(ident, sources)
        if not bundle and not meta:
            return None

        files = {}
        skill_md = ""
        if bundle:
            for rel, content in (bundle.files or {}).items():
                if isinstance(content, bytes):
                    # Some sources (e.g. official optional skills) store every
                    # file as bytes.  Decode text so SKILL.md / docs render;
                    # only fall back to a placeholder for genuinely-binary data.
                    try:
                        files[rel] = content.decode("utf-8")
                    except UnicodeDecodeError:
                        files[rel] = "(binary file)"
                else:
                    files[rel] = content
            skill_md = files.get("SKILL.md", "") or ""

        m = meta or bundle
        return {
            "name": getattr(m, "name", ident),
            "description": getattr(m, "description", "") or "",
            "source": getattr(m, "source", "") or "",
            "identifier": getattr(m, "identifier", ident) or ident,
            "trust_level": getattr(m, "trust_level", "community") or "community",
            "repo": getattr(m, "repo", None),
            "tags": list(getattr(m, "tags", None) or []),
            "skill_md": skill_md,
            "files": sorted(files.keys()),
        }

    try:
        result = await asyncio.to_thread(_run)
    except Exception as exc:
        _log.exception("skills hub preview failed")
        raise HTTPException(status_code=502, detail=f"Hub preview failed: {exc}")
    if result is None:
        raise HTTPException(status_code=404, detail=f"Skill not found: {ident}")
    return result


@hub_router.get("/api/skills/hub/scan")
async def scan_skill_hub(identifier: str = "", profile: Optional[str] = None):
    """Run the install-time security scan on a hub skill WITHOUT installing it.

    Fetches the bundle, quarantines it, and runs the same `scan_skill` /
    `should_allow_install` pipeline the CLI installer uses — then cleans up the
    quarantine.  Returns the verdict, per-finding detail, trust tier, and the
    install-policy decision so the dashboard can show a visual safety result
    on demand (the 'scan' button the Browse-hub tab was missing).

    Scoped to ``profile`` so the bundle resolves against that profile's hub
    source router, matching where an install would pull it from.
    """
    ident = (identifier or "").strip()
    if not ident:
        raise HTTPException(status_code=400, detail="identifier is required")

    def _run():
        import shutil as _shutil

        from hermes_cli.skills_hub import _resolve_source_meta_and_bundle
        from tools.skills_hub import create_source_router, quarantine_bundle
        from tools.skills_guard import scan_skill, should_allow_install

        with _config_profile_scope(profile):
            sources = create_source_router()
            meta, bundle, _src = _resolve_source_meta_and_bundle(ident, sources)
        if not bundle:
            return None

        if bundle.source == "official":
            scan_source = "official"
        else:
            scan_source = (
                getattr(bundle, "identifier", "")
                or getattr(meta, "identifier", "")
                or ident
            )

        q_path = None
        try:
            q_path = quarantine_bundle(bundle)
            result = scan_skill(q_path, source=scan_source)
        finally:
            if q_path is not None:
                _shutil.rmtree(q_path, ignore_errors=True)

        allowed, reason = should_allow_install(result, force=False)
        # `allowed` may be None ("ask") for agent-created/dangerous gates.
        if allowed is True:
            policy = "allow"
        elif allowed is None:
            policy = "ask"
        else:
            policy = "block"

        findings = [
            {
                "severity": f.severity,
                "category": f.category,
                "file": f.file,
                "line": f.line,
                "description": f.description,
            }
            for f in result.findings
        ]
        # Per-severity tally for an at-a-glance summary.
        counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        for f in result.findings:
            if f.severity in counts:
                counts[f.severity] += 1

        return {
            "name": result.skill_name,
            "identifier": ident,
            "source": result.source,
            "trust_level": result.trust_level,
            "verdict": result.verdict,
            "summary": result.summary,
            "policy": policy,
            "policy_reason": reason,
            "findings": findings,
            "severity_counts": counts,
        }

    try:
        result = await asyncio.to_thread(_run)
    except Exception as exc:
        _log.exception("skills hub scan failed")
        raise HTTPException(status_code=502, detail=f"Hub scan failed: {exc}")
    if result is None:
        raise HTTPException(status_code=404, detail=f"Skill not found: {ident}")
    return result


@router.get("/api/skills")
async def get_skills(profile: Optional[str] = None, *, retry_metadata: bool = False):
    from tools.skills_tool import _find_all_skills
    from hermes_cli.skills_config import get_disabled_skills
    from tools.skill_usage import (
        _read_bundled_manifest_names,
        _read_hub_installed_names,
        activity_count,
        load_usage,
    )
    def _run():
        with _profile_scope(profile):
            config = load_config()
            disabled = get_disabled_skills(config)
            skills = _find_all_skills(skip_disabled=True)
            usage = load_usage()
            # Set-based provenance (same classification as skill_usage.provenance,
            # without a per-skill manifest read): hub > bundled > agent, where
            # "agent" covers agent-authored AND local hand-made skills — the ones
            # the user may edit/delete from the UI.
            bundled_names = _read_bundled_manifest_names()
            hub_names = _read_hub_installed_names()
            from tools.skills_hub import HubLockFile
            lock_sources = {entry["name"]: entry.get("source") for entry in HubLockFile().list_installed()}
            imported_names = {name for name, source in lock_sources.items() if source == "local"}
        for s in skills:
            s["enabled"] = s["name"] not in disabled
            s["usage"] = max(0, int(usage.get(s["name"], {}).get("use_count") or 0))
            s["provenance"] = (
                "imported" if s["name"] in imported_names
                else "hub" if s["name"] in hub_names
                else "bundled" if s["name"] in bundled_names
                else "agent"
            )
            if s["provenance"] == "hub":
                s["hub_source"] = lock_sources.get(s["name"]) or ""
        from tools.skill_hub_usage_sync import local_skill_metrics
        with _profile_scope(profile), _config_profile_scope(profile):
            skills = local_skill_metrics(skills)
            # CUSTOM（2026-10-16）：「值得分享」「有未发布的改动」—— 只读本机回执，不调网关。
            try:
                from tools.skill_refine import publish_states
                publish_states(skills)
            except Exception:  # noqa: BLE001 — 提示是锦上添花，不能拖垮技能列表
                _log.debug("publish state annotation failed", exc_info=True)
        return _localized(skills, profile, title_field="display_name", repair_local=True, retry=retry_metadata)

    return await asyncio.to_thread(_run)


@router.post("/api/skills/metadata/complete")
async def complete_local_skill_metadata(profile: Optional[str] = None):
    """Explicit retry; paths and candidates are resolved by the backend."""
    rows = await get_skills(profile, retry_metadata=True)
    return {"queued": sum(bool(row.get("metadata_pending")) for row in rows)}


@router.put("/api/skills/toggle")
async def toggle_skill(body: SkillToggle, profile: Optional[str] = None):
    from hermes_cli.skills_config import get_disabled_skills, save_disabled_skills

    def _run():
        with _profile_scope(body.profile or profile):
            with _CONFIG_MUTATION_LOCK:
                config = load_config()
                disabled = get_disabled_skills(config)
                if body.enabled:
                    disabled.discard(body.name)
                else:
                    disabled.add(body.name)
                save_disabled_skills(config, disabled)
        return {"ok": True, "name": body.name, "enabled": body.enabled}

    return await asyncio.to_thread(_run)


@router.get("/api/skills/content")
async def get_skill_content(name: str, profile: Optional[str] = None):
    """Return the raw SKILL.md text for a skill, for the dashboard editor."""
    from tools.skill_manager_tool import _find_skill

    def _run():
        with _profile_scope(profile):
            found = _find_skill(name)
            if not found:
                raise HTTPException(status_code=404, detail=f"Skill '{name}' not found.")
            skill_md = found["path"] / "SKILL.md"
            if not skill_md.exists():
                raise HTTPException(status_code=404, detail=f"Skill '{name}' has no SKILL.md.")
            try:
                content = skill_md.read_text(encoding="utf-8")
            except OSError as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc
            return {"name": name, "content": content, "path": str(skill_md)}

    return await asyncio.to_thread(_run)


@router.post("/api/skills")
async def create_skill(body: SkillCreate):
    """Create a new custom skill (SKILL.md) from the dashboard editor.

    Calls the same validated write path as the agent's ``skill_manage``
    tool (frontmatter validation, name/category validation, size limit,
    optional security scan) — but bypasses the agent write-approval gate:
    a write from the authenticated dashboard IS the user acting directly.
    """
    from tools.skill_manager_tool import _create_skill

    def _run():
        with _profile_scope(body.profile):
            return _create_skill(body.name, body.content, body.category or None)

    result = await asyncio.to_thread(_run)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Failed to create skill."))
    _clear_skills_prompt_cache()
    return result


@router.put("/api/skills/content")
async def update_skill_content(body: SkillContentUpdate):
    """Replace the SKILL.md of an existing skill (full rewrite) from the editor."""
    from tools.skill_manager_tool import _edit_skill

    def _run():
        with _profile_scope(body.profile):
            return _edit_skill(body.name, body.content)

    result = await asyncio.to_thread(_run)
    if not result.get("success"):
        err = result.get("error", "Failed to update skill.")
        status = 404 if "not found" in str(err).lower() else 400
        raise HTTPException(status_code=status, detail=err)
    _clear_skills_prompt_cache()
    return result
