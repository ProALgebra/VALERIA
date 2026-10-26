from __future__ import annotations

import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_compose_keeps_mutable_runtime_directories_under_data() -> None:
    compose = (PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8")

    expected_mounts = {
        "./data:/app/data",
        "./data/workspaces:/home/appuser/workspaces",
        "./data/opencode/share:/home/appuser/.local/share/opencode",
        "./data/opencode/state:/home/appuser/.local/state/opencode",
        "./data/opencode/cache:/home/appuser/.cache/opencode",
        "./data/mcp/memory:/home/appuser/.mcp-memory",
        "./data/conan:/home/appuser/.conan2",
    }
    for mount in expected_mounts:
        assert mount in compose

    legacy_mounts = {
        "./workspaces:",
        "./opencode/.data/",
        "./mcp/.data/",
        "./runtime/",
    }
    for mount in legacy_mounts:
        assert mount not in compose


def test_docker_gpg_signing_uses_files_outside_the_image() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8")
    example_env = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")

    assert "gnupg" in dockerfile
    assert 'ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]' in dockerfile
    assert "GNUPGHOME: /app/data/gnupg" in compose
    assert "GIT_GPG_SIGNING_ENABLED=false" in example_env
    assert "GIT_GPG_PRIVATE_KEY_FILE=/app/.secrets/gpg/private.asc" in example_env
    assert "GIT_GPG_PASSPHRASE_FILE=/app/.secrets/gpg/passphrase" in example_env


def test_docker_shell_scripts_have_valid_syntax() -> None:
    for script in ("docker-entrypoint.sh", "git-gpg-sign"):
        subprocess.run(
            ["sh", "-n", str(PROJECT_ROOT / "docker" / script)],
            check=True,
        )

