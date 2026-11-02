# OpenCode workspace isolation on Linux

Valeria supports two global execution modes: `direct` starts OpenCode on the host
and remains the default; `podman` runs one rootless OCI worker per workspace.
The mode is stored in SQLite and changed by an administrator in the application.
Installing Podman or setting the variables below does not change it.

In `podman` mode the worker contains OpenCode, local MCP processes and build tools.
Its image filesystem is read-only. The assigned workspace is mounted writable at
the **same absolute path as on the host**, and that worker has its own persistent
`/home/agent`. Host application code, the Valeria database, other workspaces, shared
OpenCode state, SSH/GPG credentials and container-engine sockets are not mounted.
Isolated mode requires a distinct, non-overlapping assigned workspace for each
account; assign separate folders before switching if the legacy deployment shared
workspaces between users.

This is filesystem and process isolation, not an outbound network policy: workers
need network access to their configured providers and repositories. A credential
passed to a worker grants that credential's remote permissions. Restrict remote
credentials accordingly; do not expose administrative MCP services to workers.

## Prepare a rootless service account

Use a dedicated non-root Linux account for both the backend and Podman. Existing
root deployments are not converted automatically. Plan a maintenance window and
preserve the current database, workspaces, configuration and ownership before
moving an existing deployment to this account.

For a new Ubuntu 24.04 host, the administrator can prepare the packages and account:

```bash
sudo apt update
sudo apt install podman uidmap slirp4netns fuse-overlayfs dbus-user-session
sudo adduser --home /var/lib/valeria --disabled-password --gecos '' valeria
sudo loginctl enable-linger valeria
```

Verify that `valeria` has a non-overlapping subordinate UID and GID range in
`/etc/subuid` and `/etc/subgid`. `adduser` normally allocates these; if it did not,
have the host administrator allocate an unused range. Do not paste a fixed range
over ranges already assigned to other users. Rootless Podman requires user
namespaces, a supported local filesystem for container storage, and a user-mode
networking helper. Newer Podman releases use `pasta` from the `passt` package;
Ubuntu's packaged version may use `slirp4netns`. See the official
[rootless setup guide](https://github.com/containers/podman/blob/main/docs/tutorials/rootless_tutorial.md)
and [rootless storage requirements](https://docs.podman.io/en/latest/markdown/podman.1.html#rootless-mode).

Log in as `valeria` through a real login session for the following setup commands.
Check that `XDG_RUNTIME_DIR` points to `/run/user/$(id -u)`, that this directory is
owned by the account, and that cgroup v2 is available:

```bash
id
printf '%s\n' "$XDG_RUNTIME_DIR"
test -d "$XDG_RUNTIME_DIR"
stat -fc %T /sys/fs/cgroup
podman info --format '{{.Host.Security.Rootless}} {{.Host.CgroupsVersion}}'
```

Expect `cgroup2fs`, and `true v2` from Podman. `sudo podman` uses root's separate
container storage and is not the runtime used by Valeria. Do not replace a missing
user runtime directory with a directory under `/tmp`. Podman's runtime and image
storage locations are described in its
[environment reference](https://docs.podman.io/en/latest/markdown/podman.1.html#environment).

## Build the worker image and configure providers

From the repository as the service account:

```bash
cd /opt/valeria
podman build -f docker/Containerfile.opencode -t localhost/valeria-opencode:1.18.25 .
install -d -m 0700 /var/lib/valeria/opencode-config
install -m 0600 docker/opencode-isolated.jsonc /var/lib/valeria/opencode-config/opencode.jsonc
```

The worker image includes OpenCode `1.18.25`, Conan `2.27.0`, Node 22, Python 3.12,
Git, SSH, GPG, CMake, Ninja, a C/C++ toolchain and local Memory/GitLab/Vision MCP
code. It does not depend on the missing legacy `opencode/config` directory and
does not include the web backend or any secret files. Base OS packages and the
Memory/GitLab Python dependency ranges follow the main image; rebuilds are not
bit-for-bit reproducible. Vision uses its existing lock file.

Configure the model/provider manually in the copied JSONC before enabling the
mode. The example intentionally selects no model. The image disables
default/external plugins, automatic update checks and LSP downloads using
OpenCode's [documented environment flags](https://opencode.ai/docs/cli/#environment-variables).
OAuth flows that rely on plugins are not configured; use an API key or prepare a
separate reviewed worker image if such an integration is required.
For example, add the following entries for an existing OpenAI provider account,
substituting a model ID available to that account:

```jsonc
{
  "model": "openai/YOUR_MODEL_ID",
  "provider": {
    "openai": {
      "options": { "apiKey": "{env:OPENAI_API_KEY}" }
    }
  }
}
```

Merge these entries into the example rather than replacing its MCP settings.
Place `OPENAI_API_KEY` in the backend's protected environment file and explicitly
include its name in `PODMAN_FORWARD_ENV`. Host OpenCode login files and provider
credentials are not copied into worker homes. Each forwarded variable is visible
to code executing in every worker; use a credential broker or separate deployment
if different workspaces require different remote identities.

`PODMAN_CONFIG_DIR` is optional. When set, the directory is mounted read-only at
`/etc/valeria/opencode` and `OPENCODE_CONFIG` points to its `opencode.jsonc`.
OpenCode supports JSONC, environment references and local MCP commands; see its
[config reference](https://opencode.ai/docs/config/) and
[local MCP configuration](https://opencode.ai/docs/mcp-servers/#local).
OpenCode merges project settings with this file, so a read-only config is not an
authorization boundary. Project commands execute inside the worker; external
plugins remain disabled by the image defaults.

The supplied MCP example enables Memory with a private DB under `/home/agent`.
GitLab and Vision remain disabled until their URLs, credentials and environment
allowlist are configured. Their local executables are already in the image, so
no `npx` installation is needed. GitLab's repository roots resolve from the current
workspace. GitLab API tokens still authorize access to all projects permitted by
those tokens; the MCP's historical `readonly` directory name does not prevent
writes. Do not add Valeria Administrator MCP or a host-side filesystem MCP.

## Backend environment and systemd

Add these values to the protected environment file used by the backend, adjusting
absolute paths for the deployment:

```dotenv
PODMAN_BIN=/usr/bin/podman
PODMAN_IMAGE=localhost/valeria-opencode:1.18.25
PODMAN_STATE_ROOT=/opt/valeria/data/podman
PODMAN_CONFIG_DIR=/var/lib/valeria/opencode-config
PODMAN_MEMORY_MB=4096
PODMAN_CPUS=2
PODMAN_PIDS_LIMIT=512
PODMAN_FORWARD_ENV=OPENAI_API_KEY
```

The default state root is `./data/podman`; all paths are resolved by the backend.
`PODMAN_FORWARD_ENV` defaults to empty and lists variable **names**, never values.
Application/session/bootstrap/admin settings and host runtime/configuration
variables are rejected even when listed. Host CA-file paths are not inherited.
For a private CA, place a public CA bundle in the optional config directory and
refer to its container path from your provider or MCP configuration.

The service account must own its state directories and have write access to the
assigned workspaces. Keep the backend environment, database and Podman storage
outside every assigned workspace. Do not assign the deployment root, a parent of
another workspace, `/home/agent`, or a worker runtime/configuration directory as a
workspace. Worker state persists independently from the source workspace.
Before each worker starts, its workspace and private home are checked for host
sockets, FIFOs, device files, nested mounts and hardlinks reaching outside their
directory. Such entries are rejected because they can expose host files or
services through an otherwise narrow bind mount. Remove obsolete IPC files before
restarting a worker; ordinary files, directories, internal hardlinks and symlinks
remain supported.

An example **user** unit at
`/var/lib/valeria/.config/systemd/user/valeria-web.service` is:

```ini
[Unit]
Description=Valeria backend with rootless OpenCode workers

[Service]
Type=exec
WorkingDirectory=/opt/valeria
EnvironmentFile=/opt/valeria/.env
ExecStart=/opt/valeria/.venv/bin/opencode-web
Delegate=yes
KillMode=mixed
TimeoutStopSec=45
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

Use the actual home directory created for `valeria` instead of `/var/lib/valeria`
if they differ. The user manager supplies `HOME`, `XDG_RUNTIME_DIR` and its session
bus. `Delegate=yes` allows the container runtime to manage child cgroups; keep
cgroup v2 and delegated memory/CPU/PID controllers available so limits are enforced.
The meaning of delegation is defined by
[systemd's resource-control reference](https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html#Delegate=).
Do not add `NoNewPrivileges=yes` to the **launcher** unit: rootless setup may need
the `newuidmap`/`newgidmap` helpers; see
[systemd's privilege restriction](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html#NoNewPrivileges=).
Workers themselves use no-new-privileges and
drop capabilities. For details, see Podman's
[systemd integration](https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html)
and [run options](https://docs.podman.io/en/latest/markdown/podman-run.1.html).

During the planned migration, stop the legacy system service before starting this
user service; do not run both against the same database or port. As `valeria`:

```bash
systemctl --user daemon-reload
systemctl --user enable --now valeria-web.service
systemctl --user status valeria-web.service
journalctl --user -u valeria-web.service -f
```

An administrator can address the account's user manager with
`sudo systemctl --machine=valeria@.host --user status valeria-web.service`.
The repository's existing root unit and `run-local.sh` are not rewritten by this
setup; use the matching systemd manager when operating the new service.

## Switch modes and operate workers

After provisioning, sign in as an administrator and select the global execution
mode in the application. Switching is allowed only when no jobs are queued,
dispatching, running, cancelling or waiting for interaction, and no OpenCode
requests still hold a worker lease. Before switching, the backend saves the
OpenCode history needed for reading chats and task runs in SQLite. If a required
history snapshot cannot be obtained, the switch fails and the current mode stays
active. The backend then closes the workers for the previous mode.

Ordinary chats and consultations belong to the mode in which they were created.
Conversations from the other mode are archived for reading and cannot receive
new messages there. Switching back makes that mode's conversations available to
continue against their original OpenCode state. Both modes retain separate
OpenCode state; workspaces themselves retain the same files across a switch.
Task definitions, run history, saved results and history snapshots stay in SQLite.

The runtime uses `--userns=keep-id`, a read-only image, dropped capabilities and
resource limits. OpenCode listens on `0.0.0.0:4100` inside the worker; only a
backend-managed host port on `127.0.0.1` is published, with a unique credential for
that worker. Workspaces retain their absolute paths and persistent homes retain
OpenCode sessions, caches, Memory and per-workspace Git configuration. Podman's
[keep-id and read-only behavior](https://docs.podman.io/en/latest/markdown/podman-run.1.html)
explains the UID mapping and temporary writable directories.

No common SSH directory, GPG key, passphrase file or signing agent is imported.
Configure a restricted workspace credential explicitly if Git network access is
needed. Do not copy the legacy shared signing key into every home. Read-only
mounts still expose file contents, so keep secrets out of the shared config
directory unless all workers are intended to possess them.

Inspect workers as the same Linux account using `podman ps` and `podman logs`.
The backend owns worker startup, readiness, idle shutdown and removal; do not
attach a second restart policy or separate Quadlet for these dynamic workers.
Deleting a container leaves the workspace and persistent HOME intact. Before
deleting state, stop the backend and back up both `data/podman` and the Valeria
database. Switching back to `direct` is the application-level rollback; it still
requires an idle queue and successful history snapshots. It archives the Podman
conversations and restores access to the conversations created in `direct` mode.
