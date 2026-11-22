from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from opencode_web.app import create_app
from opencode_web.database import Database
from opencode_web.json_processor.models import RepairSourceType
from opencode_web.json_processor.provider import (
    ProviderConnection,
    ProviderError,
    ProviderErrorKind,
)
from opencode_web.json_processor.repository import JsonProcessorRepository

SCHEMA = {
    "type": "object",
    "required": ["ok"],
    "properties": {"ok": {"type": "boolean"}},
    "additionalProperties": False,
}
SECRET = "super-secret-provider-key-value"
API = "/api/admin/json-processor"


@dataclass(frozen=True, slots=True)
class FakeCompletion:
    content: str
    model: str = "fake-model"
    usage: dict[str, int] | None = None
    status_code: int = 200
    latency_ms: int = 2
    response_format_used: bool = True
    infrastructure_retries: int = 0


class FakeProvider:
    def __init__(
        self,
        responses: list[Any] | None = None,
        *,
        connection: ProviderConnection | None = None,
        check_error: ProviderError | None = None,
    ) -> None:
        self.responses = list(responses or [])
        self.connection = connection or ProviderConnection(
            ok=True, status_code=200, model_available=True, latency_ms=7
        )
        self.check_error = check_error
        self.calls: list[dict[str, Any]] = []
        self.checks: list[dict[str, Any]] = []

    async def complete(self, messages: Any, **kwargs: Any) -> FakeCompletion:
        self.calls.append({"messages": messages, **kwargs})
        if not self.responses:
            raise AssertionError("fake provider was called unexpectedly")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, FakeCompletion):
            return item
        return FakeCompletion(
            str(item),
            usage={
                "prompt_tokens": 12,
                "completion_tokens": 4,
                "total_tokens": 16,
            },
        )

    async def check_connection(self, **kwargs: Any) -> ProviderConnection:
        self.checks.append(kwargs)
        if self.check_error is not None:
            raise self.check_error
        return self.connection

    async def aclose(self) -> None:
        return None


@pytest.fixture
async def jp_app(settings, workspace):
    app = create_app(settings, start_dispatcher=False)
    async with app.router.lifespan_context(app):
        db: Database = app.state.database
        user_dir = workspace / "json-user"
        user_dir.mkdir()
        user = db.create_user(
            "json-user", "a-strong-password", "user", str(user_dir)
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield SimpleNamespace(
                app=app, client=client, db=db, user=user
            )


async def login(client, name: str = "admin") -> dict[str, Any]:
    password = (
        "correct-horse-battery-staple"
        if name == "admin"
        else "a-strong-password"
    )
    response = await client.post(
        "/api/auth/login", json={"login": name, "password": password}
    )
    assert response.status_code == 200, response.text
    client.headers["x-csrf-token"] = response.json()["csrf_token"]
    return response.json()["user"]


def repo(app) -> JsonProcessorRepository:
    return JsonProcessorRepository(app.db)


async def patch_settings(client, version: int, changes: dict[str, Any]):
    return await client.patch(
        f"{API}/settings",
        json={"expected_version": version, "changes": changes},
    )


async def enable_local_only(client) -> dict[str, Any]:
    await login(client)
    response = await patch_settings(
        client,
        1,
        {
            "enabled": True,
            "local_repair_enabled": True,
            "max_llm_attempts": 0,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


async def enable_llm(client, *, api_key_ref: str = "") -> dict[str, Any]:
    await login(client)
    response = await patch_settings(
        client,
        1,
        {
            "enabled": True,
            "local_repair_enabled": False,
            "max_llm_attempts": 1,
            "base_url": "https://provider.example",
            "api_key_ref": api_key_ref,
            "model": "repair-model",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def make_job(
    app,
    *,
    index: int,
    raw: str | None = None,
) -> dict[str, Any]:
    raw_text = raw if raw is not None else f"raw-{index}"
    return repo(app).create_or_get_job(
        source_type=RepairSourceType.STANDALONE,
        raw_output_ref=f"memory://json-processor/{index}",
        raw_hash=hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
        schema=SCHEMA,
        prompt_version="v1",
    )


def seed_jobs(app) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    repository = repo(app)
    succeeded = make_job(app, index=1, raw='{"ok": true}')
    failed = make_job(app, index=2, raw='{"ok": false}')
    queued = make_job(app, index=3, raw="not json")
    claimed = repository.claim_due(2, lease_seconds=60, owner="test-owner")
    by_id = {item["id"]: item for item in claimed}
    repository.complete_job(
        succeeded["id"],
        lease_owner="test-owner",
        result_json={"ok": True},
        method="local",
    )
    repository.fail_job(
        failed["id"],
        lease_owner="test-owner",
        error_code="NOT_REPAIRABLE",
        error_message="raw output is not valid JSON",
        method="llm",
    )
    repository.add_attempt(
        succeeded["id"],
        method="local",
        status="succeeded",
        latency_ms=11,
        request_meta={
            "phase": "local",
            "strategy": "normalized",
            "api_key": SECRET,
            "authorization": f"Bearer {SECRET}",
            "api_key_ref": "vault://json-processor/ref",
        },
        token_usage={"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    )
    repository.add_attempt(
        failed["id"],
        method="llm",
        status="failed",
        provider="openai-compatible",
        model="repair-model",
        latency_ms=23,
        request_meta={"phase": "llm", "provider_code": "invalid_response"},
        validation_error="JSON does not satisfy the schema",
        token_usage={"total_tokens": 9},
    )
    assert by_id.keys() == {succeeded["id"], failed["id"]}
    return succeeded, failed, queued


# ---------------------------------------------------------------------------
# Authentication and settings
# ---------------------------------------------------------------------------


async def test_admin_only_reads_and_csrf_mutations(jp_app):
    anonymous = await jp_app.client.get(
        f"{API}/settings", headers={"x-request-id": "json-req-1"}
    )
    assert anonymous.status_code == 401
    assert anonymous.json()["error"]["code"] == "UNAUTHORIZED"
    assert anonymous.json()["error"]["request_id"] == "json-req-1"

    await login(jp_app.client, "json-user")
    assert (await jp_app.client.get(f"{API}/settings")).status_code == 403
    denied = await patch_settings(jp_app.client, 1, {"enabled": False})
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "FORBIDDEN"

    await login(jp_app.client)
    assert (await jp_app.client.get(f"{API}/settings")).status_code == 200
    jp_app.client.headers.pop("x-csrf-token")
    no_csrf = await patch_settings(jp_app.client, 1, {"enabled": False})
    assert no_csrf.status_code == 403
    assert no_csrf.json()["error"]["code"] == "FORBIDDEN"
    assert "CSRF" in no_csrf.json()["error"]["message"]


async def test_default_settings_are_visible_while_disabled(jp_app):
    await login(jp_app.client)
    response = await jp_app.client.get(f"{API}/settings")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["enabled"] is False
    assert payload["version"] == 1
    assert payload["provider_configured"] is False
    assert payload["api_key_ref_configured"] is False
    assert "api_key" not in payload


async def test_settings_validation_and_optimistic_version(jp_app):
    await login(jp_app.client)

    unknown = await patch_settings(
        jp_app.client, 1, {"api_key": SECRET, "enabled": "yes"}
    )
    assert unknown.status_code == 422, unknown.text
    body = unknown.json()["error"]
    assert body["code"] == "SETTINGS_INVALID"
    assert SECRET not in unknown.text

    invalid = await patch_settings(
        jp_app.client, 1, {"max_llm_attempts": 99}
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "SETTINGS_INVALID"

    first = await patch_settings(
        jp_app.client,
        1,
        {
            "enabled": True,
            "local_repair_enabled": True,
            "max_llm_attempts": 0,
            "concurrency_limit": 7,
        },
    )
    assert first.status_code == 200, first.text
    assert first.json()["version"] == 2
    assert first.json()["concurrency_limit"] == 7

    stale = await patch_settings(jp_app.client, 1, {"queue_limit": 25})
    assert stale.status_code == 409, stale.text
    conflict = stale.json()["error"]
    assert conflict["code"] == "SETTINGS_VERSION_CONFLICT"
    assert conflict["details"] == {
        "expected_version": 1,
        "current_version": 2,
    }
    assert conflict["request_id"]

    latest = (await jp_app.client.get(f"{API}/settings")).json()
    assert latest["version"] == 2
    assert latest["queue_limit"] == 1000

    audit = jp_app.db.list_audit()
    actions = [item["action"] for item in audit]
    assert "json_processor.settings.update" in actions
    assert "json_processor.settings.update_conflict" in actions
    update = next(
        item for item in audit
        if item["action"] == "json_processor.settings.update"
    )
    assert update["object_type"] == "json_processor_settings"
    assert update["object_id"] == "1"
    assert update["actor_user_id"] == jp_app.db.get_user_by_login("admin")["id"]


async def test_provider_presets_do_not_expose_secrets(jp_app):
    await login(jp_app.client)
    configured = await enable_llm(
        jp_app.client, api_key_ref="vault://json-processor/openai"
    )
    assert configured["api_key_ref_configured"] is True

    response = await jp_app.client.get(f"{API}/providers")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["protocol"] == "openai-compatible"
    assert payload["auth"]["secret_value_exposed"] is False
    assert payload["current"]["api_key_ref_configured"] is True
    assert "api_key_ref" not in payload["current"]
    assert SECRET not in response.text
    assert "Authorization" not in response.text
    assert {item["id"] for item in payload["presets"]} >= {"openai", "local"}


# ---------------------------------------------------------------------------
# Disabled mode, connection checks, and sandbox
# ---------------------------------------------------------------------------


async def test_disabled_processor_returns_explicit_errors(jp_app):
    await login(jp_app.client)
    disabled = await jp_app.client.post(
        f"{API}/test", json={"raw_output": '{"ok": true}', "schema": SCHEMA}
    )
    assert disabled.status_code == 422, disabled.text
    assert disabled.json()["error"]["code"] == "NOT_ENABLED"

    not_configured = await jp_app.client.post(
        f"{API}/check-connection",
        json={"base_url": "", "model": ""},
    )
    assert not_configured.status_code == 422
    assert not_configured.json()["error"]["code"] == "NOT_CONFIGURED"

    # Reading settings and metrics still works while the processor is disabled.
    assert (await jp_app.client.get(f"{API}/settings")).status_code == 200
    metrics = await jp_app.client.get(f"{API}/metrics")
    assert metrics.status_code == 200
    assert metrics.json()["settings"]["enabled"] is False

    actions = [item["action"] for item in jp_app.db.list_audit()]
    assert "json_processor.sandbox.test" in actions
    assert "json_processor.connection_check" in actions


async def test_local_sandbox_repairs_without_jobs_or_provider(jp_app):
    await enable_local_only(jp_app.client)
    provider = FakeProvider()
    jp_app.app.state.json_processor_provider_factory = lambda settings: provider

    response = await jp_app.client.post(
        f"{API}/test",
        json={"raw_output": '{"ok": true,}', "schema": SCHEMA},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is True
    assert payload["method"] == "local"
    assert payload["mode"] == "syntax_only"
    assert payload["value"] == {"ok": True}
    assert payload["job_created"] is False
    assert payload["attempts"][0]["strategy"] == "normalized"
    assert "raw_output" not in payload
    assert provider.calls == []

    jobs = await jp_app.client.get(f"{API}/jobs")
    assert jobs.status_code == 200
    assert jobs.json()["total"] == 0

    rejected = await jp_app.client.post(
        f"{API}/test",
        json={"raw_output": "{not json", "schema": SCHEMA},
    )
    assert rejected.status_code == 200
    rejected_payload = rejected.json()
    assert rejected_payload["ok"] is False
    assert rejected_payload["error"]["code"] in {"NOT_REPAIRABLE", "EMPTY_OUTPUT"}
    assert "analysis" not in rejected.text.lower()

    invalid_schema = await jp_app.client.post(
        f"{API}/test",
        json={"raw_output": '{"ok": true}', "schema": {"type": "unknown"}},
    )
    assert invalid_schema.status_code == 200
    assert (
        invalid_schema.json()["error"]["code"] == "SCHEMA_DEFINITION_INVALID"
    )


async def test_llm_sandbox_and_connection_check_use_mock_provider(jp_app):
    await enable_llm(
        jp_app.client, api_key_ref="vault://json-processor/openai"
    )
    provider = FakeProvider(['{"ok": true}'])
    jp_app.app.state.json_processor_provider_factory = lambda settings: provider
    jp_app.app.state.json_processor_api_key_resolver = (
        lambda ref: SECRET if ref == "vault://json-processor/openai" else None
    )

    response = await jp_app.client.post(
        f"{API}/test",
        json={"raw_output": "model prose before json", "schema": SCHEMA},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is True
    assert payload["method"] == "llm"
    assert payload["value"] == {"ok": True}
    assert len(provider.calls) == 1
    assert payload["attempts"][0]["phase"] == "llm"

    check = await jp_app.client.post(f"{API}/check-connection", json={})
    assert check.status_code == 200, check.text
    checked = check.json()
    assert checked["ok"] is True
    assert checked["model_available"] is True
    assert checked["settings_version"] == 2
    assert SECRET not in check.text
    assert provider.checks
    assert provider.checks[0]["api_key"] == SECRET

    no_body = await jp_app.client.post(f"{API}/check-connection")
    assert no_body.status_code == 200, no_body.text
    assert no_body.json()["ok"] is True


async def test_connection_check_reports_provider_failure_safely(jp_app):
    await login(jp_app.client)
    configured = await patch_settings(
        jp_app.client,
        1,
        {
            "base_url": "https://provider.example",
            "model": "repair-model",
            "api_key_ref": "vault://json-processor/openai",
        },
    )
    assert configured.status_code == 200, configured.text
    provider = FakeProvider(
        check_error=ProviderError(
            ProviderErrorKind.AUTHENTICATION,
            "provider authentication failed",
            status_code=401,
        )
    )
    jp_app.app.state.json_processor_provider_factory = lambda settings: provider

    response = await jp_app.client.post(f"{API}/check-connection", json={})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is False
    assert payload["error"]["status_code"] == 401
    assert payload["error"]["message"] == "provider authentication failed"
    assert SECRET not in response.text


# ---------------------------------------------------------------------------
# Jobs and metrics
# ---------------------------------------------------------------------------


async def test_jobs_filters_pagination_and_detail(jp_app):
    await login(jp_app.client)
    succeeded, failed, queued = seed_jobs(jp_app)

    listing = await jp_app.client.get(f"{API}/jobs")
    assert listing.status_code == 200, listing.text
    page = listing.json()
    assert page["total"] == 3
    assert {item["id"] for item in page["items"]} == {
        succeeded["id"], failed["id"], queued["id"]
    }
    for item in page["items"]:
        assert "result" not in item
        assert "schema" not in item
        assert "raw_output" not in item

    only_failed = await jp_app.client.get(
        f"{API}/jobs", params={"status": "failed"}
    )
    assert only_failed.status_code == 200
    assert only_failed.json()["total"] == 1
    assert only_failed.json()["items"][0]["error_code"] == "NOT_REPAIRABLE"

    only_local = await jp_app.client.get(
        f"{API}/jobs", params={"method": "local"}
    )
    assert only_local.json()["total"] == 1

    only_source = await jp_app.client.get(
        f"{API}/jobs", params={"source_type": "standalone"}
    )
    assert only_source.json()["total"] == 3

    first_page = await jp_app.client.get(
        f"{API}/jobs", params={"limit": 1, "offset": 0}
    )
    second_page = await jp_app.client.get(
        f"{API}/jobs", params={"limit": 1, "offset": 1}
    )
    assert first_page.json()["total"] == 3
    assert first_page.json()["has_more"] is True
    assert len(first_page.json()["items"]) == 1
    assert len(second_page.json()["items"]) == 1
    assert (
        first_page.json()["items"][0]["id"]
        != second_page.json()["items"][0]["id"]
    )

    bad_status = await jp_app.client.get(
        f"{API}/jobs", params={"status": "bogus"}
    )
    assert bad_status.status_code == 422
    assert bad_status.json()["error"]["code"] == "INVALID_INPUT"

    detail = await jp_app.client.get(f"{API}/jobs/{succeeded['id']}")
    assert detail.status_code == 200, detail.text
    detail_payload = detail.json()
    assert detail_payload["status"] == "succeeded"
    assert detail_payload["result"] == {"ok": True}
    assert detail_payload["schema"] == SCHEMA
    assert len(detail_payload["attempts_history"]) == 1
    attempt = detail_payload["attempts_history"][0]
    assert attempt["method"] == "local"
    assert attempt["request_meta"]["strategy"] == "normalized"
    assert "api_key" not in attempt["request_meta"]
    assert "authorization" not in attempt["request_meta"]
    assert SECRET not in detail.text
    assert "Bearer" not in detail.text
    assert '"raw_output":' not in detail.text

    missing = await jp_app.client.get(f"{API}/jobs/does-not-exist")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "JOB_NOT_FOUND"


async def test_metrics_aggregate_jobs_attempts_and_retention(jp_app):
    await login(jp_app.client)
    seed_jobs(jp_app)

    response = await jp_app.client.get(f"{API}/metrics")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["settings"]["enabled"] is False
    assert payload["jobs"]["total"] == 3
    assert payload["jobs"]["by_status"]["succeeded"] == 1
    assert payload["jobs"]["by_status"]["failed"] == 1
    assert payload["jobs"]["by_status"]["queued"] == 1
    assert payload["jobs"]["by_method"]["local"] == 1
    assert payload["jobs"]["by_method"]["llm"] == 1
    assert payload["jobs"]["success_rate"] == 0.5
    assert payload["attempts"]["total"] == 2
    assert payload["attempts"]["by_method"]["local"] == 1
    assert payload["attempts"]["by_method"]["llm"] == 1
    assert payload["attempts"]["tokens"]["total_tokens"] == 16
    assert payload["queue"]["queued"] == 1
    assert payload["retention"]["raw_output_cutoff"]
    assert "api_key_ref" not in payload["settings"]
    assert SECRET not in response.text
    assert "Authorization" not in response.text


# ---------------------------------------------------------------------------
# M9 security, audit, metrics, and retention
# ---------------------------------------------------------------------------


async def test_all_admin_endpoints_enforce_rbac_and_csrf(jp_app):
    get_endpoints = (
        f"{API}/settings",
        f"{API}/providers",
        f"{API}/jobs",
        f"{API}/jobs/missing",
        f"{API}/metrics",
        f"{API}/retention",
    )
    mutation_requests = (
        ("PATCH", f"{API}/settings", {"expected_version": 1, "changes": {}}),
        ("POST", f"{API}/test", {"raw_output": '{"ok": true}'}),
        ("POST", f"{API}/check-connection", {}),
        ("POST", f"{API}/retention/purge", {"confirm": True}),
    )

    for path in get_endpoints:
        response = await jp_app.client.get(path)
        assert response.status_code == 401, (path, response.text)
    for method, path, body in mutation_requests:
        response = await jp_app.client.request(method, path, json=body)
        assert response.status_code == 401, (path, response.text)

    await login(jp_app.client, "json-user")
    for path in get_endpoints:
        response = await jp_app.client.get(path)
        assert response.status_code == 403, (path, response.text)
        assert response.json()["error"]["code"] == "FORBIDDEN"
    for method, path, body in mutation_requests:
        response = await jp_app.client.request(method, path, json=body)
        assert response.status_code == 403, (path, response.text)
        assert response.json()["error"]["code"] == "FORBIDDEN"

    await login(jp_app.client)
    for path in get_endpoints[:-1]:
        response = await jp_app.client.get(path)
        assert response.status_code in {200, 404}, (path, response.text)
    jp_app.client.headers.pop("x-csrf-token")
    for method, path, body in mutation_requests:
        response = await jp_app.client.request(method, path, json=body)
        assert response.status_code == 403, (path, response.text)
        assert "CSRF" in response.json()["error"]["message"]


async def test_settings_and_connection_checks_reject_ssrf_targets(jp_app):
    await login(jp_app.client)
    base_changes = {
        "enabled": True,
        "local_repair_enabled": True,
        "max_llm_attempts": 1,
        "model": "repair-model",
    }
    for base_url in (
        "http://169.254.169.254/latest/meta-data",
        "http://metadata.google.internal",
        "http://127.0.0.1:1234",
        "http://[fe80::1]",
    ):
        response = await patch_settings(
            jp_app.client, 1, {**base_changes, "base_url": base_url}
        )
        assert response.status_code == 422, response.text
        error = response.json()["error"]
        assert error["code"] == "SETTINGS_INVALID"
        assert base_url not in response.text

    settings = (await jp_app.client.get(f"{API}/settings")).json()
    assert settings["version"] == 1
    assert settings["base_url"] == ""

    valid = await patch_settings(
        jp_app.client,
        1,
        {**base_changes, "base_url": "https://provider.example"},
    )
    assert valid.status_code == 200, valid.text

    blocked_check = await jp_app.client.post(
        f"{API}/check-connection",
        json={"base_url": "http://169.254.169.254", "model": "repair-model"},
    )
    assert blocked_check.status_code == 422, blocked_check.text
    assert blocked_check.json()["error"]["code"] == "SETTINGS_INVALID"
    assert "169.254.169.254" not in blocked_check.text

    audit = jp_app.db.list_audit()
    rejected = [
        item for item in audit
        if item["action"] == "json_processor.settings.rejected"
    ]
    assert rejected
    assert all("169.254.169.254" not in item["metadata_json"] for item in rejected)


async def test_sandbox_and_connection_tests_are_audited_without_secrets(jp_app):
    await enable_local_only(jp_app.client)
    provider = FakeProvider(['{"ok": true}'])
    jp_app.app.state.json_processor_provider_factory = lambda settings: provider

    sandbox = await jp_app.client.post(
        f"{API}/test",
        json={
            "raw_output": f"{SECRET} is not json",
            "schema": SCHEMA,
        },
    )
    assert sandbox.status_code == 200, sandbox.text
    assert SECRET not in sandbox.text

    configured = await patch_settings(
        jp_app.client,
        2,
        {
            "enabled": True,
            "local_repair_enabled": False,
            "max_llm_attempts": 1,
            "base_url": "https://provider.example",
            "api_key_ref": "vault://json-processor/openai",
            "model": "repair-model",
        },
    )
    assert configured.status_code == 200, configured.text
    jp_app.app.state.json_processor_api_key_resolver = (
        lambda ref: SECRET if ref == "vault://json-processor/openai" else None
    )

    provider.responses = [
        ProviderError(
            ProviderErrorKind.PROVIDER_ERROR,
            f"provider leaked {SECRET}",
            status_code=500,
        )
    ]
    leaked = await jp_app.client.post(
        f"{API}/test",
        json={"raw_output": "{not json", "schema": SCHEMA},
    )
    assert leaked.status_code == 200, leaked.text
    assert SECRET not in leaked.text
    assert "[redacted]" in leaked.text

    check = await jp_app.client.post(f"{API}/check-connection", json={})
    assert check.status_code == 200, check.text
    assert check.json()["ok"] is True
    assert SECRET not in check.text

    audit = jp_app.db.list_audit()
    actions = [item["action"] for item in audit]
    assert "json_processor.sandbox.test" in actions
    assert "json_processor.connection_check" in actions
    serialized = json.dumps(audit, ensure_ascii=False)
    assert SECRET not in serialized
    assert "provider leaked" not in serialized


async def test_metrics_include_failure_categories_percentiles_and_last_24h(jp_app):
    await login(jp_app.client)
    repository = repo(jp_app)
    failure_specs = (
        (1, "NOT_REPAIRABLE", 10),
        (2, "SCHEMA_VALIDATION_FAILED", 20),
        (3, "CONTENT_CHANGED", 30),
        (4, "PROVIDER_ERROR", 40),
    )
    for index, error_code, latency in failure_specs:
        job = make_job(jp_app, index=index, raw=f"failed-{index}")
        repository.claim_due(1, lease_seconds=60, owner="metrics-worker")
        repository.fail_job(
            job["id"],
            lease_owner="metrics-worker",
            error_code=error_code,
            error_message=f"safe failure {error_code}",
            method="llm" if error_code == "PROVIDER_ERROR" else "local",
        )
        repository.add_attempt(
            job["id"],
            method="llm" if error_code == "PROVIDER_ERROR" else "local",
            status="failed",
            latency_ms=latency,
        )

    response = await jp_app.client.get(f"{API}/metrics")
    assert response.status_code == 200, response.text
    metrics = response.json()
    assert metrics["jobs"]["total"] == 4
    assert metrics["failures"]["parse"] == 1
    assert metrics["failures"]["schema"] == 1
    assert metrics["failures"]["content"] == 1
    assert metrics["failures"]["provider"] == 1
    assert metrics["failures"]["provider_errors"] == 1
    assert metrics["attempts"]["latency"]["samples"] == 4
    assert metrics["attempts"]["latency"]["p50_ms"] == 20
    assert metrics["attempts"]["latency"]["p95_ms"] == 40
    assert metrics["attempts"]["p50_latency_ms"] == 20
    assert metrics["attempts"]["p95_latency_ms"] == 40
    assert metrics["last_24h"]["failed"] == 4
    assert metrics["last_24h"]["provider_errors"] == 1
    assert metrics["last_24h"]["attempts"] == 4
    assert metrics["retention"]["raw_output_retention_days"] == 30
    assert metrics["retention"]["attempt_retention_days"] == 90
    assert SECRET not in response.text


async def test_retention_preview_and_confirmed_purge_endpoint(jp_app):
    await login(jp_app.client)
    repository = repo(jp_app)
    job = make_job(jp_app, index=1, raw="retention")
    repository.claim_due(1, lease_seconds=60, owner="retention-worker")
    repository.complete_job(
        job["id"],
        lease_owner="retention-worker",
        result_json={"ok": True},
        method="local",
    )
    attempt = repository.add_attempt(job["id"], method="local")
    now = datetime.now(UTC)
    with jp_app.db.transaction() as connection:
        connection.execute(
            "UPDATE json_repair_jobs SET finished_at = ? WHERE id = ?",
            ((now - timedelta(days=40)).isoformat(), job["id"]),
        )
        connection.execute(
            "UPDATE json_repair_attempts SET created_at = ? WHERE id = ?",
            ((now - timedelta(days=100)).isoformat(), attempt["id"]),
        )

    preview = await jp_app.client.get(f"{API}/retention")
    assert preview.status_code == 200, preview.text
    assert preview.json()["dry_run"] is True
    assert preview.json()["jobs_with_expired_raw_output"] == 1
    assert preview.json()["expired_attempts"] == 1

    denied = await jp_app.client.post(
        f"{API}/retention/purge", json={"confirm": False}
    )
    assert denied.status_code == 422, denied.text
    assert denied.json()["error"]["code"] == "INVALID_INPUT"
    assert repository.get_job(job["id"])["result"] == {"ok": True}
    assert len(repository.list_attempts(job["id"])) == 1

    purged = await jp_app.client.post(
        f"{API}/retention/purge", json={"confirm": True, "batch_size": 10}
    )
    assert purged.status_code == 200, purged.text
    payload = purged.json()
    assert payload["ok"] is True
    assert payload["dry_run"] is False
    assert payload["jobs_output_purged"] == 1
    assert payload["attempts_deleted"] == 1
    assert payload["has_more"] is False

    stored = repository.get_job(job["id"])
    assert stored is not None
    assert stored["status"] == "succeeded"
    assert stored["result"] is None
    assert repository.list_attempts(job["id"]) == []
    after = await jp_app.client.get(f"{API}/retention")
    assert after.json()["jobs_with_expired_raw_output"] == 0
    assert after.json()["expired_attempts"] == 0

    audit = jp_app.db.list_audit()
    purge_rows = [
        item for item in audit
        if item["action"] == "json_processor.retention.purge"
    ]
    assert {json.loads(item["metadata_json"])["outcome"] for item in purge_rows} == {
        "confirmation_required",
        "purged",
    }


async def test_private_base_url_requires_explicit_operator_opt_in(settings, workspace):
    from dataclasses import replace

    from opencode_web.app import create_app

    opted_in = replace(settings, json_processor_allow_private_networks=True)
    app = create_app(opted_in, start_dispatcher=False)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await login(client)
        configured = await patch_settings(
            client,
            1,
            {
                "enabled": True,
                "local_repair_enabled": True,
                "max_llm_attempts": 1,
                "base_url": "http://127.0.0.1:1234",
                "model": "local-model",
            },
        )
        assert configured.status_code == 200, configured.text
        assert configured.json()["base_url"] == "http://127.0.0.1:1234"
        assert app.state.json_processor_allow_private_networks is True

        metadata = await patch_settings(
            client,
            2,
            {"base_url": "http://169.254.169.254"},
        )
        assert metadata.status_code == 422, metadata.text
