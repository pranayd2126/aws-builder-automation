import logging
from enum import Enum


class RunPhase(str, Enum):
    """Lifecycle phases for the application."""
    INITIALIZATION = "INITIALIZATION"
    BROWSER_START = "BROWSER_START"
    AUTHENTICATION = "AUTHENTICATION"
    DISCOVERY = "DISCOVERY"
    ENGAGEMENT = "ENGAGEMENT"
    PROCESSING = "PROCESSING"
    CLEANUP = "CLEANUP"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class AppLifecycle:
    """Manages application state and transitions."""

    def __init__(self, run_id: str, logger: logging.Logger):
        self.run_id = run_id
        self.logger = logger
        self.current_phase: RunPhase | None = None

    def transition(self, new_phase: RunPhase) -> None:
        """Transition to a new phase and log the event."""
        if self.current_phase != new_phase:
            old_phase = self.current_phase.value if self.current_phase else "None"
            self.logger.info(f"Phase transition: {old_phase} -> {new_phase.value}")
            self.current_phase = new_phase
