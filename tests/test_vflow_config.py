from __future__ import annotations

import httpx
import pytest

from opencode_web.app import create_app
from opencode_web.vflow.config import VFlowConfig, load_config
from opencode_web.vflow.limits import VFlowLimits


def test_default_config() -> None:
    config = load_config({})
    assert config == VFlowConfig(
        enabled=True,
        limits=VFlowLimits(),
        runs_per_minute=30,
        rate_window_seconds=60.0,
        retention_days=30,
    )


def test_environment_overrides() -> None:
    config = load_config(
        {
            "VFLOW_ENABLED": "false",
            "VFLOW_MAX_FORK_N": "8",
            "VFLOW_MAX_ACTIVE_POINTS": "16",
            "VFLOW_MAX_TOTAL_POINTS": "32",
            "VFLOW_MAX_TRANSITIONS": "64",
            "VFLOW_MAX_JSON_BYTES": "4096",
            "VFLOW_MAX_JSON_DEPTH": "8",
            "VFLOW_MAX_RUN_SECONDS": "3600",
            "VFLOW_RUNS_PER_MINUTE": "5",
            "VFLOW_RUN_WINDOW_SECONDS": "12.5",
            "VFLOW_RETENTION_DAYS": "7",
        }
    )
    assert config.enabled is False
    assert config.limits == VFlowLimits(
        max_fork_n=8,
        max_active_points=16,
        max_total_points=32,
        max_transitions=64,
        max_json_bytes=4096,
        max_json_depth=8,
        max_run_seconds=3600,
    )
    assert config.runs_per_minute == 5
    assert config.rate_window_seconds == 12.5
    assert config.retention_days == 7


def test_blank_values_fall_back_to_defaults() -> None:
    config = load_config(
        {
            "VFLOW_ENABLED": " ",
            "VFLOW_MAX_FORK_N": "",
            "VFLOW_RUNS_PER_MINUTE": "  ",
            "VFLOW_RUN_WINDOW_SECONDS": "",
        }
    )
    assert config == VFlowConfig()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("VFLOW_ENABLED", "maybe"),
        ("VFLOW_MAX_FORK_N", "0"),
        ("VFLOW_MAX_FORK_N", "-2"),
        ("VFLOW_MAX_FORK_N", "many"),
        ("VFLOW_MAX_ACTIVE_POINTS", "0"),
        ("VFLOW_MAX_TOTAL_POINTS", "0"),
        ("VFLOW_MAX_TRANSITIONS", "0"),
        ("VFLOW_MAX_JSON_BYTES", "0"),
        ("VFLOW_MAX_JSON_DEPTH", "0"),
        ("VFLOW_MAX_RUN_SECONDS", "0"),
        ("VFLOW_RUNS_PER_MINUTE", "0"),
        ("VFLOW_RETENTION_DAYS", "0"),
        ("VFLOW_RETENTION_DAYS", "-1"),
        ("VFLOW_RETENTION_DAYS", "forever"),
        ("VFLOW_RUN_WINDOW_SECONDS", "0"),
        ("VFLOW_RUN_WINDOW_SECONDS", "-1"),
        ("VFLOW_RUN_WINDOW_SECONDS", "nan"),
        ("VFLOW_RUN_WINDOW_SECONDS", "inf"),
        ("VFLOW_RUN_WINDOW_SECONDS", "soon"),
    ],
)
def test_invalid_values_raise_value_error(name: str, value: str) -> None:
    with pytest.raises(ValueError, match=name):
        load_config({name: value})


async def login_admin(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/auth/login",
        json={
            "login": "admin",
            "password": "correct-horse-battery-staple",
        },
    )
    assert response.status_code == 200, response.text


async def test_disabled_feature_flag_removes_routes_and_public_config(
    settings, monkeypatch
) -> None:
    monkeypatch.setenv("VFLOW_ENABLED", "false")
    app = create_app(settings, start_dispatcher=False)
    assert app.state.vflow_config.enabled is False

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        await login_admin(client)

        public_config = await client.get("/api/config/public")
        assert public_config.status_code == 200
        assert public_config.json()["vflow_enabled"] is False

        for path in (
            "/api/pipelines",
            "/api/pipeline-runs",
            "/api/admin/pipelines",
            "/api/admin/vflow/metrics",
        ):
            response = await client.get(path)
            assert response.status_code == 404, path


async def test_configured_values_reach_runtime_and_rate_limiter(settings, monkeypatch) -> None:
    monkeypatch.setenv("VFLOW_MAX_FORK_N", "7")
    monkeypatch.setenv("VFLOW_MAX_JSON_DEPTH", "9")
    monkeypatch.setenv("VFLOW_RUNS_PER_MINUTE", "5")
    monkeypatch.setenv("VFLOW_RUN_WINDOW_SECONDS", "12.5")

    app = create_app(settings, start_dispatcher=False)
    assert app.state.vflow_config.limits.max_fork_n == 7
    limiter = app.state.vflow_rate_limiter
    assert limiter.limit == 5
    assert limiter.window_seconds == 12.5

    async with app.router.lifespan_context(app):
        runtime = app.state.vflow_runtime
        assert runtime.limits.max_fork_n == 7
        assert runtime.limits.max_json_depth == 9
