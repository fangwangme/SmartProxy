# -*- coding: utf-8 -*-
import os
import configparser
import copy
import ipaddress
import json
import math
import random
import secrets
import socket
import tempfile
import threading
import time
import uuid
from bisect import bisect_right
import asyncio
import subprocess
import aiohttp
from typing import Dict, List, NamedTuple, Optional, Set, Tuple
from collections import defaultdict, OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from src.utils.logger import logger
from src.database.db import DatabaseManager, DatabaseWriteError

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

# Keys every live stat must carry before the feedback path may mutate it.
REQUIRED_STAT_KEYS = frozenset(
    {
        "score",
        "quality_slow",
        "quality_fast",
        "success_count",
        "failure_count",
        "recent_results",
        "trial_handout_count",
    }
)
MIGRATABLE_STAT_KEYS = frozenset(
    {
        "score",
        "quality_slow",
        "quality_fast",
        "quality_updated_ts",
        "success_count",
        "failure_count",
        "recent_results",
        "avg_latency_ms",
        "last_feedback_ts",
        "handout_count",
        "trial_handout_count",
        "last_handed_out_ts",
        "inflight",
        "retry_after_ts",
    }
)
RESTORE_MODES = frozenset({"normal", "no-restore"})

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

def nonnegative_int(value) -> int:
    """
    A stored counter, or 0.

    `type(value) is int`, not isinstance: bool subclasses int, and a JSON
    `true` in a restored record must not read as a count of 1.
    """
    return value if type(value) is int and value >= 0 else 0


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


class ValidationBatchResult(NamedTuple):
    success_proxies: List[Dict]
    failure_proxy_ids: List[int]
    metadata: Dict[str, object]


class ProxyManager:
    """Manages the proxy lifecycle, state, and business logic."""

    def __init__(self, config_path, restore_mode: str = "normal"):
        if restore_mode not in RESTORE_MODES:
            raise ValueError(f"Unknown restore mode: {restore_mode}")
        self.restore_mode = restore_mode
        self.config_path = config_path
        self.config = configparser.ConfigParser()
        self.config.read(config_path, encoding="utf-8")

        # Use RLock for reentrant locking (allows same thread to acquire lock multiple times)
        self.lock = threading.RLock()

        self.active_proxies: Set[str] = set()
        self.source_stats: Dict[str, Dict[str, Dict]] = {}
        self.available_proxies: Dict[str, Dict[str, List[str]]] = (
            {}
        )  # MODIFIED: Structure for tiers
        self.premium_proxies: List[str] = []  # High-quality proxies for Playwright
        self.premium_sources: Dict[str, str] = {}
        self.proxy_last_handed_out_ts: Dict[str, Dict[str, float]] = defaultdict(dict)
        self.outage_states: Dict[str, Dict] = {}
        # source -> ready-to-serve plan; see _build_serving_plan().
        self.serving_plans: Dict[str, Dict] = {}
        self.cold_start_fallback_logged: Set[str] = set()
        self.plan_refreshing: Set[str] = set()
        self.allocations: Dict[str, Dict] = {}
        self.allocations_by_proxy: Dict[
            Tuple[str, str], Dict[str, None]
        ] = defaultdict(dict)
        self.completed_allocations: OrderedDict[str, Dict] = OrderedDict()
        self.accepted_feedback_success_total = 0
        self.accepted_feedback_failure_total = 0
        self.legacy_feedback_total = 0
        self.rejected_feedback_total = defaultdict(int)
        self.validation_target_failures = defaultdict(int)
        self.last_validation_success_ts: Optional[float] = None
        self.last_flush_success_ts: Optional[float] = None
        self.last_validation_quorum_healthy = False
        self.last_validation_target_health: List[bool] = []
        self.last_backup_duration_s = 0.0
        self.last_manager_lock_hold_s = 0.0
        self.last_plan_refresh_duration_s = 0.0

        # Serialises the whole snapshot -> dump -> fsync -> replace sequence.
        # self.lock only guards the snapshot, so two concurrent backups could
        # interleave and let an older snapshot land last.
        self.backup_lock = threading.Lock()
        self.feedback_flush_lock = threading.Lock()

        self.feedback_buffer = defaultdict(
            lambda: defaultdict(lambda: defaultdict(int))
        )
        self.feedback_flush_pending: Optional[Tuple[str, List[Tuple]]] = None

        self.dashboard_sources: Set[str] = set()
        self.last_source_refresh_time = 0

        self._load_config()
        if self.allow_legacy_feedback:
            logger.warning(
                "Legacy feedback compatibility is enabled; feedback without an "
                "allocation_id cannot provide exact idempotency."
            )
        # Configuration is fully parsed and semantically validated before a
        # database pool is opened or any active manager state is published.
        self.db = DatabaseManager(self.config)
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
        self.background_executor = ThreadPoolExecutor(
            max_workers=self.background_workers, thread_name_prefix="SmartProxyTask"
        )
        self.background_futures: Set = set()
        self.accepting_background_tasks = True
        self.is_validating = False
        self.debug_mode = False  # Set via command line --debug flag

    def _cfg_float(
        self,
        section: str,
        option: str,
        fallback: float,
        low: float = None,
        high: float = None,
    ) -> float:
        """Read a float tunable and reject values outside [low, high]."""
        value = self.config.getfloat(section, option, fallback=fallback)
        if not math.isfinite(value):
            raise ValueError(f"[{section}] {option} must be finite")
        if low is not None and value < low:
            raise ValueError(f"[{section}] {option} must be >= {low}")
        if high is not None and value > high:
            raise ValueError(f"[{section}] {option} must be <= {high}")
        return value

    def _cfg_int(
        self,
        section: str,
        option: str,
        fallback: int,
        low: int = None,
        high: int = None,
    ) -> int:
        """Read an int tunable and reject values outside [low, high]."""
        value = self.config.getint(section, option, fallback=fallback)
        if low is not None and value < low:
            raise ValueError(f"[{section}] {option} must be >= {low}")
        if high is not None and value > high:
            raise ValueError(f"[{section}] {option} must be <= {high}")
        return value

    def _load_config(self):
        self.server_port = self._cfg_int("server", "port", 6942, low=1, high=65535)
        self.production_threads = self._cfg_int(
            "server", "production_threads", 8, low=1, high=256
        )
        self.shutdown_deadline_s = self._cfg_float(
            "server", "shutdown_deadline_seconds", 20.0, low=1.0, high=300.0
        )
        self.readiness_min_usable_pool = self._cfg_int(
            "server", "readiness_min_usable_pool", 1, low=0
        )
        self.readiness_validation_max_age_s = self._cfg_float(
            "server", "readiness_validation_max_age_seconds", 600.0, low=1.0
        )
        self.readiness_flush_max_age_s = self._cfg_float(
            "server", "readiness_flush_max_age_seconds", 180.0, low=1.0
        )
        self.background_workers = self._cfg_int(
            "server", "background_workers", 8, low=1, high=128
        )
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
        for configured_ip in self.allowed_ips + self.trusted_proxy_ips:
            try:
                ipaddress.ip_address(configured_ip)
            except ValueError as error:
                raise ValueError(
                    f"Configured client/trusted proxy address is invalid: {configured_ip!r}"
                ) from error
        self.allowed_ips = [str(ipaddress.ip_address(ip)) for ip in self.allowed_ips]
        self.trusted_proxy_ips = [
            str(ipaddress.ip_address(ip)) for ip in self.trusted_proxy_ips
        ]

        database_min = self._cfg_int("database", "min_connections", 2, low=1)
        database_max = self._cfg_int("database", "max_connections", 50, low=1)
        if database_min > database_max:
            raise ValueError(
                "[database] min_connections must not exceed max_connections"
            )
        self._cfg_int("database", "write_max_retries", 3, low=0, high=20)
        self._cfg_int("database", "write_retry_base_ms", 25, low=0, high=60000)

        self.validation_workers = self._cfg_int(
            "validator", "validation_workers", 100, low=1, high=10000
        )
        self.validation_timeout_s = self._cfg_float(
            "validator", "validation_timeout_s", 10.0, low=0.001
        )
        legacy_validation_target = self.config.get(
            "validator", "validation_target", fallback="http://httpbin.org/get"
        )
        targets_str = self.config.get("validator", "validation_targets", fallback="")
        self.validation_targets = [
            target.strip() for target in targets_str.split(",") if target.strip()
        ] or [legacy_validation_target]
        if len(set(self.validation_targets)) != len(self.validation_targets):
            raise ValueError("[validator] validation_targets must be independent and unique")
        for target in self.validation_targets:
            parsed_target = urlparse(target)
            if parsed_target.scheme not in {"http", "https"} or not parsed_target.netloc:
                raise ValueError(
                    "[validator] validation targets must be absolute HTTP(S) URLs"
                )
        self.validation_success_threshold = self._cfg_int(
            "validator",
            "validation_success_threshold",
            len(self.validation_targets),
            low=1,
        )
        if self.validation_success_threshold > len(self.validation_targets):
            raise ValueError(
                "[validator] validation_success_threshold must not exceed validation target count"
            )
        self.validation_batch_limit = self._cfg_int(
            "validator", "validation_batch_limit", 2000, low=1
        )
        # Share of validation_batch_limit reserved for never-validated proxies.
        # The rest goes to re-validating proxies that are currently alive, so a
        # flood of freshly fetched proxies cannot starve the liveness re-checks.
        self.validation_new_proxy_ratio = self._cfg_float(
            "validator",
            "validation_new_proxy_ratio",
            0.5,
            low=0.0,
            high=1.0,
        )
        self.validation_supplement_threshold = self._cfg_int(
            "validator", "validation_supplement_threshold", 1000, low=0
        )
        if self.validation_supplement_threshold > self.validation_batch_limit:
            raise ValueError(
                "[validator] validation_supplement_threshold must not exceed validation_batch_limit"
            )
        self.validation_window_minutes = self._cfg_int(
            "validator", "validation_window_minutes", 30, low=1
        )
        self.max_validations_per_window = self._cfg_int(
            "validator", "max_validations_per_window", 5, low=1
        )
        self.validation_target_min_samples = self._cfg_int(
            "validator", "validation_target_min_samples", 1, low=1
        )
        if self.validation_target_min_samples > self.validation_batch_limit:
            raise ValueError(
                "[validator] validation_target_min_samples must not exceed validation_batch_limit"
            )
        self.validation_interval_s = self._cfg_float(
            "scheduler", "validation_interval_seconds", 60.0, low=0.001
        )
        self.stats_flush_interval_s = self._cfg_float(
            "scheduler", "stats_flush_interval_seconds", 60.0, low=0.001
        )
        self.source_refresh_interval_s = self._cfg_float(
            "scheduler", "source_refresh_interval_seconds", 300.0, low=0.001
        )
        sources_str = self.config.get(
            "sources", "predefined_sources", fallback="default"
        )
        self.predefined_sources = {
            s.strip() for s in sources_str.split(",") if s.strip()
        }
        if not self.predefined_sources:
            raise ValueError("[sources] predefined_sources must not be empty")
        if any(len(source) > 50 for source in self.predefined_sources):
            raise ValueError("[sources] source names must be at most 50 characters")
        self.default_source = self.config.get(
            "sources", "default_source", fallback="default"
        ).strip()
        if not self.default_source or len(self.default_source) > 50:
            raise ValueError("[sources] default_source must contain 1-50 characters")
        if self.default_source not in self.predefined_sources:
            self.predefined_sources.add(self.default_source)
        self.max_pool_size = self._cfg_int(
            "source_pool", "max_pool_size", 500, low=1
        )
        self.stats_pool_max_multiplier = self._cfg_int(
            "source_pool", "stats_pool_max_multiplier", 20, low=1
        )

        # Proxy selection configuration.
        legacy_weighted_selection_enabled = self.config.getboolean(
            "source_pool", "weighted_selection_enabled", fallback=False
        )
        self.selection_strategy = self.config.get(
            "source_pool", "selection_strategy", fallback="softmax"
        ).strip().lower()
        if legacy_weighted_selection_enabled and self.selection_strategy == "uniform":
            self.selection_strategy = "tiered"
        if self.selection_strategy not in {"uniform", "tiered", "weighted", "softmax"}:
            raise ValueError(f"Unknown selection_strategy: {self.selection_strategy!r}")
        self.top_tier_size = self._cfg_int(
            "source_pool", "top_tier_size", 100, low=0
        )
        if self.top_tier_size > self.max_pool_size:
            raise ValueError("[source_pool] top_tier_size must not exceed max_pool_size")
        self.top_tier_load_percentage = self._cfg_int(
            "source_pool", "top_tier_load_percentage", 70, low=0, high=100
        )
        self.proxy_cooldown_ms = self._cfg_int(
            "source_pool", "proxy_cooldown_ms", 0, low=0
        )
        if self.proxy_cooldown_ms > 0:
            logger.warning(
                "proxy_cooldown_ms={} now spaces out trial handouts only. "
                "Exploitation is paced by proxy_max_inflight instead, because a "
                "cooldown shorter than serving_plan_max_age_seconds would drop "
                "the busiest - and therefore highest-scoring - proxies from "
                "every plan rebuild.",
                self.proxy_cooldown_ms,
            )
        self.exploration_min_ratio = self._cfg_float(
            "source_pool", "exploration_min_ratio", 0.05, low=0.0, high=1.0
        )
        self.exploration_max_ratio = self._cfg_float(
            "source_pool",
            "exploration_max_ratio",
            0.30,
            low=0.0,
            high=1.0,
        )
        if self.exploration_min_ratio > self.exploration_max_ratio:
            raise ValueError(
                "[source_pool] exploration_min_ratio must not exceed exploration_max_ratio"
            )
        # The exploration ramp has to track how much of the *live* pool is still
        # unevaluated, not an absolute count of winners. A deployment with 1200
        # live proxies and 110 qualified ones is 9% evaluated, but an absolute
        # target of 50 reads that as "done" and drops exploration to the floor
        # while 91% of the pool has never been measured. The absolute target is
        # kept as a lower bound so a genuinely small pool still converges.
        self.exploration_target_qualified = self._cfg_int(
            "source_pool", "exploration_target_qualified", 50, low=1
        )
        self.exploration_target_qualified_ratio = self._cfg_float(
            "source_pool",
            "exploration_target_qualified_ratio",
            0.5,
            low=0.0,
            high=1.0,
        )
        self.exploration_discovery_share = self._cfg_float(
            "source_pool", "exploration_discovery_share", 2 / 3, low=0.0, high=1.0
        )
        self.qualification_min_results = self._cfg_int(
            "source_pool", "qualification_min_results", 3, low=1
        )
        self.probation_attempts = self._cfg_int(
            "source_pool",
            "probation_attempts",
            3,
            low=1,
        )
        if self.probation_attempts < self.qualification_min_results:
            raise ValueError(
                "[source_pool] probation_attempts must be >= qualification_min_results"
            )
        self.retry_attempts = self._cfg_int(
            "source_pool", "retry_attempts", 2, low=0
        )
        self.retry_delay_s = self._cfg_float(
            "source_pool", "retry_delay_seconds", 3600.0, low=0.0
        )
        self.probation_forgiveness_hours = self._cfg_float(
            "source_pool", "probation_forgiveness_hours", 48.0, low=0.1
        )
        self.proxy_inflight_timeout_s = self._cfg_float(
            "source_pool", "proxy_inflight_timeout_seconds", 120.0, low=0.1
        )
        # How many requests one *qualified* proxy may carry at once. This is a
        # per-proxy capacity limit, not a learning limit: a proxy whose success
        # rate is already known does not need its results serialised, and
        # holding it to one in-flight request caps the whole service at
        # (qualified proxies / round-trip time) - about 6 req/s for a pool of
        # 115 at a 20s round trip. 0 means unlimited. Trial candidates are
        # always held to one, because that is where a burst really would spend
        # the probation budget before returning a single bit of information.
        self.proxy_max_inflight = self._cfg_int(
            "source_pool", "proxy_max_inflight", 0, low=0
        )
        # Redraws allowed before a saturated plan is rebuilt. Only consulted
        # when proxy_max_inflight is set.
        self.exploit_draw_attempts = self._cfg_int(
            "source_pool", "exploit_draw_attempts", 4, low=1
        )
        # The serving plan is the control plane: eligibility, ranking and
        # weights are computed on this interval, off the request path, and
        # get_proxy() then does one weighted draw from the result. Staleness
        # costs at most this many seconds of routing to a proxy that has since
        # changed state - which validation and feedback correct anyway.
        self.serving_plan_max_age_s = self._cfg_float(
            "source_pool", "serving_plan_max_age_seconds", 2.0, low=0.001
        )
        self.selection_weight_floor = self._cfg_float(
            "source_pool", "selection_weight_floor", 1.0, low=0.01
        )
        self.softmax_temperature = self._cfg_float(
            "source_pool", "softmax_temperature", 14.0, low=0.1
        )
        self.avg_latency_alpha = self._cfg_float(
            "source_pool", "avg_latency_alpha", 0.3, low=0.01, high=1.0
        )
        self.max_feedback_latency_ms = self._cfg_int(
            "source_pool",
            "max_feedback_latency_ms",
            DEFAULT_MAX_FEEDBACK_LATENCY_MS,
            low=1,
        )
        self.premium_pool_size = self._cfg_int(
            "source_pool", "premium_pool_size", 20, low=0
        )
        self.premium_min_usage_count = self._cfg_int(
            "source_pool", "premium_min_usage_count", 50, low=0
        )
        self.allow_legacy_feedback = self.config.getboolean(
            "source_pool", "allow_legacy_feedback", fallback=True
        )
        self.completed_allocation_retention_s = self._cfg_float(
            "source_pool", "completed_allocation_retention_seconds", 3600.0, low=1.0
        )
        self.completed_allocation_max = self._cfg_int(
            "source_pool", "completed_allocation_max", 100000, low=1
        )

        # Fetcher configuration.
        # Proxy-list downloads use curl. The validator remains aiohttp-based
        # because it connects through the proxies and inspects response headers.
        self.fetch_connect_timeout_s = self._cfg_float(
            "fetcher", "connect_timeout_s", 30.0, low=0.001
        )
        self.fetch_total_timeout_s = self._cfg_float(
            "fetcher", "total_timeout_s", 60.0, low=0.001
        )
        if self.fetch_connect_timeout_s > self.fetch_total_timeout_s:
            raise ValueError(
                "[fetcher] connect_timeout_s must not exceed total_timeout_s"
            )
        self.fetch_curl_retries = self._cfg_int("fetcher", "curl_retries", 2, low=0)
        self.fetch_curl_retry_delay_s = self._cfg_int(
            "fetcher", "curl_retry_delay_s", 1, low=0
        )
        self.fetch_backoff_base_s = self._cfg_int(
            "fetcher", "backoff_base_s", 30, low=1
        )
        # Two caps, because the two failure classes deserve different patience.
        self.fetch_backoff_max_s = self._cfg_int(
            "fetcher", "backoff_max_s", 1800, low=self.fetch_backoff_base_s
        )
        self.fetch_backoff_transient_max_s = self._cfg_int(
            "fetcher",
            "backoff_transient_max_s",
            300,
            low=self.fetch_backoff_base_s,
            high=self.fetch_backoff_max_s,
        )
        for section in self.config.sections():
            if section.startswith("proxy_source_"):
                self._cfg_float(
                    section, "update_interval_minutes", 60.0, low=0.001
                )

        # Backup configuration
        self.stats_backup_enabled = self.config.getboolean(
            "backup", "stats_backup_enabled", fallback=True
        )
        self.stats_backup_interval_s = self._cfg_float(
            "backup", "stats_backup_interval_seconds", 3600.0, low=0.001
        )
        normal_stats_backup_path = self._resolve_project_path(
            self.config.get(
                "backup",
                "stats_backup_path",
                fallback="./.local/data/proxy_stats_backup.json",
            )
        )
        self.stats_backup_path = self._isolated_backup_path(
            normal_stats_backup_path, self.restore_mode
        )

        # Two-speed online reliability. Latency never enters this score.
        self.reliability_prior = self._cfg_float(
            "source_pool",
            "reliability_prior",
            DEFAULT_RELIABILITY_PRIOR,
            low=0.0,
            high=1.0,
        )
        self.reliability_slow_alpha = self._cfg_float(
            "source_pool", "reliability_slow_alpha", 0.12, low=0.0001, high=1.0
        )
        self.reliability_fast_alpha = self._cfg_float(
            "source_pool",
            "reliability_fast_alpha",
            0.30,
            low=0.0001,
            high=1.0,
        )
        if self.reliability_fast_alpha < self.reliability_slow_alpha:
            raise ValueError(
                "[source_pool] reliability_fast_alpha must be >= reliability_slow_alpha"
            )
        self.reliability_decay_half_life_hours = self._cfg_float(
            "source_pool", "reliability_decay_half_life_hours", 24.0, low=0.1
        )
        self.reliability_recent_results_limit = self._cfg_int(
            "source_pool", "reliability_recent_results_limit", 100, low=1
        )
        self.reliability_history_prior_weight = self._cfg_float(
            "source_pool", "reliability_history_prior_weight", 5.0, low=0.0
        )

        # Outage guard thresholds are relative to the source\'s own observed
        # success rate, never absolute. An absolute "healthy window >= 50%" gate
        # is unreachable for a source whose normal rate is 10%, so the guard
        # would never arm; an absolute "failure >= 90%" trigger is *below* that
        # same source\'s normal state, so it would fire on healthy traffic.
        self.outage_guard_enabled = self.config.getboolean(
            "source_pool", "outage_guard_enabled", fallback=True
        )
        self.outage_window_size = self._cfg_int(
            "source_pool", "outage_window_size", 20, low=1
        )
        self.outage_min_distinct_proxies = self._cfg_int(
            "source_pool",
            "outage_min_distinct_proxies",
            10,
            low=1,
            high=self.outage_window_size,
        )
        self.outage_healthy_baseline_ratio = self._cfg_float(
            "source_pool", "outage_healthy_baseline_ratio", 0.5, low=0.0, high=1.0
        )
        self.outage_failure_baseline_ratio = self._cfg_float(
            "source_pool", "outage_failure_baseline_ratio", 0.1, low=0.0, high=1.0
        )
        self.outage_recovery_baseline_ratio = self._cfg_float(
            "source_pool", "outage_recovery_baseline_ratio", 0.3, low=0.0, high=1.0
        )
        if not (
            self.outage_failure_baseline_ratio
            <= self.outage_recovery_baseline_ratio
            <= self.outage_healthy_baseline_ratio
        ):
            raise ValueError(
                "[source_pool] outage ratios must satisfy failure <= recovery <= healthy"
            )
        self.outage_baseline_alpha = self._cfg_float(
            "source_pool", "outage_baseline_alpha", 0.2, low=0.001, high=1.0
        )
        # A window only counts as evidence of an outage once an all-failure run
        # of that length is less likely than this under the source\'s own
        # baseline. At a 10% baseline that needs ~66 observations; at 90% it
        # needs 3. The window therefore sizes itself to the source.
        self.outage_false_positive_budget = self._cfg_float(
            "source_pool", "outage_false_positive_budget", 0.001, low=1e-9, high=0.5
        )
        self.outage_window_max_size = self._cfg_int(
            "source_pool",
            "outage_window_max_size",
            200,
            low=self.outage_window_size,
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
        return normal_path.with_name(f"{normal_path.stem}.no-restore{suffix}")

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

    def _read_source_backoff_states(self) -> Dict[str, Dict]:
        try:
            backoff_states = self.db.get_source_backoff_states()
        except Exception:
            backoff_states = None
            logger.exception("Could not restore proxy-source backoff state.")
        if not isinstance(backoff_states, dict):
            return {}
        return backoff_states

    def _load_fetcher_jobs(
        self, backoff_states: Optional[Dict[str, Dict]] = None
    ) -> List[Dict]:
        jobs = []
        if backoff_states is None:
            backoff_states = self._read_source_backoff_states()
        for section in self.config.sections():
            if section.startswith("proxy_source_"):
                state = backoff_states.get(section, {})
                next_attempt_at = float(state.get("next_attempt_at", 0.0) or 0.0)
                interval_minutes = self._cfg_float(
                    section, "update_interval_minutes", 60.0, low=0.001
                )
                job = {
                    "name": section,
                    "url": self.config.get(section, "url", fallback=None),
                    "interval_minutes": interval_minutes,
                    "default_protocol": self.config.get(
                        section, "default_protocol", fallback=None
                    ),
                    "last_run": max(
                        0.0, next_attempt_at - interval_minutes * 60
                    ),
                    "failure_count": int(state.get("failure_count", 0) or 0),
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
        "[server] production_threads / background_workers",
        "[source_pool] proxy_inflight_timeout_seconds",
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
        # Database latency must not stall allocations and feedback behind the
        # manager lock. Backoff state is advisory and can safely be snapshotted
        # immediately before applying the new file.
        backoff_states = self._read_source_backoff_states()
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
            restart_only_values = {
                "server_port": self.server_port,
                "production_threads": self.production_threads,
                "background_workers": self.background_workers,
                "proxy_inflight_timeout_s": self.proxy_inflight_timeout_s,
            }
            old_predefined_sources_before_reload = self.predefined_sources.copy()
            old_job_names = {job["name"] for job in self.fetcher_jobs}

            self.config = new_config
            try:
                self._load_config()
                for attribute, value in restart_only_values.items():
                    setattr(self, attribute, value)
                # Parsed inside the protected block, assigned outside it. This
                # keeps any job-building failure from producing a new-tunables /
                # old-jobs hybrid.
                new_fetcher_jobs = self._load_fetcher_jobs(backoff_states)
            except Exception as e:
                self.__dict__.update(attribute_snapshot)
                self.config = old_config
                logger.error(
                    "Config reload failed ({}); rolled back to the previous "
                    "configuration.",
                    e,
                )
                raise

            self.check_config_drift()

            # Routing tunables - strategy, temperature, exploration ratios -
            # are baked into the serving plan, so a reload that left the plans
            # standing would not be authoritative until they aged out.
            self.serving_plans.clear()

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
                    for allocation_id in [
                        candidate_id
                        for candidate_id, record in self.allocations.items()
                        if record["source"] == source
                    ]:
                        self._pop_allocation_locked(allocation_id)
                    for allocation_id in [
                        candidate_id
                        for candidate_id, record in self.completed_allocations.items()
                        if record["source"] == source
                    ]:
                        self.completed_allocations.pop(allocation_id, None)
                    self.source_stats.pop(source, None)
                    self.available_proxies.pop(source, None)
                    self.outage_states.pop(source, None)
                    self.serving_plans.pop(source, None)
                    self.proxy_last_handed_out_ts.pop(source, None)
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
        logger.info("Fetching proxy source '{}'.", job["name"])
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
            logger.error(
                "Proxy source '{}' fetch failed with {}.",
                job["name"],
                type(e).__name__,
            )
            failures = job.get("failure_count", 0) + 1
            job["failure_count"] = failures
            backoff_seconds = self._fetch_backoff_seconds(e, failures)
            next_attempt_at = time.time() + backoff_seconds
            job["last_run"] = next_attempt_at - job["interval_minutes"] * 60
            failure_class = (
                "transient" if self._is_transient_fetch_error(e) else "persistent"
            )
            try:
                self.db.upsert_source_backoff(
                    job["name"], failures, next_attempt_at, failure_class
                )
            except Exception:
                logger.exception("Could not persist proxy-source backoff state.")
            logger.warning(
                "Fetcher job '{}' backed off for {}s after {} consecutive "
                "{} failure(s).",
                job["name"],
                backoff_seconds,
                failures,
                failure_class,
            )
        else:
            had_failures = bool(job.get("failure_count"))
            job["failure_count"] = 0
            if had_failures:
                try:
                    self.db.clear_source_backoff(job["name"])
                except Exception:
                    logger.exception("Could not clear proxy-source backoff state.")
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
                f"HTTP status {http_status}",
                transient=http_status in TRANSIENT_HTTP_STATUS_CODES,
            )

        if result.returncode != 0:
            raise FetchError(
                f"curl failed with return code {result.returncode}",
                transient=result.returncode in TRANSIENT_CURL_EXIT_CODES,
            )
        if http_status is None:
            raise FetchError("curl did not report an HTTP status", transient=True)
        return body

    def _handle_fetch_results(
        self, futures: List, validate_after_insert: bool = False
    ):
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
                logger.error(
                    "A fetcher job raised {}.",
                    type(e).__name__,
                )

        if not all_new_proxies:
            logger.info("No new proxies were fetched in this cycle.")
            if validate_after_insert:
                self._run_validation_cycle()
            return

        unique_proxies_set = {tuple(p) for p in all_new_proxies}
        unique_proxies_list = [list(p) for p in unique_proxies_set]

        logger.info(
            f"Consolidated {len(unique_proxies_list)} unique proxies from all sources for insertion."
        )
        try:
            self.db.insert_proxies(unique_proxies_list)
        except DatabaseWriteError:
            # The scheduler has already advanced this cycle's validation
            # timestamp. Validate rows that were committed by earlier cycles
            # instead of making a transient insert failure delay recovery for
            # a full validation interval.
            logger.exception(
                "Fetched proxies were not inserted; validating existing rows."
            )
            if validate_after_insert:
                self._run_validation_cycle()
            return
        if validate_after_insert:
            self._run_validation_cycle()

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
                    logger.debug(
                        "Proxy ID {} connection error: {}", proxy_id, type(e).__name__
                    )
                return {"id": proxy_id, "success": False}
            except asyncio.TimeoutError:
                # Request timed out
                if self.debug_mode:
                    logger.debug(
                        "Proxy ID {} timed out after {}s",
                        proxy_id,
                        self.validation_timeout_s,
                    )
                return {"id": proxy_id, "success": False}
            except Exception as e:
                # Other errors
                if self.debug_mode:
                    logger.debug("Proxy ID {} failed: {}", proxy_id, type(e).__name__)
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
                    logger.debug(
                        "Proxy ID {} connection error: {}", proxy_id, type(e).__name__
                    )
                return {"id": proxy_id, "success": False}
            except asyncio.TimeoutError:
                # Request timed out
                if self.debug_mode:
                    logger.debug(
                        "Proxy ID {} timed out after {}s",
                        proxy_id,
                        self.validation_timeout_s,
                    )
                return {"id": proxy_id, "success": False}
            except Exception as e:
                # Other errors
                if self.debug_mode:
                    logger.debug("Proxy ID {} failed: {}", proxy_id, type(e).__name__)
                return {"id": proxy_id, "success": False}

    async def _validate_against_targets(self, proxy_id: int, proxy_url: str, request_factory) -> Dict:
        successes = 0
        latencies = []
        anonymity_levels = []
        target_results = []

        for target_index, target in enumerate(self.validation_targets):
            start_time = time.time()
            try:
                async with request_factory(target) as response:
                    response.raise_for_status()
                    latency_ms = int((time.time() - start_time) * 1000)
                    anonymity = await self._detect_anonymity(response, strict=True)
                    successes += 1
                    latencies.append(latency_ms)
                    anonymity_levels.append(anonymity)
                    target_results.append(
                        {"target_index": target_index, "success": True}
                    )
            except Exception as e:
                failure_kind = self._validation_failure_kind(e)
                target_results.append(
                    {
                        "target_index": target_index,
                        "success": False,
                        "failure_kind": failure_kind,
                    }
                )
                self.validation_target_failures[(target_index, failure_kind)] += 1
                if self.debug_mode:
                    logger.debug(
                        "Validation target index {} failed for proxy ID {}: {}",
                        target_index,
                        proxy_id,
                        failure_kind,
                    )

        if successes < self.validation_success_threshold:
            return {
                "id": proxy_id,
                "success": False,
                "target_results": target_results,
            }

        return {
            "id": proxy_id,
            "success": True,
            "latency": int(sum(latencies) / len(latencies)),
            "anonymity": self._combine_anonymity_levels(anonymity_levels),
            "target_results": target_results,
        }

    @staticmethod
    def _validation_failure_kind(error: Exception) -> str:
        if isinstance(error, asyncio.TimeoutError):
            return "timeout"
        if isinstance(error, aiohttp.ClientResponseError):
            return "http_status"
        dns_error = getattr(aiohttp, "ClientConnectorDNSError", ())
        if dns_error and isinstance(error, dns_error):
            return "dns"
        if isinstance(error, aiohttp.ClientConnectorError):
            if isinstance(getattr(error, "os_error", None), socket.gaierror):
                return "dns"
            return "connection"
        if isinstance(error, aiohttp.ClientConnectionError):
            return "connection"
        if isinstance(error, (ValueError, json.JSONDecodeError)):
            return "malformed_response"
        return "connection"

    async def _detect_anonymity(
        self, response: aiohttp.ClientResponse, strict: bool = False
    ) -> str:
        try:
            data = await response.json(content_type=None)
        except Exception as error:
            if strict:
                raise ValueError("validation response is not JSON") from error
            return "unknown"

        headers = data.get("headers") if isinstance(data, dict) else None
        if not isinstance(headers, dict):
            if strict:
                raise ValueError("validation response has no header mapping")
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

    async def _validate_proxies_batch_async(
        self, proxies_to_validate: List[Dict]
    ) -> ValidationBatchResult:
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
        
        normalized_results = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                normalized_results.append(
                    {"id": proxies_to_validate[i]["id"], "success": False, "target_results": []}
                )
            else:
                normalized_results.append(result)

        successes_by_target = [0] * len(self.validation_targets)
        for result in normalized_results:
            for target_result in result.get("target_results", []):
                if target_result.get("success"):
                    successes_by_target[target_result["target_index"]] += 1
        healthy_targets = [
            count >= self.validation_target_min_samples
            for count in successes_by_target
        ]
        quorum_healthy = (
            sum(1 for healthy in healthy_targets if healthy)
            >= self.validation_success_threshold
        )
        metadata = {
            "quorum_healthy": quorum_healthy,
            "healthy_targets": healthy_targets,
            "successes_by_target": successes_by_target,
        }
        success_proxies = [
            result for result in normalized_results if result.get("success")
        ]
        failure_proxy_ids = [
            result["id"] for result in normalized_results if not result.get("success")
        ]
        return ValidationBatchResult(success_proxies, failure_proxy_ids, metadata)

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
        candidates = [
            (proxy, "new") for proxy in new_proxies
        ] + [
            (proxy, "active") for proxy in revalidate_proxies
        ]
        for proxy, origin in candidates:
            if proxy["id"] in seen_ids:
                continue
            seen_ids.add(proxy["id"])
            batch.append(dict(proxy, _validation_origin=origin))
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
                        proxies_to_validate.append(
                            dict(p, _validation_origin="failed")
                        )

            if not proxies_to_validate:
                # The in-memory pool must still be refreshed: an empty batch is
                # not a reason to keep handing out a stale (possibly dead) pool.
                logger.info(
                    "No proxies to validate this cycle; refreshing pools anyway."
                )
                self._sync_and_select_top_proxies()
                return

            total_to_validate = len(proxies_to_validate)
            logger.info(f"Starting async validation for {total_to_validate} proxies...")

            # Use asyncio for high-performance validation
            validation_start_time = time.time()
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                batch_result = loop.run_until_complete(
                    self._validate_proxies_batch_async(proxies_to_validate)
                )
            finally:
                loop.close()

            success_proxies, failure_proxy_ids, validation_metadata = batch_result
            
            validation_duration = time.time() - validation_start_time
            proxies_per_second = total_to_validate / validation_duration if validation_duration > 0 else 0
            success_rate = len(success_proxies) / total_to_validate * 100 if total_to_validate > 0 else 0

            logger.info(
                f"Validation cycle finished in {validation_duration:.2f}s. "
                f"Success: {len(success_proxies)}/{total_to_validate} ({success_rate:.1f}%), "
                f"Throughput: {proxies_per_second:.1f} proxies/s"
            )

            with self.lock:
                self.last_validation_quorum_healthy = bool(
                    validation_metadata["quorum_healthy"]
                )
                self.last_validation_target_health = list(
                    validation_metadata["healthy_targets"]
                )

            if not validation_metadata["quorum_healthy"]:
                never_validated_ids = sorted(
                    {
                        int(proxy["id"])
                        for proxy in proxies_to_validate
                        if proxy.get("_validation_origin") == "new"
                    }
                )
                if never_validated_ids:
                    # Preserve last-known-good liveness, but advance the
                    # oldest-first never-validated cursor. Otherwise one bad
                    # batch can permanently hide every proxy fetched later.
                    # These rows remain eligible for the ordinary failed-proxy
                    # retry window once target health recovers.
                    self.db.batch_update_proxy_results(
                        [],
                        never_validated_ids,
                        self.validation_window_minutes,
                    )
                logger.error(
                    "Validation target quorum unavailable; preserving last-known-good "
                    "liveness; deferred candidates remain retryable. target_health={}",
                    validation_metadata["healthy_targets"],
                )
                return

            self.db.batch_update_proxy_results(
                success_proxies,
                failure_proxy_ids,
                self.validation_window_minutes,
            )
            with self.lock:
                self.last_validation_success_ts = time.time()

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
                        stats_pool[proxy_url] = self._get_new_proxy_stat(source)

                # One pass: age every estimator toward the fixed prior, and
                # classify the live ones while their score is in hand. This is
                # also the recovery path for idle proxies; it uses neither
                # validator measurements nor the population median.
                now_ts = time.time()
                groups = {"discovery": [], "probation": [], "retry": []}
                qualified = []
                live_count = 0
                max_trials = self.probation_attempts + self.retry_attempts
                for proxy_url, stat in stats_pool.items():
                    self._refresh_score(stat, source)
                    if proxy_url not in self.active_proxies:
                        continue
                    live_count += 1
                    self._refresh_trial_epoch(stat, now_ts)
                    if self._is_qualified(stat, now_ts):
                        qualified.append(proxy_url)
                        continue
                    trial_handouts = int(stat.get("trial_handout_count", 0) or 0)
                    if trial_handouts >= max_trials:
                        continue
                    if trial_handouts == 0 and not self._has_unexpired_results(
                        stat, now_ts
                    ):
                        groups["discovery"].append(proxy_url)
                    elif trial_handouts < self.probation_attempts:
                        groups["probation"].append(proxy_url)
                    else:
                        groups["retry"].append(proxy_url)

                stats_pool = self._truncate_stats_pool(source, stats_pool)
                self.source_stats[source] = stats_pool

                # Handout times outlive the pool entries they describe, so
                # prune them alongside it rather than letting them accumulate
                # one entry per proxy ever served.
                handed_out = self.proxy_last_handed_out_ts.get(source)
                if handed_out:
                    for proxy_url in [
                        url for url in handed_out if url not in stats_pool
                    ]:
                        del handed_out[proxy_url]

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

                # The ranking this loop just produced is exactly what the
                # serving plan needs, so it is assembled here rather than by a
                # second sweep over the same pool.
                self._build_serving_plan(
                    source,
                    now_ts=now_ts,
                    groups=groups,
                    qualified=qualified,
                    live_count=live_count,
                )

                logger.info(
                    f"Source '{source}' synced. "
                    f"Stats pool: {len(sorted_proxies)} proxies, "
                    f"of which {len(usable_proxies)} are alive and usable. "
                    f"Fixed reliability prior: {self._baseline_score(source):.1f}. "
                    f"Top Tier: {len(top_tier)} proxies. "
                    f"Bottom Tier: {len(bottom_tier)} proxies."
                )

            self._sync_premium_proxies_locked()

    def _flush_stats(
        self,
        include_current: bool = False,
        deadline: Optional[float] = None,
    ) -> bool:
        """Persist eligible minute aggregates."""
        return self._flush_feedback_buffer(
            include_current=include_current,
            deadline=deadline,
        )

    def _baseline_score(self, source: Optional[str] = None) -> float:
        """Fixed initial reliability score on the public 0-100 scale."""
        return self.reliability_prior * 100.0

    def _latency_sort_key(self, stat: Dict) -> float:
        # Runs once per proxy inside the pool sort; avg_latency_ms is written
        # as an int or None by this class and coerced on restore.
        latency = stat.get("avg_latency_ms")
        if type(latency) is int:
            return float(latency)
        latency = self._coerce_latency(latency, self.max_feedback_latency_ms)
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
                    cold_start = not self.active_proxies

                validation_due = (
                    now - last_validation_run >= self.validation_interval_s
                )
                if validation_due:
                    last_validation_run = now

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
                    self._submit_background(
                        self._handle_fetch_results,
                        fetch_futures,
                        validation_due and cold_start,
                    )

                if validation_due and not (fetch_futures and cold_start):
                    self._submit_background(self._run_validation_cycle)
                if now - last_flush_time >= self.stats_flush_interval_s:
                    last_flush_time = now
                    self._submit_background(self._flush_stats)
                if (
                    now - self.last_source_refresh_time
                    >= self.source_refresh_interval_s
                ):
                    self.last_source_refresh_time = now
                    self._submit_background(self._update_dashboard_sources)

                # Stats backup task
                if self.stats_backup_enabled and now - last_backup_time >= self.stats_backup_interval_s:
                    last_backup_time = now
                    self._submit_background(self.backup_stats)

                self.refresh_serving_plans()

                self.stop_scheduler_event.wait(5)
            except Exception as e:
                logger.error(f"Error in scheduler loop: {e}", exc_info=True)
                self.stop_scheduler_event.wait(60)

    def _submit_background(self, callback, *args):
        with self.lock:
            if not self.accepting_background_tasks:
                return None
            future = self.background_executor.submit(callback, *args)
            self.background_futures.add(future)

        def completed(done):
            with self.lock:
                self.background_futures.discard(done)
            try:
                done.result()
            except Exception:
                logger.exception("Tracked background task failed.")

        future.add_done_callback(completed)
        return future

    def start_scheduler(self):
        if not self.scheduler_thread or not self.scheduler_thread.is_alive():
            self.stop_scheduler_event.clear()
            self.accepting_background_tasks = True
            self.scheduler_thread = threading.Thread(
                target=self._scheduler_loop, daemon=True
            )
            self.scheduler_thread.start()
            logger.info("Background scheduler started.")

    def stop_scheduler(self):
        """Stop scheduling, drain/cancel work, then persist final state."""
        started = time.monotonic()
        deadline = started + self.shutdown_deadline_s
        persistence_reserve_s = min(
            8.0,
            max(1.0, self.shutdown_deadline_s * 0.4),
        )
        drain_deadline = deadline - persistence_reserve_s
        logger.info(
            "Stopping scheduler with a {:.1f}s graceful deadline.",
            self.shutdown_deadline_s,
        )
        with self.lock:
            self.accepting_background_tasks = False
            self.stop_scheduler_event.set()

        if self.scheduler_thread and self.scheduler_thread.is_alive():
            self.scheduler_thread.join(
                timeout=max(0.0, drain_deadline - time.monotonic())
            )

        self.fetch_executor.shutdown(wait=False, cancel_futures=True)
        with self.lock:
            tracked = list(self.background_futures)
        if tracked:
            _, unfinished = wait(
                tracked, timeout=max(0.0, drain_deadline - time.monotonic())
            )
            for future in unfinished:
                future.cancel()
            if unfinished:
                logger.warning(
                    "Shutdown deadline left {} background task(s) unfinished.",
                    len(unfinished),
                )
        self.background_executor.shutdown(wait=False, cancel_futures=True)

        # The current partial minute follows the same acknowledge-after-commit
        # path as periodic data.
        flushed = self._flush_stats(include_current=True, deadline=deadline)
        backed_up = True
        if self.stats_backup_enabled and time.monotonic() < deadline:
            backed_up = self.backup_stats(deadline=deadline).get("status") == "success"
        elif self.stats_backup_enabled:
            backed_up = False
            logger.warning("Shutdown deadline reached before the final stats backup.")
        logger.info(
            "Background scheduler stopped; final flush={} backup={} elapsed={:.2f}s.",
            "complete" if flushed else "deferred",
            "complete" if backed_up else "deferred",
            time.monotonic() - started,
        )

    def _get_new_proxy_stat(self, source: Optional[str] = None) -> Dict:
        """Create a proxy stat at the fixed reliability prior."""
        return {
            "score": self._baseline_score(source),
            "quality_slow": self.reliability_prior,
            "quality_fast": self.reliability_prior,
            "quality_updated_ts": None,
            "success_count": 0,         # Total historical success
            "failure_count": 0,         # Total historical failure
            "recent_results": [],       # List of [timestamp, success: bool, latency_ms: int|None]
            "avg_latency_ms": None,     # Exponential moving average of latency
            "last_feedback_ts": None,   # Unix timestamp of latest feedback
            "handout_count": 0,
            "trial_handout_count": 0,
            "last_handed_out_ts": None,
            "inflight": [],
            "retry_after_ts": 0.0,
        }

    def backup_stats(self, deadline: Optional[float] = None) -> Dict:
        """Backup source_stats to a JSON file."""
        if deadline is None:
            acquired = self.backup_lock.acquire()
        else:
            acquired = self.backup_lock.acquire(
                timeout=max(0.0, deadline - time.monotonic())
            )
        if not acquired:
            logger.warning("Stats backup deferred: shutdown deadline exhausted.")
            return {"status": "deferred"}
        try:
            return self._backup_stats_locked()
        finally:
            self.backup_lock.release()

    def _backup_stats_locked(self) -> Dict:
        backup_started = time.monotonic()
        lock_started = time.monotonic()
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
            # Persist the committed value of anything a live outage window has
            # only tentatively changed.
            for source, outage_state in self.outage_states.items():
                for proxy_url, committed in outage_state.get(
                    "protected_stats", {}
                ).items():
                    tentative = source_stats_snapshot.get(source, {}).get(proxy_url)
                    if tentative is not None:
                        self._apply_stat_snapshot(tentative, committed)
            stats_snapshot = {
                "scoring_version": SCORING_VERSION,
                "timestamp": datetime.now().isoformat(),
                "source_stats": source_stats_snapshot,
            }
        self.last_manager_lock_hold_s = time.monotonic() - lock_started

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
            self.last_backup_duration_s = time.monotonic() - backup_started
            return {
                "status": "success",
                "path": str(backup_path),
                "sources": len(stats_snapshot["source_stats"]),
                "total_proxies": total_proxies,
            }
        except Exception as e:
            self.last_backup_duration_s = time.monotonic() - backup_started
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

    def liveness_status(self) -> Dict[str, object]:
        return {"status": "live", "serving": True}

    def readiness_status(self) -> Dict[str, object]:
        now_ts = time.time()
        try:
            database_ready = bool(self.db.ping())
        except Exception:
            database_ready = False
        with self.lock:
            scheduler_ready = bool(
                self.scheduler_thread
                and self.scheduler_thread.is_alive()
                and not self.stop_scheduler_event.is_set()
            )
            validation_ready = bool(
                self.last_validation_quorum_healthy
                and self.last_validation_success_ts is not None
                and now_ts - self.last_validation_success_ts
                <= self.readiness_validation_max_age_s
            )
            flush_ready = bool(
                self.last_flush_success_ts is not None
                and now_ts - self.last_flush_success_ts <= self.readiness_flush_max_age_s
            )
            usable_pool = len(self.active_proxies)
            pool_ready = usable_pool >= self.readiness_min_usable_pool
        dependencies = {
            "database": database_ready,
            "scheduler": scheduler_ready,
            "validation": validation_ready,
            "feedback_flush": flush_ready,
            "usable_pool": pool_ready,
        }
        return {
            "status": "ready" if all(dependencies.values()) else "not_ready",
            "ready": all(dependencies.values()),
            "dependencies": dependencies,
            "usable_proxies": usable_pool,
            "minimum_usable_proxies": self.readiness_min_usable_pool,
        }

    def get_proxy(self, source: str) -> Optional[str]:
        allocation = self.allocate_proxy(source)
        return allocation["proxy"] if allocation else None

    def allocate_proxy(self, source: str) -> Optional[Dict[str, str]]:
        """
        Draw one proxy and create an opaque, source-bound allocation.

        The request path deliberately holds no pool logic: eligibility,
        qualification, ranking and weights are all decided by
        _build_serving_plan() on a timer. What is left here is one coin flip
        for the exploration budget and one draw - O(1) for a trial candidate,
        O(log n) for a weighted exploit pick - so throughput is bounded by the
        draw, not by the size of the pool.
        """
        source = self._get_source_or_default(source)

        with self.lock:
            self._cleanup_allocations_locked(time.time())
            plan = self._serving_plan(source)
            if plan is None:
                logger.warning(f"No proxy pools defined for source '{source}'.")
                return None

            now_ts = time.time()
            exploit = plan["exploit"]
            trial_available = len(plan["discovery"]) + len(plan["fallback"])

            if trial_available and (
                not exploit or random.random() < plan["exploration_ratio"]
            ):
                selected = self._take_trial_candidate(plan)
                if selected is not None:
                    stat = self.source_stats.get(source, {}).get(selected)
                    if (
                        selected not in self.active_proxies
                        or stat is None
                        or self._is_qualified(stat, now_ts)
                        or not self._is_eligible(source, selected, now_ts)
                    ):
                        selected = None
                if selected is not None:
                    if not exploit and source not in self.cold_start_fallback_logged:
                        logger.warning(
                            "Source '{}' has no qualified exploit candidate; serving "
                            "trial traffic through the explicit cold-start fallback "
                            "above the {:.1f}% budget.",
                            source,
                            plan["exploration_ratio"] * 100,
                        )
                        self.cold_start_fallback_logged.add(source)
                    allocation_id = self._mark_proxy_handed_out(
                        source, selected, now_ts, premium=False
                    )
                    return {
                        "proxy": selected,
                        "source": source,
                        "allocation_id": allocation_id,
                    }

            if not exploit:
                logger.warning(
                    "No eligible proxy for source '{}' in the current serving plan.",
                    source,
                )
                return None

            self.cold_start_fallback_logged.discard(source)
            selected = self._draw_exploit(plan, source, now_ts)
            if selected is None:
                logger.warning(
                    "Every qualified proxy for source '{}' is at its "
                    "proxy_max_inflight limit.",
                    source,
                )
                return None
            allocation_id = self._mark_proxy_handed_out(
                source, selected, now_ts, premium=False
            )
            return {
                "proxy": selected,
                "source": source,
                "allocation_id": allocation_id,
            }

    @staticmethod
    def _pop_random(pool: List[str]) -> str:
        """Remove and return a uniformly random member in O(1)."""
        index = random.randrange(len(pool))
        selected = pool[index]
        pool[index] = pool[-1]
        pool.pop()
        return selected

    def _take_trial_candidate(self, plan: Dict) -> Optional[str]:
        """
        Claim a trial candidate, removing it from the plan.

        Removal *is* the in-flight lease for trial traffic: a proxy on probation
        cannot be handed out twice before its result comes back, and enforcing
        that by deletion costs nothing on the request path. Feedback puts it
        back, and so does the next plan rebuild once the lease has expired.
        """
        discovery, fallback = plan["discovery"], plan["fallback"]
        if discovery and fallback:
            pool = (
                discovery
                if random.random() < self.exploration_discovery_share
                else fallback
            )
        else:
            pool = discovery or fallback
        if not pool:
            return None
        selected = self._pop_random(pool)
        plan["members"].discard(selected)
        return selected

    def _return_trial_candidate(self, source: str, proxy_url: str, stat: Dict):
        """Feedback arrived: the candidate is claimable again."""
        plan = self.serving_plans.get(source)
        if plan is None or proxy_url in plan["members"]:
            return
        # A proxy that failed validation while its request was outstanding must
        # not be put back: only proxies that survived the latest validation are
        # ever handed out, and the plan is what hands them out.
        if proxy_url not in self.active_proxies or self._is_qualified(stat):
            return
        if self.proxy_cooldown_ms > 0 and not self._is_eligible(
            source, proxy_url, apply_cooldown=True
        ):
            # The next background plan refresh makes it ready after the
            # configured cooldown; do not bypass the delay by reinserting now.
            return
        trial_handouts = int(stat.get("trial_handout_count", 0) or 0)
        if trial_handouts >= self.probation_attempts + self.retry_attempts:
            return
        if trial_handouts >= self.probation_attempts:
            if (stat.get("retry_after_ts") or 0.0) > time.time():
                return
            plan["fallback"].append(proxy_url)
        elif trial_handouts == 0 and not self._has_unexpired_results(stat, time.time()):
            plan["discovery"].append(proxy_url)
        else:
            plan["fallback"].append(proxy_url)
        plan["members"].add(proxy_url)

    def _remove_from_exploit(self, source: str, proxy_url: str):
        """Atomically tombstone a candidate that no longer qualifies."""
        plan = self.serving_plans.get(source)
        if plan is None or proxy_url not in plan["exploit_members"]:
            return
        plan["exploit"] = [url for url in plan["exploit"] if url != proxy_url]
        plan["exploit_members"].discard(proxy_url)
        plan["cum_weights"] = self._exploit_cum_weights(source, plan["exploit"])

    def _remove_from_premium(self, source: str, proxy_url: str):
        """Tombstone one demoted premium candidate without rescanning the pool."""
        if self.premium_sources.get(proxy_url) != source:
            return
        self.premium_proxies = [url for url in self.premium_proxies if url != proxy_url]
        self.premium_sources.pop(proxy_url, None)

    def _promote_to_exploit(self, source: str, proxy_url: str, stat: Dict):
        """
        A proxy that just qualified starts exploiting now, not at the next
        rebuild.

        Without this it falls into a hole: feedback takes it out of the trial
        pool because it is no longer a trial candidate, while the exploit set
        was frozen when the plan was built and does not contain it. During a
        cold start - when every proxy is qualifying at once - that hole is
        most of the pool, and the service answers 404 while holding a pool of
        healthy proxies.
        """
        plan = self.serving_plans.get(source)
        if plan is None or proxy_url in plan["exploit_members"]:
            return
        if proxy_url not in self.active_proxies:
            return
        if not self._is_eligible(source, proxy_url, None, apply_cooldown=False):
            return
        plan["exploit"].append(proxy_url)
        plan["exploit_members"].add(proxy_url)
        # Reweighting is O(pool), but it runs once per qualification event, not
        # once per request, and a qualification event is rare after cold start.
        plan["cum_weights"] = self._exploit_cum_weights(source, plan["exploit"])

    def _draw_exploit(
        self,
        plan: Dict,
        source: Optional[str] = None,
        now_ts: Optional[float] = None,
    ) -> Optional[str]:
        """
        One weighted draw from the plan, honouring the per-proxy cap.

        With proxy_max_inflight at its default of 0 this is a single bisect and
        nothing else. When a deployment does set a cap, the plan alone cannot
        enforce it - the plan is rebuilt on an interval, and a burst inside that
        interval would hand the same proxy out without limit - so the drawn
        candidate is checked and redrawn. That check is O(1) on one proxy, not a
        pass over the pool.
        """
        exploit = plan["exploit"]
        if not exploit:
            return None
        cum_weights = plan["cum_weights"]

        def draw():
            if cum_weights is None:
                return random.choice(exploit)
            # cum_weights are precomputed, so this is a bisect, not a pass over
            # the population the way random.choices(weights=...) would be.
            return random.choices(exploit, cum_weights=cum_weights, k=1)[0]

        now_ts = time.time() if now_ts is None else now_ts
        stats_pool = self.source_stats.get(source, {})
        for _ in range(self.exploit_draw_attempts):
            candidate = draw()
            stat = stats_pool.get(candidate)
            if (
                candidate not in self.active_proxies
                or stat is None
                or not self._is_qualified(stat, now_ts)
            ):
                self._remove_from_exploit(source, candidate)
                if not plan["exploit"]:
                    return None
                exploit = plan["exploit"]
                cum_weights = plan["cum_weights"]
                continue
            if self.proxy_max_inflight <= 0 or self._lease_count(
                stat, now_ts
            ) < self.proxy_max_inflight:
                return candidate
        self._schedule_plan_refresh_locked(source)
        return None

    def _serving_plan(self, source: str) -> Optional[Dict]:
        plan = self.serving_plans.get(source)
        if plan is None:
            # The initial plan is built during startup pool sync. This fallback
            # covers embedders that construct a manager without that lifecycle;
            # expired plans below are never rebuilt on the request thread.
            return self._build_serving_plan(source)
        if time.time() - plan["built_at"] >= self.serving_plan_max_age_s:
            self._schedule_plan_refresh_locked(source)
        return plan

    def _schedule_plan_refresh_locked(self, source: str):
        if source in self.plan_refreshing or not self.accepting_background_tasks:
            return
        self.plan_refreshing.add(source)

        def refresh():
            started = time.monotonic()
            try:
                with self.lock:
                    self._build_serving_plan(source)
            finally:
                duration = time.monotonic() - started
                with self.lock:
                    self.last_plan_refresh_duration_s = duration
                    self.plan_refreshing.discard(source)

        self._submit_background(refresh)

    def refresh_serving_plans(self):
        """Rebuild every source's plan; called by the scheduler, off-path."""
        with self.lock:
            for source in list(self.predefined_sources):
                self._build_serving_plan(source)

    def _build_serving_plan(
        self,
        source: str,
        now_ts: Optional[float] = None,
        groups: Optional[Dict[str, List[str]]] = None,
        qualified: Optional[List[str]] = None,
        live_count: Optional[int] = None,
    ) -> Optional[Dict]:
        """
        Assemble what this source is willing to serve, and how it is weighted.

        This is the only place proxy state is examined for routing. The pool
        sync passes in the classification it already computed while ranking;
        a standalone refresh recomputes it. Caller must hold self.lock.
        """
        pools = self.available_proxies.get(source)
        if pools is None:
            return None
        now_ts = time.time() if now_ts is None else now_ts
        stats_pool = self.source_stats.get(source, {})
        if groups is None or qualified is None or live_count is None:
            groups, qualified, live_count = self._scan_trial_pool(source, now_ts)

        # Every live qualified proxy exploits - not just the ranked slice.
        # available_proxies is recomputed only by the pool sync, so a proxy that
        # qualifies between syncs is not in it; gating exploitation on that list
        # stranded such a proxy in nothing at all, since feedback had already
        # taken it out of the trial pool for being qualified. max_pool_size
        # still bounds the tier lists, which is what weights the `tiered`
        # strategy; it no longer decides who may be served.
        # Score order is reproducible across rebuilds; set iteration is not.
        exploit = sorted(
            (
                proxy_url
                for proxy_url in qualified
                if self._is_eligible(source, proxy_url, now_ts, apply_cooldown=False)
            ),
            key=lambda url: (-float(stats_pool[url]["score"]), url),
        )
        discovery = [
            proxy_url
            for proxy_url in groups["discovery"]
            if self._is_eligible(source, proxy_url, now_ts)
        ]
        fallback = [
            proxy_url
            for proxy_url in groups["probation"]
            if self._is_eligible(source, proxy_url, now_ts)
        ] + [
            proxy_url
            for proxy_url in groups["retry"]
            if self._is_eligible(source, proxy_url, now_ts)
            and (stats_pool[proxy_url].get("retry_after_ts") or 0.0) <= now_ts
        ]

        plan = {
            "built_at": now_ts,
            "exploit": exploit,
            "exploit_members": set(exploit),
            "cum_weights": self._exploit_cum_weights(source, exploit),
            "discovery": discovery,
            "fallback": fallback,
            "members": set(discovery) | set(fallback),
            "exploration_ratio": self._exploration_ratio_for(
                live_count, len(qualified)
            ),
        }
        self.serving_plans[source] = plan
        return plan

    def _exploit_cum_weights(
        self, source: str, candidates: List[str]
    ) -> Optional[List[float]]:
        """
        Cumulative selection weights, or None when the draw is uniform.

        Computed once per plan rather than once per request: softmax over a
        200-proxy pool is a couple of hundred exp() calls, which is nothing on
        a timer and everything on a hot path.
        """
        if not candidates or self.selection_strategy == "uniform":
            return None
        stats = self.source_stats.get(source, {})
        default_score = self._baseline_score(source)
        scores = [
            float(stats.get(proxy_url, {}).get("score", default_score))
            for proxy_url in candidates
        ]
        if self.selection_strategy == "softmax":
            top = max(scores)
            weights = [
                math.exp((score - top) / self.softmax_temperature) for score in scores
            ]
        elif self.selection_strategy == "tiered":
            # Keep the tier split as a two-level weighting rather than a
            # separate code path: the top tier collectively receives
            # top_tier_load_percentage of the traffic.
            top_tier = set(self.available_proxies.get(source, {}).get("top_tier", []))
            in_top = [url for url in candidates if url in top_tier]
            share = self.top_tier_load_percentage / 100
            if not in_top or len(in_top) == len(candidates):
                weights = [1.0] * len(candidates)
            else:
                top_weight = share / len(in_top)
                bottom_weight = (1 - share) / (len(candidates) - len(in_top))
                weights = [
                    top_weight if url in top_tier else bottom_weight
                    for url in candidates
                ]
        else:
            weights = [max(self.selection_weight_floor, score) for score in scores]

        cumulative, running = [], 0.0
        for weight in weights:
            running += weight
            cumulative.append(running)
        return cumulative if running > 0 else None

    def _exploration_ratio_for(self, live_count: int, qualified_count: int) -> float:
        """
        Interpolate the exploration budget from how evaluated the pool is.

        The target is the larger of the absolute floor and a share of the live
        pool, so the budget does not collapse to its minimum while most of a
        large pool has never been measured.
        """
        target = max(
            self.exploration_target_qualified,
            live_count * self.exploration_target_qualified_ratio,
        )
        progress = min(1.0, qualified_count / target) if target > 0 else 1.0
        return self.exploration_max_ratio - (
            self.exploration_max_ratio - self.exploration_min_ratio
        ) * progress

    def _has_unexpired_results(self, stat: Dict, now_ts: float) -> bool:
        """Whether any stored result still counts. O(1): the list is sorted."""
        results = stat.get("recent_results")
        return bool(results) and (
            results[-1][0] > now_ts - self.probation_forgiveness_hours * 3600
        )

    def _is_qualified(self, stat: Dict, now_ts: Optional[float] = None) -> bool:
        """
        Live, evidenced and scoring above the prior.

        Qualification asks whether the count reaches a threshold, never what
        the count is, and recent_results is sorted - so the k-th newest entry
        answers it outright. Counting the window instead put a binary search
        plus a comparison callback on the busiest path in the service: this is
        evaluated for every ranked candidate on every single request.
        """
        results = stat.get("recent_results")
        needed = self.qualification_min_results
        if not results or len(results) < needed:
            return False
        now_ts = time.time() if now_ts is None else now_ts
        if results[-needed][0] <= now_ts - self.probation_forgiveness_hours * 3600:
            return False
        baseline = self._baseline_score()
        return float(stat.get("score", baseline)) > baseline

    def _lease_count(self, stat: Dict, now_ts: float) -> int:
        """
        In-flight handouts for this proxy, expiring the ones nobody reported.

        Every lease is granted with the same timeout, so the list is appended
        in expiry order and the expired ones are always a prefix.
        """
        leases = stat.get("inflight")
        if not leases:
            return 0
        expired = bisect_right(leases, now_ts)
        if expired:
            del leases[:expired]
        return len(leases)

    def _grant_lease(self, stat: Dict, now_ts: float):
        leases = stat.get("inflight")
        if not isinstance(leases, list):
            leases = stat["inflight"] = []
        leases.append(now_ts + self.proxy_inflight_timeout_s)

    @staticmethod
    def _release_lease_expiry(stat: Dict, expiry: float):
        leases = stat.get("inflight")
        if not leases:
            return
        try:
            leases.remove(expiry)
        except ValueError:
            # _lease_count() may already have pruned this expired slot. Never
            # substitute another live request's lease for the missing one.
            pass

    def _pop_allocation_locked(self, allocation_id: str) -> Optional[Dict]:
        """Remove one allocation and its reverse-index entry."""
        record = self.allocations.pop(allocation_id, None)
        if record is None:
            return None
        key = (record["source"], record["proxy"])
        indexed = self.allocations_by_proxy.get(key)
        if indexed is not None:
            indexed.pop(allocation_id, None)
            if not indexed:
                self.allocations_by_proxy.pop(key, None)
        return record

    def _remember_completed_allocation_locked(
        self, allocation_id: str, record: Dict, reason: str, now_ts: float
    ):
        self.completed_allocations[allocation_id] = {
            "source": record["source"],
            "proxy": record["proxy"],
            "reason": reason,
            "completed_at": now_ts,
        }
        self.completed_allocations.move_to_end(allocation_id)
        cutoff = now_ts - self.completed_allocation_retention_s
        while self.completed_allocations:
            _, oldest = next(iter(self.completed_allocations.items()))
            if (
                len(self.completed_allocations) <= self.completed_allocation_max
                and oldest["completed_at"] >= cutoff
            ):
                break
            self.completed_allocations.popitem(last=False)

    def _cleanup_allocations_locked(self, now_ts: float):
        cutoff = now_ts - self.completed_allocation_retention_s
        while self.completed_allocations:
            _, oldest = next(iter(self.completed_allocations.items()))
            if oldest["completed_at"] >= cutoff:
                break
            self.completed_allocations.popitem(last=False)
        # All live allocations use the same restart-only timeout, so insertion
        # order is expiry order and expired records are a prefix.
        expired_ids = []
        for allocation_id, record in self.allocations.items():
            if record["expires_at"] > now_ts:
                break
            expired_ids.append(allocation_id)
        for allocation_id in expired_ids:
            record = self._pop_allocation_locked(allocation_id)
            if record is None:
                continue
            stat = self.source_stats.get(record["source"], {}).get(record["proxy"])
            if stat is not None:
                self._release_lease_expiry(stat, record["expires_at"])
            self._remember_completed_allocation_locked(
                allocation_id, record, "expired", now_ts
            )

    def _accept_feedback_allocation_locked(
        self,
        source: str,
        proxy_url: str,
        allocation_id: Optional[str],
        now_ts: float,
    ) -> Tuple[bool, str, str]:
        """Validate feedback identity and close exactly one matching lease."""
        self._cleanup_allocations_locked(now_ts)
        if allocation_id is None:
            if not self.allow_legacy_feedback:
                return False, "allocation_id_required", source
            normalized_source = self._get_source_or_default(source)
            matching_id = next(
                iter(
                    self.allocations_by_proxy.get(
                        (normalized_source, proxy_url), ()
                    )
                ),
                None,
            )
            if matching_id is not None:
                record = self._pop_allocation_locked(matching_id)
                stat = self.source_stats.get(normalized_source, {}).get(proxy_url)
                if stat is not None and record is not None:
                    self._release_lease_expiry(stat, record["expires_at"])
                if record is not None:
                    self._remember_completed_allocation_locked(
                        matching_id, record, "accepted_legacy", now_ts
                    )
            self.legacy_feedback_total += 1
            logger.debug(
                "Accepted feedback without allocation_id in compatibility mode; "
                "exact idempotency is unavailable."
            )
            return True, "accepted_legacy", normalized_source

        if not isinstance(allocation_id, str) or not allocation_id.strip():
            return False, "invalid_allocation_id", source
        completed = self.completed_allocations.get(allocation_id)
        if completed is not None:
            reason = completed["reason"]
            if reason in {"accepted", "accepted_legacy"}:
                reason = "duplicate_allocation_id"
            return False, reason, source
        record = self.allocations.get(allocation_id)
        if record is None:
            return False, "unknown_allocation_id", source
        if source != record["source"]:
            return False, "allocation_source_mismatch", source
        if proxy_url != record["proxy"]:
            return False, "allocation_proxy_mismatch", source

        record = self._pop_allocation_locked(allocation_id)
        if record is None:
            return False, "unknown_allocation_id", source
        stat = self.source_stats.get(source, {}).get(proxy_url)
        if stat is not None:
            self._release_lease_expiry(stat, record["expires_at"])
        self._remember_completed_allocation_locked(
            allocation_id, record, "accepted", now_ts
        )
        return True, "accepted", source

    def _inflight_limit(self, stat: Dict, now_ts: Optional[float] = None) -> int:
        return (
            self.proxy_max_inflight
            if self._is_qualified(stat, now_ts)
            else 1
        )

    def _refresh_trial_epoch(self, stat: Dict, now_ts: float):
        # Reads only fields this class writes as floats, and _migrate_legacy_stat
        # has already coerced anything that arrived from a file. Re-validating
        # them per proxy per request is the kind of defence that only costs.
        self._lease_count(stat, now_ts)

        anchor = max(
            stat.get("last_feedback_ts") or 0.0,
            stat.get("last_handed_out_ts") or 0.0,
        )
        if anchor and now_ts - anchor > self.probation_forgiveness_hours * 3600:
            stat["trial_handout_count"] = 0
            stat["retry_after_ts"] = 0.0

    def _scan_trial_pool(
        self, source: str, now_ts: Optional[float] = None
    ) -> Tuple[Dict[str, List[str]], List[str], int]:
        """
        One pass over the live pool: trial candidates and qualified proxies.

        Membership only. The transient gates - in-flight lease, cooldown, retry
        delay - are deliberately *not* applied here: they change on every
        handout, and folding them into an index that is reused across requests
        would make a proxy invisible for as long as the index lived. They are
        applied to the one candidate actually picked, by _is_trial_candidate().

        Iterates active_proxies rather than the stats pool, which also holds
        the retained history of every proxy that has died - typically an order
        of magnitude more entries than are actually live.
        """
        now_ts = time.time() if now_ts is None else now_ts
        groups = {"discovery": [], "probation": [], "retry": []}
        stats_pool = self.source_stats.get(source, {})
        max_trials = self.probation_attempts + self.retry_attempts
        qualified = []
        live_count = 0
        for proxy_url in self.active_proxies:
            stat = stats_pool.get(proxy_url)
            if stat is None:
                continue
            live_count += 1
            self._refresh_trial_epoch(stat, now_ts)
            if self._is_qualified(stat, now_ts):
                qualified.append(proxy_url)
                continue
            trial_handouts = int(stat.get("trial_handout_count", 0) or 0)
            if trial_handouts >= max_trials:
                continue
            if trial_handouts == 0 and not self._has_unexpired_results(stat, now_ts):
                groups["discovery"].append(proxy_url)
            elif trial_handouts < self.probation_attempts:
                groups["probation"].append(proxy_url)
            else:
                groups["retry"].append(proxy_url)
        return groups, qualified, live_count

    def _mark_proxy_handed_out(
        self, source: str, proxy_url: str, now_ts: float, premium: bool = False
    ) -> str:
        # Handout times exist only to answer the cooldown question. With
        # cooldown off - the default - recording them is a write on the hot
        # path feeding a map that is never read and never shrinks.
        if self.proxy_cooldown_ms > 0:
            self.proxy_last_handed_out_ts[source][proxy_url] = now_ts
        stat = self.source_stats.get(source, {}).get(proxy_url)
        allocation_id = secrets.token_urlsafe(24)
        expires_at = now_ts + self.proxy_inflight_timeout_s
        self.allocations[allocation_id] = {
            "source": source,
            "proxy": proxy_url,
            "expires_at": expires_at,
            "premium": bool(premium),
        }
        self.allocations_by_proxy[(source, proxy_url)][allocation_id] = None
        if stat is None:
            return allocation_id
        stat["handout_count"] = int(stat.get("handout_count", 0) or 0) + 1
        stat["last_handed_out_ts"] = now_ts
        qualified = self._is_qualified(stat, now_ts)
        # Track the outstanding handout only where something reads it back: a
        # trial candidate, whose one-at-a-time rule the plan enforces, or a
        # deployment that has opted into a per-proxy concurrency cap. With the
        # cap off there is nothing to compare against, and appending to a list
        # nobody consults just grows it between syncs.
        # Every handout owns a lease even when the configured capacity is
        # unlimited; exact feedback must still be able to identify and close
        # only this request's allocation.
        self._grant_lease(stat, now_ts)
        # A paused source produces no usable evidence, so a handout made during
        # one must not spend the trial budget it would otherwise be judged on:
        # the guard exists to keep an outage from costing proxies their
        # reputation, and the trial budget is part of that reputation.
        if self.outage_states.get(source, {}).get("active"):
            return allocation_id
        if not qualified:
            stat["trial_handout_count"] = int(
                stat.get("trial_handout_count", 0) or 0
            ) + 1
            if stat["trial_handout_count"] >= self.probation_attempts:
                stat["retry_after_ts"] = now_ts + self.retry_delay_s
        return allocation_id

    def _is_eligible(
        self,
        source: str,
        proxy_url: str,
        now_ts: Optional[float] = None,
        apply_cooldown: bool = True,
    ) -> bool:
        """
        Whether this proxy may go into the serving plan.

        `apply_cooldown` is False for the exploit set, and that is not an
        oversight. Cooldown is a per-request spacing rule, but the plan is
        rebuilt on an interval that is longer than any sane cooldown, so
        filtering by it at build time excludes precisely the proxies that are
        getting traffic - the highest-scoring ones - for the whole life of the
        next plan. Measured on a 40-proxy pool at a 500ms cooldown, a burst
        left every one of the top ten out of the following plan and dropped
        the best servable score from 99.7 to 38.0. Per-proxy protection for
        qualified proxies is proxy_max_inflight, which is a concurrency limit
        and does not interact with plan staleness.
        """
        now_ts = time.time() if now_ts is None else now_ts
        if apply_cooldown and self.proxy_cooldown_ms > 0:
            last_handed_out = self.proxy_last_handed_out_ts.get(source, {})
            if (
                now_ts - last_handed_out.get(proxy_url, 0.0)
                < self.proxy_cooldown_ms / 1000
            ):
                return False
        stat = self.source_stats.get(source, {}).get(proxy_url)
        if stat is None:
            return True
        limit = self._inflight_limit(stat, now_ts)
        return limit <= 0 or self._lease_count(stat, now_ts) < limit

    def get_premium_proxy(self) -> Optional[str]:
        allocation = self.allocate_premium_proxy()
        return allocation["proxy"] if allocation else None

    def allocate_premium_proxy(self) -> Optional[Dict[str, str]]:
        """
        Allocate a premium proxy through the normal source/capacity contract.
        """
        with self.lock:
            self._cleanup_allocations_locked(time.time())
            candidates = []
            now_ts = time.time()
            for proxy_url in list(self.premium_proxies):
                source = self.premium_sources.get(proxy_url)
                stat = self.source_stats.get(source, {}).get(proxy_url) if source else None
                if (
                    source is None
                    or stat is None
                    or proxy_url not in self.active_proxies
                    or not self._is_qualified(stat, now_ts)
                    or not self._is_eligible(
                        source, proxy_url, now_ts, apply_cooldown=False
                    )
                ):
                    continue
                candidates.append((source, proxy_url))
            if not candidates:
                self._sync_premium_proxies_locked()
                candidates = [
                    (self.premium_sources[url], url)
                    for url in self.premium_proxies
                    if url in self.premium_sources
                    and self._is_eligible(
                        self.premium_sources[url], url, now_ts, apply_cooldown=False
                    )
                ]
            if not candidates:
                logger.warning("No premium proxies available.")
                return None

            source, selected = random.choice(candidates)
            allocation_id = self._mark_proxy_handed_out(
                source, selected, now_ts, premium=True
            )
            logger.debug(
                f"Premium proxy selected: {selected} "
                f"(pool size: {len(candidates)})"
            )
            return {
                "proxy": selected,
                "source": source,
                "allocation_id": allocation_id,
            }

    def _sync_premium_proxies_locked(self):
        # Aggregate all proxies with their highest score across all sources.
        battle_tested_scores: Dict[str, Tuple[float, str]] = {}

        for source, stats in self.source_stats.items():
            for proxy_url, stat in stats.items():
                usage_count = stat.get("success_count", 0) + stat.get("failure_count", 0)
                score = stat.get("score", 0)

                if usage_count >= self.premium_min_usage_count and self._is_qualified(stat):
                    if (
                        proxy_url not in battle_tested_scores
                        or score > battle_tested_scores[proxy_url][0]
                    ):
                        battle_tested_scores[proxy_url] = (score, source)

        active_battle_tested = {
            url: score
            for url, score in battle_tested_scores.items()
            if url in self.active_proxies
        }
        # Premium traffic requires qualified evidence; the old fallback routed
        # unproven proxies around the normal probation contract.
        score_pool = active_battle_tested
        if not score_pool:
            self.premium_proxies = []
            self.premium_sources = {}
            return

        sorted_proxies = sorted(
            score_pool.items(), key=lambda item: (-item[1][0], item[0])
        )
        chosen = sorted_proxies[: self.premium_pool_size]
        self.premium_proxies = [url for url, _ in chosen]
        self.premium_sources = {url: score_source[1] for url, score_source in chosen}

    def _flush_feedback_buffer(
        self,
        include_current: bool = False,
        deadline: Optional[float] = None,
    ) -> bool:
        """Flush minute aggregates, acknowledging them only after commit."""
        current_minute_start = datetime.now().replace(second=0, microsecond=0)
        if deadline is None:
            acquired = self.feedback_flush_lock.acquire()
        else:
            acquired = self.feedback_flush_lock.acquire(
                timeout=max(0.0, deadline - time.monotonic())
            )
        if not acquired:
            logger.warning("Feedback flush deferred: shutdown deadline exhausted.")
            return False
        try:
            with self.lock:
                if self.feedback_flush_pending is not None:
                    flush_id, records_to_flush = self.feedback_flush_pending
                else:
                    records_to_flush = []
                    for minute_timestamp, by_source in self.feedback_buffer.items():
                        if include_current or minute_timestamp < current_minute_start:
                            for source, counts in by_source.items():
                                success = int(counts.get("success", 0))
                                failure = int(counts.get("failure", 0))
                                if success or failure:
                                    records_to_flush.append(
                                        (minute_timestamp, source, success, failure)
                                    )
                    flush_id = str(uuid.uuid4())
                    if records_to_flush:
                        self.feedback_flush_pending = (
                            flush_id,
                            list(records_to_flush),
                        )

            if not records_to_flush:
                with self.lock:
                    self.last_flush_success_ts = time.time()
                logger.debug("Flush task found no eligible feedback aggregates.")
                return True

            try:
                if deadline is not None:
                    remaining_s = deadline - time.monotonic()
                    if remaining_s <= 0:
                        logger.warning(
                            "Feedback flush deferred: shutdown deadline exhausted."
                        )
                        return False
                if deadline is None:
                    committed = self.db.flush_feedback_stats(
                        records_to_flush,
                        flush_id,
                    )
                else:
                    committed = self.db.flush_feedback_stats(
                        records_to_flush,
                        flush_id,
                        deadline=deadline,
                    )
                if committed is not True:
                    raise DatabaseWriteError(
                        "flush_feedback_stats", RuntimeError("write returned false")
                    )
            except Exception:
                logger.exception(
                    "Feedback aggregate flush failed; snapshot remains queued."
                )
                return False

            # Feedback can arrive while the database transaction is in flight.
            # Subtract the committed snapshot instead of deleting the bucket so
            # those concurrent increments remain queued exactly once.
            with self.lock:
                for minute_timestamp, source, success, failure in records_to_flush:
                    counts = self.feedback_buffer[minute_timestamp][source]
                    counts["success"] -= success
                    counts["failure"] -= failure
                    if not counts["success"] and not counts["failure"]:
                        del self.feedback_buffer[minute_timestamp][source]
                    if not self.feedback_buffer[minute_timestamp]:
                        del self.feedback_buffer[minute_timestamp]
                self.feedback_flush_pending = None
                self.last_flush_success_ts = time.time()
            return True
        finally:
            self.feedback_flush_lock.release()

    def _migrate_legacy_stat(
        self, stat: Dict, trust_derived: bool = True
    ) -> Dict:
        """Normalize persisted input and install scoring-version-2 state."""
        if not isinstance(stat, dict):
            raise ValueError("Proxy stat must be a mapping")
        for unknown_key in stat.keys() - MIGRATABLE_STAT_KEYS:
            del stat[unknown_key]

        success_count = nonnegative_int(stat.get("success_count", 0))
        failure_count = nonnegative_int(stat.get("failure_count", 0))
        stat["success_count"] = success_count
        stat["failure_count"] = failure_count

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
        stat["handout_count"] = nonnegative_int(stat.get("handout_count", 0))
        stat["trial_handout_count"] = nonnegative_int(
            stat.get("trial_handout_count", 0)
        )
        stat["last_handed_out_ts"] = self._coerce_timestamp(
            stat.get("last_handed_out_ts"), now_ts
        )
        # Drop expired leases on the way in. A stat with no usable lease list
        # starts with none rather than inheriting one it cannot account for.
        leases = stat.get("inflight")
        if not isinstance(leases, list):
            leases = []
        else:
            leases = sorted(
                expiry
                for expiry in (self._coerce_timestamp(raw) for raw in leases)
                if expiry is not None and expiry > now_ts
            )
        stat["inflight"] = leases
        stat["retry_after_ts"] = (
            self._coerce_timestamp(stat.get("retry_after_ts")) or 0.0
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

    @staticmethod
    def _has_valid_derived_state(stat: Dict) -> bool:
        """
        Whether the estimator triple can be used without revalidation.

        Deliberately `type(...) is float` and a range test rather than the full
        coercion helpers: this runs once per proxy per sync and once per
        feedback event, and every value it reads was written as a float either
        by this class or by _migrate_legacy_stat() on the way in from a file.
        NaN and infinity both fail the range test, so nothing invalid slips
        through the fast path - it just routes to migration instead.
        """
        slow = stat.get("quality_slow")
        fast = stat.get("quality_fast")
        updated = stat.get("quality_updated_ts")
        return (
            type(slow) is float
            and 0.0 <= slow <= 1.0
            and type(fast) is float
            and 0.0 <= fast <= 1.0
            and type(updated) is float
            and updated >= 0.0
        )

    def _age_reliability_state(self, stat: Dict, target_ts: float):
        if self._has_valid_derived_state(stat):
            slow = stat["quality_slow"]
            fast = stat["quality_fast"]
            updated_ts = min(stat["quality_updated_ts"], target_ts)
        else:
            slow = self._coerce_probability(stat.get("quality_slow"))
            fast = self._coerce_probability(stat.get("quality_fast"))
            if slow is None or fast is None:
                slow = fast = self.reliability_prior
            updated_ts = self._coerce_timestamp(
                stat.get("quality_updated_ts"), target_ts
            )
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
        # Decide the anchor first: a seeded estimator is only meaningful if we
        # know how old the evidence behind it is. An absent or unusable
        # last_feedback_ts is unbounded age, not recent evidence, so the record
        # is aged all the way to the prior rather than trusted as fresh. The
        # raw counters are kept either way - it is the derived estimator that
        # cannot be reconstructed without a date.
        anchor_ts = self._coerce_timestamp(stat.get("last_feedback_ts"), now_ts)
        if anchor_ts is None:
            stat["quality_slow"] = self.reliability_prior
            stat["quality_fast"] = self.reliability_prior
            stat["quality_updated_ts"] = now_ts
        else:
            stat["quality_slow"] = seeded
            stat["quality_fast"] = seeded
            stat["quality_updated_ts"] = anchor_ts
        self._age_reliability_state(stat, now_ts)

    def _refresh_score(self, stat: Dict, source: str = None) -> float:
        """Age this proxy's estimators to now and return the resulting score."""
        if self._has_valid_derived_state(stat):
            self._age_reliability_state(stat, time.time())
        elif (
            self._coerce_probability(stat.get("quality_slow")) is None
            or self._coerce_probability(stat.get("quality_fast")) is None
            or self._coerce_timestamp(stat.get("quality_updated_ts")) is None
        ):
            self._migrate_legacy_stat(stat, trust_derived=False)
        else:
            self._age_reliability_state(stat, time.time())
        return float(stat["score"])

    # Fields the feedback path mutates, and therefore the only fields an
    # outage rollback has to restore. Snapshotting these by value is what lets
    # the guard protect a window without deep-copying every proxy\'s full
    # result history on every healthy feedback event.
    TENTATIVE_STAT_FIELDS = (
        "score",
        "quality_slow",
        "quality_fast",
        "quality_updated_ts",
        "success_count",
        "failure_count",
        "avg_latency_ms",
        "last_feedback_ts",
        "trial_handout_count",
        "retry_after_ts",
    )

    def _snapshot_stat(self, stat: Dict) -> Dict:
        snapshot = {field: stat.get(field) for field in self.TENTATIVE_STAT_FIELDS}
        # A bounded full window evicts its oldest row on append, so length alone
        # cannot restore it. Copy the small bounded list exactly.
        snapshot["recent_results"] = copy.deepcopy(stat.get("recent_results") or [])
        return snapshot

    def _apply_stat_snapshot(self, stat: Dict, snapshot: Dict) -> Dict:
        for field in self.TENTATIVE_STAT_FIELDS:
            stat[field] = snapshot.get(field)
        stat["recent_results"] = copy.deepcopy(snapshot.get("recent_results", []))
        return stat

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
                # EMA of completed-window success ratio; every threshold below
                # is a multiple of it.
                "baseline": None,
            },
        )

    def _outage_required_window(self, state: Dict) -> int:
        """
        How many observations a verdict needs, given the source's own baseline.

        A window is only evidence of an outage when a run this bad is less
        likely than outage_false_positive_budget under normal operation. At a
        90% success rate that takes 3 observations; at 10% it takes 66. A fixed
        window cannot serve both, and a fixed *ratio* threshold serves neither.
        """
        baseline = state.get("baseline")
        if baseline is None or baseline <= 0.0:
            return self.outage_window_size
        if baseline >= 1.0:
            return self.outage_window_size
        needed = math.ceil(
            math.log(self.outage_false_positive_budget) / math.log(1.0 - baseline)
        )
        return max(
            self.outage_window_size, min(needed, self.outage_window_max_size)
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
            state["protected_stats"][proxy_url] = self._snapshot_stat(
                self.source_stats[source][proxy_url]
            )
        state["observations"].append((proxy_url, is_success))

        required_window = self._outage_required_window(state)
        if len(state["observations"]) < required_window:
            if state["active"]:
                state["paused_updates"] += 1
            return bool(state["active"])

        window = state["observations"][:required_window]
        distinct = len({url for url, _ in window})
        success_ratio = sum(1 for _, ok in window if ok) / len(window)
        state["completed_windows"] += 1
        baseline = state.get("baseline")
        enough_proxies = distinct >= self.outage_min_distinct_proxies

        if state["active"]:
            state["paused_updates"] += 1
            # An outage window says nothing about normal operation, so it must
            # not drag the baseline it is being judged against down with it.
            if enough_proxies and baseline is not None and success_ratio >= (
                baseline * self.outage_recovery_baseline_ratio
            ):
                state["active"] = False
                state["previous_window_healthy"] = True
                state["last_transition_ts"] = current_timestamp
                logger.warning(
                    "Source outage guard recovered for '{}': success_ratio={:.3f} "
                    "(baseline {:.3f}), distinct_proxies={}.",
                    source,
                    success_ratio,
                    baseline,
                    distinct,
                )
            state["observations"] = []
            state["protected_stats"] = {}
            return True

        # The first completed window has nothing to compare against, so it
        # defines its own reference. A cold start that never succeeds yields a
        # zero reference, which the `> 0` gates below refuse - that is what
        # keeps a uniformly poor start from arming the guard.
        reference = success_ratio if baseline is None else baseline

        broad_failure = (
            state["previous_window_healthy"]
            and enough_proxies
            and reference > 0.0
            and success_ratio <= reference * self.outage_failure_baseline_ratio
        )
        if broad_failure:
            for protected_url, snapshot in state["protected_stats"].items():
                protected_stat = self.source_stats.get(source, {}).get(protected_url)
                if protected_stat is not None:
                    self._apply_stat_snapshot(protected_stat, snapshot)
            state["active"] = True
            state["paused_updates"] += len(window)
            state["last_transition_ts"] = current_timestamp
            logger.error(
                "Source outage guard activated for '{}': success_ratio={:.3f} is at "
                "or below {:.1%} of the {:.3f} baseline over {} observations from {} "
                "distinct proxies; rolled back {} tentative reputation update(s).",
                source,
                success_ratio,
                self.outage_failure_baseline_ratio,
                baseline,
                len(window),
                distinct,
                len(state["protected_stats"]),
            )
            paused = True
        else:
            # Only windows that are not outages describe normal operation, so
            # only they move the baseline.
            state["baseline"] = (
                success_ratio
                if baseline is None
                else (1.0 - self.outage_baseline_alpha) * baseline
                + self.outage_baseline_alpha * success_ratio
            )
            state["previous_window_healthy"] = (
                enough_proxies
                and reference > 0.0
                and success_ratio >= reference * self.outage_healthy_baseline_ratio
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
        allocation_id: Optional[str] = None,
    ) -> Dict[str, object]:
        """
        Process feedback for a proxy request with online reliability scoring.
        
        Updates:
        - Adds result to the bounded replay window (recent_results)
        - Updates exponential moving average of latency
        - Updates the slow and fast reliability estimators
        - Maintains historical counters for analytics
        """
        is_success = self.classify_feedback_status(status_code)
        if failure_kind and failure_kind not in VALID_FAILURE_KINDS:
            logger.warning("Ignoring unknown failure_kind '{}'", failure_kind)
            failure_kind = None

        current_minute = datetime.now().replace(second=0, microsecond=0)
        current_timestamp = time.time()

        with self.lock:
            accepted, reason, source = self._accept_feedback_allocation_locked(
                source, proxy_url, allocation_id, current_timestamp
            )
            if not accepted:
                self.rejected_feedback_total[reason] += 1
                return {"accepted": False, "reason": reason}

            # Aggregates and process-lifetime counters advance only after the
            # allocation contract has accepted this feedback.
            if is_success:
                self.feedback_buffer[current_minute][source]["success"] += 1
                self.accepted_feedback_success_total += 1
            else:
                self.feedback_buffer[current_minute][source]["failure"] += 1
                self.accepted_feedback_failure_total += 1

            source_reputation_paused = self._observe_source_outage_locked(
                source, proxy_url, is_success, current_timestamp
            )

            target_sources = [source]
            if not is_success and failure_kind == "dead":
                # A paused source has been judged unable to say anything
                # reliable about a proxy. Fanning its failures out to every
                # other source would let one target site's outage strip the
                # reputation those proxies earned everywhere else - the exact
                # damage the guard exists to prevent, just redirected.
                if source_reputation_paused:
                    target_sources = []
                else:
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
            return {"accepted": True, "reason": reason, "source": source}

    def _apply_feedback_to_stat(
        self,
        source: str,
        proxy_url: str,
        stat: Dict,
        is_success: bool,
        response_time_ms: Optional[int],
        current_timestamp: float,
    ):
        # Normalize only what is not already normalized. Every stat reaching
        # this path was built by _get_new_proxy_stat() or migrated by
        # restore_stats(); re-validating the whole record - including all 100
        # stored results - on every single feedback event was pure overhead.
        if not REQUIRED_STAT_KEYS <= stat.keys():
            stat = self._migrate_legacy_stat(stat)

        # Update historical counters
        if is_success:
            stat["success_count"] += 1
            
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

        old_score = stat["score"]
        self._update_reliability_state(stat, is_success, current_timestamp)
        if self._is_qualified(stat, current_timestamp):
            self._promote_to_exploit(source, proxy_url, stat)
            # Qualifying closes the trial epoch. Without this the trial budget
            # is only ever spent, never returned, so the first dip below the
            # prior - which a 20%-success proxy reaches on a routine losing
            # streak - found the budget already exhausted and exiled a proven
            # proxy outright instead of granting it the delayed retries.
            stat["trial_handout_count"] = 0
            stat["retry_after_ts"] = 0.0
        else:
            self._remove_from_exploit(source, proxy_url)
            self._remove_from_premium(source, proxy_url)
            if stat["trial_handout_count"] >= self.probation_attempts:
                stat["retry_after_ts"] = current_timestamp + self.retry_delay_s
        self._return_trial_candidate(source, proxy_url, stat)
        
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
