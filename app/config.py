"""Application configuration loaded from environment variables."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv


# Approved comments — to be supplied before Phase 8.
# Each comment is used exactly as written. Never modified at runtime.
# DO NOT generate, paraphrase, or alter these at runtime.
APPROVED_COMMENTS: list[str] = [
    "Great practical walkthrough. The implementation details make this especially useful.",
    "Nice explanation of the approach and the trade-offs involved.",
    "This is a helpful example of putting the AWS service into practice.",
    "Really useful breakdown, especially the implementation details.",
    "Good practical write-up. The step-by-step approach makes it easy to follow.",
]

class ConfigError(Exception):
    """Raised when configuration is invalid."""


@dataclass(frozen=True)
class Config:
    """Application configuration. Immutable after creation."""

    # Required
    telegram_bot_token: str
    telegram_chat_id: str

    # Operational
    dry_run: bool = True
    browser_headless: bool = True
    builder_center_url: str = "https://builder.aws.com"
    timezone: str = "Asia/Kolkata"

    # Paths (relative to project root)
    database_path: str = "data/automation.db"
    browser_profile_path: str = "browser-profile"
    log_dir: str = "logs"

    # Logging
    log_level: str = "INFO"

    # Timeouts (milliseconds)
    page_timeout_ms: int = 30000
    selector_timeout_ms: int = 15000

    # Action delays (seconds)
    action_delay_min_s: float = 2.0
    action_delay_max_s: float = 5.0

    @classmethod
    def load(
        cls, env_path: str | None = None, is_setup: bool = False
    ) -> "Config":
        """Load configuration from environment variables.

        Reads a .env file first (if present), then validates all settings.

        Args:
            env_path: Optional explicit path to .env file.
            is_setup: If True, bypasses validation for normal operational secrets
                      (like Telegram credentials) since setup is manual.

        Returns:
            Validated, immutable Config instance.

        Raises:
            ConfigError: If required settings are missing or values are invalid.
        """
        if env_path:
            load_dotenv(env_path)
        else:
            load_dotenv()

        errors: list[str] = []

        # --- Required fields (unless in setup mode) ---
        telegram_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

        if not is_setup:
            if not telegram_bot_token:
                errors.append("TELEGRAM_BOT_TOKEN is required")
            if not telegram_chat_id:
                errors.append("TELEGRAM_CHAT_ID is required")

        # --- DRY_RUN: default True (safe) ---
        dry_run_raw = os.environ.get("DRY_RUN", "true").strip().lower()
        if dry_run_raw in ("", "true", "1", "yes"):
            dry_run = True
        elif dry_run_raw in ("false", "0", "no"):
            dry_run = False
        else:
            errors.append(f"DRY_RUN must be true/false, got: {dry_run_raw!r}")
            dry_run = True

        # --- BROWSER_HEADLESS: default True ---
        browser_headless_raw = os.environ.get("BROWSER_HEADLESS", "true").strip().lower()
        if browser_headless_raw in ("", "true", "1", "yes"):
            browser_headless = True
        elif browser_headless_raw in ("false", "0", "no"):
            browser_headless = False
        else:
            errors.append(f"BROWSER_HEADLESS must be true/false, got: {browser_headless_raw!r}")
            browser_headless = True

        # --- Optional string fields ---
        builder_center_url = os.environ.get(
            "BUILDER_CENTER_URL", "https://builder.aws.com"
        ).strip()

        database_path = os.environ.get(
            "DATABASE_PATH", "data/automation.db"
        ).strip()

        browser_profile_path = os.environ.get(
            "BROWSER_PROFILE_PATH", "browser-profile"
        ).strip()

        log_dir = os.environ.get("LOG_DIR", "logs").strip()

        log_level = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
        valid_levels = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
        if log_level not in valid_levels:
            errors.append(
                f"LOG_LEVEL must be one of {valid_levels}, got: {log_level!r}"
            )

        # --- Timeout fields ---
        page_timeout_ms = _parse_positive_int(
            "PAGE_TIMEOUT_MS",
            os.environ.get("PAGE_TIMEOUT_MS", "30000"),
            errors,
        )
        selector_timeout_ms = _parse_positive_int(
            "SELECTOR_TIMEOUT_MS",
            os.environ.get("SELECTOR_TIMEOUT_MS", "15000"),
            errors,
        )

        # --- Delay fields ---
        action_delay_min_s = _parse_positive_float(
            "ACTION_DELAY_MIN_S",
            os.environ.get("ACTION_DELAY_MIN_S", "2.0"),
            errors,
        )
        action_delay_max_s = _parse_positive_float(
            "ACTION_DELAY_MAX_S",
            os.environ.get("ACTION_DELAY_MAX_S", "5.0"),
            errors,
        )
        if action_delay_min_s > action_delay_max_s:
            errors.append(
                f"ACTION_DELAY_MIN_S ({action_delay_min_s}) must be "
                f"<= ACTION_DELAY_MAX_S ({action_delay_max_s})"
            )

        # --- Report all errors at once ---
        if errors:
            raise ConfigError(
                "Configuration errors:\n"
                + "\n".join(f"  - {e}" for e in errors)
            )

        return cls(
            telegram_bot_token=telegram_bot_token,
            telegram_chat_id=telegram_chat_id,
            dry_run=dry_run,
            browser_headless=browser_headless,
            builder_center_url=builder_center_url,
            database_path=database_path,
            browser_profile_path=browser_profile_path,
            log_dir=log_dir,
            log_level=log_level,
            page_timeout_ms=page_timeout_ms,
            selector_timeout_ms=selector_timeout_ms,
            action_delay_min_s=action_delay_min_s,
            action_delay_max_s=action_delay_max_s,
        )


def _parse_positive_int(
    name: str, raw: str, errors: list[str]
) -> int:
    """Parse a string as a positive integer, appending to errors on failure."""
    raw = raw.strip()
    try:
        value = int(raw)
        if value <= 0:
            errors.append(f"{name} must be positive, got: {value}")
            return 1
        return value
    except ValueError:
        errors.append(f"{name} must be an integer, got: {raw!r}")
        return 1


def _parse_positive_float(
    name: str, raw: str, errors: list[str]
) -> float:
    """Parse a string as a positive float, appending to errors on failure."""
    raw = raw.strip()
    try:
        value = float(raw)
        if value <= 0:
            errors.append(f"{name} must be positive, got: {value}")
            return 1.0
        return value
    except ValueError:
        errors.append(f"{name} must be a number, got: {raw!r}")
        return 1.0
