"""AWS Builder Center Educational Browser Automation — Entry Point."""

import argparse
import sys
import uuid
from dataclasses import replace

from app import __version__
from app.browser import BrowserManager, AuthState
from app.config import Config, ConfigError
from app.database import DatabaseManager, DatabaseError
from app.discovery import ArticleDiscovery, DiscoveryStatus
from app.engagement import ArticleEngagement, EngagementStatus
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
        "--setup",
        action="store_true",
        help="Launch headed browser and wait for manual Google authentication",
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
    is_setup = getattr(args, "setup", False)
    try:
        config = Config.load(is_setup=is_setup)
    except ConfigError as e:
        print(f"Configuration error:\n{e}", file=sys.stderr)
        return 1

    # CLI --dry-run overrides config (can only force dry run ON, never off)
    if args.dry_run:
        config = replace(config, dry_run=True)

    # CLI --setup forces headed browser for manual login
    if getattr(args, "setup", False):
        config = replace(config, browser_headless=False)

    # 2. Setup logger
    logger = setup_logger(config, run_id)
    
    # 3. Setup lifecycle state machine
    lifecycle = AppLifecycle(run_id, logger)

    try:
        lifecycle.transition(RunPhase.INITIALIZATION)
        
        logger.info(f"AWS Builder Automation v{__version__} starting")
        logger.info(f"Run ID: {run_id}")
        logger.info(f"DRY_RUN: {config.dry_run}")
        logger.info(f"HEADLESS: {config.browser_headless}")
        logger.info(f"Timezone: {config.timezone}")
        logger.info(f"Builder Center: {config.builder_center_url}")
        logger.info(f"Database: {config.database_path}")
        logger.info(f"Browser Profile: {config.browser_profile_path}")
        logger.info("Configuration loaded successfully.")

        # Phase 3 integration: DB initialization
        db = DatabaseManager(config.database_path, logger)
        db.init_db()
        db.record_run_start(run_id, config.dry_run)

        # Phase 4 integration: Browser
        browser_manager = BrowserManager(config, logger)
        
        lifecycle.transition(RunPhase.BROWSER_START)
        
        try:
            with browser_manager.session():
                lifecycle.transition(RunPhase.AUTHENTICATION)
                auth_state = browser_manager.check_session()
                
                if auth_state == AuthState.FAILED:
                    logger.error("Authentication check failed due to browser/network error.")
                    db.record_run_final_status(run_id, "FAILED", "Authentication check failed")
                    lifecycle.transition(RunPhase.FAILED)
                    return 1
                elif auth_state == AuthState.REQUIRED:
                    if getattr(args, "setup", False):
                        logger.info("Setup mode: waiting for manual authentication...")
                        if browser_manager.wait_for_manual_login():
                            auth_state = AuthState.AVAILABLE
                        else:
                            logger.error("Setup mode: manual authentication failed or timed out.")
                            db.record_run_final_status(run_id, "AUTHENTICATION_REQUIRED")
                            lifecycle.transition(RunPhase.FAILED)
                            return 1
                    else:
                        logger.warning("Authentication is REQUIRED. Please run with --setup to login.")
                        db.record_run_final_status(run_id, "AUTHENTICATION_REQUIRED")
                        lifecycle.transition(RunPhase.FAILED)
                        return 1

                if auth_state == AuthState.AVAILABLE:
                    logger.info("Authentication verified successfully.")

                    # Phase 7 constraint: --setup must only verify/save session
                    if getattr(args, "setup", False):
                        logger.info("Setup complete. Authenticated session saved.")
                        db.record_run_final_status(run_id, "SETUP_COMPLETE")
                        lifecycle.transition(RunPhase.CLEANUP)
                        lifecycle.transition(RunPhase.COMPLETED)
                        return 0

                    # Phase 5: Article discovery
                    lifecycle.transition(RunPhase.DISCOVERY)
                    discovery = ArticleDiscovery(config, db, logger)
                    result = discovery.discover(browser_manager._page)

                    if result.status == DiscoveryStatus.SELECTED:
                        article = result.article
                        logger.info(
                            f"Discovery complete: SELECTED article "
                            f"url={article.url}, title={article.title!r}"
                        )
                        # Associate selected article with this run
                        try:
                            db.select_article_for_run(run_id, article.url)
                        except DatabaseError as e:
                            logger.error(
                                f"Failed to associate article with run: {e}",
                                exc_info=True,
                            )
                            db.record_run_final_status(run_id, "FAILED", str(e))
                            lifecycle.transition(RunPhase.FAILED)
                            return 1

                        # Phase 6: Article engagement
                        lifecycle.transition(RunPhase.ENGAGEMENT)
                        engagement = ArticleEngagement(
                            config, db, run_id, logger
                        )
                        eng_result = engagement.engage(
                            browser_manager._page, article
                        )

                        if eng_result.status == EngagementStatus.SUCCESS:
                            logger.info("Engagement completed successfully.")
                            db.record_run_final_status(run_id, "COMPLETED")
                            lifecycle.transition(RunPhase.CLEANUP)
                            lifecycle.transition(RunPhase.COMPLETED)
                            logger.info("Application finished successfully.")
                            return 0

                        elif eng_result.status == EngagementStatus.DRY_RUN:
                            logger.info("Engagement skipped (DRY_RUN mode).")
                            db.record_run_final_status(run_id, "DRY_RUN")
                            lifecycle.transition(RunPhase.CLEANUP)
                            lifecycle.transition(RunPhase.COMPLETED)
                            logger.info("Application finished (dry run).")
                            return 0

                        elif eng_result.status == EngagementStatus.SKIPPED:
                            logger.info("Engagement skipped (already engaged).")
                            db.record_run_final_status(run_id, "SKIPPED")
                            lifecycle.transition(RunPhase.CLEANUP)
                            lifecycle.transition(RunPhase.COMPLETED)
                            logger.info("Application finished (skipped).")
                            return 0

                        elif eng_result.status == EngagementStatus.AUTHENTICATION_REQUIRED:
                            logger.warning(
                                "Engagement detected authentication required."
                            )
                            db.record_run_final_status(
                                run_id, "AUTHENTICATION_REQUIRED"
                            )
                            lifecycle.transition(RunPhase.FAILED)
                            return 1

                        else:
                            # FAILED
                            logger.error(
                                f"Engagement failed: {eng_result.error}"
                            )
                            db.record_run_final_status(
                                run_id, "FAILED",
                                eng_result.error or "Engagement failed",
                            )
                            lifecycle.transition(RunPhase.FAILED)
                            return 1

                    elif result.status == DiscoveryStatus.NO_NEW_ARTICLE:
                        logger.info("Discovery complete: NO_NEW_ARTICLE.")
                        db.record_run_final_status(run_id, "NO_NEW_ARTICLE")
                        lifecycle.transition(RunPhase.CLEANUP)
                        lifecycle.transition(RunPhase.COMPLETED)
                        logger.info("Application finished — no new article to process.")
                        return 0

                    elif result.status == DiscoveryStatus.AUTHENTICATION_REQUIRED:
                        logger.warning("Discovery detected authentication required.")
                        db.record_run_final_status(run_id, "AUTHENTICATION_REQUIRED")
                        lifecycle.transition(RunPhase.FAILED)
                        return 1

                    else:
                        # FAILED or unknown
                        logger.error(f"Discovery failed: {result.error}")
                        db.record_run_final_status(
                            run_id, "FAILED",
                            result.error or "Discovery failed",
                        )
                        lifecycle.transition(RunPhase.FAILED)
                        return 1
                else:
                    logger.error(f"Unknown authentication state: {auth_state}")
                    db.record_run_final_status(run_id, "FAILED", "Unknown auth state")
                    lifecycle.transition(RunPhase.FAILED)
                    return 1
        except Exception as e:
            logger.error(f"Browser session failed: {e}", exc_info=True)
            lifecycle.transition(RunPhase.FAILED)
            return 1

    except Exception as e:
        logger.error(f"Unexpected application error: {e}", exc_info=True)
        lifecycle.transition(RunPhase.FAILED)
        return 1


if __name__ == "__main__":
    sys.exit(main())
