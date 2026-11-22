"""OpenAI-compatible provider for the standalone JSON repair processor.

The provider is deliberately small: one non-streaming Chat Completions request,
one optional ``response_format=json_object`` fallback, and one connection check.

Security properties
-------------------
* The API key is accepted only as a call argument.  It is never stored on the
  provider, included in a result, or included in an error/log payload.
* ``base_url`` is validated independently from the settings model: it must be an
  absolute ``http(s)`` URL without credentials, query, or fragment.  Metadata and
  link-local addresses are always rejected.  Loopback/private addresses are only
  accepted when the caller explicitly passes ``allow_private_networks=True``
  (for a self-hosted Ollama/vLLM endpoint).
* Redirects and environment proxies are disabled on the owned HTTP client so a
  provider cannot redirect a request to an internal address.

Known limitation
----------------
Hostnames are not resolved during validation.  A public-looking hostname can
still resolve to a private/metadata address at request time (DNS rebinding).  A
production deployment should perform DNS resolution and pin the resolved IP as
well; that is intentionally outside this module's scope.

The ``response_format`` fallback is infrastructure-only: it is not a repair
attempt and does not consume ``max_llm_attempts``.  ``ProviderCompletion``
exposes ``infrastructure_retries`` and ``response_format_fallback_used`` so the
caller can record the distinction without guessing from the returned content.
"""

from __future__ import annotations

import ipaddress
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

import httpx

from .models import ProcessorErrorCode
from .settings import JsonProcessorSettings

_CHAT_PATH = "/v1/chat/completions"
_MODELS_PATH = "/v1/models"
_SAFE_PROVIDER_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
_NUMERIC_HOST_RE = re.compile(r"^[0-9.]+$")
_HEX_HOST_RE = re.compile(r"^0[xX][0-9A-Fa-f]+$")

# These are infrastructure codes, not error messages.  The raw response body is
# never copied into an exception.
_METADATA_HOSTNAMES = frozenset(
    {
        "instance-data",
        "instance-data.ec2.internal",
        "metadata",
        "metadata.alibabacloud.com",
        "metadata.azure.com",
        "metadata.google.internal",
        "metadata.goog",
        "metadata.tencentyun.com",
    }
)
_METADATA_IPS = frozenset(
    {
        ipaddress.ip_address("100.100.100.200"),  # Alibaba Cloud
        ipaddress.ip_address("fd00:ec2::254"),  # AWS IPv6 IMDS
    }
)
# Explicit non-public ranges.  Documentation/benchmark ranges are deliberately
# not included: they are non-routable test addresses and are commonly used with
# mock transports, whereas these ranges are the actual loopback/private surface.
_PRIVATE_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("::1/128"),
)
_RESPONSE_FORMAT_HINTS = (
    "response_format",
    "response format",
    "json_object",
    "unsupported parameter",
    "unknown parameter",
    "unrecognized field",
    "unsupported value",
)
_MAX_API_KEY_CHARS = 8_192
_MAX_MESSAGES = 64
_MAX_MESSAGE_CHARS = 1_000_000
_MAX_PROVIDER_CODE_CHARS = 64
_MAX_ERROR_BODY_CHARS = 4_096


class ProviderErrorKind(StrEnum):
    """Normalized provider failure categories."""

    CONFIGURATION = "configuration"
    AUTHENTICATION = "authentication"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    BAD_REQUEST = "bad_request"
    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    PROVIDER_ERROR = "provider_error"
    TIMEOUT = "timeout"
    NETWORK = "network"
    INVALID_RESPONSE = "invalid_response"


_PROCESSOR_CODES = {
    ProviderErrorKind.CONFIGURATION: ProcessorErrorCode.SETTINGS_INVALID,
    ProviderErrorKind.AUTHENTICATION: ProcessorErrorCode.LLM_UNAVAILABLE,
    ProviderErrorKind.FORBIDDEN: ProcessorErrorCode.LLM_UNAVAILABLE,
    ProviderErrorKind.NOT_FOUND: ProcessorErrorCode.LLM_UNAVAILABLE,
    ProviderErrorKind.BAD_REQUEST: ProcessorErrorCode.PROVIDER_ERROR,
    ProviderErrorKind.RATE_LIMITED: ProcessorErrorCode.PROVIDER_ERROR,
    ProviderErrorKind.SERVER_ERROR: ProcessorErrorCode.PROVIDER_ERROR,
    ProviderErrorKind.PROVIDER_ERROR: ProcessorErrorCode.PROVIDER_ERROR,
    ProviderErrorKind.TIMEOUT: ProcessorErrorCode.TIMEOUT,
    ProviderErrorKind.NETWORK: ProcessorErrorCode.LLM_UNAVAILABLE,
    ProviderErrorKind.INVALID_RESPONSE: ProcessorErrorCode.PROVIDER_ERROR,
}


class ProviderError(RuntimeError):
    """A typed, secret-free provider failure.

    ``status_code`` and ``provider_code`` are safe metadata.  ``message`` never
    contains a response body, request headers, or the API key.
    """

    def __init__(
        self,
        kind: ProviderErrorKind,
        message: str,
        *,
        status_code: int | None = None,
        provider_code: str | None = None,
        retryable: bool = False,
        infrastructure_retries: int = 0,
    ) -> None:
        self.kind = kind
        self.message = message
        self.status_code = status_code
        self.provider_code = provider_code
        self.retryable = retryable
        self.infrastructure_retries = infrastructure_retries
        super().__init__(message)

    @property
    def code(self) -> str:
        """Stable normalized code (for example ``timeout``)."""

        return self.kind.value

    @property
    def status(self) -> int | None:
        """Alias for ``status_code``."""

        return self.status_code

    @property
    def processor_code(self) -> ProcessorErrorCode:
        """Processor error code a worker should persist for this failure."""

        return _PROCESSOR_CODES[self.kind]

    @property
    def error_code(self) -> ProcessorErrorCode:
        """Alias for ``processor_code``."""

        return self.processor_code

    def to_dict(self) -> dict[str, Any]:
        """Return audit-safe error metadata with no request or secret data."""

        return {
            "code": self.kind.value,
            "processor_code": self.processor_code.value,
            "status_code": self.status_code,
            "provider_code": self.provider_code,
            "retryable": self.retryable,
            "infrastructure_retries": self.infrastructure_retries,
            "message": self.message,
        }


class ProviderConfigurationError(ProviderError):
    """The provider configuration or call arguments are unsafe/invalid."""

    def __init__(self, message: str) -> None:
        super().__init__(ProviderErrorKind.CONFIGURATION, message)


@dataclass(frozen=True, slots=True)
class ProviderCompletion:
    """One parsed non-streaming completion.

    ``counts_as_repair_attempt`` is always ``False``: the provider never knows
    how many repair attempts the caller permits.  An internal
    ``response_format`` retry is instead visible through
    ``infrastructure_retries`` and ``response_format_fallback_used``.
    """

    content: str
    model: str
    usage: dict[str, int] | None
    status_code: int
    latency_ms: int
    response_format_used: bool
    infrastructure_retries: int = 0

    @property
    def infrastructure_retry_used(self) -> bool:
        return self.infrastructure_retries > 0

    @property
    def response_format_fallback_used(self) -> bool:
        return self.infrastructure_retries > 0 and not self.response_format_used

    @property
    def counts_as_repair_attempt(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class ProviderConnection:
    """Result of a safe provider/model availability check."""

    ok: bool
    status_code: int
    model_available: bool | None
    latency_ms: int


def _has_control_or_space(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 or char.isspace() for char in value)


def _validate_host(host: str, *, allow_private_networks: bool) -> None:
    normalized = host.strip().lower().rstrip(".")
    if not normalized:
        raise ProviderConfigurationError("base_url must contain a host")
    if "%" in normalized:
        # IPv6 zone identifiers can address link-local interfaces; reject them.
        raise ProviderConfigurationError("base_url host must not contain a zone identifier")
    if normalized in _METADATA_HOSTNAMES:
        raise ProviderConfigurationError("base_url must not target a metadata host")

    is_local_name = normalized == "localhost" or normalized.endswith(".localhost")
    if is_local_name:
        if not allow_private_networks:
            raise ProviderConfigurationError(
                "base_url targets a loopback/private network; explicit opt-in is required"
            )
        return

    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        # Alternate numeric IP spellings (``2130706433``, ``127.1``,
        # ``0177.0.0.1``) are parsed inconsistently by HTTP clients; reject them
        # rather than treating them as ordinary hostnames.
        if _NUMERIC_HOST_RE.fullmatch(normalized) or _HEX_HOST_RE.fullmatch(normalized):
            raise ProviderConfigurationError(
                "base_url host is not a valid IP address"
            ) from None
        # A normal hostname.  DNS is intentionally not resolved here; see the
        # module docstring for the documented DNS-rebinding limitation.
        return

    def check(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
        if address in _METADATA_IPS:
            raise ProviderConfigurationError("base_url must not target a metadata address")
        if address.is_link_local:
            raise ProviderConfigurationError("base_url must not target a link-local address")
        if address.is_unspecified or address.is_multicast:
            raise ProviderConfigurationError("base_url contains an unusable address")
        private_or_loopback = address.is_loopback or any(
            address in network for network in _PRIVATE_NETWORKS
        )
        if private_or_loopback:
            if not allow_private_networks:
                raise ProviderConfigurationError(
                    "base_url targets a loopback/private network; explicit opt-in is required"
                )
            return
        if address.is_reserved:
            raise ProviderConfigurationError("base_url contains a reserved address")

    check(address)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        check(address.ipv4_mapped)


def validate_provider_base_url(
    base_url: str,
    *,
    allow_private_networks: bool = False,
) -> str:
    """Validate and normalize an OpenAI-compatible base URL.

    Metadata/link-local targets are always denied.  Loopback and private network
    targets require ``allow_private_networks=True`` for self-hosted inference
    servers.  Only literal addresses are checked; see the DNS limitation in the
    module docstring.
    """

    if not isinstance(base_url, str) or not base_url.strip():
        raise ProviderConfigurationError("base_url is required")
    raw = base_url.strip()
    if _has_control_or_space(raw) or "\\" in raw:
        raise ProviderConfigurationError(
            "base_url must not contain whitespace, control characters, or backslashes"
        )
    try:
        parsed = urlsplit(raw)
        parsed.port  # noqa: B018 - validate an invalid port eagerly
    except ValueError:
        raise ProviderConfigurationError("base_url is not a valid URL") from None
    if parsed.scheme not in {"http", "https"}:
        raise ProviderConfigurationError("base_url must be an absolute http(s) URL")
    if not parsed.hostname:
        raise ProviderConfigurationError("base_url must contain a host")
    if parsed.username is not None or parsed.password is not None:
        raise ProviderConfigurationError("base_url must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ProviderConfigurationError(
            "base_url must not contain a query or fragment"
        )
    _validate_host(parsed.hostname, allow_private_networks=allow_private_networks)
    return raw.rstrip("/")


def _validate_api_key(api_key: str | None) -> str | None:
    if api_key is None:
        return None
    if not isinstance(api_key, str):
        raise ProviderConfigurationError("api_key must be a string when provided")
    if not api_key:
        return None
    if len(api_key) > _MAX_API_KEY_CHARS:
        raise ProviderConfigurationError("api_key is too long")
    if _has_control_or_space(api_key[:1]) or _has_control_or_space(api_key[-1:]):
        raise ProviderConfigurationError("api_key must not contain whitespace")
    if any(ord(char) < 32 or ord(char) == 127 for char in api_key):
        raise ProviderConfigurationError("api_key must not contain control characters")
    return api_key


def _normalize_messages(
    messages: Sequence[Mapping[str, Any]] | str,
) -> list[dict[str, str]]:
    if isinstance(messages, str):
        items: Sequence[Mapping[str, Any]] = [{"role": "user", "content": messages}]
    elif isinstance(messages, Sequence) and not isinstance(
        messages, (bytes, bytearray)
    ):
        items = messages
    else:
        raise ProviderConfigurationError("messages must be a sequence of messages")
    if not items:
        raise ProviderConfigurationError("messages must not be empty")
    if len(items) > _MAX_MESSAGES:
        raise ProviderConfigurationError("too many messages")
    normalized: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, Mapping):
            raise ProviderConfigurationError("each message must be an object")
        role = item.get("role")
        content = item.get("content")
        if role not in {"system", "user", "assistant"}:
            raise ProviderConfigurationError("message role is invalid")
        if not isinstance(content, str) or not content:
            raise ProviderConfigurationError("message content must be a non-empty string")
        if len(content) > _MAX_MESSAGE_CHARS:
            raise ProviderConfigurationError("message content is too large")
        normalized.append({"role": str(role), "content": content})
    return normalized


def _normalize_usage(value: Any) -> dict[str, int] | None:
    if not isinstance(value, Mapping):
        return None
    usage: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        item = value.get(key)
        if (
            isinstance(item, int)
            and not isinstance(item, bool)
            and 0 <= item <= 2_147_483_647
        ):
            usage[key] = item
    return usage or None


def _response_payload(response: httpx.Response) -> Any:
    try:
        return response.json()
    except (ValueError, TypeError):
        return None


def _extract_provider_code(response: httpx.Response, api_key: str | None) -> str | None:
    payload = _response_payload(response)
    if not isinstance(payload, Mapping):
        return None
    error = payload.get("error")
    candidate: Any = None
    if isinstance(error, Mapping):
        candidate = error.get("code")
        if candidate is None:
            candidate = error.get("type")
    elif isinstance(error, str):
        candidate = None
    else:
        candidate = payload.get("code")
    if not isinstance(candidate, str):
        return None
    normalized = candidate.strip()
    if not normalized or len(normalized) > _MAX_PROVIDER_CODE_CHARS:
        return None
    if not _SAFE_PROVIDER_CODE_RE.fullmatch(normalized):
        return None
    if api_key and (
        api_key in normalized or api_key.casefold() in normalized.casefold()
    ):
        return None
    return normalized


def _response_format_unsupported(response: httpx.Response) -> bool:
    if response.status_code not in {400, 422}:
        return False
    try:
        text = response.text[:_MAX_ERROR_BODY_CHARS].lower()
    except Exception:  # pragma: no cover - defensive for unusual transports
        return False
    return any(hint in text for hint in _RESPONSE_FORMAT_HINTS)


def _error_for_response(
    response: httpx.Response,
    *,
    api_key: str | None,
    infrastructure_retries: int = 0,
) -> ProviderError:
    status = response.status_code
    if status == 401:
        kind = ProviderErrorKind.AUTHENTICATION
        retryable = False
    elif status == 403:
        kind = ProviderErrorKind.FORBIDDEN
        retryable = False
    elif status == 404:
        kind = ProviderErrorKind.NOT_FOUND
        retryable = False
    elif status == 429:
        kind = ProviderErrorKind.RATE_LIMITED
        retryable = True
    elif 500 <= status <= 599:
        kind = ProviderErrorKind.SERVER_ERROR
        retryable = True
    elif 400 <= status <= 499:
        kind = ProviderErrorKind.BAD_REQUEST
        retryable = status in {408, 425}
    else:
        kind = ProviderErrorKind.PROVIDER_ERROR
        retryable = False
    return ProviderError(
        kind,
        f"provider returned HTTP {status} ({kind.value})",
        status_code=status,
        provider_code=_extract_provider_code(response, api_key),
        retryable=retryable,
        infrastructure_retries=infrastructure_retries,
    )


def _invalid_response(
    *,
    status_code: int | None = None,
    infrastructure_retries: int = 0,
) -> ProviderError:
    return ProviderError(
        ProviderErrorKind.INVALID_RESPONSE,
        "provider returned an invalid response",
        status_code=status_code,
        retryable=False,
        infrastructure_retries=infrastructure_retries,
    )


def _safe_model_name(value: Any, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    normalized = value.strip()
    if not normalized or len(normalized) > 200:
        return fallback
    if any(ord(char) < 32 or ord(char) == 127 for char in normalized):
        return fallback
    return normalized


class OpenAICompatibleProvider:
    """Non-streaming OpenAI-compatible Chat Completions client.

    The provider is configured from :class:`JsonProcessorSettings`, but the
    resolved API key is supplied per call.  The provider owns its HTTP client
    unless one is injected; injected clients are not closed by :meth:`aclose`.
    """

    def __init__(
        self,
        settings: JsonProcessorSettings | Mapping[str, Any],
        *,
        allow_private_networks: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if isinstance(settings, Mapping):
            settings = JsonProcessorSettings.from_dict(settings, reject_unknown=False)
        if not isinstance(settings, JsonProcessorSettings):
            raise TypeError("settings must be a JsonProcessorSettings")
        if client is not None and transport is not None:
            raise ValueError("pass either client or transport, not both")

        self._base_url = validate_provider_base_url(
            settings.base_url,
            allow_private_networks=allow_private_networks,
        )
        if not settings.model:
            raise ProviderConfigurationError("model is required")
        self._model = settings.model
        self._max_output_tokens = settings.max_output_tokens
        self._temperature = settings.temperature
        self._default_timeout = float(settings.request_timeout_seconds)
        self._owns_client = client is None
        if client is not None:
            self._client = client
        else:
            self._client = httpx.AsyncClient(
                transport=transport,
                follow_redirects=False,
                trust_env=False,
            )

    # ------------------------------------------------------------------
    # URLs and client lifecycle
    # ------------------------------------------------------------------
    @property
    def model(self) -> str:
        return self._model

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def chat_completions_url(self) -> str:
        return self._join_api_path(_CHAT_PATH)

    @property
    def models_url(self) -> str:
        return self._join_api_path(_MODELS_PATH)

    def _join_api_path(self, api_path: str) -> str:
        # Accept both ``http://host`` and the common ``http://host/v1`` form
        # without producing ``/v1/v1``.
        if self._base_url.endswith("/v1") and api_path.startswith("/v1/"):
            return f"{self._base_url}{api_path[3:]}"
        return f"{self._base_url}{api_path}"

    async def aclose(self) -> None:
        """Close an owned HTTP client.  Injected clients remain caller-owned."""

        if self._owns_client:
            await self._client.aclose()

    async def close(self) -> None:
        await self.aclose()

    async def __aenter__(self) -> OpenAICompatibleProvider:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # Request plumbing
    # ------------------------------------------------------------------
    def _timeout(self, timeout: float | None) -> httpx.Timeout:
        value = self._default_timeout if timeout is None else timeout
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ProviderConfigurationError("timeout must be a number")
        seconds = float(value)
        if not 0 < seconds <= 600:
            raise ProviderConfigurationError("timeout must be between 0 and 600 seconds")
        return httpx.Timeout(seconds, connect=min(10.0, seconds))

    @staticmethod
    def _headers(api_key: str | None) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        key = _validate_api_key(api_key)
        if key is not None:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    async def _send(
        self,
        method: str,
        url: str,
        *,
        api_key: str | None,
        timeout: float | None,
        body: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        headers = self._headers(api_key)
        kwargs: dict[str, Any] = {
            "headers": headers,
            "timeout": self._timeout(timeout),
        }
        if body is not None:
            kwargs["json"] = body
        try:
            return await self._client.request(method, url, **kwargs)
        except (httpx.TimeoutException, TimeoutError):
            # ``from None`` keeps the transport exception (whose request object
            # carries the Authorization header) out of the raised traceback.
            raise ProviderError(
                ProviderErrorKind.TIMEOUT,
                "provider request timed out",
                retryable=True,
            ) from None
        except httpx.HTTPError:
            raise ProviderError(
                ProviderErrorKind.NETWORK,
                "provider network error",
                retryable=True,
            ) from None

    # ------------------------------------------------------------------
    # Public operations
    # ------------------------------------------------------------------
    async def complete(
        self,
        messages: Sequence[Mapping[str, Any]] | str,
        *,
        api_key: str | None = None,
        response_format: bool = True,
        timeout: float | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> ProviderCompletion:
        """Run one non-streaming Chat Completions request and parse content.

        ``api_key`` is used only for this request.  If ``response_format`` is
        rejected by the server as unsupported, exactly one infrastructure retry
        is made without that field.  The retry is not a repair attempt.
        """

        if not isinstance(response_format, bool):
            raise ProviderConfigurationError("response_format must be a boolean")
        resolved_messages = _normalize_messages(messages)
        token_limit = self._max_output_tokens if max_tokens is None else max_tokens
        if (
            isinstance(token_limit, bool)
            or not isinstance(token_limit, int)
            or not 1 <= token_limit <= 32_768
        ):
            raise ProviderConfigurationError("max_tokens must be between 1 and 32768")
        resolved_temperature = self._temperature if temperature is None else temperature
        if isinstance(resolved_temperature, bool) or not isinstance(
            resolved_temperature, (int, float)
        ):
            raise ProviderConfigurationError("temperature must be a number")
        if not 0 <= float(resolved_temperature) <= 2:
            raise ProviderConfigurationError("temperature must be between 0 and 2")

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": resolved_messages,
            "max_tokens": token_limit,
            "temperature": float(resolved_temperature),
            "stream": False,
        }
        if response_format:
            payload["response_format"] = {"type": "json_object"}

        started = time.perf_counter()
        response = await self._send(
            "POST",
            self.chat_completions_url,
            api_key=api_key,
            timeout=timeout,
            body=payload,
        )
        if 200 <= response.status_code < 300:
            return self._parse_completion(
                response,
                started=started,
                response_format_used=response_format,
                infrastructure_retries=0,
            )

        if response_format and _response_format_unsupported(response):
            fallback_payload = {
                key: value for key, value in payload.items() if key != "response_format"
            }
            try:
                retried = await self._send(
                    "POST",
                    self.chat_completions_url,
                    api_key=api_key,
                    timeout=timeout,
                    body=fallback_payload,
                )
            except ProviderError as exc:
                exc.infrastructure_retries = 1
                raise
            if 200 <= retried.status_code < 300:
                return self._parse_completion(
                    retried,
                    started=started,
                    response_format_used=False,
                    infrastructure_retries=1,
                )
            raise _error_for_response(
                retried,
                api_key=api_key,
                infrastructure_retries=1,
            ) from None

        raise _error_for_response(response, api_key=api_key) from None

    async def chat_completion(
        self,
        messages: Sequence[Mapping[str, Any]] | str,
        **kwargs: Any,
    ) -> ProviderCompletion:
        """Alias for :meth:`complete` for callers using OpenAI terminology."""

        return await self.complete(messages, **kwargs)

    async def chat(
        self,
        messages: Sequence[Mapping[str, Any]] | str,
        **kwargs: Any,
    ) -> ProviderCompletion:
        """Short alias for :meth:`complete`."""

        return await self.complete(messages, **kwargs)

    async def check_connection(
        self,
        *,
        api_key: str | None = None,
        timeout: float | None = None,
    ) -> ProviderConnection:
        """Check ``GET /v1/models`` without sending a completion."""

        started = time.perf_counter()
        response = await self._send(
            "GET",
            self.models_url,
            api_key=api_key,
            timeout=timeout,
        )
        if not 200 <= response.status_code < 300:
            raise _error_for_response(response, api_key=api_key) from None
        payload = _response_payload(response)
        if not isinstance(payload, Mapping):
            raise _invalid_response(status_code=response.status_code) from None
        data = payload.get("data")
        model_available: bool | None
        if data is None:
            model_available = None
        elif isinstance(data, list):
            ids = {
                item.get("id")
                for item in data
                if isinstance(item, Mapping) and isinstance(item.get("id"), str)
            }
            model_available = self._model in ids
        else:
            raise _invalid_response(status_code=response.status_code) from None
        latency_ms = max(0, int((time.perf_counter() - started) * 1000))
        return ProviderConnection(
            ok=True,
            status_code=response.status_code,
            model_available=model_available,
            latency_ms=latency_ms,
        )

    async def check(
        self,
        *,
        api_key: str | None = None,
        timeout: float | None = None,
    ) -> ProviderConnection:
        """Alias for :meth:`check_connection`."""

        return await self.check_connection(api_key=api_key, timeout=timeout)

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------
    def _parse_completion(
        self,
        response: httpx.Response,
        *,
        started: float,
        response_format_used: bool,
        infrastructure_retries: int,
    ) -> ProviderCompletion:
        payload = _response_payload(response)
        if not isinstance(payload, Mapping):
            raise _invalid_response(
                status_code=response.status_code,
                infrastructure_retries=infrastructure_retries,
            ) from None
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise _invalid_response(
                status_code=response.status_code,
                infrastructure_retries=infrastructure_retries,
            ) from None
        first = choices[0]
        if not isinstance(first, Mapping):
            raise _invalid_response(
                status_code=response.status_code,
                infrastructure_retries=infrastructure_retries,
            ) from None
        message = first.get("message")
        if not isinstance(message, Mapping):
            raise _invalid_response(
                status_code=response.status_code,
                infrastructure_retries=infrastructure_retries,
            ) from None
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise _invalid_response(
                status_code=response.status_code,
                infrastructure_retries=infrastructure_retries,
            ) from None
        latency_ms = max(0, int((time.perf_counter() - started) * 1000))
        return ProviderCompletion(
            content=content,
            model=_safe_model_name(payload.get("model"), self._model),
            usage=_normalize_usage(payload.get("usage")),
            status_code=response.status_code,
            latency_ms=latency_ms,
            response_format_used=response_format_used,
            infrastructure_retries=infrastructure_retries,
        )


# Short alias for callers that treat provider URL validation as a standalone
# security helper.
validate_base_url = validate_provider_base_url


__all__ = [
    "OpenAICompatibleProvider",
    "ProviderCompletion",
    "ProviderConfigurationError",
    "ProviderConnection",
    "ProviderError",
    "ProviderErrorKind",
    "validate_base_url",
    "validate_provider_base_url",
]
