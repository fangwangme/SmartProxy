#! /usr/bin/python3
# coding:utf-8

"""
@author: Fang Wang
@date: 2025-08-02 (Upgraded)
@desc:
This module configures the logging for the entire application using Loguru.
It provides a logger that outputs to both the console with colors and to a
daily rotating file with compression and retention policies.
"""

import sys
import os
from pathlib import Path
from loguru import logger

# --- Configuration ---
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOG_DIR = PROJECT_ROOT / ".local" / "logs"
DEFAULT_LOG_FILE_BASE_NAME = "proxy"


def _resolve_log_dir(log_dir=None) -> Path:
    if not log_dir:
        return DEFAULT_LOG_DIR

    path = Path(log_dir).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def setup_logging(level="INFO", log_dir=None, log_file_base_name=None):
    """
    Configures the logger with the specified log level.
    """
    # --- Initialize Logger ---
    # First, remove any default handlers to start with a clean configuration.
    logger.remove()

    # --- Configure Console Log Handler ---
    # This handler is responsible for displaying colorful, readable logs in the terminal.
    # Only enabled when running interactively (stderr is a TTY),
    # to avoid duplicate logs when running as a background service with redirected output.
    if sys.stderr.isatty():
        logger.add(
            sys.stderr,  # Sink: standard error stream
            level=level,  # Log level
            format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
            "<level>{message}</level>",
            colorize=True,  # Enable colorization
        )

    # --- Configure File Log Handler ---
    # Keep logs inside the project by default so imports and tests do not require
    # system-level write permissions.
    base_name = log_file_base_name or DEFAULT_LOG_FILE_BASE_NAME
    resolved_log_dir = _resolve_log_dir(log_dir)
    log_file_path = resolved_log_dir / f"{base_name}_{{time:YYYY-MM-DD}}.log"

    try:
        resolved_log_dir.mkdir(parents=True, exist_ok=True)
        logger.add(
            str(log_file_path),  # Sink: path to the log file
            level=level,  # Log level
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}",
            rotation="00:00",  # Rotate the log file at midnight every day
            retention="3 days",  # Keep logs for the last 3 days
            compression="zip",  # Compress old log files into .zip format
            encoding="utf-8",  # File encoding
            enqueue=True,  # Make logging asynchronous to prevent blocking the main thread
            backtrace=True,  # Include full stack trace in exception logs
            diagnose=True,  # Add extended diagnostic information for exceptions
        )
    except OSError as exc:
        logger.warning(
            "File logging disabled: cannot write to {} ({})",
            resolved_log_dir,
            exc,
        )
    
    logger.info(f"Logger configured with level: {level}")

# Initialize with default level
setup_logging()

logger.info("Logger initialized successfully.")

# --- How to use in other modules ---
# from logger import logger
#
# logger.info("This is an info message.")
# logger.warning("This is a warning message.")
# try:
#     x = 1 / 0
# except ZeroDivisionError:
#     logger.exception("An error occurred!")
