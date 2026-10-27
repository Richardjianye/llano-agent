"""品牌构建禁掉运行期 pip —— 两道闩各自单独有效。

上游 tools/lazy_deps.py 默认 allow_lazy_installs=true：可选后端会在第一次用到时
现场从 PyPI 装包。品牌构建靠两样东西关掉它，主闸是 managed config 里的
security.allow_lazy_installs:false，副闸是 Electron 传下来的
HERMES_DISABLE_LAZY_INSTALLS=1。

这些用例盯的是**上游的闸门本身还在**。它是我们唯一的依靠，而月度同步随时可能
把默认值、键名或解析顺序改掉 —— 那种改动不会有任何报错，只会让公司机器悄悄
恢复出网装包。
"""
import pytest

from tools import lazy_deps


@pytest.fixture(autouse=True)
def _clear_managed_cache():
    from hermes_cli import managed_scope

    managed_scope.invalidate_managed_cache()
    yield
    managed_scope.invalidate_managed_cache()


def _write_managed(tmp_path, body):
    (tmp_path / "config.yaml").write_text(body, encoding="utf-8")
    return tmp_path


def test_baseline_upstream_default_still_allows_lazy_installs(monkeypatch, tmp_path):
    """基线：没有任何策略时上游是放行的。

    先立这一条，否则下面两条「关掉了」可能只是因为环境里本来就装不了 ——
    那样的绿是假绿。
    """
    monkeypatch.delenv("HERMES_DISABLE_LAZY_INSTALLS", raising=False)
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(_write_managed(tmp_path, "product:\n  enabled: true\n")))

    assert lazy_deps._allow_lazy_installs() is True


def test_managed_config_kill_switch_disables_lazy_installs(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_DISABLE_LAZY_INSTALLS", raising=False)
    monkeypatch.setenv(
        "HERMES_MANAGED_DIR",
        str(_write_managed(tmp_path, "security:\n  allow_lazy_installs: false\n")),
    )

    assert lazy_deps._allow_lazy_installs() is False


def test_env_latch_holds_when_the_managed_file_is_unreadable(monkeypatch, tmp_path):
    """managed scope 解析失败是 fail-open 的，副闸就是为这一刻准备的。

    损坏的 YAML 只会记一条 warning 然后当作没有策略 —— 主闸这时候是不设防的。
    """
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(_write_managed(tmp_path, "security: [this is not a mapping\n")))
    monkeypatch.delenv("HERMES_LAZY_INSTALL_TARGET", raising=False)

    monkeypatch.delenv("HERMES_DISABLE_LAZY_INSTALLS", raising=False)
    assert lazy_deps._allow_lazy_installs() is True, "坏掉的 managed config 应当 fail-open —— 前提变了就得重新想副闸"

    monkeypatch.setenv("HERMES_DISABLE_LAZY_INSTALLS", "1")
    assert lazy_deps._allow_lazy_installs() is False


def test_env_latch_is_a_seal_not_a_redirect(monkeypatch, tmp_path):
    """没配 HERMES_LAZY_INSTALL_TARGET，所以 =1 是封死而不是改道。

    上游那条分支在有 target 时会**放行**（装到数据卷上）。品牌构建从不设 target，
    但这层语义值得钉住：哪天有人为了别的原因加上 target，这条会报红。
    """
    monkeypatch.setenv("HERMES_DISABLE_LAZY_INSTALLS", "1")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(_write_managed(tmp_path, "product:\n  enabled: true\n")))

    monkeypatch.setattr(lazy_deps, "_lazy_install_target", lambda: tmp_path / "elsewhere")
    assert lazy_deps._allow_lazy_installs() is True

    monkeypatch.setattr(lazy_deps, "_lazy_install_target", lambda: None)
    assert lazy_deps._allow_lazy_installs() is False
