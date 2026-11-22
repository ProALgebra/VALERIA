from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from opencode_web.database import Database
from opencode_web.json_processor import (
    JsonProcessorSettings,
    ProcessorErrorCode,
    RepairJobStatus,
    RepairMethod,
    RepairSourceType,
    SchemaDefinitionError,
    SettingsValidationError,
)
from opencode_web.json_processor.repository import (
    JsonProcessorRepository,
    JsonRepairIdempotencyConflict,
    JsonRepairJobNotFound,
    JsonRepairLeaseLost,
    JsonRepairStateConflict,
    SettingsVersionConflict,
    sanitize_metadata,
)

SCHEMA = {
    "type": "object",
    "required": ["ok"],
    "properties": {"ok": {"type": "boolean"}},
    "additionalProperties": False,
}


@pytest.fixture
def database(tmp_path: Path):
    db = Database(tmp_path / "json-processor.sqlite3", "s" * 32)
    db.initialize()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def repository(database: Database) -> JsonProcessorRepository:
    return JsonProcessorRepository(database)


@pytest.fixture
def admin(database: Database) -> dict:
    return database.create_user(
        "processor-admin", "correct-horse-battery", "admin", None
    )


def make_job(
    repository: JsonProcessorRepository,
    *,
    index: int = 1,
    task_run_id: str = "task-run-1",
    source_type: RepairSourceType | str = RepairSourceType.TASK,
    vflow_run_id: str | None = None,
    point_id: str | None = None,
    raw: str | None = None,
    schema: dict | None = None,
    prompt_version: str = "v1",
    **kwargs,
) -> dict:
    raw_text = raw if raw is not None else f"raw-{index}"
    return repository.create_or_get_job(
        source_type=source_type,
        task_run_id=task_run_id,
        vflow_run_id=vflow_run_id,
        point_id=point_id,
        raw_output_ref=f"artifact://raw/{index}",
        raw_hash=hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
        schema=schema or SCHEMA,
        prompt_version=prompt_version,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_settings_defaults_are_singleton_and_persisted(
    database: Database, repository: JsonProcessorRepository
) -> None:
    settings = repository.get_settings()

    assert settings["id"] == 1
    assert settings["version"] == 1
    assert settings["enabled"] is False
    assert settings["base_url"] == ""
    assert settings["api_key_ref"] == ""
    assert settings["model"] == ""
    assert settings["local_repair_enabled"] is True
    assert settings["max_llm_attempts"] == 2
    assert settings["request_timeout_seconds"] == 45
    assert settings["max_input_bytes"] == 65_536
    assert settings["max_output_bytes"] == 65_536
    assert settings["max_output_tokens"] == 4_096
    assert settings["temperature"] == 0.0
    assert settings["repair_mode"] == "syntax_only"
    assert settings["on_processor_failure"] == "fail"
    assert settings["concurrency_limit"] == 4
    assert settings["queue_limit"] == 1_000
    assert settings["raw_output_retention_days"] == 30
    assert settings["attempt_retention_days"] == 90
    assert database._one(
        "SELECT COUNT(*) AS count FROM json_processor_settings"
    ) == {"count": 1}
    model = repository.get_settings_model()
    assert isinstance(model, JsonProcessorSettings)
    assert model == JsonProcessorSettings()


def test_settings_update_is_validated_and_cas_versioned(
    repository: JsonProcessorRepository, admin: dict
) -> None:
    updated = repository.update_settings(
        {
            "enabled": True,
            "base_url": "http://127.0.0.1:8000/",
            "api_key_ref": "vault:json-processor",
            "model": "repair-model",
            "temperature": 0.2,
            "max_llm_attempts": 3,
        },
        expected_version=1,
        updated_by=admin["id"],
    )

    assert updated["version"] == 2
    assert updated["enabled"] is True
    assert updated["base_url"] == "http://127.0.0.1:8000"
    assert updated["api_key_ref"] == "vault:json-processor"
    assert updated["temperature"] == 0.2
    assert updated["updated_by"] == admin["id"]

    with pytest.raises(SettingsVersionConflict) as conflict:
        repository.update_settings(
            {"concurrency_limit": 8}, expected_version=1, updated_by=admin["id"]
        )
    assert conflict.value.code is ProcessorErrorCode.SETTINGS_VERSION_CONFLICT
    assert repository.get_settings()["concurrency_limit"] == 4
    assert repository.get_settings()["version"] == 2

    no_change = repository.update_settings({}, expected_version=2)
    assert no_change["version"] == 2

    renamed = repository.update_settings(
        {"model": "repair-model-v2"}, expected_version=2
    )
    assert renamed["version"] == 3
    assert renamed["model"] == "repair-model-v2"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"enabled": "yes"}, "enabled must be a boolean"),
        ({"local_repair_enabled": 1}, "local_repair_enabled must be a boolean"),
        ({"max_llm_attempts": 4}, "max_llm_attempts must be between 0 and 3"),
        ({"max_llm_attempts": -1}, "max_llm_attempts must be between 0 and 3"),
        ({"request_timeout_seconds": 0}, "request_timeout_seconds must be between 1 and 300"),
        ({"max_input_bytes": 0}, "max_input_bytes must be between 1 and"),
        ({"max_output_bytes": 0}, "max_output_bytes must be between 1 and"),
        ({"max_output_tokens": 0}, "max_output_tokens must be between 1 and"),
        ({"temperature": 2.5}, "temperature must be between 0 and 2"),
        ({"temperature": "hot"}, "temperature must be a number"),
        ({"repair_mode": "semantic"}, "repair_mode must be one of"),
        ({"on_processor_failure": "ignore"}, "on_processor_failure must be one of"),
        ({"concurrency_limit": 0}, "concurrency_limit must be between 1 and 32"),
        ({"queue_limit": 0}, "queue_limit must be between 1 and"),
        ({"raw_output_retention_days": 0}, "raw_output_retention_days must be between 1 and"),
        ({"attempt_retention_days": 0}, "attempt_retention_days must be between 1 and"),
        ({"base_url": "ftp://example.test"}, "base_url must be an absolute http(s) URL"),
        ({"base_url": "http://user:pass@example.test"}, "base_url must not contain credentials"),
        ({"enabled": True}, "base_url is required when LLM repair is enabled"),
        ({"enabled": True, "base_url": "http://example.test"}, "model is required"),
        (
            {
                "enabled": True,
                "base_url": "http://example.test",
                "model": "m",
                "local_repair_enabled": False,
                "max_llm_attempts": 0,
            },
            "an enabled processor needs local repair",
        ),
    ],
)
def test_settings_validation_rejects_bad_values(
    repository: JsonProcessorRepository, changes: dict, message: str
) -> None:
    with pytest.raises(SettingsValidationError, match=re.escape(message)):
        repository.update_settings(changes, expected_version=1)
    assert repository.get_settings()["version"] == 1


def test_settings_allow_local_only_processor_without_provider(
    repository: JsonProcessorRepository,
) -> None:
    updated = repository.update_settings(
        {
            "enabled": True,
            "local_repair_enabled": True,
            "max_llm_attempts": 0,
        },
        expected_version=1,
    )

    assert updated["enabled"] is True
    assert updated["local_repair_enabled"] is True
    assert updated["max_llm_attempts"] == 0
    assert updated["base_url"] == ""
    assert updated["model"] == ""


def test_settings_reject_secret_fields_without_echoing_values(
    database: Database, repository: JsonProcessorRepository
) -> None:
    secret = "sk-live-must-never-be-stored"
    with pytest.raises(SettingsValidationError) as error:
        repository.update_settings(
            {"api_key": secret, "authorization": f"Bearer {secret}"},
            expected_version=1,
        )
    assert "api_key" in str(error.value)
    assert secret not in str(error.value)
    raw = database._one(
        "SELECT settings_json FROM json_processor_settings WHERE id = 1"
    )
    assert raw is not None
    assert secret not in raw["settings_json"]


def test_settings_read_ignores_unknown_keys_so_secrets_cannot_leak(
    database: Database, repository: JsonProcessorRepository
) -> None:
    tampered = {
        "enabled": False,
        "api_key": "sk-secret",
        "nested_secret": {"value": "hidden"},
    }
    database._connection.execute(
        "UPDATE json_processor_settings SET settings_json = ? WHERE id = 1",
        (json.dumps(tampered),),
    )
    settings = repository.get_settings()

    serialized = json.dumps(settings)
    assert "api_key" not in settings
    assert "nested_secret" not in settings
    assert "sk-secret" not in serialized
    assert settings["enabled"] is False


def test_settings_reject_unknown_updated_by(
    repository: JsonProcessorRepository,
) -> None:
    with pytest.raises(ValueError, match="updated_by user does not exist"):
        repository.update_settings(
            {"concurrency_limit": 5}, expected_version=1, updated_by="missing-user"
        )


# ---------------------------------------------------------------------------
# Schema / migration
# ---------------------------------------------------------------------------


def test_tables_and_indexes_exist_on_fresh_database(database: Database) -> None:
    tables = {
        row["name"]
        for row in database._all(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('json_processor_settings', 'json_repair_jobs', "
            "'json_repair_attempts')"
        )
    }
    assert tables == {
        "json_processor_settings",
        "json_repair_jobs",
        "json_repair_attempts",
    }
    indexes = {
        row["name"]
        for row in database._all(
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND name LIKE 'idx_json_repair_%'"
        )
    }
    assert {
        "idx_json_repair_jobs_queue",
        "idx_json_repair_jobs_lease",
        "idx_json_repair_jobs_status",
        "idx_json_repair_jobs_task",
        "idx_json_repair_jobs_vflow",
        "idx_json_repair_jobs_canonical",
        "idx_json_repair_attempts_job",
        "idx_json_repair_attempts_created",
    }.issubset(indexes)
    assert database._one("PRAGMA foreign_keys")["foreign_keys"] == 1


def test_initialize_migrates_existing_database_without_losing_data(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "existing.sqlite3", "s" * 32)
    try:
        db.initialize()
        user = db.create_user("legacy", "correct-horse-battery", "user", "/tmp/ws")
        db._connection.execute(
            "DROP TABLE json_repair_attempts"
        )
        db._connection.execute("DROP TABLE json_repair_jobs")
        db._connection.execute("DROP TABLE json_processor_settings")

        db.initialize()

        tables = {
            row["name"]
            for row in db._all("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {
            "json_processor_settings",
            "json_repair_jobs",
            "json_repair_attempts",
            "vflow_runs",
            "vflow_points",
            "consultation_artifacts",
            "consultation_attachments",
        }.issubset(tables)
        assert db.get_user(user["id"]) is not None
        assert db._one(
            "SELECT version FROM json_processor_settings WHERE id = 1"
        ) == {"version": 1}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Job creation and idempotency
# ---------------------------------------------------------------------------


def test_create_or_get_job_is_idempotent_by_canonical_identity(
    repository: JsonProcessorRepository,
) -> None:
    first = make_job(repository, raw="same")
    second = make_job(repository, raw="same")

    assert second["id"] == first["id"]
    assert second["idempotency_key"] == first["idempotency_key"]
    assert repository.list_jobs()["total"] == 1
    assert first["status"] == RepairJobStatus.QUEUED.value
    assert first["method"] == RepairMethod.NONE.value
    assert first["attempts"] == 0
    assert first["settings_version"] == 1
    assert first["schema"] == SCHEMA
    canonical_schema = json.dumps(
        SCHEMA, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    assert first["schema_hash"] == hashlib.sha256(
        canonical_schema.encode("utf-8")
    ).hexdigest()


def test_different_raw_or_schema_creates_a_new_job(
    repository: JsonProcessorRepository,
) -> None:
    first = make_job(repository, raw="one")
    other_raw = make_job(repository, raw="two")
    other_schema = make_job(
        repository,
        raw="one",
        schema={
            "type": "object",
            "properties": {"ok": {"type": "string"}},
        },
    )
    other_prompt = make_job(repository, raw="one", prompt_version="v2")
    other_run = make_job(repository, raw="one", task_run_id="task-run-2")

    assert len(
        {
            first["id"],
            other_raw["id"],
            other_schema["id"],
            other_prompt["id"],
            other_run["id"],
        }
    ) == 5
    assert first["id"] != other_raw["id"]
    assert first["id"] != other_schema["id"]
    assert first["id"] != other_prompt["id"]
    assert first["id"] != other_run["id"]


def test_explicit_idempotency_key_cannot_be_reused_for_another_request(
    repository: JsonProcessorRepository,
) -> None:
    first = make_job(repository, raw="one", idempotency_key="request-1")
    same = make_job(repository, raw="one", idempotency_key="request-1")
    canonical_wins = make_job(repository, raw="one", idempotency_key="request-2")

    assert same["id"] == first["id"]
    assert canonical_wins["id"] == first["id"]
    assert first["idempotency_key"] == "request-1"

    with pytest.raises(JsonRepairIdempotencyConflict) as conflict:
        make_job(repository, raw="two", idempotency_key="request-1")
    assert conflict.value.code is ProcessorErrorCode.IDEMPOTENCY_CONFLICT
    assert make_job(repository, raw="two")["id"] != first["id"]


def test_settings_version_is_part_of_idempotency_key(
    repository: JsonProcessorRepository,
) -> None:
    first = make_job(repository, raw="same")
    repository.update_settings({"temperature": 0.1}, expected_version=1)
    after_update = make_job(repository, raw="same")
    explicit_old = make_job(repository, raw="same", settings_version=1)

    assert first["settings_version"] == 1
    assert after_update["settings_version"] == 2
    assert after_update["id"] != first["id"]
    assert explicit_old["id"] == first["id"]


def test_job_source_validation_and_schema_validation(
    repository: JsonProcessorRepository,
) -> None:
    with pytest.raises(ValueError, match="task_run_id is required"):
        repository.create_or_get_job(
            source_type="task",
            raw_output_ref="ref",
            raw_hash="a" * 64,
            schema=SCHEMA,
            prompt_version="v1",
        )
    with pytest.raises(ValueError, match="vflow_run_id is required"):
        repository.create_or_get_job(
            source_type="vflow",
            raw_output_ref="ref",
            raw_hash="a" * 64,
            schema=SCHEMA,
            prompt_version="v1",
        )
    with pytest.raises(ValueError, match="raw_hash"):
        repository.create_or_get_job(
            source_type="standalone",
            raw_output_ref="ref",
            raw_hash="not-a-hash",
            schema=SCHEMA,
            prompt_version="v1",
        )
    with pytest.raises(ValueError, match="prompt_version"):
        repository.create_or_get_job(
            source_type="standalone",
            raw_output_ref="ref",
            raw_hash="a" * 64,
            schema=SCHEMA,
            prompt_version="bad version!",
        )
    with pytest.raises(SchemaDefinitionError):
        repository.create_or_get_job(
            source_type="standalone",
            raw_output_ref="ref",
            raw_hash="a" * 64,
            schema={"type": "object", "properties": {"x": {"$ref": "#/x"}}},
            prompt_version="v1",
        )

    standalone = repository.create_or_get_job(
        source_type="standalone",
        raw_output_ref="ref",
        raw_hash="a" * 64,
        schema=SCHEMA,
        prompt_version="v1",
    )
    assert standalone["source_type"] == "standalone"
    assert standalone["task_run_id"] is None


# ---------------------------------------------------------------------------
# Lease claim, completion and terminal guards
# ---------------------------------------------------------------------------


def test_claim_due_uses_lease_and_increments_attempts(
    repository: JsonProcessorRepository,
) -> None:
    jobs = [make_job(repository, index=index, raw=f"raw-{index}") for index in range(3)]
    all_ids = {job["id"] for job in jobs}

    claimed = repository.claim_due(2, lease_seconds=60, owner="worker-a")

    assert len(claimed) == 2
    assert {job["id"] for job in claimed}.issubset(all_ids)
    for job in claimed:
        assert job["status"] == RepairJobStatus.PROCESSING.value
        assert job["attempts"] == 1
        assert job["lease_owner"] == "worker-a"
        assert job["started_at"] is not None
        assert job["lease_until"] is not None
        assert job["lease_until"] > job["created_at"]

    remaining = repository.claim_due(10, owner="worker-b")
    assert len(remaining) == 1
    assert {job["id"] for job in remaining}.isdisjoint(
        {job["id"] for job in claimed}
    )
    assert {job["id"] for job in claimed} | {
        job["id"] for job in remaining
    } == all_ids
    assert remaining[0]["lease_owner"] == "worker-b"
    assert repository.claim_due(10, owner="worker-c") == []


def test_complete_job_requires_lease_and_is_terminal(
    repository: JsonProcessorRepository,
) -> None:
    job = make_job(repository)
    claimed = repository.claim_due(1, lease_seconds=60, owner="worker-a")[0]

    with pytest.raises(JsonRepairLeaseLost) as lost:
        repository.complete_job(
            job["id"],
            lease_owner="worker-b",
            result_json={"ok": True},
            method=RepairMethod.LOCAL,
        )
    assert lost.value.code is ProcessorErrorCode.LEASE_LOST
    assert repository.get_job(job["id"])["status"] == "processing"

    completed = repository.complete_job(
        claimed["id"],
        lease_owner="worker-a",
        result_json={"ok": True},
        method=RepairMethod.LOCAL,
    )
    assert completed["status"] == "succeeded"
    assert completed["method"] == "local"
    assert completed["result"] == {"ok": True}
    assert completed["has_result"] is True
    assert completed["lease_owner"] is None
    assert completed["lease_until"] is None
    assert completed["finished_at"] is not None
    assert completed["error_code"] is None
    assert completed["error_message"] is None

    with pytest.raises(JsonRepairStateConflict):
        repository.complete_job(
            job["id"], lease_owner="worker-a", result_json={"ok": True}
        )
    with pytest.raises(JsonRepairStateConflict):
        repository.fail_job(
            job["id"],
            lease_owner="worker-a",
            error_code=ProcessorErrorCode.TIMEOUT,
        )
    with pytest.raises(JsonRepairStateConflict):
        repository.cancel_job(job["id"], force=True)


def test_fail_job_records_error_and_is_terminal(
    repository: JsonProcessorRepository,
) -> None:
    job = make_job(repository)
    repository.claim_due(1, lease_seconds=60, owner="worker-a")

    failed = repository.fail_job(
        job["id"],
        lease_owner="worker-a",
        error_code=ProcessorErrorCode.TIMEOUT,
        error_message="provider timed out",
        method=RepairMethod.LLM,
    )

    assert failed["status"] == "failed"
    assert failed["error_code"] == "TIMEOUT"
    assert failed["error_message"] == "provider timed out"
    assert failed["method"] == "llm"
    assert failed["result_json"] is None
    assert failed["has_result"] is False
    assert failed["finished_at"] is not None
    assert failed["lease_owner"] is None

    with pytest.raises(JsonRepairStateConflict):
        repository.fail_job(
            job["id"],
            lease_owner="worker-a",
            error_code=ProcessorErrorCode.PROVIDER_ERROR,
        )
    with pytest.raises(ValueError, match="error_code"):
        repository.fail_job(
            job["id"],
            lease_owner="worker-a",
            error_code="NOT_A_REAL_CODE",  # type: ignore[arg-type]
        )


def test_cancel_queued_and_leased_jobs_with_terminal_guard(
    repository: JsonProcessorRepository,
) -> None:
    queued = make_job(repository, raw="queued")
    cancelled = repository.cancel_job(queued["id"], reason="operator request")
    assert cancelled["status"] == "cancelled"
    assert cancelled["error_code"] == "CANCELLED"
    assert cancelled["error_message"] == "operator request"
    assert cancelled["finished_at"] is not None
    with pytest.raises(JsonRepairStateConflict):
        repository.cancel_job(queued["id"])

    leased = make_job(repository, raw="leased")
    repository.claim_due(1, lease_seconds=60, owner="worker-a")
    with pytest.raises(JsonRepairLeaseLost):
        repository.cancel_job(leased["id"])
    with pytest.raises(JsonRepairLeaseLost):
        repository.cancel_job(leased["id"], lease_owner="worker-b")
    forced = repository.cancel_job(leased["id"], force=True)
    assert forced["status"] == "cancelled"
    assert forced["lease_owner"] is None


def test_expired_lease_is_recovered_and_can_be_claimed_again(
    repository: JsonProcessorRepository,
) -> None:
    job = make_job(repository)
    repository.claim_due(1, lease_seconds=60, owner="worker-a")
    future = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()

    assert repository.recover_expired_leases(now=future) == 1
    recovered = repository.get_job(job["id"])
    assert recovered is not None
    assert recovered["status"] == "queued"
    assert recovered["lease_owner"] is None
    assert recovered["lease_until"] is None
    assert recovered["attempts"] == 1
    assert recovered["finished_at"] is None
    assert repository.recover_expired_leases(now=future) == 0

    reclaimed = repository.claim_due(1, lease_seconds=60, owner="worker-b")[0]
    assert reclaimed["id"] == job["id"]
    assert reclaimed["attempts"] == 2
    assert reclaimed["lease_owner"] == "worker-b"


def test_expired_lease_cannot_complete(
    database: Database, repository: JsonProcessorRepository
) -> None:
    job = make_job(repository)
    repository.claim_due(1, lease_seconds=60, owner="worker-a")
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    database._connection.execute(
        "UPDATE json_repair_jobs SET lease_until = ? WHERE id = ?",
        (past, job["id"]),
    )

    with pytest.raises(JsonRepairLeaseLost):
        repository.complete_job(
            job["id"], lease_owner="worker-a", result_json={"ok": True}
        )
    assert repository.recover_expired_leases() == 1


def test_processing_job_without_lease_is_recovered(
    database: Database, repository: JsonProcessorRepository
) -> None:
    job = make_job(repository)
    repository.claim_due(1, lease_seconds=60, owner="worker-a")
    database._connection.execute(
        "UPDATE json_repair_jobs SET lease_until = NULL WHERE id = ?",
        (job["id"],),
    )

    assert repository.recover_expired_leases() == 1
    assert repository.get_job(job["id"])["status"] == "queued"


# ---------------------------------------------------------------------------
# Attempts
# ---------------------------------------------------------------------------


def test_attempts_are_ordered_and_metadata_is_sanitized(
    repository: JsonProcessorRepository,
) -> None:
    job = make_job(repository)
    first = repository.add_attempt(
        job["id"],
        method=RepairMethod.LOCAL,
        status="succeeded",
        latency_ms=12,
        request_meta={
            "attempt_phase": "local",
            "api_key": "sk-secret",
            "authorization": "Bearer sk-secret",
            "api_key_ref": "vault:processor",
            "nested": {"password": "hidden", "kept": "yes"},
        },
        token_usage={
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        },
    )
    second = repository.add_attempt(
        job["id"],
        method=RepairMethod.LLM,
        provider="openai-compatible",
        model="repair-model",
        attempt_no=2,
        status="failed",
        validation_error="$.ok: required field is missing",
        request_meta={"request_id": "req-2"},
    )

    assert first["attempt_no"] == 1
    assert second["attempt_no"] == 2
    assert first["request_meta"]["api_key"] == "[redacted]"
    assert first["request_meta"]["authorization"] == "[redacted]"
    assert first["request_meta"]["api_key_ref"] == "vault:processor"
    assert first["request_meta"]["nested"]["password"] == "[redacted]"
    assert first["request_meta"]["nested"]["kept"] == "yes"
    assert "sk-secret" not in first["request_meta_json"]
    assert first["token_usage"] == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
    }
    assert second["provider"] == "openai-compatible"
    assert second["model"] == "repair-model"
    assert second["validation_error"] == "$.ok: required field is missing"
    assert second["token_usage"] is None

    attempts = repository.list_attempts(job["id"])
    assert [item["attempt_no"] for item in attempts] == [1, 2]
    assert [item["method"] for item in attempts] == ["local", "llm"]

    third = repository.add_attempt(job["id"], method="local")
    assert third["attempt_no"] == 3


def test_attempt_number_conflict_and_missing_job(
    repository: JsonProcessorRepository,
) -> None:
    job = make_job(repository)
    repository.add_attempt(job["id"], method="local", attempt_no=1)

    with pytest.raises(JsonRepairStateConflict):
        repository.add_attempt(job["id"], method="local", attempt_no=1)

    with pytest.raises(JsonRepairJobNotFound):
        repository.add_attempt("missing", method="local")
    with pytest.raises(JsonRepairJobNotFound):
        repository.list_attempts("missing")


def test_sanitize_metadata_redacts_without_mutating_input() -> None:
    source = {
        "api_key": "secret",
        "api_key_ref": "ref",
        "list": [{"access_token": "secret"}],
    }
    sanitized = sanitize_metadata(source)

    assert sanitized == {
        "api_key": "[redacted]",
        "api_key_ref": "ref",
        "list": [{"access_token": "[redacted]"}],
    }
    assert source["api_key"] == "secret"


# ---------------------------------------------------------------------------
# Cancellation by source run
# ---------------------------------------------------------------------------


def test_cancel_jobs_by_task_and_vflow_run(
    repository: JsonProcessorRepository,
) -> None:
    task_a_1 = make_job(repository, index=1, task_run_id="run-a", raw="a1")
    task_a_2 = make_job(repository, index=2, task_run_id="run-a", raw="a2")
    claimed = repository.claim_due(2, lease_seconds=60, owner="worker-a")
    assert {item["id"] for item in claimed} == {task_a_1["id"], task_a_2["id"]}

    task_b = make_job(repository, index=3, task_run_id="run-b", raw="b1")
    vflow = make_job(
        repository,
        index=4,
        task_run_id=None,
        source_type="vflow",
        vflow_run_id="vflow-run-1",
        point_id="point-1",
        raw="v1",
    )

    cancelled = repository.cancel_jobs_for_run(task_run_id="run-a")
    assert {item["id"] for item in cancelled} == {task_a_1["id"], task_a_2["id"]}
    assert all(item["status"] == "cancelled" for item in cancelled)
    assert repository.get_job(task_b["id"])["status"] == "queued"
    assert repository.get_job(vflow["id"])["status"] == "queued"

    cancelled_vflow = repository.cancel_jobs_for_run(
        vflow_run_id="vflow-run-1", point_id="point-1"
    )
    assert [item["id"] for item in cancelled_vflow] == [vflow["id"]]
    assert repository.cancel_jobs_for_run(task_run_id="run-a") == []

    with pytest.raises(ValueError, match="is required"):
        repository.cancel_jobs_for_run()


def test_cancel_jobs_for_run_without_force_skips_processing(
    repository: JsonProcessorRepository,
) -> None:
    processing = make_job(repository, index=1, task_run_id="run-a", raw="a1")
    queued = make_job(repository, index=2, task_run_id="run-a", raw="a2")
    repository.claim_due(2, lease_seconds=60, owner="worker-a")

    cancelled = repository.cancel_jobs_for_run(
        task_run_id="run-a", force=False
    )

    # Both jobs are processing, so a non-forced run cancel touches neither.
    assert cancelled == []
    assert repository.get_job(processing["id"])["status"] == "processing"
    assert repository.get_job(queued["id"])["status"] == "processing"


# ---------------------------------------------------------------------------
# Listing, filters and pagination
# ---------------------------------------------------------------------------


def test_list_jobs_filters_and_pagination(
    repository: JsonProcessorRepository,
) -> None:
    created = [
        make_job(repository, index=index, task_run_id=f"run-{index}", raw=f"r{index}")
        for index in range(6)
    ]
    repository.claim_due(6, lease_seconds=60, owner="worker-a")
    repository.complete_job(
        created[0]["id"], lease_owner="worker-a", result_json={"ok": True}
    )
    repository.fail_job(
        created[1]["id"],
        lease_owner="worker-a",
        error_code=ProcessorErrorCode.PROVIDER_ERROR,
        method=RepairMethod.LLM,
    )
    repository.cancel_job(created[2]["id"], force=True)

    first_page = repository.list_jobs(limit=2, offset=0)
    second_page = repository.list_jobs(limit=2, offset=2)
    assert first_page["total"] == 6
    assert first_page["limit"] == 2
    assert first_page["offset"] == 0
    assert len(first_page["items"]) == 2
    assert len(second_page["items"]) == 2
    assert {
        item["id"] for item in first_page["items"]
    }.isdisjoint({item["id"] for item in second_page["items"]})

    succeeded = repository.list_jobs(status=RepairJobStatus.SUCCEEDED)
    assert succeeded["total"] == 1
    assert succeeded["items"][0]["id"] == created[0]["id"]
    failed = repository.list_jobs(error_code="PROVIDER_ERROR")
    assert failed["total"] == 1
    assert failed["items"][0]["id"] == created[1]["id"]
    llm = repository.list_jobs(method=RepairMethod.LLM)
    assert llm["total"] == 1
    by_run = repository.list_jobs(task_run_id="run-4")
    assert by_run["total"] == 1
    assert by_run["items"][0]["id"] == created[4]["id"]

    with pytest.raises(ValueError, match="limit"):
        repository.list_jobs(limit=0)
    with pytest.raises(ValueError, match="offset"):
        repository.list_jobs(offset=-1)
    with pytest.raises(ValueError, match="status"):
        repository.list_jobs(status="unknown")  # type: ignore[arg-type]


def test_queue_depth_counts_all_statuses(
    repository: JsonProcessorRepository,
) -> None:
    make_job(repository, index=1, raw="one")
    make_job(repository, index=2, raw="two")
    repository.claim_due(1, lease_seconds=60, owner="worker-a")

    depth = repository.queue_depth()

    assert depth == {
        "queued": 1,
        "processing": 1,
        "succeeded": 0,
        "failed": 0,
        "cancelled": 0,
    }


# ---------------------------------------------------------------------------
# Retention metadata (preview only)
# ---------------------------------------------------------------------------


def test_retention_defaults_and_preview_are_non_destructive(
    database: Database, repository: JsonProcessorRepository
) -> None:
    settings = repository.get_settings()
    assert settings["raw_output_retention_days"] == 30
    assert settings["attempt_retention_days"] == 90

    old_job = make_job(repository, index=1, raw="old")
    repository.claim_due(1, lease_seconds=60, owner="worker-a")
    repository.complete_job(
        old_job["id"], lease_owner="worker-a", result_json={"ok": True}
    )
    attempt = repository.add_attempt(old_job["id"], method="local")
    active = make_job(repository, index=2, raw="active")

    now = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
    database._connection.execute(
        "UPDATE json_repair_jobs SET finished_at = ? WHERE id = ?",
        ((now - timedelta(days=40)).isoformat(), old_job["id"]),
    )
    database._connection.execute(
        "UPDATE json_repair_attempts SET created_at = ? WHERE id = ?",
        ((now - timedelta(days=100)).isoformat(), attempt["id"]),
    )

    cutoffs = repository.retention_cutoffs(now)
    assert cutoffs["raw_output_cutoff"] == (now - timedelta(days=30)).isoformat()
    assert cutoffs["attempt_cutoff"] == (now - timedelta(days=90)).isoformat()

    preview = repository.retention_preview(now)
    assert preview["jobs_with_expired_raw_output"] == 1
    assert preview["expired_attempts"] == 1
    assert repository.get_job(old_job["id"]) is not None
    assert repository.get_job(active["id"]) is not None
    assert len(repository.list_attempts(old_job["id"])) == 1


def test_retention_preview_skips_nonterminal_jobs(
    database: Database, repository: JsonProcessorRepository
) -> None:
    job = make_job(repository)
    now = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
    database._connection.execute(
        "UPDATE json_repair_jobs SET created_at = ? WHERE id = ?",
        ((now - timedelta(days=365)).isoformat(), job["id"]),
    )

    preview = repository.retention_preview(now)
    assert preview["jobs_with_expired_raw_output"] == 0
    assert repository.get_job(job["id"])["status"] == "queued"


# ---------------------------------------------------------------------------
# Retention purge
# ---------------------------------------------------------------------------


def test_retention_purge_preserves_jobs_and_active_work(
    database: Database, repository: JsonProcessorRepository
) -> None:
    now = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
    old_finished = (now - timedelta(days=40)).isoformat()
    very_old = (now - timedelta(days=400)).isoformat()
    old_attempt_time = (now - timedelta(days=100)).isoformat()

    old_success = make_job(repository, index=1, raw="old-success")
    repository.claim_due(1, lease_seconds=60, owner="retention-worker")
    repository.complete_job(
        old_success["id"],
        lease_owner="retention-worker",
        result_json={"ok": True},
        method=RepairMethod.LOCAL,
    )
    old_success_attempt = repository.add_attempt(old_success["id"], method="local")

    old_failed = make_job(repository, index=2, raw="old-failed")
    repository.claim_due(1, lease_seconds=60, owner="retention-worker")
    repository.fail_job(
        old_failed["id"],
        lease_owner="retention-worker",
        error_code=ProcessorErrorCode.NOT_REPAIRABLE,
        error_message="raw output is not valid JSON",
        method=RepairMethod.LOCAL,
    )
    old_failed_attempt = repository.add_attempt(old_failed["id"], method="local")

    active = make_job(repository, index=3, raw="active")
    repository.claim_due(1, lease_seconds=60, owner="retention-worker")
    active_attempt = repository.add_attempt(active["id"], method="llm")

    fresh = make_job(repository, index=4, raw="fresh")
    repository.claim_due(1, lease_seconds=60, owner="retention-worker")
    repository.complete_job(
        fresh["id"],
        lease_owner="retention-worker",
        result_json={"ok": True},
        method=RepairMethod.LOCAL,
    )
    fresh_attempt = repository.add_attempt(fresh["id"], method="local")

    with database.transaction() as connection:
        connection.execute(
            "UPDATE json_repair_jobs SET finished_at = ? WHERE id IN (?, ?)",
            (old_finished, old_success["id"], old_failed["id"]),
        )
        connection.execute(
            "UPDATE json_repair_jobs SET created_at = ? WHERE id = ?",
            (very_old, active["id"]),
        )
        connection.execute(
            "UPDATE json_repair_attempts SET created_at = ? WHERE id IN (?, ?, ?)",
            (
                old_attempt_time,
                old_success_attempt["id"],
                old_failed_attempt["id"],
                active_attempt["id"],
            ),
        )

    preview = repository.retention_preview(now)
    assert preview["jobs_with_expired_raw_output"] == 1
    assert preview["expired_attempts"] == 2
    assert preview["raw_output_retention_days"] == 30
    assert preview["attempt_retention_days"] == 90

    result = repository.retention_purge(now=now, batch_size=500)

    assert result["jobs_output_purged"] == 1
    assert result["attempts_deleted"] == 2
    assert result["has_more"] is False
    assert result["remaining"]["jobs_with_expired_raw_output"] == 0
    assert result["remaining"]["expired_attempts"] == 0

    preserved = repository.get_job(old_success["id"])
    assert preserved is not None
    assert preserved["status"] == "succeeded"
    assert preserved["result"] is None
    assert preserved["has_result"] is False
    assert preserved["schema"] == SCHEMA
    assert preserved["raw_output_ref"] == "artifact://raw/1"
    assert preserved["raw_hash"]
    assert preserved["schema_hash"]
    assert preserved["method"] == "local"
    assert repository.list_attempts(old_success["id"]) == []

    failed = repository.get_job(old_failed["id"])
    assert failed is not None
    assert failed["status"] == "failed"
    assert failed["error_code"] == "NOT_REPAIRABLE"
    assert failed["error_message"] == "raw output is not valid JSON"
    assert repository.list_attempts(old_failed["id"]) == []

    stalled = repository.get_job(active["id"])
    assert stalled is not None
    assert stalled["status"] == "processing"
    assert [item["id"] for item in repository.list_attempts(active["id"])] == [
        active_attempt["id"]
    ]

    kept = repository.get_job(fresh["id"])
    assert kept is not None
    assert kept["result"] == {"ok": True}
    assert [item["id"] for item in repository.list_attempts(fresh["id"])] == [
        fresh_attempt["id"]
    ]

    again = repository.retention_purge(now=now, batch_size=500)
    assert again["jobs_output_purged"] == 0
    assert again["attempts_deleted"] == 0
    assert again["has_more"] is False


def test_retention_purge_honors_batch_size(
    database: Database, repository: JsonProcessorRepository
) -> None:
    now = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
    jobs = [make_job(repository, index=index, raw=f"old-{index}") for index in range(3)]
    repository.claim_due(3, lease_seconds=60, owner="batch-worker")
    for job in jobs:
        repository.complete_job(
            job["id"],
            lease_owner="batch-worker",
            result_json={"ok": True},
        )
        repository.add_attempt(job["id"], method="local")
    old = (now - timedelta(days=40)).isoformat()
    with database.transaction() as connection:
        connection.execute("UPDATE json_repair_jobs SET finished_at = ?", (old,))
        connection.execute(
            "UPDATE json_repair_attempts SET created_at = ?",
            ((now - timedelta(days=100)).isoformat(),),
        )

    first = repository.retention_purge(now=now, batch_size=1)
    assert first["jobs_output_purged"] == 1
    assert first["attempts_deleted"] == 1
    assert first["has_more"] is True

    second = repository.retention_purge(now=now, batch_size=10)
    assert second["jobs_output_purged"] == 2
    assert second["attempts_deleted"] == 2
    assert second["has_more"] is False
    assert repository.list_attempts(jobs[0]["id"]) == []

    with pytest.raises(ValueError, match="batch_size"):
        repository.retention_purge(batch_size=0)
