# -*- coding: utf-8 -*-
import configparser
import json
import os
import random
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Set, Tuple

import psycopg2
import psycopg2.pool

# --- [FIXED] Missing import for extras module ---
import psycopg2.extras
import requests
from flask import Flask, jsonify, request

from logger import logger

# --- Configuration & Constants ---
CONFIG_FILE_PATH = os.path.join("./", "config.ini")


class DatabaseManager:
    """Handles all interactions with the PostgreSQL database."""

    def __init__(self, config):
        try:
            self.pool = psycopg2.pool.SimpleConnectionPool(
                minconn=1,
                maxconn=10,
                host=config.get("database", "host"),
                port=config.get("database", "port"),
                dbname=config.get("database", "dbname"),
                user=config.get("database", "user"),
                password=config.get("database", "password"),
            )
            logger.info("Database connection pool created successfully.")
        except (configparser.NoSectionError, psycopg2.OperationalError) as e:
            logger.error(f"Database configuration error or connection failed: {e}")
            sys.exit(1)

    def _execute(self, query, params=None, fetch=None):
        """A helper to execute queries using a connection from the pool."""
        conn = None
        try:
            conn = self.pool.getconn()
            with conn.cursor() as cur:
                cur.execute(query, params)
                if fetch == "one":
                    return cur.fetchone()
                if fetch == "all":
                    return cur.fetchall()
                conn.commit()
        except psycopg2.Error as e:
            logger.error(f"Database query failed: {e}")
            if conn:
                conn.rollback()
            return None
        finally:
            if conn:
                self.pool.putconn(conn)

    def insert_proxies(self, proxies: List[Tuple[str, str, int]]):
        """Inserts a list of proxies, ignoring duplicates."""
        if not proxies:
            return
        # --- [OPTIMIZED] Using execute_values for better performance ---
        query = """
            INSERT INTO proxies (protocol, ip, port) VALUES %s
            ON CONFLICT (protocol, ip, port) DO NOTHING;
        """
        conn = None
        try:
            conn = self.pool.getconn()
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(cur, query, proxies)
                conn.commit()
            logger.info(
                f"Attempted to insert {len(proxies)} proxies. Duplicates were ignored."
            )
        except psycopg2.Error as e:
            logger.error(f"Database batch insert failed: {e}")
            if conn:
                conn.rollback()
        finally:
            if conn:
                self.pool.putconn(conn)

    def get_proxies_to_validate(self, interval_minutes=30) -> List[Tuple[int, str]]:
        """Selects proxies that need validation."""
        query = """
            SELECT id, protocol, ip, port FROM proxies
            WHERE last_validated_at IS NULL 
            OR (is_active = true AND last_validated_at < NOW() - INTERVAL '%s minutes');
        """
        return self._execute(query, (interval_minutes,), fetch="all") or []

    def update_proxy_validation_result(
        self,
        proxy_id: int,
        is_active: bool,
        latency: Optional[int],
        anonymity: Optional[str],
    ):
        """Updates a proxy's record after a validation attempt."""
        query = """
            UPDATE proxies 
            SET is_active = %s, latency_ms = %s, anonymity_level = %s, last_validated_at = NOW()
            WHERE id = %s;
        """
        self._execute(query, (is_active, latency, anonymity, proxy_id))

    def get_active_proxies(self) -> Set[str]:
        """Gets the set of all currently active proxy URLs."""
        query = "SELECT protocol, ip, port FROM proxies WHERE is_active = true;"
        rows = self._execute(query, fetch="all")
        return {f"{row[0]}://{row[1]}:{row[2]}" for row in rows} if rows else set()


class ProxyManager:
    """Manages the proxy lifecycle, state, and business logic."""

    def __init__(self, config_path):
        self.config = configparser.ConfigParser()
        self.config.read(config_path, encoding="utf-8")

        self.db = DatabaseManager(self.config)
        self.lock = threading.Lock()

        # --- In-Memory State ---
        self.active_proxies: Set[str] = set()
        self.source_stats: Dict[str, Dict[str, Dict]] = {}

        # --- Scheduler & Components ---
        self.fetcher_jobs = self._load_fetcher_jobs()
        self.scheduler_thread = None
        self.stop_scheduler_event = threading.Event()
        self.fetch_executor = ThreadPoolExecutor(
            max_workers=10, thread_name_prefix="Fetcher"
        )
        self.validation_workers = self.config.getint(
            "validator", "validation_workers", fallback=100
        )
        self.validation_timeout_s = self.config.getint(
            "validator", "validation_timeout_s", fallback=5
        )
        self.validation_target = self.config.get(
            "validator", "validation_target", fallback="http://httpbin.org/get"
        )

    def _load_fetcher_jobs(self) -> List[Dict]:
        """Loads proxy source jobs from the config file."""
        jobs = []
        for section in self.config.sections():
            if section.startswith("proxy_source_"):
                job = {
                    "name": section,
                    "url": self.config.get(section, "url", fallback=None),
                    "interval_minutes": self.config.getint(
                        section, "update_interval_minutes", fallback=60
                    ),
                    "default_protocol": self.config.get(
                        section, "default_protocol", fallback=None
                    ),
                    "last_run": 0,
                }
                if job["url"]:
                    jobs.append(job)
        logger.info(f"Loaded {len(jobs)} proxy source jobs from config.")
        return jobs

    def _fetch_and_parse_source(self, job: Dict):
        """Fetches a source, parses proxies, and inserts them into the DB."""
        url = job["url"]
        logger.info(f"Fetching proxy source: {job['name']}")
        try:
            response = requests.get(url, timeout=15)
            response.raise_for_status()

            proxies_to_insert = []
            lines = response.text.splitlines()
            logger.info(f"Read {len(lines)} lines from {job['name']}.")
            for line in lines:
                line = line.strip()
                if not line:
                    continue

                protocol, ip, port = None, None, None
                if "://" in line:
                    parts = line.split("://")
                    protocol = parts[0]
                    ip_port = parts[1]
                elif job["default_protocol"]:
                    protocol = job["default_protocol"]
                    ip_port = line

                if protocol and ":" in ip_port:
                    ip_port_parts = ip_port.rsplit(":", 1)
                    ip, port_str = ip_port_parts[0], ip_port_parts[1]
                    try:
                        port = int(port_str)
                        proxies_to_insert.append((protocol, ip, port))
                    except ValueError:
                        logger.warning(f"Skipping malformed proxy line: {line}")
                        continue

            logger.info(
                f"Parsed {len(proxies_to_insert)} valid proxies from {job['name']}."
            )
            self.db.insert_proxies(proxies_to_insert)
        except requests.RequestException as e:
            logger.error(f"Failed to fetch from {job['name']} ({url}): {e}")

    def _validate_proxy(self, proxy_id: int, proxy_url: str):
        """Validates a single proxy and updates its status in the DB."""
        proxies = {"http": proxy_url, "https": proxy_url}
        start_time = time.time()
        try:
            response = requests.get(
                self.validation_target,
                proxies=proxies,
                timeout=self.validation_timeout_s,
            )
            response.raise_for_status()
            latency_ms = int((time.time() - start_time) * 1000)
            headers = response.json().get("headers", {})
            is_anonymous = not any(
                h in headers for h in ["X-Forwarded-For", "Via", "X-Real-Ip"]
            )
            anonymity = "elite" if is_anonymous else "transparent"
            self.db.update_proxy_validation_result(
                proxy_id, True, latency_ms, anonymity
            )
            return True
        except (requests.RequestException, json.JSONDecodeError):
            self.db.update_proxy_validation_result(proxy_id, False, None, None)
            return False

    def _run_validation_cycle(self):
        """Runs a full validation cycle on proxies from the database."""
        proxies_to_validate = self.db.get_proxies_to_validate()
        if not proxies_to_validate:
            logger.info("Validation cycle skipped: no proxies to validate.")
            return

        total_to_validate = len(proxies_to_validate)
        logger.info(f"Starting validation for {total_to_validate} proxies...")

        success_count = 0
        processed_count = 0

        with ThreadPoolExecutor(max_workers=self.validation_workers) as executor:
            future_to_proxy = {
                executor.submit(
                    self._validate_proxy, proxy_id, f"{protocol}://{ip}:{port}"
                ): proxy_id
                for proxy_id, protocol, ip, port in proxies_to_validate
            }

            for future in as_completed(future_to_proxy):
                processed_count += 1
                result = future.result()
                if result:
                    success_count += 1

                # --- [NEW] Added progress logging ---
                if processed_count % 100 == 0 or processed_count == total_to_validate:
                    logger.info(
                        f"Validation progress: {processed_count}/{total_to_validate} proxies checked."
                    )

        logger.info(
            f"Validation cycle finished. Success: {success_count}, Failed: {total_to_validate - success_count}."
        )
        self._sync_active_proxies_from_db()

    def _sync_active_proxies_from_db(self):
        """Updates the in-memory set of active proxies from the database."""
        logger.info("Syncing active proxies from DB to memory...")
        active_proxies_from_db = self.db.get_active_proxies()
        with self.lock:
            self.active_proxies = active_proxies_from_db
            for source, stats in self.source_stats.items():
                inactive_urls = set(stats.keys()) - self.active_proxies
                for url in inactive_urls:
                    del self.source_stats[source][url]
        logger.info(
            f"Sync complete. In-memory active pool size: {len(self.active_proxies)}"
        )

    def _scheduler_loop(self):
        """The main loop for the background scheduler."""
        validation_interval = self.config.getint(
            "scheduler", "validation_interval_seconds", fallback=60
        )
        last_validation_run = 0
        logger.info("Scheduler loop started.")
        while not self.stop_scheduler_event.is_set():
            now = time.time()
            try:
                # Check and run fetcher jobs
                for job in self.fetcher_jobs:
                    if now - job["last_run"] >= job["interval_minutes"] * 60:
                        logger.info(f"Scheduler triggering fetch job: {job['name']}")
                        job["last_run"] = now
                        self.fetch_executor.submit(self._fetch_and_parse_source, job)

                # Check and run validation cycle at a fixed rate
                if now - last_validation_run >= validation_interval:
                    logger.info("Scheduler triggering validation cycle.")
                    last_validation_run = now
                    # Run validation in a separate thread to not block the scheduler loop
                    threading.Thread(target=self._run_validation_cycle).start()

                self.stop_scheduler_event.wait(5)
            except Exception as e:
                logger.error(f"Error in scheduler loop: {e}", exc_info=True)
                self.stop_scheduler_event.wait(60)

    def start_scheduler(self):
        if not self.scheduler_thread or not self.scheduler_thread.is_alive():
            self.stop_scheduler_event.clear()
            self.scheduler_thread = threading.Thread(
                target=self._scheduler_loop, daemon=True
            )
            self.scheduler_thread.start()
            logger.info("Background scheduler started.")

    def stop_scheduler(self):
        if self.scheduler_thread and self.scheduler_thread.is_alive():
            self.stop_scheduler_event.set()
            self.fetch_executor.shutdown(wait=True)
            self.scheduler_thread.join(timeout=10)
            logger.info("Background scheduler stopped.")

    def _get_new_source_stat(self) -> Dict:
        return {
            "score": 0,
            "success_count": 0,
            "failure_count": 0,
            "is_banned": False,
            "banned_until": None,
        }

    def _ensure_source_exists(self, source: str):
        """Initializes a new in-memory source pool if it doesn't exist."""
        if source not in self.source_stats:
            with self.lock:
                if source not in self.source_stats:
                    self.source_stats[source] = {
                        url: self._get_new_source_stat() for url in self.active_proxies
                    }
                    logger.info(
                        f"Initialized new in-memory source stats for '{source}' with {len(self.source_stats[source])} proxies."
                    )

    def get_proxy(self, source: str) -> Optional[str]:
        """Gets a high-scoring proxy for a specific source from in-memory stats."""
        self._ensure_source_exists(source)
        with self.lock:
            stats = self.source_stats.get(source, {})
            now = time.time()

            available_proxies = []
            for proxy_url, stat_data in stats.items():
                if stat_data["is_banned"]:
                    if stat_data["banned_until"] and now > stat_data["banned_until"]:
                        stat_data.update(
                            {"is_banned": False, "banned_until": None, "score": 0}
                        )
                        logger.info(
                            f"Proxy {proxy_url} auto-unbanned for source '{source}'."
                        )
                        available_proxies.append({"url": proxy_url, **stat_data})
                else:
                    available_proxies.append({"url": proxy_url, **stat_data})

            if not available_proxies:
                return None

            weights = [max(p["score"], 0) + 1 for p in available_proxies]
            return random.choices(available_proxies, weights=weights, k=1)[0]["url"]

    def process_feedback(self, source: str, proxy_url: str, status: str):
        """Processes feedback in-memory."""
        self._ensure_source_exists(source)
        with self.lock:
            stat = self.source_stats.get(source, {}).get(proxy_url)
            if not stat:
                return

            if status == "success":
                stat["success_count"] += 1
                stat["score"] += 1
            elif status == "failure":
                stat["failure_count"] += 1
                stat["score"] -= 5
                if stat["score"] <= self.config.getint(
                    "source_pool", "score_threshold_ban", fallback=-15
                ):
                    stat["is_banned"] = True
                    stat["banned_until"] = time.time() + (
                        self.config.getint(
                            "source_pool", "cooldown_minutes", fallback=30
                        )
                        * 60
                    )
                    logger.warning(f"Proxy {proxy_url} banned for source '{source}'.")


def load_proxy_manager(config_path: str) -> ProxyManager:
    """Loads ProxyManager and initializes its state, removing pickle dependency."""
    logger.info("Initializing ProxyManager...")
    manager = ProxyManager(config_path)

    # --- [MODIFIED] Simplified startup: always sync from DB ---
    manager._sync_active_proxies_from_db()

    # --- [MODIFIED] Cold start is now determined by whether the DB has active proxies ---
    if not manager.active_proxies:
        logger.warning(
            "Cold start detected (no active proxies in DB). Running initial synchronous fetch and validation..."
        )
        # Run fetch jobs synchronously first
        fetch_futures = [
            manager.fetch_executor.submit(manager._fetch_and_parse_source, job)
            for job in manager.fetcher_jobs
        ]
        for future in as_completed(fetch_futures):
            pass  # Wait for all fetchers to finish
        logger.info("Initial fetch complete.")

        # Then run validation
        manager._run_validation_cycle()
        logger.info("Initial validation complete. Service is ready.")

    return manager


# --- Flask API Server ---
app = Flask(__name__)
# --- [MODIFIED] Removed pickle path from load function call ---
proxy_manager = load_proxy_manager(CONFIG_FILE_PATH)


@app.route("/get-proxy", methods=["GET"])
def get_proxy():
    source = request.args.get("source")
    if not source:
        return jsonify({"error": "Query parameter 'source' is required."}), 400
    proxy_url = proxy_manager.get_proxy(source)
    if proxy_url:
        return jsonify({"proxy": proxy_url})
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
    source, proxy_url, status = (
        data.get("source"),
        data.get("proxy"),
        data.get("status"),
    )
    if not all([source, proxy_url, status]) or status not in ["success", "failure"]:
        return jsonify({"error": "Invalid feedback data."}), 400
    proxy_manager.process_feedback(source, proxy_url, status)
    return jsonify({"message": "Feedback received."})


def handle_shutdown(signal, frame):
    logger.info("Shutdown signal received. Stopping scheduler...")
    proxy_manager.stop_scheduler()
    # --- [REMOVED] No longer saving stats to pickle on shutdown ---
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)
    proxy_manager.start_scheduler()
    # --- [FIXED] Changed port back to 6942 for consistency ---
    app.run(host="0.0.0.0", port=6952, debug=False)
