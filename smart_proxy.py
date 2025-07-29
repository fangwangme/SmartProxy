# smart_proxy_server.py
from flask import Flask, request, jsonify
import requests
import random
import threading
import time
import signal
import sys
import os
import pickle
import configparser
from typing import List, Dict, Optional, Set
from logger import logger

# --- Configuration File Paths ---
DATA_DIR = "data"
CONFIG_FILE_PATH = os.path.join(DATA_DIR, "config.ini")
PROXY_FILE_PATH = os.path.join(
    DATA_DIR, "proxies.txt"
)  # Still used for initial manual load
PICKLE_FILE_PATH = os.path.join(DATA_DIR, "proxy_manager.pkl")


# --- Core Logic: Proxy Manager ---
class ProxyManager:
    """
    Manages proxy loading, selection, and performance statistics.
    This class is thread-safe.
    """

    def __init__(self):
        self.proxies: List[Dict] = []
        self.stats: Dict[str, Dict] = {}
        self.available_pools: Dict[str, List[str]] = {}
        self.sources: Set[str] = set()
        self.lock = threading.Lock()
        # Default settings, will be overwritten by config file
        self.consecutive_failure_threshold = 5
        self.min_pool_size = 100
        self.use_authenticated_proxies = False

        # --- [NEW] Scheduler components ---
        self.scheduler_thread = None
        self.stop_scheduler_event = threading.Event()
        self.proxy_source_urls = {}
        self.update_interval_seconds = 300

        logger.info("ProxyManager initialized an empty instance.")

    def load_config(self, config_path: str):
        """Loads sources and settings from the configuration file."""
        with self.lock:
            config = configparser.ConfigParser()
            self.sources = {"insolvencydirect", "test"}

            if os.path.exists(config_path):
                config.read(config_path, encoding="utf-8")
                sources_str = config.get(
                    "sources", "default_sources", fallback="insolvencydirect,test"
                )
                self.sources = {s.strip() for s in sources_str.split(",") if s.strip()}
                logger.info(f"Loaded sources from config: {self.sources}")

                self.consecutive_failure_threshold = config.getint(
                    "settings", "consecutive_failure_threshold", fallback=5
                )
                self.min_pool_size = config.getint(
                    "settings", "min_pool_size", fallback=100
                )
                self.use_authenticated_proxies = config.getboolean(
                    "settings", "use_authenticated_proxies", fallback=False
                )

                # --- [NEW] Load auto-fetch settings ---
                self.update_interval_seconds = config.getint(
                    "proxy_sources", "update_interval_seconds", fallback=300
                )
                self.proxy_source_urls["http"] = config.get(
                    "proxy_sources", "http_url", fallback=None
                )
                self.proxy_source_urls["socks5"] = config.get(
                    "proxy_sources", "socks5_url", fallback=None
                )

                logger.info(
                    f"Loaded settings: FailureThreshold={self.consecutive_failure_threshold}, MinPoolSize={self.min_pool_size}"
                )
                logger.info(
                    f"Auto-fetch enabled for URLs with interval {self.update_interval_seconds}s."
                )
            else:
                logger.warning(
                    f"Config file not found. Using default sources and settings."
                )

            self._ensure_sources_initialized()
            self._save_config(config_path)

    def _save_config(self, config_path: str):
        """Saves the current sources and settings. MUST be called within a locked context."""
        config = configparser.ConfigParser()
        config["sources"] = {"default_sources": ",".join(sorted(list(self.sources)))}
        config["settings"] = {
            "consecutive_failure_threshold": str(self.consecutive_failure_threshold),
            "min_pool_size": str(self.min_pool_size),
            "use_authenticated_proxies": str(self.use_authenticated_proxies),
        }
        # --- [NEW] Save auto-fetch settings ---
        config["proxy_sources"] = {
            "update_interval_seconds": str(self.update_interval_seconds),
            "http_url": self.proxy_source_urls.get("http", ""),
            "socks5_url": self.proxy_source_urls.get("socks5", ""),
        }
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as configfile:
            config.write(configfile)
        logger.info(f"Saved config to {config_path}")

    # --- [NEW] The main loop for the background scheduler thread ---
    def _auto_fetch_loop(self):
        logger.info("Auto-fetch scheduler thread started.")
        while not self.stop_scheduler_event.is_set():
            try:
                self._fetch_and_merge_proxies()
            except Exception as e:
                logger.error(f"Error in auto-fetch loop: {e}", exc_info=True)

            # Wait for the next interval
            self.stop_scheduler_event.wait(self.update_interval_seconds)
        logger.info("Auto-fetch scheduler thread stopped.")

    # --- [NEW] The core logic for fetching and merging proxies ---
    def _fetch_and_merge_proxies(self):
        logger.info("Starting automatic proxy fetch cycle...")
        new_proxies = set()

        # Fetch and format HTTP proxies
        http_url = self.proxy_source_urls.get("http")
        if http_url:
            try:
                response = requests.get(http_url, timeout=15)
                response.raise_for_status()
                for line in response.text.splitlines():
                    if line.strip():
                        new_proxies.add(f"http://{line.strip()}")
                logger.info(
                    f"Fetched {len(response.text.splitlines())} lines from HTTP source."
                )
            except requests.RequestException as e:
                logger.error(f"Failed to fetch HTTP proxies: {e}")

        # Fetch and format SOCKS5 proxies
        socks5_url = self.proxy_source_urls.get("socks5")
        if socks5_url:
            try:
                response = requests.get(socks5_url, timeout=15)
                response.raise_for_status()
                for line in response.text.splitlines():
                    if line.strip():
                        new_proxies.add(f"socks5://{line.strip()}")
                logger.info(
                    f"Fetched {len(response.text.splitlines())} lines from SOCKS5 source."
                )
            except requests.RequestException as e:
                logger.error(f"Failed to fetch SOCKS5 proxies: {e}")

        if not new_proxies:
            logger.warning(
                "Auto-fetch cycle completed, but no new proxies were fetched."
            )
            return

        # Merge the newly fetched proxies into the manager
        self._add_new_proxies_to_memory(list(new_proxies))

    # --- [NEW] Helper to add a list of formatted proxies directly to memory ---
    def _add_new_proxies_to_memory(self, proxy_lines: List[str]):
        added_count = 0
        with self.lock:
            existing_proxy_urls = {p["url"] for p in self.proxies}
            for line in proxy_lines:
                try:
                    # Simplified parsing since we've already formatted the lines
                    protocol, rest = line.split("://", 1)
                    host, port_str = rest.rsplit(":", 1)
                    port = int(port_str)

                    if not (1 <= port <= 65535):
                        continue  # Invalid port

                    url = f"{protocol}://{host}:{port}"
                    if url not in existing_proxy_urls:
                        proxy_info = {
                            "protocol": protocol,
                            "host": host,
                            "port": port,
                            "auth": None,
                            "url": url,
                        }
                        self.proxies.append(proxy_info)
                        existing_proxy_urls.add(url)
                        for source in self.sources:
                            self.stats[source][url] = self._get_new_proxy_stat()
                            self.available_pools[source].append(url)
                        added_count += 1
                except (ValueError, IndexError):
                    continue  # Skip malformed lines

        logger.info(
            f"Auto-fetch merge complete. Added {added_count} new unique proxies. Total proxies: {len(self.proxies)}"
        )

    # --- [NEW] Methods to start and stop the scheduler ---
    def start_scheduler(self):
        if self.scheduler_thread is None or not self.scheduler_thread.is_alive():
            self.stop_scheduler_event.clear()
            self.scheduler_thread = threading.Thread(
                target=self._auto_fetch_loop, daemon=True
            )
            self.scheduler_thread.start()

    def stop_scheduler(self):
        if self.scheduler_thread and self.scheduler_thread.is_alive():
            self.stop_scheduler_event.set()
            self.scheduler_thread.join(timeout=5)  # Wait for thread to finish
            logger.info("Scheduler stopped.")

    def add_source(self, source: str, config_path: str):
        """Adds a new source to the manager and persists it."""
        with self.lock:
            if source in self.sources:
                logger.info(f"Source '{source}' already exists.")
                return False

            logger.info(f"Adding new source: {source}")
            self.sources.add(source)
            self._ensure_sources_initialized()
            self._save_config(config_path)
            return True

    def _ensure_sources_initialized(self):
        """Ensures all current sources are initialized in stats and pools."""
        for source in self.sources:
            if source not in self.stats:
                self.stats[source] = {}
                self.available_pools[source] = []
                for p in self.proxies:
                    proxy_url = p["url"]
                    if proxy_url not in self.stats[source]:
                        self.stats[source][proxy_url] = self._get_new_proxy_stat()
                    if not self.stats[source][proxy_url].get("is_banned", False):
                        if proxy_url not in self.available_pools[source]:
                            self.available_pools[source].append(proxy_url)

    @staticmethod
    def _get_new_proxy_stat() -> Dict:
        """Returns a dictionary for a new proxy's statistics."""
        return {
            "success_count": 0,
            "failure_count": 0,
            "consecutive_failures": 0,
            "avg_response_time_ms": float("inf"),
            "is_banned": False,
            "last_used": 0,
        }

    def load_proxies_from_file(self, proxy_file: str) -> Dict:
        """
        Loads proxies from a text file and merges them with the existing state.
        """
        if not os.path.exists(proxy_file):
            return {
                "message": f"Warning: Proxy file '{proxy_file}' does not exist.",
                "added": 0,
                "total": len(self.proxies),
            }

        with open(proxy_file, "r", encoding="utf-8") as f:
            unique_proxy_lines = {
                line.strip() for line in f if line.strip() and not line.startswith("#")
            }

        self._add_new_proxies_to_memory(list(unique_proxy_lines))
        return {"message": "Proxies loaded successfully.", "total": len(self.proxies)}

    def save_state_to_pickle(self, pickle_path: str):
        """Saves the entire ProxyManager object state to a pickle file."""
        with self.lock:
            os.makedirs(os.path.dirname(pickle_path), exist_ok=True)
            try:
                with open(pickle_path, "wb") as f:
                    pickle.dump(self, f)
                logger.info(f"Successfully saved manager state to '{pickle_path}'.")
            except Exception as e:
                logger.error(f"Failed to save state to pickle file: {e}")

    def get_proxy_for_source(self, source: str) -> Optional[str]:
        """Gets a random proxy from the source's available pool."""
        with self.lock:
            if source not in self.sources:
                return None
            source_pool = self.available_pools.get(source, [])
            source_stats = self.stats.get(source, {})
            if len(source_pool) < self.min_pool_size:
                proxy_to_revive_url = None
                successful_revival_candidates = [
                    p_url
                    for p_url, p_stat in source_stats.items()
                    if p_stat.get("is_banned") and p_stat.get("success_count", 0) > 0
                ]
                if successful_revival_candidates:
                    proxy_to_revive_url = random.choice(successful_revival_candidates)
                else:
                    all_banned_proxies = [
                        p_url
                        for p_url, p_stat in source_stats.items()
                        if p_stat.get("is_banned")
                    ]
                    if all_banned_proxies:
                        proxy_to_revive_url = random.choice(all_banned_proxies)
                if proxy_to_revive_url:
                    source_stats[proxy_to_revive_url]["is_banned"] = False
                    source_stats[proxy_to_revive_url]["consecutive_failures"] = 0
                    if proxy_to_revive_url not in source_pool:
                        source_pool.append(proxy_to_revive_url)
            if not source_pool:
                return None
            chosen_proxy_url = random.choice(source_pool)
            source_stats[chosen_proxy_url]["last_used"] = time.time()
            return chosen_proxy_url

    def process_feedback(self, feedback_data: Dict):
        """Processes feedback and updates the proxy's status."""
        source, proxy_url, status, resp_time = (
            feedback_data.get("source"),
            feedback_data.get("proxy"),
            feedback_data.get("status"),
            feedback_data.get("response_time_ms"),
        )
        logger.info(
            f"Handled feedback: source={source}, proxy={proxy_url}, status={status}, resp_time={resp_time}"
        )

        if not all([source, proxy_url, status]):
            return False
        with self.lock:
            if source not in self.stats or proxy_url not in self.stats[source]:
                return False
            stat = self.stats[source][proxy_url]
            if status == "success":
                stat["success_count"] += 1
                stat["consecutive_failures"] = 0
                if stat["is_banned"]:
                    stat["is_banned"] = False
                    if proxy_url not in self.available_pools.get(source, []):
                        self.available_pools.setdefault(source, []).append(proxy_url)
            elif status == "failure":
                stat["failure_count"] += 1
                stat["consecutive_failures"] += 1
                if (
                    stat["consecutive_failures"] >= self.consecutive_failure_threshold
                    and not stat["is_banned"]
                ):
                    stat["is_banned"] = True
                    if proxy_url in self.available_pools.get(source, []):
                        self.available_pools[source].remove(proxy_url)
            return True

    def get_source_stats(self, source: str) -> Optional[Dict]:
        """Gets statistics for a given source."""
        with self.lock:
            if source not in self.sources:
                return None
            source_pool = self.available_pools.get(source, [])
            return {
                "source": source,
                "available_proxies_count": len(source_pool),
                "available_proxies_list": source_pool[:10],
            }


def load_proxy_manager(pickle_path, config_path, proxy_path) -> ProxyManager:
    """Factory function to load or create a ProxyManager instance."""
    manager = None
    if os.path.exists(pickle_path):
        try:
            with open(pickle_path, "rb") as f:
                manager = pickle.load(f)
            logger.info("Successfully loaded manager state from pickle file.")
            manager.load_config(config_path)
        except Exception as e:
            logger.error(f"Failed to load from pickle file: {e}. Creating new manager.")
            manager = None
    if manager is None:
        manager = ProxyManager()
        manager.load_config(config_path)
    logger.info(f"Loading/merging proxies from text file: {proxy_path}")
    manager.load_proxies_from_file(proxy_path)
    return manager


# --- HTTP Server ---
app = Flask(__name__)
proxy_manager = load_proxy_manager(PICKLE_FILE_PATH, CONFIG_FILE_PATH, PROXY_FILE_PATH)


@app.route("/get-proxy", methods=["GET"])
def get_proxy():
    source = request.args.get("source")
    if not source:
        return jsonify({"error": "Query parameter 'source' is required."}), 400
    proxy_url = proxy_manager.get_proxy_for_source(source)
    if proxy_url:
        return jsonify({"http": proxy_url, "https": proxy_url})
    else:
        return (
            jsonify(
                {"error": f"No available proxy for source '{source}' at the moment."}
            ),
            404,
        )


@app.route("/feedback", methods=["POST"])
def feedback():
    data = request.json
    if not data or not proxy_manager.process_feedback(data):
        return jsonify({"error": "Invalid or incomplete feedback data provided."}), 400
    return jsonify({"message": "Feedback received and processed."})


@app.route("/add-source", methods=["POST"])
def add_source():
    data = request.json
    source = data.get("source")
    if not source:
        return jsonify({"error": "JSON body must contain a 'source' key."}), 400
    if proxy_manager.add_source(source, CONFIG_FILE_PATH):
        return jsonify({"message": f"Source '{source}' added successfully."})
    else:
        return jsonify({"message": f"Source '{source}' already exists."}), 200


@app.route("/load-proxies", methods=["POST"])
def load_proxies_endpoint():
    logger.info("Hot-reload of proxies triggered via API.")
    result = proxy_manager.load_proxies_from_file(PROXY_FILE_PATH)
    return jsonify(result)


@app.route("/export-stats", methods=["GET"])
def export_stats_endpoint():
    proxy_manager.save_state_to_pickle(PICKLE_FILE_PATH)
    return jsonify({"message": f"Manager state saved to {PICKLE_FILE_PATH}"})


@app.route("/stats/<string:source>", methods=["GET"])
def get_source_stats_endpoint(source: str):
    stats = proxy_manager.get_source_stats(source)
    if stats:
        return jsonify(stats)
    else:
        return jsonify({"error": f"No statistics found for source '{source}'."}), 404


def handle_shutdown(signal, frame):
    logger.info("\nShutdown signal received, saving state and stopping scheduler...")
    proxy_manager.stop_scheduler()
    proxy_manager.save_state_to_pickle(PICKLE_FILE_PATH)
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(PROXY_FILE_PATH):
        logger.info(f"Creating a dummy proxy file at '{PROXY_FILE_PATH}'.")
        with open(PROXY_FILE_PATH, "w", encoding="utf-8") as f:
            f.write("# Add proxies here in the format: protocol:host:port\n")
    if not os.path.exists(CONFIG_FILE_PATH):
        proxy_manager.load_config(CONFIG_FILE_PATH)

    # --- [NEW] Start the background scheduler ---
    proxy_manager.start_scheduler()

    app.run(host="0.0.0.0", port=6942, debug=True, use_reloader=False)
