"""Reviewed package metadata for built-in skills; never translate user files."""
from __future__ import annotations

from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re

_CJK = re.compile(r"[\u3400-\u9fff\U00020000-\U000323af]")


@lru_cache(maxsize=1)
def bundled_catalog() -> dict:
    return json.loads(Path(__file__).with_name("bundled_skill_chinese_catalog.json").read_text(encoding="utf-8"))


def _chinese(value) -> bool:
    if not isinstance(value, str):
        return False
    if re.match(r"^\s*use when\b", value, re.IGNORECASE):
        return False
    han = len(_CJK.findall(value))
    latin = len(re.findall(r"[A-Za-z]", value))
    return han > 0 and latin <= han * 2


def apply_bundled_metadata(row: dict, title_field: str = "name") -> dict | None:
    """Localize only a known installed bundle, preserving Chinese user edits.

    Name/category alone cannot establish provenance: inspect the installed file
    and match the reviewed body plus source description. A custom same-name
    skill or edited instructions must not inherit a different skill's title.
    The caller controls the display field; durable skill IDs are unchanged when
    it supplies ``title_field='display_name'``.
    """
    from agent.skill_utils import parse_frontmatter
    from hermes_constants import get_hermes_home

    if row.get("provenance") in {"hub", "imported", "agent"}:
        return None
    name = row.get("slug") or row.get("skill_id") or row.get("name")
    category = str(row.get("category") or "general").lower()
    matches = [(relative, entry) for relative, entry in bundled_catalog()["skills"].items()
               if entry["name"] == name and entry["category"] == category]
    if len(matches) != 1:
        return None
    relative, entry = matches[0]
    path = Path(get_hermes_home()) / "skills" / relative / "SKILL.md"
    if row.get("skill_dir") and Path(row["skill_dir"]).resolve() != path.parent.resolve():
        return None
    try:
        text = path.read_text(encoding="utf-8-sig")
        meta, body = parse_frontmatter(text)
    except (OSError, ValueError):
        return None
    if meta.get("name") != entry["name"]:
        return None
    if hashlib.sha256(body.encode()).hexdigest() != entry["source_body_sha256"]:
        return None
    current_description = str(row.get("description") or "")
    actual_description = str(meta.get("description") or "")
    if not _chinese(actual_description) and actual_description != entry["source_description"]:
        return None
    if not _chinese(current_description) and current_description != entry["source_description"]:
        return None
    actual_title = str(meta.get("display_name") or "")
    if actual_title and not _chinese(actual_title) and actual_title != entry["source_display_name"]:
        return None
    result = dict(row)
    current_title = result.get(title_field)
    result[title_field] = current_title if _chinese(current_title) else (
        actual_title if _chinese(actual_title) else entry["display_name"])
    if not _chinese(current_description):
        result["description"] = entry["description"]
    result["metadata_pending"] = False
    return result
