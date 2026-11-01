import sqlite3
import stat
from pathlib import Path

import pytest

from valeria_telegram.store import CALLBACK_TTL_SECONDS, Session, Store


def session(uid: int) -> Session:
    return Session(uid, uid + 1000, f"session-{uid}", f"csrf-{uid}", {"id": f"user-{uid}"})


def submission(store: Store, uid: int = 1, update_id: int = 100) -> dict:
    return store.save_submission(
        uid,
        update_id,
        valeria_user_id=f"user-{uid}",
        path="/api/tasks/t1/runs",
        body={"prompt": "Пример задания"},
        expected_status=201,
        key=f"key-{uid}",
        kind="task",
        context={"task_name": "Пример"},
    )


def test_state_persists_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    with Store(path) as store:
        assert store.get_offset() == 0
        assert store.get_session(1) is None
        assert store.get_flow(1) == {}
        store.save_session(session(1))
        store.set_flow(1, "task", {"task_id": "t1"})
        callback = store.create_callback(1, "run", {"task_id": "t1"})
        store.upsert_watch(1, "task", "run-1", {"last_status": "running"})
        store.set_offset(101)
        store.mark_processed(100)
        submission(store)
        store.complete_submission(1, 100, {"id": "run-1"})
    with Store(path) as store:
        assert store.get_session(1) == session(1)
        assert store.get_flow(1) == {"kind": "task", "data": {"task_id": "t1"}}
        assert store.get_callback(1, callback) == {"action": "run", "data": {"task_id": "t1"}}
        assert store.get_watch(1, "task", "run-1") == {"last_status": "running"}
        assert store.get_offset() == 101
        assert store.is_processed(100)
        assert not store.is_processed(101)
        assert store.get_submission(1, 100)["result"] == {"id": "run-1"}


def test_new_directories_and_database_are_private(tmp_path: Path) -> None:
    existing_mode = stat.S_IMODE(tmp_path.stat().st_mode)
    path = tmp_path / "new" / "nested" / "state.db"
    with Store(path) as store:
        store.save_session(session(1))
        for item in path.parent.iterdir():
            assert stat.S_IMODE(item.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.parent.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(tmp_path.stat().st_mode) == existing_mode
    path.chmod(0o644)
    with Store(path):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_does_not_follow_database_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("leave this file alone")
    link = tmp_path / "state.db"
    link.symlink_to(target)
    with pytest.raises(OSError):
        Store(link)
    assert target.read_text() == "leave this file alone"


def test_constructor_closes_connection_when_database_is_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.db"
    path.write_bytes(b"This is not a SQLite database")
    opened = []
    original_connect = sqlite3.connect

    def connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr("valeria_telegram.store.sqlite3.connect", connect)
    with pytest.raises(sqlite3.DatabaseError):
        Store(path)
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        opened[0].execute("SELECT 1")


def test_sessions_hide_tokens_and_return_copies(tmp_path: Path) -> None:
    saved = session(1)
    assert saved.session_token not in repr(saved)
    assert saved.csrf_token not in repr(saved)
    with Store(tmp_path / "state.db") as store:
        store.save_session(saved)
        saved.user["id"] = "changed"
        loaded = store.get_session(1)
        assert loaded.user == {"id": "user-1"}
        loaded.user["id"] = "also changed"
        assert store.get_session(1).user == {"id": "user-1"}
        store.save_session(Session(1, 2000, "new-session", "new-csrf", {"id": "new-user"}))
        assert store.get_session(1).chat_id == 2000
        store.set_flow(1, "task")
        store.delete_session(1)
        assert store.get_session(1) is None
        assert store.get_flow(1) == {"kind": "task", "data": {}}


def test_callbacks_are_opaque_user_scoped_and_expire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 1000000
    monkeypatch.setattr("valeria_telegram.store.time.time", lambda: now)
    with Store(tmp_path / "state.db") as store:
        first = store.create_callback(1, "task", {"task_id": "secret-task-id"})
        second = store.create_callback(1, "task")
        assert first != second
        assert len(first.encode()) <= 40
        assert "secret-task-id" not in first
        assert store.get_callback(2, first) is None
        assert store.get_callback(1, "unknown") is None
        assert store.get_callback(1, first) == {
            "action": "task",
            "data": {"task_id": "secret-task-id"},
        }
        now += CALLBACK_TTL_SECONDS - 1
        assert store.get_callback(1, first) is not None
        now += 1
        assert store.get_callback(1, first) is None
        third = store.create_callback(2, "fresh")
        store.clear_callbacks(1)
        assert store.get_callback(2, third) is not None


def test_submissions_are_immutable_and_isolated_by_user(tmp_path: Path) -> None:
    with Store(tmp_path / "state.db") as store:
        original = submission(store)
        assert original["result"] is None
        assert original["telegram_id"] == 1
        assert original["update_id"] == 100
        replacement = store.save_submission(
            1,
            100,
            valeria_user_id="changed",
            path="/api/wrong",
            body={"changed": True},
            expected_status=200,
            key="different",
            kind="dialogue",
            context={"changed": True},
        )
        assert replacement == original
        replacement["body"]["prompt"] = "mutated"
        assert store.get_submission(1, 100) == original
        assert store.get_submission(2, 100) is None
        other = submission(store, 2)
        assert other["key"] != original["key"]
        store.complete_submission(1, 100, {"id": "first-result"})
        store.complete_submission(1, 100, {"id": "different-result"})
        assert store.get_submission(1, 100)["result"] == {"id": "first-result"}
        assert store.get_submission(2, 100)["result"] is None


def test_clear_user_keeps_other_users_and_processed_receipts(tmp_path: Path) -> None:
    with Store(tmp_path / "state.db") as store:
        callbacks = {}
        for uid in (1, 2):
            store.save_session(session(uid))
            store.set_flow(uid, "task")
            callbacks[uid] = store.create_callback(uid, "run")
            store.upsert_watch(uid, "task", "run-1", {"status": "running"})
            submission(store, uid)
        store.mark_processed(100)
        store.set_offset(101)
        store.clear_user(1)
        assert store.get_session(1) is None
        assert store.get_flow(1) == {}
        assert store.get_callback(1, callbacks[1]) is None
        assert store.list_watches(1) == []
        assert store.get_submission(1, 100) is None
        assert store.get_session(2) == session(2)
        assert store.get_flow(2)["kind"] == "task"
        assert store.get_callback(2, callbacks[2]) is not None
        assert len(store.list_watches(2)) == 1
        assert store.get_submission(2, 100) is not None
        assert store.is_processed(100)
        assert store.get_offset() == 101


def test_watch_updates_listing_and_removal(tmp_path: Path) -> None:
    with Store(tmp_path / "state.db") as store:
        store.upsert_watch(1, "task", "run-1", {"status": "queued"})
        store.upsert_watch(1, "task", "run-1", {"status": "completed"})
        store.upsert_watch(1, "consultation", "run-1", {"status": "running"})
        store.upsert_watch(2, "task", "run-1", {})
        assert len(store.list_watches()) == 3
        assert len(store.list_watches(1)) == 2
        assert store.get_watch(1, "task", "run-1") == {"status": "completed"}
        store.remove_watch(1, "task", "run-1")
        assert store.get_watch(1, "task", "run-1") is None
        assert len(store.list_watches(1)) == 1
        assert len(store.list_watches(2)) == 1


def test_clear_flow_keeps_other_user(tmp_path: Path) -> None:
    with Store(tmp_path / "state.db") as store:
        store.set_flow(1, "task", {"step": "prompt"})
        store.set_flow(2, "consultation")
        store.clear_flow(1)
        assert store.get_flow(1) == {}
        assert store.get_flow(2) == {"kind": "consultation", "data": {}}
