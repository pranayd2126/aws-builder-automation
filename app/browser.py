import logging
import os
from contextlib import contextmanager
from enum import Enum
from typing import Generator, Optional

from playwright.sync_api import sync_playwright, Playwright, BrowserContext, Page, Error as PlaywrightError

from app.config import Config


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

            current_url = self._page.url
            self.logger.info(f"Current URL after navigation: {current_url}")

            # Conservative check:
            # If we were redirected to a sign-in or login page, auth is required.
            if "signin" in current_url.lower() or "login" in current_url.lower():
                self.logger.info("Redirected to a login page. Authentication is required.")
                return AuthState.REQUIRED

            # Alternatively, if there is a 'Sign In' or 'Log In' link explicitly visible on the page.
            # Use case-insensitive regex locators to handle any casing variant.
            try:
                # We use a short timeout because if it's not there, we shouldn't wait long.
                sign_in_locator = self._page.get_by_text("Sign In", exact=False).first
                if sign_in_locator.is_visible(timeout=3000):
                    self.logger.info("Found 'Sign In' text on page. Authentication is required.")
                    return AuthState.REQUIRED
                
                log_in_locator = self._page.get_by_text("Log In", exact=False).first
                if log_in_locator.is_visible(timeout=1000):
                    self.logger.info("Found 'Log In' text on page. Authentication is required.")
                    return AuthState.REQUIRED
            except PlaywrightError:
                pass # Timeout or other error while looking for the text

            # To claim success safely, we need a verifiable signal.
            # Check for a 'Sign Out' element as positive proof of an authenticated session.
            try:
                sign_out_locator = self._page.get_by_text("Sign Out", exact=False).first
                if sign_out_locator.is_visible(timeout=3000):
                    self.logger.info("Found 'Sign Out' text on page. Authentication is available.")
                    return AuthState.AVAILABLE
            except PlaywrightError:
                pass

            # If we can't definitively prove either, we return REQUIRED to be safe.
            self.logger.warning("Could not definitively verify authentication state. Assuming REQUIRED for safety.")
            return AuthState.REQUIRED

        except PlaywrightError as e:
            self.logger.error(f"Navigation or session check failed: {e}", exc_info=True)
            return AuthState.FAILED
        except Exception as e:
            self.logger.error(f"Unexpected error during session check: {e}", exc_info=True)
            return AuthState.FAILED
