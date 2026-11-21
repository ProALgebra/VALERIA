"""Durable, atomic acceptance of browser submissions, independent of OpenCode."""

import hashlib
import json
from contextlib import contextmanager

from fastapi import HTTPException


@contextmanager
def submission(db, request, user, body, *, extra=None):
    """Never await inside this block: it holds a SQLite write transaction."""
    key = request.headers.get("idempotency-key")
    if key is not None and (not key.strip() or len(key) > 200):
        raise HTTPException(422, "Invalid Idempotency-Key")
    payload = [request.url.path, user.get("workspace_path"), body.model_dump()]
    if extra is not None:
        payload.append(extra)
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    with db.transaction() as connection:
        if key:
            previous = connection.execute(
                "SELECT * FROM submissions WHERE actor_id = ? AND request_key = ?",
                (user["id"], key),
            ).fetchone()
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise HTTPException(409, "Idempotency-Key was used for another request")
                yield json.loads(previous["response_json"])
                return
        result = {}
        yield result
        if key:
            connection.execute(
                "INSERT INTO submissions VALUES (?, ?, ?, ?)",
                (user["id"], key, fingerprint, json.dumps(result)),
            )
