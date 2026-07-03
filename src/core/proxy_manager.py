# -*- coding: utf-8 -*-
import os
import configparser
import json
import math
import random
import threading
import time
import asyncio
import subprocess
import aiohttp
from typing import Dict, List, Optional, Set, Tuple
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from src.utils.logger import logger
from src.database.db import DatabaseManager

try:
    from aiohttp_socks import ProxyConnector
except ImportError:  # pragma: no cover - dependency is declared in requirements.
    ProxyConnector = None

# --- Constants ---
FAILED_STATUS_CODES = {
    0,
    4,
}  # Set of status codes that indicate failure (0=timeout, 4=proxy error)
LEGACY_SUCCESS_STATUS_CODES = {1, 2, 3}
VALID_FAILURE_KINDS = {
    "timeout",
    "proxy_error",
    "dead",
    "blocked",
    "slow",
    "content_error",
}

class ProxyManager:
    """Manages the proxy lifecycle, state, and business logic."""

    def __init__(self, config_path):
        self.config_path = config_path
        self.config = configparser.ConfigParser()
        self.config.read(config_path, encoding="utf-8")

        self.db = DatabaseManager(self.config)
        # Use RLock for reentrant locking (allows same thread to acquire lock multiple times)
        self.lock = threading.RLock()

        self.active_proxies: Set[str] = set()
        self.source_stats: Dict[str, Dict[str, Dict]] = {}
        self.available_proxies: Dict[str, Dict[str, List[str]]] = (
            {}
        )  # MODIFIED: Structure for tiers
        self.premium_proxies: List[str] = []  # High-quality proxies for Playwright
        self.proxy_last_handed_out_ts: Dict[str, Dict[str, float]] = defaultdict(dict)

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
        allowed_ips_str = self.config.get("server", "allowed_ips", fallback="")
        if not allowed_ips_str:
            legacy_allowed_ips = self.config.get(
                "server", "allowed_dashboard_ips", fallback=""
            )
            if legacy_allowed_ips:
                logger.warning(
                    "Config key 'allowed_dashboard_ips' is deprecated. Use 'allowed_ips' instead."
                )
                allowed_ips_str = legacy_allowed_ips
        self.allowed_ips = [ip.strip() for ip in allowed_ips_str.split(",") if ip.strip()]
        self.trust_proxy_headers = self.config.getboolean(
            "server", "trust_proxy_headers", fallback=False
        )
        trusted_proxy_ips_str = self.config.get("server", "trusted_proxy_ips", fallback="")
        self.trusted_proxy_ips = [
            ip.strip() for ip in trusted_proxy_ips_str.split(",") if ip.strip()
        ]

        self.validation_workers = self.config.getint(
            "validator", "validation_workers", fallback=100
        )
        self.validation_timeout_s = self.config.getint(
            "validator", "validation_timeout_s", fallback=10
        )
        self.validation_target = self.config.get(
            "validator", "validation_target", fallback="http://httpbin.org/get"
        )
        targets_str = self.config.get("validator", "validation_targets", fallback="")
        self.validation_targets = [
            target.strip() for target in targets_str.split(",") if target.strip()
        ] or [self.validation_target]
        self.validation_success_threshold = max(
            1,
            self.config.getint(
                "validator",
                "validation_success_threshold",
                fallback=len(self.validation_targets),
            ),
        )
        self.validation_success_threshold = min(
            self.validation_success_threshold, len(self.validation_targets)
        )
        self.validation_batch_limit = self.config.getint(
            "validator", "validation_batch_limit", fallback=2000
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

        # Proxy selection configuration.
        self.weighted_selection_enabled = self.config.getboolean(
            "source_pool", "weighted_selection_enabled", fallback=False
        )
        self.selection_strategy = self.config.get(
            "source_pool", "selection_strategy", fallback="uniform"
        ).strip().lower()
        if self.weighted_selection_enabled and self.selection_strategy == "uniform":
            self.selection_strategy = "tiered"
        if self.selection_strategy not in {"uniform", "tiered", "weighted", "softmax"}:
            logger.warning(
                "Unknown selection_strategy '{}'; falling back to uniform.",
                self.selection_strategy,
            )
            self.selection_strategy = "uniform"
        self.top_tier_size = self.config.getint(
            "source_pool", "top_tier_size", fallback=100
        )
        self.top_tier_load_percentage = self.config.getint(
            "source_pool", "top_tier_load_percentage", fallback=70
        )
        self.proxy_cooldown_ms = max(
            0, self.config.getint("source_pool", "proxy_cooldown_ms", fallback=0)
        )
        self.selection_weight_floor = max(
            0.01,
            self.config.getfloat("source_pool", "selection_weight_floor", fallback=1.0),
        )
        self.softmax_temperature = max(
            0.1,
            self.config.getfloat("source_pool", "softmax_temperature", fallback=20.0),
        )
        self.avg_latency_alpha = min(
            1.0,
            max(0.01, self.config.getfloat("source_pool", "avg_latency_alpha", fallback=0.3)),
        )
        self.latency_full_score_ms = max(
            1, self.config.getint("source_pool", "latency_full_score_ms", fallback=300)
        )
        self.latency_zero_score_ms = max(
            self.latency_full_score_ms + 1,
            self.config.getint("source_pool", "latency_zero_score_ms", fallback=2000),
        )

        # Fetcher configuration.
        self.fetcher_use_curl = self.config.getboolean(
            "fetcher", "use_curl", fallback=False
        )
        self.fetch_connect_timeout_s = self.config.getint(
            "fetcher", "connect_timeout_s", fallback=30
        )
        self.fetch_total_timeout_s = self.config.getint(
            "fetcher", "total_timeout_s", fallback=60
        )

        # Backup configuration
        self.stats_backup_enabled = self.config.getboolean(
            "backup", "stats_backup_enabled", fallback=True
        )
        self.stats_backup_interval_s = self.config.getint(
            "backup", "stats_backup_interval_seconds", fallback=3600  # 1 hour
        )
        self.stats_backup_path = Path(
            self.config.get("backup", "stats_backup_path", fallback="./.local/data/proxy_stats_backup.json")
        )

        # ELO scoring configuration
        self.elo_max_window = self.config.getint(
            "source_pool", "elo_max_window", fallback=100
        )
        self.elo_scoring_window = self.config.getint(
            "source_pool", "elo_scoring_window", fallback=50
        )
        self.elo_time_decay_enabled = self.config.getboolean(
            "source_pool", "elo_time_decay_enabled", fallback=True
        )
        self.elo_decay_half_life_hours = max(
            0.1,
            self.config.getfloat(
                "source_pool", "elo_decay_half_life_hours", fallback=24.0
            ),
        )
        self.elo_max_result_age_hours = max(
            0.1,
            self.config.getfloat(
                "source_pool", "elo_max_result_age_hours", fallback=168.0
            ),
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
        logger.info(f"Loaded {len(jobs)} proxy source fetcher jobs from config.")
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
        Uses aiohttp by default; curl remains available for local environments that
        route Python and shell traffic differently.
        """
        url = job["url"]
        logger.info(f"Fetching proxy source: {job['name']} from {url}")
        proxies_to_insert = []
        try:
            response_text = self._fetch_source_text(url)
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
            failures = job.get("failure_count", 0) + 1
            job["failure_count"] = failures
            backoff_seconds = min(3600, 60 * (2 ** min(failures - 1, 5)))
            job["last_run"] = time.time() + backoff_seconds - job["interval_minutes"] * 60
            logger.warning(
                "Fetcher job '{}' backed off for {}s after {} consecutive failure(s).",
                job["name"],
                backoff_seconds,
                failures,
            )
        else:
            job["failure_count"] = 0
        return proxies_to_insert

    def _fetch_source_text(self, url: str) -> str:
        if self.fetcher_use_curl:
            result = subprocess.run(
                [
                    "curl",
                    "-s",
                    "-f",
                    "--connect-timeout",
                    str(self.fetch_connect_timeout_s),
                    "--max-time",
                    str(self.fetch_total_timeout_s),
                    url,
                ],
                capture_output=True,
                text=True,
                timeout=self.fetch_total_timeout_s + 5,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"curl failed with return code {result.returncode}: {result.stderr}"
                )
            return result.stdout

        return asyncio.run(self._fetch_source_text_async(url))

    async def _fetch_source_text_async(self, url: str) -> str:
        timeout = aiohttp.ClientTimeout(
            total=self.fetch_total_timeout_s,
            connect=self.fetch_connect_timeout_s,
        )
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as response:
                response.raise_for_status()
                return await response.text()

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

    async def _validate_proxy_async(
        self,
        session: aiohttp.ClientSession,
        proxy_id: int,
        proxy_url: str,
        semaphore: asyncio.Semaphore,
    ) -> Dict:
        """
        Async version of proxy validation using aiohttp for better performance.
        Uses semaphore to ensure timeout timer only starts when execution begins.
        """
        protocol = proxy_url.split("://", 1)[0].lower() if "://" in proxy_url else "http"
        if protocol.startswith("socks"):
            return await self._validate_socks_proxy_async(proxy_id, proxy_url, semaphore)

        async with semaphore:
            try:
                return await self._validate_http_proxy_with_session(
                    session, proxy_id, proxy_url
                )
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

    async def _validate_http_proxy_with_session(
        self, session: aiohttp.ClientSession, proxy_id: int, proxy_url: str
    ) -> Dict:
        return await self._validate_against_targets(
            proxy_id,
            proxy_url,
            lambda target: session.get(
                target,
                proxy=proxy_url,
                timeout=aiohttp.ClientTimeout(total=self.validation_timeout_s),
            ),
        )

    async def _validate_socks_proxy_async(
        self, proxy_id: int, proxy_url: str, semaphore: asyncio.Semaphore
    ) -> Dict:
        async with semaphore:
            if ProxyConnector is None:
                logger.error(
                    "Cannot validate SOCKS proxy {} because aiohttp-socks is not installed.",
                    proxy_url,
                )
                return {"id": proxy_id, "success": False}

            try:
                connector = ProxyConnector.from_url(proxy_url)
                async with aiohttp.ClientSession(connector=connector) as socks_session:
                    return await self._validate_against_targets(
                        proxy_id,
                        proxy_url,
                        lambda target: socks_session.get(
                            target,
                            timeout=aiohttp.ClientTimeout(
                                total=self.validation_timeout_s
                            ),
                        ),
                    )
            except aiohttp.ClientProxyConnectionError as e:
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

    async def _validate_against_targets(self, proxy_id: int, proxy_url: str, request_factory) -> Dict:
        successes = 0
        latencies = []
        anonymity_levels = []

        for target in self.validation_targets:
            start_time = time.time()
            try:
                async with request_factory(target) as response:
                    response.raise_for_status()
                    latency_ms = int((time.time() - start_time) * 1000)
                    anonymity = await self._detect_anonymity(response)
                    successes += 1
                    latencies.append(latency_ms)
                    anonymity_levels.append(anonymity)
            except Exception as e:
                if self.debug_mode:
                    logger.debug(
                        "Proxy {} (ID: {}) failed target {}: {}",
                        proxy_url,
                        proxy_id,
                        target,
                        type(e).__name__,
                    )

        if successes < self.validation_success_threshold:
            return {"id": proxy_id, "success": False}

        return {
            "id": proxy_id,
            "success": True,
            "latency": int(sum(latencies) / len(latencies)),
            "anonymity": self._combine_anonymity_levels(anonymity_levels),
        }

    async def _detect_anonymity(self, response: aiohttp.ClientResponse) -> str:
        try:
            data = await response.json(content_type=None)
        except Exception:
            return "unknown"

        headers = data.get("headers") if isinstance(data, dict) else None
        if not isinstance(headers, dict):
            return "unknown"

        normalized_headers = {str(key).lower() for key in headers}
        transparent_headers = {"x-forwarded-for", "via", "x-real-ip"}
        return "transparent" if normalized_headers & transparent_headers else "elite"

    def _combine_anonymity_levels(self, anonymity_levels: List[str]) -> str:
        if not anonymity_levels:
            return "unknown"
        if "transparent" in anonymity_levels:
            return "transparent"
        if all(level == "elite" for level in anonymity_levels):
            return "elite"
        return "unknown"

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

    def _run_validation_cycle(self):
        with self.lock:
            if self.is_validating:
                logger.warning(
                    "Validation cycle is already in progress. Skipping this scheduled run."
                )
                return
            self.is_validating = True
        try:
            proxies_to_validate = self.db.get_proxies_to_validate(
                interval_minutes=self.validation_window_minutes,
                limit=self.validation_batch_limit,
            )
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
        if newly_active_proxies is None:
            logger.warning(
                "Skipping proxy sync because active proxy query failed. Keeping previous in-memory pools."
            )
            return

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

            self._sync_premium_proxies_locked()

    def _update_dashboard_sources(self):
        logger.info("Refreshing dashboard sources from config and database...")
        # Use only unique sources from stats data (source_stats_by_minute).
        # This keeps dashboard source options aligned with real observable traffic.
        db_sources = self.db.get_distinct_sources()
        with self.lock:
            fetcher_job_names = {job["name"] for job in self.fetcher_jobs}

        all_sources = {
            source
            for source in db_sources
            if source
            and source not in fetcher_job_names
            and not source.startswith("proxy_source_")
        }

        with self.lock:
            self.dashboard_sources = all_sources
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
            "consecutive_failures": 0,  # Diagnostic only; selection is score-based
            # NEW: Sliding window for recent performance (last 100 results max)
            "recent_results": [],       # List of [timestamp, success: bool, latency_ms: int|None]
            "avg_latency_ms": None,     # Exponential moving average of latency
            "last_feedback_ts": None,   # Unix timestamp of latest feedback
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
            proxy_pools = self.available_proxies.get(source)
            if not proxy_pools:
                logger.warning(f"No proxy pools defined for source '{source}'.")
                return None

            top_tier = proxy_pools.get("top_tier", [])
            bottom_tier = proxy_pools.get("bottom_tier", [])
            flat_pool = top_tier + bottom_tier
            candidates = self._filter_cooldown_candidates(source, flat_pool)
            if not candidates:
                logger.warning(f"No available proxy for source '{source}' after cooldown filtering.")
                return None

            if self.selection_strategy == "uniform":
                selected = random.choice(candidates)
            elif self.selection_strategy == "tiered":
                selected = self._select_from_tiers(source, top_tier, bottom_tier, candidates)
            elif self.selection_strategy == "softmax":
                selected = self._select_weighted_by_score(source, candidates, softmax=True)
            else:
                selected = self._select_weighted_by_score(source, candidates)

            self.proxy_last_handed_out_ts[source][selected] = time.time()
            return selected

    def _filter_cooldown_candidates(self, source: str, proxy_urls: List[str]) -> List[str]:
        if self.proxy_cooldown_ms <= 0:
            return list(proxy_urls)

        now = time.time()
        cooldown_s = self.proxy_cooldown_ms / 1000
        last_handed_out = self.proxy_last_handed_out_ts.get(source, {})
        return [
            proxy_url
            for proxy_url in proxy_urls
            if now - last_handed_out.get(proxy_url, 0) >= cooldown_s
        ]

    def _select_from_tiers(
        self,
        source: str,
        top_tier: List[str],
        bottom_tier: List[str],
        candidates: List[str],
    ) -> str:
        candidate_set = set(candidates)
        available_top = [proxy for proxy in top_tier if proxy in candidate_set]
        available_bottom = [proxy for proxy in bottom_tier if proxy in candidate_set]

        # Determine which tier to pull from
        use_top_tier = random.randint(1, 100) <= self.top_tier_load_percentage

        if use_top_tier and available_top:
            return random.choice(available_top)
        if available_bottom:
            return random.choice(available_bottom)
        if available_top:
            return random.choice(available_top)
        return random.choice(candidates)

    def _select_weighted_by_score(self, source: str, candidates: List[str], softmax: bool = False) -> str:
        stats = self.source_stats.get(source, {})
        weights = []
        for proxy_url in candidates:
            score = float(stats.get(proxy_url, {}).get("score", 50.0))
            if softmax:
                weights.append(math.exp((score - 50.0) / self.softmax_temperature))
            else:
                weights.append(max(self.selection_weight_floor, score))
        return random.choices(candidates, weights=weights, k=1)[0]

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
        with self.lock:
            self._sync_premium_proxies_locked()

        if self.premium_proxies:
            top_scores = []
            with self.lock:
                for url in self.premium_proxies[:5]:
                    top_scores.append(
                        max(
                            (
                                stats.get(url, {}).get("score", 0)
                                for stats in self.source_stats.values()
                            ),
                            default=0,
                        )
                    )
            logger.info(
                f"Premium proxy pool synced: {len(self.premium_proxies)} proxies loaded "
                f"(top 5 scores: {top_scores})"
            )
        else:
            logger.warning("No premium proxies found: no active proxies available.")

    def _sync_premium_proxies_locked(self):
        premium_pool_size = self.config.getint(
            "source_pool", "premium_pool_size", fallback=20
        )
        min_usage_count = self.config.getint(
            "source_pool", "premium_min_usage_count", fallback=50
        )

        # Aggregate all proxies with their highest score across all sources.
        battle_tested_scores: Dict[str, float] = {}
        all_proxy_scores: Dict[str, float] = {}

        for source, stats in self.source_stats.items():
            for proxy_url, stat in stats.items():
                usage_count = stat.get("success_count", 0) + stat.get("failure_count", 0)
                score = stat.get("score", 0)

                if proxy_url not in all_proxy_scores or score > all_proxy_scores[proxy_url]:
                    all_proxy_scores[proxy_url] = score

                if usage_count >= min_usage_count:
                    if (
                        proxy_url not in battle_tested_scores
                        or score > battle_tested_scores[proxy_url]
                    ):
                        battle_tested_scores[proxy_url] = score

        active_battle_tested = {
            url: score
            for url, score in battle_tested_scores.items()
            if url in self.active_proxies
        }
        active_all_proxies = {
            url: score
            for url, score in all_proxy_scores.items()
            if url in self.active_proxies
        }

        score_pool = active_battle_tested or active_all_proxies
        if not score_pool:
            self.premium_proxies = []
            return

        sorted_proxies = sorted(score_pool.items(), key=lambda x: x[1], reverse=True)
        self.premium_proxies = [url for url, _ in sorted_proxies[:premium_pool_size]]

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

        if "last_feedback_ts" not in stat:
            recent_results = stat.get("recent_results", [])
            valid_timestamps = [
                r[0]
                for r in recent_results
                if isinstance(r, list) and len(r) >= 1 and isinstance(r[0], (int, float))
            ]
            stat["last_feedback_ts"] = max(valid_timestamps) if valid_timestamps else None
        if "consecutive_failures" not in stat:
            stat["consecutive_failures"] = 0
        if "avg_latency_ms" not in stat:
            stat["avg_latency_ms"] = None

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
        now_ts = time.time()

        if self.elo_time_decay_enabled and recent:
            max_age_seconds = self.elo_max_result_age_hours * 3600
            recent = [
                r
                for r in recent
                if isinstance(r, list)
                and len(r) >= 3
                and isinstance(r[0], (int, float))
                and now_ts - r[0] <= max_age_seconds
            ]

        if not recent:
            # No recent data, use historical if available
            total = stat.get("success_count", 0) + stat.get("failure_count", 0)
            if total > 0:
                success_rate = stat.get("success_count", 0) / total
                historical_score = min(100, max(0, success_rate * 80 + 10))

                if self.elo_time_decay_enabled:
                    last_feedback_ts = stat.get("last_feedback_ts")
                    if isinstance(last_feedback_ts, (int, float)):
                        age_seconds = max(0.0, now_ts - last_feedback_ts)
                        half_life_seconds = self.elo_decay_half_life_hours * 3600
                        decay_factor = math.pow(0.5, age_seconds / half_life_seconds)
                        # Pull old historical scores back to neutral over time.
                        return 50 + (historical_score - 50) * decay_factor
                    # Timestamp unknown: treat historical counters as stale.
                    return 50.0

                return historical_score  # Range 10-90 for historical
            return 50.0  # Neutral for completely new proxies

        if self.elo_time_decay_enabled:
            half_life_seconds = self.elo_decay_half_life_hours * 3600
            weights = [
                math.pow(0.5, max(0.0, now_ts - r[0]) / half_life_seconds)
                if isinstance(r[0], (int, float))
                else 1.0
                for r in recent
            ]
        else:
            weights = [1.0] * len(recent)

        # 1. Success rate component (0-60 points)
        total_weight = sum(weights) or 1.0
        successes_weight = sum(
            w for r, w in zip(recent, weights) if len(r) >= 2 and bool(r[1])
        )
        success_rate = successes_weight / total_weight
        success_score = success_rate * 60

        # 2. Latency component (0-30 points)
        # Lower latency = higher score
        latency_pairs = [
            (r[2], w)
            for r, w in zip(recent, weights)
            if len(r) >= 3 and r[1] and r[2] is not None
        ]
        if latency_pairs:
            weighted_latency_sum = sum(latency * weight for latency, weight in latency_pairs)
            latency_weight_sum = sum(weight for _, weight in latency_pairs) or 1.0
            avg_latency = weighted_latency_sum / latency_weight_sum
            # Fast proxies get full latency score; slower ones linearly decay to 0.
            if avg_latency <= self.latency_full_score_ms:
                latency_score = 30
            elif avg_latency <= self.latency_zero_score_ms:
                latency_range = self.latency_zero_score_ms - self.latency_full_score_ms
                latency_score = max(
                    0,
                    30
                    - ((avg_latency - self.latency_full_score_ms) / latency_range)
                    * 30,
                )
            else:
                latency_score = 0
        else:
            latency_score = 15  # Neutral if no latency data

        # 3. Consistency bonus (0-10 points)
        # Reward proxies with stable recent performance
        if len(recent) >= 10:
            recent_10 = recent[-10:]
            weights_10 = weights[-10:]
            success_weight_10 = sum(
                w for r, w in zip(recent_10, weights_10) if len(r) >= 2 and bool(r[1])
            )
            total_weight_10 = sum(weights_10) or 1.0
            recent_success_rate = success_weight_10 / total_weight_10
            if recent_success_rate >= 0.9:
                consistency_score = 10  # Excellent consistency
            elif recent_success_rate >= 0.7:
                consistency_score = 7   # Good consistency
            else:
                consistency_score = recent_success_rate * 10  # Proportional
        else:
            consistency_score = 5  # Neutral for new proxies

        return min(100, max(0, success_score + latency_score + consistency_score))

    def process_feedback(
        self,
        source: str,
        proxy_url: str,
        status_code: int,
        response_time_ms: Optional[int] = None,
        failure_kind: Optional[str] = None,
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
        is_success = self.classify_feedback_status(status_code)
        if failure_kind and failure_kind not in VALID_FAILURE_KINDS:
            logger.warning("Ignoring unknown failure_kind '{}'", failure_kind)
            failure_kind = None

        current_minute = datetime.now().replace(second=0, microsecond=0)
        current_timestamp = time.time()

        with self.lock:
            # Update feedback buffer for database flush
            if is_success:
                self.feedback_buffer[current_minute][source]["success"] += 1
            else:
                self.feedback_buffer[current_minute][source]["failure"] += 1

            target_sources = [source]
            if not is_success and failure_kind == "dead":
                target_sources = [
                    candidate_source
                    for candidate_source, stats in self.source_stats.items()
                    if proxy_url in stats
                ] or [source]

            for target_source in target_sources:
                stat = self.source_stats.get(target_source, {}).get(proxy_url)
                if not stat:
                    continue
                self._apply_feedback_to_stat(
                    target_source,
                    proxy_url,
                    stat,
                    is_success,
                    response_time_ms,
                    current_timestamp,
                )

    def _apply_feedback_to_stat(
        self,
        source: str,
        proxy_url: str,
        stat: Dict,
        is_success: bool,
        response_time_ms: Optional[int],
        current_timestamp: float,
    ):
        # Migrate legacy stats if needed
        stat = self._migrate_legacy_stat(stat)
        
        # Update historical counters
        if is_success:
            stat["success_count"] += 1
            stat["consecutive_failures"] = 0
            
            # Update exponential moving average of latency for observability.
            if response_time_ms is not None:
                alpha = self.avg_latency_alpha
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
        stat["last_feedback_ts"] = current_timestamp
        
        # Recalculate ELO score
        old_score = stat["score"]
        stat["score"] = self._calculate_elo_score(stat, source)
        
        response_time_str = f"{response_time_ms:.0f}" if response_time_ms is not None else "N/A"
        logger.debug(
            f"ELO Score: {source:<15} | {proxy_url:<30} | "
            f"{'OK' if is_success else 'FAIL':<4} | {response_time_str:<6}ms | "
            f"{old_score:.1f} -> {stat['score']:.1f}"
        )

    def classify_feedback_status(self, status_code: int) -> bool:
        if status_code in FAILED_STATUS_CODES:
            return False
        if status_code in LEGACY_SUCCESS_STATUS_CODES:
            return True
        if 100 <= status_code < 400:
            return True
        if 400 <= status_code <= 599:
            return False
        raise ValueError(f"Unsupported feedback status: {status_code}")

    def is_valid_feedback_status(self, status_code: int) -> bool:
        try:
            self.classify_feedback_status(status_code)
            return True
        except ValueError:
            return False
