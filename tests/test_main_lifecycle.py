import sys
from unittest.mock import patch, MagicMock

import pytest

from app.main import main
from app.lifecycle import AppLifecycle, RunPhase


from app.browser import AuthState

def test_independent_run_ids(monkeypatch):
    """Test that repeated runs generate independent run IDs."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "dummy")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "dummy")

    run_ids = set()

    with patch("app.main.setup_logger") as mock_setup_logger:
        mock_setup_logger.return_value = MagicMock()

        with patch("app.main.BrowserManager") as mock_bm:
            mock_bm.return_value.check_session.return_value = AuthState.AVAILABLE
            mock_bm.return_value.session.return_value.__enter__ = MagicMock()
            mock_bm.return_value.session.return_value.__exit__ = MagicMock()

            # Run main twice
            with patch.object(sys, "argv", ["main.py", "--dry-run"]):
                assert main() == 0

            with patch.object(sys, "argv", ["main.py", "--dry-run"]):
                assert main() == 0

        assert mock_setup_logger.call_count == 2
        run_ids.add(mock_setup_logger.call_args_list[0][0][1])
        run_ids.add(mock_setup_logger.call_args_list[1][0][1])

        assert len(run_ids) == 2, "Each run should generate a unique run ID"

def test_setup_mode_bypasses_telegram_vars(monkeypatch):
    """Test that --setup allows missing Telegram variables and stops after setup."""
    # Ensure no environment variables are set
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    with patch("app.main.setup_logger") as mock_setup_logger:
        mock_setup_logger.return_value = MagicMock()

        with patch("app.main.BrowserManager") as mock_bm:
            # Fake that authentication is already available
            mock_bm.return_value.check_session.return_value = AuthState.AVAILABLE
            mock_bm.return_value.session.return_value.__enter__ = MagicMock()
            mock_bm.return_value.session.return_value.__exit__ = MagicMock()

            # Discovery should not be instantiated or called
            with patch("app.main.ArticleDiscovery") as mock_disc:
                with patch.object(sys, "argv", ["main.py", "--setup"]):
                    # Should return 0 cleanly without complaining about config
                    assert main() == 0

                # Ensure discovery was not run
                mock_disc.assert_not_called()


def test_main_unexpected_exception_handling(monkeypatch):
    """Test that unexpected exceptions transition to FAILED and return 1."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "dummy")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "dummy")

    # Force an exception during the second transition (COMPLETED)
    with patch("app.main.AppLifecycle.transition") as mock_transition:
        mock_transition.side_effect = [None, None, None, RuntimeError("Boom"), None]

        with patch("app.main.BrowserManager") as mock_bm:
            mock_bm.return_value.check_session.return_value = AuthState.AVAILABLE
            mock_bm.return_value.session.return_value.__enter__ = MagicMock()
            mock_bm.return_value.session.return_value.__exit__ = MagicMock(return_value=False)

            with patch.object(sys, "argv", ["main.py"]):
                exit_code = main()

        assert exit_code == 1
        assert mock_transition.call_args_list[-1][0][0] == RunPhase.FAILED

def test_main_unexpected_exception_clean(monkeypatch):
    """Refined exception handling test without complex mock side_effects."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "dummy")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "dummy")

    original_transition = AppLifecycle.transition

    def buggy_transition(self, phase):
        if phase == RunPhase.COMPLETED:
            raise RuntimeError("Fake transition crash")
        original_transition(self, phase)

    with patch("app.main.AppLifecycle.transition", new=buggy_transition):
        with patch("app.main.BrowserManager") as mock_bm:
            mock_bm.return_value.check_session.return_value = AuthState.AVAILABLE
            mock_bm.return_value.session.return_value.__enter__ = MagicMock()
            mock_bm.return_value.session.return_value.__exit__ = MagicMock(return_value=False)

            with patch.object(sys, "argv", ["main.py"]):
                exit_code = main()

    assert exit_code == 1


def test_lifecycle_transitions():
    """Test that lifecycle records transitions correctly."""
    logger = MagicMock()
    lifecycle = AppLifecycle("run_123", logger)

    assert lifecycle.current_phase is None

    lifecycle.transition(RunPhase.INITIALIZATION)
    assert lifecycle.current_phase == RunPhase.INITIALIZATION

    lifecycle.transition(RunPhase.COMPLETED)
    assert lifecycle.current_phase == RunPhase.COMPLETED

    assert logger.info.call_count == 2

    # Transition to same phase shouldn't log again
    lifecycle.transition(RunPhase.COMPLETED)
    assert logger.info.call_count == 2
