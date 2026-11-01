"""Administrative Valeria tools exposed through the MCP stdio transport."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from pydantic import ValidationError

from admin_client import AdminClient, AdminError
from admin_config import ConfigError, Settings
from admin_operations import OPERATIONS

logger = logging.getLogger(__name__)


def error_result(message: str, **details: Any) -> types.CallToolResult:
    payload = {"error": {"message": message, **details}}
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
        structuredContent=payload,
        isError=True,
    )


def create_server(
    settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None,
) -> Server:
    @asynccontextmanager
    async def lifespan(_server: Server) -> AsyncIterator[AdminClient]:
        async with AdminClient(settings, transport=transport) as client:
            yield client

    server = Server(
        "valeria-administrator",
        version="0.1.0",
        lifespan=lifespan,
        instructions=(
            "Администрирование Valeria от имени учётной записи в конфигурации MCP. "
            "Требуется роль admin. Пользователи, таски, очереди, шаблоны, консультации, "
            "запуски, статистика и аудит управляются через существующий HTTP API. "
            "Перед созданием таски получите доступные модели и агенты через get_options. "
            "При PATCH передавайте только изменяемые поля: пустой список явно очищает "
            "связи, null сбрасывает только поддерживаемые настройки. "
            "Создание пользователей, тасок, шаблонов и очередей не поддерживает ключи "
            "идемпотентности в API: после сетевой ошибки сначала проверьте состояние. "
            "Для send_chat_message и create_task_run обязателен уникальный idempotency_key; "
            "после сетевой ошибки повторяйте те же аргументы с тем же ключом. "
            "create_task_run создаёт запуск только для настроенного администратора."
        ),
    )

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=operation.name,
                description=operation.description,
                inputSchema=operation.input_model.model_json_schema(),
                outputSchema={"type": "object", "additionalProperties": True},
                annotations=types.ToolAnnotations(
                    readOnlyHint=operation.read_only,
                    destructiveHint=operation.destructive,
                    idempotentHint=operation.idempotent,
                    openWorldHint=True,
                ),
            )
            for operation in OPERATIONS.values()
        ]

    # Pydantic errors are formatted without input values, including supplied passwords.
    @server.call_tool(validate_input=False)
    async def call_tool(
        name: str, arguments: dict[str, Any],
    ) -> dict[str, Any] | types.CallToolResult:
        operation = OPERATIONS.get(name)
        if operation is None:
            return error_result("Unknown administrator tool")
        try:
            request = operation.prepare(arguments)
        except ValidationError as exc:
            errors = [
                {"field": ".".join(str(part) for part in error["loc"]), "type": error["type"]}
                for error in exc.errors(include_input=False, include_context=False)
            ]
            return error_result("Invalid tool arguments", fields=errors)
        except ValueError:
            return error_result("Invalid tool arguments")
        try:
            result = await server.request_context.lifespan_context.request(**request)
        except AdminError as exc:
            details: dict[str, Any] = {}
            if exc.status_code is not None:
                details["status_code"] = exc.status_code
            if exc.idempotency_key is not None:
                details["idempotency_key"] = exc.idempotency_key
            return error_result(str(exc), **details)
        except Exception:
            logger.error("Unexpected error executing administrator tool %s", name)
            return error_result("Unexpected administrator MCP error")
        if operation.result_key is not None:
            return {operation.result_key: result}
        if isinstance(result, dict):
            return result
        return {"result": result}

    return server


async def serve(settings: Settings) -> None:
    server = create_server(settings)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    parser = argparse.ArgumentParser(description="Valeria administrator MCP server (stdio)")
    parser.add_argument(
        "--env-file", type=Path,
        help="Файл настроек; по умолчанию config.env рядом с server.py",
    )
    args = parser.parse_args()
    env_file = args.env_file or Path(__file__).with_name("config.env")
    if args.env_file is not None and not env_file.is_file():
        parser.error("Файл --env-file не найден")
    load_dotenv(env_file, override=False)
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        parser.error(str(exc))
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(serve(settings))


if __name__ == "__main__":
    main()
