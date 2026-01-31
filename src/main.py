# -*- coding: utf-8 -*-
import os
import sys
import argparse
import signal
import threading
from concurrent.futures import as_completed

# Local imports
from src.utils.logger import logger, setup_logging
from src.core.proxy_manager import ProxyManager
from src.api.server import create_app

# --- Configuration ---
CONFIG_FILE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "config.ini")

def load_proxy_manager(config_path: str) -> ProxyManager:
    logger.info("Initializing ProxyManager...")
    manager = ProxyManager(config_path)
    manager.restore_stats()  # Restore from backup if available
    manager._sync_and_select_top_proxies()
    manager._sync_premium_proxies()  # Sync premium proxies on startup
    manager._update_dashboard_sources()
    if not manager.active_proxies:
        logger.warning(
            "Cold start detected. Running initial fetch and validation in background..."
        )
        
        def _cold_start_initialization():
            """Background task for cold start initialization."""
            try:
                initial_jobs = manager._load_fetcher_jobs()
                manager.fetcher_jobs = initial_jobs
                fetch_futures = [
                    manager.fetch_executor.submit(manager._fetch_and_parse_source, job)
                    for job in initial_jobs
                ]

                all_proxies = []
                for future in as_completed(fetch_futures):
                    try:
                        proxies = future.result()
                        if proxies:
                            all_proxies.extend(proxies)
                    except Exception as e:
                        logger.error(f"Initial fetcher job failed: {e}")

                if all_proxies:
                    unique_proxies_set = {tuple(p) for p in all_proxies}
                    unique_proxies_list = [list(p) for p in unique_proxies_set]
                    logger.info(
                        f"Initial fetch: Consolidated {len(unique_proxies_list)} unique proxies for insertion."
                    )
                    manager.db.insert_proxies(unique_proxies_list)

                manager._run_validation_cycle()
                logger.info("Cold start initialization complete.")
            except Exception as e:
                logger.error(f"Cold start initialization failed: {e}")
        
        # Run initialization in background thread to avoid blocking server startup
        threading.Thread(target=_cold_start_initialization, daemon=True).start()
        logger.info("Server starting immediately, cold start running in background.")
    return manager

def main():
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="SmartProxy Service")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging for validation")
    args = parser.parse_args()
    
    # Setup logging (if not already handled, usually good to ensure base logging)
    if args.debug:
        setup_logging("DEBUG")
        logger.info("Debug mode enabled - verbose validation logging active")
    else:
        # Default logging (INFO is usually default in logger.py)
        pass

    # Suppress Werkzeug's default access logs for per-request noise reduction
    import logging
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    
    # Initialize ProxyManager
    proxy_manager = load_proxy_manager(CONFIG_FILE_PATH)
    proxy_manager.debug_mode = args.debug

    # Create Flask App
    app = create_app(proxy_manager)

    # Shutdown handler
    def handle_shutdown(signum, frame):
        logger.info("Shutdown signal received. Performing graceful shutdown...")
        proxy_manager.stop_scheduler()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)
    
    proxy_manager.start_scheduler()
    
    # Run server
    app.run(host="0.0.0.0", port=proxy_manager.server_port, debug=False)

if __name__ == "__main__":
    main()
