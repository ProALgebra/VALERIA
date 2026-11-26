from typing import Any

from .contracts import OpenCodeBackend
from .v1 import OpenCodeV1Backend


def create_backend(
    base_url: str,
    username: str,
    password: str,
    *,
    api_version: str = "v1",
    workspace: str | None = None,
    api_key: str | None = None,
    state_store: Any = None,
    source: str = "valeria",
    model_name: str = "hermes",
) -> OpenCodeBackend:
    if api_version == "v1":
        return OpenCodeV1Backend(base_url, username, password, workspace=workspace)
    if api_version == "v2":
        from .v2 import OpenCodeV2Backend

        return OpenCodeV2Backend(base_url, "opencode", password, workspace=workspace)
    if api_version == "hermes":
        from .hermes import HermesBackend

        return HermesBackend(
            base_url,
            api_key or password,
            workspace=workspace,
            state_store=state_store,
            source=source,
            model_name=model_name,
        )
    raise ValueError("Unknown backend API version")
