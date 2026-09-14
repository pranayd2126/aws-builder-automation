"""AWS Builder Center Educational Browser Automation — Entry Point."""

import argparse
import sys
from dataclasses import replace

from app import __version__
from app.config import Config, ConfigError


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="AWS Builder Center Educational Browser Automation",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=None,
        help="Force DRY_RUN mode (simulate without real engagement)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser.parse_args()


def main() -> int:
    """Main entry point. Returns exit code (0 = success, 1 = error)."""
    args = parse_args()

    # Load and validate configuration
    try:
        config = Config.load()
    except ConfigError as e:
        print(f"Configuration error:\n{e}", file=sys.stderr)
        return 1

    # CLI --dry-run overrides config (can only force dry run ON, never off)
    if args.dry_run:
        config = replace(config, dry_run=True)

    # Phase 1: Basic startup confirmation
    # (Logging, database, browser, and orchestration added in later phases)
    print(f"AWS Builder Automation v{__version__}")
    print(f"DRY_RUN: {config.dry_run}")
    print(f"Timezone: {config.timezone}")
    print(f"Builder Center: {config.builder_center_url}")
    print(f"Database: {config.database_path}")
    print(f"Log level: {config.log_level}")
    print("Configuration loaded successfully.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
