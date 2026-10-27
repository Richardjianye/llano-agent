"""Role-aware example app attached to a published site.

The example data is deliberately synthetic and separate from gateway business data.
The policy lives beside the site's private artifacts, outside uploaded versions.
"""

import json
import os
import tempfile
from pathlib import Path

from fastapi import HTTPException

ROLES = frozenset({"president", "developer", "admin", "auditor", "manager", "employee", "operator"})
COMPONENTS = frozenset({"welcome", "team", "finance"})
DATASETS = frozenset({"team", "finance"})
DEFAULT_POLICY = {
    "components": {
        "welcome": sorted(ROLES),
        "team": ["president", "developer", "admin", "manager"],
        "finance": ["president", "developer", "admin"],
    },
    "data": {
        "team": ["president", "developer", "admin", "manager"],
        "finance": ["president", "developer", "admin"],
    },
}
EXAMPLE_DATA = {
    "team": {"title": "团队进度（演示数据）", "completed": 12, "total": 16},
    "finance": {"title": "预算概览（演示数据）", "used": 36, "total": 50, "unit": "万元"},
}


def validate_policy(policy: dict) -> dict:
    if set(policy) != {"components", "data"}:
        raise HTTPException(400, "示例权限配置字段无效")
    for section, keys in (("components", COMPONENTS), ("data", DATASETS)):
        values = policy[section]
        if not isinstance(values, dict) or set(values) != keys:
            raise HTTPException(400, "示例权限配置项目无效")
        for roles in values.values():
            if (not isinstance(roles, list)
                    or any(not isinstance(role, str) or role not in ROLES for role in roles)
                    or len(roles) != len(set(roles))):
                raise HTTPException(400, "示例权限配置角色无效")
    return policy


def _path(root: Path, site_id: str) -> Path:
    # site_id comes from an existing DB row, but do not rely on that at the file boundary.
    if not site_id or any(char not in "0123456789abcdef-" for char in site_id):
        raise HTTPException(400, "网站标识无效")
    return root / site_id / ".demo-policy.json"


def load_policy(root: Path, site_id: str) -> dict:
    path = _path(root, site_id)
    if not path.exists():
        return json.loads(json.dumps(DEFAULT_POLICY))
    if path.is_symlink() or not path.is_file():
        raise HTTPException(503, "示例权限配置无法读取")
    try:
        return validate_policy(json.loads(path.read_text(encoding="utf-8")))
    except HTTPException as exc:
        raise HTTPException(503, "示例权限配置无法读取") from exc
    except (OSError, ValueError, TypeError) as exc:
        raise HTTPException(503, "示例权限配置无法读取") from exc


def save_policy(root: Path, site_id: str, policy: dict) -> None:
    validate_policy(policy)
    path = _path(root, site_id)
    if not path.parent.is_dir() or path.is_symlink():
        raise HTTPException(503, "网站文件目录不可用")
    filename = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".demo-policy-", delete=False) as handle:
            filename = handle.name
            json.dump(policy, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(filename, path)
    except OSError as exc:
        raise HTTPException(503, "示例权限配置保存失败") from exc
    finally:
        if filename and os.path.exists(filename):
            os.unlink(filename)


def granted(policy: dict, section: str, key: str, role: str) -> bool:
    return role in policy[section][key]
