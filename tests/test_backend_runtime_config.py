from dataclasses import replace

import pytest
from test_execution_mode_manager import runtime_factory as runtime_factory

from opencode_web.config import ConfigError, Settings
from opencode_web.podman_runtime import PodmanRuntime


def test_v2_config_uses_fixed_username_and_password_precedence(monkeypatch, workspace):
    monkeypatch.setenv("SESSION_SECRET", "x" * 32)
    monkeypatch.setenv("ALLOWED_WORKSPACE_ROOTS", str(workspace))
    monkeypatch.setenv("OPENCODE_API_VERSION", "v2")
    monkeypatch.setenv("OPENCODE_SERVER_USERNAME", "old-name")
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "old-password")
    monkeypatch.setenv("OPENCODE_PASSWORD", "new-password")
    settings = Settings.from_env()
    assert settings.opencode_api_version == "v2"
    assert settings.opencode_username == "opencode"
    assert settings.opencode_password == "new-password"
    assert settings.podman_image.endswith(":2.3.0")


def test_unknown_backend_is_rejected(settings):
    with pytest.raises(ConfigError, match="OPENCODE_API_VERSION"):
        replace(settings, opencode_api_version="invalid")


async def test_direct_v2_separates_state_and_configuration(runtime_factory, tmp_path, monkeypatch):
    t = runtime_factory()
    t.manager.settings = replace(
        t.manager.settings,
        opencode_api_version="v2",
        opencode_username="old-name",
        opencode_v2_state_root=tmp_path / "v2-state",
    )
    monkeypatch.setenv("OPENCODE_CONFIG", "/old/opencode.jsonc")
    await t.manager.ensure(str(t.workspace))
    env = t.direct_spawn.await_args.kwargs["env"]
    assert env["OPENCODE_PASSWORD"] == t.manager.settings.opencode_password
    assert "OPENCODE_SERVER_USERNAME" not in env
    assert "OPENCODE_CONFIG" not in env
    for key in ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME"):
        assert str(tmp_path / "v2-state") in env[key]


def test_podman_v2_uses_versioned_home_and_protected_password(settings, workspace, tmp_path):
    config = replace(
        settings, opencode_api_version="v2", podman_state_root=tmp_path / "podman-state"
    )
    v2 = PodmanRuntime(config).build_launch(workspace, 45100, "opencode", "test-secret")
    v1 = PodmanRuntime(settings).build_launch(workspace, 45101, "old-name", "test-secret")
    assert v2.home_path.is_relative_to(config.podman_state_root / "v2")
    assert v2.home_path != v1.home_path
    assert v2.env["OPENCODE_PASSWORD"] == "test-secret"
    assert "OPENCODE_SERVER_USERNAME" not in v2.env
    assert "test-secret" not in repr(v2)
