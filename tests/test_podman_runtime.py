from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import threading
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

import pytest

from opencode_web.podman_runtime import (
    CONTAINER_CONFIG,
    MANAGED_LABEL,
    OWNER_LABEL,
    PodmanError,
    PodmanRuntime,
)

IMAGE_ID = "sha256:" + "a" * 64


@pytest.fixture
def podman_settings(settings, tmp_path):
    values = {item.name: getattr(settings, item.name) for item in fields(settings)}
    values.update(
        podman_bin=Path("/usr/bin/podman"),
        podman_image="localhost/valeria-opencode:1.18.25",
        podman_state_root=tmp_path / "podman-state",
        podman_config_dir=None,
        podman_memory_mb=4096,
        podman_cpus=2.0,
        podman_pids_limit=512,
        podman_forward_env=(),
        validate_workspace=settings.validate_workspace,
    )
    return SimpleNamespace(**values)


class FakeProcess:
    def __init__(self, output=b"", code=0):
        self.output = output
        self.returncode = code
        self.pid = 12345
        self.killed = False

    async def communicate(self):
        return self.output, b""

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


@pytest.fixture
def fake_cli(monkeypatch):
    state = SimpleNamespace(
        calls=[], rootless=b"true\n", image_code=0, inventory=[], run=None, removal_code=0,
    )
    monkeypatch.setattr("opencode_web.podman_runtime.os.geteuid", lambda: 1000)

    async def execute(*argv, **kwargs):
        state.calls.append((argv, kwargs))
        command = argv[1:]
        if command[0] == "info":
            return FakeProcess(state.rootless)
        if command[:2] == ("image", "exists"):
            return FakeProcess(code=state.image_code)
        if command[:2] == ("image", "inspect"):
            return FakeProcess((IMAGE_ID + "\n").encode())
        if command[0] == "ps":
            return FakeProcess(json.dumps(state.inventory).encode())
        if command[0] == "rm":
            return FakeProcess(code=state.removal_code)
        if command[0] == "run":
            state.run = FakeProcess(code=None)
            return state.run
        raise AssertionError(command)

    monkeypatch.setattr("opencode_web.podman_runtime.asyncio.create_subprocess_exec", execute)
    state.execute = execute
    return state


def test_launch_limits_mounts_environment_and_credentials(podman_settings, workspace, monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "never-in-the-container")
    monkeypatch.setenv("APP_DATABASE_PATH", "/private/database")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "provider-secret")
    monkeypatch.setenv("CONTAINER_HOST", "ssh://unwanted-remote-engine")
    podman_settings.podman_forward_env = ("ANTHROPIC_API_KEY",)
    runtime = PodmanRuntime(podman_settings)
    launch = runtime.build_launch(workspace, 45100, "session-user", "session-secret")
    arguments = launch.argv
    mounts = [arguments[index + 1] for index, arg in enumerate(arguments) if arg == "--mount"]
    assert mounts == [
        f"type=bind,src={workspace},dst={workspace}",
        f"type=bind,src={launch.home_path},dst=/home/agent",
    ]
    assert launch.home_path.is_relative_to(podman_settings.podman_state_root)
    assert launch.home_path.stat().st_mode & 0o777 == 0o700
    assert arguments[arguments.index("--workdir") + 1] == str(workspace)
    assert arguments[arguments.index("--publish") + 1] == "127.0.0.1:45100:4100"
    assert {"--read-only", "--cap-drop=all", "--security-opt=no-new-privileges"} <= set(arguments)
    assert {"--pull=never", "--network=private", "--pid=private", "--ipc=private"} <= set(arguments)
    assert arguments[arguments.index("--memory") + 1] == "4096m"
    assert "HOME=/home/agent" in arguments
    assert not {"SESSION_SECRET", "APP_DATABASE_PATH", "CONTAINER_HOST"} & launch.env.keys()
    assert launch.env["ANTHROPIC_API_KEY"] == "provider-secret"
    assert launch.env["OPENCODE_SERVER_PASSWORD"] == "session-secret"
    assert "provider-secret" not in repr(launch)
    assert "session-secret" not in repr(launch)
    assert not any("provider-secret" in arg or "session-secret" in arg for arg in arguments)
    repeated = runtime.build_launch(workspace, 45101, "another", "another-secret")
    assert repeated.home_path == launch.home_path
    assert repeated.container_name != launch.container_name


def test_config_has_one_read_only_mount(podman_settings, workspace, tmp_path):
    config = tmp_path / "trusted-config"
    config.mkdir()
    (config / "opencode.jsonc").write_text("{}")
    podman_settings.podman_config_dir = config
    launch = PodmanRuntime(podman_settings).build_launch(workspace, 45100, "u", "secret")
    assert f"type=bind,src={config},dst={CONTAINER_CONFIG},ro=true" in launch.argv
    assert f"OPENCODE_CONFIG={CONTAINER_CONFIG}/opencode.jsonc" in launch.argv


@pytest.mark.parametrize("variable", [
    "SESSION_SECRET", "APP_DATABASE_PATH", "BOOTSTRAP_ADMIN_PASSWORD_FILE",
    "OPENCODE_CONFIG_CONTENT", "OPENCODE_SERVER_PASSWORD", "MATTERMOST_TOKEN_FILE",
    "VALERIA_ADMIN_PASSWORD", "CONTAINER_HOST", "LD_PRELOAD", "NODE_OPTIONS",
    "CONTAINERS_CONF", "REGISTRY_AUTH_FILE", "STORAGE_DRIVER", "GCONV_PATH",
    "_CONTAINERS_ROOTLESS_UID",
    "HOME", "PATH", "XDG_CONFIG_HOME", "SSL_CERT_FILE", "GIT_SSH_COMMAND", "bad-name",
])
def test_explicit_forwarding_cannot_override_protected_values(
    podman_settings, workspace, variable,
):
    podman_settings.podman_forward_env = (variable,)
    with pytest.raises(PodmanError, match="protected"):
        PodmanRuntime(podman_settings).build_launch(workspace, 45100, "u", "secret")


@pytest.mark.parametrize("placement", ["inside", "ancestor", "symlink"])
def test_state_cannot_be_reached_through_any_workspace_root(
    podman_settings, workspace, tmp_path, placement,
):
    if placement == "inside":
        podman_settings.podman_state_root = workspace / "state"
    elif placement == "ancestor":
        podman_settings.podman_state_root = workspace.parent
    else:
        alias = tmp_path / "alias"
        alias.symlink_to(workspace, target_is_directory=True)
        podman_settings.podman_state_root = alias / "state"
    with pytest.raises(PodmanError, match="separate"):
        PodmanRuntime(podman_settings).build_launch(workspace, 45100, "u", "secret")


def test_private_home_cannot_be_replaced_with_a_symlink(podman_settings, workspace, tmp_path):
    state = podman_settings.podman_state_root
    state.mkdir(mode=0o700)
    key = hashlib.sha256(str(workspace).encode()).hexdigest()
    parent = state / key
    parent.mkdir(mode=0o700)
    secret = tmp_path / "not-agent-home"
    secret.mkdir()
    (parent / "home").symlink_to(secret, target_is_directory=True)
    with pytest.raises(PodmanError, match="symlinks"):
        PodmanRuntime(podman_settings).build_launch(workspace, 45100, "u", "secret")


def test_shared_state_permissions_are_refused(podman_settings, workspace):
    podman_settings.podman_state_root.mkdir(mode=0o755)
    with pytest.raises(PodmanError, match="0700"):
        PodmanRuntime(podman_settings).build_launch(workspace, 45100, "u", "secret")


def test_nested_mount_and_mount_argument_injection_are_refused(
    podman_settings, workspace, monkeypatch,
):
    runtime = PodmanRuntime(podman_settings)
    monkeypatch.setattr(runtime, "_nested_mounts", lambda path: [path / "external"])
    with pytest.raises(PodmanError, match="nested host mounts"):
        runtime.build_launch(workspace, 45100, "u", "secret")
    monkeypatch.setattr(runtime, "_nested_mounts", lambda path: [])
    malicious = workspace / "target,src=elsewhere"
    malicious.mkdir()
    with pytest.raises(PodmanError, match="commas"):
        runtime.build_launch(malicious, 45100, "u", "secret")


def test_existing_external_hardlink_is_refused(podman_settings, workspace):
    outside = workspace.parent / "outside-credential"
    outside.write_text("must stay outside")
    os.link(outside, workspace / "innocent-name")
    with pytest.raises(PodmanError, match="hardlinked outside"):
        PodmanRuntime(podman_settings).build_launch(workspace, 45100, "u", "secret")
    assert not podman_settings.podman_state_root.exists()


def test_internal_hardlinks_are_allowed(podman_settings, workspace):
    first = workspace / "original"
    first.write_text("workspace data")
    nested = workspace / "nested"
    nested.mkdir()
    os.link(first, nested / "second-name")
    launch = PodmanRuntime(podman_settings).build_launch(workspace, 45100, "u", "secret")
    assert launch.home_path.is_dir()


def test_symlink_to_external_tree_is_not_followed(podman_settings, workspace):
    outside = workspace.parent / "outside-tree"
    outside.mkdir()
    first = outside / "file"
    first.write_text("outside data")
    os.link(first, workspace.parent / "unscanned-link")
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    launch = PodmanRuntime(podman_settings).build_launch(workspace, 45100, "u", "secret")
    assert launch.home_path.is_dir()


@pytest.mark.parametrize("failure", ["scan", "lstat"])
def test_workspace_scanning_errors_fail_closed(podman_settings, workspace, monkeypatch, failure):
    original = os.scandir

    class BrokenEntry:
        def stat(self, *, follow_symlinks):
            assert follow_symlinks is False
            raise PermissionError("Cannot stat directory entry")

    class BrokenEntries:
        def __enter__(self):
            return iter([BrokenEntry()])

        def __exit__(self, *args):
            return False

    def denied(path):
        if isinstance(path, int):
            if failure == "scan":
                raise PermissionError("Cannot inspect directory")
            return BrokenEntries()
        return original(path)

    monkeypatch.setattr("opencode_web.podman_runtime.os.scandir", denied)
    with pytest.raises(PodmanError, match="Cannot verify workspace hardlink"):
        PodmanRuntime(podman_settings).build_launch(workspace, 45100, "u", "secret")
    assert not podman_settings.podman_state_root.exists()


def test_reserved_runtime_paths_are_refused(podman_settings, monkeypatch):
    monkeypatch.setattr(podman_settings, "validate_workspace", lambda path: Path(path))
    for path in ("/usr", "/etc/project", "/home/agent/project", "/opt/valeria/mcp/project"):
        with pytest.raises(PodmanError, match="conflicts"):
            PodmanRuntime(podman_settings).build_launch(Path(path), 45100, "u", "secret")


@pytest.mark.parametrize("protected", [
    "database", "bootstrap", "mattermost", "dotenv", "engine_config", "engine_storage",
    "xdg_storage", "engine_storage_symlink", "podman_binary", "python_binary", "backend_code",
])
def test_workspace_cannot_expose_backend_files_or_engine_storage(
    podman_settings, workspace, monkeypatch, protected,
):
    if protected == "database":
        podman_settings.database_path = workspace / "database.sqlite3"
    elif protected == "bootstrap":
        podman_settings.bootstrap_admin_password_file = workspace / "admin-password"
    elif protected == "mattermost":
        podman_settings.mattermost_token_file = workspace / "mattermost-token"
    elif protected == "dotenv":
        monkeypatch.chdir(workspace)
        (workspace / ".env").write_text("SESSION_SECRET=outside-application-only")
    elif protected == "engine_config":
        monkeypatch.setenv("XDG_CONFIG_HOME", str(workspace / "host-config"))
    elif protected == "engine_storage":
        monkeypatch.setenv("HOME", str(workspace))
    elif protected == "xdg_storage":
        monkeypatch.setenv("XDG_DATA_HOME", str(workspace / "host-data"))
    elif protected == "engine_storage_symlink":
        service_home = workspace.parent / "service-home"
        local_share = service_home / ".local/share"
        local_share.mkdir(parents=True)
        actual_storage = workspace / "actual-engine-state"
        actual_storage.mkdir()
        (local_share / "containers").symlink_to(actual_storage, target_is_directory=True)
        monkeypatch.setenv("HOME", str(service_home))
    elif protected == "podman_binary":
        podman_settings.podman_bin = workspace / "podman"
    elif protected == "python_binary":
        monkeypatch.setattr("opencode_web.podman_runtime.sys.executable", str(workspace / "python"))
    else:
        monkeypatch.setattr(
            "opencode_web.podman_runtime.__file__", str(workspace / "backend/code.py"),
        )
    with pytest.raises(PodmanError, match="protected"):
        PodmanRuntime(podman_settings).build_launch(workspace, 45100, "u", "secret")
    assert not podman_settings.podman_state_root.exists()


def test_workspace_below_service_home_does_not_expose_sibling_container_state(
    podman_settings, workspace, monkeypatch,
):
    monkeypatch.setenv("HOME", str(workspace.parent))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    launch = PodmanRuntime(podman_settings).build_launch(workspace, 45100, "u", "secret")
    assert launch.home_path.is_dir()


async def test_preflight_only_checks_local_rootless_image(podman_settings, fake_cli):
    runtime = PodmanRuntime(podman_settings)
    await runtime.preflight()
    await runtime.preflight()
    assert [argv[1:3] for argv, _ in fake_cli.calls] == [
        ("info", "--format"), ("image", "exists"), ("image", "inspect"),
    ]
    assert not podman_settings.podman_state_root.exists()


async def test_forced_preflight_does_not_reuse_an_image_removed_after_check(
    podman_settings, fake_cli,
):
    runtime = PodmanRuntime(podman_settings)
    await runtime.preflight()
    fake_cli.image_code = 1
    with pytest.raises(PodmanError, match="already exist locally"):
        await runtime.preflight(force=True)
    with pytest.raises(PodmanError, match="already exist locally"):
        await runtime.preflight()


@pytest.mark.parametrize("invalid", ["state", "config", "forward", "resources"])
async def test_preflight_validates_policy_without_any_workspace_assignment(
    podman_settings, workspace, fake_cli, invalid,
):
    if invalid == "state":
        podman_settings.podman_state_root = workspace / "private"
    elif invalid == "config":
        podman_settings.podman_config_dir = workspace.parent / "missing-config"
    elif invalid == "forward":
        podman_settings.podman_forward_env = ("SESSION_SECRET",)
    else:
        podman_settings.podman_cpus = float("nan")
    with pytest.raises(PodmanError):
        await PodmanRuntime(podman_settings).preflight()
    assert fake_cli.calls == []


async def test_cli_timeout_reaps_the_check_process(podman_settings, fake_cli, monkeypatch):
    process = FakeProcess(code=None)

    async def blocked_communicate():
        await asyncio.Event().wait()

    process.communicate = blocked_communicate

    async def create(*args, **kwargs):
        return process

    monkeypatch.setattr("opencode_web.podman_runtime.asyncio.create_subprocess_exec", create)
    with pytest.raises(TimeoutError):
        await PodmanRuntime(podman_settings)._cli("info", timeout=0.01)
    assert process.killed
    assert process.returncode == -9


@pytest.mark.parametrize("failure", ["root", "rootful", "missing", "latest"])
async def test_preflight_fails_closed(podman_settings, fake_cli, monkeypatch, failure):
    if failure == "root":
        monkeypatch.setattr("opencode_web.podman_runtime.os.geteuid", lambda: 0)
    elif failure == "rootful":
        fake_cli.rootless = b"false\n"
    elif failure == "missing":
        fake_cli.image_code = 1
    else:
        podman_settings.podman_image = "localhost/valeria-opencode:latest"
    with pytest.raises(PodmanError):
        await PodmanRuntime(podman_settings).preflight()
    assert not any(argv[1] in {"run", "pull"} for argv, _ in fake_cli.calls)


async def test_start_uses_pinned_image_and_stop_only_accepts_owned_name(
    podman_settings, workspace, fake_cli,
):
    runtime = PodmanRuntime(podman_settings)
    child = await runtime.start(workspace, 45100, "user", "unique-secret")
    assert child.process is fake_cli.run
    run_argv, run_options = next(call for call in fake_cli.calls if call[0][1] == "run")
    assert IMAGE_ID in run_argv
    assert podman_settings.podman_image not in run_argv
    assert run_options["start_new_session"] is True
    with pytest.raises(PodmanError, match="not owned"):
        await runtime.stop("valeria-ocw-" + "f" * 32)
    await runtime.stop(child.container_name)
    assert fake_cli.calls[-1][0][1:] == (
        "rm", "--force", "--ignore", "--time", "10", child.container_name,
    )
    with pytest.raises(PodmanError, match="not owned"):
        await runtime.stop(child.container_name)


async def test_failed_removal_keeps_ownership_for_a_retry(
    podman_settings, workspace, fake_cli, monkeypatch,
):
    runtime = PodmanRuntime(podman_settings)
    child = await runtime.start(workspace, 45100, "u", "secret")
    original = runtime._cli

    async def fail(*args, **kwargs):
        return 1, b""

    monkeypatch.setattr(runtime, "_cli", fail)
    with pytest.raises(PodmanError, match="Cannot remove"):
        await runtime.stop(child.container_name)
    monkeypatch.setattr(runtime, "_cli", original)
    await runtime.stop(child.container_name)


async def test_reconcile_cannot_remove_other_deployments_or_arbitrary_names(
    podman_settings, workspace, fake_cli,
):
    runtime = PodmanRuntime(podman_settings)
    current = await runtime.start(workspace, 45100, "u", "secret")
    orphan = "valeria-ocw-" + "b" * 32
    labels = {MANAGED_LABEL: "true", OWNER_LABEL: runtime.owner}
    fake_cli.inventory = [
        {"Names": [orphan], "Labels": labels},
        {"Names": [current.container_name], "Labels": labels},
        {"Names": ["precious-database"], "Labels": labels},
        {"Names": ["valeria-ocw-" + "c" * 32], "Labels": {**labels, OWNER_LABEL: "other"}},
        {"Names": ["valeria-ocw-" + "d" * 32], "Labels": {OWNER_LABEL: runtime.owner}},
    ]
    await runtime.reconcile()
    removed = [argv[-1] for argv, _ in fake_cli.calls if argv[1] == "rm"]
    assert removed == [orphan]


async def test_orphan_cleanup_does_not_require_image_or_current_launch_configuration(
    podman_settings, workspace, fake_cli,
):
    runtime = PodmanRuntime(podman_settings)
    orphan = "valeria-ocw-" + "b" * 32
    fake_cli.image_code = 1
    fake_cli.inventory = [{
        "Names": [orphan], "Labels": {MANAGED_LABEL: "true", OWNER_LABEL: runtime.owner},
    }]
    # Cleanup remains possible after image deletion or an invalid launch-policy edit.
    podman_settings.podman_forward_env = ("SESSION_SECRET",)
    podman_settings.podman_state_root = workspace / "invalid-state"
    await runtime.reconcile()
    assert [argv[1] for argv, _ in fake_cli.calls] == ["info", "ps", "rm"]
    assert fake_cli.calls[-1][0][-1] == orphan


async def test_cancellation_during_spawn_removes_its_container(
    podman_settings, workspace, fake_cli, monkeypatch,
):
    runtime = PodmanRuntime(podman_settings)
    await runtime.preflight()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def delayed(*argv, **kwargs):
        if argv[1] == "run":
            entered.set()
            await release.wait()
        return await fake_cli.execute(*argv, **kwargs)

    monkeypatch.setattr("opencode_web.podman_runtime.asyncio.create_subprocess_exec", delayed)
    task = asyncio.create_task(runtime.start(workspace, 45100, "u", "secret"))
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fake_cli.run.killed
    assert len([argv for argv, _ in fake_cli.calls if argv[1] == "rm"]) == 1


async def test_failed_aborted_start_blocks_replacement_until_cleanup_succeeds(
    podman_settings, workspace, fake_cli, monkeypatch,
):
    runtime = PodmanRuntime(podman_settings)
    active_workspace = workspace / "active"
    failed_workspace = workspace / "failed"
    active_workspace.mkdir()
    failed_workspace.mkdir()
    active = await runtime.start(active_workspace, 45100, "u", "active-secret")
    entered = asyncio.Event()
    release = asyncio.Event()
    delay_next_run = True

    async def delayed(*argv, **kwargs):
        nonlocal delay_next_run
        if argv[1] == "run" and delay_next_run:
            delay_next_run = False
            entered.set()
            await release.wait()
        return await fake_cli.execute(*argv, **kwargs)

    monkeypatch.setattr("opencode_web.podman_runtime.asyncio.create_subprocess_exec", delayed)
    fake_cli.removal_code = 1
    task = asyncio.create_task(runtime.start(failed_workspace, 45101, "u", "failed-secret"))
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(PodmanError, match="Cannot remove"):
        await task
    failed = fake_cli.run
    failed_name = next(
        argv[-1] for argv, _ in reversed(fake_cli.calls) if argv[1] == "rm"
    )
    assert failed.killed
    before_retry = len(fake_cli.calls)
    with pytest.raises(PodmanError, match="Cannot remove"):
        await runtime.start(failed_workspace, 45102, "u", "replacement-secret")
    assert [argv[1] for argv, _ in fake_cli.calls[before_retry:]] == ["rm"]
    assert fake_cli.calls[-1][0][-1] == failed_name
    with pytest.raises(PodmanError, match="Cannot remove"):
        await runtime.cleanup_pending_starts()
    fake_cli.removal_code = 0
    before_success = len(fake_cli.calls)
    replacement = await runtime.start(failed_workspace, 45102, "u", "replacement-secret")
    assert [argv[1] for argv, _ in fake_cli.calls[before_success:]] == ["rm", "run"]
    assert replacement.container_name != failed_name
    await runtime.cleanup_pending_starts()
    removed_names = [argv[-1] for argv, _ in fake_cli.calls if argv[1] == "rm"]
    assert set(removed_names) == {failed_name}
    assert not active.process.killed
    assert not replacement.process.killed
    assert active.container_name in runtime._owned_names


async def test_reconcile_retries_unhanded_orphan_cleanup_without_removing_active_workers(
    podman_settings, workspace, fake_cli,
):
    runtime = PodmanRuntime(podman_settings)
    active = await runtime.start(workspace, 45100, "u", "secret")
    orphan = "valeria-ocw-" + "b" * 32
    labels = {MANAGED_LABEL: "true", OWNER_LABEL: runtime.owner}
    fake_cli.inventory = [
        {"Names": [orphan], "Labels": labels},
        {"Names": [active.container_name], "Labels": labels},
    ]
    fake_cli.removal_code = 1
    with pytest.raises(PodmanError, match="Cannot remove"):
        await runtime.reconcile()
    before_retry = len(fake_cli.calls)
    with pytest.raises(PodmanError, match="Cannot remove"):
        await runtime.reconcile()
    assert [argv[1] for argv, _ in fake_cli.calls[before_retry:]] == ["rm"]
    fake_cli.removal_code = 0
    fake_cli.inventory = [{"Names": [active.container_name], "Labels": labels}]
    await runtime.reconcile()
    assert {
        argv[-1] for argv, _ in fake_cli.calls if argv[1] == "rm"
    } == {orphan}
    assert active.container_name in runtime._owned_names
    assert not active.process.killed


async def test_repeated_start_cancellation_keeps_creator_shielded_until_cleanup(
    podman_settings, workspace, fake_cli, monkeypatch,
):
    runtime = PodmanRuntime(podman_settings)
    entered = asyncio.Event()
    release = asyncio.Event()
    delay_next_run = True
    creators = []

    async def delayed(*argv, **kwargs):
        nonlocal delay_next_run
        if argv[1] == "run" and delay_next_run:
            delay_next_run = False
            entered.set()
            await release.wait()
        process = await fake_cli.execute(*argv, **kwargs)
        if argv[1] == "run":
            creators.append(process)
        return process

    monkeypatch.setattr("opencode_web.podman_runtime.asyncio.create_subprocess_exec", delayed)
    task = asyncio.create_task(runtime.start(workspace, 45100, "u", "secret"))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    replacement_task = asyncio.create_task(runtime.start(workspace, 45101, "u", "new-secret"))
    await asyncio.sleep(0)
    assert not replacement_task.done()
    assert not any(argv[1] == "rm" for argv, _ in fake_cli.calls)
    release.set()
    replacement = await replacement_task
    assert creators[0].killed
    assert replacement.process is creators[1]
    assert not replacement.process.killed
    commands = [argv[1] for argv, _ in fake_cli.calls]
    assert commands[-3:] == ["run", "rm", "run"]


async def test_cancellation_during_threaded_filesystem_check_never_launches_container(
    podman_settings, workspace, fake_cli, monkeypatch,
):
    runtime = PodmanRuntime(podman_settings)
    entered = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    original = runtime.build_launch

    def blocked_build(*args):
        entered.set()
        try:
            if not release.wait(5):
                raise RuntimeError("Filesystem check was never released")
            return original(*args)
        finally:
            completed.set()

    monkeypatch.setattr(runtime, "build_launch", blocked_build)
    task = asyncio.create_task(runtime.start(workspace, 45100, "u", "secret"))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not any(argv[1] == "run" for argv, _ in fake_cli.calls)
    finally:
        release.set()
        assert await asyncio.to_thread(completed.wait, 5)
    await runtime.cleanup_pending_starts()
    assert not any(argv[1] in {"run", "rm"} for argv, _ in fake_cli.calls)


@pytest.mark.skipif(
    os.getenv("RUN_PODMAN_SANDBOX_TESTS") != "1" or shutil.which("podman") is None,
    reason="Opt in with RUN_PODMAN_SANDBOX_TESTS=1 and a prepared rootless local image",
)
async def test_real_container_cannot_read_sibling_data(podman_settings, workspace, tmp_path):
    """Never pulls an image, creates users, or starts an external application service."""
    podman_settings.podman_bin = Path(shutil.which("podman"))
    podman_settings.podman_image = os.getenv("PODMAN_IMAGE", podman_settings.podman_image)
    sibling = workspace.parent / "outside-secret"
    sibling.write_text("must remain outside")
    link = workspace / "escape"
    link.symlink_to(sibling)
    runtime = PodmanRuntime(podman_settings)
    await runtime.preflight()
    launch = runtime.build_launch(workspace, 45100, "u", "unique-secret")
    index = launch.argv.index(runtime._image_id)
    command = [
        *launch.argv[:index + 1], "/bin/sh", "-c",
        'test ! -r "$1" && test ! -r "$2" && touch "$3"',
        "sandbox-check", str(sibling), str(link), str(workspace / "allowed-write"),
    ]
    process = await asyncio.create_subprocess_exec(
        *command, env=launch.env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(30):
            await process.communicate()
        assert process.returncode == 0
        assert (workspace / "allowed-write").is_file()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        runtime._owned_names.add(launch.container_name)
        await runtime.stop(launch.container_name)


def test_persistent_home_cannot_expose_an_external_hardlink(podman_settings, workspace, tmp_path):
    runtime = PodmanRuntime(podman_settings)
    launch = runtime.build_launch(workspace, 45100, "u", "secret")
    outside = tmp_path / "outside-home-secret"
    outside.write_text("private")
    os.link(outside, launch.home_path / "escape")
    with pytest.raises(PodmanError, match="hardlinked outside"):
        runtime.build_launch(workspace, 45100, "u", "secret")


@pytest.mark.parametrize("entry_type", ["socket", "fifo"])
def test_host_ipc_files_are_not_exposed_by_workspace_bind(
    podman_settings, workspace, entry_type,
):
    import socket

    entry = workspace / "host-ipc"
    listener = None
    try:
        if entry_type == "socket":
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(entry))
            listener.listen(1)
        else:
            os.mkfifo(entry)
        with pytest.raises(PodmanError, match="socket or special file"):
            PodmanRuntime(podman_settings).build_launch(workspace, 45100, "u", "secret")
    finally:
        if listener is not None:
            listener.close()
