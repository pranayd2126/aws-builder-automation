"""Article engagement for AWS Builder Center.

Navigates to the selected article, posts an approved comment,
and likes the article. Respects DRY_RUN mode.
"""

import logging
import time
import random
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from playwright.sync_api import Page, Error as PlaywrightError

from app.config import Config, APPROVED_COMMENTS
from app.database import DatabaseManager, DatabaseError
from app.discovery import ArticleCandidate, validate_article_url


class EngagementStatus(str, Enum):
    """Result states for article engagement."""
    SUCCESS = "SUCCESS"
    SKIPPED = "SKIPPED"
    DRY_RUN = "DRY_RUN"
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    FAILED = "FAILED"


@dataclass
class EngagementResult:
    """Result of the engagement process."""
    status: EngagementStatus
    comment_status: Optional[str] = None
    like_status: Optional[str] = None
    error: Optional[str] = None


class ArticleEngagement:
    """Performs engagement actions on a selected Builder Center article.

    Engagement consists of:
    1. Navigating to the article page
    2. Posting an approved comment (from the rotation pool)
    3. Liking the article

    Safety guarantees:
    - Only operates on a validated ArticleCandidate from discovery
    - Pre-records comment attempt before performing the action (crash safety)
    - Respects DRY_RUN mode (no actions performed)
    - Detects authentication redirects
    - Handles already-engaged states idempotently
    """

    # CSS selectors for engagement controls
    COMMENT_INPUT_SELECTOR = (
        'textarea[name="comment"], '
        'textarea[placeholder*="comment" i], '
        '[contenteditable="true"][aria-label*="comment" i], '
        'textarea[aria-label*="comment" i]'
    )
    COMMENT_SUBMIT_SELECTOR = (
        'button[type="submit"]:has-text("Post"), '
        'button[type="submit"]:has-text("Comment"), '
        'button:has-text("Post Comment"), '
        'button:has-text("Submit")'
    )
    LIKE_BUTTON_SELECTOR = (
        'button[aria-label*="like" i], '
        'button:has-text("Like"), '
        '[data-testid*="like" i]'
    )
    LIKE_ACTIVE_SELECTOR = (
        'button[aria-pressed="true"][aria-label*="like" i], '
        'button.active:has-text("Like"), '
        'button[aria-label*="unlike" i]'
    )

    def __init__(
        self,
        config: Config,
        db: DatabaseManager,
        run_id: str,
        logger: logging.Logger,
    ):
        self.config = config
        self.db = db
        self.run_id = run_id
        self.logger = logger

    def engage(
        self,
        page: Page,
        article: ArticleCandidate,
    ) -> EngagementResult:
        """Perform engagement on the selected article.

        Args:
            page: An active Playwright Page from the authenticated session.
            article: The selected ArticleCandidate from discovery.

        Returns:
            EngagementResult with the outcome.
        """
        # Step 1: Validate the article
        if article is None or not article.url:
            self.logger.error("No article or article URL provided for engagement.")
            return EngagementResult(
                status=EngagementStatus.FAILED,
                error="No article provided",
            )

        self.logger.info(f"Engagement started for article: {article.url}")

        if not validate_article_url(article.url):
            self.logger.error(f"Article URL failed validation: {article.url}")
            return EngagementResult(
                status=EngagementStatus.FAILED,
                error="Invalid article URL",
            )

        # Step 2: Check if already engaged (idempotency)
        try:
            if not self.db.is_article_eligible(article.url):
                self.logger.info(
                    f"Article already engaged (not eligible): {article.url}"
                )
                return EngagementResult(
                    status=EngagementStatus.SKIPPED,
                    error="Article already engaged",
                )
        except DatabaseError as e:
            self.logger.error(
                f"Failed to check article eligibility: {e}", exc_info=True
            )
            return EngagementResult(
                status=EngagementStatus.FAILED,
                error=f"Eligibility check failed: {e}",
            )

        # Step 4: Navigate to the article page
        try:
            self.logger.info(f"Navigating to article: {article.url}")
            response = page.goto(article.url)
            if not response:
                self.logger.error("Navigation to article failed (no response).")
                return EngagementResult(
                    status=EngagementStatus.FAILED,
                    error="Navigation returned no response",
                )

            page.wait_for_load_state(
                "networkidle", timeout=self.config.page_timeout_ms
            )
        except PlaywrightError as e:
            self.logger.error(
                f"Failed to navigate to article: {e}", exc_info=True
            )
            return EngagementResult(
                status=EngagementStatus.FAILED,
                error=f"Navigation failed: {e}",
            )
        except Exception as e:
            self.logger.error(
                f"Unexpected error navigating to article: {e}", exc_info=True
            )
            return EngagementResult(
                status=EngagementStatus.FAILED,
                error=f"Unexpected navigation error: {e}",
            )

        # Step 5: Check for authentication redirect
        current_url = page.url.lower()
        if "signin" in current_url or "login" in current_url:
            self.logger.info(
                "Redirected to login page during engagement. "
                "Authentication required."
            )
            return EngagementResult(
                status=EngagementStatus.AUTHENTICATION_REQUIRED,
                error="Redirected to login page",
            )

        # Step 6: Select a comment
        try:
            comment_text = self.db.get_next_comment(
                APPROVED_COMMENTS, self.config.dry_run
            )
        except DatabaseError as e:
            self.logger.error(
                f"Failed to get next comment: {e}", exc_info=True
            )
            return EngagementResult(
                status=EngagementStatus.FAILED,
                error=f"Comment selection failed: {e}",
            )

        if not comment_text:
            self.logger.warning(
                "No approved comments configured or available. Aborting engagement."
            )
            return EngagementResult(
                status=EngagementStatus.FAILED,
                error="No approved comments configured",
            )

        # Step 7: Perform comment engagement
        comment_result = self._perform_comment(page, comment_text)

        # Step 8: Perform like engagement
        like_result = self._perform_like(page)

        # Step 9: Determine overall result
        if comment_result == "DRY_RUN" or like_result == "DRY_RUN":
            overall_status = EngagementStatus.DRY_RUN
        elif comment_result == "SUCCESS" or like_result == "SUCCESS":
            overall_status = EngagementStatus.SUCCESS
        elif comment_result == "FAILED" and like_result == "FAILED":
            overall_status = EngagementStatus.FAILED
        else:
            # At least one action succeeded or was already present
            overall_status = EngagementStatus.SUCCESS

        self.logger.info(
            f"Engagement completed: status={overall_status.value}, "
            f"comment={comment_result}, like={like_result}"
        )
        return EngagementResult(
            status=overall_status,
            comment_status=comment_result,
            like_status=like_result,
        )

    def _perform_comment(
        self,
        page: Page,
        comment_text: str,
    ) -> str:
        """Attempt to post a comment on the article.

        Args:
            page: The Playwright page on the article.
            comment_text: The approved comment to post.

        Returns:
            Status string: SUCCESS, FAILED, NOT_FOUND.
        """

        # Pre-record the comment attempt BEFORE performing the action
        # This ensures crash safety: if we crash after clicking but before
        # recording, the article is permanently blocked for real runs.
        if not self.config.dry_run:
            try:
                self.db.record_comment_attempt(self.run_id, comment_text)
                self.logger.info(
                    f"Comment attempt recorded for run {self.run_id}."
                )
            except DatabaseError as e:
                self.logger.error(
                    f"Failed to pre-record comment attempt: {e}", exc_info=True
                )
                return "FAILED"

        # Find the comment input
        try:
            comment_input = page.query_selector(self.COMMENT_INPUT_SELECTOR)
            if not comment_input:
                self.logger.warning("Comment input not found on page.")
                self._record_comment_result("NOT_FOUND", "Comment input not found")
                return "NOT_FOUND"

            # Check if the input is visible and enabled
            if not comment_input.is_visible():
                self.logger.warning("Comment input is not visible.")
                self._record_comment_result("NOT_FOUND", "Comment input not visible")
                return "NOT_FOUND"

            if not comment_input.is_enabled():
                self.logger.warning("Comment input is disabled.")
                self._record_comment_result("FAILED", "Comment input disabled")
                return "FAILED"

            # Add a natural delay before typing
            self._action_delay()

            # Find and click submit
            submit_button = page.query_selector(self.COMMENT_SUBMIT_SELECTOR)
            if not submit_button:
                self.logger.warning("Comment submit button not found.")
                self._record_comment_result("FAILED", "Submit button not found")
                return "FAILED"

            if not submit_button.is_visible():
                self.logger.warning("Comment submit button is not visible.")
                self._record_comment_result("FAILED", "Submit button not visible")
                return "FAILED"

            if not submit_button.is_enabled():
                self.logger.warning("Comment submit button is disabled.")
                self._record_comment_result("FAILED", "Submit button disabled")
                return "FAILED"

            if self.config.dry_run:
                self.logger.info("DRY_RUN: Comment inputs verified. Skipping actual typing and submission.")
                return "DRY_RUN"

            # Fill in the comment
            comment_input.fill(comment_text)
            self.logger.info("Comment text entered.")

            # Add delay before submitting
            self._action_delay()
            
            submit_button.click()
            self.logger.info("Comment submitted.")

            # Wait for the page to process the submission
            try:
                page.wait_for_load_state(
                    "networkidle", timeout=self.config.page_timeout_ms
                )
            except PlaywrightError:
                self.logger.warning(
                    "Timeout waiting for page after comment submission."
                )

            self._record_comment_result("SUCCESS")
            return "SUCCESS"

        except PlaywrightError as e:
            self.logger.error(
                f"Playwright error during comment: {e}", exc_info=True
            )
            self._record_comment_result("FAILED", str(e))
            return "FAILED"
        except Exception as e:
            self.logger.error(
                f"Unexpected error during comment: {e}", exc_info=True
            )
            self._record_comment_result("FAILED", str(e))
            return "FAILED"

    def _perform_like(self, page: Page) -> str:
        """Attempt to like the article.

        Args:
            page: The Playwright page on the article.

        Returns:
            Status string: SUCCESS, ALREADY_PRESENT, FAILED, NOT_FOUND.
        """
        # Pre-record the like attempt
        if not self.config.dry_run:
            try:
                self.db.record_like_attempt(self.run_id)
                self.logger.info(f"Like attempt recorded for run {self.run_id}.")
            except DatabaseError as e:
                self.logger.error(
                    f"Failed to pre-record like attempt: {e}", exc_info=True
                )
                return "FAILED"

        try:
            # Check if already liked
            already_liked = page.query_selector(self.LIKE_ACTIVE_SELECTOR)
            if already_liked and already_liked.is_visible():
                self.logger.info("Article is already liked.")
                self._record_like_result("ALREADY_PRESENT")
                return "ALREADY_PRESENT"

            # Find the like button
            like_button = page.query_selector(self.LIKE_BUTTON_SELECTOR)
            if not like_button:
                self.logger.warning("Like button not found on page.")
                self._record_like_result("NOT_FOUND", "Like button not found")
                return "NOT_FOUND"

            if not like_button.is_visible():
                self.logger.warning("Like button is not visible.")
                self._record_like_result("NOT_FOUND", "Like button not visible")
                return "NOT_FOUND"

            if not like_button.is_enabled():
                self.logger.warning("Like button is disabled.")
                self._record_like_result("FAILED", "Like button disabled")
                return "FAILED"

            # Add a natural delay before clicking
            self._action_delay()

            if self.config.dry_run:
                self.logger.info("DRY_RUN: Like button verified. Skipping actual click.")
                return "DRY_RUN"

            like_button.click()
            self.logger.info("Like button clicked.")

            # Brief wait for UI state to update
            try:
                page.wait_for_timeout(2000)
            except PlaywrightError:
                pass

            self._record_like_result("SUCCESS")
            return "SUCCESS"

        except PlaywrightError as e:
            self.logger.error(
                f"Playwright error during like: {e}", exc_info=True
            )
            self._record_like_result("FAILED", str(e))
            return "FAILED"
        except Exception as e:
            self.logger.error(
                f"Unexpected error during like: {e}", exc_info=True
            )
            self._record_like_result("FAILED", str(e))
            return "FAILED"

    def _record_comment_result(
        self,
        status: str,
        error_diagnostics: Optional[str] = None,
    ) -> None:
        """Safely record the comment result in the database."""
        if self.config.dry_run:
            return
        try:
            self.db.record_comment_result(
                self.run_id, status, error_diagnostics
            )
        except DatabaseError as e:
            self.logger.error(
                f"Failed to record comment result: {e}", exc_info=True
            )

    def _record_like_result(
        self,
        status: str,
        error_diagnostics: Optional[str] = None,
    ) -> None:
        """Safely record the like result in the database."""
        if self.config.dry_run:
            return
        try:
            self.db.record_like_result(
                self.run_id, status, error_diagnostics
            )
        except DatabaseError as e:
            self.logger.error(
                f"Failed to record like result: {e}", exc_info=True
            )

    def _action_delay(self) -> None:
        """Add a natural delay between actions."""
        delay = random.uniform(
            self.config.action_delay_min_s,
            self.config.action_delay_max_s,
        )
        self.logger.debug(f"Action delay: {delay:.2f}s")
        time.sleep(delay)
