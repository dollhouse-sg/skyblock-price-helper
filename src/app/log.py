import datetime
import logging
import os
from logging.handlers import RotatingFileHandler


def setup(name: str) -> logging.Logger:
    """Configure a named logger with stdout and rotating file output.

    Idempotent — calling setup() twice for the same name returns the existing
    logger without adding duplicate handlers.

    Args:
        name: Logger name and log file stem (e.g. "bot" writes to logs/bot.log).

    Returns:
        A configured Logger instance.
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    log_date = datetime.date.today().strftime("%Y-%m-%d")
    log_dir = os.path.join(os.environ.get("LOG_DIR", "logs"), log_date)
    os.makedirs(log_dir, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        os.path.join(log_dir, f"{name}.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)

    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)

    return logger
