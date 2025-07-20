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
PROXY_FILE_PATH = "data/proxies.txt"
STATS_FILE_PATH = "data/proxy_stats.json"
CONSECUTIVE_FAILURE_THRESHOLD = (
    8  # Number of consecutive failures before a proxy is banned
)
COOL_DOWN_SECONDS = 30  # Cooldown in seconds after a proxy is used (not currently used in logic, but kept for future)
MIN_POOL_SIZE = 200  # Minimum number of proxies in the available pool for a source
# --- [NEW] Default sources to be initialized on startup ---
DEFAULT_SOURCES = ["insolvencydirect", "test"]


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
        # --- [MODIFIED] Ensure default sources are initialized ---
        self._ensure_default_sources()
        # Initialize available pools based on loaded stats
        self._initialize_pools()

    def _ensure_default_sources(self):
        """
        Ensures that the default sources are present in the stats.
        """
        with self.lock:
            for source in DEFAULT_SOURCES:
                if source not in self.stats:
                    self.stats[source] = {}
                    logger.info(f"Initialized new default source '{source}' in stats.")
                    # Initialize stats for all existing proxies for this new default source
                    for p in self.proxies:
                        if p["url"] not in self.stats[source]:
                            self.stats[source][p["url"]] = {
                                "success_count": 0,
                                "failure_count": 0,
                                "consecutive_failures": 0,
                                "avg_response_time_ms": float("inf"),
                                "is_banned": False,
                                "last_used": 0,
                            }

    def load_proxies(self, is_reload: bool = False) -> Dict:
        """
        Loads and deduplicates the proxy list from a file with enhanced validation.
        """
        if not os.path.exists(self.proxy_file):
            message = f"Warning: Proxy file '{self.proxy_file}' does not exist."
            logger.info(message)
            return {
                "message": message,
                "added": 0,
                "skipped_duplicates": 0,
                "skipped_malformed": 0,
                "total": len(self.proxies),
            }

        unique_proxy_lines = set()
        try:
            with open(self.proxy_file, "r", encoding="utf-8") as f:
                for line in f:
                    stripped_line = line.strip()
                    if stripped_line and not stripped_line.startswith("#"):
                        unique_proxy_lines.add(stripped_line)
        except Exception as e:
            logger.error(f"Failed to read proxy file {self.proxy_file}: {e}")
            return {"message": f"Error reading proxy file: {e}"}

        added_count = 0
        skipped_duplicates_count = 0
        skipped_malformed_count = 0

        with self.lock:
            existing_proxy_urls = {p["url"] for p in self.proxies}

            for line in unique_proxy_lines:
                try:
                    parts = line.split(":")
                    proxy_info = {}
                    url = ""

                    if len(parts) == 3:  # protocol:host:port
                        protocol, host, port_str = parts[0].lower(), parts[1], parts[2]

                        if protocol not in ["http", "https", "socks4", "socks5"]:
                            skipped_malformed_count += 1
                            continue
                        if not host:
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
                        continue

                        protocol, host, port_str, user, password = (
                            parts[0].lower(),
                            parts[1],
                            parts[2],
                            parts[3],
                            parts[4],
                        )

                        if protocol not in ["http", "https", "socks4", "socks5"]:
                            skipped_malformed_count += 1
                            continue
                        if not host:
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

                        # --- [MODIFIED] Initialize stats for the new proxy across all known sources ---
                        for source in self.stats:
                            if url not in self.stats[source]:
                                self.stats[source][url] = {
                                    "success_count": 0,
                                    "failure_count": 0,
                                    "consecutive_failures": 0,
                                    "avg_response_time_ms": float("inf"),
                                    "is_banned": False,
                                    "last_used": 0,
                                }
                            # Add to available pool if not banned
                            if not self.stats[source][url].get("is_banned", False):
                                if (
                                    source in self.available_pools
                                    and url not in self.available_pools[source]
                                ):
                                    self.available_pools[source].append(url)
                                elif source not in self.available_pools:
                                    self.available_pools[source] = [url]

                        added_count += 1
                    else:
                        skipped_duplicates_count += 1
                except (ValueError, IndexError):
                    skipped_malformed_count += 1
                    continue

        load_type = "reload" if is_reload else "load"
        message = (
            f"Proxy {load_type} finished. Added: {added_count}, "
            f"Skipped (duplicates): {skipped_duplicates_count}, "
            f"Skipped (malformed): {skipped_malformed_count}, Total: {len(self.proxies)}"
        )
        logger.info(message)
        return {
            "message": message,
            "added": added_count,
            "skipped_duplicates": skipped_duplicates_count,
            "skipped_malformed": skipped_malformed_count,
            "total": len(self.proxies),
        }

    def _load_stats(self):
        """
        Loads historical performance statistics from the JSON file.
        """
        if os.path.exists(self.stats_file):
            with self.lock:
                try:
                    with open(self.stats_file, "r", encoding="utf-8") as f:
                        self.stats = json.load(f)
                    logger.info(
                        f"Successfully loaded performance stats from '{self.stats_file}'."
                    )
                except json.JSONDecodeError:
                    logger.error(
                        f"Error decoding JSON from {self.stats_file}. Starting with empty stats."
                    )
                    self.stats = {}
                except Exception as e:
                    logger.error(f"Failed to load stats file {self.stats_file}: {e}")
                    self.stats = {}

    def _initialize_pools(self):
        """
        Initializes the available proxy pools based on loaded stats.
        """
        with self.lock:
            all_proxy_urls = {p["url"] for p in self.proxies}
            for source, source_stats in self.stats.items():
                self.available_pools[source] = [
                    url
                    for url in all_proxy_urls
                    if url in source_stats
                    and not source_stats.get(url, {}).get("is_banned", False)
                ]
            logger.info("Initialized available proxy pools for all sources.")

    def save_stats(self):
        """
        Saves the current performance statistics to the JSON file.
        """
        with self.lock:
            stats_dir = os.path.dirname(self.stats_file)
            if stats_dir:
                os.makedirs(stats_dir, exist_ok=True)
            try:
                with open(self.stats_file, "w", encoding="utf-8") as f:
                    json.dump(self.stats, f, indent=4)
                logger.info(
                    f"Performance stats successfully saved to '{self.stats_file}'."
                )
            except Exception as e:
                logger.error(f"Failed to save stats to {self.stats_file}: {e}")

    def get_proxy_for_source(self, source: str) -> Optional[str]:
        """
        Gets a random proxy from the source's available pool.
        If the pool is too small, it attempts to revive a banned proxy using enhanced logic.
        """
        if not self.proxies:
            return None

        with self.lock:
            if source not in self.stats:
                self.stats[source] = {}
                self.available_pools[source] = []
                for p in self.proxies:
                    self.stats[source][p["url"]] = {
                        "success_count": 0,
                        "failure_count": 0,
                        "consecutive_failures": 0,
                        "avg_response_time_ms": float("inf"),
                        "is_banned": False,
                        "last_used": 0,
                    }
                self.available_pools[source] = [p["url"] for p in self.proxies]
                logger.info(
                    f"Initialized new pool for source '{source}' with {len(self.proxies)} proxies."
                )

            source_pool = self.available_pools.get(source, [])
            source_stats = self.stats.get(source, {})

            # --- [MODIFIED] Enhanced revival logic ---
            if len(source_pool) < MIN_POOL_SIZE:
                logger.warning(
                    f"Pool for '{source}' has {len(source_pool)} proxies, below threshold {MIN_POOL_SIZE}. Attempting revival."
                )

                proxy_to_revive_url = None

                # 1. Try to find a banned proxy with a history of success
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
                    # 2. If none found, find any banned proxy as a last resort
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

                # If a proxy was chosen for revival, update its status
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
        """
        Processes feedback and updates the proxy's status and the available pool.
        """
        source = feedback_data.get("source")
        proxy_url = feedback_data.get("proxy")
        status = feedback_data.get("status")
        response_time_ms = feedback_data.get("response_time_ms")

        logger.info(
            f"Feedback: proxy-[{proxy_url}], status-[{status}], source-[{source}], speed-[{response_time_ms}]"
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
                            f"Info: Reinstated proxy {proxy_url} for source '{source}' after success."
                        )

                if (
                    not isinstance(response_time_ms, (int, float))
                    or response_time_ms < 0
                ):
                    logger.warning(
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
                    if proxy_url in self.available_pools.get(source, []):
                        self.available_pools[source].remove(proxy_url)
                        logger.info(
                            f"Info: Removed {proxy_url} from '{source}' available pool."
                        )
            return True

    def get_source_stats(self, source: str) -> Optional[Dict]:
        """
        Gets statistics for a given source, including available proxy count and list.
        """
        with self.lock:
            if source not in self.available_pools:
                self.get_proxy_for_source(source)

            source_pool = self.available_pools.get(source, [])
            return {
                "source": source,
                "available_proxies_count": len(source_pool),
                "available_proxies_list": source_pool,
            }


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
        return jsonify({"error": "Invalid or incomplete feedback data provided."}), 400


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


@app.route("/stats/<string:source>", methods=["GET"])
def get_source_stats_endpoint(source: str):
    """
    Endpoint to get statistics for a specific source, including
    the number of available proxies and a list of them.
    """
    stats = proxy_manager.get_source_stats(source)

    if stats:
        return jsonify(stats)
    else:
        return (
            jsonify({"error": f"No statistics found for source '{source}'."}),
            404,
        )


def handle_shutdown(signal, frame):
    """
    Saves statistics upon receiving a shutdown signal.
    """
    logger.info("\nShutdown signal received, saving statistics...")
    proxy_manager.save_stats()
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    proxy_dir = os.path.dirname(PROXY_FILE_PATH)
    if proxy_dir:
        os.makedirs(proxy_dir, exist_ok=True)

    if not os.path.exists(PROXY_FILE_PATH):
        logger.info(
            f"Creating a dummy proxy file at '{PROXY_FILE_PATH}'. Please populate it."
        )
        with open(PROXY_FILE_PATH, "w", encoding="utf-8") as f:
            f.write(
                "# Add proxies here in the format: protocol:host:port or protocol:host:port:user:pass\n"
            )
            f.write("# Example:\n")
            f.write("# http:127.0.0.1:8080\n")
            f.write("# socks5:user:password@proxy.example.com:1080\n")

    app.run(host="0.0.0.0", port=6942, debug=True, use_reloader=False)
