from __future__ import annotations

import pytest

from opencode_web.database import Database
from opencode_web.vflow.repository import (
    VFlowConflict,
    VFlowPreconditionFailed,
)
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
    database = Database(tmp_path / "vflow.sqlite3", "secret")
    database.initialize()
    admin = database.create_user("admin", "a-strong-password", "admin", None)
    return database, admin, VFlowService(database)


def test_draft_etag_publish_rollback_and_catalog(tmp_path):
    database, admin, service = make_service(tmp_path)
    try:
        definition = service.create_definition(
            admin["id"],
            slug="pass-through",
            title="Pass through",
            description="Test pipeline",
        )
        draft = service.save_draft(
            definition["id"],
            admin["id"],
            source=SOURCE,
            input_schema={"type": "object"},
        )
        assert draft["status"] == "draft"
        assert draft["version_number"] == 1
        assert draft["etag"].startswith('"')

        report = service.validate_definition(definition["id"])
        assert report["valid"] is True

        with pytest.raises(VFlowPreconditionFailed):
            service.save_draft(
                definition["id"],
                admin["id"],
                source=SOURCE,
                expected_etag='"wrong"',
            )

        updated = service.save_draft(
            definition["id"],
            admin["id"],
            source=SOURCE,
            expected_etag=draft["etag"],
        )
        assert updated["etag"] == draft["etag"]

        published = service.publish(
            definition["id"], admin["id"], expected_etag=draft["etag"]
        )
        assert published["status"] == "published"
        assert service.user_catalog("someone")[0]["version"] == 1

        second = service.save_draft(
            definition["id"],
            admin["id"],
            source=SOURCE,
        )
        assert second["version_number"] == 2
        service.publish(definition["id"], admin["id"])
        assert service.user_catalog("someone")[0]["version"] == 2

        service.rollback(definition["id"], published["id"], admin["id"])
        assert service.user_catalog("someone")[0]["version"] == 1
    finally:
        database.close()


def test_selected_audience_and_archive(tmp_path):
    database, admin, service = make_service(tmp_path)
    try:
        user = database.create_user(
            "worker", "a-strong-password", "user", str(tmp_path)
        )
        definition = service.create_definition(
            admin["id"],
            slug="selected-flow",
            title="Selected flow",
        )
        service.save_draft(
            definition["id"],
            admin["id"],
            source=SOURCE,
        )
        service.publish(definition["id"], admin["id"])

        assert service.user_catalog(admin["id"])
        assert service.user_catalog(user["id"])

        service.set_audience(
            definition["id"],
            admin["id"],
            {"mode": "selected", "user_ids": [user["id"]]},
        )
        assert service.user_catalog(user["id"])
        assert service.user_catalog(admin["id"]) == []

        service.archive(definition["id"], admin["id"])
        assert service.user_catalog(user["id"]) == []
        with pytest.raises(VFlowConflict):
            service.save_draft(definition["id"], admin["id"], source=SOURCE)
    finally:
        database.close()


def test_new_draft_inherits_published_schemas_and_explicit_null_clears(tmp_path):
    database, admin, service = make_service(tmp_path)
    try:
        definition = service.create_definition(
            admin["id"], slug="schema-inherit", title="Schema inherit"
        )
        input_schema = {"type": "object", "required": ["name"]}
        public_schema = {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
        }
        service.save_draft(
            definition["id"],
            admin["id"],
            source=SOURCE,
            input_schema=input_schema,
            public_output_schema=public_schema,
        )
        service.publish(definition["id"], admin["id"])

        # Editing a published pipeline without resending schemas must not
        # silently discard them when the next draft is created.
        second = service.save_draft(
            definition["id"], admin["id"], source=SOURCE
        )
        assert second["version_number"] == 2
        assert second["input_schema"] == input_schema
        assert second["public_output_schema"] == public_schema

        # An explicit null still clears the public schema, matching the UI's
        # "empty public result" action.
        cleared = service.save_draft(
            definition["id"],
            admin["id"],
            source=SOURCE,
            public_output_schema=None,
            expected_etag=second["etag"],
        )
        assert cleared["public_output_schema"] is None
        assert cleared["input_schema"] == input_schema
    finally:
        database.close()
