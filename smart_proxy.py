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

# --- Configuration ---
PROXY_FILE_PATH = "proxies.txt"
STATS_FILE_PATH = "proxy_stats.json"
CONSECUTIVE_FAILURE_THRESHOLD = (
    10  # Number of consecutive failures before a proxy is banned
)
COOL_DOWN_SECONDS = 30  # Cooldown in seconds after a proxy is used


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
        self.lock = threading.Lock()  # Lock for thread-safe access to stats and proxies
        self.load_proxies()
        self._load_stats()

    def load_proxies(self, is_reload: bool = False) -> Dict:
        """Loads and deduplicates the proxy list from a file."""
        if not os.path.exists(self.proxy_file):
            message = f"Warning: Proxy file '{self.proxy_file}' does not exist."
            print(message)
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
            # Create a set of existing proxy URLs for quick lookups
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
                            print(f"Skipping malformed line on reload: {line}")
                        continue

                    if url not in existing_proxy_urls:
                        self.proxies.append(proxy_info)
                        existing_proxy_urls.add(url)
                        added_count += 1
                    else:
                        skipped_count += 1
                except (ValueError, IndexError):
                    if is_reload:
                        print(f"Skipping malformed line on reload: {line}")

        load_type = "reload" if is_reload else "load"
        message = f"Proxy {load_type} finished. Added: {added_count}, Skipped (duplicates): {skipped_count}, Total: {len(self.proxies)}"
        print(message)
        return {
            "message": message,
            "added": added_count,
            "skipped": skipped_count,
            "total": len(self.proxies),
        }

    def _load_stats(self):
        """Loads historical performance statistics from the JSON file."""
        if os.path.exists(self.stats_file):
            with self.lock:
                with open(self.stats_file, "r") as f:
                    self.stats = json.load(f)
                print(
                    f"Successfully loaded performance stats from '{self.stats_file}'."
                )

    def save_stats(self):
        """Saves the current performance statistics to the JSON file."""
        with self.lock:
            with open(self.stats_file, "w") as f:
                json.dump(self.stats, f, indent=4)
            print(f"Performance stats successfully saved to '{self.stats_file}'.")

    def get_proxy_for_source(self, source: str) -> Optional[str]:
        """
        Gets the best proxy for a given source.
        If all proxies are banned, it attempts to revive one that has a history of success.
        """
        if not self.proxies:
            return None

        with self.lock:
            if source not in self.stats:
                self.stats[source] = {}

            source_stats = self.stats[source]

            # Ensure all proxies from the list are present in the stats for this source
            all_proxy_urls = {p["url"] for p in self.proxies}
            for proxy_url in all_proxy_urls:
                if proxy_url not in source_stats:
                    source_stats[proxy_url] = {
                        "success_count": 0,
                        "failure_count": 0,
                        "consecutive_failures": 0,
                        "avg_response_time_ms": float("inf"),
                        "is_banned": False,
                        "last_used": 0,
                    }

            # 1. Try to select from available (non-banned) proxies
            available_proxies = [
                p for p in self.proxies if not source_stats[p["url"]]["is_banned"]
            ]

            now = time.time()
            chosen_proxy = None

            if available_proxies:
                # Prioritize proxies that have never been tried
                untried_proxies = [
                    p
                    for p in available_proxies
                    if source_stats[p["url"]]["success_count"] == 0
                    and source_stats[p["url"]]["failure_count"] == 0
                ]
                if untried_proxies:
                    chosen_proxy = random.choice(untried_proxies)
                else:
                    # Select from tried proxies, preferring those not in cooldown
                    non_cooled_down_proxies = [
                        p
                        for p in available_proxies
                        if (now - source_stats[p["url"]]["last_used"])
                        > COOL_DOWN_SECONDS
                    ]

                    selection_pool = (
                        non_cooled_down_proxies
                        if non_cooled_down_proxies
                        else available_proxies
                    )

                    # --- OPTIMIZATION: Instead of picking the best, pick randomly from the best ---
                    # Sort by performance to identify the best proxies
                    selection_pool.sort(
                        key=lambda p: source_stats[p["url"]]["avg_response_time_ms"]
                    )

                    # Create a smaller pool of top performers to choose from
                    # e.g., top 5 or top 20% of the pool, whichever is smaller
                    top_n = min(5, len(selection_pool) // 5 + 1)
                    top_performers_pool = selection_pool[:top_n]

                    # Randomly choose from this high-quality pool
                    chosen_proxy = random.choice(top_performers_pool)
                    # --- END OPTIMIZATION ---

            # 2. If no proxies are available (all are banned), enter 'revival' mode
            else:
                print(
                    f"Warning: All proxies for source '{source}' are banned. Attempting to revive one..."
                )
                # Find all banned proxies that have at least one past success
                banned_but_successful_proxies = [
                    p
                    for p in self.proxies
                    if source_stats[p["url"]]["is_banned"]
                    and source_stats[p["url"]]["success_count"] > 0
                ]

                if banned_but_successful_proxies:
                    # Randomly choose a proxy to revive from the successful-but-banned list
                    proxy_to_revive = random.choice(banned_but_successful_proxies)
                    print(
                        f"Info: Reviving proxy for source '{source}': {proxy_to_revive['url']}"
                    )

                    # Un-ban the proxy and reset its consecutive failure count
                    source_stats[proxy_to_revive["url"]]["is_banned"] = False
                    source_stats[proxy_to_revive["url"]]["consecutive_failures"] = 0
                    chosen_proxy = proxy_to_revive
                else:
                    # If no banned proxies have a success history, revive a random one as a last resort
                    if self.proxies:
                        proxy_to_revive = random.choice(self.proxies)
                        print(
                            f"Warning: No successful proxies to revive. Reviving a random proxy for source '{source}': {proxy_to_revive['url']}"
                        )
                        source_stats[proxy_to_revive["url"]]["is_banned"] = False
                        source_stats[proxy_to_revive["url"]]["consecutive_failures"] = 0
                        chosen_proxy = proxy_to_revive

            if chosen_proxy:
                source_stats[chosen_proxy["url"]]["last_used"] = now
                return chosen_proxy["url"]

            return None  # Finally, if no proxy could be chosen, return None

    def process_feedback(self, feedback_data: Dict):
        """Processes feedback from a client about a proxy's performance."""
        source = feedback_data.get("source")
        proxy_url = feedback_data.get("proxy")
        status = feedback_data.get("status")
        response_time_ms = feedback_data.get("response_time_ms")

        if not all([source, proxy_url, status]):
            return False

        with self.lock:
            if source not in self.stats or proxy_url not in self.stats[source]:
                return False

            stat = self.stats[source][proxy_url]

            if status == "success":
                stat["success_count"] += 1
                stat["consecutive_failures"] = 0
                stat["is_banned"] = False

                if not isinstance(response_time_ms, (int, float)):
                    print(
                        f"Warning: Success feedback for source '{source}' is missing a valid 'response_time_ms'."
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
                if stat["consecutive_failures"] >= CONSECUTIVE_FAILURE_THRESHOLD:
                    stat["is_banned"] = True
                    print(
                        f"Info: Proxy {proxy_url} has been banned for source '{source}'."
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
    """Saves statistics upon receiving a shutdown signal."""
    print("\nShutdown signal received, saving statistics...")
    proxy_manager.save_stats()
    sys.exit(0)


if __name__ == "__main__":
    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    # Start the Flask server
    app.run(host="0.0.0.0", port=6942)
