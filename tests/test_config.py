"""Tests for app.config module."""

import os
import pytest
from unittest.mock import patch

from app.config import Config, ConfigError, APPROVED_COMMENTS


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _load_with_env(overrides: dict | None = None) -> Config:
    """Load Config with mocked dotenv and controlled env vars."""
    env = {
        "TELEGRAM_BOT_TOKEN": "test-token-123",
        "TELEGRAM_CHAT_ID": "-1001234567890",
    }
    if overrides:
        env.update(overrides)
    with patch("app.config.load_dotenv"):
        with patch.dict(os.environ, env, clear=True):
            return Config.load()


def _load_with_env_expecting_error(
    overrides: dict | None = None,
) -> ConfigError:
    """Load Config expecting a ConfigError, return the exception."""
    with pytest.raises(ConfigError) as exc_info:
        _load_with_env(overrides)
    return exc_info.value


# ---------------------------------------------------------------------------
# Required fields
# ---------------------------------------------------------------------------

class TestRequiredFields:

    def test_loads_with_required_vars_only(self):
        config = _load_with_env()
        assert config.telegram_bot_token == "test-token-123"
        assert config.telegram_chat_id == "-1001234567890"

    def test_missing_telegram_bot_token(self):
        env = {"TELEGRAM_CHAT_ID": "-100123"}
        with patch("app.config.load_dotenv"):
            with patch.dict(os.environ, env, clear=True):
                err = _load_with_env_expecting_error.__wrapped__ if hasattr(
                    _load_with_env_expecting_error, '__wrapped__'
                ) else None
                with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
                    Config.load()

    def test_missing_telegram_chat_id(self):
        env = {"TELEGRAM_BOT_TOKEN": "tok"}
        with patch("app.config.load_dotenv"):
            with patch.dict(os.environ, env, clear=True):
                with pytest.raises(ConfigError, match="TELEGRAM_CHAT_ID"):
                    Config.load()

    def test_missing_both_required_reports_both(self):
        with patch("app.config.load_dotenv"):
            with patch.dict(os.environ, {}, clear=True):
                with pytest.raises(ConfigError) as exc_info:
                    Config.load()
                msg = str(exc_info.value)
                assert "TELEGRAM_BOT_TOKEN" in msg
                assert "TELEGRAM_CHAT_ID" in msg

    def test_empty_token_treated_as_missing(self):
        with patch("app.config.load_dotenv"):
            with patch.dict(
                os.environ,
                {"TELEGRAM_BOT_TOKEN": "   ", "TELEGRAM_CHAT_ID": "-123"},
                clear=True,
            ):
                with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
                    Config.load()


# ---------------------------------------------------------------------------
# DRY_RUN
# ---------------------------------------------------------------------------

class TestDryRun:

    def test_defaults_to_true(self):
        config = _load_with_env()
        assert config.dry_run is True

    def test_empty_value_defaults_to_true(self):
        assert _load_with_env({"DRY_RUN": ""}).dry_run is True

    def test_whitespace_only_defaults_to_true(self):
        assert _load_with_env({"DRY_RUN": "   "}).dry_run is True

    def test_explicit_true(self):
        assert _load_with_env({"DRY_RUN": "true"}).dry_run is True

    def test_explicit_false(self):
        assert _load_with_env({"DRY_RUN": "false"}).dry_run is False

    def test_accepts_yes(self):
        assert _load_with_env({"DRY_RUN": "yes"}).dry_run is True

    def test_accepts_no(self):
        assert _load_with_env({"DRY_RUN": "no"}).dry_run is False

    def test_accepts_1(self):
        assert _load_with_env({"DRY_RUN": "1"}).dry_run is True

    def test_accepts_0(self):
        assert _load_with_env({"DRY_RUN": "0"}).dry_run is False

    def test_case_insensitive(self):
        assert _load_with_env({"DRY_RUN": "TRUE"}).dry_run is True
        assert _load_with_env({"DRY_RUN": "False"}).dry_run is False

    def test_invalid_value_raises(self):
        with pytest.raises(ConfigError, match="DRY_RUN"):
            _load_with_env({"DRY_RUN": "maybe"})


# ---------------------------------------------------------------------------
# Default values
# ---------------------------------------------------------------------------

class TestDefaults:

    def test_builder_center_url(self):
        assert _load_with_env().builder_center_url == "https://builder.aws.com"

    def test_database_path(self):
        assert _load_with_env().database_path == "data/automation.db"

    def test_browser_profile_path(self):
        assert _load_with_env().browser_profile_path == "browser-profile"

    def test_log_dir(self):
        assert _load_with_env().log_dir == "logs"

    def test_log_level(self):
        assert _load_with_env().log_level == "INFO"

    def test_page_timeout_ms(self):
        assert _load_with_env().page_timeout_ms == 30000

    def test_selector_timeout_ms(self):
        assert _load_with_env().selector_timeout_ms == 15000

    def test_action_delay_min_s(self):
        assert _load_with_env().action_delay_min_s == 2.0

    def test_action_delay_max_s(self):
        assert _load_with_env().action_delay_max_s == 5.0

    def test_timezone_is_asia_kolkata(self):
        assert _load_with_env().timezone == "Asia/Kolkata"


# ---------------------------------------------------------------------------
# Custom values
# ---------------------------------------------------------------------------

class TestCustomValues:

    def test_custom_builder_center_url(self):
        config = _load_with_env({"BUILDER_CENTER_URL": "https://custom.example.com"})
        assert config.builder_center_url == "https://custom.example.com"

    def test_custom_database_path(self):
        config = _load_with_env({"DATABASE_PATH": "custom/db.sqlite"})
        assert config.database_path == "custom/db.sqlite"

    def test_custom_log_level(self):
        config = _load_with_env({"LOG_LEVEL": "DEBUG"})
        assert config.log_level == "DEBUG"

    def test_custom_timeouts(self):
        config = _load_with_env({
            "PAGE_TIMEOUT_MS": "60000",
            "SELECTOR_TIMEOUT_MS": "20000",
        })
        assert config.page_timeout_ms == 60000
        assert config.selector_timeout_ms == 20000

    def test_custom_delays(self):
        config = _load_with_env({
            "ACTION_DELAY_MIN_S": "1.5",
            "ACTION_DELAY_MAX_S": "3.5",
        })
        assert config.action_delay_min_s == 1.5
        assert config.action_delay_max_s == 3.5


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------

class TestValidation:

    def test_invalid_log_level(self):
        with pytest.raises(ConfigError, match="LOG_LEVEL"):
            _load_with_env({"LOG_LEVEL": "VERBOSE"})

    def test_log_level_case_insensitive(self):
        config = _load_with_env({"LOG_LEVEL": "debug"})
        assert config.log_level == "DEBUG"

    def test_non_integer_timeout(self):
        with pytest.raises(ConfigError, match="PAGE_TIMEOUT_MS"):
            _load_with_env({"PAGE_TIMEOUT_MS": "abc"})

    def test_negative_timeout(self):
        with pytest.raises(ConfigError, match="PAGE_TIMEOUT_MS"):
            _load_with_env({"PAGE_TIMEOUT_MS": "-100"})

    def test_zero_timeout(self):
        with pytest.raises(ConfigError, match="PAGE_TIMEOUT_MS"):
            _load_with_env({"PAGE_TIMEOUT_MS": "0"})

    def test_non_numeric_delay(self):
        with pytest.raises(ConfigError, match="ACTION_DELAY_MIN_S"):
            _load_with_env({"ACTION_DELAY_MIN_S": "slow"})

    def test_negative_delay(self):
        with pytest.raises(ConfigError, match="ACTION_DELAY_MIN_S"):
            _load_with_env({"ACTION_DELAY_MIN_S": "-1"})

    def test_min_delay_greater_than_max(self):
        with pytest.raises(ConfigError, match="ACTION_DELAY_MIN_S"):
            _load_with_env({
                "ACTION_DELAY_MIN_S": "10.0",
                "ACTION_DELAY_MAX_S": "5.0",
            })

    def test_multiple_errors_reported_together(self):
        with pytest.raises(ConfigError) as exc_info:
            _load_with_env({
                "PAGE_TIMEOUT_MS": "abc",
                "LOG_LEVEL": "VERBOSE",
            })
        msg = str(exc_info.value)
        assert "PAGE_TIMEOUT_MS" in msg
        assert "LOG_LEVEL" in msg


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------

class TestImmutability:

    def test_config_is_frozen(self):
        config = _load_with_env()
        with pytest.raises(AttributeError):
            config.dry_run = False  # type: ignore[misc]

    def test_config_is_frozen_token(self):
        config = _load_with_env()
        with pytest.raises(AttributeError):
            config.telegram_bot_token = "new"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Whitespace handling
# ---------------------------------------------------------------------------

class TestWhitespace:

    def test_token_stripped(self):
        config = _load_with_env({"TELEGRAM_BOT_TOKEN": "  tok  "})
        assert config.telegram_bot_token == "tok"

    def test_chat_id_stripped(self):
        config = _load_with_env({"TELEGRAM_CHAT_ID": "  -123  "})
        assert config.telegram_chat_id == "-123"

    def test_dry_run_stripped(self):
        config = _load_with_env({"DRY_RUN": "  false  "})
        assert config.dry_run is False


# ---------------------------------------------------------------------------
# Approved comments placeholder
# ---------------------------------------------------------------------------

class TestApprovedComments:

    def test_approved_comments_is_empty_list(self):
        """Comments will be supplied before Phase 8."""
        assert APPROVED_COMMENTS == []
        assert isinstance(APPROVED_COMMENTS, list)
