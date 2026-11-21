"""Publish files produced by a consultation agent as Valeria artifacts."""

from __future__ import annotations

import argparse
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

PUBLISH = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)
SESSION_META_KEY = "ai.opencode/sessionID"
DEFAULT_TOKEN_FILE = "/app/data/consultation-artifacts.token"


class SettingsError(ValueError):
    pass


class ArtifactClientError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None):
        self.status_code = status_code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class Settings:
    url: str = "http://127.0.0.1:8081"
    token: str | None = None
    token_file: Path | None = None
    timeout_seconds: float = 60.0

    @classmethod
    def from_env(cls) -> Settings:
        url = os.getenv("VALERIA_URL", "http://127.0.0.1:8081").strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise SettingsError("VALERIA_URL must be an HTTP(S) URL")
        token = os.getenv("VALERIA_ARTIFACT_TOKEN", "").strip() or None
        raw_token_file = os.getenv("VALERIA_ARTIFACT_TOKEN_FILE", DEFAULT_TOKEN_FILE).strip()
        token_file = Path(raw_token_file) if raw_token_file else None
        raw_timeout = os.getenv("VALERIA_REQUEST_TIMEOUT_SECONDS", "60")
        try:
            timeout = float(raw_timeout)
        except ValueError as exc:
            raise SettingsError("VALERIA_REQUEST_TIMEOUT_SECONDS must be a number") from exc
        if timeout <= 0:
            raise SettingsError("VALERIA_REQUEST_TIMEOUT_SECONDS must be positive")
        return cls(url=url, token=token, token_file=token_file, timeout_seconds=timeout)

    def resolve_token(self) -> str:
        token = (self.token or "").strip()
        if not token and self.token_file:
            try:
                token = self.token_file.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise SettingsError("Cannot read VALERIA_ARTIFACT_TOKEN_FILE") from exc
        if len(token) < 32:
            raise SettingsError("Artifact token must contain at least 32 characters")
        return token


class ArtifactClient:
    def __init__(
        self,
        settings: Settings,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.settings = settings
        self.token = token
        self.transport = transport

    async def publish(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            async with httpx.AsyncClient(
                base_url=self.settings.url,
                timeout=self.settings.timeout_seconds,
                transport=self.transport,
            ) as client:
                response = await client.post(
                    "/api/internal/consultation-artifacts",
                    json=payload,
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            raise ArtifactClientError("Valeria is unavailable") from exc
        if response.status_code >= 400:
            raise ArtifactClientError(
                "Valeria rejected the artifact",
                status_code=response.status_code,
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise ArtifactClientError("Valeria returned an invalid response") from exc
        if not isinstance(data, dict) or not data.get("id"):
            raise ArtifactClientError("Valeria returned an invalid response")
        return data


def _session_id(ctx: Context) -> str:
    meta = ctx.request_context.meta
    if meta is None:
        raise ToolError("OpenCode session metadata is missing")
    if isinstance(meta, dict):
        value = meta.get(SESSION_META_KEY)
    else:
        extra = getattr(meta, "model_extra", None)
        if isinstance(extra, dict) and SESSION_META_KEY in extra:
            value = extra.get(SESSION_META_KEY)
        else:
            value = getattr(meta, SESSION_META_KEY, None)
    if not isinstance(value, str) or not value:
        raise ToolError("OpenCode session metadata is missing")
    return value


def create_server(
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastMCP:
    token = settings.resolve_token()

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[ArtifactClient]:
        yield ArtifactClient(settings, token, transport=transport)

    mcp = FastMCP(
        "consultation-artifacts",
        lifespan=lifespan,
        log_level="WARNING",
        instructions=(
            "Publish generated files for the current consultation. The caller "
            "must pass the absolute path of a regular file created in the job's "
            "outbox. Use artifact_publish_text for a short text deliverable. "
            "Never claim a file was delivered unless the tool returned an id."
        ),
    )

    @mcp.tool(annotations=PUBLISH)
    async def artifact_publish(
        path: Annotated[
            str, Field(min_length=1, max_length=4096, description="Absolute file path")
        ],
        ctx: Context,
        name: Annotated[str | None, Field(max_length=200, description="Display name")] = None,
        description: Annotated[
            str | None, Field(max_length=2000, description="Short description")
        ] = None,
        content_type: Annotated[
            str | None, Field(max_length=200, description="MIME type")
        ] = None,
    ) -> dict[str, Any]:
        """Publish a regular file from the consultation outbox."""
        payload = {
            "session_id": _session_id(ctx),
            "path": path,
            "name": name,
            "description": description,
            "content_type": content_type,
        }
        try:
            return await ctx.request_context.lifespan_context.publish(payload)
        except ArtifactClientError as exc:
            suffix = f" (HTTP {exc.status_code})" if exc.status_code else ""
            raise ToolError(f"Artifact publish failed{suffix}") from None

    @mcp.tool(annotations=PUBLISH)
    async def artifact_publish_text(
        name: Annotated[str, Field(min_length=1, max_length=200, description="Display name")],
        text: Annotated[str, Field(min_length=1, max_length=2_000_000, description="File text")],
        ctx: Context,
        description: Annotated[
            str | None, Field(max_length=2000, description="Short description")
        ] = None,
        content_type: Annotated[
            str | None, Field(max_length=200, description="MIME type")
        ] = None,
    ) -> dict[str, Any]:
        """Publish generated UTF-8 text without creating a workspace file."""
        payload = {
            "session_id": _session_id(ctx),
            "text": text,
            "name": name,
            "description": description,
            "content_type": content_type,
        }
        try:
            return await ctx.request_context.lifespan_context.publish(payload)
        except ArtifactClientError as exc:
            suffix = f" (HTTP {exc.status_code})" if exc.status_code else ""
            raise ToolError(f"Artifact publish failed{suffix}") from None

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(description="Consultation artifacts MCP server (stdio)")
    parser.add_argument("--env-file", type=Path, help="Optional dotenv file")
    args = parser.parse_args()
    if args.env_file is not None:
        if not args.env_file.is_file():
            parser.error("Env file was not found")
        for line in args.env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())
    try:
        settings = Settings.from_env()
        settings.resolve_token()
    except (SettingsError, OSError) as exc:
        parser.error(str(exc))
    create_server(settings).run(transport="stdio")


if __name__ == "__main__":
    main()
