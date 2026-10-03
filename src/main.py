# -*- coding: utf-8 -*-
import os
import sys
import argparse
import signal
import threading
import configparser
import logging
import select
from waitress import wasyncore
from waitress.server import create_server

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


def waitress_options(proxy_manager) -> dict:
    """
    The production server's settings, in one place so tests run the same ones.

    - asyncore_use_poll: poll(), not waitress's default select(). select()
      cannot watch a descriptor numbered 1024 or above and raises straight out
      of the serving loop when it meets one. The database pool, validation
      sockets and fetcher pipes share that numbering, so a connection_limit
      sized for real traffic crosses the line long before its own count does.
    - clear_untrusted_proxy_headers=False: by default waitress strips
      forwarding headers before the app sees them, which silently disabled
      [server] trust_proxy_headers / trusted_proxy_ips in production. Waitress's
      own trusted_proxy takes a single address; the app's list is the one
      authority, and it only honours forwarding headers from a peer on it.
    """
    return {
        "host": "0.0.0.0",
        "port": proxy_manager.server_port,
        "threads": proxy_manager.production_threads,
        "connection_limit": proxy_manager.server_connection_limit,
        "asyncore_use_poll": True,
        "clear_untrusted_proxy_headers": False,
    }

def _poll_once(timeout, map):
    """
    waitress's poll2(), without subscribing to POLLPRI.

    POLLPRI signals TCP urgent data, which HTTP never sends. macOS poll()
    nevertheless reports it alongside POLLHUP whenever a peer closes its
    connection, and waitress answers a POLLPRI that carries no socket error by
    logging "unhandled incoming priority event" - once for every request.
    Not asking for the event means the kernel never reports it; POLLHUP,
    POLLERR and POLLNVAL are reported regardless and still close the channel.
    """
    pollster = select.poll()
    for fd, obj in list(map.items()):
        flags = 0
        if obj.readable():
            flags |= select.POLLIN
        # accepting sockets should not be writable
        if obj.writable() and not obj.accepting:
            flags |= select.POLLOUT
        if flags:
            pollster.register(fd, flags)
    for fd, flags in pollster.poll(None if timeout is None else int(timeout * 1000)):
        obj = map.get(fd)
        if obj is not None:
            wasyncore.readwrite(obj, flags)


class _PollLoop:
    """The wasyncore surface BaseWSGIServer.run() uses, on _poll_once()."""

    dispatcher = wasyncore.dispatcher

    @staticmethod
    def loop(timeout=30.0, use_poll=True, map=None, count=None):
        while map and (count is None or count > 0):
            _poll_once(timeout, map)
            if count is not None:
                count -= 1


def create_production_server(app, proxy_manager, **overrides):
    """The waitress server main() runs; tests build theirs here too."""
    server = create_server(app, **(waitress_options(proxy_manager) | overrides))
    server.asyncore = _PollLoop
    return server


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
            # One process keeps lease and scoring state coherent.
            logging.basicConfig()  # what waitress.serve() did for its own logger
            server = create_production_server(app, proxy_manager)
            server.print_listen("Serving on http://{}:{}")
            server.run()
    finally:
        if not shutdown_started.is_set():
            shutdown_started.set()
            proxy_manager.stop_scheduler()

if __name__ == "__main__":
    main()
