"""Tests for app.engagement module — Phase 6."""

import logging
import sys
import time
import pytest
from dataclasses import replace as dataclass_replace
from unittest.mock import MagicMock, patch, call

from playwright.sync_api import Error as PlaywrightError

from app.config import Config, APPROVED_COMMENTS
from app.database import DatabaseManager, DatabaseError
from app.discovery import ArticleCandidate, DiscoveryStatus
from app.engagement import (
    ArticleEngagement,
    EngagementResult,
    EngagementStatus,
)
from app.lifecycle import RunPhase


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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
        builder_center_url="https://community.aws",
        page_timeout_ms=5000,
        selector_timeout_ms=2000,
        dry_run=False,
        action_delay_min_s=0.01,
        action_delay_max_s=0.02,
    )


@pytest.fixture
def dry_config(test_config):
    """Provides a Config with DRY_RUN enabled."""
    return dataclass_replace(test_config, dry_run=True)


@pytest.fixture
def logger():
    return logging.getLogger("test_engagement")


@pytest.fixture
def db(test_config):
    """Provides a DatabaseManager with an initialized schema."""
    db = DatabaseManager(test_config.database_path, logger=logging.getLogger("test_db"))
    db.init_db()
    return db


@pytest.fixture
def run_id():
    return "test-run-001"


@pytest.fixture
def engagement(test_config, db, run_id, logger):
    """Provides an ArticleEngagement instance with real run in DB."""
    db.record_run_start(run_id, is_dry_run=test_config.dry_run)
    return ArticleEngagement(test_config, db, run_id, logger)


@pytest.fixture
def valid_article(db):
    """Provides a valid ArticleCandidate that exists in the DB."""
    url = "https://community.aws/posts/test-article"
    db.save_article(url, "Test Article", "Author", "2024-01-01")
    return ArticleCandidate(
        url=url,
        title="Test Article",
        author="Author",
        pub_date="2024-01-01",
        article_id="test-article",
    )


def _make_mock_page(
    url_after_nav="https://community.aws/posts/test-article",
    goto_response=True,
    goto_exception=None,
    has_comment_input=True,
    has_submit_button=True,
    has_like_button=True,
    already_liked=False,
    comment_input_visible=True,
    comment_input_enabled=True,
    submit_visible=True,
    submit_enabled=True,
    like_visible=True,
    like_enabled=True,
):
    """Create a mock Playwright Page for engagement tests.

    Returns the same mock element for repeated query_selector calls with the
    same matching category, so tests can configure side_effects on elements
    obtained from the page.
    """
    page = MagicMock()

    if goto_exception:
        page.goto.side_effect = goto_exception
    elif goto_response:
        page.goto.return_value = MagicMock()
    else:
        page.goto.return_value = None

    page.url = url_after_nav

    # Pre-build cached mock elements
    _comment_input = MagicMock(name="comment_input") if has_comment_input else None
    if _comment_input:
        _comment_input.is_visible.return_value = comment_input_visible
        _comment_input.is_enabled.return_value = comment_input_enabled

    _submit_button = MagicMock(name="submit_button") if has_submit_button else None
    if _submit_button:
        _submit_button.is_visible.return_value = submit_visible
        _submit_button.is_enabled.return_value = submit_enabled

    _like_button = MagicMock(name="like_button") if has_like_button else None
    if _like_button:
        _like_button.is_visible.return_value = like_visible
        _like_button.is_enabled.return_value = like_enabled

    _like_active = None
    if already_liked:
        _like_active = MagicMock(name="like_active")
        _like_active.is_visible.return_value = True

    def query_selector_side_effect(selector):
        # Like active selector (already liked check)
        if "aria-pressed" in selector or "unlike" in selector:
            return _like_active

        # Comment input
        if "textarea" in selector or "contenteditable" in selector:
            return _comment_input

        # Comment submit button
        if "submit" in selector.lower() or "Post" in selector or "Comment" in selector:
            return _submit_button

        # Like button
        if "like" in selector.lower():
            return _like_button

        return None

    page.query_selector.side_effect = query_selector_side_effect

    # Store cached elements as attributes for test access
    page._mock_comment_input = _comment_input
    page._mock_submit_button = _submit_button
    page._mock_like_button = _like_button
    page._mock_like_active = _like_active

    return page


# ===========================================================================
# 1. Successful Engagement
# ===========================================================================

class TestSuccessfulEngagement:

    def test_full_success_with_comment_and_like(self, test_config, db, run_id, logger, valid_article):
        """Test successful comment + like engagement."""
        db.record_run_start(run_id, is_dry_run=False)
        eng = ArticleEngagement(test_config, db, run_id, logger)

        with patch("app.engagement.APPROVED_COMMENTS", ["Great article!"]):
            page = _make_mock_page()
            result = eng.engage(page, valid_article)

        assert result.status == EngagementStatus.SUCCESS
        assert result.comment_status == "SUCCESS"
        assert result.like_status == "SUCCESS"

    def test_success_persisted_in_db(self, test_config, db, run_id, logger, valid_article):
        """Test that successful engagement is persisted in the DB."""
        db.record_run_start(run_id, is_dry_run=False)
        eng = ArticleEngagement(test_config, db, run_id, logger)

        with patch("app.engagement.APPROVED_COMMENTS", ["Nice post!"]):
            page = _make_mock_page()
            eng.engage(page, valid_article)

        with db.get_connection() as conn:
            cursor = conn.execute(
                "SELECT comment_status, like_status FROM runs WHERE run_id = ?",
                (run_id,),
            )
            row = cursor.fetchone()
            assert row["comment_status"] == "SUCCESS"
            assert row["like_status"] == "SUCCESS"


# ===========================================================================
# 2. DRY_RUN Behavior
# ===========================================================================

class TestDryRun:

    def test_dry_run_returns_dry_run_status(self, dry_config, db, run_id, logger, valid_article):
        """Test DRY_RUN returns DRY_RUN status without performing actions."""
        db.record_run_start(run_id, is_dry_run=True)
        eng = ArticleEngagement(dry_config, db, run_id, logger)

        with patch("app.engagement.APPROVED_COMMENTS", ["Test comment"]):
            page = _make_mock_page()
            result = eng.engage(page, valid_article)

        assert result.status == EngagementStatus.DRY_RUN

    def test_dry_run_does_navigate_for_verification(self, dry_config, db, run_id, logger, valid_article):
        """Test DRY_RUN navigates to the article page for safe verification."""
        db.record_run_start(run_id, is_dry_run=True)
        eng = ArticleEngagement(dry_config, db, run_id, logger)

        page = _make_mock_page()
        eng.engage(page, valid_article)

        page.goto.assert_called_once()

    def test_dry_run_does_not_click(self, dry_config, db, run_id, logger, valid_article):
        """Test DRY_RUN does not perform click actions."""
        db.record_run_start(run_id, is_dry_run=True)
        eng = ArticleEngagement(dry_config, db, run_id, logger)

        page = _make_mock_page()
        eng.engage(page, valid_article)

        page.click.assert_not_called()

    def test_dry_run_no_comment_recorded(self, dry_config, db, run_id, logger, valid_article):
        """Test DRY_RUN does not record any comment attempt in the DB."""
        db.record_run_start(run_id, is_dry_run=True)
        eng = ArticleEngagement(dry_config, db, run_id, logger)

        page = _make_mock_page()
        eng.engage(page, valid_article)

        with db.get_connection() as conn:
            cursor = conn.execute(
                "SELECT comment_status FROM runs WHERE run_id = ?", (run_id,)
            )
            row = cursor.fetchone()
            assert row["comment_status"] is None

    def test_dry_run_no_like_recorded(self, dry_config, db, run_id, logger, valid_article):
        """Test DRY_RUN does not record any like attempt in the DB."""
        db.record_run_start(run_id, is_dry_run=True)
        eng = ArticleEngagement(dry_config, db, run_id, logger)

        page = _make_mock_page()
        eng.engage(page, valid_article)

        with db.get_connection() as conn:
            cursor = conn.execute(
                "SELECT like_status FROM runs WHERE run_id = ?", (run_id,)
            )
            row = cursor.fetchone()
            assert row["like_status"] is None


# ===========================================================================
# 3. Authentication Redirect
# ===========================================================================

class TestAuthenticationRedirect:

    def test_auth_redirect_on_signin(self, test_config, db, run_id, logger, valid_article):
        """Test AUTHENTICATION_REQUIRED when redirected to signin."""
        db.record_run_start(run_id, is_dry_run=False)
        eng = ArticleEngagement(test_config, db, run_id, logger)

        page = _make_mock_page(url_after_nav="https://signin.aws.amazon.com/signin")
        result = eng.engage(page, valid_article)

        assert result.status == EngagementStatus.AUTHENTICATION_REQUIRED

    def test_auth_redirect_on_login(self, test_config, db, run_id, logger, valid_article):
        """Test AUTHENTICATION_REQUIRED when redirected to login."""
        db.record_run_start(run_id, is_dry_run=False)
        eng = ArticleEngagement(test_config, db, run_id, logger)

        page = _make_mock_page(url_after_nav="https://community.aws/login")
        result = eng.engage(page, valid_article)

        assert result.status == EngagementStatus.AUTHENTICATION_REQUIRED


# ===========================================================================
# 4. Navigation Failure
# ===========================================================================

class TestNavigationFailure:

    def test_playwright_error_on_nav(self, test_config, db, run_id, logger, valid_article):
        """Test FAILED on Playwright navigation error."""
        db.record_run_start(run_id, is_dry_run=False)
        eng = ArticleEngagement(test_config, db, run_id, logger)

        page = _make_mock_page(goto_exception=PlaywrightError("Timeout"))
        result = eng.engage(page, valid_article)

        assert result.status == EngagementStatus.FAILED
        assert "Timeout" in result.error

    def test_null_response_on_nav(self, test_config, db, run_id, logger, valid_article):
        """Test FAILED when navigation returns None."""
        db.record_run_start(run_id, is_dry_run=False)
        eng = ArticleEngagement(test_config, db, run_id, logger)

        page = _make_mock_page(goto_response=None)
        result = eng.engage(page, valid_article)

        assert result.status == EngagementStatus.FAILED


# ===========================================================================
# 5. Timeout
# ===========================================================================

class TestTimeout:

    def test_wait_timeout(self, test_config, db, run_id, logger, valid_article):
        """Test that wait_for_load_state timeout is handled."""
        db.record_run_start(run_id, is_dry_run=False)
        eng = ArticleEngagement(test_config, db, run_id, logger)

        page = _make_mock_page()
        page.wait_for_load_state.side_effect = PlaywrightError("Timeout waiting for load state")

        result = eng.engage(page, valid_article)

        assert result.status == EngagementStatus.FAILED
        assert "Timeout" in result.error


# ===========================================================================
# 6. Missing Engagement Control
# ===========================================================================

class TestMissingControl:

    def test_no_comment_input(self, test_config, db, run_id, logger, valid_article):
        """Test handling when comment input is not found."""
        db.record_run_start(run_id, is_dry_run=False)
        eng = ArticleEngagement(test_config, db, run_id, logger)

        with patch("app.engagement.APPROVED_COMMENTS", ["A comment"]):
            page = _make_mock_page(has_comment_input=False)
            result = eng.engage(page, valid_article)

        # Comment fails but like may succeed
        assert result.comment_status == "NOT_FOUND"

    def test_no_submit_button(self, test_config, db, run_id, logger, valid_article):
        """Test handling when submit button is not found."""
        db.record_run_start(run_id, is_dry_run=False)
        eng = ArticleEngagement(test_config, db, run_id, logger)

        with patch("app.engagement.APPROVED_COMMENTS", ["A comment"]):
            page = _make_mock_page(has_submit_button=False)
            result = eng.engage(page, valid_article)

        assert result.comment_status == "FAILED"

    def test_no_like_button(self, test_config, db, run_id, logger, valid_article):
        """Test handling when like button is not found."""
        db.record_run_start(run_id, is_dry_run=False)
        eng = ArticleEngagement(test_config, db, run_id, logger)

        with patch("app.engagement.APPROVED_COMMENTS", ["A comment"]):
            page = _make_mock_page(has_like_button=False)
            result = eng.engage(page, valid_article)

        assert result.like_status == "NOT_FOUND"


# ===========================================================================
# 7. Malformed/Unexpected Page
# ===========================================================================

class TestMalformedPage:

    def test_invalid_article_url(self, test_config, db, run_id, logger):
        """Test FAILED when article URL is invalid."""
        db.record_run_start(run_id, is_dry_run=False)
        eng = ArticleEngagement(test_config, db, run_id, logger)

        bad_article = ArticleCandidate(url="not-a-valid-url")
        page = _make_mock_page()
        result = eng.engage(page, bad_article)

        assert result.status == EngagementStatus.FAILED
        assert "Invalid article URL" in result.error

    def test_none_article(self, test_config, db, run_id, logger):
        """Test FAILED when article is None."""
        db.record_run_start(run_id, is_dry_run=False)
        eng = ArticleEngagement(test_config, db, run_id, logger)

        page = _make_mock_page()
        result = eng.engage(page, None)

        assert result.status == EngagementStatus.FAILED

    def test_empty_url_article(self, test_config, db, run_id, logger):
        """Test FAILED when article URL is empty."""
        db.record_run_start(run_id, is_dry_run=False)
        eng = ArticleEngagement(test_config, db, run_id, logger)

        bad_article = ArticleCandidate(url="")
        page = _make_mock_page()
        result = eng.engage(page, bad_article)

        assert result.status == EngagementStatus.FAILED

    def test_disabled_comment_input(self, test_config, db, run_id, logger, valid_article):
        """Test handling when comment input is disabled."""
        db.record_run_start(run_id, is_dry_run=False)
        eng = ArticleEngagement(test_config, db, run_id, logger)

        with patch("app.engagement.APPROVED_COMMENTS", ["A comment"]):
            page = _make_mock_page(comment_input_enabled=False)
            result = eng.engage(page, valid_article)

        assert result.comment_status == "FAILED"


# ===========================================================================
# 8. Already-Engaged Article
# ===========================================================================

class TestAlreadyEngaged:

    def test_already_engaged_returns_skipped(self, test_config, db, run_id, logger):
        """Test SKIPPED when article was already engaged."""
        url = "https://community.aws/posts/already-engaged"
        db.save_article(url, "Already Engaged")
        # Create a previous real run that engaged this article
        db.record_run_start("prev-run", is_dry_run=False)
        db.select_article_for_run("prev-run", url)
        db.record_comment_attempt("prev-run", "Previous comment")
        db.record_comment_result("prev-run", "SUCCESS")

        article = ArticleCandidate(url=url, title="Already Engaged")
        db.record_run_start(run_id, is_dry_run=False)
        eng = ArticleEngagement(test_config, db, run_id, logger)

        page = _make_mock_page()
        result = eng.engage(page, article)

        assert result.status == EngagementStatus.SKIPPED

    def test_already_liked(self, test_config, db, run_id, logger, valid_article):
        """Test ALREADY_PRESENT when article is already liked."""
        db.record_run_start(run_id, is_dry_run=False)
        eng = ArticleEngagement(test_config, db, run_id, logger)

        with patch("app.engagement.APPROVED_COMMENTS", ["A comment"]):
            page = _make_mock_page(already_liked=True)
            result = eng.engage(page, valid_article)

        assert result.like_status == "ALREADY_PRESENT"


# ===========================================================================
# 9. Duplicate Engagement Prevention
# ===========================================================================

class TestDuplicateEngagement:

    def test_second_engagement_blocked(self, test_config, db, logger):
        """Test that a second engagement on the same article is blocked."""
        url = "https://community.aws/posts/dup-article"
        db.save_article(url, "Dup Article")
        article = ArticleCandidate(url=url, title="Dup Article")

        # First engagement
        run1 = "run-first"
        db.record_run_start(run1, is_dry_run=False)
        db.select_article_for_run(run1, url)
        eng1 = ArticleEngagement(test_config, db, run1, logger)

        with patch("app.engagement.APPROVED_COMMENTS", ["Comment 1"]):
            page = _make_mock_page()
            result1 = eng1.engage(page, article)

        assert result1.status == EngagementStatus.SUCCESS

        # Second engagement should be SKIPPED
        run2 = "run-second"
        db.record_run_start(run2, is_dry_run=False)
        eng2 = ArticleEngagement(test_config, db, run2, logger)

        page = _make_mock_page()
        result2 = eng2.engage(page, article)

        assert result2.status == EngagementStatus.SKIPPED

    def test_action_performed_at_most_once(self, test_config, db, run_id, logger, valid_article):
        """Test that the engagement action is performed exactly once per run."""
        db.record_run_start(run_id, is_dry_run=False)
        eng = ArticleEngagement(test_config, db, run_id, logger)

        with patch("app.engagement.APPROVED_COMMENTS", ["Test comment"]):
            page = _make_mock_page()
            eng.engage(page, valid_article)

        # Verify goto was called exactly once (navigation to article)
        page.goto.assert_called_once()


# ===========================================================================
# 10. Database Failure
# ===========================================================================

class TestDatabaseFailure:

    def test_eligibility_check_db_failure(self, test_config, db, run_id, logger, valid_article):
        """Test FAILED when eligibility check DB fails."""
        db.record_run_start(run_id, is_dry_run=False)
        eng = ArticleEngagement(test_config, db, run_id, logger)

        with patch.object(db, "is_article_eligible", side_effect=DatabaseError("DB error")):
            page = _make_mock_page()
            result = eng.engage(page, valid_article)

        assert result.status == EngagementStatus.FAILED

    def test_comment_attempt_db_failure(self, test_config, db, run_id, logger, valid_article):
        """Test comment fails when pre-recording DB fails."""
        db.record_run_start(run_id, is_dry_run=False)
        eng = ArticleEngagement(test_config, db, run_id, logger)

        with patch("app.engagement.APPROVED_COMMENTS", ["A comment"]):
            with patch.object(db, "record_comment_attempt", side_effect=DatabaseError("DB write error")):
                page = _make_mock_page()
                result = eng.engage(page, valid_article)

        assert result.comment_status == "FAILED"

    def test_like_attempt_db_failure(self, test_config, db, run_id, logger, valid_article):
        """Test like fails when pre-recording DB fails."""
        db.record_run_start(run_id, is_dry_run=False)
        eng = ArticleEngagement(test_config, db, run_id, logger)

        with patch("app.engagement.APPROVED_COMMENTS", ["A comment"]):
            with patch.object(db, "record_like_attempt", side_effect=DatabaseError("DB error")):
                page = _make_mock_page()
                result = eng.engage(page, valid_article)

        assert result.like_status == "FAILED"


# ===========================================================================
# 11. Playwright Failure
# ===========================================================================

class TestPlaywrightFailure:

    def test_comment_fill_playwright_error(self, test_config, db, run_id, logger, valid_article):
        """Test comment fails on Playwright error during fill."""
        db.record_run_start(run_id, is_dry_run=False)
        eng = ArticleEngagement(test_config, db, run_id, logger)

        with patch("app.engagement.APPROVED_COMMENTS", ["Test comment"]):
            page = _make_mock_page()
            # Configure the cached comment input to raise on fill
            page._mock_comment_input.fill.side_effect = PlaywrightError("Fill failed")

            result = eng.engage(page, valid_article)

        assert result.comment_status == "FAILED"

    def test_like_click_playwright_error(self, test_config, db, run_id, logger, valid_article):
        """Test like fails on Playwright error during click."""
        db.record_run_start(run_id, is_dry_run=False)
        eng = ArticleEngagement(test_config, db, run_id, logger)

        with patch("app.engagement.APPROVED_COMMENTS", ["Test comment"]):
            page = _make_mock_page()
            # Configure the cached like button to raise on click
            page._mock_like_button.click.side_effect = PlaywrightError("Click failed")

            result = eng.engage(page, valid_article)

        assert result.like_status == "FAILED"


# ===========================================================================
# 12. Unexpected Exception
# ===========================================================================

class TestUnexpectedException:

    def test_unexpected_runtime_error_on_nav(self, test_config, db, run_id, logger, valid_article):
        """Test FAILED on unexpected RuntimeError during navigation."""
        db.record_run_start(run_id, is_dry_run=False)
        eng = ArticleEngagement(test_config, db, run_id, logger)

        page = _make_mock_page(goto_exception=RuntimeError("Unexpected crash"))
        result = eng.engage(page, valid_article)

        assert result.status == EngagementStatus.FAILED
        assert "Unexpected" in result.error


# ===========================================================================
# 13. Successful DB Persistence
# ===========================================================================

class TestDBPersistence:

    def test_comment_attempt_recorded_before_action(self, test_config, db, run_id, logger, valid_article):
        """Test that comment attempt is recorded BEFORE the action."""
        db.record_run_start(run_id, is_dry_run=False)

        recorded_before_fill = []

        def mock_fill(text):
            # At this point, comment_attempt should already be recorded
            with db.get_connection() as conn:
                cursor = conn.execute(
                    "SELECT comment_status FROM runs WHERE run_id = ?",
                    (run_id,),
                )
                row = cursor.fetchone()
                recorded_before_fill.append(row["comment_status"])

        eng = ArticleEngagement(test_config, db, run_id, logger)

        with patch("app.engagement.APPROVED_COMMENTS", ["Test comment"]):
            page = _make_mock_page()
            page._mock_comment_input.fill.side_effect = mock_fill
            eng.engage(page, valid_article)

        assert recorded_before_fill == ["ATTEMPTED"]


# ===========================================================================
# 14. Failed Engagement Does Not Become Success
# ===========================================================================

class TestFailedNotSuccess:

    def test_comment_failure_not_recorded_as_success(self, test_config, db, run_id, logger, valid_article):
        """Test that a failed comment is not recorded as SUCCESS."""
        db.record_run_start(run_id, is_dry_run=False)
        eng = ArticleEngagement(test_config, db, run_id, logger)

        with patch("app.engagement.APPROVED_COMMENTS", ["A comment"]):
            page = _make_mock_page(has_submit_button=False)
            eng.engage(page, valid_article)

        with db.get_connection() as conn:
            cursor = conn.execute(
                "SELECT comment_status FROM runs WHERE run_id = ?", (run_id,)
            )
            row = cursor.fetchone()
            assert row["comment_status"] != "SUCCESS"
            assert row["comment_status"] == "FAILED"

    def test_like_failure_not_recorded_as_success(self, test_config, db, run_id, logger, valid_article):
        """Test that a failed like is not recorded as SUCCESS."""
        db.record_run_start(run_id, is_dry_run=False)
        eng = ArticleEngagement(test_config, db, run_id, logger)

        with patch("app.engagement.APPROVED_COMMENTS", []):
            page = _make_mock_page(has_like_button=False)
            eng.engage(page, valid_article)

        with db.get_connection() as conn:
            cursor = conn.execute(
                "SELECT like_status FROM runs WHERE run_id = ?", (run_id,)
            )
            row = cursor.fetchone()
            assert row["like_status"] != "SUCCESS"


# ===========================================================================
# 15. Main Lifecycle Integration
# ===========================================================================

class TestMainLifecycleIntegration:

    def test_main_with_engagement_success(self, monkeypatch):
        """Test main lifecycle with successful engagement."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "dummy")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "dummy")

        from app.main import main
        from app.browser import AuthState

        mock_discovery_result = MagicMock()
        mock_discovery_result.status = DiscoveryStatus.SELECTED
        mock_discovery_result.article = ArticleCandidate(
            url="https://community.aws/posts/test",
            title="Test",
        )

        mock_eng_result = EngagementResult(status=EngagementStatus.SUCCESS)

        with patch("app.main.setup_logger") as mock_logger:
            mock_logger.return_value = MagicMock()
            with patch("app.main.BrowserManager") as mock_bm:
                mock_bm.return_value.check_session.return_value = AuthState.AVAILABLE
                mock_bm.return_value.session.return_value.__enter__ = MagicMock()
                mock_bm.return_value.session.return_value.__exit__ = MagicMock()

                with patch("app.main.ArticleDiscovery") as mock_disc:
                    mock_disc.return_value.discover.return_value = mock_discovery_result
                    with patch("app.main.ArticleEngagement") as mock_eng:
                        mock_eng.return_value.engage.return_value = mock_eng_result

                        with patch.object(sys, "argv", ["main.py", "--dry-run"]):
                            exit_code = main()

        assert exit_code == 0

    def test_main_with_engagement_failed(self, monkeypatch):
        """Test main lifecycle with failed engagement."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "dummy")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "dummy")

        from app.main import main
        from app.browser import AuthState

        mock_discovery_result = MagicMock()
        mock_discovery_result.status = DiscoveryStatus.SELECTED
        mock_discovery_result.article = ArticleCandidate(
            url="https://community.aws/posts/test",
            title="Test",
        )

        mock_eng_result = EngagementResult(
            status=EngagementStatus.FAILED,
            error="Navigation failed"
        )

        with patch("app.main.setup_logger") as mock_logger:
            mock_logger.return_value = MagicMock()
            with patch("app.main.BrowserManager") as mock_bm:
                mock_bm.return_value.check_session.return_value = AuthState.AVAILABLE
                mock_bm.return_value.session.return_value.__enter__ = MagicMock()
                mock_bm.return_value.session.return_value.__exit__ = MagicMock()

                with patch("app.main.ArticleDiscovery") as mock_disc:
                    mock_disc.return_value.discover.return_value = mock_discovery_result
                    with patch("app.main.ArticleEngagement") as mock_eng:
                        mock_eng.return_value.engage.return_value = mock_eng_result

                        with patch.object(sys, "argv", ["main.py", "--dry-run"]):
                            exit_code = main()

        assert exit_code == 1


# ===========================================================================
# 16. Discovery NO_NEW_ARTICLE Does Not Trigger Engagement
# ===========================================================================

class TestNoEngagementOnNoArticle:

    def test_no_new_article_skips_engagement(self, monkeypatch):
        """Test that NO_NEW_ARTICLE does not trigger engagement."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "dummy")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "dummy")

        from app.main import main
        from app.browser import AuthState
        from app.discovery import DiscoveryResult

        mock_discovery_result = DiscoveryResult(
            status=DiscoveryStatus.NO_NEW_ARTICLE
        )

        with patch("app.main.setup_logger") as mock_logger:
            mock_logger.return_value = MagicMock()
            with patch("app.main.BrowserManager") as mock_bm:
                mock_bm.return_value.check_session.return_value = AuthState.AVAILABLE
                mock_bm.return_value.session.return_value.__enter__ = MagicMock()
                mock_bm.return_value.session.return_value.__exit__ = MagicMock()

                with patch("app.main.ArticleDiscovery") as mock_disc:
                    mock_disc.return_value.discover.return_value = mock_discovery_result
                    with patch("app.main.ArticleEngagement") as mock_eng:
                        with patch.object(sys, "argv", ["main.py", "--dry-run"]):
                            exit_code = main()

                        # ArticleEngagement should not be instantiated
                        mock_eng.assert_not_called()

        assert exit_code == 0


# ===========================================================================
# 17. Discovery AUTHENTICATION_REQUIRED Does Not Trigger Engagement
# ===========================================================================

class TestNoEngagementOnAuthRequired:

    def test_auth_required_skips_engagement(self, monkeypatch):
        """Test that AUTHENTICATION_REQUIRED does not trigger engagement."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "dummy")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "dummy")

        from app.main import main
        from app.browser import AuthState
        from app.discovery import DiscoveryResult

        mock_discovery_result = DiscoveryResult(
            status=DiscoveryStatus.AUTHENTICATION_REQUIRED,
            error="Redirected to login",
        )

        with patch("app.main.setup_logger") as mock_logger:
            mock_logger.return_value = MagicMock()
            with patch("app.main.BrowserManager") as mock_bm:
                mock_bm.return_value.check_session.return_value = AuthState.AVAILABLE
                mock_bm.return_value.session.return_value.__enter__ = MagicMock()
                mock_bm.return_value.session.return_value.__exit__ = MagicMock()

                with patch("app.main.ArticleDiscovery") as mock_disc:
                    mock_disc.return_value.discover.return_value = mock_discovery_result
                    with patch("app.main.ArticleEngagement") as mock_eng:
                        with patch.object(sys, "argv", ["main.py", "--dry-run"]):
                            exit_code = main()

                        mock_eng.assert_not_called()

        assert exit_code == 1


# ===========================================================================
# 18. Discovery FAILED Does Not Trigger Engagement
# ===========================================================================

class TestNoEngagementOnDiscoveryFailed:

    def test_discovery_failed_skips_engagement(self, monkeypatch):
        """Test that discovery FAILED does not trigger engagement."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "dummy")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "dummy")

        from app.main import main
        from app.browser import AuthState
        from app.discovery import DiscoveryResult

        mock_discovery_result = DiscoveryResult(
            status=DiscoveryStatus.FAILED,
            error="Navigation failed",
        )

        with patch("app.main.setup_logger") as mock_logger:
            mock_logger.return_value = MagicMock()
            with patch("app.main.BrowserManager") as mock_bm:
                mock_bm.return_value.check_session.return_value = AuthState.AVAILABLE
                mock_bm.return_value.session.return_value.__enter__ = MagicMock()
                mock_bm.return_value.session.return_value.__exit__ = MagicMock()

                with patch("app.main.ArticleDiscovery") as mock_disc:
                    mock_disc.return_value.discover.return_value = mock_discovery_result
                    with patch("app.main.ArticleEngagement") as mock_eng:
                        with patch.object(sys, "argv", ["main.py", "--dry-run"]):
                            exit_code = main()

                        mock_eng.assert_not_called()

        assert exit_code == 1


# ===========================================================================
# 19. Only the Selected Article Is Engaged
# ===========================================================================

class TestOnlySelectedArticle:

    def test_navigates_only_to_selected_url(self, test_config, db, run_id, logger, valid_article):
        """Test that engagement navigates only to the selected article URL."""
        db.record_run_start(run_id, is_dry_run=False)
        eng = ArticleEngagement(test_config, db, run_id, logger)

        with patch("app.engagement.APPROVED_COMMENTS", ["Test"]):
            page = _make_mock_page()
            eng.engage(page, valid_article)

        # Should navigate only to the specific article URL
        page.goto.assert_called_once_with(valid_article.url)


# ===========================================================================
# 20. Action Performed At Most Once
# ===========================================================================

class TestActionAtMostOnce:

    def test_comment_fill_called_once(self, test_config, db, run_id, logger, valid_article):
        """Test comment fill is called exactly once."""
        db.record_run_start(run_id, is_dry_run=False)
        eng = ArticleEngagement(test_config, db, run_id, logger)

        with patch("app.engagement.APPROVED_COMMENTS", ["Single comment"]):
            page = _make_mock_page()
            eng.engage(page, valid_article)

            page._mock_comment_input.fill.assert_called_once()


# ===========================================================================
# 21. No Approved Comments Available
# ===========================================================================

class TestNoApprovedComments:

    def test_no_comments_aborts_engagement(self, test_config, db, run_id, logger, valid_article):
        """Test that empty APPROVED_COMMENTS aborts the engagement without liking."""
        db.record_run_start(run_id, is_dry_run=False)
        eng = ArticleEngagement(test_config, db, run_id, logger)

        with patch("app.engagement.APPROVED_COMMENTS", []):
            page = _make_mock_page()
            result = eng.engage(page, valid_article)

        assert result.status == EngagementStatus.FAILED
        assert "No approved comments configured" in result.error
        assert result.comment_status is None
        assert result.like_status is None


# ===========================================================================
# 22. EngagementStatus enum values
# ===========================================================================

class TestEngagementStatusEnum:

    def test_all_statuses_are_strings(self):
        for status in EngagementStatus:
            assert isinstance(status, str)

    def test_expected_statuses_exist(self):
        assert EngagementStatus.SUCCESS == "SUCCESS"
        assert EngagementStatus.SKIPPED == "SKIPPED"
        assert EngagementStatus.DRY_RUN == "DRY_RUN"
        assert EngagementStatus.AUTHENTICATION_REQUIRED == "AUTHENTICATION_REQUIRED"
        assert EngagementStatus.FAILED == "FAILED"
