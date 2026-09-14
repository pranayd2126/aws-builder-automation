import logging
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Tuple


class DatabaseError(Exception):
    """Base exception for database operations."""


class DatabaseManager:
    """Manages SQLite persistence and state tracking for the application."""

    def __init__(self, db_path: str, logger: logging.Logger):
        self.db_path = db_path
        self.logger = logger

    @contextmanager
    def get_connection(self):
        """Yield a database connection with dictionary-like rows."""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            # Enable foreign keys
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
        except sqlite3.Error as e:
            self.logger.error(f"SQLite connection error: {e}", exc_info=True)
            raise DatabaseError(f"Database connection failed: {e}") from e
        finally:
            if 'conn' in locals():
                conn.close()

    def init_db(self) -> None:
        """Initialize the database schema idempotently."""
        try:
            with self.get_connection() as conn:
                with conn:
                    # articles table
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS articles (
                            url TEXT PRIMARY KEY,
                            title TEXT,
                            author TEXT,
                            pub_date TEXT,
                            discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    """)

                    # runs table
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS runs (
                            run_id TEXT PRIMARY KEY,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            article_url TEXT REFERENCES articles(url),
                            comment_text TEXT,
                            comment_status TEXT,
                            like_status TEXT,
                            final_status TEXT,
                            is_dry_run BOOLEAN NOT NULL DEFAULT 0,
                            error_diagnostics TEXT
                        )
                    """)

                    # comment_usage table
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS comment_usage (
                            comment_text TEXT PRIMARY KEY,
                            usage_count INTEGER DEFAULT 0,
                            last_used_at TIMESTAMP
                        )
                    """)
            self.logger.info("Database schema initialized successfully.")
        except sqlite3.Error as e:
            self.logger.error(f"Failed to initialize database schema: {e}", exc_info=True)
            raise DatabaseError(f"Schema initialization failed: {e}") from e

    def save_article(self, url: str, title: Optional[str] = None, author: Optional[str] = None, pub_date: Optional[str] = None) -> None:
        """Save or update an article."""
        try:
            with self.get_connection() as conn:
                with conn:
                    conn.execute("""
                        INSERT INTO articles (url, title, author, pub_date)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(url) DO UPDATE SET
                            title = excluded.title,
                            author = excluded.author,
                            pub_date = excluded.pub_date
                    """, (url, title, author, pub_date))
        except sqlite3.Error as e:
            self.logger.error(f"Failed to save article {url}: {e}", exc_info=True)
            raise DatabaseError(f"Failed to save article: {e}") from e

    def record_run_start(self, run_id: str, is_dry_run: bool) -> None:
        """Record the start of a run."""
        try:
            with self.get_connection() as conn:
                with conn:
                    conn.execute("""
                        INSERT INTO runs (run_id, is_dry_run)
                        VALUES (?, ?)
                    """, (run_id, is_dry_run))
        except sqlite3.Error as e:
            self.logger.error(f"Failed to record run start for {run_id}: {e}", exc_info=True)
            raise DatabaseError(f"Failed to record run start: {e}") from e

    def is_article_eligible(self, url: str) -> bool:
        """
        Check if an article is eligible for a comment attempt.
        An article is NOT eligible if a REAL comment attempt has already been made,
        even if it failed or crashed midway.
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.execute("""
                    SELECT 1 FROM runs
                    WHERE article_url = ? AND is_dry_run = 0 AND comment_status IN ('ATTEMPTED', 'SUCCESS', 'FAILED')
                    LIMIT 1
                """, (url,))
                row = cursor.fetchone()
                return row is None
        except sqlite3.Error as e:
            self.logger.error(f"Failed to check eligibility for {url}: {e}", exc_info=True)
            raise DatabaseError(f"Failed to check eligibility: {e}") from e

    def select_article_for_run(self, run_id: str, url: str) -> None:
        """Associate a run with a specific article."""
        try:
            with self.get_connection() as conn:
                with conn:
                    conn.execute("""
                        UPDATE runs SET article_url = ?, final_status = 'SELECTED'
                        WHERE run_id = ?
                    """, (url, run_id))
        except sqlite3.Error as e:
            self.logger.error(f"Failed to select article {url} for run {run_id}: {e}", exc_info=True)
            raise DatabaseError(f"Failed to select article: {e}") from e

    def record_comment_attempt(self, run_id: str, comment_text: str) -> None:
        """
        Record that a comment attempt has begun.
        This MUST be called BEFORE the external action occurs to ensure a crash
        will permanently block reselection for real runs.
        """
        try:
            with self.get_connection() as conn:
                with conn:
                    conn.execute("""
                        UPDATE runs SET comment_text = ?, comment_status = 'ATTEMPTED'
                        WHERE run_id = ?
                    """, (comment_text, run_id))
        except sqlite3.Error as e:
            self.logger.error(f"Failed to record comment attempt for run {run_id}: {e}", exc_info=True)
            raise DatabaseError(f"Failed to record comment attempt: {e}") from e

    def record_comment_result(self, run_id: str, status: str, error_diagnostics: Optional[str] = None) -> None:
        """Record the result of a comment attempt (SUCCESS or FAILED)."""
        try:
            with self.get_connection() as conn:
                with conn:
                    conn.execute("""
                        UPDATE runs SET comment_status = ?, error_diagnostics = COALESCE(error_diagnostics || '; ', '') || ?
                        WHERE run_id = ?
                    """, (status, error_diagnostics or '', run_id))
        except sqlite3.Error as e:
            self.logger.error(f"Failed to record comment result for run {run_id}: {e}", exc_info=True)
            raise DatabaseError(f"Failed to record comment result: {e}") from e

    def record_like_attempt(self, run_id: str) -> None:
        """Record that a like attempt has begun."""
        try:
            with self.get_connection() as conn:
                with conn:
                    conn.execute("""
                        UPDATE runs SET like_status = 'ATTEMPTED'
                        WHERE run_id = ?
                    """, (run_id,))
        except sqlite3.Error as e:
            self.logger.error(f"Failed to record like attempt for run {run_id}: {e}", exc_info=True)
            raise DatabaseError(f"Failed to record like attempt: {e}") from e

    def record_like_result(self, run_id: str, status: str, error_diagnostics: Optional[str] = None) -> None:
        """Record the result of a like attempt (e.g., SUCCESS, ALREADY_PRESENT, FAILED)."""
        try:
            with self.get_connection() as conn:
                with conn:
                    conn.execute("""
                        UPDATE runs SET like_status = ?, error_diagnostics = COALESCE(error_diagnostics || '; ', '') || ?
                        WHERE run_id = ?
                    """, (status, error_diagnostics or '', run_id))
        except sqlite3.Error as e:
            self.logger.error(f"Failed to record like result for run {run_id}: {e}", exc_info=True)
            raise DatabaseError(f"Failed to record like result: {e}") from e

    def record_run_final_status(self, run_id: str, status: str, error_diagnostics: Optional[str] = None) -> None:
        """Record the final outcome of the run."""
        try:
            with self.get_connection() as conn:
                with conn:
                    conn.execute("""
                        UPDATE runs SET final_status = ?, error_diagnostics = COALESCE(error_diagnostics || '; ', '') || ?
                        WHERE run_id = ?
                    """, (status, error_diagnostics or '', run_id))
        except sqlite3.Error as e:
            self.logger.error(f"Failed to record final status for run {run_id}: {e}", exc_info=True)
            raise DatabaseError(f"Failed to record final status: {e}") from e

    def get_next_comment(self, approved_comments: List[str], is_dry_run: bool) -> Optional[str]:
        """
        Selects a comment ensuring even distribution and preventing consecutive use.
        For dry runs, we don't update the usage tracking.
        """
        if not approved_comments:
            return None

        try:
            with self.get_connection() as conn:
                with conn:
                    # Sync the comments to ensure they are in the database
                    for comment in approved_comments:
                        conn.execute("""
                            INSERT OR IGNORE INTO comment_usage (comment_text, usage_count)
                            VALUES (?, 0)
                        """, (comment,))
                    
                    # Delete any comments from the table that are no longer in approved_comments
                    placeholders = ','.join(['?'] * len(approved_comments))
                    conn.execute(f"""
                        DELETE FROM comment_usage WHERE comment_text NOT IN ({placeholders})
                    """, approved_comments)

                    # Determine the last used REAL comment (if any)
                    cursor = conn.execute("""
                        SELECT comment_text FROM runs
                        WHERE is_dry_run = 0 AND comment_text IS NOT NULL
                        ORDER BY created_at DESC
                        LIMIT 1
                    """)
                    row = cursor.fetchone()
                    last_used_real_comment = row['comment_text'] if row else None

                    # Query comments sorted by usage_count ASC, last_used_at ASC
                    if len(approved_comments) > 1 and last_used_real_comment:
                        cursor = conn.execute("""
                            SELECT comment_text FROM comment_usage
                            WHERE comment_text != ?
                            ORDER BY usage_count ASC, last_used_at ASC
                            LIMIT 1
                        """, (last_used_real_comment,))
                    else:
                        cursor = conn.execute("""
                            SELECT comment_text FROM comment_usage
                            ORDER BY usage_count ASC, last_used_at ASC
                            LIMIT 1
                        """)
                    
                    row = cursor.fetchone()
                    selected_comment = row['comment_text'] if row else None
                    
                    if selected_comment and not is_dry_run:
                        # Update usage tracking only for real runs
                        conn.execute("""
                            UPDATE comment_usage
                            SET usage_count = usage_count + 1, last_used_at = CURRENT_TIMESTAMP
                            WHERE comment_text = ?
                        """, (selected_comment,))

                    return selected_comment
        except sqlite3.Error as e:
            self.logger.error(f"Failed to get next comment: {e}", exc_info=True)
            raise DatabaseError(f"Failed to get next comment: {e}") from e
