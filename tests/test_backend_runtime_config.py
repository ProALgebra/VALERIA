from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from opencode_web.config import ConfigError, Settings
from opencode_web.opencode_client import OpenCodeManager


@pytest.fixture
async def runtime(settings, workspace, tmp_path, monkeypatch):
    binary = tmp_path / "opencode"
    binary.touch()
    manager = OpenCodeManager(replace(
        settings, opencode_bin=binary, opencode_v2_state_root=tmp_path / "v2-state",
    ))
    process = SimpleNamespace(pid=12345, returncode=None, wait=AsyncMock(return_value=0))
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr("opencode_web.opencode_client.asyncio.create_subprocess_exec", spawn)
    monkeypatch.setattr(manager, "_signal_instance", lambda instance, sig: None)
    monkeypatch.setattr(manager, "_free_port", lambda: 45100)

    async def ready(instance):
        instance.ready.set()
        return instance.client

    monkeypatch.setattr(manager, "_start_instance", ready)
    try:
        yield SimpleNamespace(manager=manager, spawn=spawn, workspace=workspace)
    finally:
        await manager.close()


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


def test_legacy_environment_keeps_v1_credentials_and_ignores_inactive_v2_config(
    monkeypatch, workspace, tmp_path,
):
    monkeypatch.setenv("SESSION_SECRET", "x" * 32)
    monkeypatch.setenv("ALLOWED_WORKSPACE_ROOTS", str(workspace))
    monkeypatch.delenv("OPENCODE_API_VERSION", raising=False)
    monkeypatch.setenv("OPENCODE_SERVER_USERNAME", "legacy-user")
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "legacy-password")
    monkeypatch.setenv("OPENCODE_PASSWORD", "v2-only-password")
    monkeypatch.setenv("OPENCODE_V2_CONFIG_DIR", str(tmp_path / "absent-v2-config"))
    settings = Settings.from_env()
    assert settings.opencode_api_version == "v1"
    assert settings.opencode_username == "legacy-user"
    assert settings.opencode_password == "legacy-password"


def test_unknown_backend_is_rejected(settings):
    with pytest.raises(ConfigError, match="OPENCODE_API_VERSION"):
        replace(settings, opencode_api_version="invalid")


def test_hermes_environment_requires_a_key(monkeypatch, workspace):
    monkeypatch.setenv("SESSION_SECRET", "x" * 32)
    monkeypatch.setenv("ALLOWED_WORKSPACE_ROOTS", str(workspace))
    monkeypatch.setenv("OPENCODE_API_VERSION", "hermes")
    monkeypatch.delenv("HERMES_API_KEY", raising=False)
    with pytest.raises(ConfigError, match="HERMES_API_KEY"):
        Settings.from_env()
    monkeypatch.setenv("HERMES_API_KEY", "test-hermes-key")
    monkeypatch.setenv("HERMES_PORT_RANGE_START", "4300")
    monkeypatch.setenv("HERMES_PORT_RANGE_END", "4309")
    settings = Settings.from_env()
    assert settings.opencode_api_version == "hermes"
    assert settings.hermes_api_key == "test-hermes-key"
    assert settings.hermes_port_start == 4300
    assert settings.hermes_port_end == 4309


async def test_hermes_launch_uses_isolated_home_and_api_server(
    runtime, monkeypatch, tmp_path
):
    manager = runtime.manager
    binary = tmp_path / "hermes"
    binary.touch()
    template = tmp_path / "hermes-template"
    template.mkdir()
    (template / "config.yaml").write_text("provider: test\n", encoding="utf-8")
    manager.settings = replace(
        manager.settings,
        opencode_api_version="hermes",
        hermes_bin=binary,
        hermes_api_key="secret-key",
        hermes_state_root=tmp_path / "hermes-state",
        hermes_home_template=template,
    )
    monkeypatch.setenv("HERMES_HOME", "/legacy/hermes")
    await manager.ensure(str(runtime.workspace))
    assert runtime.spawn.await_args.args == (str(binary), "gateway", "run")
    env = runtime.spawn.await_args.kwargs["env"]
    assert env["API_SERVER_ENABLED"] == "true"
    assert env["API_SERVER_KEY"] == "secret-key"
    assert env["API_SERVER_PORT"] == "45100"
    assert env["HERMES_HOME"] != "/legacy/hermes"
    assert (Path(env["HERMES_HOME"]) / "config.yaml").is_file()


async def test_v1_launch_preserves_binary_configuration_and_state_paths(runtime, monkeypatch):
    manager = runtime.manager
    monkeypatch.setenv("OPENCODE_CONFIG", "/legacy/opencode.jsonc")
    monkeypatch.setenv("XDG_DATA_HOME", "/legacy/data")
    monkeypatch.setenv("XDG_STATE_HOME", "/legacy/state")
    monkeypatch.setenv("XDG_CACHE_HOME", "/legacy/cache")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/legacy/config")
    await manager.ensure(str(runtime.workspace))
    assert runtime.spawn.await_args.args == (
        str(manager.settings.opencode_bin), "serve", "--hostname", "127.0.0.1", "--port", "45100",
    )
    env = runtime.spawn.await_args.kwargs["env"]
    assert env["OPENCODE_CONFIG"] == "/legacy/opencode.jsonc"
    for key, value in (
        ("XDG_DATA_HOME", "/legacy/data"), ("XDG_STATE_HOME", "/legacy/state"),
        ("XDG_CACHE_HOME", "/legacy/cache"), ("XDG_CONFIG_HOME", "/legacy/config"),
    ):
        assert env[key] == value
    assert env["OPENCODE_SERVER_USERNAME"] == manager.settings.opencode_username
    assert env["OPENCODE_SERVER_PASSWORD"] == manager.settings.opencode_password
    assert not manager.settings.opencode_v2_state_root.exists()


async def test_v2_separates_state_and_configuration(runtime, monkeypatch):
    manager = runtime.manager
    manager.settings = replace(manager.settings, opencode_api_version="v2", opencode_username="old")
    monkeypatch.setenv("OPENCODE_CONFIG", "/legacy/opencode.jsonc")
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", "/legacy/config")
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", "{}")
    await manager.ensure(str(runtime.workspace))
    env = runtime.spawn.await_args.kwargs["env"]
    assert env["OPENCODE_PASSWORD"] == manager.settings.opencode_password
    assert "OPENCODE_SERVER_USERNAME" not in env
    assert not {"OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR", "OPENCODE_CONFIG_CONTENT"} & env.keys()
    for key in ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME"):
        assert str(manager.settings.opencode_v2_state_root) in env[key]
