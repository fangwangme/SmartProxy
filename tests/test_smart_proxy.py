# -*- coding: utf-8 -*-
import asyncio
import unittest
import configparser
import threading
from unittest.mock import MagicMock, patch, AsyncMock

from src.core.proxy_manager import ProxyManager, FAILED_STATUS_CODES
from src.database.db import DatabaseManager


class TestProxyManager(unittest.TestCase):

    def setUp(self):
        """Set up a mock environment for each test."""
        self.mock_config = configparser.ConfigParser()
        self.mock_config.read_dict(
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
                    "failure_penalties": "-1, -5",
                    "weighted_selection_enabled": "false",
                    "top_tier_size": "50",
                    "top_tier_load_percentage": "70",
                    "elo_time_decay_enabled": "true",
                    "elo_decay_half_life_hours": "24",
                    "elo_max_result_age_hours": "168",
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

        patcher_config = patch(
            "configparser.ConfigParser.read", return_value=self.mock_config
        )
        self.addCleanup(patcher_config.stop)
        patcher_config.start()

        # PATCH THE IMPORT IN PROXY_MANAGER, NOT THE DEFINITION
        patcher_db = patch("src.core.proxy_manager.DatabaseManager", spec=DatabaseManager)
        self.addCleanup(patcher_db.stop)
        self.MockDatabaseManager = patcher_db.start()

        self.manager = ProxyManager("dummy_path.ini")
        self.mock_db_instance = self.MockDatabaseManager.return_value

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

    def test_process_feedback_handles_unknown_proxy(self):
        """Test that process_feedback handles unknown proxy gracefully."""
        self.manager.predefined_sources.add("source1")
        self.manager.source_stats["source1"] = {}
        
        # Should not raise
        self.manager.process_feedback("source1", "http://unknown:80", 200)

    # ========== Lock Tests ==========
    
    def test_get_source_lock_creates_and_returns_lock(self):
        """Test that _get_source_lock creates a new lock for each source."""
        lock1 = self.manager._get_source_lock("source1")
        lock2 = self.manager._get_source_lock("source2")
        lock1_again = self.manager._get_source_lock("source1")
        
        self.assertIs(lock1, lock1_again)
        self.assertIsNot(lock1, lock2)
        # threading.Lock is a factory function, use type() to get actual type
        self.assertIsInstance(lock1, type(threading.Lock()))

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
        """Test that lower latency results in higher score."""
        import time
        
        # Low latency proxy (200ms)
        stat_low = self.manager._get_new_proxy_stat()
        for i in range(50):
            stat_low["recent_results"].append([time.time() - i, True, 200])
        
        # High latency proxy (1500ms)
        stat_high = self.manager._get_new_proxy_stat()
        for i in range(50):
            stat_high["recent_results"].append([time.time() - i, True, 1500])
        
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


class TestDatabaseManager(unittest.TestCase):
    """Test DatabaseManager methods with mocked psycopg2."""

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


if __name__ == "__main__":
    unittest.main()
