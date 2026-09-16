"""Tests for app.discovery module — Phase 5."""

import logging
import sqlite3
import pytest
from dataclasses import dataclass
from unittest.mock import MagicMock, patch, PropertyMock

from playwright.sync_api import Error as PlaywrightError

from app.config import Config
from app.database import DatabaseManager, DatabaseError
from app.discovery import (
    ArticleCandidate,
    ArticleDiscovery,
    DiscoveryResult,
    DiscoveryStatus,
    extract_article_id,
    normalize_url,
    validate_article_url,
)


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
    )


@pytest.fixture
def logger():
    return logging.getLogger("test_discovery")


@pytest.fixture
def db(test_config):
    """Provides a DatabaseManager with an initialized schema."""
    db = DatabaseManager(test_config.database_path, logger=logging.getLogger("test_db"))
    db.init_db()
    return db


@pytest.fixture
def discovery(test_config, db, logger):
    """Provides an ArticleDiscovery instance."""
    return ArticleDiscovery(test_config, db, logger)


def _make_mock_page(
    url_after_nav="https://community.aws/posts",
    elements=None,
    goto_response=True,
    goto_exception=None,
):
    """Create a mock Playwright Page for discovery tests.

    Args:
        url_after_nav: The URL the page reports after goto.
        elements: List of dicts with element data, e.g.:
            [{"href": "...", "text": "...", "author": "...", "pub_date": "..."}]
        goto_response: If True, goto returns a MagicMock response. If None, returns None.
        goto_exception: If set, goto raises this exception.
    """
    page = MagicMock()

    if goto_exception:
        page.goto.side_effect = goto_exception
    elif goto_response:
        page.goto.return_value = MagicMock()
    else:
        page.goto.return_value = None

    page.url = url_after_nav

    if elements is None:
        elements = []

    mock_elements = []
    for elem_data in elements:
        el = MagicMock()
        el.get_attribute.side_effect = lambda attr, data=elem_data: {
            "href": data.get("href"),
            "aria-label": data.get("aria_label"),
            "title": data.get("title_attr"),
        }.get(attr)
        el.text_content.return_value = elem_data.get("text", "")

        # Mock parent/context for author and pub_date extraction
        parent_handle = MagicMock()
        parent_element = MagicMock()
        parent_handle.as_element.return_value = parent_element

        # Author
        if elem_data.get("author"):
            author_node = MagicMock()
            author_node.text_content.return_value = elem_data["author"]
            parent_element.query_selector.return_value = author_node
        else:
            parent_element.query_selector.return_value = None

        # Pub date
        if elem_data.get("pub_date"):
            time_el = MagicMock()
            time_el.get_attribute.return_value = elem_data["pub_date"]
            time_el.text_content.return_value = elem_data["pub_date"]
            parent_element.query_selector.side_effect = lambda sel, author=elem_data.get("author"), pub=elem_data.get("pub_date"): (
                _author_or_time(sel, author, pub)
            )
        elif not elem_data.get("author"):
            parent_element.query_selector.return_value = None

        el.evaluate_handle.return_value = parent_handle
        mock_elements.append(el)

    page.query_selector_all.return_value = mock_elements
    return page


def _author_or_time(selector, author, pub_date):
    """Helper for side_effect to return author or time node."""
    if "author" in selector and author:
        node = MagicMock()
        node.text_content.return_value = author
        return node
    if selector == "time" and pub_date:
        node = MagicMock()
        node.get_attribute.return_value = pub_date
        node.text_content.return_value = pub_date
        return node
    return None


# ===========================================================================
# 1. URL Normalization
# ===========================================================================

class TestURLNormalization:

    def test_basic_normalization(self):
        url = "https://community.aws/posts/my-article"
        assert normalize_url(url) == "https://community.aws/posts/my-article"

    def test_strips_whitespace(self):
        assert normalize_url("  https://community.aws/posts/abc  ") == "https://community.aws/posts/abc"

    def test_forces_https(self):
        assert normalize_url("http://community.aws/posts/abc") == "https://community.aws/posts/abc"

    def test_lowercases_hostname(self):
        assert normalize_url("https://COMMUNITY.AWS/posts/abc") == "https://community.aws/posts/abc"

    def test_removes_trailing_slash(self):
        assert normalize_url("https://community.aws/posts/abc/") == "https://community.aws/posts/abc"

    def test_removes_query_params(self):
        assert normalize_url("https://community.aws/posts/abc?ref=home") == "https://community.aws/posts/abc"

    def test_removes_fragment(self):
        assert normalize_url("https://community.aws/posts/abc#comments") == "https://community.aws/posts/abc"

    def test_removes_query_and_fragment(self):
        assert normalize_url("https://community.aws/posts/abc?a=1#top") == "https://community.aws/posts/abc"

    def test_empty_string_returns_none(self):
        assert normalize_url("") is None

    def test_none_returns_none(self):
        assert normalize_url(None) is None

    def test_whitespace_only_returns_none(self):
        assert normalize_url("   ") is None

    def test_no_scheme_returns_none(self):
        assert normalize_url("community.aws/posts/abc") is None

    def test_ftp_scheme_returns_none(self):
        assert normalize_url("ftp://community.aws/posts/abc") is None

    def test_javascript_scheme_returns_none(self):
        assert normalize_url("javascript:void(0)") is None

    def test_non_string_returns_none(self):
        assert normalize_url(12345) is None

    def test_preserves_path_case(self):
        """Path components may be case-sensitive (article slugs)."""
        assert normalize_url("https://community.aws/posts/My-Article") == "https://community.aws/posts/My-Article"


# ===========================================================================
# 2. URL Validation
# ===========================================================================

class TestURLValidation:

    def test_valid_community_aws_post(self):
        assert validate_article_url("https://community.aws/posts/my-article") is True

    def test_valid_community_aws_content(self):
        assert validate_article_url("https://community.aws/content/my-article") is True

    def test_valid_community_aws_articles(self):
        assert validate_article_url("https://community.aws/articles/my-article") is True

    def test_valid_builder_aws_com_host(self):
        assert validate_article_url("https://builder.aws.com/content/my-article") is True

    def test_valid_www_subdomain(self):
        assert validate_article_url("https://www.community.aws/posts/my-article") is True

    def test_valid_nested_path(self):
        assert validate_article_url("https://community.aws/posts/category/my-article") is True

    def test_invalid_wrong_host(self):
        assert validate_article_url("https://example.com/posts/my-article") is False

    def test_invalid_root_url(self):
        assert validate_article_url("https://community.aws/") is False

    def test_invalid_no_article_path(self):
        assert validate_article_url("https://community.aws/about") is False

    def test_invalid_http_scheme(self):
        assert validate_article_url("http://community.aws/posts/abc") is False

    def test_invalid_empty_string(self):
        assert validate_article_url("") is False

    def test_invalid_none(self):
        assert validate_article_url(None) is False

    def test_invalid_path_with_special_chars(self):
        assert validate_article_url("https://community.aws/posts/abc@def") is False

    def test_valid_path_with_underscores_and_hyphens(self):
        assert validate_article_url("https://community.aws/posts/my_article-123") is True


# ===========================================================================
# 3. Article ID Extraction
# ===========================================================================

class TestArticleIDExtraction:

    def test_extracts_last_segment(self):
        assert extract_article_id("https://community.aws/posts/my-article") == "my-article"

    def test_extracts_from_nested_path(self):
        assert extract_article_id("https://community.aws/content/cat/my-article") == "my-article"

    def test_empty_url_returns_none(self):
        assert extract_article_id("") is None

    def test_none_url_returns_none(self):
        assert extract_article_id(None) is None

    def test_root_url_returns_none(self):
        assert extract_article_id("https://community.aws") is None


# ===========================================================================
# 4. Valid Article Extraction (discovery with mock page)
# ===========================================================================

class TestValidArticleExtraction:

    def test_extracts_single_valid_article(self, discovery):
        page = _make_mock_page(elements=[{
            "href": "https://community.aws/posts/my-article",
            "text": "My Great Article",
        }])
        result = discovery.discover(page)
        assert result.status == DiscoveryStatus.SELECTED
        assert result.article is not None
        assert result.article.url == "https://community.aws/posts/my-article"
        assert result.article.title == "My Great Article"

    def test_extracts_multiple_articles(self, discovery):
        page = _make_mock_page(elements=[
            {"href": "https://community.aws/posts/article-1", "text": "Article 1"},
            {"href": "https://community.aws/posts/article-2", "text": "Article 2"},
        ])
        result = discovery.discover(page)
        assert result.status == DiscoveryStatus.SELECTED
        assert result.candidates_found == 2
        # Selects first eligible
        assert result.article.url == "https://community.aws/posts/article-1"


# ===========================================================================
# 5. Invalid/Malformed Candidates
# ===========================================================================

class TestInvalidCandidates:

    def test_skips_malformed_url(self, discovery):
        page = _make_mock_page(elements=[
            {"href": "not-a-url", "text": "Bad Article"},
            {"href": "https://community.aws/posts/good-article", "text": "Good Article"},
        ])
        result = discovery.discover(page)
        assert result.status == DiscoveryStatus.SELECTED
        assert result.article.url == "https://community.aws/posts/good-article"

    def test_skips_wrong_host(self, discovery):
        page = _make_mock_page(elements=[
            {"href": "https://example.com/posts/article", "text": "Wrong Host"},
            {"href": "https://community.aws/posts/good-article", "text": "Good"},
        ])
        result = discovery.discover(page)
        assert result.status == DiscoveryStatus.SELECTED
        assert result.article.url == "https://community.aws/posts/good-article"
        assert result.candidates_skipped >= 1

    def test_all_malformed_returns_no_new(self, discovery):
        page = _make_mock_page(elements=[
            {"href": "javascript:void(0)", "text": "Bad 1"},
            {"href": "", "text": "Bad 2"},
        ])
        result = discovery.discover(page)
        assert result.status == DiscoveryStatus.NO_NEW_ARTICLE


# ===========================================================================
# 6. Candidate Deduplication
# ===========================================================================

class TestDeduplication:

    def test_deduplicates_same_url(self, discovery):
        page = _make_mock_page(elements=[
            {"href": "https://community.aws/posts/same-article", "text": "A"},
            {"href": "https://community.aws/posts/same-article", "text": "B"},
        ])
        result = discovery.discover(page)
        assert result.status == DiscoveryStatus.SELECTED
        assert result.candidates_found == 1  # Deduplicated

    def test_deduplicates_url_with_trailing_slash_difference(self, discovery):
        page = _make_mock_page(elements=[
            {"href": "https://community.aws/posts/same-article/", "text": "A"},
            {"href": "https://community.aws/posts/same-article", "text": "B"},
        ])
        result = discovery.discover(page)
        assert result.candidates_found == 1


# ===========================================================================
# 7. SQLite Eligibility
# ===========================================================================

class TestSQLiteEligibility:

    def test_new_article_is_eligible(self, discovery, db):
        page = _make_mock_page(elements=[
            {"href": "https://community.aws/posts/brand-new", "text": "Brand New"},
        ])
        result = discovery.discover(page)
        assert result.status == DiscoveryStatus.SELECTED
        assert result.article.url == "https://community.aws/posts/brand-new"

    def test_article_saved_to_db(self, discovery, db):
        page = _make_mock_page(elements=[
            {"href": "https://community.aws/posts/saved-article", "text": "Saved Article"},
        ])
        discovery.discover(page)
        with db.get_connection() as conn:
            cursor = conn.execute(
                "SELECT title FROM articles WHERE url = ?",
                ("https://community.aws/posts/saved-article",),
            )
            row = cursor.fetchone()
            assert row is not None
            assert row["title"] == "Saved Article"


# ===========================================================================
# 8. Already-Processed Article Skipping
# ===========================================================================

class TestAlreadyProcessedSkipping:

    def test_skips_already_processed(self, discovery, db):
        url = "https://community.aws/posts/old-article"
        # Pre-populate: article exists and has been processed (real comment attempt)
        db.save_article(url, "Old Article")
        db.record_run_start("old-run", is_dry_run=False)
        db.select_article_for_run("old-run", url)
        db.record_comment_attempt("old-run", "Old comment")
        db.record_comment_result("old-run", "SUCCESS")

        page = _make_mock_page(elements=[
            {"href": url, "text": "Old Article"},
        ])
        result = discovery.discover(page)
        assert result.status == DiscoveryStatus.NO_NEW_ARTICLE
        assert result.candidates_skipped == 1


# ===========================================================================
# 9. Selecting a New Article (first blocked, second selected)
# ===========================================================================

class TestFirstBlockedSecondSelected:

    def test_first_blocked_second_selected(self, discovery, db):
        blocked_url = "https://community.aws/posts/blocked-article"
        new_url = "https://community.aws/posts/new-article"

        # Block the first article
        db.save_article(blocked_url, "Blocked Article")
        db.record_run_start("block-run", is_dry_run=False)
        db.select_article_for_run("block-run", blocked_url)
        db.record_comment_attempt("block-run", "comment")
        db.record_comment_result("block-run", "SUCCESS")

        page = _make_mock_page(elements=[
            {"href": blocked_url, "text": "Blocked Article"},
            {"href": new_url, "text": "New Article"},
        ])
        result = discovery.discover(page)
        assert result.status == DiscoveryStatus.SELECTED
        assert result.article.url == new_url
        assert result.candidates_skipped == 1


# ===========================================================================
# 10. NO_NEW_ARTICLE
# ===========================================================================

class TestNoNewArticle:

    def test_empty_listing_returns_no_new(self, discovery):
        page = _make_mock_page(elements=[])
        result = discovery.discover(page)
        assert result.status == DiscoveryStatus.NO_NEW_ARTICLE
        assert result.candidates_found == 0

    def test_all_blocked_returns_no_new(self, discovery, db):
        url1 = "https://community.aws/posts/art-1"
        url2 = "https://community.aws/posts/art-2"

        for url, run_id in [(url1, "r1"), (url2, "r2")]:
            db.save_article(url, "Title")
            db.record_run_start(run_id, is_dry_run=False)
            db.select_article_for_run(run_id, url)
            db.record_comment_attempt(run_id, "c")
            db.record_comment_result(run_id, "SUCCESS")

        page = _make_mock_page(elements=[
            {"href": url1, "text": "A1"},
            {"href": url2, "text": "A2"},
        ])
        result = discovery.discover(page)
        assert result.status == DiscoveryStatus.NO_NEW_ARTICLE
        assert result.candidates_skipped == 2


# ===========================================================================
# 11. AUTHENTICATION_REQUIRED
# ===========================================================================

class TestAuthenticationRequired:

    def test_redirect_to_signin(self, discovery):
        page = _make_mock_page(url_after_nav="https://signin.aws.amazon.com/signin")
        result = discovery.discover(page)
        assert result.status == DiscoveryStatus.AUTHENTICATION_REQUIRED

    def test_redirect_to_login(self, discovery):
        page = _make_mock_page(url_after_nav="https://community.aws/login")
        result = discovery.discover(page)
        assert result.status == DiscoveryStatus.AUTHENTICATION_REQUIRED


# ===========================================================================
# 12. Discovery Failure
# ===========================================================================

class TestDiscoveryFailure:

    def test_navigation_exception(self, discovery):
        page = _make_mock_page(goto_exception=PlaywrightError("Timeout"))
        result = discovery.discover(page)
        assert result.status == DiscoveryStatus.FAILED
        assert result.error is not None

    def test_navigation_returns_none(self, discovery):
        page = _make_mock_page(goto_response=None)
        result = discovery.discover(page)
        assert result.status == DiscoveryStatus.FAILED

    def test_unexpected_exception_during_navigation(self, discovery):
        page = _make_mock_page(goto_exception=RuntimeError("Unexpected"))
        result = discovery.discover(page)
        assert result.status == DiscoveryStatus.FAILED

    def test_query_selector_exception(self, discovery):
        page = _make_mock_page()
        page.query_selector_all.side_effect = PlaywrightError("DOM error")
        result = discovery.discover(page)
        assert result.status == DiscoveryStatus.FAILED


# ===========================================================================
# 13. SQLite Failure
# ===========================================================================

class TestSQLiteFailure:

    def test_db_save_failure_skips_candidate(self, discovery, db):
        """When save_article fails, the candidate is skipped."""
        page = _make_mock_page(elements=[
            {"href": "https://community.aws/posts/article-1", "text": "A1"},
        ])
        with patch.object(db, "save_article", side_effect=DatabaseError("DB write error")):
            result = discovery.discover(page)
        assert result.status == DiscoveryStatus.NO_NEW_ARTICLE
        assert result.candidates_skipped == 1

    def test_db_eligibility_failure_skips_candidate(self, discovery, db):
        """When is_article_eligible fails, the candidate is skipped."""
        page = _make_mock_page(elements=[
            {"href": "https://community.aws/posts/article-1", "text": "A1"},
        ])
        with patch.object(db, "is_article_eligible", side_effect=DatabaseError("DB read error")):
            result = discovery.discover(page)
        assert result.status == DiscoveryStatus.NO_NEW_ARTICLE
        assert result.candidates_skipped == 1

    def test_db_failure_with_fallback_candidate(self, discovery, db):
        """When first candidate has DB error, second candidate can still succeed."""
        page = _make_mock_page(elements=[
            {"href": "https://community.aws/posts/article-1", "text": "A1"},
            {"href": "https://community.aws/posts/article-2", "text": "A2"},
        ])

        call_count = 0
        original_save = db.save_article

        def failing_then_ok(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise DatabaseError("Transient error")
            return original_save(*args, **kwargs)

        with patch.object(db, "save_article", side_effect=failing_then_ok):
            result = discovery.discover(page)

        assert result.status == DiscoveryStatus.SELECTED
        assert result.article.url == "https://community.aws/posts/article-2"


# ===========================================================================
# 14. Metadata Update/Persistence
# ===========================================================================

class TestMetadataPersistence:

    def test_metadata_update_does_not_reset_processing_state(self, discovery, db):
        """Re-discovering an article should update metadata without resetting processing state."""
        url = "https://community.aws/posts/existing-article"

        # Simulate previous processing
        db.save_article(url, "Old Title", "Old Author")
        db.record_run_start("old-run", is_dry_run=False)
        db.select_article_for_run("old-run", url)
        db.record_comment_attempt("old-run", "old comment")
        db.record_comment_result("old-run", "SUCCESS")

        # Re-discover with updated metadata
        page = _make_mock_page(elements=[
            {"href": url, "text": "New Title"},
        ])
        result = discovery.discover(page)

        # Should still be blocked (processed state not reset)
        assert result.status == DiscoveryStatus.NO_NEW_ARTICLE

        # But metadata should be updated
        with db.get_connection() as conn:
            cursor = conn.execute("SELECT title FROM articles WHERE url = ?", (url,))
            row = cursor.fetchone()
            assert row["title"] == "New Title"

        # Runs table should be unchanged
        with db.get_connection() as conn:
            cursor = conn.execute(
                "SELECT comment_status FROM runs WHERE run_id = ?", ("old-run",)
            )
            row = cursor.fetchone()
            assert row["comment_status"] == "SUCCESS"

    def test_discovered_at_preserved_on_update(self, discovery, db):
        """The discovered_at timestamp should not change on metadata update."""
        url = "https://community.aws/posts/timestamp-test"
        db.save_article(url, "Original")

        with db.get_connection() as conn:
            cursor = conn.execute("SELECT discovered_at FROM articles WHERE url = ?", (url,))
            original_ts = cursor.fetchone()["discovered_at"]

        # Re-save with updated title
        db.save_article(url, "Updated")

        with db.get_connection() as conn:
            cursor = conn.execute("SELECT discovered_at FROM articles WHERE url = ?", (url,))
            updated_ts = cursor.fetchone()["discovered_at"]

        assert original_ts == updated_ts


# ===========================================================================
# 15. DRY_RUN Compatibility
# ===========================================================================

class TestDryRunCompatibility:

    def test_discovery_works_in_dry_run(self, test_config, db, logger):
        """Discovery itself does not change behavior based on dry_run.
        DRY_RUN affects engagement (comments, likes), not discovery."""
        from dataclasses import replace
        dry_config = replace(test_config, dry_run=True)
        disc = ArticleDiscovery(dry_config, db, logger)

        page = _make_mock_page(elements=[
            {"href": "https://community.aws/posts/dry-article", "text": "Dry Article"},
        ])
        result = disc.discover(page)
        assert result.status == DiscoveryStatus.SELECTED
        assert result.article.url == "https://community.aws/posts/dry-article"


# ===========================================================================
# 16. No Engagement Actions Occur
# ===========================================================================

class TestNoEngagementActions:

    def test_no_comment_or_like_recorded_during_discovery(self, discovery, db):
        """Discovery must not create any comment or like records."""
        page = _make_mock_page(elements=[
            {"href": "https://community.aws/posts/article-1", "text": "A1"},
        ])
        discovery.discover(page)

        with db.get_connection() as conn:
            # No runs should have comment or like status
            cursor = conn.execute(
                "SELECT count(*) as c FROM runs WHERE comment_status IS NOT NULL"
            )
            assert cursor.fetchone()["c"] == 0

            cursor = conn.execute(
                "SELECT count(*) as c FROM runs WHERE like_status IS NOT NULL"
            )
            assert cursor.fetchone()["c"] == 0

    def test_page_not_clicked_or_typed(self, discovery):
        """The page should not receive any click, fill, or type actions."""
        page = _make_mock_page(elements=[
            {"href": "https://community.aws/posts/article-1", "text": "A1"},
        ])
        discovery.discover(page)

        # Verify no engagement actions on the page
        page.click.assert_not_called()
        page.fill.assert_not_called()
        page.type.assert_not_called()


# ===========================================================================
# 17. Discovery Result States
# ===========================================================================

class TestDiscoveryResultStates:

    def test_selected_result_has_article(self, discovery):
        page = _make_mock_page(elements=[
            {"href": "https://community.aws/posts/article-1", "text": "A1"},
        ])
        result = discovery.discover(page)
        assert result.status == DiscoveryStatus.SELECTED
        assert result.article is not None
        assert result.candidates_found > 0

    def test_no_new_article_result_has_no_article(self, discovery):
        page = _make_mock_page(elements=[])
        result = discovery.discover(page)
        assert result.status == DiscoveryStatus.NO_NEW_ARTICLE
        assert result.article is None

    def test_auth_required_result(self, discovery):
        page = _make_mock_page(url_after_nav="https://signin.example.com/login")
        result = discovery.discover(page)
        assert result.status == DiscoveryStatus.AUTHENTICATION_REQUIRED
        assert result.article is None

    def test_failed_result_has_error(self, discovery):
        page = _make_mock_page(goto_exception=PlaywrightError("Network error"))
        result = discovery.discover(page)
        assert result.status == DiscoveryStatus.FAILED
        assert result.error is not None
        assert result.article is None


# ===========================================================================
# 18. Listing URL construction
# ===========================================================================

class TestListingURL:

    def test_listing_url_built_correctly(self, discovery):
        url = discovery._build_listing_url()
        assert url == "https://community.aws/"

    def test_listing_url_strips_trailing_slash(self, test_config, db, logger):
        from dataclasses import replace
        config = replace(test_config, builder_center_url="https://community.aws/")
        disc = ArticleDiscovery(config, db, logger)
        assert disc._build_listing_url() == "https://community.aws/"


# ===========================================================================
# 19. Relative URL handling
# ===========================================================================

class TestRelativeURLHandling:

    def test_relative_url_resolved(self, discovery):
        page = _make_mock_page(
            url_after_nav="https://community.aws/posts",
            elements=[
                {"href": "/posts/relative-article", "text": "Relative Article"},
            ],
        )
        result = discovery.discover(page)
        assert result.status == DiscoveryStatus.SELECTED
        assert result.article.url == "https://community.aws/posts/relative-article"


# ===========================================================================
# 20. DiscoveryStatus enum values
# ===========================================================================

class TestDiscoveryStatusEnum:

    def test_all_statuses_are_strings(self):
        for status in DiscoveryStatus:
            assert isinstance(status, str)

    def test_expected_statuses_exist(self):
        assert DiscoveryStatus.SELECTED == "SELECTED"
        assert DiscoveryStatus.NO_NEW_ARTICLE == "NO_NEW_ARTICLE"
        assert DiscoveryStatus.AUTHENTICATION_REQUIRED == "AUTHENTICATION_REQUIRED"
        assert DiscoveryStatus.FAILED == "FAILED"
