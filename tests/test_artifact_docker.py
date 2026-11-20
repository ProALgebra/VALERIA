from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_v2_config_installs_artifact_mcp_and_allows_its_staging_root():
    config = (ROOT / "docker" / "opencode-v2.jsonc").read_text(encoding="utf-8")
    assert '"consultation-artifacts"' in config
    assert "/home/appuser/mcp/mcp-consultation-artifacts/venv/bin/python" in config
    assert "/home/appuser/mcp/mcp-consultation-artifacts/server.py" in config
    assert "VALERIA_ARTIFACT_TOKEN_FILE" in config
    assert "codemode" in config and "false" in config
    assert '"/app/data/consultation-staging/**"' in config
    assert '"effect": "allow"' in config


def test_dockerfile_creates_the_artifact_mcp_venv_before_copying_mcp():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    requirements_copy = "COPY mcp/mcp-consultation-artifacts/requirements.txt"
    venv_create = "python3.12 -m venv /home/appuser/mcp/mcp-consultation-artifacts/venv"
    copy_mcp = "COPY --chown=appuser:appuser mcp /home/appuser/mcp"
    assert requirements_copy in dockerfile
    assert venv_create in dockerfile
    assert dockerfile.index(requirements_copy) < dockerfile.index(venv_create)
    assert dockerfile.index(venv_create) < dockerfile.index(copy_mcp)
