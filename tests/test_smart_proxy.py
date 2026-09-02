# -*- coding: utf-8 -*-
import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import configparser
import copy
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

from src.core.proxy_manager import ProxyManager, FetchError, FAILED_STATUS_CODES
from src.database.db import DatabaseManager


def write_config_file(directory: str, config_dict: dict, name: str = "config.ini") -> str:
    """
    Write a config dict to a real .ini file and return its path.

    Tests must load config through the real configparser path. Patching
    ConfigParser.read is not equivalent: read()'s return value is never used by
    configparser, so the patch leaves manager.config empty and every setting
    silently falls back to its hardcoded default - which means the config values
    under test are never actually exercised.
    """
    config = configparser.ConfigParser()
    config.read_dict(config_dict)
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as f:
        config.write(f)
    return path


class ProxyManagerTestBase(unittest.TestCase):
    """Shared fixture: a ProxyManager built from a real ini file, mocked DB."""

    def setUp(self):
        """Set up a mock environment for each test."""
        self.config_dict = (
            {
                "database": {
                    "host": "localhost",
                    "port": "5432",
                    "dbname": "test",
                    "user": "user",
                    "password": "password",
                },
                "server": {"port": "6942"},
                "validator": {
                    "validation_workers": "10",
                    "validation_timeout_s": "5",
                    "validation_target": "http://mocktarget.com",
                    "validation_supplement_threshold": "1000",  # Match default
                    "validation_window_minutes": "30",
                    "max_validations_per_window": "5",
                },
                "scheduler": {
                    "validation_interval_seconds": "60",
                    "stats_flush_interval_seconds": "60",
                    "source_refresh_interval_seconds": "300",
                },
                "sources": {
                    "predefined_sources": "source1,source2",
                    "default_source": "source1",
                },
                "source_pool": {
                    "max_pool_size": "100",
                    "stats_pool_max_multiplier": "10",
                    "weighted_selection_enabled": "false",
                    "selection_strategy": "uniform",
                    "proxy_cooldown_ms": "0",
                    "top_tier_size": "50",
                    "top_tier_load_percentage": "70",
                    "elo_time_decay_enabled": "true",
                    "elo_decay_half_life_hours": "24",
                    "elo_max_result_age_hours": "168",
                    "max_feedback_latency_ms": "86400000",
                },
                "backup": {
                    "stats_backup_enabled": "false",
                },
                "proxy_source_A": {
                    "url": "http://source-a.com",
                    "update_interval_minutes": "10",
                },
            }
        )

        self.tmp_dir = tempfile.mkdtemp(prefix="smartproxy-test-")
        self.addCleanup(shutil.rmtree, self.tmp_dir, True)
        self.config_path = write_config_file(self.tmp_dir, self.config_dict)

        # PATCH THE IMPORT IN PROXY_MANAGER, NOT THE DEFINITION
        patcher_db = patch("src.core.proxy_manager.DatabaseManager", spec=DatabaseManager)
        self.addCleanup(patcher_db.stop)
        self.MockDatabaseManager = patcher_db.start()

        self.manager = ProxyManager(self.config_path)
        self.mock_db_instance = self.MockDatabaseManager.return_value
        # Default to "no persisted history"; tests about reputation
        # persistence override this with real rows.
        self.mock_db_instance.get_active_feedback_history.return_value = {}

    def make_manager(self, overrides: dict, name: str = "override.ini") -> ProxyManager:
        """Build a second manager from a real ini file with merged overrides."""
        merged = {
            section: dict(options) for section, options in self.config_dict.items()
        }
        for section, options in overrides.items():
            merged.setdefault(section, {}).update(options)
        path = write_config_file(self.tmp_dir, merged, name=name)
        return ProxyManager(path)

    def make_stat(self, results, **extra):
        """Build a stat whose sliding window holds `results` of (success, latency_ms)."""
        stat = self.manager._get_new_proxy_stat()
        now = time.time()
        for index, (is_success, latency_ms) in enumerate(results):
            stat["recent_results"].append([now - index, is_success, latency_ms])
        stat.update(extra)
        return stat


class TestProxyManager(ProxyManagerTestBase):

    # ========== Config Loading Tests ==========

    def test_load_allowed_ips_from_new_key(self):
        """allowed_ips should be loaded from [server]."""
        if not self.manager.config.has_section("server"):
            self.manager.config.add_section("server")
        self.manager.config.set("server", "allowed_ips", "10.0.0.1, 10.0.0.2")

        self.manager._load_config()

        self.assertEqual(self.manager.allowed_ips, ["10.0.0.1", "10.0.0.2"])

    def test_load_allowed_ips_falls_back_to_legacy_key(self):
        """Fallback to allowed_dashboard_ips for backward compatibility."""
        if not self.manager.config.has_section("server"):
            self.manager.config.add_section("server")
        self.manager.config.remove_option("server", "allowed_ips")
        self.manager.config.set("server", "allowed_dashboard_ips", "192.168.0.10")

        self.manager._load_config()

        self.assertEqual(self.manager.allowed_ips, ["192.168.0.10"])

    # ========== Core Proxy Selection Tests ==========
    
    def test_get_proxy_returns_proxy_from_pool(self):
        """Test get_proxy returns a proxy when available."""
        self.manager.available_proxies["source1"] = {
            "top_tier": ["http://1.1.1.1:80", "http://2.2.2.2:80"],
            "bottom_tier": ["http://3.3.3.3:80"],
        }
        self.manager.predefined_sources.add("source1")
        
        proxy = self.manager.get_proxy("source1")
        
        self.assertIn(proxy, ["http://1.1.1.1:80", "http://2.2.2.2:80", "http://3.3.3.3:80"])

    def test_get_proxy_returns_none_when_empty(self):
        """Test get_proxy returns None when no proxies available."""
        self.manager.available_proxies["source1"] = {
            "top_tier": [],
            "bottom_tier": [],
        }
        
        proxy = self.manager.get_proxy("source1")
        
        self.assertIsNone(proxy)

    def test_get_proxy_uses_default_source_for_unknown(self):
        """Test get_proxy falls back to default source for unknown source."""
        self.manager.available_proxies["source1"] = {
            "top_tier": ["http://1.1.1.1:80"],
            "bottom_tier": [],
        }
        self.manager.predefined_sources.add("source1")
        self.manager.default_source = "source1"
        
        proxy = self.manager.get_proxy("unknown_source")
        
        self.assertEqual(proxy, "http://1.1.1.1:80")

    def test_get_proxy_respects_cooldown(self):
        """Recently handed out proxies should be skipped until cooldown expires."""
        now = __import__("time").time()
        self.manager.proxy_cooldown_ms = 10000
        self.manager.predefined_sources.add("source1")
        self.manager.available_proxies["source1"] = {
            "top_tier": ["http://1.1.1.1:80", "http://2.2.2.2:80"],
            "bottom_tier": [],
        }
        self.manager.proxy_last_handed_out_ts["source1"]["http://1.1.1.1:80"] = now

        proxy = self.manager.get_proxy("source1")

        self.assertEqual(proxy, "http://2.2.2.2:80")

    def test_weighted_selection_uses_scores(self):
        """Weighted strategy should pass score-derived weights to random.choices."""
        self.manager.selection_strategy = "weighted"
        self.manager.predefined_sources.add("source1")
        self.manager.available_proxies["source1"] = {
            "top_tier": ["http://1.1.1.1:80", "http://2.2.2.2:80"],
            "bottom_tier": [],
        }
        self.manager.source_stats["source1"] = {
            "http://1.1.1.1:80": {"score": 10},
            "http://2.2.2.2:80": {"score": 90},
        }

        with patch("src.core.proxy_manager.random.choices", return_value=["http://2.2.2.2:80"]) as choices:
            proxy = self.manager.get_proxy("source1")

        self.assertEqual(proxy, "http://2.2.2.2:80")
        self.assertEqual(choices.call_args.kwargs["weights"], [10.0, 90.0])

    def test_get_premium_proxy_returns_proxy_when_available(self):
        """Test get_premium_proxy returns a proxy when premium pool is available."""
        self.manager.premium_proxies = ["http://premium1:80", "http://premium2:80"]
        
        proxy = self.manager.get_premium_proxy()
        
        self.assertIn(proxy, self.manager.premium_proxies)

    def test_get_premium_proxy_returns_none_when_empty(self):
        """Test get_premium_proxy returns None when premium pool is empty."""
        self.manager.premium_proxies = []
        
        proxy = self.manager.get_premium_proxy()
        
        self.assertIsNone(proxy)

    # ========== Feedback Processing Tests ==========
    
    def test_process_feedback_updates_score_on_success(self):
        """Test that process_feedback correctly updates proxy score on success."""
        proxy_url = "http://1.1.1.1:80"
        self.manager.predefined_sources.add("source1")
        self.manager.source_stats["source1"] = {
            proxy_url: {
                "score": 50.0,
                "success_count": 5,
                "failure_count": 1,
                "consecutive_failures": 0,
                "recent_results": [],
                "avg_latency_ms": None,
            }
        }
        
        self.manager.process_feedback("source1", proxy_url, 200, response_time_ms=500)
        
        stat = self.manager.source_stats["source1"][proxy_url]
        # Score should be recalculated based on new sliding window
        self.assertGreaterEqual(stat["score"], 0)
        self.assertLessEqual(stat["score"], 100)
        self.assertEqual(stat["success_count"], 6)
        self.assertEqual(stat["consecutive_failures"], 0)
        self.assertEqual(len(stat["recent_results"]), 1)

    def test_process_feedback_updates_score_on_failure(self):
        """Test that process_feedback correctly updates proxy score on failure."""
        proxy_url = "http://1.1.1.1:80"
        self.manager.predefined_sources.add("source1")
        self.manager.source_stats["source1"] = {
            proxy_url: {
                "score": 50.0,
                "success_count": 5,
                "failure_count": 1,
                "consecutive_failures": 0,
                "recent_results": [],
                "avg_latency_ms": None,
            }
        }
        
        # 0 is in FAILED_STATUS_CODES
        self.manager.process_feedback("source1", proxy_url, 0)
        
        stat = self.manager.source_stats["source1"][proxy_url]
        # Score recalculated based on sliding window (now has 1 failure)
        self.assertGreaterEqual(stat["score"], 0)
        self.assertLessEqual(stat["score"], 100)
        self.assertEqual(stat["failure_count"], 2)
        self.assertEqual(stat["consecutive_failures"], 1)
        self.assertEqual(len(stat["recent_results"]), 1)

    def test_feedback_status_classification(self):
        """Feedback status should reject unknown values and classify HTTP failures."""
        self.assertTrue(self.manager.classify_feedback_status(200))
        self.assertTrue(self.manager.classify_feedback_status(2))
        self.assertFalse(self.manager.classify_feedback_status(0))
        self.assertFalse(self.manager.classify_feedback_status(500))
        self.assertFalse(self.manager.is_valid_feedback_status(999))

    def test_process_feedback_handles_unknown_proxy(self):
        """Test that process_feedback handles unknown proxy gracefully."""
        self.manager.predefined_sources.add("source1")
        self.manager.source_stats["source1"] = {}
        
        # Should not raise
        self.manager.process_feedback("source1", "http://unknown:80", 200)

    # ========== Lock Tests ==========
    
    def test_rlock_is_reentrant(self):
        """Test that main lock is RLock and reentrant."""
        self.assertIsInstance(self.manager.lock, type(threading.RLock()))
        
        # Should not deadlock
        with self.manager.lock:
            with self.manager.lock:
                pass

    # ========== Source Management Tests ==========
    
    def test_get_source_or_default_returns_source_if_defined(self):
        """Test _get_source_or_default returns source if it's predefined."""
        self.manager.predefined_sources = {"source1", "source2"}
        
        result = self.manager._get_source_or_default("source1")
        
        self.assertEqual(result, "source1")

    def test_get_source_or_default_returns_default_if_unknown(self):
        """Test _get_source_or_default returns default for unknown source."""
        self.manager.predefined_sources = {"source1"}
        self.manager.default_source = "source1"
        
        result = self.manager._get_source_or_default("unknown")
        
        self.assertEqual(result, "source1")

    def test_update_dashboard_sources_uses_only_distinct_stat_sources(self):
        """Dashboard source list should come from stats unique sources only."""
        self.manager.predefined_sources = {"source1", "source2"}
        self.mock_db_instance.get_distinct_sources.return_value = ["legacy_source", "source1"]

        self.manager._update_dashboard_sources()

        self.assertEqual(self.manager.dashboard_sources, {"legacy_source", "source1"})

    def test_update_dashboard_sources_filters_internal_fetcher_names(self):
        """Internal fetcher section names should not leak to dashboard source options."""
        self.mock_db_instance.get_distinct_sources.return_value = [
            "default",
            "proxy_source_socks5_list_B",
            "proxy_source_A",
            "",
        ]

        self.manager._update_dashboard_sources()

        self.assertEqual(self.manager.dashboard_sources, {"default"})

    def test_update_dashboard_sources_preserves_cache_on_query_failure(self):
        self.manager.dashboard_sources = {"last-known-good"}
        self.mock_db_instance.get_distinct_sources.return_value = None

        with patch("src.core.proxy_manager.logger.warning") as warning:
            refreshed = self.manager._update_dashboard_sources()

        self.assertFalse(refreshed)
        self.assertEqual(self.manager.dashboard_sources, {"last-known-good"})
        warning.assert_called_once()

    def test_update_dashboard_sources_clears_cache_after_successful_empty_query(self):
        self.manager.dashboard_sources = {"last-known-good"}
        self.mock_db_instance.get_distinct_sources.return_value = []

        refreshed = self.manager._update_dashboard_sources()

        self.assertTrue(refreshed)
        self.assertEqual(self.manager.dashboard_sources, set())

    # ========== Validation Cycle Tests ==========
    
    def test_validation_cycle_skips_when_already_validating(self):
        """Test that validation cycle is skipped if already in progress."""
        self.manager.is_validating = True
        
        self.manager._run_validation_cycle()
        
        # Should not have called DB methods
        self.mock_db_instance.get_proxies_to_validate.assert_not_called()

    def test_validation_cycle_skips_when_no_proxies(self):
        """Test that validation cycle is skipped when no proxies to validate."""
        self.mock_db_instance.get_proxies_to_validate.return_value = []
        self.mock_db_instance.get_eligible_failed_proxies.return_value = []
        
        self.manager._run_validation_cycle()
        
        # Should not have updated counters
        self.mock_db_instance.update_validation_counters.assert_not_called()

    def test_validation_cycle_supplements_when_below_threshold(self):
        """Test validation cycle supplements with failed proxies when below threshold."""
        initial_proxies = [
            {"id": 1, "protocol": "http", "ip": "1.1.1.1", "port": 80},
        ]
        self.mock_db_instance.get_proxies_to_validate.return_value = initial_proxies
        self.mock_db_instance.get_eligible_failed_proxies.return_value = [
            {"id": 2, "protocol": "http", "ip": "2.2.2.2", "port": 80},
        ]
        
        async def mock_validate_batch(proxies):
            return [], [p["id"] for p in proxies]
        
        with patch.object(self.manager, "_validate_proxies_batch_async", side_effect=mock_validate_batch):
            self.manager._run_validation_cycle()
        
        # Should have called get_eligible_failed_proxies since we're below threshold
        self.mock_db_instance.get_eligible_failed_proxies.assert_called_once()

    def test_sync_keeps_existing_pool_when_active_proxy_query_fails(self):
        """A transient DB read failure should not clear the in-memory pool."""
        self.manager.active_proxies = {"http://1.1.1.1:80"}
        self.manager.available_proxies["source1"] = {
            "top_tier": ["http://1.1.1.1:80"],
            "bottom_tier": [],
        }
        self.mock_db_instance.get_active_proxies.return_value = None

        self.manager._sync_and_select_top_proxies()

        self.assertEqual(self.manager.active_proxies, {"http://1.1.1.1:80"})
        self.assertEqual(
            self.manager.available_proxies["source1"]["top_tier"],
            ["http://1.1.1.1:80"],
        )

    # ========== Stats Proxy Helper Tests ==========
    
    def test_get_new_proxy_stat_returns_correct_structure(self):
        """Test _get_new_proxy_stat returns correct initial ELO structure."""
        stat = self.manager._get_new_proxy_stat()
        
        self.assertEqual(stat["score"], 50.0)  # ELO neutral starting score
        self.assertEqual(stat["success_count"], 0)
        self.assertEqual(stat["failure_count"], 0)
        self.assertEqual(stat["consecutive_failures"], 0)
        self.assertEqual(stat["recent_results"], [])  # NEW: sliding window
        self.assertIsNone(stat["avg_latency_ms"])     # NEW: latency tracking

    # ========== FAILED_STATUS_CODES Tests ==========
    
    def test_failed_status_codes_contains_expected_values(self):
        """Test FAILED_STATUS_CODES contains timeout and proxy error codes."""
        self.assertIn(0, FAILED_STATUS_CODES)  # Timeout
        self.assertIn(4, FAILED_STATUS_CODES)  # Proxy error

    # ========== ELO Scoring Algorithm Tests ==========

    def test_elo_score_new_proxy_baseline(self):
        """Test ELO score for a completely new proxy with no history."""
        stat = self.manager._get_new_proxy_stat()
        score = self.manager._calculate_elo_score(stat)
        # New proxy should get neutral score of 50
        self.assertEqual(score, 50.0)

    def test_elo_score_perfect_proxy(self):
        """Test ELO score for a proxy with 100% success rate and low latency."""
        import time
        stat = self.manager._get_new_proxy_stat()
        # Simulate 50 perfect requests with low latency (200ms)
        for i in range(50):
            stat["recent_results"].append([time.time() - i, True, 200])
        
        score = self.manager._calculate_elo_score(stat)
        # Should get max or near-max score: 60 (success) + 30 (latency) + 10 (consistency)
        self.assertGreaterEqual(score, 95)
        self.assertLessEqual(score, 100)

    def test_elo_score_failing_proxy(self):
        """Test ELO score for a proxy with 0% success rate."""
        import time
        stat = self.manager._get_new_proxy_stat()
        # Simulate 50 failed requests
        for i in range(50):
            stat["recent_results"].append([time.time() - i, False, None])
        
        score = self.manager._calculate_elo_score(stat)
        # Should get low score: 0 (success) + 15 (neutral latency) + 0 (consistency)
        self.assertLessEqual(score, 20)

    def test_elo_score_80_percent_success_rate(self):
        """Test ELO score for a proxy with 80% success rate."""
        import time
        stat = self.manager._get_new_proxy_stat()
        # Simulate 40 successes and 10 failures
        for i in range(40):
            stat["recent_results"].append([time.time() - i, True, 500])
        for i in range(10):
            stat["recent_results"].append([time.time() - 40 - i, False, None])
        
        score = self.manager._calculate_elo_score(stat)
        # 80% success = 48pts, latency ~26pts, consistency varies
        self.assertGreaterEqual(score, 60)
        self.assertLessEqual(score, 85)

    def test_elo_score_latency_impact(self):
        """
        Lower latency scores higher, at latencies these proxies really have.

        The two samples sit inside the observed free-proxy band (8-33s). The
        200ms/1500ms pair this used to compare is below latency_full_score_ms
        now, where both ends score full marks and the component says nothing -
        which is the mirror image of the bug being fixed, where the calibration
        was so tight that every real proxy scored zero.
        """
        import time

        stat_low = self.manager._get_new_proxy_stat()
        for i in range(50):
            stat_low["recent_results"].append([time.time() - i, True, 8000])

        stat_high = self.manager._get_new_proxy_stat()
        for i in range(50):
            stat_high["recent_results"].append([time.time() - i, True, 25000])

        score_low = self.manager._calculate_elo_score(stat_low)
        score_high = self.manager._calculate_elo_score(stat_high)

        # Low latency should score higher
        self.assertGreater(score_low, score_high)
        # Difference should be meaningful (about 10-20 points)
        self.assertGreater(score_low - score_high, 8)

    def test_elo_score_legacy_migration(self):
        """Test that legacy stats are properly migrated and scored."""
        # Legacy stat format (no recent_results)
        legacy_stat = {
            "score": 150.0,  # Old unbounded score
            "success_count": 80,
            "failure_count": 20,
            "consecutive_failures": 0,
        }
        
        migrated = self.manager._migrate_legacy_stat(legacy_stat)
        
        # Should have new fields
        self.assertIn("recent_results", migrated)
        self.assertIn("avg_latency_ms", migrated)
        self.assertEqual(migrated["recent_results"], [])
        
        # Score should be recalculated based on historical success rate (80%)
        self.assertGreaterEqual(migrated["score"], 70)
        self.assertLessEqual(migrated["score"], 90)

    def test_elo_score_consistency_bonus(self):
        """Test that consistent recent performance gives bonus points."""
        import time
        
        # Consistent proxy: 10/10 recent successes
        stat_consistent = self.manager._get_new_proxy_stat()
        for i in range(50):
            stat_consistent["recent_results"].append([time.time() - i, True, 400])
        
        # Inconsistent proxy: 5/10 recent successes (50%)
        stat_inconsistent = self.manager._get_new_proxy_stat()
        for i in range(25):
            stat_inconsistent["recent_results"].append([time.time() - i, True, 400])
        for i in range(25):
            stat_inconsistent["recent_results"].append([time.time() - 25 - i, False, None])
        
        score_consistent = self.manager._calculate_elo_score(stat_consistent)
        score_inconsistent = self.manager._calculate_elo_score(stat_inconsistent)
        
        # Consistent should score significantly higher
        self.assertGreater(score_consistent, score_inconsistent)
        self.assertGreater(score_consistent - score_inconsistent, 30)

    def test_elo_score_bounded_0_to_100(self):
        """Test that ELO scores are always bounded between 0 and 100."""
        import time
        
        # Test extreme cases
        test_cases = [
            # Perfect case
            [(True, 100)] * 100,
            # Worst case
            [(False, None)] * 100,
            # Mixed case
            [(True, 500)] * 50 + [(False, None)] * 50,
        ]
        
        for results in test_cases:
            stat = self.manager._get_new_proxy_stat()
            for success, latency in results:
                stat["recent_results"].append([time.time(), success, latency])
            
            score = self.manager._calculate_elo_score(stat)
            self.assertGreaterEqual(score, 0, "Score below 0")
            self.assertLessEqual(score, 100, "Score above 100")

    def test_elo_score_ignores_stale_recent_results(self):
        """Old recent results should age out and return neutral score when no fresh data exists."""
        import time

        self.manager.elo_time_decay_enabled = True
        self.manager.elo_max_result_age_hours = 24
        stat = self.manager._get_new_proxy_stat()

        stale_ts = time.time() - (3 * 24 * 3600)
        for _ in range(50):
            stat["recent_results"].append([stale_ts, True, 200])

        score = self.manager._calculate_elo_score(stat)
        self.assertEqual(score, 50.0)

    def test_elo_score_prefers_fresh_failures_over_old_successes(self):
        """Recent failures should dominate when old successes are out of age window."""
        import time

        self.manager.elo_time_decay_enabled = True
        self.manager.elo_max_result_age_hours = 24
        stat = self.manager._get_new_proxy_stat()

        old_ts = time.time() - (8 * 24 * 3600)
        for _ in range(40):
            stat["recent_results"].append([old_ts, True, 200])

        for _ in range(10):
            stat["recent_results"].append([time.time(), False, None])

        score = self.manager._calculate_elo_score(stat)
        self.assertLessEqual(score, 25)

    def test_elo_score_historical_data_decays_toward_neutral(self):
        """Historical counters should decay toward neutral when feedback is very old."""
        import time

        self.manager.elo_time_decay_enabled = True
        self.manager.elo_decay_half_life_hours = 24

        stat = self.manager._get_new_proxy_stat()
        stat["success_count"] = 80
        stat["failure_count"] = 20
        stat["last_feedback_ts"] = time.time() - (10 * 24 * 3600)

        score = self.manager._calculate_elo_score(stat)
        self.assertGreaterEqual(score, 49)
        self.assertLessEqual(score, 52)

    def test_elo_score_historical_without_timestamp_is_neutral(self):
        """If historical data has no timestamp, treat it as stale under decay mode."""
        self.manager.elo_time_decay_enabled = True

        stat = self.manager._get_new_proxy_stat()
        stat["success_count"] = 95
        stat["failure_count"] = 5
        stat["last_feedback_ts"] = None

        score = self.manager._calculate_elo_score(stat)
        self.assertEqual(score, 50.0)


class TestIssue13PoolQuality(ProxyManagerTestBase):
    """
    Regression tests for issue #13: defects that made pool output quality
    degrade monotonically with uptime. One test (at least) per finding.
    """

    # ---------- Finding 1: dead proxies kept being handed out ----------

    def test_sync_excludes_dead_proxies_from_tiers(self):
        """Only proxies the DB reports as alive may enter the usable tiers."""
        dead = "http://dead:80"
        alive = "http://alive:80"
        self.manager.source_stats["source1"] = {
            # The dead proxy outscores the live one, so score ordering alone
            # would place it first.
            dead: self.manager._get_new_proxy_stat() | {"score": 88.0},
            alive: self.manager._get_new_proxy_stat() | {"score": 51.0},
        }
        self.mock_db_instance.get_active_proxies.return_value = {alive}

        self.manager._sync_and_select_top_proxies()

        tiers = self.manager.available_proxies["source1"]
        self.assertNotIn(dead, tiers["top_tier"])
        self.assertNotIn(dead, tiers["bottom_tier"])
        self.assertIn(alive, tiers["top_tier"])

    def test_sync_keeps_dead_proxy_history_in_stats(self):
        """A dead proxy loses its pool slot but keeps its score history."""
        dead = "http://dead:80"
        self.manager.source_stats["source1"] = {
            dead: self.manager._get_new_proxy_stat() | {
                "score": 88.0,
                "success_count": 40,
                "last_feedback_ts": time.time(),
            }
        }
        self.mock_db_instance.get_active_proxies.return_value = set()

        self.manager._sync_and_select_top_proxies()

        self.assertIn(dead, self.manager.source_stats["source1"])
        self.assertEqual(
            self.manager.source_stats["source1"][dead]["success_count"], 40
        )

    def test_get_proxy_never_returns_a_dead_proxy_after_sync(self):
        """End-to-end: 20 get_proxy calls after a sync never hit a dead proxy."""
        dead = "http://dead:80"
        alive = "http://alive:80"
        self.manager.source_stats["source1"] = {
            dead: self.manager._get_new_proxy_stat() | {"score": 99.0},
            alive: self.manager._get_new_proxy_stat() | {"score": 50.0},
        }
        self.mock_db_instance.get_active_proxies.return_value = {alive}
        self.manager._sync_and_select_top_proxies()

        handed_out = {self.manager.get_proxy("source1") for _ in range(20)}

        self.assertNotIn(dead, handed_out)
        self.assertEqual(handed_out, {alive})

    # ---------- Finding 2: one observation swung the score too far ----------

    def test_single_success_scores_between_baseline_and_veteran(self):
        """
        One success must stay optimistic (> the 50 baseline) but must not
        outrank a proxy with a full window of successes.
        """
        rookie = self.manager._calculate_elo_score(self.make_stat([(True, 200)]))
        veteran = self.manager._calculate_elo_score(
            self.make_stat([(True, 200)] * 48 + [(False, None)] * 2)
        )
        untried = self.manager._calculate_elo_score(
            self.manager._get_new_proxy_stat()
        )

        self.assertEqual(untried, 50.0)
        self.assertGreater(rookie, 50.0)
        self.assertLess(rookie, veteran)
        # Exploration budget, not a coronation: issue #13 pins 1 success to 50-75.
        self.assertLessEqual(rookie, 75.0)
        self.assertGreaterEqual(veteran, 90.0)

    def test_single_success_without_latency_still_beats_baseline(self):
        """The optimistic bonus must not depend on a latency being reported."""
        score = self.manager._calculate_elo_score(self.make_stat([(True, None)]))

        self.assertGreater(score, 50.0)
        self.assertLessEqual(score, 75.0)

    def test_single_failure_is_not_permanent_exile(self):
        """One failure lands well above 0, leaving a recovery path."""
        score = self.manager._calculate_elo_score(self.make_stat([(False, None)]))

        self.assertGreater(score, 20.0)
        self.assertLess(score, 50.0)

    # ---------- Finding 5: an all-failure window got a neutral latency score ----------

    def test_all_failures_get_no_neutral_latency_credit(self):
        """A window with results but zero successes scores near zero."""
        score = self.manager._calculate_elo_score(
            self.make_stat([(False, None)] * 50)
        )

        self.assertLessEqual(score, 5.0)

    def test_untried_proxy_keeps_the_neutral_baseline(self):
        """The neutral score belongs to proxies with no data, not broken ones."""
        self.assertEqual(
            self.manager._calculate_elo_score(self.manager._get_new_proxy_stat()),
            50.0,
        )

    # ---------- Finding 4: latency masked a low success rate ----------

    def test_low_success_rate_cannot_hide_behind_low_latency(self):
        """A fast but unreliable proxy must rank below an untried one."""
        score = self.manager._calculate_elo_score(
            self.make_stat([(True, 200)] * 17 + [(False, None)] * 33)
        )

        self.assertLess(score, 50.0)

    def test_latency_score_is_scaled_by_success_rate(self):
        """Same latency, different reliability: the reliable one scores higher."""
        reliable = self.manager._calculate_elo_score(
            self.make_stat([(True, 200)] * 45 + [(False, None)] * 5)
        )
        flaky = self.manager._calculate_elo_score(
            self.make_stat([(True, 200)] * 20 + [(False, None)] * 30)
        )

        self.assertGreater(reliable - flaky, 30.0)

    # ---------- Finding 3: time decay never applied to idle proxies ----------

    def test_sync_rescores_idle_proxies_so_decay_applies(self):
        """
        A proxy whose only results are stale must have its frozen score
        recomputed by the sync, not carried forever.
        """
        proxy_url = "http://idle:80"
        stale_ts = time.time() - (10 * 24 * 3600)
        stat = self.manager._get_new_proxy_stat()
        stat["recent_results"] = [[stale_ts, True, 200] for _ in range(50)]
        stat["last_feedback_ts"] = stale_ts
        stat["score"] = 100.0  # frozen from when the results were fresh
        self.manager.source_stats["source1"] = {proxy_url: stat}
        self.mock_db_instance.get_active_proxies.return_value = {proxy_url}

        score_before = self.manager.source_stats["source1"][proxy_url]["score"]
        self.manager._sync_and_select_top_proxies()
        score_after = self.manager.source_stats["source1"][proxy_url]["score"]

        self.assertEqual(score_before, 100.0)
        self.assertNotEqual(score_after, score_before)
        self.assertEqual(score_after, 50.0)

    def test_sync_rescore_can_be_disabled_by_config(self):
        """rescore_on_sync_enabled is a real config switch, not a hardcode."""
        manager = self.make_manager(
            {"source_pool": {"rescore_on_sync_enabled": "false"}},
            name="no_rescore.ini",
        )
        proxy_url = "http://idle:80"
        stale_ts = time.time() - (10 * 24 * 3600)
        stat = manager._get_new_proxy_stat()
        stat["recent_results"] = [[stale_ts, True, 200] for _ in range(50)]
        stat["last_feedback_ts"] = stale_ts
        stat["score"] = 100.0
        manager.source_stats["source1"] = {proxy_url: stat}
        manager.db.get_active_proxies.return_value = {proxy_url}

        manager._sync_and_select_top_proxies()

        self.assertFalse(manager.rescore_on_sync_enabled)
        self.assertEqual(manager.source_stats["source1"][proxy_url]["score"], 100.0)

    # ---------- Finding 8: one dirty row rolled back the whole insert ----------

    def test_parser_rejects_malformed_proxy_lines(self):
        """Values that violate the DB column constraints are dropped, not stored."""
        cases = {
            # (line, default_protocol): expected
            ("1.2.3.4:99999999999", "http"): None,       # port > PG INT / 65535
            ("averylongprotocolname://5.6.7.8:80", None): None,  # > VARCHAR(10)
            ("socks5://user:pass@9.9.9.9:1080", None): None,     # credentials in ip
            ("1.2.3.4:0", "http"): None,                 # port below range
            ("1.2.3.4:-1", "http"): None,
            ("1.2.3.4:notaport", "http"): None,
            ("1.2.3.4", "http"): None,                   # no port at all
            ("1.2.3.4:8080", None): None,                # no protocol available
            ("http://1.2.3.4:8080", None): ("http", "1.2.3.4", 8080),
            ("1.2.3.4:8080", "http"): ("http", "1.2.3.4", 8080),
            ("SOCKS5://1.2.3.4:1080", None): ("socks5", "1.2.3.4", 1080),
            ("1.2.3.4:65535", "http"): ("http", "1.2.3.4", 65535),
        }
        for (line, default_protocol), expected in cases.items():
            with self.subTest(line=line):
                self.assertEqual(
                    self.manager._parse_proxy_line(line, default_protocol), expected
                )

    def test_fetch_keeps_valid_lines_and_drops_dirty_ones(self):
        """The three reproduced dirty rows are dropped; the clean row survives."""
        payload = "\n".join(
            [
                "1.2.3.4:99999999999",
                "averylongprotocolname://5.6.7.8:80",
                "socks5://user:pass@9.9.9.9:1080",
                "7.7.7.7:8080",
            ]
        )
        job = {
            "name": "proxy_source_dirty",
            "url": "http://example.invalid/list.txt",
            "default_protocol": "http",
            "interval_minutes": 10,
            "last_run": 0,
        }

        with patch.object(self.manager, "_fetch_source_text", return_value=payload):
            parsed = self.manager._fetch_and_parse_source(job)

        self.assertEqual(parsed, [("http", "7.7.7.7", 8080)])

    def test_long_ip_is_rejected(self):
        """The ip column is VARCHAR(45); anything longer would abort the batch."""
        long_host = "a" * 46
        self.assertIsNone(
            self.manager._parse_proxy_line(f"{long_host}:8080", "http")
        )

    # ---------- Finding 9: empty batch skipped the pool refresh ----------

    def test_empty_validation_batch_still_refreshes_pool(self):
        """No proxies to validate is not a reason to keep serving a stale pool."""
        self.mock_db_instance.get_new_proxies_to_validate.return_value = []
        self.mock_db_instance.get_active_proxies_to_revalidate.return_value = []
        self.mock_db_instance.get_eligible_failed_proxies.return_value = []
        self.mock_db_instance.get_active_proxies.return_value = set()

        self.manager._run_validation_cycle()

        self.mock_db_instance.update_validation_counters.assert_not_called()
        self.assertEqual(self.mock_db_instance.get_active_proxies.call_count, 1)

    # ---------- Finding 14: new proxies starved the re-validation queue ----------

    def test_validation_budget_is_split_between_new_and_revalidation(self):
        """Each population gets its own share of validation_batch_limit."""
        self.manager.validation_batch_limit = 100
        self.manager.validation_new_proxy_ratio = 0.5
        self.mock_db_instance.get_new_proxies_to_validate.return_value = []
        self.mock_db_instance.get_active_proxies_to_revalidate.return_value = []

        self.manager._collect_validation_batch()

        self.assertEqual(
            self.mock_db_instance.get_new_proxies_to_validate.call_args.kwargs["limit"],
            50,
        )
        self.assertEqual(
            self.mock_db_instance.get_active_proxies_to_revalidate.call_args.kwargs[
                "limit"
            ],
            50,
        )

    def test_unused_new_proxy_budget_is_donated_to_revalidation(self):
        """An empty new-proxy queue must not shrink the cycle."""
        self.manager.validation_batch_limit = 100
        self.manager.validation_new_proxy_ratio = 0.5
        self.mock_db_instance.get_new_proxies_to_validate.return_value = []
        self.mock_db_instance.get_active_proxies_to_revalidate.side_effect = [
            [{"id": i, "protocol": "http", "ip": "1.1.1.1", "port": 80} for i in range(50)],
            [
                {"id": i, "protocol": "http", "ip": "1.1.1.1", "port": 80}
                for i in range(100)
            ],
        ]

        batch = self.manager._collect_validation_batch()

        self.assertEqual(len(batch), 100)
        self.assertEqual(
            self.mock_db_instance.get_active_proxies_to_revalidate.call_args.kwargs[
                "limit"
            ],
            100,
        )

    def test_supplement_query_excludes_already_selected_ids(self):
        """
        Deduplicating in Python after the SQL LIMIT wastes budget; the excluded
        ids are pushed into the query instead.
        """
        self.mock_db_instance.get_new_proxies_to_validate.return_value = [
            {"id": 7, "protocol": "http", "ip": "1.1.1.1", "port": 80}
        ]
        self.mock_db_instance.get_active_proxies_to_revalidate.return_value = []
        self.mock_db_instance.get_eligible_failed_proxies.return_value = []

        async def mock_validate_batch(proxies):
            return [], [p["id"] for p in proxies]

        with patch.object(
            self.manager, "_validate_proxies_batch_async", side_effect=mock_validate_batch
        ):
            self.manager._run_validation_cycle()

        self.assertEqual(
            self.mock_db_instance.get_eligible_failed_proxies.call_args.kwargs[
                "exclude_ids"
            ],
            [7],
        )

    # ---------- Finding 12: pool truncation laundered bad reputations ----------

    def test_stats_truncation_keeps_proxies_that_carry_a_record(self):
        """
        Evicting by score drops a proxy with a failure history, which then
        re-enters at score=50 / failure_count=0 on the next sync. Evict the
        never-used entries instead.
        """
        self.manager.max_pool_size = 2
        self.manager.stats_pool_max_multiplier = 1  # cap the stats pool at 2
        punished = "http://punished:80"
        stats_pool = {
            punished: self.manager._get_new_proxy_stat()
            | {
                "score": 20.0,
                "failure_count": 30,
                "last_feedback_ts": time.time(),
            },
            "http://never_used_a:80": self.manager._get_new_proxy_stat(),
            "http://never_used_b:80": self.manager._get_new_proxy_stat(),
        }

        retained = self.manager._truncate_stats_pool("source1", stats_pool)

        self.assertEqual(len(retained), 2)
        self.assertIn(punished, retained)
        self.assertEqual(retained[punished]["failure_count"], 30)
        self.assertEqual(retained[punished]["score"], 20.0)

    def test_truncated_bad_proxy_does_not_come_back_whitewashed(self):
        """After a full sync the punished proxy still carries its history."""
        self.manager.max_pool_size = 2
        self.manager.stats_pool_max_multiplier = 1
        self.manager.rescore_on_sync_enabled = False
        punished = "http://punished:80"
        self.manager.source_stats["source1"] = {
            punished: self.manager._get_new_proxy_stat()
            | {"score": 20.0, "failure_count": 30, "last_feedback_ts": time.time()},
            "http://never_used_a:80": self.manager._get_new_proxy_stat(),
            "http://never_used_b:80": self.manager._get_new_proxy_stat(),
        }
        self.mock_db_instance.get_active_proxies.return_value = {punished}

        self.manager._sync_and_select_top_proxies()

        stat = self.manager.source_stats["source1"][punished]
        self.assertEqual(stat["score"], 20.0)
        self.assertEqual(stat["failure_count"], 30)

    # ---------- Finding 12 (batch 2): exploration quota for untried proxies ----------

    def test_exploration_can_hand_out_a_proxy_outside_the_top_pool(self):
        """An untried but live proxy must be reachable even when not in a tier."""
        self.manager.exploration_ratio = 1.0
        untried = "http://untried:80"
        incumbent = "http://incumbent:80"
        self.manager.active_proxies = {untried, incumbent}
        self.manager.source_stats["source1"] = {
            incumbent: self.manager._get_new_proxy_stat()
            | {"recent_results": [[time.time(), True, 100]]},
            untried: self.manager._get_new_proxy_stat(),
        }
        self.manager.available_proxies["source1"] = {
            "top_tier": [incumbent],
            "bottom_tier": [],
        }

        self.assertEqual(self.manager.get_proxy("source1"), untried)

    def test_exploration_is_disabled_when_ratio_is_zero(self):
        """exploration_ratio = 0 restores the pure top-pool behaviour."""
        manager = self.make_manager(
            {"source_pool": {"exploration_ratio": "0"}}, name="no_explore.ini"
        )
        untried = "http://untried:80"
        incumbent = "http://incumbent:80"
        manager.active_proxies = {untried, incumbent}
        manager.source_stats["source1"] = {untried: manager._get_new_proxy_stat()}
        manager.available_proxies["source1"] = {
            "top_tier": [incumbent],
            "bottom_tier": [],
        }

        self.assertEqual(manager.exploration_ratio, 0.0)
        self.assertEqual(
            {manager.get_proxy("source1") for _ in range(20)}, {incumbent}
        )

    def test_exploration_never_returns_a_dead_proxy(self):
        """The exploration pool is gated on active_proxies too."""
        self.manager.exploration_ratio = 1.0
        dead_untried = "http://dead-untried:80"
        incumbent = "http://incumbent:80"
        self.manager.active_proxies = {incumbent}
        self.manager.source_stats["source1"] = {
            dead_untried: self.manager._get_new_proxy_stat()
        }
        self.manager.available_proxies["source1"] = {
            "top_tier": [incumbent],
            "bottom_tier": [],
        }

        self.assertEqual(self.manager.get_proxy("source1"), incumbent)

    # ---------- Finding 6: backup path drifted / restore failed silently ----------

    def test_relative_backup_path_resolves_from_project_root(self):
        """A relative stats_backup_path must not depend on the process CWD."""
        manager = self.make_manager(
            {
                "backup": {
                    "stats_backup_enabled": "false",
                    "stats_backup_path": "./.local/data/proxy_stats_backup.json",
                }
            },
            name="relpath.ini",
        )
        project_root = Path(__file__).resolve().parents[1]

        self.assertTrue(manager.stats_backup_path.is_absolute())
        self.assertEqual(
            manager.stats_backup_path,
            (project_root / ".local" / "data" / "proxy_stats_backup.json").resolve(),
        )

    def test_absolute_backup_path_is_left_alone(self):
        absolute = os.path.join(self.tmp_dir, "backup.json")
        manager = self.make_manager(
            {"backup": {"stats_backup_enabled": "false", "stats_backup_path": absolute}},
            name="abspath.ini",
        )

        self.assertEqual(manager.stats_backup_path, Path(absolute).resolve())

    def test_missing_backup_file_is_reported_as_a_warning(self):
        """Losing every score on restart must not be an INFO-level event."""
        self.manager.stats_backup_path = Path(self.tmp_dir) / "missing.json"

        with patch("src.core.proxy_manager.logger.warning") as warn:
            result = self.manager.restore_stats()

        self.assertEqual(result["status"], "skipped")
        self.assertTrue(warn.called)
        self.assertIn("missing.json", str(warn.call_args))

    # ---------- Batch 1 item 4: atomic backup write ----------

    def test_backup_is_written_atomically(self):
        """A failure mid-dump must leave the previous backup intact."""
        backup_path = Path(self.tmp_dir) / "stats.json"
        backup_path.write_text('{"previous": true}', encoding="utf-8")
        self.manager.stats_backup_path = backup_path
        self.manager.source_stats["source1"] = {
            "http://1.1.1.1:80": self.manager._get_new_proxy_stat()
        }

        with patch(
            "src.core.proxy_manager.json.dump", side_effect=OSError("disk full")
        ):
            result = self.manager.backup_stats()

        self.assertEqual(result["status"], "error")
        self.assertEqual(json.loads(backup_path.read_text(encoding="utf-8")), {"previous": True})
        # No temp files left behind.
        self.assertEqual(
            [n for n in os.listdir(self.tmp_dir) if n.endswith(".tmp")], []
        )

    def test_backup_then_restore_round_trips(self):
        backup_path = Path(self.tmp_dir) / "stats.json"
        self.manager.stats_backup_path = backup_path
        self.manager.source_stats["source1"] = {
            "http://1.1.1.1:80": self.manager._get_new_proxy_stat() | {"score": 77.0}
        }

        self.assertEqual(self.manager.backup_stats()["status"], "success")
        self.manager.source_stats["source1"] = {}
        self.assertEqual(self.manager.restore_stats()["status"], "success")

        self.assertEqual(
            self.manager.source_stats["source1"]["http://1.1.1.1:80"]["score"], 77.0
        )

    def test_restore_sanitizes_oversized_timestamps_before_sync(self):
        backup_path = Path(self.tmp_dir) / "stats.json"
        valid_timestamp = time.time() - 10
        poisoned = self.manager._get_new_proxy_stat()
        poisoned["recent_results"] = [
            [10 ** 400, True, 200],
            [valid_timestamp, True, 250],
        ]
        poisoned["last_feedback_ts"] = 10 ** 400
        backup_path.write_text(
            json.dumps(
                {
                    "timestamp": "2026-08-30T00:00:00",
                    "source_stats": {
                        "source1": {"http://poisoned:80": poisoned}
                    },
                }
            ),
            encoding="utf-8",
        )
        self.manager.stats_backup_path = backup_path

        result = self.manager.restore_stats()

        self.assertEqual(result["status"], "success")
        restored = self.manager.source_stats["source1"]["http://poisoned:80"]
        self.assertEqual(restored["recent_results"], [[valid_timestamp, True, 250]])
        self.assertEqual(restored["last_feedback_ts"], valid_timestamp)

        self.manager.active_proxies = {"http://poisoned:80"}
        self.mock_db_instance.get_active_proxies.return_value = {
            "http://poisoned:80"
        }
        self.manager._sync_and_select_top_proxies()  # must not raise

    def test_structurally_invalid_restore_is_transactional(self):
        backup_path = Path(self.tmp_dir) / "stats.json"
        existing = {
            "source1": {
                "http://existing:80": self.manager._get_new_proxy_stat()
            }
        }
        self.manager.source_stats = copy.deepcopy(existing)
        backup_path.write_text(
            json.dumps(
                {
                    "timestamp": "2026-08-30T00:00:00",
                    "source_stats": {
                        "source1": {
                            "http://valid:80": self.manager._get_new_proxy_stat()
                        },
                        "source2": [],
                    },
                }
            ),
            encoding="utf-8",
        )
        self.manager.stats_backup_path = backup_path

        result = self.manager.restore_stats()

        self.assertEqual(result["status"], "error")
        self.assertEqual(self.manager.source_stats, existing)

    # ---------- Finding 11: reload_sources only reloaded sources ----------

    def test_reload_sources_reloads_the_whole_config(self):
        """Editing any tunable and calling /reload-sources must take effect."""
        self.assertEqual(self.manager.max_pool_size, 100)
        self.assertEqual(self.manager.selection_strategy, "uniform")

        updated = {
            section: dict(options) for section, options in self.config_dict.items()
        }
        updated["source_pool"]["max_pool_size"] = "321"
        updated["source_pool"]["selection_strategy"] = "weighted"
        updated["validator"]["validation_workers"] = "42"
        config = configparser.ConfigParser()
        config.read_dict(updated)
        with open(self.config_path, "w", encoding="utf-8") as f:
            config.write(f)

        result = self.manager.reload_sources()

        self.assertEqual(self.manager.max_pool_size, 321)
        self.assertEqual(self.manager.selection_strategy, "weighted")
        self.assertEqual(self.manager.validation_workers, 42)
        self.assertIn("restart_required_for", result)

    def test_reload_sources_still_reports_source_changes(self):
        updated = {
            section: dict(options) for section, options in self.config_dict.items()
        }
        updated["sources"]["predefined_sources"] = "source1,source3"
        config = configparser.ConfigParser()
        config.read_dict(updated)
        with open(self.config_path, "w", encoding="utf-8") as f:
            config.write(f)

        result = self.manager.reload_sources()

        self.assertEqual(result["added_predefined_sources"], ["source3"])
        self.assertEqual(result["removed_predefined_sources"], ["source2"])

    # ---------- Finding 18: silent config drift ----------

    def test_config_drift_reports_missing_keys(self):
        example_path = Path(self.tmp_dir) / "example.ini"
        # softmax_temperature is deliberately absent from the test fixture ini,
        # so it is exactly the kind of key that silently falls back to a default.
        example_path.write_text(
            "[source_pool]\nmax_pool_size = 200\nsoftmax_temperature = 20.0\n"
            "[brand_new]\nkey = value\n",
            encoding="utf-8",
        )

        drift = self.manager.check_config_drift(example_path)

        self.assertIn("[source_pool] softmax_temperature", drift["missing"])
        self.assertIn("[brand_new] (entire section)", drift["missing"])
        self.assertNotIn("[source_pool] max_pool_size", drift["missing"])

    def test_config_drift_reports_deprecated_keys(self):
        example_path = Path(self.tmp_dir) / "example.ini"
        example_path.write_text("[source_pool]\nmax_pool_size = 200\n", encoding="utf-8")
        self.manager.config.set("source_pool", "failure_penalties", "1")

        drift = self.manager.check_config_drift(example_path)

        self.assertIn("[source_pool] failure_penalties", drift["unknown"])

    def test_config_drift_ignores_proxy_source_sections(self):
        """Proxy sources are per-deployment and must never be flagged."""
        example_path = Path(self.tmp_dir) / "example.ini"
        example_path.write_text(
            "[proxy_source_zzz]\nurl = http://example.invalid\n", encoding="utf-8"
        )

        drift = self.manager.check_config_drift(example_path)

        self.assertEqual(drift["missing"], [])
        self.assertEqual(drift["unknown"], [])

    def test_shipped_example_config_covers_every_configurable_key(self):
        """
        Guard against the reverse drift: a new tunable added in code but never
        documented in config.example.ini.
        """
        from src.core.proxy_manager import CONFIG_EXAMPLE_PATH

        example = configparser.ConfigParser()
        example.read(CONFIG_EXAMPLE_PATH, encoding="utf-8")
        for section, option in [
            ("source_pool", "exploration_ratio"),
            ("source_pool", "elo_prior_successes"),
            ("source_pool", "elo_prior_failures"),
            ("source_pool", "rescore_on_sync_enabled"),
            ("source_pool", "max_feedback_latency_ms"),
            ("validator", "validation_new_proxy_ratio"),
        ]:
            with self.subTest(option=option):
                self.assertTrue(
                    example.has_option(section, option),
                    f"[{section}] {option} is missing from config.example.ini",
                )

    # ---------- Test baseline: config really is read from the file ----------

    def test_manager_loads_values_from_the_real_config_file(self):
        """
        The old fixture patched ConfigParser.read, so manager.config was empty
        and every assertion below would have seen a fallback default instead.
        """
        self.assertTrue(self.manager.config.has_section("source_pool"))
        self.assertEqual(self.manager.max_pool_size, 100)          # default is 500
        self.assertEqual(self.manager.stats_pool_max_multiplier, 10)  # default is 20
        self.assertEqual(self.manager.validation_workers, 10)      # default is 100
        self.assertEqual(self.manager.top_tier_size, 50)           # default is 100
        self.assertEqual(
            self.manager.validation_target, "http://mocktarget.com"
        )  # default is httpbin


class TestReviewRegressions(ProxyManagerTestBase):
    """
    Regressions found reviewing the issue #13 fix itself. Several were
    introduced by that fix; the rest it made reachable.
    """

    # --- Malformed feedback must not poison persistent scoring state ---

    def test_non_numeric_latency_never_reaches_the_stat(self):
        """
        A string latency used to be stored verbatim in recent_results, and
        _calculate_elo_score then raised on it. Because the pool is rescored on
        every sync, that one stat stopped pool refreshes for every source.
        """
        url = "http://1.1.1.1:80"
        self.manager.source_stats["source1"] = {url: self.manager._get_new_proxy_stat()}

        self.manager.process_feedback("source1", url, 200, response_time_ms="fast")

        stat = self.manager.source_stats["source1"][url]
        self.assertIsNone(stat["avg_latency_ms"])
        self.assertEqual(stat["recent_results"], [[stat["last_feedback_ts"], True, None]])
        self.assertIsInstance(self.manager._calculate_elo_score(stat), float)

    def test_poisoned_stat_from_a_backup_cannot_break_the_sync(self):
        """Restored backups are untrusted; the score path must survive them."""
        url = "http://1.1.1.1:80"
        poisoned = self.manager._get_new_proxy_stat()
        poisoned["recent_results"] = [
            [time.time(), True, "fast"],
            [time.time(), True, None],
            [time.time(), True, float("inf")],
            [time.time(), True, -5],
            [time.time(), True, True],
        ]
        self.manager.source_stats["source1"] = {url: poisoned}
        self.mock_db_instance.get_active_proxies.return_value = {url}

        self.manager._sync_and_select_top_proxies()   # must not raise

        self.assertIsInstance(
            self.manager.source_stats["source1"][url]["score"], float
        )

    def test_feedback_endpoint_rejects_malformed_payloads(self):
        from src.api.server import create_app

        pm = MagicMock(spec=ProxyManager)
        pm.allowed_ips = []
        pm.trust_proxy_headers = False
        pm.trusted_proxy_ips = []
        pm.lock = threading.RLock()
        pm.is_valid_feedback_status.return_value = True
        client = create_app(pm).test_client()

        bad_payloads = [
            {"source": "s", "proxy": "p", "status": 200, "response_time_ms": "fast"},
            {"source": "s", "proxy": "p", "status": 200, "response_time_ms": -1},
            {"source": "s", "proxy": "p", "status": 200, "response_time_ms": float("nan")},
            {"source": "s", "proxy": "p", "status": True},          # bool is not an int here
            {"source": "", "proxy": "p", "status": 200},
            {"source": 5, "proxy": "p", "status": 200},
            {"source": "s", "proxy": None, "status": 200},
            {"source": "s", "proxy": "p", "status": 200, "failure_kind": 7},
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                r = client.post(
                    "/feedback", json=payload,
                    environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
                )
                self.assertEqual(r.status_code, 400)
        pm.process_feedback.assert_not_called()

        r = client.post(
            "/feedback",
            json={"source": "s", "proxy": "p", "status": 200, "response_time_ms": 12.5},
            environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
        )
        self.assertEqual(r.status_code, 200)

    # --- Parser must reject anything PostgreSQL would reject ---

    def test_parser_rejects_nul_and_non_ip_hosts(self):
        """
        A NUL byte passes a length/'@' check but makes psycopg2 raise on the
        whole execute_values batch, losing every valid row with it.
        """
        for line in [
            "1.2.3.4\x00:80",
            "1.2.3.4\x01:80",
            "1.2.3.4 5:80",
            "not_an_ip:80",
            "256.1.1.1:80",
            "1.2.3.4.5:80",
            "::80",
            "'; DROP TABLE proxies; --:80",
        ]:
            with self.subTest(line=line):
                self.assertIsNone(self.manager._parse_proxy_line(line, "http"))

    def test_parser_still_accepts_valid_ipv4_and_ipv6(self):
        cases = {
            ("1.2.3.4:8080", "http"): ("http", "1.2.3.4", 8080),
            ("  1.2.3.4:8080  ".strip(), "http"): ("http", "1.2.3.4", 8080),
            ("[2001:db8::1]:1080", "socks5"): ("socks5", "[2001:db8::1]", 1080),
            ("http://[::1]:8080", None): ("http", "[::1]", 8080),
            # Brackets are added even when the source list omits them.
            ("2001:db8::1:1080", "socks5"): ("socks5", "[2001:db8::1]", 1080),
        }
        for (line, dp), expected in cases.items():
            with self.subTest(line=line):
                self.assertEqual(self.manager._parse_proxy_line(line, dp), expected)

    def test_dirty_row_between_two_valid_rows_is_dropped(self):
        payload = "\n".join(["1.1.1.1:80", "2.2.2.2\x00:80", "3.3.3.3:80"])
        job = {"name": "j", "url": "u", "default_protocol": "http",
               "interval_minutes": 10, "last_run": 0}

        with patch.object(self.manager, "_fetch_source_text", return_value=payload):
            parsed = self.manager._fetch_and_parse_source(job)

        self.assertEqual(parsed, [("http", "1.1.1.1", 80), ("http", "3.3.3.3", 80)])

    # --- Truncation must never trade a live proxy for a dead one ---

    def test_truncation_never_evicts_live_in_favour_of_dead(self):
        """
        Staleness-only eviction ranks a freshly discovered live proxy (no
        last_feedback_ts) below every dead proxy that still carries an old
        timestamp - at the cap that empties the live pool entirely.
        """
        self.manager.max_pool_size = 2
        self.manager.stats_pool_max_multiplier = 1
        self.manager.rescore_on_sync_enabled = False
        now = time.time()
        newcomer = "http://new:80"
        self.manager.source_stats["source1"] = {
            "http://dead1:80": self.manager._get_new_proxy_stat() | {"last_feedback_ts": now},
            "http://dead2:80": self.manager._get_new_proxy_stat() | {"last_feedback_ts": now},
        }
        self.mock_db_instance.get_active_proxies.return_value = {newcomer}

        self.manager._sync_and_select_top_proxies()

        self.assertIn(newcomer, self.manager.source_stats["source1"])
        self.assertEqual(self.manager.available_proxies["source1"]["top_tier"], [newcomer])
        self.assertEqual(self.manager.get_proxy("source1"), newcomer)

    def test_truncation_still_keeps_punished_history_over_stale_dead(self):
        """The original anti-laundering property must survive the live-first rule."""
        self.manager.max_pool_size = 2
        self.manager.stats_pool_max_multiplier = 1
        now = time.time()
        punished = "http://punished:80"
        self.manager.active_proxies = {punished, "http://live2:80"}
        pool = {
            punished: self.manager._get_new_proxy_stat()
            | {"score": 20.0, "failure_count": 30, "last_feedback_ts": now},
            "http://live2:80": self.manager._get_new_proxy_stat(),
            "http://deadold:80": self.manager._get_new_proxy_stat()
            | {"last_feedback_ts": now - 99999},
        }

        retained = self.manager._truncate_stats_pool("source1", pool)

        self.assertIn(punished, retained)
        self.assertEqual(retained[punished]["failure_count"], 30)
        self.assertNotIn("http://deadold:80", retained)

    def test_truncation_never_evicts_a_live_proxy(self):
        """
        The cap applies to dead history only. Evicting a live proxy is
        reputation loss, because _sync_and_select_top_proxies re-seeds any
        active proxy missing from the pool with a pristine stat.
        """
        self.manager.max_pool_size = 10
        self.manager.stats_pool_max_multiplier = 1   # cap 10
        now = time.time()
        live = {f"http://p{i}:80": self.manager._get_new_proxy_stat()
                | {"last_feedback_ts": now - i} for i in range(15)}
        dead = {f"http://d{i}:80": self.manager._get_new_proxy_stat()
                | {"last_feedback_ts": now - i} for i in range(5)}
        self.manager.active_proxies = set(live)

        retained = self.manager._truncate_stats_pool("source1", {**live, **dead})

        self.assertEqual(set(retained), set(live))

    def test_truncation_keeps_freshest_dead_history_in_the_leftover_room(self):
        self.manager.max_pool_size = 10
        self.manager.stats_pool_max_multiplier = 1   # cap 10
        now = time.time()
        live = {f"http://p{i}:80": self.manager._get_new_proxy_stat() for i in range(6)}
        dead = {f"http://d{i}:80": self.manager._get_new_proxy_stat()
                | {"last_feedback_ts": now - i} for i in range(9)}
        self.manager.active_proxies = set(live)

        retained = self.manager._truncate_stats_pool("source1", {**live, **dead})

        self.assertEqual(len(retained), 10)
        self.assertTrue(set(live).issubset(retained))
        # 4 slots left, filled by the four most recently seen dead proxies.
        self.assertEqual(
            {u for u in retained if u.startswith("http://d")},
            {"http://d0:80", "http://d1:80", "http://d2:80", "http://d3:80"},
        )

    # --- Exploration ---

    def test_exploration_survives_a_fully_cooled_down_top_pool(self):
        """
        get_proxy checked the ranked candidates for emptiness before running
        exploration, so an all-cooled-down top pool returned None even though
        an untried proxy was available.
        """
        now = time.time()
        incumbent, newcomer = "http://incumbent:80", "http://newcomer:80"
        self.manager.proxy_cooldown_ms = 10000
        self.manager.exploration_ratio = 1.0
        self.manager.active_proxies = {incumbent, newcomer}
        self.manager.source_stats["source1"] = {
            incumbent: self.manager._get_new_proxy_stat() | {"recent_results": [[now, True, 100]]},
            newcomer: self.manager._get_new_proxy_stat(),
        }
        self.manager.available_proxies["source1"] = {"top_tier": [incumbent], "bottom_tier": []}
        self.manager.proxy_last_handed_out_ts["source1"][incumbent] = now

        self.assertEqual(self.manager.get_proxy("source1"), newcomer)

    def test_exploration_rotates_instead_of_repeating_one_proxy(self):
        """
        Eligibility is 'no feedback yet', which is not 'never handed out': a
        caller that never reports back would otherwise let one proxy absorb the
        whole exploration budget forever.
        """
        self.manager.exploration_ratio = 1.0
        self.manager.proxy_cooldown_ms = 0
        urls = [f"http://n{i}:80" for i in range(3)]
        self.manager.active_proxies = set(urls)
        self.manager.source_stats["source1"] = {
            u: self.manager._get_new_proxy_stat() for u in urls
        }
        self.manager.available_proxies["source1"] = {"top_tier": [], "bottom_tier": []}

        picks = [self.manager.get_proxy("source1") for _ in range(6)]

        self.assertEqual(set(picks[:3]), set(urls))   # every untried one first
        self.assertEqual(set(picks[3:]), set(urls))   # then rotate, not repeat

    # --- Backup concurrency ---

    def test_concurrent_backup_cannot_let_a_stale_snapshot_win(self):
        backup_path = Path(self.tmp_dir) / "stats.json"
        self.manager.stats_backup_path = backup_path
        url = "http://a:80"
        self.manager.source_stats["source1"] = {
            url: self.manager._get_new_proxy_stat() | {"score": 1.0}
        }
        gate = threading.Event()
        real_dump = json.dump

        def slow_dump(obj, f, **kw):
            if obj["source_stats"]["source1"][url]["score"] == 1.0:
                gate.wait(5)
            return real_dump(obj, f, **kw)

        def old_writer():
            with patch("src.core.proxy_manager.json.dump", side_effect=slow_dump):
                self.manager.backup_stats()

        t = threading.Thread(target=old_writer)
        t.start()
        time.sleep(0.2)
        self.manager.source_stats["source1"][url]["score"] = 2.0
        gate.set()
        self.manager.backup_stats()
        t.join()

        written = json.loads(backup_path.read_text(encoding="utf-8"))
        self.assertEqual(written["source_stats"]["source1"][url]["score"], 2.0)

    # --- reload_sources must be authoritative and transactional ---

    def test_reload_drops_keys_deleted_from_the_file(self):
        """ConfigParser.read() merges, so a fresh parser is required."""
        self.assertEqual(self.manager.max_pool_size, 100)
        updated = {s: dict(o) for s, o in self.config_dict.items()}
        del updated["source_pool"]["max_pool_size"]
        config = configparser.ConfigParser()
        config.read_dict(updated)
        with open(self.config_path, "w", encoding="utf-8") as f:
            config.write(f)

        self.manager.reload_sources()

        self.assertEqual(self.manager.max_pool_size, 500)   # the code fallback

    def test_reload_drops_fetcher_jobs_deleted_from_the_file(self):
        self.assertEqual([j["name"] for j in self.manager.fetcher_jobs], ["proxy_source_A"])
        updated = {s: dict(o) for s, o in self.config_dict.items()}
        del updated["proxy_source_A"]
        config = configparser.ConfigParser()
        config.read_dict(updated)
        with open(self.config_path, "w", encoding="utf-8") as f:
            config.write(f)

        result = self.manager.reload_sources()

        self.assertEqual(self.manager.fetcher_jobs, [])
        self.assertEqual(result["removed_fetcher_jobs"], ["proxy_source_A"])

    def test_reload_rolls_back_completely_on_an_invalid_value(self):
        """A bad value halfway through must not leave half-new settings."""
        before = (self.manager.max_pool_size, self.manager.top_tier_size)
        updated = {s: dict(o) for s, o in self.config_dict.items()}
        updated["source_pool"]["max_pool_size"] = "321"
        updated["source_pool"]["top_tier_size"] = "not_a_number"
        config = configparser.ConfigParser()
        config.read_dict(updated)
        with open(self.config_path, "w", encoding="utf-8") as f:
            config.write(f)

        with self.assertRaises(ValueError):
            self.manager.reload_sources()

        self.assertEqual((self.manager.max_pool_size, self.manager.top_tier_size), before)
        self.assertEqual(self.manager.config.getint("source_pool", "max_pool_size"), 100)

    def test_reload_aborts_when_the_file_is_unreadable(self):
        before = self.manager.max_pool_size
        os.remove(self.config_path)

        with self.assertRaises(RuntimeError):
            self.manager.reload_sources()

        self.assertEqual(self.manager.max_pool_size, before)

    def test_logging_is_declared_restart_required(self):
        """Docs must not promise a reload that _load_config never performs."""
        self.assertTrue(
            any("logging" in entry for entry in self.manager.RESTART_REQUIRED_CONFIG)
        )


class TestSourcesEndpointCaching(unittest.TestCase):
    """Finding 10: /api/sources ran a SELECT DISTINCT on every request."""

    def setUp(self):
        from src.api.server import create_app

        self.mock_proxy_manager = MagicMock(spec=ProxyManager)
        self.mock_proxy_manager.allowed_ips = []
        self.mock_proxy_manager.trust_proxy_headers = False
        self.mock_proxy_manager.trusted_proxy_ips = []
        self.mock_proxy_manager.lock = threading.RLock()
        self.mock_proxy_manager.dashboard_sources = {"default", "insolvencydirect"}
        self.mock_proxy_manager.db = MagicMock(spec=DatabaseManager)
        self.client = create_app(self.mock_proxy_manager).test_client()

    def test_repeated_requests_do_not_hit_the_database(self):
        for _ in range(3):
            response = self.client.get(
                "/api/sources", environ_overrides={"REMOTE_ADDR": "127.0.0.1"}
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.get_json(), ["default", "insolvencydirect"]
            )

        self.assertLessEqual(
            self.mock_proxy_manager.db.get_distinct_sources.call_count, 1
        )
        self.mock_proxy_manager._update_dashboard_sources.assert_not_called()


class TestValidationQueueSQL(unittest.TestCase):
    """
    Finding 7 and finding 14, verified at the SQL-text level.

    There is no local PostgreSQL instance, so these assert the statement text
    and the bound parameters only - not the execution plan or index usage.
    """

    def setUp(self):
        with patch("src.database.db.psycopg2.pool.ThreadedConnectionPool"):
            config = configparser.ConfigParser()
            config.read_dict(
                {
                    "database": {
                        "host": "localhost",
                        "port": "5432",
                        "dbname": "test",
                        "user": "user",
                        "password": "password",
                    }
                }
            )
            self.db = DatabaseManager(config)

    def _capture(self, call):
        with patch.object(self.db, "_execute", return_value=[]) as execute:
            call()
        return execute.call_args

    def test_eligible_failed_proxies_orders_oldest_first(self):
        """
        DESC re-tested the proxies that had just failed; NULLS FIRST pulled in
        never-validated rows that the main query already owns.
        """
        args = self._capture(
            lambda: self.db.get_eligible_failed_proxies(
                window_minutes=30, max_attempts=5, limit=10
            )
        )
        query = " ".join(args[0][0].split())

        self.assertIn("ORDER BY last_validated_at ASC, created_at ASC", query)
        self.assertNotIn("DESC", query)
        self.assertNotIn("NULLS FIRST", query)
        self.assertIn("AND last_validated_at IS NOT NULL", query)

    def test_eligible_failed_proxies_passes_exclude_ids(self):
        args = self._capture(
            lambda: self.db.get_eligible_failed_proxies(
                window_minutes=30, max_attempts=5, limit=10, exclude_ids=[1, 2]
            )
        )

        self.assertEqual(args[0][1]["exclude_ids"], [1, 2])
        self.assertIn("exclude_ids", " ".join(args[0][0].split()))

    def test_new_and_revalidation_queries_are_disjoint(self):
        new_query = " ".join(
            self._capture(lambda: self.db.get_new_proxies_to_validate(limit=10))[0][
                0
            ].split()
        )
        reval_query = " ".join(
            self._capture(
                lambda: self.db.get_active_proxies_to_revalidate(
                    interval_minutes=30, limit=10
                )
            )[0][0].split()
        )

        self.assertIn("WHERE last_validated_at IS NULL", new_query)
        self.assertIn("ORDER BY created_at ASC, id ASC", new_query)
        self.assertIn("last_validated_at IS NOT NULL", reval_query)
        self.assertIn("is_active = true", reval_query)
        self.assertIn("ORDER BY last_validated_at ASC, id ASC", reval_query)

    def test_queue_queries_short_circuit_on_empty_budget(self):
        with patch.object(self.db, "_execute") as execute:
            self.assertEqual(self.db.get_new_proxies_to_validate(limit=0), [])
            self.assertEqual(
                self.db.get_active_proxies_to_revalidate(interval_minutes=30, limit=0),
                [],
            )
            self.assertEqual(
                self.db.get_eligible_failed_proxies(
                    window_minutes=30, max_attempts=5, limit=0
                ),
                [],
            )
        execute.assert_not_called()

    def test_combined_queue_query_has_deterministic_ordering(self):
        args = self._capture(
            lambda: self.db.get_proxies_to_validate(interval_minutes=30, limit=10)
        )
        query = " ".join(args[0][0].split())

        self.assertIn("ORDER BY last_validated_at ASC NULLS FIRST, id ASC", query)

    def test_insert_proxies_uses_a_large_page_size(self):
        """Finding: execute_values defaults to page_size=100."""
        conn = MagicMock()
        self.db.pool = MagicMock()
        self.db.pool.getconn.return_value = conn

        with patch("src.database.db.psycopg2.extras.execute_values") as execute_values:
            self.db.insert_proxies([("http", "1.1.1.1", 80)])

        self.assertEqual(execute_values.call_args.kwargs["page_size"], 1000)

    def test_insert_proxies_does_not_log_rowcount(self):
        """
        psycopg2 documents that after execute_values, cursor.rowcount "will not
        contain a total result" - the old log line reported the last page only.
        """
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.rowcount = 1  # would be the misleading number
        self.db.pool = MagicMock()
        self.db.pool.getconn.return_value = conn

        with patch("src.database.db.psycopg2.extras.execute_values"), patch(
            "src.database.db.logger.info"
        ) as info:
            self.db.insert_proxies([("http", "1.1.1.1", 80)] * 5)

        logged = " ".join(str(call) for call in info.call_args_list)
        self.assertIn("5", logged)
        self.assertNotIn("1/", logged)


class TestStatsQueryIndexability(unittest.TestCase):
    """
    The stats queries filtered on DATE(minute), which is not indexable, and the
    schema's matching functional index could not even be created (DATE() on a
    TIMESTAMPTZ is not IMMUTABLE, so database_setup.sql aborted there).
    """

    def setUp(self):
        with patch("src.database.db.psycopg2.pool.ThreadedConnectionPool"):
            config = configparser.ConfigParser()
            config.read_dict({"database": {"host": "h", "port": "5432", "dbname": "d",
                                           "user": "u", "password": "p"}})
            self.db = DatabaseManager(config)

    def _capture(self, call):
        with patch.object(self.db, "_execute", return_value=[]) as execute:
            call()
        return execute.call_args

    def test_no_stats_query_filters_on_a_function_call(self):
        calls = [
            lambda: self.db.get_daily_stats("s", "2026-08-29"),
            lambda: self.db.get_timeseries_stats("s", "2026-08-29", 15),
            lambda: self.db.get_overview_stats("2026-08-29", 15),
        ]
        for call in calls:
            with self.subTest(call=call):
                query = " ".join(self._capture(call)[0][0].split())
                self.assertNotIn("DATE(minute)", query)
                self.assertIn("minute >=", query)
                self.assertIn("minute <", query)

    def test_daily_stats_binds_the_date_twice(self):
        args = self._capture(lambda: self.db.get_daily_stats("s", "2026-08-29"))
        self.assertEqual(args[0][1], ("s", "2026-08-29", "2026-08-29"))

    def test_overview_daily_binds_the_date_twice(self):
        with patch.object(self.db, "_execute", return_value=[]) as execute:
            self.db.get_overview_stats("2026-08-29", 10)
        self.assertEqual(execute.call_args_list[0][0][1], ("2026-08-29", "2026-08-29"))

    def test_schema_index_is_immutable(self):
        """database_setup.sql must not reference DATE() in an index expression."""
        schema = (Path(__file__).resolve().parents[1] / "config" / "database_setup.sql").read_text(
            encoding="utf-8"
        )
        index_lines = [
            line for line in schema.splitlines()
            if line.strip().upper().startswith("CREATE INDEX")
        ]
        self.assertTrue(index_lines)
        for line in index_lines:
            with self.subTest(line=line):
                self.assertNotIn("DATE(", line.upper())

    def test_existing_databases_have_a_non_destructive_index_migration(self):
        migration = (
            Path(__file__).resolve().parents[1]
            / "config"
            / "migrations"
            / "20260830_add_source_stats_source_minute_index.sql"
        ).read_text(encoding="utf-8")
        normalized = " ".join(migration.split()).upper()

        self.assertIn("CREATE INDEX CONCURRENTLY IF NOT EXISTS", normalized)
        self.assertIn("(SOURCE_NAME, MINUTE)", normalized)
        self.assertNotIn("DROP TABLE", normalized)


class TestDatabaseManager(unittest.TestCase):
    """Test DatabaseManager methods with mocked psycopg2."""

    def _make_database_manager(self):
        config = configparser.ConfigParser()
        config.read_dict(
            {
                "database": {
                    "host": "localhost",
                    "port": "5432",
                    "dbname": "test",
                    "user": "user",
                    "password": "password",
                    "max_connections": "50",
                }
            }
        )
        return DatabaseManager(config)

    @patch("src.database.db.psycopg2.pool.ThreadedConnectionPool")
    def test_database_manager_uses_threaded_pool(self, mock_pool_class):
        """Test that DatabaseManager uses ThreadedConnectionPool."""
        mock_config = configparser.ConfigParser()
        mock_config.read_dict({
            "database": {
                "host": "localhost",
                "port": "5432", 
                "dbname": "test",
                "user": "user",
                "password": "password",
                "max_connections": "50",
            }
        })
        
        db = DatabaseManager(mock_config)
        
        mock_pool_class.assert_called_once()
        call_kwargs = mock_pool_class.call_args
        self.assertEqual(call_kwargs[1]["maxconn"], 50)

    @patch("src.database.db.psycopg2.pool.ThreadedConnectionPool")
    def test_distinct_sources_distinguishes_failure_from_empty_result(self, _mock_pool):
        db = self._make_database_manager()

        with patch.object(db, "_execute", return_value=None):
            self.assertIsNone(db.get_distinct_sources())
        with patch.object(db, "_execute", return_value=[]):
            self.assertEqual(db.get_distinct_sources(), [])


class TestSecondReviewRegressions(ProxyManagerTestBase):
    """
    Second review round on PR #14. Every test here drives the public path the
    original coverage skipped: the first round asserted on hand-built
    recent_results, which never populates failure_count, so the historical
    fallback branch in _calculate_elo_score was never reached.
    """

    # --- Finding 1: a single failure must actually recover ---

    def _aged_single_failure(self, manager, url, hours):
        """One real failure through process_feedback(), then aged by `hours`."""
        manager.process_feedback("source1", url, 500, None, None)
        stat = manager.source_stats["source1"][url]
        old = time.time() - hours * 3600
        for result in stat["recent_results"]:
            result[0] = old
        stat["last_feedback_ts"] = old
        return stat

    def _manager_with_three_proxies(self, age_hours="48"):
        manager = self.make_manager(
            {"source_pool": {"elo_max_result_age_hours": age_hours}},
            name=f"recovery-{age_hours}.ini",
        )
        urls = ["http://bad:1", "http://n1:1", "http://n2:1"]
        manager.active_proxies = set(urls)
        manager.db.get_active_proxies.return_value = set(urls)
        manager.source_stats["source1"] = {
            url: manager._get_new_proxy_stat() for url in urls
        }
        return manager

    def test_real_failure_returns_to_baseline_once_it_expires(self):
        manager = self._manager_with_three_proxies()
        stat = self._aged_single_failure(manager, "http://bad:1", hours=49)

        # The cumulative counter is still there; it just must not be consulted.
        self.assertEqual(stat["failure_count"], 1)
        self.assertEqual(manager._calculate_elo_score(stat, "source1"), 50.0)

    def test_fresh_failure_is_still_punished(self):
        manager = self._manager_with_three_proxies()
        manager.process_feedback("source1", "http://bad:1", 500, None, None)
        stat = manager.source_stats["source1"]["http://bad:1"]
        self.assertLess(manager._calculate_elo_score(stat, "source1"), 35)

    def test_expired_failure_can_re_enter_the_ranked_pool(self):
        manager = self._manager_with_three_proxies()
        self._aged_single_failure(manager, "http://bad:1", hours=49)
        manager.max_pool_size = 2

        manager._sync_and_select_top_proxies()

        pool = manager.available_proxies["source1"]
        self.assertIn("http://bad:1", list(pool["top_tier"]) + list(pool["bottom_tier"]))

    def test_expired_failure_is_reachable_by_exploration(self):
        manager = self._manager_with_three_proxies()
        self._aged_single_failure(manager, "http://bad:1", hours=49)
        manager.exploration_ratio = 1.0

        picks = {manager._maybe_select_exploration_candidate("source1") for _ in range(200)}

        self.assertIn("http://bad:1", picks)

    def test_unexpired_failure_is_not_treated_as_unproven(self):
        manager = self._manager_with_three_proxies()
        self._aged_single_failure(manager, "http://bad:1", hours=1)
        manager.exploration_ratio = 1.0

        picks = {manager._maybe_select_exploration_candidate("source1") for _ in range(200)}

        self.assertNotIn("http://bad:1", picks)

    def test_never_observed_proxy_still_uses_historical_counters(self):
        """The recovery rule must not swallow the restored-backup fallback."""
        manager = self._manager_with_three_proxies()
        stat = manager._get_new_proxy_stat()
        stat.update(
            {
                "success_count": 9,
                "failure_count": 1,
                "recent_results": [],
                "last_feedback_ts": time.time(),
            }
        )

        # 90% historical success rate -> 0.9 * 80 + 10, undecayed because the
        # counters were just touched.
        self.assertAlmostEqual(manager._calculate_elo_score(stat, "source1"), 82.0, places=1)

    def test_code_fallback_matches_the_shipped_default(self):
        merged = {s: dict(o) for s, o in self.config_dict.items()}
        del merged["source_pool"]["elo_max_result_age_hours"]
        manager = ProxyManager(write_config_file(self.tmp_dir, merged, name="nokey.ini"))

        self.assertEqual(manager.elo_max_result_age_hours, 48.0)

    # --- Finding 2: the cap must not launder a live proxy over two syncs ---

    def test_two_syncs_over_the_cap_do_not_reset_failure_history(self):
        manager = self.make_manager(
            {"source_pool": {"max_pool_size": "1", "stats_pool_max_multiplier": "2"}},
            name="laundering.ini",
        )
        urls = ["http://punished:1", "http://a:1", "http://b:1"]
        manager.active_proxies = set(urls)
        manager.db.get_active_proxies.return_value = set(urls)
        manager.source_stats["source1"] = {u: manager._get_new_proxy_stat() for u in urls}
        manager.source_stats["source1"]["http://punished:1"].update(
            {"score": 20.0, "failure_count": 30, "last_feedback_ts": time.time() - 3600}
        )

        manager._sync_and_select_top_proxies()
        manager._sync_and_select_top_proxies()

        punished = manager.source_stats["source1"]["http://punished:1"]
        self.assertEqual(punished["failure_count"], 30)
        self.assertLess(punished["score"], 50.0)

    # --- Finding 3: reload is all-or-nothing across jobs too ---

    def test_failed_fetcher_job_parse_rolls_back_the_tunables(self):
        manager = self.manager
        before_pool_size = manager.max_pool_size
        before_intervals = [j["interval_minutes"] for j in manager.fetcher_jobs]

        merged = {s: dict(o) for s, o in self.config_dict.items()}
        merged["source_pool"]["max_pool_size"] = "321"
        merged["proxy_source_A"]["update_interval_minutes"] = "not-a-number"
        write_config_file(
            os.path.dirname(manager.config_path),
            merged,
            name=os.path.basename(manager.config_path),
        )

        with self.assertRaises(ValueError):
            manager.reload_sources()

        self.assertEqual(manager.max_pool_size, before_pool_size)
        self.assertEqual(
            [j["interval_minutes"] for j in manager.fetcher_jobs], before_intervals
        )

    # --- Finding 4: a parsed IPv6 proxy must survive URL construction ---

    def test_parsed_ipv6_builds_a_url_with_a_readable_port(self):
        from urllib.parse import urlsplit

        for line in ("http://[2001:db8::1]:8080", "socks5://2001:db8::1:1080"):
            with self.subTest(line=line):
                protocol, ip, port = self.manager._parse_proxy_line(line, None)
                url = f"{protocol}://{ip}:{port}"
                self.assertEqual(urlsplit(url).port, port)

    def test_ipv6_forms_are_canonicalized_to_one_database_key(self):
        compact = self.manager._parse_proxy_line("[2001:db8::1]:8080", "http")
        expanded = self.manager._parse_proxy_line(
            "[2001:0db8:0:0:0:0:0:1]:8080", "http"
        )

        self.assertIsNotNone(compact)
        self.assertEqual(expanded, compact)
        self.assertEqual(compact, ("http", "[2001:db8::1]", 8080))

    def test_max_width_ipv6_is_canonicalized_and_retained(self):
        from src.core.proxy_manager import MAX_IP_LENGTH

        widest = "[ffff:ffff:ffff:ffff:ffff:ffff:255.255.255.255]:80"
        parsed = self.manager._parse_proxy_line(widest, "http")

        self.assertIsNotNone(parsed)
        self.assertEqual(
            parsed,
            ("http", "[ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff]", 80),
        )
        self.assertLessEqual(len(parsed[1]), MAX_IP_LENGTH)

    # --- Finding 5: an out-of-range latency must not reach a float op ---

    def test_oversized_integer_latency_is_rejected_not_raised(self):
        from src.core.proxy_manager import DEFAULT_MAX_FEEDBACK_LATENCY_MS

        for value in (
            10 ** 400,
            DEFAULT_MAX_FEEDBACK_LATENCY_MS + 1,
            float("inf"),
            float("nan"),
            -1,
        ):
            with self.subTest(value=repr(value)[:20]):
                self.assertIsNone(self.manager._coerce_latency(value))

    def test_latency_boundary_is_loaded_from_config(self):
        manager = self.make_manager(
            {"source_pool": {"max_feedback_latency_ms": "1000"}},
            name="latency-boundary.ini",
        )

        self.assertEqual(manager.max_feedback_latency_ms, 1000)
        self.assertEqual(
            manager._coerce_latency(1000, manager.max_feedback_latency_ms), 1000
        )
        self.assertIsNone(
            manager._coerce_latency(1001, manager.max_feedback_latency_ms)
        )

        proxy_url = "http://bounded:80"
        manager.source_stats["source1"][proxy_url] = manager._get_new_proxy_stat()
        manager.process_feedback(
            "source1", proxy_url, 200, response_time_ms=1001
        )
        self.assertIsNone(
            manager.source_stats["source1"][proxy_url]["recent_results"][-1][2]
        )

    def test_backup_with_an_oversized_latency_does_not_block_sync(self):
        stat = self.manager._get_new_proxy_stat()
        stat["recent_results"] = [[time.time(), True, 10 ** 400]]
        self.manager.source_stats["source1"] = {"http://poisoned:80": stat}
        self.manager.active_proxies = {"http://poisoned:80"}
        self.mock_db_instance.get_active_proxies.return_value = {"http://poisoned:80"}

        self.manager._sync_and_select_top_proxies()  # must not raise

        self.assertIn("http://poisoned:80", self.manager.source_stats["source1"])

    # --- Finding 6: the backup destination is read once, under the lock ---

    def test_backup_uses_one_destination_even_if_reload_moves_it(self):
        first = Path(self.tmp_dir) / "a" / "stats.json"
        second = Path(self.tmp_dir) / "b" / "stats.json"
        self.manager.stats_backup_path = first
        self.manager.source_stats["source1"] = {}

        real_deepcopy = copy.deepcopy

        def move_path_mid_backup(value):
            self.manager.stats_backup_path = second
            return real_deepcopy(value)

        with patch("src.core.proxy_manager.copy.deepcopy", side_effect=move_path_mid_backup):
            result = self.manager.backup_stats()

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["path"], str(first))
        self.assertTrue(first.exists())


class TestFeedbackLatencyBoundary(unittest.TestCase):
    """Finding 5 at the HTTP boundary: an unbounded int must be a 400, not a 500."""

    def setUp(self):
        from src.api.server import create_app

        self.mock_proxy_manager = MagicMock(spec=ProxyManager)
        self.mock_proxy_manager.allowed_ips = []
        self.mock_proxy_manager.trust_proxy_headers = False
        self.mock_proxy_manager.trusted_proxy_ips = []
        self.mock_proxy_manager.lock = threading.RLock()
        self.mock_proxy_manager.db = MagicMock(spec=DatabaseManager)
        self.client = create_app(self.mock_proxy_manager).test_client()

    def _post(self, resp_time):
        return self.client.post(
            "/feedback",
            json={
                "source": "source1",
                "proxy": "http://1.2.3.4:80",
                "status": 200,
                "response_time_ms": resp_time,
            },
            environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
        )

    def test_oversized_integer_latency_returns_400(self):
        from src.core.proxy_manager import DEFAULT_MAX_FEEDBACK_LATENCY_MS

        for value in (
            10 ** 400,
            DEFAULT_MAX_FEEDBACK_LATENCY_MS + 1,
            -1,
            "fast",
            True,
        ):
            with self.subTest(value=repr(value)[:20]):
                self.assertEqual(self._post(value).status_code, 400)

    def test_configured_latency_boundary_is_enforced_at_http_boundary(self):
        self.mock_proxy_manager.max_feedback_latency_ms = 1000

        response = self._post(1001)

        self.assertEqual(response.status_code, 400)
        self.assertIn("1000", response.get_json()["error"])

    def test_ordinary_latency_still_accepted(self):
        self.mock_proxy_manager.is_valid_feedback_status.return_value = True
        self.assertEqual(self._post(250).status_code, 200)


class TestIssue17FetcherSupply(ProxyManagerTestBase):
    """
    Issue #17 A+B: proxy supply died because the only fetch transport was the
    one this host routes to a broken egress, and the backoff turned a 35%
    failure rate into a near-total outage.
    """

    def _curl_result(self, stdout, returncode=0, stderr="", http_status=200):
        return subprocess.CompletedProcess(
            args=["curl"],
            returncode=returncode,
            stdout=f"{stdout}\n{http_status:03d}",
            stderr=stderr,
        )

    def test_fetch_source_text_uses_curl_and_parses_the_output(self):
        manager = self.make_manager({}, "curl.ini")
        body = "1.2.3.4:8080\nsocks5://5.6.7.8:1080\nnot-a-proxy\n"

        with patch(
            "src.core.proxy_manager.subprocess.run",
            return_value=self._curl_result(body),
        ) as mock_run:
            parsed = manager._fetch_and_parse_source(
                {
                    "name": "proxy_source_A",
                    "url": "http://source-a.com/list.txt",
                    "interval_minutes": 10,
                    "default_protocol": "http",
                }
            )

        command = mock_run.call_args.args[0]
        self.assertEqual(command[0], "curl")
        self.assertEqual(command[-1], "http://source-a.com/list.txt")
        # Retry is the point: a single reset connection must not cost the whole
        # fetch cycle.
        self.assertIn("--retry", command)
        self.assertIn("--write-out", command)
        self.assertEqual(
            command[command.index("--retry") + 1], str(manager.fetch_curl_retries)
        )
        self.assertEqual(
            parsed, [("http", "1.2.3.4", 8080), ("socks5", "5.6.7.8", 1080)]
        )

    def test_curl_process_timeout_covers_every_retry(self):
        """
        --max-time bounds one attempt. If the subprocess timeout only allowed
        one attempt's worth of seconds it would fire first and the retries
        configured above would never actually happen.
        """
        manager = self.make_manager(
            {
                "fetcher": {
                    "total_timeout_s": "60",
                    "curl_retries": "2",
                    "curl_retry_delay_s": "1",
                }
            },
            "curl_timeout.ini",
        )

        with patch(
            "src.core.proxy_manager.subprocess.run",
            return_value=self._curl_result(""),
        ) as mock_run:
            manager._fetch_source_text("http://source-a.com/list.txt")

        self.assertGreaterEqual(mock_run.call_args.kwargs["timeout"], 3 * 60)

    def test_curl_exit_codes_are_classified(self):
        manager = self.make_manager({}, "classify.ini")
        cases = {
            56: True,   # recv failure - the reset that started this issue
            7: True,    # failed to connect
            28: True,   # operation timed out
            3: False,   # malformed URL
        }
        for returncode, expected_transient in cases.items():
            with self.subTest(returncode=returncode):
                with patch(
                    "src.core.proxy_manager.subprocess.run",
                    return_value=self._curl_result("", returncode=returncode),
                ):
                    with self.assertRaises(FetchError) as ctx:
                        manager._fetch_source_text_curl("http://source-a.com")
                self.assertEqual(ctx.exception.transient, expected_transient)

    def test_curl_http_statuses_are_classified_from_the_real_status_code(self):
        manager = self.make_manager({}, "http.ini")
        cases = {
            404: False,
            429: True,
            500: True,
            503: True,
        }
        for http_status, expected_transient in cases.items():
            with self.subTest(http_status=http_status):
                with patch(
                    "src.core.proxy_manager.subprocess.run",
                    return_value=self._curl_result(
                        "", returncode=22, http_status=http_status
                    ),
                ):
                    with self.assertRaises(FetchError) as ctx:
                        manager._fetch_source_text_curl("http://source-a.com")
                self.assertIn(str(http_status), str(ctx.exception))
                self.assertEqual(ctx.exception.transient, expected_transient)

    def test_transient_backoff_never_exceeds_the_transient_cap(self):
        """
        The outage this fixes: a source with an intermittently reset connection
        sat in a 16-32 minute backoff, so a 35% failure rate became no supply
        at all. A blip must stay inside the short cap no matter how many times
        it repeats.
        """
        transient = FetchError("Connection reset by peer", transient=True)
        cap = self.manager.fetch_backoff_transient_max_s

        for failures in range(1, 25):
            with self.subTest(failures=failures):
                self.assertLessEqual(
                    self.manager._fetch_backoff_seconds(transient, failures), cap
                )
        self.assertLess(cap, 3600)
        # The old curve: 60 * 2**min(n-1, 5) reached 1920s by the sixth failure.
        self.assertLess(self.manager._fetch_backoff_seconds(transient, 6), 1920)

    def test_persistent_failure_waits_longer_than_a_blip(self):
        transient = FetchError("reset", transient=True)
        persistent = FetchError("HTTP 404", transient=False)

        self.assertGreater(
            self.manager._fetch_backoff_seconds(persistent, 12),
            self.manager._fetch_backoff_seconds(transient, 12),
        )
        for failures in range(1, 25):
            with self.subTest(failures=failures):
                self.assertLessEqual(
                    self.manager._fetch_backoff_seconds(persistent, failures),
                    self.manager.fetch_backoff_max_s,
                )

    def test_repeated_transient_failures_reschedule_within_the_cap(self):
        """End to end: the backoff the job actually gets, not just the helper."""
        job = {
            "name": "proxy_source_A",
            "url": "http://source-a.com/list.txt",
            "interval_minutes": 10,
            "default_protocol": "http",
            "last_run": 0,
        }
        with patch.object(
            self.manager,
            "_fetch_source_text",
            side_effect=FetchError("Connection reset by peer", transient=True),
        ):
            for _ in range(10):
                self.assertEqual(self.manager._fetch_and_parse_source(job), [])

        self.assertEqual(job["failure_count"], 10)
        applied_backoff = job["last_run"] + job["interval_minutes"] * 60 - time.time()
        self.assertLessEqual(
            applied_backoff, self.manager.fetch_backoff_transient_max_s + 1
        )
        self.assertGreater(applied_backoff, 0)

    def test_a_success_clears_the_backoff(self):
        job = {
            "name": "proxy_source_A",
            "url": "http://source-a.com/list.txt",
            "interval_minutes": 10,
            "default_protocol": "http",
            "last_run": 0,
        }
        with patch.object(
            self.manager,
            "_fetch_source_text",
            side_effect=FetchError("reset", transient=True),
        ):
            self.manager._fetch_and_parse_source(job)
        self.assertEqual(job["failure_count"], 1)

        with patch.object(
            self.manager, "_fetch_source_text", return_value="1.2.3.4:8080\n"
        ):
            self.manager._fetch_and_parse_source(job)

        self.assertEqual(job["failure_count"], 0)


class TestIssue17DynamicBaseline(ProxyManagerTestBase):
    """
    Issue #17 C: with a hardcoded 50.0 baseline, every proxy that had ever been
    measured sorted below every proxy that had not, and the pool filled with
    proxies whose only qualification was that nothing was known about them.
    """

    def _measured_stat(self, successes, failures, latency_ms=15000):
        results = [(True, latency_ms)] * successes + [(False, None)] * failures
        return self.make_stat(results)

    def _population(self, count=9, trials=20, top_rate=0.4):
        """
        A live pool shaped like the real one: mostly poor performers, a thin
        tail of decent ones. The measured population's window success rate runs
        well under 50% (the service's own end-to-end rate was 11-48%), which is
        exactly why a fixed 50.0 baseline sorted every measured proxy below
        every unmeasured one.
        """
        pool = {}
        for i in range(count):
            successes = round(trials * top_rate * i / (count - 1))
            pool[f"http://m{i}:80"] = self._measured_stat(
                successes, trials - successes
            )
        return pool

    def _sync_with(self, pool, live=None):
        self.manager.source_stats["source1"] = pool
        self.mock_db_instance.get_active_proxies.return_value = set(
            pool if live is None else live
        )
        self.manager._sync_and_select_top_proxies()

    def test_untried_baseline_is_the_median_of_the_measured_live_pool(self):
        pool = self._population()
        measured_urls = list(pool)
        pool["http://untried:80"] = self.manager._get_new_proxy_stat()

        self._sync_with(pool)

        measured_scores = sorted(pool[url]["score"] for url in measured_urls)
        expected = measured_scores[len(measured_scores) // 2]
        baseline = self.manager.baseline_scores["source1"]
        self.assertAlmostEqual(baseline, expected, places=6)
        self.assertNotAlmostEqual(baseline, 50.0, places=3)
        self.assertAlmostEqual(
            pool["http://untried:80"]["score"], expected, places=6
        )

    def test_a_measured_mid_quality_proxy_outranks_an_untried_one(self):
        """
        The inversion this fixes: at a real 15s latency a 50%-success proxy
        scored 35 while a proxy nobody had ever measured scored 50, so every
        blank slate outranked every proxy with a record.
        """
        pool = self._population()
        untried = "http://untried:80"
        pool[untried] = self.manager._get_new_proxy_stat()
        # The proxy from the issue's table: half its requests succeed, at the
        # 15s latency these proxies really run at. It used to score 35 against
        # an untried proxy's 50.
        mid = "http://mid:80"
        pool[mid] = self._measured_stat(10, 10, latency_ms=15000)

        self._sync_with(pool)

        self.assertGreater(pool[mid]["score"], self.manager.baseline_scores["source1"])
        self.assertGreater(pool[mid]["score"], pool[untried]["score"])
        top_tier = self.manager.available_proxies["source1"]["top_tier"]
        self.assertLess(top_tier.index(mid), top_tier.index(untried))

    def test_proven_proxies_reach_the_top_tier_ahead_of_blank_slates(self):
        """
        The production symptom: 100/100 top-tier slots held by unmeasured
        proxies while proxies with a proven record sat outside the pool.
        """
        self.manager.top_tier_size = 5
        # Blanks are inserted first, so under the old scoring they won every
        # tie at 50.0 and took the whole top tier by dict order alone.
        pool = {f"http://blank{i}:80": self.manager._get_new_proxy_stat()
                for i in range(50)}
        pool.update(self._population())
        # "Proven good" as the issue defines it: >=20 observations at >=40%
        # window success rate. There were 44 such proxies and none of them was
        # in the pool, because at a real 15s latency they scored ~35 while every
        # blank slate scored the hardcoded 50.
        proven = {f"http://proven{i}:80": self._measured_stat(10, 10)
                  for i in range(5)}
        pool.update(proven)

        self._sync_with(pool)

        top_tier = self.manager.available_proxies["source1"]["top_tier"]
        self.assertEqual(set(top_tier), set(proven))

    def test_baseline_falls_back_to_neutral_when_nothing_is_measured(self):
        pool = {f"http://blank{i}:80": self.manager._get_new_proxy_stat()
                for i in range(3)}

        self._sync_with(pool)

        self.assertEqual(self.manager.baseline_scores["source1"], 50.0)
        for stat in pool.values():
            self.assertEqual(stat["score"], 50.0)

    def test_baseline_survives_an_empty_pool(self):
        self._sync_with({}, live=set())

        self.assertEqual(self.manager.baseline_scores["source1"], 50.0)
        self.assertEqual(self.manager._baseline_score("source1"), 50.0)
        self.assertEqual(self.manager._baseline_score(), 50.0)
        self.assertEqual(self.manager._baseline_score("no-such-source"), 50.0)

    def test_baseline_with_a_uniform_pool_is_that_uniform_score(self):
        pool = {f"http://m{i}:80": self._measured_stat(5, 5) for i in range(4)}

        self._sync_with(pool)

        scores = {round(stat["score"], 6) for stat in pool.values()}
        self.assertEqual(len(scores), 1)
        self.assertAlmostEqual(
            self.manager.baseline_scores["source1"], scores.pop(), places=6
        )

    def test_dead_proxies_do_not_vote_on_the_baseline(self):
        """
        The baseline says what a fresh candidate competes against, and a dead
        proxy is not competing: it cannot be handed out at all.
        """
        live = "http://live:80"
        pool = {
            live: self._measured_stat(8, 2),
            "http://dead1:80": self._measured_stat(0, 20),
            "http://dead2:80": self._measured_stat(0, 20),
        }

        self._sync_with(pool, live={live})

        self.assertAlmostEqual(
            self.manager.baseline_scores["source1"], pool[live]["score"], places=6
        )

    def test_latency_component_discriminates_across_real_latencies(self):
        """
        Calibrated at 300/2000ms, every proxy in this population sat past the
        zero point, so the 30-point latency component scored a constant 0 and
        ranked nothing.
        """
        scores = [
            self.manager._calculate_elo_score(self.make_stat([(True, lat)] * 20))
            for lat in (8000, 15000, 25000, 33000)
        ]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertGreater(scores[0] - scores[-1], 10)


class TestIssue17ReputationPersistence(ProxyManagerTestBase):
    """
    Issue #17 D: the stats pool caps retained history, so a proxy that failed
    its way out and later revalidated came back as a pristine candidate -
    eviction was an amnesty.
    """

    def test_feedback_counters_are_queued_and_written_back(self):
        proxy = "http://1.2.3.4:8080"
        self.manager.source_stats["source1"][proxy] = self.manager._get_new_proxy_stat()

        self.manager.process_feedback("source1", proxy, 500)
        self.manager.process_feedback("source1", proxy, 200, 900)
        self.assertIn(proxy, self.manager.pending_feedback_persist)

        self.manager._persist_feedback_history()

        rows = self.mock_db_instance.upsert_proxy_feedback_history.call_args.args[0]
        self.assertEqual(len(rows), 1)
        protocol, ip, port, successes, failures, last_ts = rows[0]
        self.assertEqual((protocol, ip, port), ("http", "1.2.3.4", 8080))
        self.assertEqual((successes, failures), (1, 1))
        self.assertIsNotNone(last_ts)
        # The queue is drained, so an idle service does not rewrite the same
        # rows every flush interval.
        self.assertEqual(self.manager.pending_feedback_persist, set())

    def test_overlapping_flushes_cannot_overwrite_newer_totals_with_older_ones(self):
        proxy = "http://1.2.3.4:8080"
        self.manager.source_stats["source1"][proxy] = self.manager._get_new_proxy_stat()
        self.manager.process_feedback("source1", proxy, 500)

        first_entered = threading.Event()
        release_first = threading.Event()
        stored_failure_counts = []

        def persist(rows):
            failure_count = rows[0][4]
            if not stored_failure_counts:
                first_entered.set()
                self.assertTrue(release_first.wait(2))
            stored_failure_counts.append(failure_count)

        self.mock_db_instance.upsert_proxy_feedback_history.side_effect = persist
        first = threading.Thread(target=self.manager._persist_feedback_history)
        first.start()
        self.assertTrue(first_entered.wait(2))

        self.manager.process_feedback("source1", proxy, 500)
        second = threading.Thread(target=self.manager._persist_feedback_history)
        second.start()
        release_first.set()
        first.join(2)
        second.join(2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(stored_failure_counts, [1, 2])

    def test_ipv6_proxy_urls_round_trip_through_the_write_back_key(self):
        proxy = "http://[2001:db8::1]:8080"
        self.manager.source_stats["source1"][proxy] = self.manager._get_new_proxy_stat()
        self.manager.process_feedback("source1", proxy, 500)

        self.manager._persist_feedback_history()

        rows = self.mock_db_instance.upsert_proxy_feedback_history.call_args.args[0]
        self.assertEqual(rows[0][:3], ("http", "[2001:db8::1]", 8080))

    def test_evicted_proxy_returns_with_its_failure_history(self):
        """
        Evict a proxy with a bad record through the real cap, bring it back
        through the real sync, and it must not be a blank slate.
        """
        punished = "http://punished:80"
        self.manager.source_stats["source1"] = {
            punished: self.manager._get_new_proxy_stat()
            | {
                "score": 8.0,
                "success_count": 2,
                "failure_count": 40,
                # Stale feedback: this is the proxy the cap drops first, which
                # is precisely the population that came back whitewashed.
                "last_feedback_ts": time.time() - 2 * 3600,
            }
        }
        self.manager.pending_feedback_persist.add(punished)
        self.manager._persist_feedback_history()

        # Round-trip the row the manager just wrote back through the reader's
        # shape, so the test exercises the real serialisation both ways.
        written = self.mock_db_instance.upsert_proxy_feedback_history.call_args.args[0]
        protocol, ip, port, successes, failures, last_ts = written[0]
        self.mock_db_instance.get_active_feedback_history.return_value = {
            f"{protocol}://{ip}:{port}": {
                "success_count": successes,
                "failure_count": failures,
                "last_feedback_ts": last_ts,
            }
        }

        # It goes dead, gets evicted by the cap, then passes validation again.
        self.manager.max_pool_size = 1
        self.manager.stats_pool_max_multiplier = 1
        self.manager.active_proxies = set()
        self.manager.source_stats["source1"] = self.manager._truncate_stats_pool(
            "source1",
            self.manager.source_stats["source1"]
            | {
                "http://other:80": self.manager._get_new_proxy_stat()
                | {"last_feedback_ts": time.time()}
            },
        )
        self.assertNotIn(punished, self.manager.source_stats["source1"])

        self.mock_db_instance.get_active_proxies.return_value = {punished}
        self.manager._sync_and_select_top_proxies()

        restored = self.manager.source_stats["source1"][punished]
        self.assertEqual(restored["failure_count"], 40)
        self.assertEqual(restored["success_count"], 2)
        self.assertLess(restored["score"], self.manager.baseline_scores["source1"])

    def test_history_is_only_queried_when_something_needs_seeding(self):
        known = "http://known:80"
        for source in self.manager.predefined_sources:
            self.manager.source_stats[source] = {
                known: self.manager._get_new_proxy_stat()
            }
        self.mock_db_instance.get_active_proxies.return_value = {known}

        self.manager._sync_and_select_top_proxies()

        self.mock_db_instance.get_active_feedback_history.assert_not_called()

    def test_a_failed_history_query_does_not_break_the_sync(self):
        self.mock_db_instance.get_active_feedback_history.return_value = None
        newcomer = "http://newcomer:80"
        self.mock_db_instance.get_active_proxies.return_value = {newcomer}

        self.manager._sync_and_select_top_proxies()  # must not raise

        self.assertIn(newcomer, self.manager.source_stats["source1"])
        self.assertEqual(
            self.manager.source_stats["source1"][newcomer]["score"], 50.0
        )

    def test_seeded_history_decays_toward_the_baseline_with_age(self):
        """
        A restored record is evidence, not a life sentence: an old one converges
        on what an unknown proxy is worth rather than pinning the proxy forever.
        """
        fresh = self.manager._get_new_proxy_stat(
            "source1",
            {"success_count": 0, "failure_count": 40, "last_feedback_ts": time.time()},
        )
        stale = self.manager._get_new_proxy_stat(
            "source1",
            {
                "success_count": 0,
                "failure_count": 40,
                "last_feedback_ts": time.time() - 10 * 24 * 3600,
            },
        )

        self.assertLess(fresh["score"], stale["score"])
        self.assertLess(stale["score"], 50.0)
        self.assertAlmostEqual(stale["score"], 50.0, delta=1.0)


if __name__ == "__main__":
    unittest.main()
