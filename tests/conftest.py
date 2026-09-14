"""Shared test fixtures."""

import os
import pytest
from unittest.mock import patch


@pytest.fixture()
def clean_env():
    """Provide a clean environment with only the required config vars.

    Mocks load_dotenv to prevent reading any .env file, and patches
    os.environ to contain only the vars we provide.
    """
    base_env = {
        "TELEGRAM_BOT_TOKEN": "test-token-123",
        "TELEGRAM_CHAT_ID": "-1001234567890",
    }
    with patch("app.config.load_dotenv"):
        with patch.dict(os.environ, base_env, clear=True):
            yield base_env


@pytest.fixture()
def empty_env():
    """Provide a completely empty environment (no vars set).

    Mocks load_dotenv to prevent reading any .env file.
    """
    with patch("app.config.load_dotenv"):
        with patch.dict(os.environ, {}, clear=True):
            yield
