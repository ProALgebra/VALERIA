"""User-scoped MCP tools backed by Valeria's HTTP API."""

from __future__ import annotations

import argparse
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from config import ConfigError, Settings
from valeria_client import ValeriaClient

READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True,
)
CREATE_RUN = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True,
)


def create_server(
    settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None,
) -> FastMCP:
    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[ValeriaClient]:
        async with ValeriaClient(settings, transport=transport) as client:
            yield client

    mcp = FastMCP(
        "valeria-tasks",
        lifespan=lifespan,
        log_level="WARNING",
        instructions=(
            "Все операции выполняются от имени пользователя, заданного в конфигурации MCP. "
            "Получите доступные таски через valeria_list_tasks. Для запуска задайте уникальный "
            "idempotency_key; при повторе того же запроса после сетевой ошибки используйте "
            "тот же ключ. Полученный id запуска передавайте в valeria_get_task_run. "
            "Статусы completed, failed и cancelled конечные. После completed итоговый ответ "
            "доступен через valeria_get_task_result."
        ),
    )

    @mcp.tool(annotations=READ_ONLY)
    async def valeria_list_tasks(ctx: Context) -> dict[str, Any]:
        """Список опубликованных тасок, доступных настроенному пользователю.

        Возвращает tasks с id, названием и описанием. Используйте id при запуске.
        """
        return {"tasks": await ctx.request_context.lifespan_context.list_tasks()}

    @mcp.tool(annotations=CREATE_RUN)
    async def valeria_create_task_run(
        task_id: Annotated[str, Field(min_length=1, description="ID доступной таски")],
        idempotency_key: Annotated[
            str, Field(min_length=1, max_length=200, description="Уникальный ключ запроса")
        ],
        ctx: Context,
        user_input: Annotated[
            str, Field(max_length=100_000, description="Дополнение пользователя к таске")
        ] = "",
    ) -> dict[str, Any]:
        """Создать запуск доступной таски от имени настроенного пользователя.

        idempotency_key: новый уникальный ASCII-ключ для нового запуска, например UUID.
        При сетевой ошибке повторяйте те же аргументы с тем же ключом. Не меняйте ключ
        при повторе: сервер мог уже принять запуск. Ответ содержит id и исходный status;
        текущий статус запрашивайте через valeria_get_task_run.
        """
        return await ctx.request_context.lifespan_context.create_task_run(
            task_id, user_input, idempotency_key=idempotency_key,
        )

    @mcp.tool(annotations=READ_ONLY)
    async def valeria_list_task_runs(ctx: Context) -> dict[str, Any]:
        """Последние запуски настроенного пользователя с текущими статусами.

        Возвращает runs от новых к старым, не более 500: это лимит HTTP API Valeria.
        """
        return {"runs": await ctx.request_context.lifespan_context.list_task_runs()}

    @mcp.tool(annotations=READ_ONLY)
    async def valeria_get_task_run(
        run_id: Annotated[str, Field(min_length=1, description="ID запуска")],
        ctx: Context,
    ) -> dict[str, Any]:
        """Текущее состояние собственного запуска по id.

        status: queued, dispatching, running, cancelling, completed, failed или cancelled.
        Последние три статуса конечные. phase показывает plan или build.
        """
        return await ctx.request_context.lifespan_context.get_task_run(run_id)

    @mcp.tool(annotations=READ_ONLY)
    async def valeria_get_task_result(
        run_id: Annotated[str, Field(min_length=1, description="ID запуска")],
        ctx: Context,
    ) -> dict[str, Any]:
        """Итоговый ответ запуска: run_id, status и text.

        status: available — ответ в text; not_completed — запуск ещё не завершён успешно;
        unavailable — итогового ответа нет. При временной ошибке API вызов можно повторить.
        """
        return await ctx.request_context.lifespan_context.get_task_result(run_id)

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(description="Valeria tasks MCP server (stdio)")
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
    create_server(settings).run(transport="stdio")


if __name__ == "__main__":
    main()
