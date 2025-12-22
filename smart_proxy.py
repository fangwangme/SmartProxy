# -*- coding: utf-8 -*-
import configparser
import json
import os
import random
import signal
import subprocess
import sys
import threading
import time
import argparse
import asyncio
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import aiohttp

import psycopg2
import psycopg2.pool
import psycopg2.extras
import requests
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

from logger import logger, setup_logging

# --- Configuration & Constants ---
CONFIG_FILE_PATH = os.path.join("./", "config.ini")
FAILED_STATUS_CODES = {
    0,
    4,
}  # Set of status codes that indicate failure (0=timeout, 4=proxy error)


class DatabaseManager:
    """Handles all interactions with the PostgreSQL database."""

    def __init__(self, config):
        try:
            # OPTIMIZATION: Increased maxconn to better handle concurrent workers.
            # The ideal number depends on your DB server's capacity.
            max_connections = config.getint("database", "max_connections", fallback=50)
            # Use ThreadedConnectionPool for thread-safe access in multi-threaded environment
            self.pool = psycopg2.pool.ThreadedConnectionPool(
                minconn=2,
                maxconn=max_connections,
                host=config.get("database", "host"),
                port=config.get("database", "port"),
                dbname=config.get("database", "dbname"),
                user=config.get("database", "user"),
                password=config.get("database", "password"),
            )
            logger.info(
                f"Database connection pool created successfully (max_conn={max_connections})."
            )
        except (configparser.NoSectionError, psycopg2.OperationalError) as e:
            logger.error(f"Database configuration error or connection failed: {e}")
            sys.exit(1)

    def _execute(self, query, params=None, fetch=None):
        """A helper to execute queries using a connection from the pool."""
        conn = None
        try:
            conn = self.pool.getconn()
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
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
        query = "INSERT INTO proxies (protocol, ip, port) VALUES %s ON CONFLICT (protocol, ip, port) DO NOTHING;"
        conn = None
        try:
            conn = self.pool.getconn()
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(cur, query, proxies)
                logger.info(f"Inserted {cur.rowcount}/ {len(proxies)} new proxies.")
                conn.commit()
        except Exception as e:
            logger.error(f"Database batch insert failed: {e}")
            if conn:
                conn.rollback()
        finally:
            if conn:
                self.pool.putconn(conn)

    def get_proxies_to_validate(self, interval_minutes=30, limit=2000) -> List[Tuple]:
        query = "SELECT id, protocol, ip, port FROM proxies WHERE last_validated_at IS NULL OR (is_active = true AND last_validated_at < NOW() - INTERVAL '%s minutes') LIMIT %s;"
        return self._execute(query, (interval_minutes, limit), fetch="all") or []

    def get_eligible_failed_proxies(
        self, window_minutes: int, max_attempts: int, limit: int
    ) -> List[Tuple]:
        """
        Gets recently failed proxies that are eligible for re-validation based on the time window and attempt count.
        """
        query = """
            SELECT id, protocol, ip, port
            FROM proxies
            WHERE is_active = false
            AND (
                window_start_time IS NULL OR
                NOW() > window_start_time + INTERVAL '%(window)s minutes' OR
                validation_attempts_in_window < %(max_attempts)s
            )
            ORDER BY last_validated_at DESC NULLS FIRST, created_at DESC
            LIMIT %(limit)s;
        """
        params = {
            "window": window_minutes,
            "max_attempts": max_attempts,
            "limit": limit,
        }
        return self._execute(query, params, fetch="all") or []

    def update_validation_counters(self, proxy_ids: List[int], window_minutes: int):
        """
        Updates the validation counters for a batch of proxies before they are validated.
        Resets the counter and window if the window has expired.
        """
        if not proxy_ids:
            return

        query = """
            UPDATE proxies
            SET
                validation_attempts_in_window = CASE
                    WHEN window_start_time IS NULL OR NOW() > window_start_time + INTERVAL '%(window)s minutes'
                    THEN 1
                    ELSE validation_attempts_in_window + 1
                END,
                window_start_time = CASE
                    WHEN window_start_time IS NULL OR NOW() > window_start_time + INTERVAL '%(window)s minutes'
                    THEN NOW()
                    ELSE window_start_time
                END
            WHERE id = ANY(%(ids)s);
        """
        params = {"window": window_minutes, "ids": proxy_ids}
        self._execute(query, params)
        logger.debug(f"Updated validation counters for {len(proxy_ids)} proxies.")

    def batch_update_proxy_results(
        self, success_proxies: List[Dict], failure_proxy_ids: List[int]
    ):
        """
        OPTIMIZATION: Batch update results of a validation cycle.
        """
        conn = None
        try:
            conn = self.pool.getconn()
            with conn.cursor() as cur:
                # Batch update successful proxies
                if success_proxies:
                    update_query_success = """
                        UPDATE proxies SET
                            is_active = true,
                            latency_ms = data.latency_ms,
                            anonymity_level = data.anonymity_level,
                            last_validated_at = NOW()
                        FROM (VALUES %s) AS data(id, latency_ms, anonymity_level)
                        WHERE proxies.id = data.id;
                    """
                    psycopg2.extras.execute_values(
                        cur,
                        update_query_success,
                        [
                            (p["id"], p["latency"], p["anonymity"])
                            for p in success_proxies
                        ],
                    )
                    logger.info(
                        f"Batch updated {len(success_proxies)} successful proxies."
                    )

                # Batch update failed proxies
                if failure_proxy_ids:
                    update_query_failure = """
                        UPDATE proxies SET
                            is_active = false,
                            latency_ms = NULL,
                            anonymity_level = NULL,
                            last_validated_at = NOW()
                        WHERE id = ANY(%s);
                    """
                    cur.execute(update_query_failure, (failure_proxy_ids,))
                    logger.info(
                        f"Batch updated {len(failure_proxy_ids)} failed proxies."
                    )

                conn.commit()
        except psycopg2.Error as e:
            logger.error(f"Database batch update for validation results failed: {e}")
            if conn:
                conn.rollback()
        finally:
            if conn:
                self.pool.putconn(conn)

    def get_active_proxies(self) -> Set[str]:
        query = "SELECT protocol, ip, port FROM proxies WHERE is_active = true;"
        rows = self._execute(query, fetch="all")
        return (
            {f"{row['protocol']}://{row['ip']}:{row['port']}" for row in rows}
            if rows
            else set()
        )

    def flush_feedback_stats(self, stats_buffer: List[Tuple]):
        if not stats_buffer:
            return
        query = """
            INSERT INTO source_stats_by_minute (minute, source_name, success_count, failure_count)
            VALUES %s
            ON CONFLICT (minute, source_name) DO UPDATE SET
                success_count = source_stats_by_minute.success_count + EXCLUDED.success_count,
                failure_count = source_stats_by_minute.failure_count + EXCLUDED.failure_count;
        """
        conn = None
        try:
            conn = self.pool.getconn()
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(cur, query, stats_buffer)
                conn.commit()
            flushed_minutes = sorted(
                list({item[0].strftime("%H:%M") for item in stats_buffer})
            )
            logger.info(
                f"Flushed stats for {len(stats_buffer)} source-minute combination(s). Minutes: {flushed_minutes}"
            )
        except psycopg2.Error as e:
            logger.error(f"Failed to flush feedback stats to DB: {e}")
            if conn:
                conn.rollback()
        finally:
            if conn:
                self.pool.putconn(conn)

    def get_daily_stats(self, source: str, date: str):
        query = "SELECT COALESCE(SUM(success_count), 0) as total_success, COALESCE(SUM(failure_count), 0) as total_failure FROM source_stats_by_minute WHERE source_name = %s AND DATE(minute) = %s;"
        return self._execute(query, (source, date), fetch="one")

    def get_timeseries_stats(self, source: str, date: str, interval_minutes: int):
        query = """
            SELECT
                date_trunc('hour', minute) + (EXTRACT(minute FROM minute)::int / %(interval)s * %(interval)s) * interval '1 minute' AS interval_start,
                SUM(success_count) as success,
                SUM(failure_count) as failure
            FROM source_stats_by_minute
            WHERE source_name = %(source)s AND DATE(minute) = %(date)s
            GROUP BY interval_start ORDER BY interval_start;
        """
        return self._execute(
            query,
            {"source": source, "date": date, "interval": interval_minutes},
            fetch="all",
        )

    def get_distinct_sources(self) -> List[str]:
        query = "SELECT DISTINCT source_name FROM source_stats_by_minute ORDER BY source_name;"
        rows = self._execute(query, fetch="all")
        return [row["source_name"] for row in rows] if rows else []


class ProxyManager:
    """Manages the proxy lifecycle, state, and business logic."""

    def __init__(self, config_path):
        self.config_path = config_path
        self.config = configparser.ConfigParser()
        self.config.read(config_path, encoding="utf-8")

        self.db = DatabaseManager(self.config)
        # Use RLock for reentrant locking (allows same thread to acquire lock multiple times)
        self.lock = threading.RLock()
        # Per-source locks for fine-grained concurrency control
        self.source_locks: Dict[str, threading.Lock] = {}

        self.active_proxies: Set[str] = set()
        self.source_stats: Dict[str, Dict[str, Dict]] = {}
        self.available_proxies: Dict[str, Dict[str, List[str]]] = (
            {}
        )  # MODIFIED: Structure for tiers
        self.premium_proxies: List[str] = []  # High-quality proxies for Playwright

        self.feedback_buffer = defaultdict(
            lambda: defaultdict(lambda: defaultdict(int))
        )

        self.dashboard_sources: Set[str] = set()
        self.last_source_refresh_time = 0

        self._load_config()
        self._initialize_source_pools()

        self.fetcher_jobs = self._load_fetcher_jobs()
        self.scheduler_thread = None
        self.stop_scheduler_event = threading.Event()
        self.fetch_executor = ThreadPoolExecutor(
            max_workers=10, thread_name_prefix="Fetcher"
        )
        self.is_validating = False
        self.debug_mode = False  # Set via command line --debug flag

    def _load_config(self):
        self.server_port = self.config.getint("server", "port", fallback=6942)
        self.validation_workers = self.config.getint(
            "validator", "validation_workers", fallback=100
        )
        self.validation_timeout_s = self.config.getint(
            "validator", "validation_timeout_s", fallback=5
        )
        self.validation_target = self.config.get(
            "validator", "validation_target", fallback="http://httpbin.org/get"
        )
        self.validation_supplement_threshold = self.config.getint(
            "validator", "validation_supplement_threshold", fallback=1000
        )
        self.validation_window_minutes = self.config.getint(
            "validator", "validation_window_minutes", fallback=30
        )
        self.max_validations_per_window = self.config.getint(
            "validator", "max_validations_per_window", fallback=5
        )
        self.validation_interval_s = self.config.getint(
            "scheduler", "validation_interval_seconds", fallback=60
        )
        self.stats_flush_interval_s = self.config.getint(
            "scheduler", "stats_flush_interval_seconds", fallback=60
        )
        self.source_refresh_interval_s = self.config.getint(
            "scheduler", "source_refresh_interval_seconds", fallback=300
        )
        sources_str = self.config.get(
            "sources", "predefined_sources", fallback="default"
        )
        self.predefined_sources = {
            s.strip() for s in sources_str.split(",") if s.strip()
        }
        self.default_source = self.config.get(
            "sources", "default_source", fallback="default"
        )
        if self.default_source not in self.predefined_sources:
            self.predefined_sources.add(self.default_source)
        self.max_pool_size = self.config.getint(
            "source_pool", "max_pool_size", fallback=500
        )
        self.stats_pool_max_multiplier = self.config.getint(
            "source_pool", "stats_pool_max_multiplier", fallback=20
        )
        penalties_str = self.config.get(
            "source_pool", "failure_penalties", fallback="-1, -5, -20"
        )
        self.failure_penalties = [int(p.strip()) for p in penalties_str.split(",")]

        # NEW: Load weighted selection config
        self.weighted_selection_enabled = self.config.getboolean(
            "source_pool", "weighted_selection_enabled", fallback=False
        )
        self.top_tier_size = self.config.getint(
            "source_pool", "top_tier_size", fallback=100
        )
        self.top_tier_load_percentage = self.config.getint(
            "source_pool", "top_tier_load_percentage", fallback=70
        )

        # Backup configuration
        self.stats_backup_enabled = self.config.getboolean(
            "backup", "stats_backup_enabled", fallback=True
        )
        self.stats_backup_interval_s = self.config.getint(
            "backup", "stats_backup_interval_seconds", fallback=3600  # 1 hour
        )
        self.stats_backup_path = Path(
            self.config.get("backup", "stats_backup_path", fallback="./data/proxy_stats_backup.json")
        )

        # ELO scoring configuration
        self.elo_max_window = self.config.getint(
            "source_pool", "elo_max_window", fallback=100
        )
        self.elo_scoring_window = self.config.getint(
            "source_pool", "elo_scoring_window", fallback=50
        )

        logger.info("Configuration loaded.")

    def _initialize_source_pools(self):
        with self.lock:
            for source in self.predefined_sources:
                if source not in self.source_stats:
                    self.source_stats[source] = {}
                    # MODIFIED: Initialize tiered structure
                    self.available_proxies[source] = {"top_tier": [], "bottom_tier": []}
            logger.info(
                f"Initialized in-memory pools for sources: {self.predefined_sources}"
            )

    def _load_fetcher_jobs(self) -> List[Dict]:
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
        return jobs

    def reload_sources(self) -> Dict:
        """
        Dynamically reloads proxy sources and fetcher jobs from the config file.
        """
        logger.info("Attempting to reload sources from config file...")
        with self.lock:
            self.config.read(self.config_path, encoding="utf-8")

            old_job_names = {job["name"] for job in self.fetcher_jobs}
            self.fetcher_jobs = self._load_fetcher_jobs()
            new_job_names = {job["name"] for job in self.fetcher_jobs}
            added_jobs = list(new_job_names - old_job_names)
            removed_jobs = list(old_job_names - new_job_names)
            if added_jobs or removed_jobs:
                logger.info(
                    f"Fetcher jobs reloaded. Added: {added_jobs}, Removed: {removed_jobs}"
                )
            else:
                logger.info("Fetcher jobs reloaded. No changes detected.")

            old_predefined_sources = self.predefined_sources.copy()
            sources_str = self.config.get(
                "sources", "predefined_sources", fallback="default"
            )
            self.predefined_sources = {
                s.strip() for s in sources_str.split(",") if s.strip()
            }
            self.default_source = self.config.get(
                "sources", "default_source", fallback="default"
            )
            if self.default_source not in self.predefined_sources:
                self.predefined_sources.add(self.default_source)

            added_sources = list(self.predefined_sources - old_predefined_sources)
            removed_sources = list(old_predefined_sources - self.predefined_sources)

            if added_sources:
                logger.info(f"Predefined sources changed. Added: {added_sources}")
                # Get default source data for copying to new sources
                default_stats = self.source_stats.get(self.default_source, {})
                default_proxies = self.available_proxies.get(self.default_source, {})

                for source in added_sources:
                    if source not in self.source_stats:
                        self.source_stats[source] = {}
                        self.available_proxies[source] = {
                            "top_tier": [],
                            "bottom_tier": [],
                        }

                    # Copy proxy stats from default_source with fresh scores
                    for proxy_url in default_stats:
                        if proxy_url not in self.source_stats[source]:
                            self.source_stats[source][proxy_url] = self._get_new_proxy_stat()

                    # Copy available proxy lists from default_source
                    self.available_proxies[source]["top_tier"] = list(
                        default_proxies.get("top_tier", [])
                    )
                    self.available_proxies[source]["bottom_tier"] = list(
                        default_proxies.get("bottom_tier", [])
                    )

                    copied_count = len(self.source_stats[source])
                    logger.info(
                        f"Initialized new source '{source}' with {copied_count} proxies copied from '{self.default_source}'"
                    )

            if removed_sources:
                logger.info(f"Predefined sources changed. Removed: {removed_sources}")
                for source in removed_sources:
                    self.source_stats.pop(source, None)
                    self.available_proxies.pop(source, None)
                    logger.info(
                        f"Cleaned up in-memory pool for removed source: {source}"
                    )

            if not added_sources and not removed_sources:
                logger.info("Predefined sources reloaded. No changes detected.")

        return {
            "added_fetcher_jobs": added_jobs,
            "removed_fetcher_jobs": removed_jobs,
            "added_predefined_sources": added_sources,
            "removed_predefined_sources": removed_sources,
        }

    def _fetch_and_parse_source(self, job: Dict) -> List:
        """
        DEADLOCK FIX: This method now returns a list of proxies instead of writing to the DB.
        Uses curl subprocess to bypass Python process direct rule in Clash Verge.
        """
        url = job["url"]
        logger.info(f"Fetching proxy source: {job['name']} from {url}")
        proxies_to_insert = []
        try:
            # Use curl instead of requests to bypass Python's direct rule in Clash Verge
            result = subprocess.run(
                ["curl", "-s", "-f", "--connect-timeout", "15", "--max-time", "30", url],
                capture_output=True,
                text=True,
                timeout=35
            )
            if result.returncode != 0:
                raise Exception(f"curl failed with return code {result.returncode}: {result.stderr}")
            response_text = result.stdout
            for line in response_text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    if "://" in line:
                        protocol, rest = line.split("://", 1)
                        ip, port_str = rest.rsplit(":", 1)
                        proxies_to_insert.append((protocol.lower(), ip, int(port_str)))
                    elif job["default_protocol"]:
                        ip, port_str = line.rsplit(":", 1)
                        proxies_to_insert.append(
                            (job["default_protocol"].lower(), ip, int(port_str))
                        )
                except ValueError:
                    continue
        except Exception as e:
            logger.error(f"Failed to fetch from {job['name']} ({url}): {e}")
        return proxies_to_insert

    def _handle_fetch_results(self, futures: List):
        """
        DEADLOCK FIX: New method to consolidate results from all fetchers and insert in a single batch.
        """
        all_new_proxies = []
        for future in as_completed(futures):
            try:
                proxies = future.result()
                if proxies:
                    all_new_proxies.extend(proxies)
            except Exception as e:
                logger.error(f"A fetcher job raised an exception: {e}")

        if not all_new_proxies:
            logger.info("No new proxies were fetched in this cycle.")
            return

        unique_proxies_set = {tuple(p) for p in all_new_proxies}
        unique_proxies_list = [list(p) for p in unique_proxies_set]

        logger.info(
            f"Consolidated {len(unique_proxies_list)} unique proxies from all sources for insertion."
        )
        self.db.insert_proxies(unique_proxies_list)

    async def _validate_proxy_async(self, session: aiohttp.ClientSession, proxy_id: int, proxy_url: str, semaphore: asyncio.Semaphore) -> Dict:
        """
        Async version of proxy validation using aiohttp for better performance.
        Uses semaphore to ensure timeout timer only starts when execution begins.
        """
        async with semaphore:
            start_time = time.time()
            try:
                async with session.get(
                    self.validation_target,
                    proxy=proxy_url,
                    timeout=aiohttp.ClientTimeout(total=self.validation_timeout_s),
                ) as response:
                    response.raise_for_status()
                    latency_ms = int((time.time() - start_time) * 1000)
                    data = await response.json()
                    headers = data.get("headers", {})
                    is_anonymous = not any(
                        h in headers for h in ["X-Forwarded-For", "Via", "X-Real-Ip"]
                    )
                    anonymity = "elite" if is_anonymous else "transparent"
                    return {
                        "id": proxy_id,
                        "success": True,
                        "latency": latency_ms,
                        "anonymity": anonymity,
                    }
            except aiohttp.ClientProxyConnectionError as e:
                # Proxy connection refused/unreachable
                if self.debug_mode:
                    logger.debug(f"Proxy {proxy_url} (ID: {proxy_id}) connection error: {type(e).__name__}")
                return {"id": proxy_id, "success": False}
            except asyncio.TimeoutError:
                # Request timed out
                if self.debug_mode:
                    logger.debug(f"Proxy {proxy_url} (ID: {proxy_id}) timeout after {self.validation_timeout_s}s")
                return {"id": proxy_id, "success": False}
            except Exception as e:
                # Other errors
                if self.debug_mode:
                    logger.debug(f"Proxy {proxy_url} (ID: {proxy_id}) failed: {type(e).__name__}")
                return {"id": proxy_id, "success": False}

    async def _validate_proxies_batch_async(self, proxies_to_validate: List[Dict]) -> Tuple[List[Dict], List[int]]:
        """
        Validate a batch of proxies concurrently using aiohttp.
        Returns (success_proxies, failure_proxy_ids).
        """
        # No session-level timeout - each request has its own timeout
        # limit controls max concurrent connections at session level,
        # but we also need a semaphore to control task execution start time.
        # We increase connector limit to avoid bottleneck there, relying on semaphore.
        connector = aiohttp.TCPConnector(
            limit=0, # Unlimited at connector level, controlled by semaphore
            force_close=True,
            enable_cleanup_closed=True,
        )
        
        semaphore = asyncio.Semaphore(self.validation_workers)

        async with aiohttp.ClientSession(connector=connector) as session:
            tasks = [
                self._validate_proxy_async(
                    session,
                    p["id"],
                    f"{p['protocol']}://{p['ip']}:{p['port']}",
                    semaphore
                )
                for p in proxies_to_validate
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
        
        success_proxies = []
        failure_proxy_ids = []
        
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                failure_proxy_ids.append(proxies_to_validate[i]["id"])
            elif result.get("success"):
                success_proxies.append(result)
            else:
                failure_proxy_ids.append(result["id"])
        
        return success_proxies, failure_proxy_ids

    def _validate_proxy(self, proxy_id: int, proxy_url: str) -> Dict:
        """
        Sync fallback for proxy validation (kept for compatibility).
        """
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
            return {
                "id": proxy_id,
                "success": True,
                "latency": latency_ms,
                "anonymity": anonymity,
            }
        except (requests.RequestException, json.JSONDecodeError):
            return {"id": proxy_id, "success": False}

    def _run_validation_cycle(self):
        with self.lock:
            if self.is_validating:
                logger.warning(
                    "Validation cycle is already in progress. Skipping this scheduled run."
                )
                return
            self.is_validating = True
        try:
            proxies_to_validate = self.db.get_proxies_to_validate()
            logger.info(
                f"There are {len(proxies_to_validate)} proxies need to be validated"
            )
            if len(proxies_to_validate) < self.validation_supplement_threshold:
                supplement_needed = self.validation_supplement_threshold - len(
                    proxies_to_validate
                )
                logger.info(
                    f"Validation pool below threshold. Supplementing with eligible failed proxies."
                )
                eligible_failed = self.db.get_eligible_failed_proxies(
                    window_minutes=self.validation_window_minutes,
                    max_attempts=self.max_validations_per_window,
                    limit=supplement_needed,
                )
                existing_ids = {p["id"] for p in proxies_to_validate}
                for p in eligible_failed:
                    if p["id"] not in existing_ids:
                        proxies_to_validate.append(p)

            if not proxies_to_validate:
                logger.info("Validation cycle skipped: no proxies to validate.")
                return

            proxy_ids_to_update = [p["id"] for p in proxies_to_validate]
            self.db.update_validation_counters(
                proxy_ids_to_update, self.validation_window_minutes
            )

            total_to_validate = len(proxies_to_validate)
            logger.info(f"Starting async validation for {total_to_validate} proxies...")

            # Use asyncio for high-performance validation
            validation_start_time = time.time()
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                success_proxies, failure_proxy_ids = loop.run_until_complete(
                    self._validate_proxies_batch_async(proxies_to_validate)
                )
            finally:
                loop.close()
            
            validation_duration = time.time() - validation_start_time
            proxies_per_second = total_to_validate / validation_duration if validation_duration > 0 else 0
            success_rate = len(success_proxies) / total_to_validate * 100 if total_to_validate > 0 else 0

            logger.info(
                f"Validation cycle finished in {validation_duration:.2f}s. "
                f"Success: {len(success_proxies)}/{total_to_validate} ({success_rate:.1f}%), "
                f"Throughput: {proxies_per_second:.1f} proxies/s"
            )

            self.db.batch_update_proxy_results(success_proxies, failure_proxy_ids)

            self._sync_and_select_top_proxies()
            # Sync premium proxies after validation
            self._sync_premium_proxies()
        finally:
            with self.lock:
                self.is_validating = False
            logger.info("Validation cycle lock released.")

    def _sync_and_select_top_proxies(self):
        """
        MODIFIED: Syncs proxies and splits them into performance tiers.
        """
        logger.info("Syncing and selecting proxies for all sources...")
        newly_active_proxies = self.db.get_active_proxies()

        with self.lock:
            self.active_proxies = newly_active_proxies
            for source in self.predefined_sources:
                stats_pool = self.source_stats.get(source, {})

                for proxy_url in self.active_proxies:
                    if proxy_url not in stats_pool:
                        stats_pool[proxy_url] = self._get_new_proxy_stat()

                sorted_proxies = sorted(
                    stats_pool.items(), key=lambda item: item[1]["score"], reverse=True
                )

                max_stats_size = self.max_pool_size * self.stats_pool_max_multiplier
                if len(sorted_proxies) > max_stats_size:
                    proxies_to_delete_count = len(sorted_proxies) - max_stats_size
                    logger.info(
                        f"Stats pool for source '{source}' exceeds limit ({len(sorted_proxies)} > {max_stats_size}). "
                        f"Removing {proxies_to_delete_count} lowest-scoring proxies."
                    )
                    sorted_proxies = sorted_proxies[:max_stats_size]
                    self.source_stats[source] = dict(sorted_proxies)
                else:
                    self.source_stats[source] = dict(sorted_proxies)

                # Select all proxies up to the max_pool_size for potential use
                usable_proxies = [
                    p_url for p_url, _ in sorted_proxies[: self.max_pool_size]
                ]

                # NEW: Split the usable proxies into tiers
                top_tier = usable_proxies[: self.top_tier_size]
                bottom_tier = usable_proxies[self.top_tier_size :]

                self.available_proxies[source]["top_tier"] = top_tier
                self.available_proxies[source]["bottom_tier"] = bottom_tier

                logger.info(
                    f"Source '{source}' synced. "
                    f"Total : {len(sorted_proxies)} proxies."
                    f"Top Tier: {len(top_tier)} proxies. "
                    f"Bottom Tier: {len(bottom_tier)} proxies."
                )

    def _update_dashboard_sources(self):
        logger.info("Refreshing dashboard sources from database...")
        db_sources = self.db.get_distinct_sources()
        with self.lock:
            self.dashboard_sources = set(db_sources)
        logger.info(
            f"Dashboard sources updated: {len(self.dashboard_sources)} sources found."
        )

    def _scheduler_loop(self):
        last_validation_run = 0
        last_flush_time = 0
        last_backup_time = 0
        while not self.stop_scheduler_event.is_set():
            now = time.time()
            try:
                with self.lock:
                    current_jobs = list(self.fetcher_jobs)

                fetch_futures = []
                for job in current_jobs:
                    if now - job.get("last_run", 0) >= job["interval_minutes"] * 60:
                        job["last_run"] = now
                        fetch_futures.append(
                            self.fetch_executor.submit(
                                self._fetch_and_parse_source, job
                            )
                        )

                if fetch_futures:
                    logger.info(f"Submitted {len(fetch_futures)} fetcher jobs.")
                    threading.Thread(
                        target=self._handle_fetch_results,
                        args=(fetch_futures,),
                        daemon=True,
                    ).start()

                if now - last_validation_run >= self.validation_interval_s:
                    last_validation_run = now
                    threading.Thread(
                        target=self._run_validation_cycle, daemon=True
                    ).start()
                if now - last_flush_time >= self.stats_flush_interval_s:
                    last_flush_time = now
                    threading.Thread(
                        target=self._flush_feedback_buffer, daemon=True
                    ).start()
                if (
                    now - self.last_source_refresh_time
                    >= self.source_refresh_interval_s
                ):
                    self.last_source_refresh_time = now
                    threading.Thread(
                        target=self._update_dashboard_sources, daemon=True
                    ).start()

                # Stats backup task
                if self.stats_backup_enabled and now - last_backup_time >= self.stats_backup_interval_s:
                    last_backup_time = now
                    threading.Thread(target=self.backup_stats, daemon=True).start()

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
        logger.info("Stopping scheduler and flushing final stats...")
        self._flush_feedback_buffer()
        if self.stats_backup_enabled:
            self.backup_stats()  # Backup before shutdown
        if self.scheduler_thread and self.scheduler_thread.is_alive():
            self.stop_scheduler_event.set()
            self.fetch_executor.shutdown(wait=True)
            self.scheduler_thread.join(timeout=10)
            logger.info("Background scheduler stopped.")

    def _get_new_proxy_stat(self) -> Dict:
        """
        Create a new proxy stat entry with ELO-inspired scoring fields.
        
        Score range: 0-100 (starts at 50 as neutral)
        - recent_results: sliding window of (timestamp, success, latency_ms)
        - avg_latency_ms: exponential moving average of latency
        """
        return {
            "score": 50.0,              # ELO-like score, bounded 0-100
            "success_count": 0,         # Total historical success
            "failure_count": 0,         # Total historical failure
            "consecutive_failures": 0,  # For penalty escalation
            # NEW: Sliding window for recent performance (last 100 results max)
            "recent_results": [],       # List of [timestamp, success: bool, latency_ms: int|None]
            "avg_latency_ms": None,     # Exponential moving average of latency
        }

    def backup_stats(self) -> Dict:
        """Backup source_stats to a JSON file."""
        with self.lock:
            stats_snapshot = {
                "timestamp": datetime.now().isoformat(),
                "source_stats": {source: dict(proxies) for source, proxies in self.source_stats.items()},
            }

        try:
            # Create directory if it doesn't exist
            self.stats_backup_path.parent.mkdir(parents=True, exist_ok=True)

            with open(self.stats_backup_path, "w", encoding="utf-8") as f:
                json.dump(stats_snapshot, f, ensure_ascii=False, indent=2)

            total_proxies = sum(len(proxies) for proxies in stats_snapshot["source_stats"].values())
            logger.info(
                f"Stats backup completed: {len(stats_snapshot['source_stats'])} sources, "
                f"{total_proxies} proxy stats saved to {self.stats_backup_path}"
            )
            return {
                "status": "success",
                "path": str(self.stats_backup_path),
                "sources": len(stats_snapshot["source_stats"]),
                "total_proxies": total_proxies,
            }
        except Exception as e:
            logger.error(f"Failed to backup stats: {e}")
            return {"status": "error", "message": str(e)}

    def restore_stats(self) -> Dict:
        """Restore source_stats from JSON file on startup."""
        if not self.stats_backup_path.exists():
            logger.info(f"No backup file found at {self.stats_backup_path}, starting with fresh stats.")
            return {"status": "skipped", "message": "No backup file found"}

        try:
            # Log file info before loading
            file_size = self.stats_backup_path.stat().st_size
            logger.info(f"Loading stats backup from: {self.stats_backup_path} (size: {file_size / 1024:.2f} KB)")

            with open(self.stats_backup_path, "r", encoding="utf-8") as f:
                snapshot = json.load(f)

            # Log backup metadata
            backup_time = snapshot.get("timestamp", "unknown")
            total_sources_in_file = len(snapshot.get("source_stats", {}))
            logger.info(f"Backup file parsed successfully. Timestamp: {backup_time}, Sources in file: {total_sources_in_file}")

            with self.lock:
                restored_sources = 0
                restored_proxies = 0
                skipped_sources = []

                for source, proxies in snapshot.get("source_stats", {}).items():
                    if source in self.predefined_sources:
                        # Migrate legacy stats to new ELO format
                        migrated_proxies = {}
                        legacy_count = 0
                        for proxy_url, stat in proxies.items():
                            if "recent_results" not in stat:
                                legacy_count += 1
                            migrated_proxies[proxy_url] = self._migrate_legacy_stat(stat)
                        
                        self.source_stats[source] = migrated_proxies
                        restored_sources += 1
                        proxy_count = len(migrated_proxies)
                        restored_proxies += proxy_count
                        
                        if legacy_count > 0:
                            logger.info(f"  [RESTORED] Source '{source}': {proxy_count} proxies ({legacy_count} migrated to ELO format)")
                        else:
                            logger.info(f"  [RESTORED] Source '{source}': {proxy_count} proxies loaded")
                    else:
                        skipped_sources.append(source)
                        logger.debug(f"  [SKIPPED] Source '{source}': not in predefined_sources")

                # Log skipped sources summary if any
                if skipped_sources:
                    logger.warning(f"Skipped {len(skipped_sources)} sources not in predefined_sources: {skipped_sources}")

            logger.info(
                f"Stats restore completed: {restored_sources}/{total_sources_in_file} sources, "
                f"{restored_proxies} proxy stats loaded"
            )
            return {
                "status": "success",
                "timestamp": backup_time,
                "restored_sources": restored_sources,
                "restored_proxies": restored_proxies,
            }
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse backup file (invalid JSON): {e}")
            return {"status": "error", "message": f"Invalid JSON: {e}"}
        except Exception as e:
            logger.error(f"Failed to restore stats: {e}")
            return {"status": "error", "message": str(e)}

    def _get_source_lock(self, source: str) -> threading.Lock:
        """Get or create a lock for a specific source for fine-grained locking."""
        with self.lock:
            if source not in self.source_locks:
                self.source_locks[source] = threading.Lock()
            return self.source_locks[source]

    def _get_source_or_default(self, source: str) -> str:
        with self.lock:
            is_defined = source in self.predefined_sources
        return source if is_defined else self.default_source

    def get_proxy(self, source: str) -> Optional[str]:
        """
        MODIFIED: Implements weighted random selection from performance tiers.
        """
        source = self._get_source_or_default(source)

        with self.lock:
            # If weighted selection is disabled, fall back to old behavior
            if not self.weighted_selection_enabled:
                proxy_pools = self.available_proxies.get(source, {})
                flat_pool = proxy_pools.get("top_tier", []) + proxy_pools.get(
                    "bottom_tier", []
                )
                if not flat_pool:
                    logger.warning(
                        f"No available proxy for source '{source}' (weighted selection disabled)."
                    )
                    return None
                return random.choice(flat_pool)

            # Weighted selection logic
            proxy_pools = self.available_proxies.get(source)
            if not proxy_pools:
                logger.warning(f"No proxy pools defined for source '{source}'.")
                return None

            top_tier = proxy_pools.get("top_tier", [])
            bottom_tier = proxy_pools.get("bottom_tier", [])

            # Determine which tier to pull from
            use_top_tier = random.randint(1, 100) <= self.top_tier_load_percentage

            if use_top_tier and top_tier:
                return random.choice(top_tier)
            elif (
                bottom_tier
            ):  # Fallback to bottom tier if top is chosen but empty, or if bottom is chosen
                return random.choice(bottom_tier)
            elif top_tier:  # Fallback to top tier if bottom is chosen but empty
                return random.choice(top_tier)
            else:
                logger.warning(f"No available proxy in any tier for source '{source}'.")
                return None

    def get_premium_proxy(self) -> Optional[str]:
        """
        Get a premium (highest quality) proxy for Playwright and other high-reliability use cases.
        Returns one of the lowest-latency proxies from the database.
        """
        with self.lock:
            if not self.premium_proxies:
                logger.warning("No premium proxies available.")
                return None

            selected = random.choice(self.premium_proxies)
            logger.debug(
                f"Premium proxy selected: {selected} "
                f"(pool size: {len(self.premium_proxies)})"
            )
            return selected

    def _sync_premium_proxies(self):
        """
        Sync premium proxies from source_stats.
        Aggregates proxies across all sources and selects the top N by score.
        
        Strategy:
        1. Prefer proxies with sufficient usage history (>= min_usage_count) to avoid
           new proxies with inflated initial scores (0 score can rank higher than negative).
        2. Fallback: If no proxies meet the usage threshold, select from all active proxies
           by score to ensure we never return an empty premium pool.
        """
        premium_pool_size = self.config.getint(
            "source_pool", "premium_pool_size", fallback=20
        )
        min_usage_count = self.config.getint(
            "source_pool", "premium_min_usage_count", fallback=50
        )

        with self.lock:
            # Aggregate all proxies with their highest score across all sources
            # Separate into two pools: battle-tested (sufficient usage) and all proxies
            battle_tested_scores: Dict[str, float] = {}
            all_proxy_scores: Dict[str, float] = {}
            
            for source, stats in self.source_stats.items():
                for proxy_url, stat in stats.items():
                    usage_count = stat.get("success_count", 0) + stat.get("failure_count", 0)
                    score = stat.get("score", 0)
                    
                    # Track all proxy scores
                    if proxy_url not in all_proxy_scores or score > all_proxy_scores[proxy_url]:
                        all_proxy_scores[proxy_url] = score
                    
                    # Also track battle-tested proxies separately
                    if usage_count >= min_usage_count:
                        if proxy_url not in battle_tested_scores or score > battle_tested_scores[proxy_url]:
                            battle_tested_scores[proxy_url] = score

            # Filter to only active proxies
            active_battle_tested = {
                url: score for url, score in battle_tested_scores.items()
                if url in self.active_proxies
            }
            active_all_proxies = {
                url: score for url, score in all_proxy_scores.items()
                if url in self.active_proxies
            }

            # Strategy: prefer battle-tested, fallback to all active proxies
            if active_battle_tested:
                # Have enough battle-tested proxies, use them
                sorted_proxies = sorted(
                    active_battle_tested.items(),
                    key=lambda x: x[1],
                    reverse=True
                )
                self.premium_proxies = [url for url, _ in sorted_proxies[:premium_pool_size]]
                source_type = "battle-tested"
                score_dict = battle_tested_scores
            elif active_all_proxies:
                # Fallback: no battle-tested proxies, use all active proxies
                sorted_proxies = sorted(
                    active_all_proxies.items(),
                    key=lambda x: x[1],
                    reverse=True
                )
                self.premium_proxies = [url for url, _ in sorted_proxies[:premium_pool_size]]
                source_type = "fallback (all active)"
                score_dict = all_proxy_scores
            else:
                # No proxies at all
                self.premium_proxies = []
                source_type = None
                score_dict = {}

        if self.premium_proxies:
            top_scores = [score_dict.get(url, 0) for url in self.premium_proxies[:5]]
            logger.info(
                f"Premium proxy pool synced ({source_type}): {len(self.premium_proxies)} proxies loaded "
                f"(top 5 scores: {top_scores})"
            )
        else:
            logger.warning("No premium proxies found: no active proxies available.")

    def _flush_feedback_buffer(self):
        """Flushes stats for all fully completed minutes to the database."""
        current_minute_start = datetime.now().replace(second=0, microsecond=0)

        records_to_flush = []
        minutes_to_clear = []

        with self.lock:
            buffer_keys = list(self.feedback_buffer.keys())
            for minute_timestamp in buffer_keys:
                if minute_timestamp < current_minute_start:
                    logger.debug(
                        f"Preparing to flush stats for completed minute: {minute_timestamp.strftime('%Y-%m-%d %H:%M')}"
                    )
                    for source, counts in self.feedback_buffer[
                        minute_timestamp
                    ].items():
                        records_to_flush.append(
                            (
                                minute_timestamp,
                                source,
                                counts.get("success", 0),
                                counts.get("failure", 0),
                            )
                        )
                    minutes_to_clear.append(minute_timestamp)

            for minute in minutes_to_clear:
                del self.feedback_buffer[minute]

        if records_to_flush:
            self.db.flush_feedback_stats(records_to_flush)
        else:
            logger.debug(
                "Flush stats task ran, but no completed minutes were found in the buffer."
            )

    def _migrate_legacy_stat(self, stat: Dict) -> Dict:
        """
        Migrate legacy stat format to new ELO-based format.
        Called when restoring stats from backup or encountering old format.
        """
        if "recent_results" not in stat:
            # Legacy format detected, migrate
            stat["recent_results"] = []
            stat["avg_latency_ms"] = None
            
            # Estimate initial score from historical data
            total = stat.get("success_count", 0) + stat.get("failure_count", 0)
            if total > 0:
                success_rate = stat.get("success_count", 0) / total
                # Map success rate to 0-100 score
                stat["score"] = min(100, max(0, success_rate * 100))
            else:
                stat["score"] = 50.0  # Neutral for no history
        return stat

    def _calculate_elo_score(self, stat: Dict, source: str = None) -> float:
        """
        Calculate ELO-inspired score based on:
        1. Recent success rate (sliding window)
        2. Average latency (lower is better)
        3. Consistency bonus (stable recent performance)
        
        Score range: 0-100
        
        Components:
        - Success rate:   0-60 points (primary factor)
        - Latency:        0-30 points (secondary factor)
        - Consistency:    0-10 points (bonus for stability)
        """
        # Use configurable window size
        window_size = self.elo_scoring_window
        
        recent = stat.get("recent_results", [])[-window_size:]
        
        if not recent:
            # No recent data, use historical if available
            total = stat.get("success_count", 0) + stat.get("failure_count", 0)
            if total > 0:
                success_rate = stat.get("success_count", 0) / total
                return min(100, max(0, success_rate * 80 + 10))  # Range 10-90 for historical
            return 50.0  # Neutral for completely new proxies
        
        # 1. Success rate component (0-60 points)
        successes = sum(1 for r in recent if r[1])
        success_rate = successes / len(recent)
        success_score = success_rate * 60
        
        # 2. Latency component (0-30 points)
        # Lower latency = higher score
        latencies = [r[2] for r in recent if r[1] and r[2] is not None]
        if latencies:
            avg_latency = sum(latencies) / len(latencies)
            # 0-300ms = 30pts, 300-2000ms linear decay to 0, >2000ms = 0
            if avg_latency <= 300:
                latency_score = 30
            elif avg_latency <= 2000:
                latency_score = max(0, 30 - (avg_latency - 300) / 56.67)
            else:
                latency_score = 0
        else:
            latency_score = 15  # Neutral if no latency data
        
        # 3. Consistency bonus (0-10 points)
        # Reward proxies with stable recent performance
        if len(recent) >= 10:
            recent_10 = recent[-10:]
            recent_successes = sum(1 for r in recent_10 if r[1])
            if recent_successes >= 9:
                consistency_score = 10  # Excellent consistency
            elif recent_successes >= 7:
                consistency_score = 7   # Good consistency
            else:
                consistency_score = recent_successes * 0.7  # Proportional
        else:
            consistency_score = 5  # Neutral for new proxies
        
        return min(100, max(0, success_score + latency_score + consistency_score))

    def process_feedback(
        self,
        source: str,
        proxy_url: str,
        status_code: int,
        response_time_ms: Optional[int] = None,
    ):
        """
        Process feedback for a proxy request with ELO-based scoring.
        
        Updates:
        - Adds result to sliding window (recent_results)
        - Updates exponential moving average of latency
        - Recalculates ELO score
        - Maintains historical counters for analytics
        """
        source = self._get_source_or_default(source)
        is_success = status_code not in FAILED_STATUS_CODES

        current_minute = datetime.now().replace(second=0, microsecond=0)
        current_timestamp = time.time()

        with self.lock:
            # Update feedback buffer for database flush
            if is_success:
                self.feedback_buffer[current_minute][source]["success"] += 1
            else:
                self.feedback_buffer[current_minute][source]["failure"] += 1

            stat = self.source_stats.get(source, {}).get(proxy_url)
            if not stat:
                return
            
            # Migrate legacy stats if needed
            stat = self._migrate_legacy_stat(stat)
            
            # Update historical counters
            if is_success:
                stat["success_count"] += 1
                stat["consecutive_failures"] = 0
                
                # Update exponential moving average of latency
                if response_time_ms is not None:
                    alpha = 0.3  # Smoothing factor
                    if stat["avg_latency_ms"] is None:
                        stat["avg_latency_ms"] = response_time_ms
                    else:
                        stat["avg_latency_ms"] = (
                            alpha * response_time_ms + 
                            (1 - alpha) * stat["avg_latency_ms"]
                        )
            else:
                stat["failure_count"] += 1
                stat["consecutive_failures"] += 1
            
            # Add to sliding window (keep last elo_max_window results)
            stat["recent_results"].append([current_timestamp, is_success, response_time_ms])
            if len(stat["recent_results"]) > self.elo_max_window:
                stat["recent_results"] = stat["recent_results"][-self.elo_max_window:]
            
            # Recalculate ELO score
            old_score = stat["score"]
            stat["score"] = self._calculate_elo_score(stat, source)
            
            response_time_str = f"{response_time_ms:.0f}" if response_time_ms is not None else "N/A"
            logger.debug(
                f"ELO Score: {source:<15} | {proxy_url:<30} | "
                f"{'OK' if is_success else 'FAIL':<4} | {response_time_str:<6}ms | "
                f"{old_score:.1f} -> {stat['score']:.1f}"
            )


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


# --- Flask API Server ---
app = Flask(__name__, static_folder="dashboard/dist")
CORS(app)
proxy_manager = load_proxy_manager(CONFIG_FILE_PATH)


@app.route("/health", methods=["GET"])
def health_check():
    """Health check endpoint for monitoring."""
    with proxy_manager.lock:
        active_count = len(proxy_manager.active_proxies)
        premium_count = len(proxy_manager.premium_proxies)
        sources_count = len(proxy_manager.predefined_sources)
    
    return jsonify({
        "status": "healthy",
        "active_proxies": active_count,
        "premium_proxies": premium_count,
        "sources": sources_count,
        "is_validating": proxy_manager.is_validating,
    })


@app.route("/metrics", methods=["GET"])
def metrics():
    """Prometheus-compatible metrics endpoint."""
    with proxy_manager.lock:
        active_count = len(proxy_manager.active_proxies)
        premium_count = len(proxy_manager.premium_proxies)
        sources_count = len(proxy_manager.predefined_sources)
        
        # Calculate total stats across all sources
        total_success = 0
        total_failure = 0
        for source_stats in proxy_manager.source_stats.values():
            for stat in source_stats.values():
                total_success += stat.get("success_count", 0)
                total_failure += stat.get("failure_count", 0)
    
    total_requests = total_success + total_failure
    success_rate = (total_success / total_requests * 100) if total_requests > 0 else 0
    
    # Prometheus text format
    metrics_text = f"""# HELP smartproxy_active_proxies Number of active proxies
# TYPE smartproxy_active_proxies gauge
smartproxy_active_proxies {active_count}

# HELP smartproxy_premium_proxies Number of premium proxies
# TYPE smartproxy_premium_proxies gauge
smartproxy_premium_proxies {premium_count}

# HELP smartproxy_sources_total Number of configured sources
# TYPE smartproxy_sources_total gauge
smartproxy_sources_total {sources_count}

# HELP smartproxy_requests_total Total requests processed
# TYPE smartproxy_requests_total counter
smartproxy_requests_success_total {total_success}
smartproxy_requests_failure_total {total_failure}

# HELP smartproxy_success_rate_percent Current success rate percentage
# TYPE smartproxy_success_rate_percent gauge
smartproxy_success_rate_percent {success_rate:.2f}

# HELP smartproxy_is_validating Whether validation is in progress
# TYPE smartproxy_is_validating gauge
smartproxy_is_validating {1 if proxy_manager.is_validating else 0}
"""
    return metrics_text, 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/get-proxy", methods=["GET"])
def get_proxy_route():
    source = request.args.get("source")
    if not source:
        return jsonify({"error": "Query parameter 'source' is required."}), 400
    proxy_url = proxy_manager.get_proxy(source)
    if proxy_url:
        return jsonify({"http": proxy_url, "https": proxy_url})
    else:
        return (
            jsonify(
                {"error": f"No available proxy for source '{source}' at the moment."}
            ),
            404,
        )


@app.route("/get-premium-proxy", methods=["GET"])
def get_premium_proxy_route():
    """Get a premium (highest quality) proxy for Playwright and high-reliability use cases."""
    proxy_url = proxy_manager.get_premium_proxy()
    if proxy_url:
        return jsonify({"http": proxy_url, "https": proxy_url, "premium": True})
    else:
        return (
            jsonify(
                {"error": "No premium proxy available at the moment."}
            ),
            404,
        )


@app.route("/feedback", methods=["POST"])
def feedback_route():
    data = request.json
    source = data.get("source")
    proxy_url = data.get("proxy")
    status_code = data.get("status")
    resp_time = data.get("response_time_ms")
    logger.debug(
        f"Handled feedback: {source} - {status_code} - {proxy_url} - {resp_time}"
    )
    if not all([source, proxy_url]) or not isinstance(status_code, int):
        return (
            jsonify(
                {
                    "error": "Invalid feedback data. 'source', 'proxy', and 'status_code' (int) are required."
                }
            ),
            400,
        )
    proxy_manager.process_feedback(source, proxy_url, status_code, resp_time)
    return jsonify({"message": "Feedback received."})


@app.route("/reload-sources", methods=["POST"])
def reload_sources_route():
    """
    API endpoint to dynamically reload proxy sources from the config file.
    """
    try:
        result = proxy_manager.reload_sources()
        return (
            jsonify(
                {
                    "status": "success",
                    "message": "Configuration and sources reloaded.",
                    "details": result,
                }
            ),
            200,
        )
    except Exception as e:
        logger.error(f"Error during source reload via API: {e}", exc_info=True)
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "An internal error occurred during reload.",
                }
            ),
            500,
        )


@app.route("/backup-stats", methods=["POST"])
def backup_stats_route():
    """API endpoint to manually trigger stats backup."""
    try:
        result = proxy_manager.backup_stats()
        status_code = 200 if result["status"] == "success" else 500
        return jsonify(result), status_code
    except Exception as e:
        logger.error(f"Error during stats backup via API: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/sources", methods=["GET"])
def get_sources():
    return jsonify(sorted(list(proxy_manager.dashboard_sources)))


@app.route("/api/stats/daily", methods=["GET"])
def get_daily_stats_route():
    source = request.args.get("source")
    date = request.args.get("date")
    if not all([source, date]):
        return (
            jsonify({"error": "'source' and 'date' query parameters are required."}),
            400,
        )
    stats = proxy_manager.db.get_daily_stats(source, date)
    if stats:
        total = stats["total_success"] + stats["total_failure"]
        success_rate = (stats["total_success"] / total * 100) if total > 0 else 0
        return jsonify(
            {
                "total_requests": total,
                "total_success": stats["total_success"],
                "success_rate": round(success_rate, 2),
            }
        )
    return jsonify({"total_requests": 0, "total_success": 0, "success_rate": 0})


@app.route("/api/stats/timeseries", methods=["GET"])
def get_timeseries_stats_route():
    source = request.args.get("source")
    date_str = request.args.get("date")
    interval = request.args.get("interval", "10", type=int)
    if not all([source, date_str]):
        return (
            jsonify({"error": "'source' and 'date' query parameters are required."}),
            400,
        )
    valid_intervals = [2, 5, 10, 30, 60]
    if interval not in valid_intervals:
        return jsonify({"error": f"'interval' must be one of {valid_intervals}."}), 400
    
    # Get raw stats from DB (sparse data)
    stats = proxy_manager.db.get_timeseries_stats(source, date_str, interval)
    
    # Convert stats to dictionary for O(1) lookup
    # Key: HH:MM string, Value: row data
    stats_map = {}
    if stats:
        for row in stats:
            time_key = row["interval_start"].strftime("%H:%M")
            stats_map[time_key] = row

    # Generate full list of time slots for the day
    results = []
    try:
        start_date = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "Invalid date format. Use YYYY-MM-DD."}), 400

    current_time = start_date
    end_time = start_date + timedelta(days=1)
    
    while current_time < end_time:
        time_str = current_time.strftime("%H:%M")
        
        if time_str in stats_map:
            row = stats_map[time_str]
            total = row["success"] + row["failure"]
            success_rate = (row["success"] / total * 100) if total > 0 else 0
            results.append({
                "time": time_str,
                "success_rate": round(success_rate, 2),
                "total_requests": total,
                "success_count": row["success"],
            })
        else:
            # Fill missing data with 0
            results.append({
                "time": time_str,
                "success_rate": 0,
                "total_requests": 0,
                "success_count": 0,
            })
        
        current_time += timedelta(minutes=interval)

    return jsonify(results)


@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_frontend(path):
    if path != "" and os.path.exists(os.path.join(app.static_folder, path)):
        return send_from_directory(app.static_folder, path)
    else:
        return send_from_directory(app.static_folder, "index.html")


def handle_shutdown(signal, frame):
    logger.info("Shutdown signal received. Performing graceful shutdown...")
    proxy_manager.stop_scheduler()
    sys.exit(0)


if __name__ == "__main__":
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="SmartProxy Service")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging for validation")
    args = parser.parse_args()
    
    # Set debug mode on proxy manager
    proxy_manager.debug_mode = args.debug
    if args.debug:
        setup_logging("DEBUG")
        logger.info("Debug mode enabled - verbose validation logging active")
    
    # Suppress Werkzeug's default access logs for per-request noise reduction
    import logging
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)
    proxy_manager.start_scheduler()
    app.run(host="0.0.0.0", port=proxy_manager.server_port, debug=False)
