"""Article discovery and eligibility for AWS Builder Center.

Navigates the Builder Center article listing, discovers candidates,
extracts metadata, checks SQLite eligibility, and selects one new article.
"""

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional
from urllib.parse import urlparse, urlunparse, urljoin

from playwright.sync_api import Page, Error as PlaywrightError

from app.config import Config
from app.database import DatabaseManager, DatabaseError
from app.browser import is_auth_redirect_url


class DiscoveryStatus(str, Enum):
    """Result states for article discovery."""
    SELECTED = "SELECTED"
    NO_NEW_ARTICLE = "NO_NEW_ARTICLE"
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    FAILED = "FAILED"


@dataclass
class ArticleCandidate:
    """Represents a discovered article candidate."""
    url: str
    title: Optional[str] = None
    author: Optional[str] = None
    pub_date: Optional[str] = None
    article_id: Optional[str] = None


@dataclass
class DiscoveryResult:
    """Result of the discovery process."""
    status: DiscoveryStatus
    article: Optional[ArticleCandidate] = None
    candidates_found: int = 0
    candidates_skipped: int = 0
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# URL normalization and validation
# ---------------------------------------------------------------------------

# Patterns for valid Builder Center article URLs
_BUILDER_CENTER_HOSTS = frozenset({
    "community.aws",
    "www.community.aws",
    "builder.aws.com",
})

_BUILDER_CENTER_PATH_PATTERN = re.compile(
    r"^/(?:content|posts|articles)/[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*$"
)


def normalize_url(raw_url: str) -> Optional[str]:
    """Normalize an article URL.

    - Strip whitespace
    - Parse components
    - Force HTTPS scheme
    - Lowercase hostname
    - Remove trailing slashes from path
    - Remove query string and fragment
    - Return None if the URL is fundamentally malformed

    Args:
        raw_url: The raw URL string.

    Returns:
        Normalized URL string, or None if malformed.
    """
    if not raw_url or not isinstance(raw_url, str):
        return None

    raw_url = raw_url.strip()
    if not raw_url:
        return None

    try:
        parsed = urlparse(raw_url)
    except Exception:
        return None

    # Must have a scheme and netloc
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        return None

    netloc = parsed.netloc.lower()
    if not netloc:
        return None

    # Normalize path: remove trailing slashes, collapse double slashes
    path = parsed.path.rstrip("/")
    if not path:
        path = ""

    # Strip query params and fragments — URL is the canonical identity
    normalized = urlunparse((
        "https",    # Always HTTPS
        netloc,
        path,
        "",         # params
        "",         # query
        "",         # fragment
    ))

    return normalized


def validate_article_url(url: str) -> bool:
    """Validate that a URL looks like a Builder Center article URL.

    Checks:
    - Valid scheme (https)
    - Known Builder Center hostname
    - Path matches expected article URL pattern
    - URL is not empty or just the root

    Args:
        url: A normalized URL string.

    Returns:
        True if the URL appears to be a valid article URL.
    """
    if not url:
        return False

    try:
        parsed = urlparse(url)
    except Exception:
        return False

    if parsed.scheme != "https":
        return False

    hostname = parsed.hostname
    if not hostname:
        return False

    if hostname not in _BUILDER_CENTER_HOSTS:
        return False

    path = parsed.path
    if not path or path == "/":
        return False

    if not _BUILDER_CENTER_PATH_PATTERN.match(path):
        return False

    return True


def extract_article_id(url: str) -> Optional[str]:
    """Extract a useful article identifier from a URL.

    Takes the last meaningful path segment as the article ID.

    Args:
        url: A normalized article URL.

    Returns:
        Article identifier string, or None.
    """
    if not url:
        return None

    try:
        parsed = urlparse(url)
        path = parsed.path.strip("/")
        if not path:
            return None
        segments = path.split("/")
        # Last segment is the article slug/ID
        return segments[-1] if segments else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Article discovery
# ---------------------------------------------------------------------------

class ArticleDiscovery:
    """Discovers and selects articles from the AWS Builder Center."""

    # CSS selectors for the article listing — kept as class-level constants
    # so tests can verify them without hard-coding selectors elsewhere.
    LISTING_PATH = "/"
    ARTICLE_LINK_SELECTOR = 'a[href*="/posts/"], a[href*="/content/"], a[href*="/articles/"]'

    def __init__(
        self,
        config: Config,
        db: DatabaseManager,
        logger: logging.Logger,
    ):
        self.config = config
        self.db = db
        self.logger = logger

    def discover(self, page: Page) -> DiscoveryResult:
        """Run the full discovery pipeline.

        1. Navigate to the article listing
        2. Check for authentication issues
        3. Extract article candidates
        4. Normalize and validate each candidate
        5. Check eligibility against SQLite
        6. Select the first suitable new article
        7. Persist metadata and return result

        Args:
            page: An active Playwright Page from the authenticated session.

        Returns:
            DiscoveryResult with status and optional selected article.
        """
        self.logger.info("Article discovery started.")

        # Step 1: Navigate to the article listing
        try:
            listing_url = self._build_listing_url()
            self.logger.info(f"Navigating to article listing: {listing_url}")

            response = page.goto(listing_url)
            if not response:
                self.logger.error("Navigation to article listing failed (no response).")
                return DiscoveryResult(
                    status=DiscoveryStatus.FAILED,
                    error="Navigation returned no response",
                )

            page.wait_for_load_state("networkidle", timeout=self.config.page_timeout_ms)

        except PlaywrightError as e:
            self.logger.error(f"Failed to navigate to article listing: {e}", exc_info=True)
            return DiscoveryResult(
                status=DiscoveryStatus.FAILED,
                error=f"Navigation failed: {e}",
            )
        except Exception as e:
            self.logger.error(f"Unexpected error navigating to listing: {e}", exc_info=True)
            return DiscoveryResult(
                status=DiscoveryStatus.FAILED,
                error=f"Unexpected navigation error: {e}",
            )

        # Step 2: Check for authentication redirect
        current_url = page.url.lower()
        if is_auth_redirect_url(current_url):
            self.logger.info("Redirected to login page during discovery. Authentication required.")
            return DiscoveryResult(
                status=DiscoveryStatus.AUTHENTICATION_REQUIRED,
                error="Redirected to login page",
            )

        # Step 3: Extract candidates from the page
        try:
            candidates = self._extract_candidates(page)
        except PlaywrightError as e:
            self.logger.error(f"Failed to extract article candidates: {e}", exc_info=True)
            return DiscoveryResult(
                status=DiscoveryStatus.FAILED,
                error=f"Candidate extraction failed: {e}",
            )
        except Exception as e:
            self.logger.error(f"Unexpected error extracting candidates: {e}", exc_info=True)
            return DiscoveryResult(
                status=DiscoveryStatus.FAILED,
                error=f"Unexpected extraction error: {e}",
            )

        self.logger.info(f"Discovered {len(candidates)} candidate(s).")

        if not candidates:
            self.logger.info("No article candidates found on the listing page.")
            return DiscoveryResult(
                status=DiscoveryStatus.NO_NEW_ARTICLE,
                candidates_found=0,
            )

        # Step 4-7: Process candidates and select
        return self._process_candidates(candidates)

    def _build_listing_url(self) -> str:
        """Build the full listing URL from the configured base URL."""
        base = self.config.builder_center_url.rstrip("/")
        return f"{base}{self.LISTING_PATH}"

    def _extract_candidates(self, page: Page) -> List[ArticleCandidate]:
        """Extract article candidates from the current page.

        Finds all article links, extracts their href and any available
        metadata (title, author, publication date).

        Args:
            page: The Playwright page positioned on the article listing.

        Returns:
            List of ArticleCandidate objects (may contain invalid entries).
        """
        candidates: List[ArticleCandidate] = []
        seen_urls: set = set()

        elements = page.query_selector_all(self.ARTICLE_LINK_SELECTOR)

        for element in elements:
            try:
                href = element.get_attribute("href")
                if not href:
                    continue

                # Build absolute URL
                base_url = page.url
                if href.startswith("/"):
                    parsed_base = urlparse(base_url)
                    href = f"{parsed_base.scheme}://{parsed_base.netloc}{href}"
                elif not href.startswith("http"):
                    href = urljoin(base_url, href)

                # Normalize
                normalized = normalize_url(href)
                if not normalized:
                    self.logger.debug(f"Skipping malformed URL: {href}")
                    continue

                # Deduplicate within this discovery run
                if normalized in seen_urls:
                    continue
                seen_urls.add(normalized)

                # Extract metadata from element and surrounding context
                title = self._extract_title(element)
                author = self._extract_author(element)
                pub_date = self._extract_pub_date(element)
                article_id = extract_article_id(normalized)

                candidate = ArticleCandidate(
                    url=normalized,
                    title=title,
                    author=author,
                    pub_date=pub_date,
                    article_id=article_id,
                )
                candidates.append(candidate)

            except PlaywrightError:
                self.logger.debug("Error extracting data from an article element, skipping.")
                continue
            except Exception:
                self.logger.debug("Unexpected error processing an article element, skipping.")
                continue

        return candidates

    def _extract_title(self, element) -> Optional[str]:
        """Extract title from an article link element."""
        try:
            # Try the link text itself
            text = element.text_content()
            if text and text.strip():
                return text.strip()

            # Try aria-label
            label = element.get_attribute("aria-label")
            if label and label.strip():
                return label.strip()

            # Try title attribute
            title_attr = element.get_attribute("title")
            if title_attr and title_attr.strip():
                return title_attr.strip()
        except Exception:
            pass
        return None

    def _extract_author(self, element) -> Optional[str]:
        """Extract author from the article element's context."""
        try:
            # Try parent or sibling elements with common author patterns
            parent = element.evaluate_handle("el => el.closest('article') || el.parentElement")
            if parent:
                # Look for elements with author-like attributes
                author_el = parent.as_element()
                if author_el:
                    author_node = author_el.query_selector(
                        '[class*="author"], [data-author], [rel="author"]'
                    )
                    if author_node:
                        text = author_node.text_content()
                        if text and text.strip():
                            return text.strip()
        except Exception:
            pass
        return None

    def _extract_pub_date(self, element) -> Optional[str]:
        """Extract publication date from the article element's context."""
        try:
            parent = element.evaluate_handle("el => el.closest('article') || el.parentElement")
            if parent:
                parent_el = parent.as_element()
                if parent_el:
                    # Look for time element or date-like attributes
                    time_el = parent_el.query_selector("time")
                    if time_el:
                        datetime_attr = time_el.get_attribute("datetime")
                        if datetime_attr and datetime_attr.strip():
                            return datetime_attr.strip()
                        text = time_el.text_content()
                        if text and text.strip():
                            return text.strip()
        except Exception:
            pass
        return None

    def _process_candidates(
        self,
        candidates: List[ArticleCandidate],
    ) -> DiscoveryResult:
        """Process candidates: validate, check eligibility, persist, and select.

        Args:
            candidates: List of extracted article candidates.

        Returns:
            DiscoveryResult with the outcome.
        """
        total = len(candidates)
        skipped = 0

        for candidate in candidates:
            self.logger.info(
                f"Evaluating candidate: url={candidate.url}, title={candidate.title!r}"
            )

            # Validate URL
            if not validate_article_url(candidate.url):
                self.logger.info(
                    f"Skipping candidate (invalid URL): {candidate.url}"
                )
                skipped += 1
                continue

            # Persist/update metadata — must not reset processing state
            try:
                self.db.save_article(
                    url=candidate.url,
                    title=candidate.title,
                    author=candidate.author,
                    pub_date=candidate.pub_date,
                )
                self.logger.info(f"Article metadata saved/updated: {candidate.url}")
            except DatabaseError as e:
                self.logger.error(
                    f"Failed to save article metadata for {candidate.url}: {e}",
                    exc_info=True,
                )
                skipped += 1
                continue

            # Check eligibility
            try:
                eligible = self.db.is_article_eligible(candidate.url)
            except DatabaseError as e:
                self.logger.error(
                    f"Failed to check eligibility for {candidate.url}: {e}",
                    exc_info=True,
                )
                skipped += 1
                continue

            if not eligible:
                self.logger.info(
                    f"Skipping candidate (already processed): {candidate.url}"
                )
                skipped += 1
                continue

            # This candidate is eligible — select it
            self.logger.info(
                f"Selected article: url={candidate.url}, "
                f"title={candidate.title!r}, "
                f"author={candidate.author!r}, "
                f"pub_date={candidate.pub_date!r}, "
                f"article_id={candidate.article_id!r}"
            )
            return DiscoveryResult(
                status=DiscoveryStatus.SELECTED,
                article=candidate,
                candidates_found=total,
                candidates_skipped=skipped,
            )

        # No eligible candidate found
        self.logger.info(
            f"No new eligible article found. "
            f"Candidates: {total}, skipped: {skipped}."
        )
        return DiscoveryResult(
            status=DiscoveryStatus.NO_NEW_ARTICLE,
            candidates_found=total,
            candidates_skipped=skipped,
        )
