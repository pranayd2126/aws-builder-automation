import logging
import pytest
from unittest.mock import patch, MagicMock

from playwright.sync_api import Error as PlaywrightError

from app.browser import BrowserManager, AuthState
from app.config import Config


@pytest.fixture
def test_config(tmp_path):
    """Provides a safe Config object with temporary paths for tests."""
    return Config(
        telegram_bot_token="test_token",
        telegram_chat_id="test_chat",
        browser_profile_path=str(tmp_path / "test-browser-profile"),
        log_dir=str(tmp_path / "logs"),
        database_path=str(tmp_path / "test.db"),
        browser_headless=True,
        builder_center_url="http://example.com/mock-builder",
        page_timeout_ms=5000,
        selector_timeout_ms=2000,
    )


@pytest.fixture
def logger():
    return logging.getLogger("test_browser")


def test_browser_launch_and_cleanup(test_config, logger):
    """Test that Chromium launches successfully and cleans up properly."""
    manager = BrowserManager(test_config, logger)

    assert manager._playwright is None
    assert manager._context is None
    assert manager._page is None

    with manager.session():
        assert manager._playwright is not None
        assert manager._context is not None
        assert manager._page is not None
        assert not manager._page.is_closed()

    # After session exits
    assert manager._playwright is None
    assert manager._context is None
    assert manager._page is None


def test_authentication_required_due_to_login_url(test_config, logger):
    """Test AuthState.REQUIRED is returned when redirected to login URL."""
    manager = BrowserManager(test_config, logger)
    with patch.object(manager, 'start'), patch.object(manager, 'stop'):
        manager._page = MagicMock()
        manager._page.goto.return_value = MagicMock()
        # Simulate being redirected to a signin page
        manager._page.url = "https://signin.example.com"

        state = manager.check_session()
        assert state == AuthState.REQUIRED


def test_authentication_required_due_to_sign_in_text(test_config, logger):
    """Test AuthState.REQUIRED is returned when 'Sign In' text is present."""
    manager = BrowserManager(test_config, logger)
    with patch.object(manager, 'start'), patch.object(manager, 'stop'):
        manager._page = MagicMock()
        manager._page.goto.return_value = MagicMock()
        manager._page.url = "http://example.com/mock-builder"

        # Mock get_by_text: "Sign In" visible, others not
        def get_by_text_side_effect(text, **kwargs):
            m = MagicMock()
            m.first = m
            if text == "Sign In":
                m.is_visible.return_value = True
            else:
                m.is_visible.return_value = False
            return m

        manager._page.get_by_text.side_effect = get_by_text_side_effect

        state = manager.check_session()
        assert state == AuthState.REQUIRED


def test_authentication_available_due_to_sign_out_text(test_config, logger):
    """Test AuthState.AVAILABLE is returned when 'Sign Out' text is present."""
    manager = BrowserManager(test_config, logger)
    with patch.object(manager, 'start'), patch.object(manager, 'stop'):
        manager._page = MagicMock()
        manager._page.goto.return_value = MagicMock()
        manager._page.url = "http://example.com/mock-builder"

        def get_by_text_side_effect(text, **kwargs):
            m = MagicMock()
            m.first = m
            if text == "Sign Out":
                m.is_visible.return_value = True
            else:
                m.is_visible.return_value = False
            return m

        manager._page.get_by_text.side_effect = get_by_text_side_effect

        state = manager.check_session()
        assert state == AuthState.AVAILABLE


def test_navigation_failure_returns_failed(test_config, logger):
    """Test AuthState.FAILED is returned on navigation exception."""
    manager = BrowserManager(test_config, logger)
    with patch.object(manager, 'start'), patch.object(manager, 'stop'):
        manager._page = MagicMock()
        manager._page.goto.side_effect = PlaywrightError("Navigation timeout")

        state = manager.check_session()
        assert state == AuthState.FAILED


def test_navigation_returns_none(test_config, logger):
    """Test AuthState.FAILED is returned if goto returns None."""
    manager = BrowserManager(test_config, logger)
    with patch.object(manager, 'start'), patch.object(manager, 'stop'):
        manager._page = MagicMock()
        manager._page.goto.return_value = None

        state = manager.check_session()
        assert state == AuthState.FAILED


def test_browser_startup_failure_handling(test_config, logger):
    """Test that a startup failure raises exception and cleans up."""
    manager = BrowserManager(test_config, logger)

    with patch("app.browser.sync_playwright") as mock_pw:
        mock_pw.return_value.start.side_effect = Exception("Failed to start")

        with pytest.raises(Exception, match="Failed to start"):
            manager.start()

        assert manager._playwright is None
        assert manager._context is None


def test_closed_page_handled_safely(test_config, logger):
    """Test check_session handles closed page safely."""
    manager = BrowserManager(test_config, logger)
    # Don't initialize page
    assert manager._page is None

    state = manager.check_session()
    assert state == AuthState.FAILED


def test_wait_for_manual_login_success(test_config, logger):
    """Test wait_for_manual_login returns True on success."""
    manager = BrowserManager(test_config, logger)
    manager._page = MagicMock()

    # Mock locator to return successfully
    mock_locator = MagicMock()
    manager._page.get_by_text.return_value.first = mock_locator

    result = manager.wait_for_manual_login(timeout_ms=10)
    assert result is True
    mock_locator.wait_for.assert_called_once_with(state="visible", timeout=10)


def test_wait_for_manual_login_timeout(test_config, logger):
    """Test wait_for_manual_login returns False on timeout."""
    manager = BrowserManager(test_config, logger)
    manager._page = MagicMock()

    # Mock locator to raise PlaywrightError
    mock_locator = MagicMock()
    mock_locator.wait_for.side_effect = PlaywrightError("Timeout")
    manager._page.get_by_text.return_value.first = mock_locator

    result = manager.wait_for_manual_login(timeout_ms=10)
    assert result is False


def test_wait_for_manual_login_no_page(test_config, logger):
    """Test wait_for_manual_login returns False if no page exists."""
    manager = BrowserManager(test_config, logger)
    assert manager._page is None
    assert manager.wait_for_manual_login() is False
