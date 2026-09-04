# -*- coding: utf-8 -*-
import os
import sys
import argparse
import signal
import threading
import configparser
from waitress import serve

# Local imports
from src.utils.logger import logger, setup_logging
from src.core.proxy_manager import ProxyManager
from src.api.server import create_app

# --- Configuration ---
CONFIG_FILE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "config.ini")


def configure_logging_from_file(config_path: str, level: str):
    config = configparser.ConfigParser()
    config.read(config_path, encoding="utf-8")
    log_dir = config.get("logging", "log_dir", fallback="./.local/logs")
    log_file_base_name = config.get("logging", "log_file_base_name", fallback="proxy")
    setup_logging(level, log_dir=log_dir, log_file_base_name=log_file_base_name)

def load_proxy_manager(config_path: str, restore_mode: str = "normal") -> ProxyManager:
    logger.info("Initializing ProxyManager in '{}' restore mode...", restore_mode)
    manager = ProxyManager(config_path, restore_mode=restore_mode)
    manager.restore_stats()
    manager._sync_and_select_top_proxies()
    manager._update_dashboard_sources()
    if not manager.active_proxies:
        logger.warning(
            "Cold start detected; the scheduler will run one initial fetch and validation cycle."
        )
    return manager

def main():
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="SmartProxy Service")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging for validation")
    parser.add_argument(
        "--no-restore",
        action="store_true",
        help="Skip JSON restore and write to isolated experiment state",
    )
    args = parser.parse_args()
    
    # Persistent sinks are initialized only by the process entry point. Imports
    # and tests therefore cannot write into operational log files.
    log_level = "DEBUG" if args.debug else "INFO"
    configure_logging_from_file(CONFIG_FILE_PATH, log_level)
    if args.debug:
        logger.info("Debug mode enabled - verbose validation logging active")

    # Suppress Werkzeug's default access logs for per-request noise reduction
    import logging
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    
    # Initialize ProxyManager
    restore_mode = "no-restore" if args.no_restore else "normal"
    logger.info("Selected scoring restore mode: {}", restore_mode)
    proxy_manager = load_proxy_manager(CONFIG_FILE_PATH, restore_mode=restore_mode)
    proxy_manager.debug_mode = args.debug

    # Create Flask App
    app = create_app(proxy_manager)

    shutdown_started = threading.Event()

    def handle_shutdown(signum, frame):
        if shutdown_started.is_set():
            return
        shutdown_started.set()
        logger.info("Shutdown signal received. Performing graceful shutdown...")
        proxy_manager.stop_scheduler()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)
    
    proxy_manager.start_scheduler()
    try:
        if args.debug:
            app.run(host="0.0.0.0", port=proxy_manager.server_port, debug=False)
        else:
            # One process keeps allocation, lease, and scoring state coherent.
            serve(
                app,
                host="0.0.0.0",
                port=proxy_manager.server_port,
                threads=proxy_manager.production_threads,
            )
    finally:
        if not shutdown_started.is_set():
            shutdown_started.set()
            proxy_manager.stop_scheduler()

if __name__ == "__main__":
    main()
