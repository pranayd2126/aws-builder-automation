import logging
import os
import re
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import Config


class SecretFilter(logging.Filter):
    """Filter out secrets from log records."""

    def __init__(self, secrets: list[str]):
        super().__init__()
        # Keep non-empty secrets
        self.secrets = [s for s in secrets if s]
        
        # Regex to catch accidental logging of secrets not explicitly in config
        self.pattern = re.compile(
            r'(?i)(password|token|secret|mfa_?code|cookie|session_?id)s?[\s]*[:=][\s]*([^\s,;]+)'
        )

    def filter(self, record: logging.LogRecord) -> bool:
        msg = str(record.msg)
        
        # 1. Exact string replacement of known secrets
        for secret in self.secrets:
            if secret in msg:
                msg = msg.replace(secret, "***REDACTED***")
                
        # 2. Regex pattern replacement for common secret patterns
        msg = self.pattern.sub(r'\1=***REDACTED***', msg)

        # 3. Handle arguments if present
        if record.args:
            new_args = []
            for arg in record.args:
                if isinstance(arg, str):
                    arg_str = arg
                    for secret in self.secrets:
                        if secret in arg_str:
                            arg_str = arg_str.replace(secret, "***REDACTED***")
                    arg_str = self.pattern.sub(r'\1=***REDACTED***', arg_str)
                    new_args.append(arg_str)
                else:
                    new_args.append(arg)
            record.args = tuple(new_args)

        record.msg = msg
        return True


class ISTFormatter(logging.Formatter):
    """Format timestamps in configured timezone (default Asia/Kolkata)."""

    def __init__(self, fmt: str, tz_name: str):
        super().__init__(fmt)
        self.tz = ZoneInfo(tz_name)

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        dt = datetime.fromtimestamp(record.created, tz=self.tz)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.isoformat(timespec='milliseconds')


def setup_logger(config: Config, run_id: str) -> logging.Logger:
    """Setup application logger with file and console handlers."""
    logger = logging.getLogger("automation")
    logger.setLevel(getattr(logging, config.log_level))
    logger.propagate = False
    logger.handlers.clear()

    os.makedirs(config.log_dir, exist_ok=True)

    fmt = f"%(asctime)s | {run_id} | %(levelname)s | %(name)s | %(message)s"
    formatter = ISTFormatter(fmt, config.timezone)

    # File handler
    log_file = os.path.join(config.log_dir, f"run_{run_id}.log")
    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setFormatter(formatter)

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    # Setup secrets filter
    secrets = [
        config.telegram_bot_token,
        config.telegram_chat_id,
    ]
    secret_filter = SecretFilter(secrets)
    file_handler.addFilter(secret_filter)
    console_handler.addFilter(secret_filter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger
