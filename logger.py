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
from loguru import logger

# --- Configuration ---
# You can easily configure the log directory and base filename here.
LOG_DIR = "/usr/local/var/log"
LOG_FILE_BASE_NAME = "proxy"
# The full path template for the log file. {time} is automatically replaced by Loguru.
LOG_FILE_PATH = os.path.join(LOG_DIR, f"{LOG_FILE_BASE_NAME}_{{time:YYYY-MM-DD}}.log")

# --- Ensure Log Directory Exists ---
# Note: Ensure the application has write permissions for the specified LOG_DIR.
try:
    os.makedirs(LOG_DIR, exist_ok=True)
except PermissionError:
    sys.stderr.write(
        f"Error: Permission denied to create log directory: {LOG_DIR}\n"
        "Please check directory permissions or run with appropriate privileges.\n"
    )
    sys.exit(1)


# --- Initialize Logger ---
# First, remove any default handlers to start with a clean configuration.
logger.remove()

# --- Configure Console Log Handler ---
# This handler is responsible for displaying colorful, readable logs in the terminal.
# Ideal for development and real-time monitoring.
logger.add(
    sys.stderr,  # Sink: standard error stream
    level="INFO",  # Log level
    format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>",
    colorize=True,  # Enable colorization
)

# --- Configure File Log Handler ---
# This handler writes logs to a file and manages rotation, retention, and compression.
logger.add(
    LOG_FILE_PATH,  # Sink: path to the log file
    level="INFO",  # Log level
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}",
    rotation="00:00",  # Rotate the log file at midnight every day
    retention="3 days",  # Keep logs for the last 7 days
    compression="zip",  # Compress old log files into .zip format
    encoding="utf-8",  # File encoding
    enqueue=True,  # Make logging asynchronous to prevent blocking the main thread
    backtrace=True,  # Include full stack trace in exception logs
    diagnose=True,  # Add extended diagnostic information for exceptions
)

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
