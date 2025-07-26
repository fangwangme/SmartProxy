# smart_proxy_server.py
from flask import Flask, request, jsonify
import json
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
PROXY_FILE_PATH = os.path.join(DATA_DIR, "proxies.txt")
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
        logger.info("ProxyManager initialized an empty instance.")

    def load_config(self, config_path: str):
        """Loads sources and settings from the configuration file."""
        with self.lock:
            config = configparser.ConfigParser()
            # Set default values first
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
                # --- [NEW] Load setting for authenticated proxies ---
                self.use_authenticated_proxies = config.getboolean(
                    "settings", "use_authenticated_proxies", fallback=True
                )
                logger.info(
                    f"Loaded settings: FailureThreshold={self.consecutive_failure_threshold}, MinPoolSize={self.min_pool_size}, UseAuthProxies={self.use_authenticated_proxies}"
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
            # --- [NEW] Save setting for authenticated proxies ---
            "use_authenticated_proxies": str(self.use_authenticated_proxies),
        }
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as configfile:
            config.write(configfile)
        logger.info(f"Saved config to {config_path}")

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
                # Initialize stats for all existing proxies
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
            message = f"Warning: Proxy file '{proxy_file}' does not exist."
            logger.info(message)
            return {"message": message, "added": 0, "total": len(self.proxies)}

        unique_proxy_lines = set()
        try:
            with open(proxy_file, "r", encoding="utf-8") as f:
                for line in f:
                    stripped_line = line.strip()
                    if stripped_line and not stripped_line.startswith("#"):
                        unique_proxy_lines.add(stripped_line)
        except Exception as e:
            logger.error(f"Failed to read proxy file {proxy_file}: {e}")
            return {"message": f"Error reading proxy file: {e}"}

        added_count = 0
        skipped_duplicates_count = 0
        skipped_malformed_count = 0
        skipped_auth_count = 0

        with self.lock:
            existing_proxy_urls = {p["url"] for p in self.proxies}

            for line in unique_proxy_lines:
                if not line:
                    continue
                try:
                    parts = line.split(":")
                    proxy_info = {}
                    url = ""

                    if len(parts) == 3:  # protocol:host:port
                        protocol, host, port_str = parts[0].lower(), parts[1], parts[2]
                        if (
                            protocol not in ["http", "https", "socks4", "socks5"]
                            or not host
                        ):
                            skipped_malformed_count += 1
                            continue
                        port = int(port_str)
                        if not (1 <= port <= 65535):
                            skipped_malformed_count += 1
                            continue
                        url = f"{protocol}://{host}:{port}"
                        proxy_info = {
                            "protocol": protocol,
                            "host": host,
                            "port": port,
                            "auth": None,
                            "url": url,
                        }
                    elif len(parts) == 5:  # protocol:host:port:user:pass
                        # --- [NEW] Skip authenticated proxies if disabled in config ---
                        if not self.use_authenticated_proxies:
                            skipped_auth_count += 1
                            continue

                        protocol, host, port_str, user, password = (
                            parts[0].lower(),
                            parts[1],
                            parts[2],
                            parts[3],
                            parts[4],
                        )
                        if (
                            protocol not in ["http", "https", "socks4", "socks5"]
                            or not host
                        ):
                            skipped_malformed_count += 1
                            continue
                        port = int(port_str)
                        if not (1 <= port <= 65535):
                            skipped_malformed_count += 1
                            continue
                        url = f"{protocol}://{user}:{password}@{host}:{port}"
                        proxy_info = {
                            "protocol": protocol,
                            "host": host,
                            "port": port,
                            "auth": (user, password),
                            "url": url,
                        }
                    else:
                        skipped_malformed_count += 1
                        continue

                    if url not in existing_proxy_urls:
                        self.proxies.append(proxy_info)
                        existing_proxy_urls.add(url)
                        for source in self.sources:
                            self.stats[source][url] = self._get_new_proxy_stat()
                            self.available_pools[source].append(url)
                        added_count += 1
                    else:
                        skipped_duplicates_count += 1
                except (ValueError, IndexError):
                    skipped_malformed_count += 1
                    continue

        message = (
            f"Proxy loading finished. Added: {added_count}, "
            f"Skipped (duplicates): {skipped_duplicates_count}, "
            f"Skipped (malformed): {skipped_malformed_count}, "
            f"Skipped (auth disabled): {skipped_auth_count}, Total: {len(self.proxies)}"
        )
        logger.info(message)
        return {"message": message, "added": added_count, "total": len(self.proxies)}

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
                logger.warning(
                    f"Requested source '{source}' is unknown. Add it first via the API."
                )
                return None

            source_pool = self.available_pools.get(source, [])
            source_stats = self.stats.get(source, {})

            if len(source_pool) < self.min_pool_size:
                logger.warning(
                    f"Pool for '{source}' has {len(source_pool)} proxies, below threshold {self.min_pool_size}. Attempting revival."
                )
                proxy_to_revive_url = None

                successful_revival_candidates = [
                    p_url
                    for p_url, p_stat in source_stats.items()
                    if p_stat.get("is_banned") and p_stat.get("success_count", 0) > 0
                ]
                if successful_revival_candidates:
                    proxy_to_revive_url = random.choice(successful_revival_candidates)
                    logger.info(
                        f"Reviving historically successful proxy for '{source}': {proxy_to_revive_url}"
                    )
                else:
                    all_banned_proxies = [
                        p_url
                        for p_url, p_stat in source_stats.items()
                        if p_stat.get("is_banned")
                    ]
                    if all_banned_proxies:
                        proxy_to_revive_url = random.choice(all_banned_proxies)
                        logger.warning(
                            f"No successful proxies to revive. Reviving random banned proxy for '{source}': {proxy_to_revive_url}"
                        )

                if proxy_to_revive_url:
                    source_stats[proxy_to_revive_url]["is_banned"] = False
                    source_stats[proxy_to_revive_url]["consecutive_failures"] = 0
                    if proxy_to_revive_url not in source_pool:
                        source_pool.append(proxy_to_revive_url)

            if not source_pool:
                logger.error(
                    f"No available proxies for source '{source}' after revival attempt."
                )
                return None

            chosen_proxy_url = random.choice(source_pool)
            source_stats[chosen_proxy_url]["last_used"] = time.time()
            return chosen_proxy_url

    def process_feedback(self, feedback_data: Dict):
        """Processes feedback and updates the proxy's status."""
        source = feedback_data.get("source")
        proxy_url = feedback_data.get("proxy")
        status = feedback_data.get("status")
        response_time_ms = feedback_data.get("response_time_ms")
        logger.info(
            f"Handled feedback: proxy-[{proxy_url}], source-[{source}], status-[{status}], time-[{response_time_ms}]"
        )

        if not all([source, proxy_url, status]):
            return False

        with self.lock:
            if source not in self.stats or proxy_url not in self.stats[source]:
                logger.warning(
                    f"Feedback for unknown proxy or source ignored: {source} - {proxy_url}"
                )
                return False

            stat = self.stats[source][proxy_url]

            if status == "success":
                stat["success_count"] += 1
                stat["consecutive_failures"] = 0
                if stat["is_banned"]:
                    stat["is_banned"] = False
                    if proxy_url not in self.available_pools.get(source, []):
                        self.available_pools.setdefault(source, []).append(proxy_url)
                        logger.info(
                            f"Reinstated proxy {proxy_url} for source '{source}' after success."
                        )

                if isinstance(response_time_ms, (int, float)) and response_time_ms >= 0:
                    current_total_time = (
                        stat["avg_response_time_ms"]
                        if stat["avg_response_time_ms"] != float("inf")
                        else 0
                    ) * (stat["success_count"] - 1)
                    new_total_time = current_total_time + response_time_ms
                    stat["avg_response_time_ms"] = (
                        new_total_time / stat["success_count"]
                    )
            elif status == "failure":
                stat["failure_count"] += 1
                stat["consecutive_failures"] += 1
                if (
                    stat["consecutive_failures"] >= self.consecutive_failure_threshold
                    and not stat["is_banned"]
                ):
                    stat["is_banned"] = True
                    logger.info(
                        f"Proxy {proxy_url} has been banned for source '{source}'."
                    )
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
        logger.info(f"Attempting to load state from pickle file: {pickle_path}")
        try:
            with open(pickle_path, "rb") as f:
                manager = pickle.load(f)
            logger.info("Successfully loaded manager state from pickle file.")
            # Always reload config on start to apply changes
            manager.load_config(config_path)
        except Exception as e:
            logger.error(f"Failed to load from pickle file: {e}. Creating new manager.")
            manager = None

    if manager is None:
        manager = ProxyManager()
        manager.load_config(config_path)

    # Always load/merge from proxies.txt to catch updates
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
    """Manually triggers a hot-reload of the proxies.txt file."""
    logger.info("Hot-reload of proxies triggered via API.")
    result = proxy_manager.load_proxies_from_file(PROXY_FILE_PATH)
    return jsonify(result)


@app.route("/export-stats", methods=["GET"])
def export_stats_endpoint():
    """Manually triggers saving the current manager state to a pickle file."""
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
    logger.info("\nShutdown signal received, saving state...")
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

    # Initialize config if it doesn't exist
    if not os.path.exists(CONFIG_FILE_PATH):
        # This will call _save_config within load_config, creating the file
        proxy_manager.load_config(CONFIG_FILE_PATH)

    app.run(host="0.0.0.0", port=6942, debug=True, use_reloader=False)
