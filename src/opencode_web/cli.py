from __future__ import annotations

import argparse
import asyncio
import os

import uvicorn
from dotenv import load_dotenv

from .config import Settings


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="OpenCode Web server")
    parser.add_argument("--host", help="Override APP_HOST")
    parser.add_argument("--port", type=int, help="Override APP_PORT")
    commands = parser.add_subparsers(dest="command")
    switch = commands.add_parser(
        "backend-switch", help="Snapshot histories before changing API version"
    )
    switch.add_argument("--to", choices=("v1", "v2"), required=True)
    args = parser.parse_args()
    if args.host:
        os.environ["APP_HOST"] = args.host
    if args.port:
        os.environ["APP_PORT"] = str(args.port)
    settings = Settings.from_env()
    if args.command == "backend-switch":
        from .backend_switch import prepare_backend_switch

        count = asyncio.run(prepare_backend_switch(settings, args.to))
        print(f"Saved {count} histories. Set OPENCODE_API_VERSION={args.to} and start the service.")
        return
    uvicorn.run(
        "opencode_web.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        # SSE connections otherwise prevent lifespan cleanup from being reached.
        timeout_graceful_shutdown=10,
    )


if __name__ == "__main__":
    main()
