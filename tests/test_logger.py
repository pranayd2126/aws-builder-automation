import logging
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from app.logger import SecretFilter, ISTFormatter, setup_logger
from app.config import Config


def test_secret_filter_exact_match():
    filter = SecretFilter(["my_super_secret_token", "another_secret"])
    record = logging.LogRecord("name", logging.INFO, "pathname", 1, "Connecting with my_super_secret_token to server", None, None)
    filter.filter(record)
    assert record.msg == "Connecting with ***REDACTED*** to server"

    # Test with string formatting args
    record = logging.LogRecord("name", logging.INFO, "pathname", 1, "Token: %s", ("my_super_secret_token",), None)
    filter.filter(record)
    assert record.args == ("***REDACTED***",)


def test_secret_filter_regex():
    filter = SecretFilter(["some_secret"])
    
    # Test common patterns
    cases = [
        ("password=12345", "password=***REDACTED***"),
        ("Token: ABC-DEF", "Token=***REDACTED***"),
        ("MFA_code: 987654", "MFA_code=***REDACTED***"),
        ("cookie=session_id_value", "cookie=***REDACTED***"),
        ("session_id=123", "session_id=***REDACTED***"),
    ]
    
    for input_msg, expected_msg in cases:
        record = logging.LogRecord("name", logging.INFO, "pathname", 1, input_msg, None, None)
        filter.filter(record)
        assert record.msg == expected_msg


def test_ist_formatter():
    formatter = ISTFormatter("%(asctime)s - %(message)s", "Asia/Kolkata")
    record = logging.LogRecord("name", logging.INFO, "pathname", 1, "test message", None, None)
    # Mock the created time to a known timestamp (e.g., 2024-01-01 12:00:00 UTC)
    record.created = 1704110400.0
    
    formatted = formatter.format(record)
    assert "2024-01-01T17:30:00.000+05:30" in formatted


def test_setup_logger_creates_file(tmp_path):
    config = Config(
        telegram_bot_token="bot_token",
        telegram_chat_id="chat_id",
        log_dir=str(tmp_path / "logs"),
        timezone="Asia/Kolkata"
    )
    
    run_id = "test_run_123"
    logger = setup_logger(config, run_id)
    
    # Log a message
    logger.info("Test log message with bot_token")
    
    log_file = tmp_path / "logs" / f"run_{run_id}.log"
    assert log_file.exists()
    
    content = log_file.read_text(encoding="utf-8")
    assert "Test log message with ***REDACTED***" in content
    assert "test_run_123" in content
