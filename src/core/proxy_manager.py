# -*- coding: utf-8 -*-
import os
import configparser
import copy
import ipaddress
import json
import math
import random
import tempfile
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
# Relative paths in config are resolved from here, not from the process CWD.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_EXAMPLE_PATH = PROJECT_ROOT / "config" / "config.example.ini"

# Protocols accepted by the fetcher parser. The DB column is VARCHAR(10).
VALID_PROXY_PROTOCOLS = {"http", "https", "socks4", "socks5"}
MAX_PROTOCOL_LENGTH = 10
MAX_IP_LENGTH = 45  # Matches the proxies.ip VARCHAR(45) column.
MIN_PORT = 1
MAX_PORT = 65535
# A latency is a request duration in milliseconds; anything past a day is not
# one by default. Deployments may tune the boundary in [source_pool], while this
# constant remains the backward-compatible fallback.
DEFAULT_MAX_FEEDBACK_LATENCY_MS = 24 * 60 * 60 * 1000

# Persisted derived scoring state is only trusted when this version matches.
SCORING_VERSION = 2
DEFAULT_RELIABILITY_PRIOR = 0.05
RESTORE_MODES = frozenset({"normal", "no-restore", "fresh-scoring"})

# curl exit codes that mean "the network path failed right now" rather than
# "the server answered and said no". Only the latter earns the long backoff.
TRANSIENT_CURL_EXIT_CODES = frozenset({5, 6, 7, 16, 18, 28, 35, 52, 55, 56, 92})

# HTTP statuses that say "ask again later"; every other >=400 is persistent.
TRANSIENT_HTTP_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})

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

class FetchError(RuntimeError):
    """
    A proxy-source fetch failure, tagged with how long it is worth waiting.

    `transient` separates "the connection was reset" from "the server returned
    404": the first is a blip that must not lock a source out for half an hour,
    the second is a source that will keep saying no. _fetch_and_parse_source
    picks the backoff cap from this flag.
    """

    def __init__(self, message: str, transient: bool = True):
        super().__init__(message)
        self.transient = transient


class ProxyManager:
    """Manages the proxy lifecycle, state, and business logic."""

    def __init__(self, config_path, restore_mode: str = "normal"):
        if restore_mode not in RESTORE_MODES:
            raise ValueError(f"Unknown restore mode: {restore_mode}")
        self.restore_mode = restore_mode
        self.durable_reputation_enabled = restore_mode != "fresh-scoring"
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
        self.outage_states: Dict[str, Dict] = {}
        self.cold_start_fallback_logged: Set[str] = set()

        # Serialises the whole snapshot -> dump -> fsync -> replace sequence.
        # self.lock only guards the snapshot, so two concurrent backups could
        # interleave and let an older snapshot land last.
        self.backup_lock = threading.Lock()
        # Periodic and shutdown flushes can overlap. Keep per-proxy absolute
        # totals ordered so an older snapshot cannot land after a newer one and
        # make durable reputation move backwards.
        self.feedback_persist_lock = threading.Lock()

        self.feedback_buffer = defaultdict(
            lambda: defaultdict(lambda: defaultdict(int))
        )

        self.dashboard_sources: Set[str] = set()
        self.last_source_refresh_time = 0

        # Proxy URLs whose feedback counters have moved since the last time
        # they were written back to the proxies table.
        self.pending_feedback_persist: Set[str] = set()

        self._load_config()
        self.check_config_drift()
        logger.info(
            "Stats backup path resolved to {} (exists: {}).",
            self.stats_backup_path,
            self.stats_backup_path.exists(),
        )
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
        # Share of validation_batch_limit reserved for never-validated proxies.
        # The rest goes to re-validating proxies that are currently alive, so a
        # flood of freshly fetched proxies cannot starve the liveness re-checks.
        self.validation_new_proxy_ratio = min(
            1.0,
            max(
                0.0,
                self.config.getfloat(
                    "validator", "validation_new_proxy_ratio", fallback=0.5
                ),
            ),
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
            "source_pool", "selection_strategy", fallback="softmax"
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
        self.exploration_min_ratio = min(
            1.0,
            max(
                0.0,
                self.config.getfloat(
                    "source_pool", "exploration_min_ratio", fallback=0.05
                ),
            ),
        )
        self.exploration_max_ratio = min(
            1.0,
            max(
                self.exploration_min_ratio,
                self.config.getfloat(
                    "source_pool", "exploration_max_ratio", fallback=0.30
                ),
            ),
        )
        self.exploration_target_qualified = max(
            1,
            self.config.getint(
                "source_pool", "exploration_target_qualified", fallback=50
            ),
        )
        self.exploration_discovery_share = min(
            1.0,
            max(
                0.0,
                self.config.getfloat(
                    "source_pool", "exploration_discovery_share", fallback=2 / 3
                ),
            ),
        )
        self.qualification_min_results = max(
            1,
            self.config.getint(
                "source_pool", "qualification_min_results", fallback=3
            ),
        )
        self.probation_attempts = max(
            self.qualification_min_results,
            self.config.getint("source_pool", "probation_attempts", fallback=3),
        )
        self.retry_attempts = max(
            0, self.config.getint("source_pool", "retry_attempts", fallback=2)
        )
        self.retry_delay_s = max(
            0.0,
            self.config.getfloat("source_pool", "retry_delay_seconds", fallback=3600),
        )
        self.probation_forgiveness_hours = max(
            0.1,
            self.config.getfloat(
                "source_pool", "probation_forgiveness_hours", fallback=48.0
            ),
        )
        self.proxy_inflight_timeout_s = max(
            0.1,
            self.config.getfloat(
                "source_pool", "proxy_inflight_timeout_seconds", fallback=120.0
            ),
        )
        self.selection_weight_floor = max(
            0.01,
            self.config.getfloat("source_pool", "selection_weight_floor", fallback=1.0),
        )
        self.softmax_temperature = max(
            0.1,
            self.config.getfloat("source_pool", "softmax_temperature", fallback=14.0),
        )
        self.avg_latency_alpha = min(
            1.0,
            max(0.01, self.config.getfloat("source_pool", "avg_latency_alpha", fallback=0.3)),
        )
        self.max_feedback_latency_ms = max(
            1,
            self.config.getint(
                "source_pool",
                "max_feedback_latency_ms",
                fallback=DEFAULT_MAX_FEEDBACK_LATENCY_MS,
            ),
        )

        # Fetcher configuration.
        # Proxy-list downloads always use curl. This host routes curl and Python
        # traffic differently, and Python's direct egress to the source URLs is
        # the path that failed. The validator remains aiohttp-based because it
        # connects through the proxies and inspects response headers.
        self.fetch_connect_timeout_s = self.config.getint(
            "fetcher", "connect_timeout_s", fallback=30
        )
        self.fetch_total_timeout_s = self.config.getint(
            "fetcher", "total_timeout_s", fallback=60
        )
        self.fetch_curl_retries = max(
            0, self.config.getint("fetcher", "curl_retries", fallback=2)
        )
        self.fetch_curl_retry_delay_s = max(
            0, self.config.getint("fetcher", "curl_retry_delay_s", fallback=1)
        )
        self.fetch_backoff_base_s = max(
            1, self.config.getint("fetcher", "backoff_base_s", fallback=30)
        )
        # Two caps, because the two failure classes deserve different patience.
        self.fetch_backoff_max_s = max(
            self.fetch_backoff_base_s,
            self.config.getint("fetcher", "backoff_max_s", fallback=1800),
        )
        self.fetch_backoff_transient_max_s = max(
            self.fetch_backoff_base_s,
            min(
                self.fetch_backoff_max_s,
                self.config.getint(
                    "fetcher", "backoff_transient_max_s", fallback=300
                ),
            ),
        )

        # Backup configuration
        self.stats_backup_enabled = self.config.getboolean(
            "backup", "stats_backup_enabled", fallback=True
        )
        self.stats_backup_interval_s = self.config.getint(
            "backup", "stats_backup_interval_seconds", fallback=3600  # 1 hour
        )
        self.normal_stats_backup_path = self._resolve_project_path(
            self.config.get(
                "backup",
                "stats_backup_path",
                fallback="./.local/data/proxy_stats_backup.json",
            )
        )
        self.stats_backup_path = self._isolated_backup_path(
            self.normal_stats_backup_path, self.restore_mode
        )

        # Two-speed online reliability. Latency never enters this score.
        self.reliability_prior = min(
            1.0,
            max(
                0.0,
                self.config.getfloat(
                    "source_pool", "reliability_prior", fallback=DEFAULT_RELIABILITY_PRIOR
                ),
            ),
        )
        self.reliability_slow_alpha = min(
            1.0,
            max(
                0.0001,
                self.config.getfloat(
                    "source_pool", "reliability_slow_alpha", fallback=0.12
                ),
            ),
        )
        self.reliability_fast_alpha = min(
            1.0,
            max(
                self.reliability_slow_alpha,
                self.config.getfloat(
                    "source_pool", "reliability_fast_alpha", fallback=0.30
                ),
            ),
        )
        self.reliability_decay_half_life_hours = max(
            0.1,
            self.config.getfloat(
                "source_pool", "reliability_decay_half_life_hours", fallback=24.0
            ),
        )
        self.reliability_recent_results_limit = max(
            1,
            self.config.getint(
                "source_pool", "reliability_recent_results_limit", fallback=100
            ),
        )
        self.reliability_history_prior_weight = max(
            0.0,
            self.config.getfloat(
                "source_pool", "reliability_history_prior_weight", fallback=5.0
            ),
        )

        self.outage_guard_enabled = self.config.getboolean(
            "source_pool", "outage_guard_enabled", fallback=True
        )
        self.outage_window_size = max(
            1, self.config.getint("source_pool", "outage_window_size", fallback=20)
        )
        self.outage_min_distinct_proxies = max(
            1,
            min(
                self.outage_window_size,
                self.config.getint(
                    "source_pool", "outage_min_distinct_proxies", fallback=10
                ),
            ),
        )
        self.outage_healthy_success_ratio = min(
            1.0,
            max(
                0.0,
                self.config.getfloat(
                    "source_pool", "outage_healthy_success_ratio", fallback=0.50
                ),
            ),
        )
        self.outage_failure_ratio = min(
            1.0,
            max(
                0.0,
                self.config.getfloat(
                    "source_pool", "outage_failure_ratio", fallback=0.90
                ),
            ),
        )
        self.outage_recovery_success_ratio = min(
            1.0,
            max(
                0.0,
                self.config.getfloat(
                    "source_pool", "outage_recovery_success_ratio", fallback=0.30
                ),
            ),
        )

        logger.info("Configuration loaded.")

    @staticmethod
    def _resolve_project_path(raw_path: str) -> Path:
        """Resolve a config path against the project root, not the process CWD."""
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path.resolve()

    @staticmethod
    def _isolated_backup_path(normal_path: Path, restore_mode: str) -> Path:
        if restore_mode == "normal":
            return normal_path
        suffix = normal_path.suffix or ".json"
        marker = "no-restore" if restore_mode == "no-restore" else "fresh-scoring"
        return normal_path.with_name(f"{normal_path.stem}.{marker}{suffix}")

    def check_config_drift(self, example_path: Path = CONFIG_EXAMPLE_PATH) -> Dict:
        """
        Compare the live config against config.example.ini and warn about drift.

        Silent config drift is how this service lost its scoring state for weeks:
        keys that exist in the example but not in config.ini quietly fall back to
        code defaults. Report them loudly at startup instead.
        """
        example = configparser.ConfigParser()
        try:
            if not example.read(example_path, encoding="utf-8"):
                logger.warning(
                    "Config drift check skipped: example config not found at {}",
                    example_path,
                )
                return {"missing": [], "unknown": [], "checked": False}
        except configparser.Error as e:
            logger.warning("Config drift check skipped: cannot parse example: {}", e)
            return {"missing": [], "unknown": [], "checked": False}

        def _is_user_defined(section: str) -> bool:
            # Proxy source sections are per-deployment; never diff them.
            return section.startswith("proxy_source_")

        missing: List[str] = []
        unknown: List[str] = []

        for section in example.sections():
            if _is_user_defined(section):
                continue
            if not self.config.has_section(section):
                missing.append(f"[{section}] (entire section)")
                continue
            for option in example.options(section):
                if not self.config.has_option(section, option):
                    missing.append(f"[{section}] {option}")

        for section in self.config.sections():
            if _is_user_defined(section) or not example.has_section(section):
                continue
            for option in self.config.options(section):
                if not example.has_option(section, option):
                    unknown.append(f"[{section}] {option}")

        if missing:
            logger.warning(
                "Config drift: {} key(s) present in config.example.ini are missing from "
                "the live config and fall back to code defaults.",
                len(missing),
            )
            for entry in missing:
                logger.warning("  [config-drift] missing: {}", entry)
        if unknown:
            logger.warning(
                "Config drift: {} key(s) in the live config are absent from "
                "config.example.ini (possibly deprecated).",
                len(unknown),
            )
            for entry in unknown:
                logger.warning("  [config-drift] unknown: {}", entry)
        if not missing and not unknown:
            logger.info("Config drift check passed: live config matches the example.")

        return {"missing": missing, "unknown": unknown, "checked": True}

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

    # Config that cannot be applied without a restart, because it is consumed
    # once at construction time.
    RESTART_REQUIRED_CONFIG = (
        "[database] connection pool",
        "[server] port",
        "[logging] log_dir / log_file_base_name",
    )

    def reload_sources(self) -> Dict:
        """
        Reloads the config file: all tunables via _load_config(), plus proxy
        sources and fetcher jobs.

        Everything in the config is re-read except the settings listed in
        RESTART_REQUIRED_CONFIG, which are consumed once during construction.
        """
        logger.info("Attempting to reload configuration from config file...")
        with self.lock:
            # A fresh parser, not self.config.read(): ConfigParser.read() MERGES
            # into the existing object, so a key or a [proxy_source_*] section
            # deleted from the file would keep its old in-memory value and the
            # reload would silently not be authoritative.
            new_config = configparser.ConfigParser()
            if not new_config.read(self.config_path, encoding="utf-8"):
                logger.error(
                    "Config reload aborted: {} could not be read. Keeping the "
                    "running configuration.",
                    self.config_path,
                )
                raise RuntimeError(f"Config file not readable: {self.config_path}")

            # Apply transactionally. _load_config() assigns ~40 attributes one
            # by one, so an invalid value partway through would otherwise leave
            # the service running on half-new, half-old settings.
            old_config = self.config
            attribute_snapshot = dict(self.__dict__)
            old_predefined_sources_before_reload = self.predefined_sources.copy()
            old_job_names = {job["name"] for job in self.fetcher_jobs}

            self.config = new_config
            try:
                self._load_config()
                # Parsed inside the protected block, assigned outside it.
                # _load_fetcher_jobs() calls int() on update_interval_minutes,
                # so a typo in one [proxy_source_*] section raises here - after
                # _load_config() has already committed ~40 tunables. Leaving the
                # assignment out here is what keeps a failure from producing the
                # new-tunables / old-jobs hybrid that README rules out.
                new_fetcher_jobs = self._load_fetcher_jobs()
            except Exception as e:
                self.__dict__.update(attribute_snapshot)
                self.config = old_config
                self._load_config()
                logger.error(
                    "Config reload failed ({}); rolled back to the previous "
                    "configuration.",
                    e,
                )
                raise

            self.check_config_drift()

            self.fetcher_jobs = new_fetcher_jobs
            new_job_names = {job["name"] for job in self.fetcher_jobs}
            added_jobs = list(new_job_names - old_job_names)
            removed_jobs = list(old_job_names - new_job_names)
            if added_jobs or removed_jobs:
                logger.info(
                    f"Fetcher jobs reloaded. Added: {added_jobs}, Removed: {removed_jobs}"
                )
            else:
                logger.info("Fetcher jobs reloaded. No changes detected.")

            old_predefined_sources = old_predefined_sources_before_reload

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
                    self.outage_states.pop(source, None)
                    self.cold_start_fallback_logged.discard(source)
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
            "restart_required_for": list(self.RESTART_REQUIRED_CONFIG),
        }

    def _fetch_and_parse_source(self, job: Dict) -> List:
        """
        DEADLOCK FIX: This method now returns a list of proxies instead of writing to the DB.
        Proxy-list downloads always use curl via _fetch_source_text.
        """
        url = job["url"]
        logger.info(f"Fetching proxy source: {job['name']} from {url}")
        proxies_to_insert = []
        rejected_count = 0
        try:
            response_text = self._fetch_source_text(url)
            for line in response_text.splitlines():
                line = line.strip()
                if not line:
                    continue
                parsed = self._parse_proxy_line(line, job["default_protocol"])
                if parsed is None:
                    rejected_count += 1
                    continue
                proxies_to_insert.append(parsed)
            if rejected_count:
                logger.warning(
                    "Fetcher job '{}' discarded {} malformed proxy line(s) out of {}.",
                    job["name"],
                    rejected_count,
                    rejected_count + len(proxies_to_insert),
                )
        except Exception as e:
            logger.error(f"Failed to fetch from {job['name']} ({url}): {e}")
            failures = job.get("failure_count", 0) + 1
            job["failure_count"] = failures
            backoff_seconds = self._fetch_backoff_seconds(e, failures)
            job["last_run"] = time.time() + backoff_seconds - job["interval_minutes"] * 60
            logger.warning(
                "Fetcher job '{}' backed off for {}s after {} consecutive "
                "{} failure(s).",
                job["name"],
                backoff_seconds,
                failures,
                "transient" if self._is_transient_fetch_error(e) else "persistent",
            )
        else:
            job["failure_count"] = 0
        return proxies_to_insert

    @staticmethod
    def _is_transient_fetch_error(error: Exception) -> bool:
        """
        Whether a fetch failure is worth retrying soon.

        Anything the fetch layer did not classify counts as transient: the
        failure this backoff exists to contain is a connectivity blip being
        amplified into a half-hour outage, so an unknown error waits the short
        cap rather than the long one.
        """
        return bool(getattr(error, "transient", True))

    def _fetch_backoff_seconds(self, error: Exception, failures: int) -> int:
        """
        Exponential backoff, capped by the failure class.

        A connection reset is a blip: with the shipped defaults it tops out at
        backoff_transient_max_s, so a source cannot be locked out for the rest
        of the hour by a network that is only intermittently broken. A 404 is
        the source itself saying no, and gets the longer backoff_max_s.
        """
        cap = (
            self.fetch_backoff_transient_max_s
            if self._is_transient_fetch_error(error)
            else self.fetch_backoff_max_s
        )
        # The exponent is bounded before the shift so a long-broken source
        # cannot build a 2**n that costs anything to compute.
        growth = self.fetch_backoff_base_s * (2 ** min(max(failures - 1, 0), 16))
        return int(min(cap, growth))

    @staticmethod
    def _parse_proxy_line(
        line: str, default_protocol: Optional[str]
    ) -> Optional[Tuple[str, str, int]]:
        """
        Parse one proxy list line into (protocol, ip, port), or None if invalid.

        Every value is validated against the DB column constraints before it can
        reach insert_proxies(): that INSERT is a single transaction, so one row
        that PostgreSQL rejects would roll back the whole fetch cycle.
        """
        if "://" in line:
            protocol, rest = line.split("://", 1)
        elif default_protocol:
            protocol, rest = default_protocol, line
        else:
            return None

        protocol = protocol.strip().lower()
        if protocol not in VALID_PROXY_PROTOCOLS or len(protocol) > MAX_PROTOCOL_LENGTH:
            return None

        if ":" not in rest:
            return None
        ip, port_str = rest.rsplit(":", 1)
        ip = ip.strip()

        # Credentials are not supported; "user:pass@host" would otherwise be
        # stored verbatim as the IP.
        if not ip or "@" in ip:
            return None

        # Must be a real IP literal. Length and "@" checks alone are not enough:
        # a NUL or other control character passes them, and psycopg2 then raises
        # on the whole execute_values batch, taking every valid row with it.
        # The ip column is VARCHAR(45) - exactly max IPv6 length - so the schema
        # already intends literals, not hostnames.
        host = ip[1:-1] if ip.startswith("[") and ip.endswith("]") else ip
        try:
            parsed_ip = ipaddress.ip_address(host)
        except ValueError:
            return None

        # An IPv6 literal must keep its brackets. The stored value is
        # interpolated straight into "{protocol}://{ip}:{port}" by the
        # validator, by get_active_proxies() and by the API, and without them
        # "http://2001:db8::1:8080" has no parseable port - yarl rejects it, so
        # the proxy can be neither validated nor handed out. Normalising here,
        # at the only writer, keeps every one of those call sites correct.
        canonical_host = parsed_ip.compressed
        ip = f"[{canonical_host}]" if parsed_ip.version == 6 else canonical_host
        if len(ip) > MAX_IP_LENGTH:
            return None

        try:
            port = int(port_str.strip())
        except ValueError:
            return None
        if not MIN_PORT <= port <= MAX_PORT:
            return None

        return (protocol, ip, port)

    def _fetch_source_text(self, url: str) -> str:
        """Fetch one proxy list through curl."""
        return self._fetch_source_text_curl(url)

    def _fetch_source_text_curl(self, url: str) -> str:
        command = [
            "curl",
            "-sS",
            "-f",
            # Follow redirects because several public proxy-list URLs move to a
            # canonical download endpoint.
            "-L",
            "--connect-timeout",
            str(self.fetch_connect_timeout_s),
            "--max-time",
            str(self.fetch_total_timeout_s),
            # curl's exit code 22 collapses every HTTP 4xx/5xx response. Append
            # the final response code so 404 can receive the persistent cap
            # while 429/503 receive the transient cap.
            "--write-out",
            "\n%{http_code}",
        ]
        if self.fetch_curl_retries > 0:
            command += [
                "--retry",
                str(self.fetch_curl_retries),
                "--retry-delay",
                str(self.fetch_curl_retry_delay_s),
                # curl does not retry a refused connection unless asked, and a
                # refused connection is exactly the blip worth one more try.
                "--retry-connrefused",
            ]
        command.append(url)

        # --max-time bounds one attempt, so the process budget has to cover
        # every retry plus its delay, or the subprocess timeout fires first and
        # the retries never happen.
        attempts = self.fetch_curl_retries + 1
        process_timeout = (
            attempts * (self.fetch_total_timeout_s + self.fetch_curl_retry_delay_s) + 5
        )
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=process_timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise FetchError(
                f"curl timed out after {process_timeout}s", transient=True
            ) from e
        except FileNotFoundError as e:
            # No curl binary on this host. Waiting will not install it, so this
            # is persistent and requires operator action.
            raise FetchError("curl executable not found", transient=False) from e

        body, separator, status_text = result.stdout.rpartition("\n")
        http_status = (
            int(status_text)
            if separator and len(status_text) == 3 and status_text.isdigit()
            else None
        )

        if http_status is not None and http_status >= 400:
            raise FetchError(
                f"HTTP {http_status} from {url}",
                transient=http_status in TRANSIENT_HTTP_STATUS_CODES,
            )

        if result.returncode != 0:
            raise FetchError(
                f"curl failed with return code {result.returncode}: "
                f"{result.stderr.strip()}",
                transient=result.returncode in TRANSIENT_CURL_EXIT_CODES,
            )
        if http_status is None:
            raise FetchError("curl did not report an HTTP status", transient=True)
        return body

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

    def _collect_validation_batch(self) -> List[Dict]:
        """
        Build the validation batch with an explicit budget split.

        Never-validated proxies and live proxies due for a re-check compete for
        the same validation_batch_limit. Splitting the budget stops a flood of
        freshly fetched proxies from starving the liveness re-checks; whichever
        side under-uses its share donates the remainder to the other.
        """
        limit = self.validation_batch_limit
        new_budget = int(limit * self.validation_new_proxy_ratio)
        revalidate_budget = limit - new_budget

        new_proxies = self.db.get_new_proxies_to_validate(limit=new_budget) or []
        revalidate_proxies = (
            self.db.get_active_proxies_to_revalidate(
                interval_minutes=self.validation_window_minutes,
                limit=revalidate_budget,
            )
            or []
        )

        # Donate unused budget in both directions so the cycle still fills up.
        unused_new = new_budget - len(new_proxies)
        if unused_new > 0 and len(revalidate_proxies) == revalidate_budget:
            revalidate_proxies += (
                self.db.get_active_proxies_to_revalidate(
                    interval_minutes=self.validation_window_minutes,
                    limit=revalidate_budget + unused_new,
                )
                or []
            )[revalidate_budget:]

        unused_revalidate = revalidate_budget - len(revalidate_proxies)
        if unused_revalidate > 0 and len(new_proxies) == new_budget:
            new_proxies += (
                self.db.get_new_proxies_to_validate(limit=new_budget + unused_revalidate)
                or []
            )[new_budget:]

        logger.info(
            "Validation budget: {} new (of {}) + {} re-validation (of {}).",
            len(new_proxies),
            new_budget,
            len(revalidate_proxies),
            revalidate_budget,
        )

        batch: List[Dict] = []
        seen_ids = set()
        for proxy in list(new_proxies) + list(revalidate_proxies):
            if proxy["id"] in seen_ids:
                continue
            seen_ids.add(proxy["id"])
            batch.append(proxy)
        return batch

    def _run_validation_cycle(self):
        with self.lock:
            if self.is_validating:
                logger.warning(
                    "Validation cycle is already in progress. Skipping this scheduled run."
                )
                return
            self.is_validating = True
        try:
            proxies_to_validate = self._collect_validation_batch()
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
                existing_ids = {p["id"] for p in proxies_to_validate}
                eligible_failed = self.db.get_eligible_failed_proxies(
                    window_minutes=self.validation_window_minutes,
                    max_attempts=self.max_validations_per_window,
                    limit=supplement_needed,
                    exclude_ids=sorted(existing_ids),
                )
                for p in eligible_failed:
                    if p["id"] not in existing_ids:
                        proxies_to_validate.append(p)

            if not proxies_to_validate:
                # The in-memory pool must still be refreshed: an empty batch is
                # not a reason to keep handing out a stale (possibly dead) pool.
                logger.info(
                    "No proxies to validate this cycle; refreshing pools anyway."
                )
                self._sync_and_select_top_proxies()
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

        # A proxy the stats pool has never seen - or has evicted since - is
        # about to be seeded. Bring its persisted feedback history back with it,
        # or eviction plus one validation pass would launder the record into a
        # pristine baseline score. Queried outside the lock; the query is
        # skipped entirely when nothing needs seeding.
        feedback_history = self._load_feedback_history_for(newly_active_proxies)

        with self.lock:
            self.active_proxies = newly_active_proxies
            for source in self.predefined_sources:
                stats_pool = self.source_stats.get(source, {})

                for proxy_url in self.active_proxies:
                    if proxy_url not in stats_pool:
                        stats_pool[proxy_url] = self._get_new_proxy_stat(
                            source, feedback_history.get(proxy_url)
                        )

                # Age every estimator toward the fixed prior before ranking.
                # This is the recovery path for idle proxies; it does not use
                # validator measurements or the measured population median.
                for proxy_url, stat in list(stats_pool.items()):
                    normalized = self._migrate_legacy_stat(stat)
                    normalized["score"] = self._calculate_elo_score(
                        normalized, source
                    )
                    stats_pool[proxy_url] = normalized

                stats_pool = self._truncate_stats_pool(source, stats_pool)
                self.source_stats[source] = stats_pool

                sorted_proxies = sorted(
                    stats_pool.items(),
                    key=lambda item: (
                        -float(item[1]["score"]),
                        self._latency_sort_key(item[1]),
                        item[0],
                    ),
                )

                # Only proxies that survived the latest validation may be handed
                # out. Their stats stay in the pool either way, so a proxy that
                # comes back to life keeps its history.
                usable_proxies = [
                    p_url
                    for p_url, _ in sorted_proxies
                    if p_url in self.active_proxies
                ][: self.max_pool_size]

                # NEW: Split the usable proxies into tiers
                top_tier = usable_proxies[: self.top_tier_size]
                bottom_tier = usable_proxies[self.top_tier_size :]

                self.available_proxies[source]["top_tier"] = top_tier
                self.available_proxies[source]["bottom_tier"] = bottom_tier

                logger.info(
                    f"Source '{source}' synced. "
                    f"Stats pool: {len(sorted_proxies)} proxies, "
                    f"of which {len(usable_proxies)} are alive and usable. "
                    f"Fixed reliability prior: {self._baseline_score(source):.1f}. "
                    f"Top Tier: {len(top_tier)} proxies. "
                    f"Bottom Tier: {len(bottom_tier)} proxies."
                )

            self._sync_premium_proxies_locked()

    @staticmethod
    def _split_proxy_url(proxy_url: str) -> Optional[Tuple[str, str, int]]:
        """Split a stored proxy URL back into its (protocol, ip, port) key."""
        if not isinstance(proxy_url, str) or "://" not in proxy_url:
            return None
        protocol, rest = proxy_url.split("://", 1)
        if ":" not in rest:
            return None
        # rsplit, so a bracketed IPv6 literal keeps its colons and only the
        # port is taken off the end.
        ip, port_str = rest.rsplit(":", 1)
        try:
            port = int(port_str)
        except ValueError:
            return None
        if not protocol or not ip:
            return None
        return protocol, ip, port

    def _load_feedback_history_for(self, active_proxies: Set[str]) -> Dict[str, Dict]:
        """
        Persisted feedback records for proxies this sync is about to seed.

        Skipped entirely when every live proxy is already in every pool, which
        is the steady state - the query only runs on the cycles where a proxy
        is actually joining or rejoining a pool.
        """
        if not self.durable_reputation_enabled:
            logger.info(
                "Fresh-scoring mode: skipping database reputation hydration."
            )
            return {}

        with self.lock:
            needs_seeding = any(
                proxy_url not in self.source_stats.get(source, {})
                for source in self.predefined_sources
                for proxy_url in active_proxies
            )
        if not needs_seeding:
            return {}

        history = self.db.get_active_feedback_history()
        if not isinstance(history, dict):
            logger.warning(
                "Feedback history unavailable this cycle; new stats are seeded "
                "at the untried baseline instead of their persisted record."
            )
            return {}
        return history

    def _persist_feedback_history(self):
        """
        Write the in-memory feedback counters of recently updated proxies back
        to the proxies table.

        Only proxies whose counters have actually moved are written, so this
        stays proportional to feedback volume rather than to pool size. The
        failed writes are put back into the pending set atomically, so an idle
        proxy is retried even if no later feedback arrives.
        """
        with self.feedback_persist_lock:
            self._persist_feedback_history_locked()

    def _persist_feedback_history_locked(self):
        """Persist one ordered snapshot; caller holds feedback_persist_lock."""
        if not self.durable_reputation_enabled:
            with self.lock:
                self.pending_feedback_persist.clear()
            return

        with self.lock:
            outage_protected = {
                proxy_url
                for state in self.outage_states.values()
                for proxy_url in state.get("protected_stats", {})
            }
            pending = self.pending_feedback_persist - outage_protected
            # Candidate-window mutations stay queued but cannot reach durable
            # monotonic counters until the outage decision commits or rolls
            # them back.
            self.pending_feedback_persist = (
                self.pending_feedback_persist & outage_protected
            )

            rows = []
            for proxy_url in pending:
                key = self._split_proxy_url(proxy_url)
                if key is None:
                    continue
                # A proxy can be tracked under several sources with different
                # counters. Persist the record of the source that has observed
                # it most: that is the fullest history available, and the
                # re-seed it feeds is per-source anyway.
                best_stat, best_total = None, -1
                for stats in self.source_stats.values():
                    stat = stats.get(proxy_url)
                    if not isinstance(stat, dict):
                        continue
                    total = int(stat.get("success_count", 0) or 0) + int(
                        stat.get("failure_count", 0) or 0
                    )
                    if total > best_total:
                        best_stat, best_total = stat, total
                if best_stat is None:
                    continue
                protocol, ip, port = key
                rows.append(
                    (
                        protocol,
                        ip,
                        port,
                        int(best_stat.get("success_count", 0) or 0),
                        int(best_stat.get("failure_count", 0) or 0),
                        self._coerce_timestamp(
                            best_stat.get("last_feedback_ts"), time.time()
                        ),
                    )
                )

        if not rows:
            return
        try:
            persisted = self.db.upsert_proxy_feedback_history(rows)
        except Exception:
            persisted = False
            logger.exception("Durable reputation write raised unexpectedly.")
        if persisted is False:
            with self.lock:
                self.pending_feedback_persist.update(pending)
            logger.warning(
                "Durable reputation write failed; re-queued {} proxy record(s).",
                len(pending),
            )

    def _flush_stats(self):
        """Periodic persistence: minute aggregates plus per-proxy reputation."""
        self._flush_feedback_buffer()
        self._persist_feedback_history()

    def _compute_baseline_score(self, measured: List[Tuple[str, Dict]]) -> float:
        """Compatibility helper: unknown quality is always the fixed prior."""
        return self._baseline_score()

    def _baseline_score(self, source: Optional[str] = None) -> float:
        """Fixed per-source initial reliability score on the public 0-100 scale."""
        return self.reliability_prior * 100.0

    def _latency_sort_key(self, stat: Dict) -> float:
        latency = self._coerce_latency(
            stat.get("avg_latency_ms"), self.max_feedback_latency_ms
        )
        return float(latency) if latency is not None else math.inf

    @staticmethod
    def _coerce_latency(
        value, max_latency_ms: int = DEFAULT_MAX_FEEDBACK_LATENCY_MS
    ) -> Optional[int]:
        """
        Return value as a usable latency under the supplied boundary, or None.

        Anything non-numeric that reaches recent_results poisons the stat
        permanently: the compatibility scorer runs over the whole pool on every
        sync, so one bad entry would raise there and stop pool refreshes for
        every source. The API validates its input, but restored backups and
        legacy files are also untrusted, so the score path never assumes.
        """
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        # Integer comparisons first: they are exact for arbitrarily large ints,
        # whereas math.isfinite() would already have raised OverflowError.
        if value < 0 or value > max_latency_ms:
            return None
        # NaN escapes both comparisons above, so it still needs the finite check.
        # A deployment can also configure an unusually large boundary; keep an
        # arbitrary-size JSON integer from overflowing math.isfinite() then.
        try:
            is_finite = math.isfinite(value)
        except OverflowError:
            return None
        if not is_finite:
            return None
        return int(value)

    @staticmethod
    def _coerce_timestamp(value, now_ts: Optional[float] = None) -> Optional[float]:
        """Return a finite Unix timestamp, clamping future clock skew to now."""
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        try:
            timestamp = float(value)
        except (OverflowError, TypeError, ValueError):
            return None
        if not math.isfinite(timestamp) or timestamp < 0:
            return None
        if now_ts is not None:
            timestamp = min(timestamp, now_ts)
        return timestamp

    def _normalize_recent_result(
        self, result, now_ts: Optional[float] = None
    ) -> Optional[List]:
        """Normalize one untrusted feedback row from a persisted backup."""
        if not isinstance(result, list) or len(result) < 3:
            return None

        timestamp = self._coerce_timestamp(result[0], now_ts)
        if timestamp is None:
            return None

        success = result[1]
        if type(success) is int and success in (0, 1):
            success = bool(success)
        if not isinstance(success, bool):
            return None

        latency = (
            None
            if result[2] is None
            else self._coerce_latency(result[2], self.max_feedback_latency_ms)
        )
        return [timestamp, success, latency]

    def _unexpired_results(self, stat: Dict) -> List:
        """Valid results in the current probation/forgiveness epoch."""
        raw_results = stat.get("recent_results", [])
        if not isinstance(raw_results, list):
            return []
        now_ts = time.time()
        max_age_seconds = self.probation_forgiveness_hours * 3600
        recent = []
        for raw_result in raw_results[-self.reliability_recent_results_limit :]:
            result = self._normalize_recent_result(raw_result, now_ts)
            if result is None:
                continue
            if now_ts - result[0] > max_age_seconds:
                continue
            recent.append(result)
        return recent

    @staticmethod
    def _feedback_ts(stat: Dict) -> float:
        ts = ProxyManager._coerce_timestamp(
            stat.get("last_feedback_ts"), now_ts=time.time()
        )
        return ts if ts is not None else 0.0

    def _truncate_stats_pool(self, source: str, stats_pool: Dict[str, Dict]) -> Dict[str, Dict]:
        """
        Cap the stats pool by evicting dead history only.

        Eviction is reputation loss, because the record does not survive it:
        _sync_and_select_top_proxies re-seeds any active proxy missing from the
        pool with _get_new_proxy_stat(), so an evicted-but-still-active proxy
        returns one cycle later as a pristine score=50 / failure_count=0
        candidate. Whatever the eviction order is, if a live proxy can be
        evicted at all then a bad record can be laundered by waiting two syncs -
        which is the failure this pool exists to prevent.

        So live proxies do not participate in the cap. It applies to dead
        history alone, oldest feedback first; that is the part which is safe to
        drop, because a dead proxy that comes back has to pass validation again
        anyway. The cap therefore bounds retained *dead* history, not total
        memory: the live half tracks however many proxies are genuinely active,
        and the pool logs a warning when that alone exceeds the configured size
        so the operator can raise it or lower max_pool_size.
        """
        max_stats_size = self.max_pool_size * self.stats_pool_max_multiplier
        if len(stats_pool) <= max_stats_size:
            return stats_pool

        live, dead = [], []
        for item in stats_pool.items():
            (live if item[0] in self.active_proxies else dead).append(item)

        room_for_dead = max_stats_size - len(live)
        if room_for_dead <= 0:
            retained = live
            logger.warning(
                f"Stats pool for source '{source}' holds {len(live)} live proxies, "
                f"at or above the configured limit of {max_stats_size} "
                f"(max_pool_size x stats_pool_max_multiplier). Dropping all "
                f"{len(dead)} dead entries and keeping every live one: evicting a "
                f"live proxy would reset its failure history on the next sync. "
                f"Raise stats_pool_max_multiplier or lower max_pool_size."
            )
        else:
            by_staleness = lambda item: (
                self._feedback_ts(item[1]),
                float(item[1].get("score", 0.0)),
            )
            retained = live + sorted(dead, key=by_staleness, reverse=True)[:room_for_dead]
            logger.info(
                f"Stats pool for source '{source}' exceeds limit "
                f"({len(stats_pool)} > {max_stats_size}). Evicting "
                f"{len(dead) - room_for_dead} dead proxies (oldest feedback first); "
                f"{len(live)} live proxies retained."
            )
        return dict(retained)

    def _update_dashboard_sources(self):
        logger.info("Refreshing dashboard sources from config and database...")
        # Use only unique sources from stats data (source_stats_by_minute).
        # This keeps dashboard source options aligned with real observable traffic.
        db_sources = self.db.get_distinct_sources()
        if db_sources is None:
            logger.warning(
                "Dashboard source refresh failed; preserving the last-known-good cache."
            )
            return False

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
        return True

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
                        target=self._flush_stats, daemon=True
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
        self._flush_stats()
        if self.stats_backup_enabled:
            self.backup_stats()  # Backup before shutdown
        if self.scheduler_thread and self.scheduler_thread.is_alive():
            self.stop_scheduler_event.set()
            self.fetch_executor.shutdown(wait=True)
            self.scheduler_thread.join(timeout=10)
            logger.info("Background scheduler stopped.")

    def _get_new_proxy_stat(
        self, source: Optional[str] = None, history: Optional[Dict] = None
    ) -> Dict:
        """
        Create a proxy stat at the fixed reliability prior.

        `history` is the proxy's persisted feedback record, when the proxies
        table has one. Seeding those counters is what stops eviction from the
        stats pool being an amnesty: a proxy that failed its way out and later
        passed validation again comes back with its record, not with the
        untried baseline it never earned.
        """
        stat = {
            "score": self._baseline_score(source),
            "quality_slow": self.reliability_prior,
            "quality_fast": self.reliability_prior,
            "quality_updated_ts": None,
            "success_count": 0,         # Total historical success
            "failure_count": 0,         # Total historical failure
            "consecutive_failures": 0,  # Diagnostic only; selection is score-based
            "recent_results": [],       # List of [timestamp, success: bool, latency_ms: int|None]
            "avg_latency_ms": None,     # Exponential moving average of latency
            "last_feedback_ts": None,   # Unix timestamp of latest feedback
            "completed_feedback_count": 0,
            "handout_count": 0,
            "trial_handout_count": 0,
            "last_handed_out_ts": None,
            "outstanding_until": 0.0,
            "retry_after_ts": 0.0,
        }
        if not history:
            return stat

        def nonnegative_int(value) -> int:
            # `type(value) is int`, not isinstance: bool subclasses int, and a
            # JSON `true` in a restored record must not read as a count of 1.
            return value if type(value) is int and value >= 0 else 0

        stat["success_count"] = nonnegative_int(history.get("success_count"))
        stat["failure_count"] = nonnegative_int(history.get("failure_count"))
        stat["completed_feedback_count"] = (
            stat["success_count"] + stat["failure_count"]
        )
        now_ts = time.time()
        stat["last_feedback_ts"] = self._coerce_timestamp(
            history.get("last_feedback_ts"), now_ts
        )
        if stat["success_count"] or stat["failure_count"]:
            total = stat["completed_feedback_count"]
            if (
                stat["last_feedback_ts"] is not None
                and now_ts - stat["last_feedback_ts"]
                <= self.probation_forgiveness_hours * 3600
            ):
                stat["trial_handout_count"] = min(
                    total, self.probation_attempts + self.retry_attempts
                )
                stat["handout_count"] = stat["trial_handout_count"]
                if stat["trial_handout_count"] >= self.probation_attempts:
                    stat["retry_after_ts"] = (
                        stat["last_feedback_ts"] + self.retry_delay_s
                    )
            self._seed_reliability_from_history(stat, now_ts)
        return stat

    def backup_stats(self) -> Dict:
        """Backup source_stats to a JSON file."""
        with self.backup_lock:
            return self._backup_stats_locked()

    def _backup_stats_locked(self) -> Dict:
        with self.lock:
            # Snapshot the destination together with the data, and before the
            # deep copy rather than after it - the copy is the slow part, and
            # reload_sources() can move stats_backup_path at any point. The
            # write is a mkstemp-next-to-target plus os.replace, so re-reading
            # the attribute later would create the temp file beside the old path
            # and then rename it onto the new one: that fails across directories
            # and leaves neither file written.
            backup_path = self.stats_backup_path
            # Deep copy: a shallow dict() still shares every stat dict and its
            # recent_results list with the live pool, which json.dump then walks
            # outside the lock while feedback threads mutate them.
            source_stats_snapshot = copy.deepcopy(self.source_stats)
            for source, outage_state in self.outage_states.items():
                for proxy_url, committed_stat in outage_state.get(
                    "protected_stats", {}
                ).items():
                    if proxy_url in source_stats_snapshot.get(source, {}):
                        source_stats_snapshot[source][proxy_url] = copy.deepcopy(
                            committed_stat
                        )
            stats_snapshot = {
                "scoring_version": SCORING_VERSION,
                "timestamp": datetime.now().isoformat(),
                "source_stats": source_stats_snapshot,
            }

        try:
            # Create directory if it doesn't exist
            backup_path.parent.mkdir(parents=True, exist_ok=True)

            # Atomic write: a crash or SIGKILL mid-dump would otherwise leave a
            # truncated file, and restore_stats() would drop all scoring state.
            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=str(backup_path.parent),
                prefix=backup_path.name + ".",
                suffix=".tmp",
            )
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                    json.dump(stats_snapshot, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, backup_path)
            except Exception:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise

            total_proxies = sum(len(proxies) for proxies in stats_snapshot["source_stats"].values())
            logger.info(
                f"Stats backup completed: {len(stats_snapshot['source_stats'])} sources, "
                f"{total_proxies} proxy stats saved to {backup_path}"
            )
            return {
                "status": "success",
                "path": str(backup_path),
                "sources": len(stats_snapshot["source_stats"]),
                "total_proxies": total_proxies,
            }
        except Exception as e:
            logger.error(f"Failed to backup stats: {e}")
            return {"status": "error", "message": str(e)}

    def restore_stats(self) -> Dict:
        """Restore source_stats from JSON file on startup."""
        if self.restore_mode != "normal":
            logger.info(
                "Restore mode '{}': skipping JSON scoring restore; isolated state path is {}.",
                self.restore_mode,
                self.stats_backup_path,
            )
            return {
                "status": "skipped",
                "mode": self.restore_mode,
                "path": str(self.stats_backup_path),
            }

        with self.lock:
            backup_path = self.stats_backup_path
            predefined_sources = set(self.predefined_sources)

        if not backup_path.exists():
            # WARNING, not INFO: every restart that hits this path silently
            # discards the entire scoring history.
            logger.warning(
                "No stats backup found at {} - starting with fresh scores. "
                "All accumulated proxy scoring history is lost. Check "
                "[backup] stats_backup_path if this is unexpected.",
                backup_path,
            )
            return {
                "status": "skipped",
                "message": f"No backup file found at {backup_path}",
                "path": str(backup_path),
            }

        try:
            # Log file info before loading
            file_size = backup_path.stat().st_size
            logger.info(
                f"Loading stats backup from: {backup_path} "
                f"(size: {file_size / 1024:.2f} KB)"
            )

            with open(backup_path, "r", encoding="utf-8") as f:
                snapshot = json.load(f)

            if not isinstance(snapshot, dict):
                raise ValueError("Backup root must be a JSON object")
            source_stats = snapshot.get("source_stats", {})
            if not isinstance(source_stats, dict):
                raise ValueError("Backup source_stats must be a JSON object")

            stored_scoring_version = snapshot.get("scoring_version")
            trust_derived = stored_scoring_version == SCORING_VERSION
            if not trust_derived:
                logger.warning(
                    "Scoring version mismatch (stored={}, current={}); replaying raw recent results.",
                    stored_scoring_version,
                    SCORING_VERSION,
                )

            # Log backup metadata
            backup_time = snapshot.get("timestamp", "unknown")
            total_sources_in_file = len(source_stats)
            logger.info(
                "Backup file parsed successfully. Timestamp: {}, Sources in file: {}",
                backup_time,
                total_sources_in_file,
            )

            # Build and validate the complete replacement before taking the
            # manager lock. If a later source is structurally invalid, no
            # earlier source may have been partially installed.
            restored_stats = {}
            restored_sources = 0
            restored_proxies = 0
            skipped_sources = []
            restore_summaries = []
            for source, proxies in source_stats.items():
                if source not in predefined_sources:
                    skipped_sources.append(source)
                    logger.debug(
                        f"  [SKIPPED] Source '{source}': not in predefined_sources"
                    )
                    continue
                if not isinstance(proxies, dict):
                    raise ValueError(
                        f"Backup source '{source}' must map proxy URLs to stats"
                    )

                migrated_proxies = {}
                legacy_count = 0
                for proxy_url, raw_stat in proxies.items():
                    if not isinstance(raw_stat, dict):
                        raise ValueError(
                            f"Backup stat for '{source}'/'{proxy_url}' must be an object"
                        )
                    if "recent_results" not in raw_stat:
                        legacy_count += 1
                    migrated_proxies[proxy_url] = self._migrate_legacy_stat(
                        copy.deepcopy(raw_stat), trust_derived=trust_derived
                    )

                restored_stats[source] = migrated_proxies
                restored_sources += 1
                proxy_count = len(migrated_proxies)
                restored_proxies += proxy_count
                restore_summaries.append((source, proxy_count, legacy_count))

            with self.lock:
                self.source_stats.update(restored_stats)

            for source, proxy_count, legacy_count in restore_summaries:
                if legacy_count > 0:
                    logger.info(
                        f"  [RESTORED] Source '{source}': {proxy_count} proxies "
                        f"({legacy_count} normalized to online reliability format)"
                    )
                else:
                    logger.info(
                        f"  [RESTORED] Source '{source}': {proxy_count} proxies loaded"
                    )

            if skipped_sources:
                logger.warning(
                    f"Skipped {len(skipped_sources)} sources not in "
                    f"predefined_sources: {skipped_sources}"
                )

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
        """Route one request through a shared adaptive exploration budget."""
        source = self._get_source_or_default(source)

        with self.lock:
            proxy_pools = self.available_proxies.get(source)
            if not proxy_pools:
                logger.warning(f"No proxy pools defined for source '{source}'.")
                return None

            top_tier = proxy_pools.get("top_tier", [])
            bottom_tier = proxy_pools.get("bottom_tier", [])
            stats_pool = self.source_stats.get(source, {})
            now_ts = time.time()

            for stat in stats_pool.values():
                self._refresh_trial_epoch(stat, now_ts)

            exploit_pool = top_tier or bottom_tier
            exploit_candidates = self._filter_cooldown_candidates(
                source,
                [
                    proxy_url
                    for proxy_url in exploit_pool
                    if proxy_url not in stats_pool
                    or self._is_qualified(stats_pool[proxy_url])
                ],
            )
            exploration_groups = self._exploration_candidate_groups(source, now_ts)
            has_exploration = any(exploration_groups.values())
            exploration_ratio = self._compute_exploration_ratio(source)
            if exploit_candidates:
                self.cold_start_fallback_logged.discard(source)

            explore = has_exploration and (
                not exploit_candidates or random.random() < exploration_ratio
            )
            if explore:
                selected = self._select_exploration_candidate(exploration_groups)
                if (
                    not exploit_candidates
                    and source not in self.cold_start_fallback_logged
                ):
                    logger.warning(
                        "Source '{}' has no qualified exploit candidate; serving trial "
                        "traffic through the explicit cold-start fallback above the {:.1f}% budget.",
                        source,
                        exploration_ratio * 100,
                    )
                    self.cold_start_fallback_logged.add(source)
                self._mark_proxy_handed_out(source, selected, now_ts)
                return selected

            if not exploit_candidates:
                logger.warning(
                    "No eligible proxy for source '{}' after qualification, retry, "
                    "cooldown, and in-flight filtering.",
                    source,
                )
                return None

            if self.selection_strategy == "uniform":
                selected = random.choice(exploit_candidates)
            elif self.selection_strategy == "tiered":
                selected = self._select_from_tiers(
                    source, top_tier, bottom_tier, exploit_candidates
                )
            elif self.selection_strategy == "softmax":
                selected = self._select_weighted_by_score(
                    source, exploit_candidates, softmax=True
                )
            else:
                selected = self._select_weighted_by_score(source, exploit_candidates)

            self._mark_proxy_handed_out(source, selected, now_ts)
            return selected

    def _compute_exploration_ratio(self, source: str) -> float:
        stats_pool = self.source_stats.get(source, {})
        qualified = sum(
            1
            for proxy_url, stat in stats_pool.items()
            if proxy_url in self.active_proxies and self._is_qualified(stat)
        )
        progress = min(1.0, qualified / self.exploration_target_qualified)
        return self.exploration_max_ratio - (
            self.exploration_max_ratio - self.exploration_min_ratio
        ) * progress

    def _is_qualified(self, stat: Dict) -> bool:
        return (
            len(self._unexpired_results(stat)) >= self.qualification_min_results
            and float(stat.get("score", self._baseline_score()))
            > self._baseline_score()
        )

    def _refresh_trial_epoch(self, stat: Dict, now_ts: float):
        outstanding = self._coerce_timestamp(stat.get("outstanding_until")) or 0.0
        if outstanding <= now_ts:
            stat["outstanding_until"] = 0.0

        last_feedback = self._coerce_timestamp(stat.get("last_feedback_ts")) or 0.0
        last_handout = self._coerce_timestamp(stat.get("last_handed_out_ts")) or 0.0
        anchor = max(last_feedback, last_handout)
        if anchor and now_ts - anchor > self.probation_forgiveness_hours * 3600:
            stat["trial_handout_count"] = 0
            stat["retry_after_ts"] = 0.0
            stat["outstanding_until"] = 0.0

    def _exploration_candidate_groups(
        self, source: str, now_ts: Optional[float] = None
    ) -> Dict[str, List[str]]:
        now_ts = time.time() if now_ts is None else now_ts
        groups = {"discovery": [], "probation": [], "retry": []}
        stats_pool = self.source_stats.get(source, {})
        max_trials = self.probation_attempts + self.retry_attempts
        for proxy_url, stat in stats_pool.items():
            if proxy_url not in self.active_proxies:
                continue
            self._refresh_trial_epoch(stat, now_ts)
            if self._is_qualified(stat):
                continue
            if (self._coerce_timestamp(stat.get("outstanding_until")) or 0.0) > now_ts:
                continue
            if not self._filter_cooldown_candidates(source, [proxy_url]):
                continue
            trial_handouts = int(stat.get("trial_handout_count", 0) or 0)
            completed = len(self._unexpired_results(stat))
            if trial_handouts == 0 and completed == 0:
                groups["discovery"].append(proxy_url)
            elif trial_handouts < self.probation_attempts:
                groups["probation"].append(proxy_url)
            elif trial_handouts < max_trials:
                retry_after = self._coerce_timestamp(stat.get("retry_after_ts")) or 0.0
                if retry_after <= now_ts:
                    groups["retry"].append(proxy_url)
        return groups

    def _select_exploration_candidate(self, groups: Dict[str, List[str]]) -> str:
        discovery = groups["discovery"]
        probation_retry = groups["probation"] + groups["retry"]
        if discovery and probation_retry:
            candidates = (
                discovery
                if random.random() < self.exploration_discovery_share
                else probation_retry
            )
        else:
            candidates = discovery or probation_retry
        return random.choice(candidates)

    def _maybe_select_exploration_candidate(self, source: str) -> Optional[str]:
        """Compatibility helper for callers that only want an exploration pick."""
        groups = self._exploration_candidate_groups(source)
        if not any(groups.values()):
            return None
        if random.random() >= self._compute_exploration_ratio(source):
            return None
        return self._select_exploration_candidate(groups)

    def _mark_proxy_handed_out(self, source: str, proxy_url: str, now_ts: float):
        self.proxy_last_handed_out_ts[source][proxy_url] = now_ts
        stat = self.source_stats.get(source, {}).get(proxy_url)
        if stat is None:
            return
        stat["handout_count"] = int(stat.get("handout_count", 0) or 0) + 1
        if not self._is_qualified(stat):
            stat["trial_handout_count"] = int(
                stat.get("trial_handout_count", 0) or 0
            ) + 1
        stat["last_handed_out_ts"] = now_ts
        stat["outstanding_until"] = now_ts + self.proxy_inflight_timeout_s
        if stat["trial_handout_count"] >= self.probation_attempts:
            stat["retry_after_ts"] = now_ts + self.retry_delay_s

    def _filter_cooldown_candidates(self, source: str, proxy_urls: List[str]) -> List[str]:
        now = time.time()
        cooldown_s = self.proxy_cooldown_ms / 1000
        last_handed_out = self.proxy_last_handed_out_ts.get(source, {})
        stats_pool = self.source_stats.get(source, {})
        return [
            proxy_url
            for proxy_url in proxy_urls
            if now - last_handed_out.get(proxy_url, 0) >= cooldown_s
            and (
                self._coerce_timestamp(
                    stats_pool.get(proxy_url, {}).get("outstanding_until")
                )
                or 0.0
            )
            <= now
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
        default_score = self._baseline_score(source)
        max_score = max(
            (float(stats.get(url, {}).get("score", default_score)) for url in candidates),
            default=default_score,
        )
        weights = []
        for proxy_url in candidates:
            score = float(stats.get(proxy_url, {}).get("score", default_score))
            if softmax:
                weights.append(math.exp((score - max_score) / self.softmax_temperature))
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

    def _migrate_legacy_stat(
        self, stat: Dict, trust_derived: bool = True
    ) -> Dict:
        """Normalize persisted input and install scoring-version-2 state."""
        if not isinstance(stat, dict):
            raise ValueError("Proxy stat must be a mapping")

        def nonnegative_int(value) -> int:
            return value if type(value) is int and value >= 0 else 0

        success_count = nonnegative_int(stat.get("success_count", 0))
        failure_count = nonnegative_int(stat.get("failure_count", 0))
        stat["success_count"] = success_count
        stat["failure_count"] = failure_count
        stat["consecutive_failures"] = nonnegative_int(
            stat.get("consecutive_failures", 0)
        )

        now_ts = time.time()
        raw_results = stat.get("recent_results", [])
        if not isinstance(raw_results, list):
            raw_results = []
        normalized_results = []
        for raw_result in raw_results[-self.reliability_recent_results_limit :]:
            result = self._normalize_recent_result(raw_result, now_ts)
            if result is not None:
                normalized_results.append(result)
        stat["recent_results"] = sorted(normalized_results, key=lambda result: result[0])

        stat["avg_latency_ms"] = self._coerce_latency(
            stat.get("avg_latency_ms"), self.max_feedback_latency_ms
        )

        last_feedback_ts = self._coerce_timestamp(
            stat.get("last_feedback_ts"), now_ts
        )
        if last_feedback_ts is None and normalized_results:
            last_feedback_ts = max(result[0] for result in normalized_results)
        stat["last_feedback_ts"] = last_feedback_ts

        total = success_count + failure_count
        stat["completed_feedback_count"] = total
        stat["handout_count"] = nonnegative_int(stat.get("handout_count", 0))
        stat["trial_handout_count"] = nonnegative_int(
            stat.get("trial_handout_count", 0)
        )
        stat["last_handed_out_ts"] = self._coerce_timestamp(
            stat.get("last_handed_out_ts"), now_ts
        )
        stat["outstanding_until"] = (
            self._coerce_timestamp(stat.get("outstanding_until")) or 0.0
        )
        stat["retry_after_ts"] = (
            self._coerce_timestamp(stat.get("retry_after_ts")) or 0.0
        )

        current_results = self._unexpired_results(stat)
        if stat["trial_handout_count"] == 0 and current_results:
            stat["trial_handout_count"] = min(
                len(current_results), self.probation_attempts + self.retry_attempts
            )
        elif stat["trial_handout_count"] == 0 and total and last_feedback_ts:
            if now_ts - last_feedback_ts <= self.probation_forgiveness_hours * 3600:
                stat["trial_handout_count"] = min(
                    total, self.probation_attempts + self.retry_attempts
                )
        stat["handout_count"] = max(
            stat["handout_count"], stat["trial_handout_count"]
        )

        slow = self._coerce_probability(stat.get("quality_slow"))
        fast = self._coerce_probability(stat.get("quality_fast"))
        quality_ts = self._coerce_timestamp(stat.get("quality_updated_ts"), now_ts)
        if (
            trust_derived
            and slow is not None
            and fast is not None
            and quality_ts is not None
        ):
            stat["quality_slow"] = slow
            stat["quality_fast"] = fast
            stat["quality_updated_ts"] = quality_ts
            self._age_reliability_state(stat, now_ts)
        elif normalized_results:
            self._replay_reliability_results(stat, normalized_results, now_ts)
        elif total:
            self._seed_reliability_from_history(stat, now_ts)
        else:
            stat["quality_slow"] = self.reliability_prior
            stat["quality_fast"] = self.reliability_prior
            stat["quality_updated_ts"] = None
            stat["score"] = self._baseline_score()

        return stat

    @staticmethod
    def _coerce_probability(value) -> Optional[float]:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        try:
            result = float(value)
        except (OverflowError, TypeError, ValueError):
            return None
        return result if math.isfinite(result) and 0.0 <= result <= 1.0 else None

    def _age_reliability_state(self, stat: Dict, target_ts: float):
        slow = self._coerce_probability(stat.get("quality_slow"))
        fast = self._coerce_probability(stat.get("quality_fast"))
        if slow is None or fast is None:
            slow = fast = self.reliability_prior
        updated_ts = self._coerce_timestamp(stat.get("quality_updated_ts"), target_ts)
        if updated_ts is not None:
            elapsed = max(0.0, target_ts - updated_ts)
            half_life_s = self.reliability_decay_half_life_hours * 3600
            decay = math.pow(0.5, elapsed / half_life_s)
            slow = self.reliability_prior + (slow - self.reliability_prior) * decay
            fast = self.reliability_prior + (fast - self.reliability_prior) * decay
        stat["quality_slow"] = min(1.0, max(0.0, slow))
        stat["quality_fast"] = min(1.0, max(0.0, fast))
        stat["quality_updated_ts"] = target_ts
        stat["score"] = 100.0 * min(stat["quality_slow"], stat["quality_fast"])

    def _update_reliability_state(self, stat: Dict, outcome: bool, event_ts: float):
        self._age_reliability_state(stat, event_ts)
        numeric_outcome = 1.0 if outcome else 0.0
        stat["quality_slow"] = (
            (1.0 - self.reliability_slow_alpha) * stat["quality_slow"]
            + self.reliability_slow_alpha * numeric_outcome
        )
        stat["quality_fast"] = (
            (1.0 - self.reliability_fast_alpha) * stat["quality_fast"]
            + self.reliability_fast_alpha * numeric_outcome
        )
        stat["quality_updated_ts"] = event_ts
        stat["score"] = 100.0 * min(stat["quality_slow"], stat["quality_fast"])

    def _replay_reliability_results(
        self, stat: Dict, results: List[List], now_ts: float
    ):
        stat["quality_slow"] = self.reliability_prior
        stat["quality_fast"] = self.reliability_prior
        stat["quality_updated_ts"] = None
        for event_ts, outcome, _ in sorted(results, key=lambda result: result[0]):
            self._update_reliability_state(stat, bool(outcome), event_ts)
        self._age_reliability_state(stat, now_ts)

    def _seed_reliability_from_history(self, stat: Dict, now_ts: float):
        successes = int(stat.get("success_count", 0) or 0)
        failures = int(stat.get("failure_count", 0) or 0)
        total = successes + failures
        if total <= 0:
            seeded = self.reliability_prior
        else:
            prior_weight = self.reliability_history_prior_weight
            seeded = (
                successes + prior_weight * self.reliability_prior
            ) / (total + prior_weight)
        stat["quality_slow"] = seeded
        stat["quality_fast"] = seeded
        stat["quality_updated_ts"] = self._coerce_timestamp(
            stat.get("last_feedback_ts"), now_ts
        )
        if stat["quality_updated_ts"] is None:
            stat["quality_slow"] = self.reliability_prior
            stat["quality_fast"] = self.reliability_prior
        self._age_reliability_state(stat, now_ts)

    def _calculate_elo_score(self, stat: Dict, source: str = None) -> float:
        """Compatibility name for the online reliability score."""
        slow = self._coerce_probability(stat.get("quality_slow"))
        fast = self._coerce_probability(stat.get("quality_fast"))
        quality_ts = self._coerce_timestamp(stat.get("quality_updated_ts"))
        if slow is None or fast is None or quality_ts is None:
            self._migrate_legacy_stat(stat, trust_derived=False)
        else:
            self._age_reliability_state(stat, time.time())
        return float(stat["score"])

    def _outage_state(self, source: str) -> Dict:
        return self.outage_states.setdefault(
            source,
            {
                "active": False,
                "previous_window_healthy": False,
                "observations": [],
                "protected_stats": {},
                "paused_updates": 0,
                "completed_windows": 0,
                "last_transition_ts": None,
            },
        )

    def _observe_source_outage_locked(
        self, source: str, proxy_url: str, is_success: bool, current_timestamp: float
    ) -> bool:
        if (
            not self.outage_guard_enabled
            or proxy_url not in self.source_stats.get(source, {})
        ):
            return False

        state = self._outage_state(source)
        if (
            not state["active"]
            and state["previous_window_healthy"]
            and proxy_url not in state["protected_stats"]
        ):
            state["protected_stats"][proxy_url] = copy.deepcopy(
                self.source_stats[source][proxy_url]
            )
        state["observations"].append((proxy_url, is_success))

        if len(state["observations"]) < self.outage_window_size:
            if state["active"]:
                state["paused_updates"] += 1
            return bool(state["active"])

        window = state["observations"][: self.outage_window_size]
        distinct = len({url for url, _ in window})
        success_ratio = sum(1 for _, ok in window if ok) / len(window)
        failure_ratio = 1.0 - success_ratio
        state["completed_windows"] += 1

        if state["active"]:
            state["paused_updates"] += 1
            if (
                distinct >= self.outage_min_distinct_proxies
                and success_ratio >= self.outage_recovery_success_ratio
            ):
                state["active"] = False
                state["previous_window_healthy"] = True
                state["last_transition_ts"] = current_timestamp
                logger.warning(
                    "Source outage guard recovered for '{}': success_ratio={:.3f}, distinct_proxies={}.",
                    source,
                    success_ratio,
                    distinct,
                )
            state["observations"] = []
            state["protected_stats"] = {}
            return True

        broad_failure = (
            state["previous_window_healthy"]
            and distinct >= self.outage_min_distinct_proxies
            and failure_ratio >= self.outage_failure_ratio
        )
        if broad_failure:
            for protected_url, snapshot in state["protected_stats"].items():
                if protected_url in self.source_stats.get(source, {}):
                    self.source_stats[source][protected_url] = snapshot
            state["active"] = True
            state["paused_updates"] += len(window)
            state["last_transition_ts"] = current_timestamp
            logger.error(
                "Source outage guard activated for '{}': failure_ratio={:.3f}, distinct_proxies={}; rolled back {} tentative reputation update(s).",
                source,
                failure_ratio,
                distinct,
                len(state["protected_stats"]),
            )
            paused = True
        else:
            state["previous_window_healthy"] = (
                distinct >= self.outage_min_distinct_proxies
                and success_ratio >= self.outage_healthy_success_ratio
            )
            paused = False
        state["observations"] = []
        state["protected_stats"] = {}
        return paused

    def process_feedback(
        self,
        source: str,
        proxy_url: str,
        status_code: int,
        response_time_ms: Optional[int] = None,
        failure_kind: Optional[str] = None,
    ):
        """
        Process feedback for a proxy request with online reliability scoring.
        
        Updates:
        - Adds result to the bounded replay window (recent_results)
        - Updates exponential moving average of latency
        - Updates the slow and fast reliability estimators
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

            reported_stat = self.source_stats.get(source, {}).get(proxy_url)
            if reported_stat is not None:
                # Feedback completes the outstanding allocation even when an
                # outage guard pauses the reputation mutation itself.
                reported_stat["outstanding_until"] = 0.0

            source_reputation_paused = self._observe_source_outage_locked(
                source, proxy_url, is_success, current_timestamp
            )

            target_sources = [source]
            if not is_success and failure_kind == "dead":
                target_sources = [
                    candidate_source
                    for candidate_source, stats in self.source_stats.items()
                    if proxy_url in stats
                ] or [source]

            for target_source in target_sources:
                if target_source == source and source_reputation_paused:
                    continue
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
        stat = self._migrate_legacy_stat(stat)
        
        # Update historical counters
        if is_success:
            stat["success_count"] += 1
            stat["consecutive_failures"] = 0
            
            # Update exponential moving average of latency for observability.
            latency = self._coerce_latency(
                response_time_ms, self.max_feedback_latency_ms
            )
            if latency is not None:
                alpha = self.avg_latency_alpha
                previous = self._coerce_latency(
                    stat.get("avg_latency_ms"), self.max_feedback_latency_ms
                )
                if previous is None:
                    stat["avg_latency_ms"] = latency
                else:
                    stat["avg_latency_ms"] = alpha * latency + (1 - alpha) * previous
        else:
            stat["failure_count"] += 1
            stat["consecutive_failures"] += 1
        
        # Add to the bounded raw replay window.
        latency_for_log = self._coerce_latency(
            response_time_ms, self.max_feedback_latency_ms
        )
        stat["recent_results"].append(
            [
                current_timestamp,
                is_success,
                self._coerce_latency(
                    response_time_ms, self.max_feedback_latency_ms
                ),
            ]
        )
        if len(stat["recent_results"]) > self.reliability_recent_results_limit:
            stat["recent_results"] = stat["recent_results"][
                -self.reliability_recent_results_limit:
            ]
        stat["last_feedback_ts"] = current_timestamp
        stat["completed_feedback_count"] = (
            stat["success_count"] + stat["failure_count"]
        )
        stat["trial_handout_count"] = max(
            int(stat.get("trial_handout_count", 0) or 0),
            min(
                len(self._unexpired_results(stat)),
                self.probation_attempts + self.retry_attempts,
            ),
        )
        # Queue the updated counters for write-back, so this proxy's record
        # outlives its entry in the in-memory pool.
        if self.durable_reputation_enabled:
            self.pending_feedback_persist.add(proxy_url)

        old_score = stat["score"]
        self._update_reliability_state(stat, is_success, current_timestamp)
        if (
            not self._is_qualified(stat)
            and stat["trial_handout_count"] >= self.probation_attempts
        ):
            stat["retry_after_ts"] = current_timestamp + self.retry_delay_s
        
        response_time_str = f"{latency_for_log:.0f}" if latency_for_log is not None else "N/A"
        logger.debug(
            f"Reliability Score: {source:<15} | {proxy_url:<30} | "
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
