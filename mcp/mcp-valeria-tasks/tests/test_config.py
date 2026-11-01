from dataclasses import FrozenInstanceError

import pytest

from config import ConfigError, Settings


def environment(**overrides):
    return {
        "VALERIA_URL": "https://valeria.example/team",
        "VALERIA_LOGIN": "operator",
        "VALERIA_PASSWORD": "environment-secret",
        **overrides,
    }


def test_environment_loads_credentials_without_exposing_password():
    settings = Settings.from_env(environment())

    assert settings.url.rstrip("/") == "https://valeria.example/team"
    assert settings.login == "operator"
    assert settings.password == "environment-secret"
    assert settings.request_timeout_seconds == 30
    assert settings.ca_bundle is None
    assert settings.password not in repr(settings)
    with pytest.raises(FrozenInstanceError):
        settings.password = "replacement"


def test_password_file_takes_precedence_and_removes_only_line_ending(tmp_path):
    password_file = tmp_path / "password"
    password_file.write_text("  file-secret  \n", encoding="utf-8")

    settings = Settings.from_env(environment(VALERIA_PASSWORD_FILE=str(password_file)))

    assert settings.password == "  file-secret  "
    assert settings.password not in repr(settings)


@pytest.mark.parametrize("content", ["", "\n", "\r\n"])
def test_empty_password_file_is_rejected_without_falling_back(tmp_path, content):
    password_file = tmp_path / "password"
    password_file.write_text(content, encoding="utf-8")

    with pytest.raises(ConfigError):
        Settings.from_env(environment(VALERIA_PASSWORD_FILE=str(password_file)))


def test_missing_password_file_is_reported_without_leaking_environment_password(tmp_path):
    with pytest.raises(ConfigError) as caught:
        Settings.from_env(environment(VALERIA_PASSWORD_FILE=str(tmp_path / "absent")))

    assert "environment-secret" not in str(caught.value)


def test_custom_ca_and_timeout_are_loaded(tmp_path):
    ca_bundle = tmp_path / "internal-ca.pem"
    ca_bundle.write_text("certificate", encoding="utf-8")

    settings = Settings.from_env(
        environment(
            VALERIA_CA_BUNDLE=str(ca_bundle),
            VALERIA_REQUEST_TIMEOUT_SECONDS="12.5",
        )
    )

    assert settings.ca_bundle == ca_bundle
    assert settings.request_timeout_seconds == 12.5


@pytest.mark.parametrize(
    "name",
    ["VALERIA_URL", "VALERIA_LOGIN", "VALERIA_PASSWORD"],
)
def test_required_environment_variables_are_rejected_when_absent(name):
    env = environment()
    del env[name]

    with pytest.raises(ConfigError):
        Settings.from_env(env)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "valeria.example",
        "ftp://valeria.example",
        "https:///missing-host",
        "https://operator:private-password@valeria.example",
        "https://valeria.example/?token=private-token",
        "https://valeria.example/#private-fragment",
    ],
)
def test_invalid_or_credential_bearing_urls_are_rejected_without_secrets(url):
    with pytest.raises(ConfigError) as caught:
        Settings.from_env(environment(VALERIA_URL=url))

    assert "private-password" not in str(caught.value)
    assert "private-token" not in str(caught.value)
    assert "private-fragment" not in str(caught.value)


@pytest.mark.parametrize("timeout", ["0", "-1", "nan", "inf", "-inf", "abc"])
def test_timeout_must_be_finite_and_positive(timeout):
    with pytest.raises(ConfigError):
        Settings.from_env(environment(VALERIA_REQUEST_TIMEOUT_SECONDS=timeout))


def test_missing_custom_ca_bundle_is_rejected(tmp_path):
    with pytest.raises(ConfigError):
        Settings.from_env(environment(VALERIA_CA_BUNDLE=str(tmp_path / "absent.pem")))
