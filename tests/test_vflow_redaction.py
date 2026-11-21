from __future__ import annotations

import json

from opencode_web.database import Database
from opencode_web.vflow.api import _admin_run_view
from opencode_web.vflow.redaction import (
    DEFAULT_MAX_DEPTH,
    REDACTED,
    redact_metadata,
)
from opencode_web.vflow.runtime import VFlowRuntime
from opencode_web.vflow.service import VFlowService

SOURCE = """\
workflow "pass-through" {
  version: 1;
  input: json;
  step pass = transform(operations: [
    {op: "set", path: "$.ok", value: true}
  ]);
  start -> pass -> end
}
"""


def make_service(tmp_path):
    database = Database(tmp_path / "redaction.sqlite3", "secret")
    database.initialize()
    admin = database.create_user("admin", "a-strong-password", "admin", str(tmp_path))
    return database, admin, VFlowService(database)


def test_redact_nested_objects_lists_and_case_insensitive_keys():
    metadata = {
        "slug": "safe-flow",
        "count": 3,
        "enabled": True,
        "nothing": None,
        "Password": "hunter2",
        "client_secret": {"value": "s3cr3t"},
        "headers": {
            "Authorization": "Bearer abc",
            "X-API-Key": "api-key-value",
            "Cookie": "session=abc",
            "Accept": "application/json",
        },
        "items": [
            {"accessToken": "token-value", "label": "first"},
            {"private-key": "key-value", "number": 7},
            {
                "safe": [
                    1,
                    True,
                    None,
                    {"refreshToken": "refresh-value", "nested": {"ok": "yes"}},
                ]
            },
        ],
        "credentials": ["one", "two"],
    }

    redacted = redact_metadata(metadata)

    assert redacted["slug"] == "safe-flow"
    assert redacted["count"] == 3
    assert redacted["enabled"] is True
    assert redacted["nothing"] is None
    assert redacted["Password"] == REDACTED
    assert redacted["client_secret"] == REDACTED
    assert redacted["credentials"] == REDACTED
    assert redacted["headers"] == {
        "Authorization": REDACTED,
        "X-API-Key": REDACTED,
        "Cookie": REDACTED,
        "Accept": "application/json",
    }
    assert redacted["items"][0] == {"accessToken": REDACTED, "label": "first"}
    assert redacted["items"][1] == {"private-key": REDACTED, "number": 7}
    assert redacted["items"][2]["safe"][:3] == [1, True, None]
    assert redacted["items"][2]["safe"][3] == {
        "refreshToken": REDACTED,
        "nested": {"ok": "yes"},
    }

    # The input is not mutated.
    assert metadata["Password"] == "hunter2"
    assert metadata["client_secret"] == {"value": "s3cr3t"}


def test_redact_depth_guard_hides_deep_values_and_keeps_shallow_ones():
    shallow = {"a": {"b": {"c": 1}}}
    assert redact_metadata(shallow) == shallow
    assert redact_metadata(shallow, max_depth=1) == {"a": REDACTED}

    nested: dict[str, object] = {}
    cursor = nested
    for _ in range(DEFAULT_MAX_DEPTH + 3):
        child: dict[str, object] = {}
        cursor["level"] = child
        cursor = child
    cursor["password"] = "too-deep-to-leak"

    redacted = redact_metadata(nested)
    rendered = json.dumps(redacted)
    assert "too-deep-to-leak" not in rendered
    assert REDACTED in rendered


def test_audit_row_redacts_secrets_and_keeps_safe_metadata(tmp_path):
    database, admin, service = make_service(tmp_path)
    try:
        definition = service.create_definition(
            admin["id"],
            slug="safe-flow",
            title="Safe flow",
        )
        created_audit = database._one(
            """SELECT metadata_json FROM vflow_audit_events
               WHERE action = 'definition.create' AND object_id = ?""",
            (definition["id"],),
        )
        assert created_audit is not None
        assert json.loads(created_audit["metadata_json"]) == {"slug": "safe-flow"}

        with database.transaction() as connection:
            service.repository._audit(
                connection,
                admin["id"],
                "test.secret",
                "definition",
                definition["id"],
                {
                    "slug": "safe-flow",
                    "password": "hunter2",
                    "nested": {
                        "apiKey": "api-key-value",
                        "safe": {"mode": "all"},
                    },
                },
            )

        row = database._one(
            "SELECT metadata_json FROM vflow_audit_events WHERE action = 'test.secret'"
        )
        assert row is not None
        assert "hunter2" not in row["metadata_json"]
        assert "api-key-value" not in row["metadata_json"]
        assert json.loads(row["metadata_json"]) == {
            "slug": "safe-flow",
            "password": REDACTED,
            "nested": {
                "apiKey": REDACTED,
                "safe": {"mode": "all"},
            },
        }
    finally:
        database.close()


def test_runtime_event_payload_is_redacted(tmp_path):
    database, admin, service = make_service(tmp_path)
    try:
        definition = service.create_definition(
            admin["id"],
            slug="event-safe",
            title="Event safe",
        )
        service.save_draft(definition["id"], admin["id"], source=SOURCE)
        service.publish(definition["id"], admin["id"])
        version = service.repository.get_published(definition["id"])
        assert version is not None
        runtime = VFlowRuntime(database)
        run = runtime.create_run(
            definition_id=definition["id"],
            version_id=version["id"],
            user_id=admin["id"],
            workspace_id=str(tmp_path),
            input_data={},
        )

        with database.transaction() as connection:
            runtime._event(
                connection,
                run["id"],
                None,
                None,
                "TEST_SECRET",
                {
                    "token": "event-token",
                    "safe": {"phase": "test"},
                },
            )

        row = database._one(
            "SELECT payload_json FROM vflow_events WHERE event_type = 'TEST_SECRET'"
        )
        assert row is not None
        assert "event-token" not in row["payload_json"]
        assert json.loads(row["payload_json"]) == {
            "token": REDACTED,
            "safe": {"phase": "test"},
        }

        # Legacy rows written before redaction existed are masked on read too.
        with database.transaction() as connection:
            connection.execute(
                """INSERT INTO vflow_events
                   (run_id, point_id, node_id, event_type, payload_json, created_at)
                   VALUES (?, NULL, NULL, 'LEGACY_SECRET', ?, ?)""",
                (
                    run["id"],
                    json.dumps({"password": "legacy-secret", "step": 1}),
                    run["created_at"],
                ),
            )
        view = _admin_run_view(database, runtime.get_run(run["id"]), details=True)
        payloads = {event["event_type"]: event["payload"] for event in view["events"]}
        assert payloads["LEGACY_SECRET"] == {"password": REDACTED, "step": 1}
    finally:
        database.close()
