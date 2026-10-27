"""Fixtures shared across hermes_cli kanban tests."""

from __future__ import annotations

import pytest


@pytest.fixture
def all_assignees_spawnable(monkeypatch):
    """Pretend every assignee maps to a real Hermes profile.

    Most dispatcher tests use synthetic assignees ("alice", "bob") that
    don't correspond to actual profile directories on disk. Without this
    patch, the dispatcher's profile-exists guard (PR #20105) routes
    those tasks into ``skipped_nonspawnable`` instead of spawning, which
    would break tests that assert spawn behavior.
    """
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)


@pytest.fixture(autouse=True)
def _suppress_concurrent_hermes_gate(request, monkeypatch):
    """Default ``_detect_concurrent_hermes_instances`` to ``[]`` for every test.

    The Windows update path now refuses to proceed when another
    ``hermes.exe`` is detected (issue #26670). On a developer's Windows
    machine running the test suite via ``hermes`` itself, this would
    flag the running agent as a concurrent instance and abort every
    ``cmd_update`` test. Tests that want to exercise the gate explicitly
    re-patch ``_detect_concurrent_hermes_instances`` with their own
    return value — autouse here gives a clean default without touching
    the rest of the suite.

    Tests that need to call the REAL function (e.g. unit tests for the
    helper itself) opt out with ``@pytest.mark.real_concurrent_gate``.
    """
    if request.node.get_closest_marker("real_concurrent_gate"):
        return
    try:
        from hermes_cli import main as _cli_main
    except Exception:
        return
    # raising=False: under pytest's per-test spawn isolation, a concurrent
    # xdist worker importing a module that transitively touches hermes_cli.main
    # can briefly expose a partially-initialized module object here — one where
    # _detect_concurrent_hermes_instances isn't defined yet. A bare setattr
    # would raise AttributeError and error the (unrelated) test. The attribute
    # always exists once main.py finishes importing, so a no-op when it's
    # transiently absent is the correct, race-free default.
    monkeypatch.setattr(
        _cli_main,
        "_detect_concurrent_hermes_instances",
        lambda *_a, **_k: [],
        raising=False,
    )


# ---------------------------------------------------------------------------
# 真实仓库保护
# ---------------------------------------------------------------------------
#
# 这个目录里有一批测 `hermes update` 的用例。update 的第一步是
# `git stash push --include-untracked -m hermes-update-autostash-<时间戳>`
# （见 hermes_cli/update_cmd.py）。只要有一个用例漏掉 monkeypatch、
# 让真的 stash 跑起来，它 stash 的就是**开发者当前的工作区**——
# 未提交的改动会当场消失，而且 pytest 一个字都不会报，
# 现象只是「我的改动没了」。2026-09-03 真的发生过一次，丢了一整轮改动。
#
# 这个 autouse fixture 把那种静默数据丢失变成一条指名道姓的失败：
# 谁制造的 autostash，就在谁头上报错，并且当场把改动恢复回去。

import re as _re
import subprocess

# 导入期抓住真的 subprocess.run。用例 monkeypatch 掉 subprocess.run 是常态，
# 守卫必须绕过它，否则读到的是用例伪造的输出。
_REAL_RUN = subprocess.run
_SHA_RE = _re.compile(r"^[0-9a-f]{40}$")


def _autostash_refs() -> set[str]:
    """当前所有 hermes-update-autostash-* 的 stash ref。git 不可用时返回空集。"""
    try:
        out = _REAL_RUN(
            ["git", "stash", "list"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except Exception:
        return set()

    return {
        line.split(":", 1)[0]
        for line in out.splitlines()
        if "hermes-update-autostash-" in line
    }


def _head_state() -> tuple[str, str]:
    """(当前分支, HEAD sha)。git 不可用时返回空串。"""
    try:
        branch = _REAL_RUN(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        head = _REAL_RUN(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except Exception:
        return ("", "")

    # 形状校验是第二道保险：万一还是读到了假输出，宁可弃权也不要误报。
    # 误报会把一堆好用例判红，比漏报更快摧毁这道防线的可信度。
    if not _SHA_RE.match(head):
        return ("", "")

    return (branch, head)


@pytest.fixture(autouse=True)
def _no_live_repo_mutation():
    """任何用例都不许改动真实仓库的 HEAD。

    `hermes update` 的完整流程是 stash -> reset --hard -> checkout，
    只盯 stash 不够：2026-09-03 在 reflog 里实测抓到过
    `checkout: moving from <branch> to main` 和 `reset: moving to HEAD`。

    分支被切走比改动被 stash 更阴险——工作区看着是干净的，人还在原地敲，
    但跑的已经是另一个分支的代码了。

    这条不挑具体是哪个 git 子命令干的，换个方式改活检出照样抓得到。
    """
    before = _head_state()
    yield
    after = _head_state()

    if before != after and all(before) and all(after):
        pytest.fail(
            f"这个用例改动了真实仓库的 HEAD：{before} -> {after}。"
            "update 类用例必须在 tmp_path 建的临时仓库里跑。"
        )


@pytest.fixture(autouse=True)
def _no_real_repo_autostash():
    """任何用例都不许在真实仓库上留下 update autostash。"""
    before = _autostash_refs()
    yield
    leaked = _autostash_refs() - before

    if not leaked:
        return

    # 先恢复再报错：测试挂掉是次要的，开发者的工作区不能丢。
    # 只 pop 本次新增的、且名字确认是 update autostash 的那些。
    restored = []
    for ref in sorted(leaked, reverse=True):
        result = subprocess.run(
            ["git", "stash", "pop", ref], capture_output=True, text=True, timeout=30
        )
        restored.append(f"{ref}{'' if result.returncode == 0 else ' (pop 失败，手动 git stash list 找回)'}")

    pytest.fail(
        "这个用例在真实仓库上跑了 `git stash`，把工作区暂存掉了。"
        "改成在 tmp_path 建临时仓库，或 monkeypatch 掉 _stash_local_changes_if_needed。"
        f" 已尝试恢复：{', '.join(restored)}"
    )
