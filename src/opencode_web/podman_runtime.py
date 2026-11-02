"""Rootless, per-workspace OpenCode containers with no implicit host mounts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import stat
import sys
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Settings

MANAGED_LABEL = "io.valeria.opencode.managed"
OWNER_LABEL = "io.valeria.opencode.owner"
WORKSPACE_LABEL = "io.valeria.opencode.workspace"
CONTAINER_PORT = 4100
CONTAINER_HOME = "/home/agent"
CONTAINER_CONFIG = "/etc/valeria/opencode"
_NAME_RE = re.compile(r"valeria-ocw-[0-9a-f]{32}\Z")
_IMAGE_ID_RE = re.compile(r"(?:sha256:)?[0-9a-f]{64}\Z")
_ENV_NAME_RE = re.compile(r"[A-Z_][A-Z0-9_]*\Z")
_HOST_ENV = (
    "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "XDG_RUNTIME_DIR",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "DBUS_SESSION_BUS_ADDRESS",
)
_FORBIDDEN_ENV_PREFIXES = (
    "APP_", "SESSION_", "BOOTSTRAP_", "OPENCODE_", "PODMAN_", "CONTAINER_",
    "CONTAINERS_", "_CONTAINERS_", "BUILDAH_", "REGISTRY_", "STORAGE_", "SYSTEMD_",
    "DOCKER_", "VALERIA_", "MATTERMOST_", "XDG_", "LD_", "DYLD_", "PYTHON",
    "BASH", "SSH_", "GIT_CONFIG", "GIT_SSH",
)
_FORBIDDEN_ENV = {
    "PATH", "HOME", "ENV", "SHELLOPTS", "BASHOPTS", "NODE_OPTIONS",
    "NODE_PATH", "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO", "GNUPGHOME",
    "TMPDIR", "TMP", "TEMP", "GCONV_PATH", "LOCPATH", "GLIBC_TUNABLES",
    "GODEBUG", "GOTRACEBACK", "DBUS_SESSION_BUS_ADDRESS",
}
# These paths contain image executables/configuration or kernel interfaces.
# /opt/valeria/data/workspaces and descendants of /tmp remain valid workspaces.
_RESERVED_PATHS = tuple(Path(value) for value in (
    "/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/proc", "/sys",
    "/dev", "/run", CONTAINER_HOME, "/opt/valeria/mcp", "/opt/valeria/venv",
    "/opt/venv",
))


class PodmanError(RuntimeError):
    """A container cannot safely be prepared, started, or removed."""


@dataclass(frozen=True, slots=True)
class PodmanLaunch:
    argv: tuple[str, ...]
    env: dict[str, str] = field(repr=False)
    container_name: str
    home_path: Path


@dataclass(frozen=True, slots=True)
class PodmanProcess:
    process: asyncio.subprocess.Process
    container_name: str


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _mount_path(path: Path) -> str:
    value = str(path)
    if any(char in value for char in (",", "\n", "\r", "\0")):
        raise PodmanError("Container mount paths cannot contain commas or control characters")
    return value


class PodmanRuntime:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.owner = hashlib.sha256(
            str(settings.database_path.expanduser().resolve()).encode()
        ).hexdigest()[:24]
        self._owned_names: set[str] = set()
        self._pending_start_cleanup: dict[
            str, asyncio.Task[asyncio.subprocess.Process] | None
        ] = {}
        self._startup_lock = asyncio.Lock()
        self._image_id: str | None = None
        self._preflight_lock = asyncio.Lock()

    @staticmethod
    def _engine_environment() -> dict[str, str]:
        environment = {key: os.environ[key] for key in _HOST_ENV if key in os.environ}
        environment["PATH"] = os.defpath
        return environment

    async def _cli(self, *args: str, timeout: float = 10) -> tuple[int, bytes]:
        try:
            process = await asyncio.create_subprocess_exec(
                str(self.settings.podman_bin), *args,
                env=self._engine_environment(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise PodmanError("Podman executable is unavailable") from exc
        try:
            async with asyncio.timeout(timeout):
                stdout, _ = await process.communicate()
        except BaseException:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
            await process.wait()
            raise
        if len(stdout) > 1024 * 1024:
            raise PodmanError("Podman returned an unexpectedly large response")
        return process.returncode, stdout

    async def _check_engine(self) -> None:
        """Cleanup needs a rootless engine, even after its worker image is removed."""
        if sys.platform != "linux" or os.geteuid() == 0:
            raise PodmanError("Isolated mode requires Linux and a non-root service user")
        if not self.settings.podman_bin.is_absolute():
            raise PodmanError("PODMAN_BIN must be an absolute executable path")
        try:
            code, output = await self._cli("info", "--format", "{{.Host.Security.Rootless}}")
        except TimeoutError as exc:
            raise PodmanError("Podman readiness check timed out") from exc
        if code or output.strip() != b"true":
            raise PodmanError("Podman must use a working rootless runtime")

    def validate_configuration(self) -> None:
        """Validate policy even when no user has been assigned a workspace yet."""
        if not self.settings.podman_bin.is_absolute():
            raise PodmanError("PODMAN_BIN must be an absolute executable path")
        image = self.settings.podman_image
        if (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,254}", image)
            or image.endswith(":latest")
            or ":" not in image.rsplit("/", 1)[-1]
        ):
            raise PodmanError("PODMAN_IMAGE must specify a version tag or image digest")
        roots = tuple(root.expanduser().resolve() for root in self.settings.allowed_workspace_roots)
        state = self.settings.podman_state_root.expanduser().resolve()
        if state == Path("/") or any(_overlaps(state, root) for root in roots):
            raise PodmanError(
                "PODMAN_STATE_ROOT must be separate from every allowed workspace root"
            )
        if state.exists():
            metadata = state.stat()
            if not state.is_dir() or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
                raise PodmanError(
                    "Podman state directories must be owned by the service user with mode 0700"
                )
        if (
            self.settings.podman_memory_mb < 128
            or not math.isfinite(self.settings.podman_cpus)
            or self.settings.podman_cpus <= 0
            or self.settings.podman_pids_limit < 16
        ):
            raise PodmanError("Invalid Podman resource limits")
        for name in self.settings.podman_forward_env:
            if (
                not _ENV_NAME_RE.fullmatch(name)
                or name in _FORBIDDEN_ENV
                or name.startswith(_FORBIDDEN_ENV_PREFIXES)
            ):
                raise PodmanError("PODMAN_FORWARD_ENV contains a protected environment variable")
        if self.settings.podman_config_dir is not None:
            config = self.settings.podman_config_dir.expanduser().resolve()
            config_file = config / "opencode.jsonc"
            if (
                not config.is_dir() or not config_file.is_file()
                or not os.access(config_file, os.R_OK)
                or any(_overlaps(config, root) for root in roots)
                or _overlaps(config, state)
            ):
                raise PodmanError(
                    "PODMAN_CONFIG_DIR must contain readable opencode.jsonc "
                    "outside workspaces and state"
                )
            self._check_sensitive_paths(config, "PODMAN_CONFIG_DIR")
            _mount_path(config)
        _mount_path(state)

    async def preflight(self, *, force: bool = False) -> None:
        """Check rootless execution and resolve an existing local image to its ID."""
        async with self._preflight_lock:
            self.validate_configuration()
            if self._image_id is not None and not force:
                return
            self._image_id = None
            image = self.settings.podman_image
            await self._check_engine()
            try:
                code, _ = await self._cli("image", "exists", "--", image)
                if code:
                    raise PodmanError("The configured Podman image must already exist locally")
                code, output = await self._cli(
                    "image", "inspect", "--format", "{{.Id}}", "--", image,
                )
                image_id = output.decode(errors="replace").strip()
                if code or not _IMAGE_ID_RE.fullmatch(image_id):
                    raise PodmanError("Cannot identify the local Podman image")
                self._image_id = image_id
            except TimeoutError as exc:
                raise PodmanError("Podman readiness check timed out") from exc

    @staticmethod
    def _nested_mounts(workspace: Path) -> list[Path]:
        try:
            mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
        except OSError as exc:
            raise PodmanError("Cannot verify workspace mount boundaries") from exc
        result = []
        for line in mountinfo.splitlines():
            fields = line.split()
            if len(fields) < 6:
                raise PodmanError("Cannot parse host mount boundaries")
            mountpoint = re.sub(
                r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), fields[4]
            )
            path = Path(mountpoint)
            if path != workspace and path.is_relative_to(workspace):
                result.append(path)
        return result

    def _private_directory(self, path: Path) -> None:
        if path.is_symlink():
            raise PodmanError("Podman state directories must not be symlinks")
        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            metadata = path.stat()
        except OSError as exc:
            raise PodmanError("Cannot create private Podman state") from exc
        if not path.is_dir() or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            raise PodmanError(
                "Podman state directories must be owned by the service user with mode 0700"
            )

    @staticmethod
    def _check_hardlinks(workspace: Path) -> None:
        """Existing host hardlinks must not smuggle files across the bind boundary."""
        links: dict[tuple[int, int], list[int]] = {}
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

        def scan(directory_fd: int, device: int) -> None:
            with os.scandir(directory_fd) as entries:
                for entry in entries:
                    metadata = entry.stat(follow_symlinks=False)
                    if metadata.st_dev != device:
                        raise PodmanError("Workspace mount boundaries changed during inspection")
                    if stat.S_ISDIR(metadata.st_mode):
                        child_fd = os.open(entry.name, flags, dir_fd=directory_fd)
                        try:
                            opened = os.fstat(child_fd)
                            if (opened.st_dev, opened.st_ino) != (
                                metadata.st_dev, metadata.st_ino,
                            ):
                                raise PodmanError("Workspace changed during boundary inspection")
                            scan(child_fd, device)
                        finally:
                            os.close(child_fd)
                    elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink > 1:
                        key = (metadata.st_dev, metadata.st_ino)
                        observed = links.setdefault(key, [0, 0])
                        observed[0] += 1
                        observed[1] = max(observed[1], metadata.st_nlink)
                    elif not (stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)):
                        raise PodmanError("Workspace contains a socket or special file")

        try:
            root_fd = os.open(workspace, flags)
            try:
                scan(root_fd, os.fstat(root_fd).st_dev)
            finally:
                os.close(root_fd)
        except (OSError, RecursionError) as exc:
            raise PodmanError("Cannot verify workspace hardlink boundaries") from exc
        if any(total > internal for internal, total in links.values()):
            raise PodmanError("Workspace contains a file hardlinked outside its boundary")

    def _check_sensitive_paths(self, workspace: Path, subject: str = "Workspace") -> None:
        files = [
            self.settings.database_path.expanduser().resolve(),
            self.settings.podman_bin.expanduser().resolve(), Path(sys.executable).resolve(),
        ]
        for name in ("bootstrap_admin_password_file", "mattermost_token_file"):
            path = getattr(self.settings, name, None)
            if path is not None:
                files.append(path.expanduser().resolve())
        dotenv = Path.cwd() / ".env"
        if dotenv.exists():
            files.append(dotenv.resolve())
        if any(path == workspace or path.is_relative_to(workspace) for path in files):
            raise PodmanError(f"{subject} contains protected backend files")
        host_home = Path.home().resolve()
        config_home = Path(os.getenv("XDG_CONFIG_HOME", host_home / ".config")).resolve()
        data_home = Path(os.getenv("XDG_DATA_HOME", host_home / ".local/share")).resolve()
        directories = {
            host_home / ".config/containers", host_home / ".local/share/containers",
            config_home / "containers", data_home / "containers",
            Path(__file__).resolve().parent,
        }
        if runtime_dir := os.getenv("XDG_RUNTIME_DIR"):
            directories.add(Path(runtime_dir).resolve())
        if any(_overlaps(workspace, path.resolve()) for path in directories):
            raise PodmanError(f"{subject} overlaps protected backend or container-engine state")

    def build_launch(
        self, workspace: Path, port: int, username: str, password: str,
    ) -> PodmanLaunch:
        """Build argv without a shell; credentials stay out of the process arguments."""
        self.validate_configuration()
        workspace = self.settings.validate_workspace(workspace)
        _mount_path(workspace)
        if any(_overlaps(workspace, reserved) for reserved in _RESERVED_PATHS):
            raise PodmanError("Workspace conflicts with the container runtime filesystem")
        self._check_sensitive_paths(workspace)
        if not 1024 <= port <= 65535 or not username or not password:
            raise PodmanError("A valid local port and unique OpenCode credentials are required")
        state_root = self.settings.podman_state_root.expanduser().resolve()
        if self._nested_mounts(workspace):
            raise PodmanError("Workspace contains nested host mounts")
        self._check_hardlinks(workspace)
        memory = self.settings.podman_memory_mb
        cpus = self.settings.podman_cpus
        pids = self.settings.podman_pids_limit
        environment = self._engine_environment()
        forwarded = []
        for name in dict.fromkeys(self.settings.podman_forward_env):
            if name in os.environ:
                environment[name] = os.environ[name]
                forwarded.append(name)
        workspace_key = hashlib.sha256(str(workspace).encode()).hexdigest()
        home_parent = state_root / workspace_key
        home = home_parent / "home"
        # Only the individual home enters the container, never the state root.
        for directory in (state_root, home_parent, home):
            self._private_directory(directory)
        if self._nested_mounts(home):
            raise PodmanError("Podman home contains nested host mounts")
        self._check_hardlinks(home)
        name = f"valeria-ocw-{uuid.uuid4().hex}"
        argv = [
            str(self.settings.podman_bin), "run", "--rm", "--pull=never", "--name", name,
            "--label", f"{MANAGED_LABEL}=true", "--label", f"{OWNER_LABEL}={self.owner}",
            "--label", f"{WORKSPACE_LABEL}={workspace_key}",
            "--read-only", "--cap-drop=all", "--security-opt=no-new-privileges",
            "--userns=keep-id", "--user", f"{os.getuid()}:{os.getgid()}",
            "--network=private", "--pid=private", "--ipc=private", "--uts=private",
            "--http-proxy=false", "--image-volume=ignore",
            "--memory", f"{memory}m", "--cpus", str(cpus), "--pids-limit", str(pids),
            "--publish", f"127.0.0.1:{port}:{CONTAINER_PORT}",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m",
            "--tmpfs", "/run:rw,nosuid,nodev,size=16m",
            "--tmpfs", (
                "/run/agent:rw,nosuid,nodev,size=16m,mode=0700,"
                f"uid={os.getuid()},gid={os.getgid()}"
            ),
            "--mount", f"type=bind,src={_mount_path(workspace)},dst={_mount_path(workspace)}",
            "--mount", f"type=bind,src={_mount_path(home)},dst={CONTAINER_HOME}",
            "--workdir", str(workspace),
            "--env", f"HOME={CONTAINER_HOME}",
            "--env", f"XDG_DATA_HOME={CONTAINER_HOME}/.local/share",
            "--env", f"XDG_STATE_HOME={CONTAINER_HOME}/.local/state",
            "--env", f"XDG_CACHE_HOME={CONTAINER_HOME}/.cache",
            "--env", f"XDG_CONFIG_HOME={CONTAINER_HOME}/.config",
            "--env", "XDG_RUNTIME_DIR=/run/agent",
        ]
        config_dir = self.settings.podman_config_dir
        if config_dir is not None:
            config = config_dir.expanduser().resolve()
            argv += [
                "--mount", f"type=bind,src={_mount_path(config)},dst={CONTAINER_CONFIG},ro=true",
                "--env", f"OPENCODE_CONFIG={CONTAINER_CONFIG}/opencode.jsonc",
            ]
        environment["OPENCODE_SERVER_USERNAME"] = username
        environment["OPENCODE_SERVER_PASSWORD"] = password
        for key in ["OPENCODE_SERVER_USERNAME", "OPENCODE_SERVER_PASSWORD", *forwarded]:
            argv += ["--env", key]
        argv += [
            self._image_id or self.settings.podman_image,
            "/usr/bin/opencode", "serve", "--hostname", "0.0.0.0",
            "--port", str(CONTAINER_PORT),
        ]
        return PodmanLaunch(tuple(argv), environment, name, home)

    async def start(
        self, workspace: Path, port: int, username: str, password: str,
    ) -> PodmanProcess:
        async with self._startup_lock:
            # An aborted start has no manager instance to reserve its workspace.
            # Do not launch another worker until every such removal is confirmed.
            await self._cleanup_pending_starts()
            await self.preflight()
            launch = await asyncio.to_thread(
                self.build_launch, workspace, port, username, password,
            )
            self._owned_names.add(launch.container_name)
            spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                *launch.argv, env=launch.env, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            ))
            try:
                process = await asyncio.shield(spawn)
            except asyncio.CancelledError:
                # Retain the shielded creator too: another cancellation must not
                # let cleanup race a process which can still create its container.
                self._pending_start_cleanup[launch.container_name] = spawn
                await self._cleanup_pending_starts()
                raise
            except OSError as exc:
                self._owned_names.discard(launch.container_name)
                raise PodmanError("Cannot launch the isolated OpenCode runtime") from exc
            return PodmanProcess(process, launch.container_name)

    async def cleanup_pending_starts(self) -> None:
        """Retry unhanded startup/orphan cleanup without touching active workers."""
        async with self._startup_lock:
            await self._cleanup_pending_starts()

    async def _cleanup_pending_starts(self) -> None:
        for name, spawn in tuple(self._pending_start_cleanup.items()):
            if spawn is not None:
                try:
                    process = await asyncio.shield(spawn)
                except OSError:
                    self._owned_names.discard(name)
                    self._pending_start_cleanup.pop(name, None)
                    continue
                if process.returncode is None:
                    with suppress(ProcessLookupError):
                        process.kill()
                await process.wait()
            await self.stop(name)

    async def stop(self, container_name: str) -> None:
        if container_name not in self._owned_names or not _NAME_RE.fullmatch(container_name):
            raise PodmanError("Refusing to remove a container not owned by this runtime")
        try:
            code, _ = await self._cli(
                "rm", "--force", "--ignore", "--time", "10", container_name, timeout=15,
            )
        except TimeoutError as exc:
            raise PodmanError("Isolated OpenCode cleanup timed out") from exc
        if code:
            raise PodmanError("Cannot remove the isolated OpenCode container")
        self._owned_names.discard(container_name)
        self._pending_start_cleanup.pop(container_name, None)

    async def reconcile(self) -> None:
        """Remove only previous containers belonging to this backend deployment."""
        async with self._startup_lock:
            await self._cleanup_pending_starts()
            await self._reconcile()

    async def _reconcile(self) -> None:
        await self._check_engine()
        try:
            code, output = await self._cli(
                "ps", "--all", "--filter", f"label={MANAGED_LABEL}=true",
                "--filter", f"label={OWNER_LABEL}={self.owner}", "--format", "json",
            )
        except TimeoutError as exc:
            raise PodmanError("Cannot inspect previously managed containers") from exc
        if code:
            raise PodmanError("Cannot inspect previously managed containers")
        try:
            containers = json.loads(output)
        except (ValueError, UnicodeDecodeError) as exc:
            raise PodmanError("Podman returned an invalid container inventory") from exc
        if not isinstance(containers, list):
            raise PodmanError("Podman returned an invalid container inventory")
        for container in containers:
            if not isinstance(container, dict):
                continue
            labels = container.get("Labels")
            names = container.get("Names")
            if (
                not isinstance(labels, dict)
                or labels.get(MANAGED_LABEL) != "true"
                or labels.get(OWNER_LABEL) != self.owner
            ):
                continue
            if isinstance(names, str):
                names = [names]
            if not isinstance(names, list):
                continue
            for name in names:
                if (
                    isinstance(name, str) and _NAME_RE.fullmatch(name)
                    and name not in self._owned_names
                ):
                    self._owned_names.add(name)
                    # A failed orphan removal must also block the next launch;
                    # ownership alone would make subsequent inventories skip it.
                    self._pending_start_cleanup[name] = None
                    await self.stop(name)
