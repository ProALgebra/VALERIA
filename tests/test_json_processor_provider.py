from __future__ import annotations

import json
import logging

import httpx
import pytest

from opencode_web.json_processor.provider import (
    OpenAICompatibleProvider,
    ProviderConfigurationError,
    ProviderError,
    ProviderErrorKind,
    validate_provider_base_url,
)
from opencode_web.json_processor.settings import JsonProcessorSettings


def make_settings(**overrides) -> JsonProcessorSettings:
    data = {
        "base_url": "http://provider.test",
        "model": "repair-model",
    }
    data.update(overrides)
    return JsonProcessorSettings.from_dict(data)


def make_provider(handler, *, allow_private_networks: bool = False, **overrides):
    return OpenAICompatibleProvider(
        make_settings(**overrides),
        allow_private_networks=allow_private_networks,
        transport=httpx.MockTransport(handler),
    )


def completion_body(
    content: object = '{"ok": true}',
    *,
    model: object = "repair-model",
    usage: object = None,
    include_usage: bool = False,
) -> dict:
    body: dict = {
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
        "model": model,
    }
    if include_usage:
        body["usage"] = usage
    return body


def completion_response(**kwargs) -> httpx.Response:
    return httpx.Response(200, json=completion_body(**kwargs))


async def test_complete_parses_content_payload_and_usage() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return completion_response(
            usage={
                "prompt_tokens": 12,
                "completion_tokens": 4,
                "total_tokens": 16,
            },
            include_usage=True,
        )

    provider = make_provider(handler)
    try:
        result = await provider.complete(
            [
                {"role": "system", "content": "Return JSON."},
                {"role": "user", "content": "repair this"},
            ],
            api_key="sk-test-key",
        )
    finally:
        await provider.aclose()

    assert result.content == '{"ok": true}'
    assert result.model == "repair-model"
    assert result.usage == {
        "prompt_tokens": 12,
        "completion_tokens": 4,
        "total_tokens": 16,
    }
    assert result.status_code == 200
    assert result.response_format_used is True
    assert result.infrastructure_retries == 0
    assert result.infrastructure_retry_used is False
    assert result.response_format_fallback_used is False
    assert result.counts_as_repair_attempt is False

    assert len(captured) == 1
    request = captured[0]
    assert request.method == "POST"
    assert str(request.url) == "http://provider.test/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer sk-test-key"
    payload = json.loads(request.content)
    assert payload["model"] == "repair-model"
    assert payload["messages"] == [
        {"role": "system", "content": "Return JSON."},
        {"role": "user", "content": "repair this"},
    ]
    assert payload["max_tokens"] == 4096
    assert payload["temperature"] == 0.0
    assert payload["stream"] is False
    assert payload["response_format"] == {"type": "json_object"}


async def test_invalid_json_content_is_returned_verbatim_for_the_parser() -> None:
    provider = make_provider(lambda request: completion_response(content="not json"))
    try:
        result = await provider.complete("repair", api_key=None)
    finally:
        await provider.aclose()

    assert result.content == "not json"


@pytest.mark.parametrize(
    "body",
    [
        completion_body(content=""),
        completion_body(content="   "),
        completion_body(content=123),
        {
            "choices": [{"message": {"role": "assistant"}}],
            "model": "repair-model",
        },
        {"choices": [], "model": "repair-model"},
        {"choices": [{}], "model": "repair-model"},
        {"model": "repair-model"},
    ],
)
async def test_missing_empty_or_non_string_content_is_typed_error(body: dict) -> None:
    provider = make_provider(lambda request: httpx.Response(200, json=body))
    try:
        with pytest.raises(ProviderError) as info:
            await provider.complete("repair")
    finally:
        await provider.aclose()

    error = info.value
    assert error.kind is ProviderErrorKind.INVALID_RESPONSE
    assert error.status_code == 200
    assert error.processor_code.value == "PROVIDER_ERROR"


async def test_non_json_success_body_is_invalid_response() -> None:
    provider = make_provider(lambda request: httpx.Response(200, text="not-json"))
    try:
        with pytest.raises(ProviderError) as info:
            await provider.complete("repair")
    finally:
        await provider.aclose()

    assert info.value.kind is ProviderErrorKind.INVALID_RESPONSE


@pytest.mark.parametrize(
    ("status", "kind", "processor_code", "retryable"),
    [
        (401, ProviderErrorKind.AUTHENTICATION, "LLM_UNAVAILABLE", False),
        (403, ProviderErrorKind.FORBIDDEN, "LLM_UNAVAILABLE", False),
        (404, ProviderErrorKind.NOT_FOUND, "LLM_UNAVAILABLE", False),
        (429, ProviderErrorKind.RATE_LIMITED, "PROVIDER_ERROR", True),
        (500, ProviderErrorKind.SERVER_ERROR, "PROVIDER_ERROR", True),
        (503, ProviderErrorKind.SERVER_ERROR, "PROVIDER_ERROR", True),
        (502, ProviderErrorKind.SERVER_ERROR, "PROVIDER_ERROR", True),
        (400, ProviderErrorKind.BAD_REQUEST, "PROVIDER_ERROR", False),
    ],
)
async def test_http_status_error_mapping(
    status: int,
    kind: ProviderErrorKind,
    processor_code: str,
    retryable: bool,
) -> None:
    provider = make_provider(
        lambda request: httpx.Response(
            status,
            json={"error": {"code": "provider_safe_code"}},
        )
    )
    try:
        with pytest.raises(ProviderError) as info:
            await provider.complete("repair")
    finally:
        await provider.aclose()

    error = info.value
    assert error.kind is kind
    assert error.code == kind.value
    assert error.status_code == status
    assert error.provider_code == "provider_safe_code"
    assert error.processor_code.value == processor_code
    assert error.retryable is retryable
    assert error.infrastructure_retries == 0
    assert error.to_dict()["status_code"] == status
    assert error.to_dict()["code"] == kind.value


async def test_timeout_is_typed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    provider = make_provider(handler)
    try:
        with pytest.raises(ProviderError) as info:
            await provider.complete("repair")
    finally:
        await provider.aclose()

    error = info.value
    assert error.kind is ProviderErrorKind.TIMEOUT
    assert error.status_code is None
    assert error.processor_code.value == "TIMEOUT"
    assert error.retryable is True


async def test_network_error_is_typed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    provider = make_provider(handler)
    try:
        with pytest.raises(ProviderError) as info:
            await provider.complete("repair")
    finally:
        await provider.aclose()

    error = info.value
    assert error.kind is ProviderErrorKind.NETWORK
    assert error.status_code is None
    assert error.processor_code.value == "LLM_UNAVAILABLE"
    assert error.retryable is True


async def test_unsupported_response_format_is_retried_once_without_it() -> None:
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        payloads.append(payload)
        if "response_format" in payload:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "code": "unsupported_parameter",
                        "message": "Unsupported parameter: response_format",
                    }
                },
            )
        return completion_response(content='{"ok": true}')

    provider = make_provider(handler)
    try:
        result = await provider.complete("repair")
    finally:
        await provider.aclose()

    assert len(payloads) == 2
    assert payloads[0]["response_format"] == {"type": "json_object"}
    assert "response_format" not in payloads[1]
    assert result.content == '{"ok": true}'
    assert result.response_format_used is False
    assert result.infrastructure_retries == 1
    assert result.infrastructure_retry_used is True
    assert result.response_format_fallback_used is True
    assert result.counts_as_repair_attempt is False


async def test_unrelated_400_does_not_retry_response_format() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, json={"error": {"message": "bad request"}})

    provider = make_provider(handler)
    try:
        with pytest.raises(ProviderError) as info:
            await provider.complete("repair")
    finally:
        await provider.aclose()

    assert calls == 1
    assert info.value.kind is ProviderErrorKind.BAD_REQUEST
    assert info.value.infrastructure_retries == 0


async def test_failed_response_format_retry_reports_infrastructure_retry() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                400,
                json={"error": {"message": "response_format is not supported"}},
            )
        return httpx.Response(503, json={"error": {"code": "overloaded"}})

    provider = make_provider(handler)
    try:
        with pytest.raises(ProviderError) as info:
            await provider.complete("repair")
    finally:
        await provider.aclose()

    assert calls == 2
    assert info.value.kind is ProviderErrorKind.SERVER_ERROR
    assert info.value.status_code == 503
    assert info.value.infrastructure_retries == 1


async def test_response_format_can_be_disabled() -> None:
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return completion_response()

    provider = make_provider(handler)
    try:
        result = await provider.complete("repair", response_format=False)
    finally:
        await provider.aclose()

    assert "response_format" not in payloads[0]
    assert result.response_format_used is False
    assert result.infrastructure_retries == 0


async def test_missing_usage_is_optional_and_malformed_usage_is_ignored() -> None:
    provider = make_provider(lambda request: completion_response())
    try:
        result = await provider.complete("repair")
    finally:
        await provider.aclose()
    assert result.usage is None

    provider = make_provider(
        lambda request: completion_response(
            usage={"prompt_tokens": "many", "completion_tokens": 2},
            include_usage=True,
        )
    )
    try:
        result = await provider.complete("repair")
    finally:
        await provider.aclose()
    assert result.usage == {"completion_tokens": 2}


async def test_api_key_is_never_in_error_or_log_metadata(caplog) -> None:
    secret = "sk-live-never-log-this"
    caplog.set_level(logging.DEBUG)
    provider = make_provider(
        lambda request: httpx.Response(
            401,
            json={"error": {"code": secret, "message": f"bad key {secret}"}},
        )
    )
    try:
        with pytest.raises(ProviderError) as info:
            await provider.complete("repair", api_key=secret)
    finally:
        await provider.aclose()

    error = info.value
    assert secret not in str(error)
    assert secret not in repr(error)
    assert secret not in json.dumps(error.to_dict())
    assert secret not in caplog.text
    assert error.provider_code is None
    assert "authorization" not in json.dumps(error.to_dict()).lower()


@pytest.mark.parametrize(
    "base_url",
    [
        "ftp://provider.test",
        "provider.test",
        "http://user:pass@provider.test",
        "http://provider.test/v1?token=x",
        "http://provider.test/v1#fragment",
        "http://provider.test:99999",
        "http://169.254.169.254/latest/meta-data",
        "http://169.254.170.2/v2/credentials",
        "http://100.100.100.200/latest/meta-data",
        "http://2130706433",
        "http://127.1",
        "http://0177.0.0.1",
        "http://0x7f000001",
        "http://[fd00:ec2::254]",
        "http://[fe80::1]",
        "http://metadata.google.internal",
        "http://0.0.0.0",
        "http://127.0.0.1:11434",
        "http://[::1]:11434",
    ],
)
def test_unsafe_base_urls_are_rejected(base_url: str) -> None:
    with pytest.raises(ProviderConfigurationError):
        OpenAICompatibleProvider(
            JsonProcessorSettings(base_url=base_url, model="repair-model")
        )


@pytest.mark.parametrize(
    "base_url",
    [
        "http://127.0.0.1:11434",
        "http://localhost:11434",
        "http://192.168.1.20:8000",
        "http://[::1]:11434",
        "http://10.0.0.5",
    ],
)
async def test_private_and_loopback_are_allowed_with_explicit_opt_in(
    base_url: str,
) -> None:
    provider = OpenAICompatibleProvider(
        JsonProcessorSettings(base_url=base_url, model="repair-model"),
        allow_private_networks=True,
    )
    try:
        assert provider.base_url == base_url
    finally:
        await provider.aclose()


@pytest.mark.parametrize(
    "base_url",
    [
        "http://169.254.169.254",
        "http://100.100.100.200",
        "http://[fd00:ec2::254]",
        "http://metadata.google.internal",
    ],
)
def test_metadata_targets_remain_denied_with_private_opt_in(base_url: str) -> None:
    with pytest.raises(ProviderConfigurationError):
        OpenAICompatibleProvider(
            JsonProcessorSettings(base_url=base_url, model="repair-model"),
            allow_private_networks=True,
        )


def test_base_url_is_normalized_and_v1_is_not_duplicated() -> None:
    assert (
        validate_provider_base_url("http://provider.test/")
        == "http://provider.test"
    )
    assert (
        validate_provider_base_url("http://203.0.113.10")
        == "http://203.0.113.10"
    )


async def test_base_url_v1_suffix_does_not_create_a_duplicate_path() -> None:
    provider = OpenAICompatibleProvider(
        JsonProcessorSettings(base_url="http://provider.test/v1/", model="repair-model")
    )
    try:
        assert provider.chat_completions_url == "http://provider.test/v1/chat/completions"
        assert provider.models_url == "http://provider.test/v1/models"
    finally:
        await provider.aclose()


async def test_check_connection_models_and_availability() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == "http://provider.test/v1/models"
        return httpx.Response(
            200,
            json={"data": [{"id": "other"}, {"id": "repair-model"}]},
        )

    provider = make_provider(handler)
    try:
        result = await provider.check_connection(api_key="sk-check")
    finally:
        await provider.aclose()

    assert result.ok is True
    assert result.status_code == 200
    assert result.model_available is True


async def test_check_connection_handles_missing_model_list_and_errors() -> None:
    provider = make_provider(lambda request: httpx.Response(200, json={}))
    try:
        result = await provider.check_connection()
    finally:
        await provider.aclose()
    assert result.model_available is None

    provider = make_provider(lambda request: httpx.Response(404, json={}))
    try:
        with pytest.raises(ProviderError) as info:
            await provider.check_connection()
    finally:
        await provider.aclose()
    assert info.value.kind is ProviderErrorKind.NOT_FOUND


async def test_settings_are_copied_without_storing_api_key() -> None:
    provider = make_provider(lambda request: completion_response())
    try:
        await provider.complete("repair", api_key="sk-call-only")
        serialized = repr(provider.__dict__)
    finally:
        await provider.aclose()

    assert "sk-call-only" not in serialized
    assert provider.model == "repair-model"


async def test_redirects_and_environment_proxies_are_disabled() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            302,
            headers={"location": "http://169.254.169.254/latest/meta-data"},
        )

    provider = make_provider(handler)
    try:
        assert provider._client.follow_redirects is False
        assert provider._client.trust_env is False
        with pytest.raises(ProviderError) as info:
            await provider.complete("repair")
    finally:
        await provider.aclose()

    assert info.value.kind is ProviderErrorKind.PROVIDER_ERROR
    assert info.value.status_code == 302
    assert len(calls) == 1
    assert "169.254.169.254" not in str(info.value)
