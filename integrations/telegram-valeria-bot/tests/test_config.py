from pathlib import Path

import pytest

from valeria_telegram.config import ConfigError, Settings, load_settings


def base_env(**overrides: str) -> dict[str, str]:
    return {
        "TELEGRAM_BOT_TOKEN": "123456:fake_example-token",
        "VALERIA_BASE_URL": "https://valeria.example/prefix/",
        **overrides,
    }


def test_defaults_and_secret_repr() -> None:
    settings = Settings.from_env(base_env())
    assert settings.valeria_url == "https://valeria.example/prefix"
    assert settings.database_path == Path("data/telegram-bot.db")
    assert settings.ca_bundle is None
    assert settings.request_timeout_seconds == 30
    assert settings.long_poll_seconds == 25
    assert settings.poll_interval_seconds == 3
    assert settings.allowed_user_ids == frozenset()
    assert settings.bot_token not in repr(settings)


def test_custom_settings_and_token_file_precedence(tmp_path: Path) -> None:
    token_path = tmp_path / "token"
    token_path.write_text("98765:secret-from-file\n")
    ca_path = tmp_path / "ca.pem"
    ca_path.write_text("custom CA")
    settings = load_settings(
        base_env(
            TELEGRAM_BOT_TOKEN_FILE=str(token_path),
            TELEGRAM_BOT_TOKEN="ignored",
            TELEGRAM_STATE_DB=str(tmp_path / "state.db"),
            TELEGRAM_LONG_POLL_SECONDS="50",
            TELEGRAM_POLL_INTERVAL_SECONDS="2.5",
            REQUEST_TIMEOUT_SECONDS="12.5",
            TLS_CA_BUNDLE=str(ca_path),
            TELEGRAM_ALLOWED_USER_IDS="123, 456,123",
        )
    )
    assert settings.bot_token == "98765:secret-from-file"
    assert settings.database_path == tmp_path / "state.db"
    assert settings.ca_bundle == ca_path
    assert settings.long_poll_seconds == 50
    assert settings.poll_interval_seconds == 2.5
    assert settings.request_timeout_seconds == 12.5
    assert settings.allowed_user_ids == frozenset({123, 456})


@pytest.mark.parametrize(
    "url",
    [
        "",
        "ftp://valeria.example",
        "https:///empty",
        "https://name:secret@valeria.example",
        "http://valeria.example:999999",
        "http://[broken",
        "https://valeria.example/?token=secret",
        "https://valeria.example/?",
        "https://valeria.example/#fragment",
        "https://valeria.example/#",
        "https://valeria.example/\n",
        " https://valeria.example/",
        "https://valeria.example/\x7f",
    ],
)
def test_invalid_base_url(url: str) -> None:
    with pytest.raises(ConfigError, match="VALERIA_BASE_URL"):
        load_settings(base_env(VALERIA_BASE_URL=url))


@pytest.mark.parametrize("token", ["", "secret", "123:secret with space", "123:секрет", "123:\x00"])
def test_invalid_token_does_not_leak(token: str) -> None:
    with pytest.raises(ConfigError) as failure:
        load_settings(base_env(TELEGRAM_BOT_TOKEN=token))
    if token:
        assert token not in str(failure.value)


def test_missing_token_file_does_not_fall_back_to_environment(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN_FILE"):
        load_settings(base_env(TELEGRAM_BOT_TOKEN_FILE=str(tmp_path / "missing")))


@pytest.mark.parametrize(
    "name,value",
    [
        ("REQUEST_TIMEOUT_SECONDS", "nan"),
        ("REQUEST_TIMEOUT_SECONDS", "inf"),
        ("REQUEST_TIMEOUT_SECONDS", "0"),
        ("TELEGRAM_POLL_INTERVAL_SECONDS", "-1"),
        ("TELEGRAM_POLL_INTERVAL_SECONDS", "nonsense"),
        ("TELEGRAM_LONG_POLL_SECONDS", "0"),
        ("TELEGRAM_LONG_POLL_SECONDS", "51"),
        ("TELEGRAM_LONG_POLL_SECONDS", "1.5"),
        ("TELEGRAM_ALLOWED_USER_IDS", "123,bad"),
        ("TELEGRAM_ALLOWED_USER_IDS", "-123"),
        ("TELEGRAM_ALLOWED_USER_IDS", "123,"),
        ("TELEGRAM_ALLOWED_USER_IDS", "0"),
        ("TELEGRAM_STATE_DB", ""),
    ],
)
def test_invalid_options(name: str, value: str) -> None:
    with pytest.raises(ConfigError, match=name):
        load_settings(base_env(**{name: value}))


def test_ca_bundle_must_be_a_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="TLS_CA_BUNDLE"):
        load_settings(base_env(TLS_CA_BUNDLE=str(tmp_path)))


def test_load_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "TELEGRAM_BOT_TOKEN_FILE",
        "TLS_CA_BUNDLE",
        "TELEGRAM_ALLOWED_USER_IDS",
        "TELEGRAM_LONG_POLL_SECONDS",
        "TELEGRAM_POLL_INTERVAL_SECONDS",
        "REQUEST_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in base_env().items():
        monkeypatch.setenv(name, value)
    assert load_settings().bot_token == "123456:fake_example-token"
