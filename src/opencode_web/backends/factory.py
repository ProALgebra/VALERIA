from .contracts import OpenCodeBackend
from .v1 import OpenCodeV1Backend


def create_backend(
    base_url: str,
    username: str,
    password: str,
    *,
    api_version: str = "v1",
    workspace: str | None = None,
) -> OpenCodeBackend:
    if api_version == "v1":
        return OpenCodeV1Backend(base_url, username, password, workspace=workspace)
    if api_version == "v2":
        from .v2 import OpenCodeV2Backend

        return OpenCodeV2Backend(base_url, "opencode", password, workspace=workspace)
    raise ValueError("Unknown OpenCode API version")
