import logging
import os
from contextlib import contextmanager
from enum import Enum
from typing import Generator, Optional
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, Playwright, BrowserContext, Page, Error as PlaywrightError

from app.config import Config


def is_auth_redirect_url(url: str) -> bool:
    """Check if the given URL is an authentication redirect."""
    url_str = str(url).lower()
    parsed = urlparse(url_str)
    if "signin" in parsed.netloc or "login" in parsed.netloc:
        return True
    path_parts = [p for p in parsed.path.strip('/').split('/') if p]
    if "signin" in path_parts or "login" in path_parts:
        return True
    return False


class AuthState(str, Enum):
    """Represents the authentication state of the browser session."""
    AVAILABLE = "AVAILABLE"
    REQUIRED = "REQUIRED"
    FAILED = "FAILED"


class BrowserManager:
    """Manages the Playwright browser, persistent context, and page."""

    def __init__(self, config: Config, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self._playwright: Optional[Playwright] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

    def start(self) -> None:
        """Starts Playwright and the persistent Chromium browser context."""
        try:
            self.logger.info("Starting browser...")
            # Ensure the profile directory exists
            os.makedirs(self.config.browser_profile_path, exist_ok=True)
            self.logger.info(f"Using persistent profile path: {self.config.browser_profile_path}")

            self._playwright = sync_playwright().start()
            self._context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=self.config.browser_profile_path,
                headless=self.config.browser_headless,
                # Uses Playwright's bundled Chromium — no system channel required.
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage"
                ],
                ignore_default_args=["--enable-automation"],
                viewport={"width": 1280, "height": 720},
            )

            # Set default timeouts
            self._context.set_default_timeout(self.config.selector_timeout_ms)
            self._context.set_default_navigation_timeout(self.config.page_timeout_ms)

            # Usually launch_persistent_context provides a default page
            if self._context.pages:
                self._page = self._context.pages[0]
            else:
                self._page = self._context.new_page()

            self.logger.info("Browser launched successfully.")
        except Exception as e:
            self.logger.error(f"Failed to start browser: {e}", exc_info=True)
            self.stop()
            raise

    def stop(self) -> None:
        """Safely stops the browser context and Playwright."""
        try:
            if self._context:
                self.logger.info("Closing browser context...")
                self._context.close()
        except Exception as e:
            self.logger.error(f"Error closing browser context: {e}", exc_info=True)
        finally:
            self._context = None
            self._page = None

        try:
            if self._playwright:
                self.logger.info("Stopping Playwright...")
                self._playwright.stop()
        except Exception as e:
            self.logger.error(f"Error stopping Playwright: {e}", exc_info=True)
        finally:
            self._playwright = None

    @contextmanager
    def session(self) -> Generator[None, None, None]:
        """Context manager to ensure safe startup and shutdown."""
        self.start()
        try:
            yield
        finally:
            self.stop()

    def _is_authenticated_passively(self) -> bool:
        """
        Passively analyze the current DOM for authenticated state signals.
        Returns True if authenticated, False if unauthenticated or uncertain.
        """
        if not self._page:
            return False

        try:
            current_url = self._page.url.lower()
            # 1. Obvious sign-in pages mean not authenticated
            if is_auth_redirect_url(current_url):
                return False

            # 2. Check for explicit unauthenticated signals (visible)
            # Use small timeouts for these negative checks so we don't stall.
            if self._page.get_by_text("Sign In", exact=False).first.is_visible(timeout=500):
                return False
            if self._page.get_by_text("Log In", exact=False).first.is_visible(timeout=500):
                return False
        except PlaywrightError:
            pass

        # 3. Look for positive indicators of authentication.
        # These can be hidden in dropdowns, so we check if they are in the DOM at all (count > 0).
        try:
            positive_indicators = [
                self._page.get_by_text("Sign Out", exact=False).first,
                self._page.get_by_text("Log Out", exact=False).first,
                self._page.get_by_text("Write a post", exact=False).first,
                self._page.locator('img[alt*="profile" i]').first,
                self._page.locator('img[alt*="avatar" i]').first,
                self._page.locator('[aria-label*="profile" i]').first,
                self._page.locator('[aria-label*="account" i]').first,
                self._page.locator('[aria-label*="user menu" i]').first,
                self._page.locator('[aria-label="Notifications" i]').first,
                self._page.locator('[aria-label="Settings" i]').first,
            ]

            for indicator in positive_indicators:
                if indicator.count() > 0:
                    return True
        except PlaywrightError:
            pass

        return False

    def check_session(self) -> AuthState:
        """
        Navigates to the builder center URL and conservatively checks authentication state.
        Does not perform any engagement or attempt automated login.
        """
        if not self._page:
            self.logger.error("Page is not initialized.")
            return AuthState.FAILED

        try:
            self.logger.info(f"Navigating to {self.config.builder_center_url} to check session...")
            response = self._page.goto(self.config.builder_center_url)

            if not response:
                self.logger.error("Navigation failed (no response).")
                return AuthState.FAILED

            # Wait for the network to be idle to ensure redirects complete
            self._page.wait_for_load_state("networkidle", timeout=self.config.page_timeout_ms)

            self.logger.info(f"Current URL after navigation: {self._page.url}")

            if self._is_authenticated_passively():
                self.logger.info("Authentication is available.")
                return AuthState.AVAILABLE

            self.logger.warning("Could not definitively verify authentication state. Assuming REQUIRED for safety.")
            return AuthState.REQUIRED

        except PlaywrightError as e:
            self.logger.error(f"Navigation or session check failed: {e}", exc_info=True)
            return AuthState.FAILED
        except Exception as e:
            self.logger.error(f"Unexpected error during session check: {e}", exc_info=True)
            return AuthState.FAILED

    def wait_for_manual_login(self, timeout_ms: int = 300000) -> bool:
        """
        Wait for the user to manually log in via the browser UI.
        This passively polls the page state without navigating, waiting for positive
        authentication signals to appear in the DOM.

        Args:
            timeout_ms: Maximum time to wait in milliseconds (default 5 minutes).

        Returns:
            True if authentication succeeds, False otherwise.
        """
        if not self._page:
            return False

        self.logger.info(
            "Waiting up to 5 minutes for manual Google authentication. "
            "Please log in using the opened browser window..."
        )

        import time
        start_time = time.time()
        timeout_s = timeout_ms / 1000.0

        while time.time() - start_time < timeout_s:
            try:
                # Ensure the page hasn't crashed and is not just a blank tab
                if self._page.url != "about:blank":
                    if self._is_authenticated_passively():
                        self.logger.info("Manual authentication detected successfully!")
                        return True
            except PlaywrightError:
                pass

            # Wait briefly before checking again to avoid CPU spin
            self._page.wait_for_timeout(2000)

        self.logger.warning("Timeout or error waiting for manual authentication.")
        return False
