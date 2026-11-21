from __future__ import annotations

from opencode_web.database import Database


def test_vflow_tables_are_created(tmp_path):
    db = Database(tmp_path / "test.sqlite3", "secret")
    try:
        db.initialize()
        names = {
            row["name"]
            for row in db._all(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name LIKE 'vflow_%'"
            )
        }
        assert names == {
            "vflow_audit_events",
            "vflow_definitions",
            "vflow_events",
            "vflow_fork_instances",
            "vflow_merge_arrivals",
            "vflow_outbox",
            "vflow_points",
            "vflow_runs",
            "vflow_task_links",
            "vflow_versions",
        }
        assert db._one("PRAGMA foreign_keys")["foreign_keys"] == 1
    finally:
        db.close()


def test_vflow_run_limits_column_is_migrated_for_existing_databases(tmp_path):
    db = Database(tmp_path / "legacy.sqlite3", "secret")
    try:
        db.initialize()
        db._connection.execute("ALTER TABLE vflow_runs DROP COLUMN limits_json")
        db.initialize()
        columns = {
            row["name"] for row in db._all("PRAGMA table_info(vflow_runs)")
        }
        assert "limits_json" in columns
    finally:
        db.close()
