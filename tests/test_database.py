import logging
import sqlite3
import pytest
from app.database import DatabaseManager, DatabaseError


@pytest.fixture
def memory_db(tmp_path):
    """Fixture to provide a DatabaseManager with an in-memory or temporary database."""
    # We use a file-based temporary DB instead of :memory: to test connection closing/reopening easily
    db_path = tmp_path / "test.db"
    logger = logging.getLogger("test")
    db = DatabaseManager(str(db_path), logger)
    db.init_db()
    return db


def test_initialization_idempotent(memory_db):
    """Test schema initialization being idempotent."""
    # Should not raise an exception when called multiple times
    memory_db.init_db()
    memory_db.init_db()
    
    with memory_db.get_connection() as conn:
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row['name'] for row in cursor.fetchall()}
    assert 'articles' in tables
    assert 'runs' in tables
    assert 'comment_usage' in tables


def test_article_url_uniqueness(memory_db):
    """Test article URL uniqueness/deduplication and same URL discovered twice."""
    memory_db.save_article("https://example.com/1", "Title 1")
    # Same URL, should update, not throw
    memory_db.save_article("https://example.com/1", "Title 1 Updated")
    
    with memory_db.get_connection() as conn:
        cursor = conn.execute("SELECT * FROM articles WHERE url = ?", ("https://example.com/1",))
        rows = cursor.fetchall()
        assert len(rows) == 1
        assert rows[0]['title'] == "Title 1 Updated"


def test_dry_run_does_not_block_future_real(memory_db):
    """Test dry-run processing does not permanently block future real processing."""
    url = "https://example.com/dryrun"
    memory_db.save_article(url, "Test")
    
    # Dry run attempt
    memory_db.record_run_start("run1", is_dry_run=True)
    memory_db.select_article_for_run("run1", url)
    memory_db.record_comment_attempt("run1", "Great post!")
    memory_db.record_comment_result("run1", "SUCCESS")
    
    # Should still be eligible
    assert memory_db.is_article_eligible(url) is True


def test_real_comment_success_blocks(memory_db):
    """Test real comment SUCCESS blocks future automatic selection."""
    url = "https://example.com/success"
    memory_db.save_article(url, "Test")
    
    # Real run attempt
    memory_db.record_run_start("run1", is_dry_run=False)
    memory_db.select_article_for_run("run1", url)
    memory_db.record_comment_attempt("run1", "Great post!")
    memory_db.record_comment_result("run1", "SUCCESS")
    
    # Should be blocked
    assert memory_db.is_article_eligible(url) is False


def test_real_comment_failed_blocks(memory_db):
    """Test real comment FAILED blocks future automatic selection."""
    url = "https://example.com/failed"
    memory_db.save_article(url, "Test")
    
    # Real run attempt
    memory_db.record_run_start("run1", is_dry_run=False)
    memory_db.select_article_for_run("run1", url)
    memory_db.record_comment_attempt("run1", "Great post!")
    memory_db.record_comment_result("run1", "FAILED")
    
    # Should be blocked
    assert memory_db.is_article_eligible(url) is False


def test_selected_no_engagement_remains_eligible(memory_db):
    """Test selected article with no engagement attempt can remain eligible."""
    url = "https://example.com/selected"
    memory_db.save_article(url, "Test")
    
    memory_db.record_run_start("run1", is_dry_run=False)
    memory_db.select_article_for_run("run1", url)
    # No comment attempt made...
    
    assert memory_db.is_article_eligible(url) is True


def test_crash_after_comment_attempt(memory_db):
    """Test crash after comment attempt/state reservation blocks reselection."""
    url = "https://example.com/crash"
    memory_db.save_article(url, "Test")
    
    memory_db.record_run_start("run1", is_dry_run=False)
    memory_db.select_article_for_run("run1", url)
    # Record that attempt has begun
    memory_db.record_comment_attempt("run1", "Attempting...")
    # Simulate crash by not recording result
    
    assert memory_db.is_article_eligible(url) is False


def test_comment_success_like_failed(memory_db):
    """Test comment SUCCESS + like FAILED."""
    url = "https://example.com/like_fail"
    memory_db.save_article(url, "Test")
    
    memory_db.record_run_start("run1", is_dry_run=False)
    memory_db.select_article_for_run("run1", url)
    memory_db.record_comment_attempt("run1", "Great post!")
    memory_db.record_comment_result("run1", "SUCCESS")
    memory_db.record_like_attempt("run1")
    memory_db.record_like_result("run1", "FAILED", "Timeout")
    memory_db.record_run_final_status("run1", "COMPLETED")
    
    with memory_db.get_connection() as conn:
        cursor = conn.execute("SELECT * FROM runs WHERE run_id = ?", ("run1",))
        row = cursor.fetchone()
        assert row['comment_status'] == "SUCCESS"
        assert row['like_status'] == "FAILED"
        assert "Timeout" in row['error_diagnostics']


def test_independent_runs_receive_unique_ids(memory_db):
    """Test independent runs receive unique IDs."""
    memory_db.record_run_start("run1", is_dry_run=False)
    memory_db.record_run_start("run2", is_dry_run=False)
    
    with memory_db.get_connection() as conn:
        cursor = conn.execute("SELECT count(*) as c FROM runs")
        assert cursor.fetchone()['c'] == 2


def test_consecutive_comment_prevention(memory_db):
    """Test consecutive comment prevention and distribution."""
    comments = ["C1", "C2", "C3"]
    
    c1 = memory_db.get_next_comment(comments, is_dry_run=False)
    # Simulate a run with c1
    memory_db.record_run_start("run1", is_dry_run=False)
    memory_db.record_comment_attempt("run1", c1)
    
    c2 = memory_db.get_next_comment(comments, is_dry_run=False)
    assert c2 != c1
    memory_db.record_run_start("run2", is_dry_run=False)
    memory_db.record_comment_attempt("run2", c2)
    
    c3 = memory_db.get_next_comment(comments, is_dry_run=False)
    assert c3 != c1 and c3 != c2


def test_dry_run_comment_tracking(memory_db):
    """Dry run should not update usage count."""
    comments = ["C1"]
    memory_db.get_next_comment(comments, is_dry_run=True)
    with memory_db.get_connection() as conn:
        cursor = conn.execute("SELECT usage_count FROM comment_usage WHERE comment_text = 'C1'")
        assert cursor.fetchone()['usage_count'] == 0


def test_database_persistence(tmp_path):
    """Test database persistence after closing and reopening."""
    db_path = tmp_path / "persist.db"
    logger = logging.getLogger("test")
    db1 = DatabaseManager(str(db_path), logger)
    db1.init_db()
    db1.save_article("https://example.com/persist", "Title")
    
    db2 = DatabaseManager(str(db_path), logger)
    with db2.get_connection() as conn:
        cursor = conn.execute("SELECT * FROM articles WHERE url = ?", ("https://example.com/persist",))
        assert len(cursor.fetchall()) == 1


def test_sqlite_errors_surfaced():
    """Test SQLite errors are surfaced correctly."""
    logger = logging.getLogger("test")
    # Invalid path
    db = DatabaseManager("/invalid/path/that/does/not/exist/db.sqlite", logger)
    with pytest.raises(DatabaseError):
        db.init_db()


def test_foreign_key_constraint(memory_db):
    """Test transaction rollback/errors on foreign key violation."""
    memory_db.record_run_start("run1", is_dry_run=False)
    
    with pytest.raises(DatabaseError):
        # Attempt to link to non-existent article
        memory_db.select_article_for_run("run1", "https://doesnotexist.com")
