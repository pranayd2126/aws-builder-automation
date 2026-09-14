"""AWS Builder Center Educational Browser Automation — Entry Point."""

import argparse
import sys
import uuid
from dataclasses import replace

from app import __version__
from app.config import Config, ConfigError
from app.logger import setup_logger
from app.lifecycle import AppLifecycle, RunPhase


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
    # 1. Generate unique run ID
    run_id = uuid.uuid4().hex

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

    # 2. Setup logger
    logger = setup_logger(config, run_id)
    
    # 3. Setup lifecycle state machine
    lifecycle = AppLifecycle(run_id, logger)

    try:
        lifecycle.transition(RunPhase.INITIALIZATION)
        
        logger.info(f"AWS Builder Automation v{__version__} starting")
        logger.info(f"Run ID: {run_id}")
        logger.info(f"DRY_RUN: {config.dry_run}")
        logger.info(f"Timezone: {config.timezone}")
        logger.info(f"Builder Center: {config.builder_center_url}")
        logger.info(f"Database: {config.database_path}")
        logger.info("Configuration loaded successfully.")

        lifecycle.transition(RunPhase.COMPLETED)
        logger.info("Application finished successfully.")
        return 0

    except Exception as e:
        logger.error(f"Unexpected application error: {e}", exc_info=True)
        lifecycle.transition(RunPhase.FAILED)
        return 1


if __name__ == "__main__":
    sys.exit(main())
