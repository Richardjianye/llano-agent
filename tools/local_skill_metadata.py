"""Bounded, profile-owned AI display metadata for local skills.

Never rewrites instructional files or the prompt of an existing conversation.
Bundle/company translations remain owned by the package/server respectively.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import logging
from pathlib import Path
import queue
import threading
import time

from hermes_constants import get_hermes_home, set_hermes_home_override, reset_hermes_home_override

_log = logging.getLogger(__name__)
_lock = threading.Lock()
_jobs = queue.Queue(maxsize=20)
_pending = set()
_failed_until = {}
_worker_started = False
_RETRY_DELAY = 15 * 60


def _cache_path(home):
    return home / "cache" / "local-skill-metadata-zh.json"


def _cache(home):
    from tools.skill_chinese_metadata import _read_cache
    return _read_cache(_cache_path(home))


def _completable(row):
    """本机自有或从外部来源装的技能；公司技能由服务端、随包技能由安装包负责中文。"""
    provenance = row.get("provenance")
    if provenance in {"agent", "imported"}:
        return True
    return provenance == "hub" and bool(row.get("hub_source")) and row.get("hub_source") != "llano-hub"


def can_complete(row):
    return _snapshot(row, Path(get_hermes_home()).resolve()) is not None


def _snapshot(row, home):
    if not _completable(row) or not row.get("skill_dir"):
        return None
    directory = Path(row["skill_dir"]).resolve()
    if not directory.is_relative_to((home / "skills").resolve()):
        return None
    definition = directory / "SKILL.md"
    try:
        if definition.is_symlink() or definition.stat().st_size > 1_048_576:
            return None
        text = definition.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return None
    key = hashlib.sha256((str(directory) + "\0" + text).encode()).hexdigest()
    return {"id": key, "path": definition, "original": text,
            "name": row.get("display_name") or row.get("name") or directory.name,
            "description": row.get("description") or "", "content": text[:8000]}


def display_metadata(row):
    """Read once-generated metadata only for the same file/content/profile."""
    home = Path(get_hermes_home()).resolve()
    item = _snapshot(row, home)
    return _cache(home).get(item["id"], {}) if item else {}


def _complete_batch(home, items):
    from tools.skill_chinese_metadata import _translate
    from utils import atomic_write_text
    token = set_hermes_home_override(str(home))
    try:
        proposals = _translate([{key: item[key] for key in ("id", "name", "description", "content")}
                                for item in items])
        with _lock:
            cache = _cache(home)
            for item in items:
                # A concurrent edit/delete must not acquire stale display metadata.
                try:
                    if item["path"].read_text(encoding="utf-8-sig") == item["original"]:
                        cache[item["id"]] = proposals[item["id"]]
                except OSError:
                    continue
            destination = _cache_path(home)
            destination.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(destination, json.dumps(cache, ensure_ascii=False))
    finally:
        reset_hermes_home_override(token)


def _worker():
    while True:
        context, home, items = _jobs.get()
        keys = [(str(home), item["id"]) for item in items]
        try:
            context.run(_complete_batch, home, items)
        except Exception:
            _log.warning("Local skill Chinese metadata completion failed; will retry later", exc_info=True)
            with _lock:
                _failed_until.update({key: time.monotonic() + _RETRY_DELAY for key in keys})
        finally:
            with _lock:
                _pending.difference_update(keys)
            _jobs.task_done()


def schedule_metadata(rows, *, retry=False, enqueue=True):
    """Return names really queued/in flight. Reads never wait for the model.

    Five skills per model request, one daemon worker, at most twenty batches;
    failures cool down across page visits instead of generating a request storm.
    ``enqueue=False`` only reports work already in flight: since 2026-10-16 a
    plain list read no longer starts model calls — the 「用 AI 补全中文」 button does.
    """
    global _worker_started
    home = Path(get_hermes_home()).resolve()
    cached = _cache(home)
    candidates = []
    for row in rows:
        item = _snapshot(row, home)
        if item and item["id"] not in cached:
            candidates.append((row["name"], item))
    names = set()
    with _lock:
        now = time.monotonic()
        for key, expiry in list(_failed_until.items()):
            if expiry <= now:
                _failed_until.pop(key, None)
        batch = []
        for name, item in candidates:
            key = (str(home), item["id"])
            if key in _pending:
                names.add(name)
                continue
            if not enqueue:
                continue
            if not retry and key in _failed_until:
                continue
            if _jobs.full():
                break
            _pending.add(key)
            _failed_until.pop(key, None)
            names.add(name)
            batch.append(item)
            if len(batch) == 5:
                _jobs.put_nowait((contextvars.copy_context(), home, batch))
                batch = []
        if batch:
            _jobs.put_nowait((contextvars.copy_context(), home, batch))
        if not _worker_started and not _jobs.empty():
            threading.Thread(target=_worker, name="local-skill-metadata", daemon=True).start()
            _worker_started = True
    return names
