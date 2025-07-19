# smart_proxy_server.py
from flask import Flask, request, jsonify
import json
import random
import threading
import time
import signal
import sys
import os
from typing import List, Dict, Optional
from logger import logger

# --- Configuration ---
PROXY_FILE_PATH = "proxies.txt"
STATS_FILE_PATH = "proxy_stats.json"
CONSECUTIVE_FAILURE_THRESHOLD = (
    10  # Number of consecutive failures before a proxy is banned
)
COOL_DOWN_SECONDS = 30  # Cooldown in seconds after a proxy is used (not currently used in logic, but kept for future)
MIN_POOL_SIZE = 300  # Minimum number of proxies in the available pool for a source


# --- Core Logic: Proxy Manager ---
class ProxyManager:
    """
    Manages proxy loading, selection, and performance statistics.
    This class is thread-safe.
    """

    def __init__(self, proxy_file: str, stats_file: str):
        self.proxy_file = proxy_file
        self.stats_file = stats_file
        self.proxies: List[Dict] = []
        self.stats: Dict[str, Dict] = {}
        # A dictionary to hold the list of available proxy URLs for each source
        self.available_pools: Dict[str, List[str]] = {}
        self.lock = threading.Lock()
        self.load_proxies()
        self._load_stats()
        # Initialize available pools based on loaded stats
        self._initialize_pools()

    def load_proxies(self, is_reload: bool = False) -> Dict:
        """
        Loads and deduplicates the proxy list from a file.
        """
        if not os.path.exists(self.proxy_file):
            message = f"Warning: Proxy file '{self.proxy_file}' does not exist."
            logger.info(message)
            return {
                "message": message,
                "added": 0,
                "skipped": 0,
                "total": len(self.proxies),
            }

        unique_proxy_lines = set()
        with open(self.proxy_file, "r") as f:
            for line in f:
                stripped_line = line.strip()
                if stripped_line and not stripped_line.startswith("#"):
                    unique_proxy_lines.add(stripped_line)

        added_count = 0
        skipped_count = 0

        with self.lock:
            existing_proxy_urls = {p["url"] for p in self.proxies}

            for line in unique_proxy_lines:
                try:
                    parts = line.split(":")
                    proxy_info = {}
                    url = ""
                    if len(parts) == 3:  # protocol:host:port
                        url = f"{parts[0]}://{parts[1]}:{parts[2]}"
                        proxy_info = {
                            "protocol": parts[0],
                            "host": parts[1],
                            "port": int(parts[2]),
                            "auth": None,
                            "url": url,
                        }
                    elif len(parts) == 5:  # protocol:host:port:user:pass
                        url = (
                            f"{parts[0]}://{parts[3]}:{parts[4]}@{parts[1]}:{parts[2]}"
                        )
                        proxy_info = {
                            "protocol": parts[0],
                            "host": parts[1],
                            "port": int(parts[2]),
                            "auth": (parts[3], parts[4]),
                            "url": url,
                        }
                    else:
                        if is_reload:
                            logger.info(f"Skipping malformed line on reload: {line}")
                        continue

                    if url not in existing_proxy_urls:
                        self.proxies.append(proxy_info)
                        existing_proxy_urls.add(url)
                        # Add the new proxy to all existing source pools
                        for source in self.available_pools:
                            self.available_pools[source].append(url)
                        added_count += 1
                    else:
                        skipped_count += 1
                except (ValueError, IndexError):
                    if is_reload:
                        logger.info(f"Skipping malformed line on reload: {line}")

        load_type = "reload" if is_reload else "load"
        message = f"Proxy {load_type} finished. Added: {added_count}, Skipped (duplicates): {skipped_count}, Total: {len(self.proxies)}"
        logger.info(message)
        return {
            "message": message,
            "added": added_count,
            "skipped": skipped_count,
            "total": len(self.proxies),
        }

    def _load_stats(self):
        """
        Loads historical performance statistics from the JSON file.
        """
        if os.path.exists(self.stats_file):
            with self.lock:
                with open(self.stats_file, "r") as f:
                    self.stats = json.load(f)
                logger.info(
                    f"Successfully loaded performance stats from '{self.stats_file}'."
                )

    def _initialize_pools(self):
        """
        Initializes the available proxy pools based on loaded stats.
        """
        with self.lock:
            all_proxy_urls = {p["url"] for p in self.proxies}
            for source, source_stats in self.stats.items():
                # A proxy is available for a source if it's not marked as banned in the stats
                self.available_pools[source] = [
                    url
                    for url in all_proxy_urls
                    if not source_stats.get(url, {}).get("is_banned", False)
                ]
            logger.info("Initialized available proxy pools for all sources.")

    def save_stats(self):
        """
        Saves the current performance statistics to the JSON file.
        """
        with self.lock:
            with open(self.stats_file, "w") as f:
                json.dump(self.stats, f, indent=4)
            logger.info(f"Performance stats successfully saved to '{self.stats_file}'.")

    def get_proxy_for_source(self, source: str) -> Optional[str]:
        """
        Gets a random proxy from the source's available pool.
        If the pool is too small, it attempts to revive a banned proxy.
        """
        if not self.proxies:
            return None

        with self.lock:
            # --- Initialization for a brand new source ---
            if source not in self.stats:
                self.stats[source] = {}
                # Initialize stats for all proxies for this new source
                for p in self.proxies:
                    self.stats[source][p["url"]] = {
                        "success_count": 0,
                        "failure_count": 0,
                        "consecutive_failures": 0,
                        "avg_response_time_ms": float("inf"),
                        "is_banned": False,
                        "last_used": 0,
                    }
                # The initial pool for a new source is all proxies
                self.available_pools[source] = [p["url"] for p in self.proxies]
                logger.info(
                    f"Initialized new pool for source '{source}' with {len(self.proxies)} proxies."
                )

            source_pool = self.available_pools.get(source, [])
            source_stats = self.stats[source]

            # --- Revival logic if the pool is too small ---
            if len(source_pool) < MIN_POOL_SIZE:
                logger.info(
                    f"Warning: Pool for '{source}' has {len(source_pool)} proxies, which is below the threshold of {MIN_POOL_SIZE}. Attempting to revive a proxy."
                )

                # Find banned proxies that have a history of success
                revival_candidates = [
                    p["url"]
                    for p in self.proxies
                    if source_stats.get(p["url"], {}).get("is_banned")
                    and source_stats.get(p["url"], {}).get("success_count", 0) > 0
                ]

                if revival_candidates:
                    proxy_to_revive_url = random.choice(revival_candidates)
                    logger.info(
                        f"Info: Reviving historically successful proxy for '{source}': {proxy_to_revive_url}"
                    )
                    # Un-ban the proxy and add it back to the available pool
                    source_stats[proxy_to_revive_url]["is_banned"] = False
                    source_stats[proxy_to_revive_url]["consecutive_failures"] = 0
                    if proxy_to_revive_url not in source_pool:
                        source_pool.append(proxy_to_revive_url)

            # --- Select a proxy from the pool ---
            if not source_pool:
                logger.info(
                    f"Error: No available proxies for source '{source}' after revival attempt."
                )
                return None

            chosen_proxy_url = random.choice(source_pool)
            source_stats[chosen_proxy_url]["last_used"] = time.time()
            return chosen_proxy_url

    def process_feedback(self, feedback_data: Dict):
        """
        Processes feedback and updates the proxy's status and the available pool.
        """
        source = feedback_data.get("source")
        proxy_url = feedback_data.get("proxy")
        status = feedback_data.get("status")
        response_time_ms = feedback_data.get("time")

        logger.info(
            f"Feedback: proxy-[{proxy_url}], status-[{status}], source-[{source}], speed-[{response_time_ms}]"
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
                # A successful proxy should always be considered not banned
                if stat["is_banned"]:
                    stat["is_banned"] = False
                    # If it was previously banned, add it back to the pool
                    if proxy_url not in self.available_pools.get(source, []):
                        self.available_pools.setdefault(source, []).append(proxy_url)
                        logger.info(
                            f"Info: Reinstated proxy {proxy_url} for source '{source}' after success."
                        )

                if not isinstance(response_time_ms, (int, float)):
                    logger.info(
                        f"Warning: Success feedback for '{source}' is missing valid 'response_time_ms'."
                    )
                    return False

                current_total_time = (
                    stat["avg_response_time_ms"]
                    if stat["avg_response_time_ms"] != float("inf")
                    else 0
                ) * (stat["success_count"] - 1)
                new_total_time = current_total_time + response_time_ms
                stat["avg_response_time_ms"] = new_total_time / stat["success_count"]

            elif status == "failure":
                stat["failure_count"] += 1
                stat["consecutive_failures"] += 1
                if (
                    stat["consecutive_failures"] >= CONSECUTIVE_FAILURE_THRESHOLD
                    and not stat["is_banned"]
                ):
                    stat["is_banned"] = True
                    logger.info(
                        f"Info: Proxy {proxy_url} has been banned for source '{source}'."
                    )
                    # Remove the proxy from the available pool for this source
                    if proxy_url in self.available_pools.get(source, []):
                        self.available_pools[source].remove(proxy_url)
                        logger.info(
                            f"Info: Removed {proxy_url} from '{source}' available pool."
                        )
            return True


# --- HTTP Server ---
app = Flask(__name__)
proxy_manager = ProxyManager(PROXY_FILE_PATH, STATS_FILE_PATH)


@app.route("/get-proxy", methods=["GET"])
def get_proxy():
    source = request.args.get("source")
    if not source:
        return jsonify({"error": "Query parameter 'source' is required."}), 400

    proxy_url = proxy_manager.get_proxy_for_source(source)

    if proxy_url:
        return jsonify(
            {
                "http": proxy_url,
                "https": proxy_url,
            }
        )
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
    if not data:
        return jsonify({"error": "Invalid JSON body."}), 400

    success = proxy_manager.process_feedback(data)

    if success:
        return jsonify({"message": "Feedback received and processed."})
    else:
        return jsonify({"error": "Invalid feedback data provided."}), 400


@app.route("/load-proxies", methods=["POST"])
def load_proxies_endpoint():
    """Endpoint to trigger a reload of the proxy file."""
    result = proxy_manager.load_proxies(is_reload=True)
    return jsonify(result)


@app.route("/export-stats", methods=["GET"])
def export_stats_endpoint():
    """Endpoint to manually trigger saving the proxy_stats.json file."""
    try:
        proxy_manager.save_stats()
        return jsonify({"message": f"Stats successfully saved to {STATS_FILE_PATH}"})
    except Exception as e:
        return jsonify({"error": f"Failed to save stats: {e}"}), 500


def handle_shutdown(signal, frame):
    """
    Saves statistics upon receiving a shutdown signal.
    """
    logger.info("\nShutdown signal received, saving statistics...")
    proxy_manager.save_stats()
    sys.exit(0)


if __name__ == "__main__":
    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    # Create dummy proxy file if it doesn't exist
    if not os.path.exists(PROXY_FILE_PATH):
        logger.info(
            f"Creating a dummy proxy file at '{PROXY_FILE_PATH}'. Please populate it."
        )
        with open(PROXY_FILE_PATH, "w") as f:
            f.write(
                "# Add proxies here in the format: protocol:host:port or protocol:host:port:user:pass\n"
            )
            f.write("# Example:\n")
            f.write("# http:127.0.0.1:8080\n")
            f.write("# socks5:user:password@proxy.example.com:1080\n")

    # Start the Flask server
    app.run(host="0.0.0.0", port=6942, debug=True, use_reloader=False)
