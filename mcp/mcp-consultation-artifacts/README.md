# Consultation Artifacts MCP

Local MCP server for delivering files generated during a Valeria consultation.
The agent writes a deliverable to the per-job outbox and calls
`artifact_publish`; the server sends it to Valeria's authenticated internal
artifact endpoint. Valeria copies the bytes into its own store and records
metadata in SQLite.

The server does not expose workspace paths to the browser. The HTTP endpoint
accepts only regular files from the job outbox and only while that consultation
question is active.

## Tools

- `artifact_publish(path, name?, description?, content_type?)` — publish a
  regular file at the top level of the current job outbox.
- `artifact_publish_text(name, text, description?, content_type?)` — publish
  generated UTF-8 text without creating a workspace file.

The OpenCode v2 session ID is read from request metadata
(`ai.opencode/sessionID`). It is used to find the current consultation; it is
not accepted as a model-visible tool argument.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `VALERIA_URL` | `http://127.0.0.1:8081` | Valeria base URL |
| `VALERIA_ARTIFACT_TOKEN` | empty | Explicit service token (32+ chars) |
| `VALERIA_ARTIFACT_TOKEN_FILE` | `/app/data/consultation-artifacts.token` | Token file created by Valeria |
| `VALERIA_REQUEST_TIMEOUT_SECONDS` | `60` | HTTP timeout |

`VALERIA_ARTIFACT_TOKEN` takes precedence over the file. The token is never
written to logs or returned in tool errors.

## Development

```bash
cd mcp/mcp-consultation-artifacts
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pytest -q
```
