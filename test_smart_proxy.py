import unittest
import configparser
from unittest.mock import MagicMock, patch, call

# It's better to import the classes directly for easier mocking
from smart_proxy import ProxyManager, DatabaseManager


class TestProxyManager(unittest.TestCase):

    def setUp(self):
        """Set up a mock environment for each test."""
        # Create a mock config parser object
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
                    "validation_supplement_threshold": "100",
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
                },
                "proxy_source_A": {
                    "url": "http://source-a.com",
                    "update_interval_minutes": "10",
                },
            }
        )

        # Patch the config parser's read method to return our mock config
        patcher_config = patch(
            "configparser.ConfigParser.read", return_value=self.mock_config
        )
        self.addCleanup(patcher_config.stop)
        patcher_config.start()

        # Patch the entire DatabaseManager to avoid actual DB connections
        patcher_db = patch("smart_proxy.DatabaseManager", spec=DatabaseManager)
        self.addCleanup(patcher_db.stop)
        self.MockDatabaseManager = patcher_db.start()

        # Instantiate the class we are testing
        self.manager = ProxyManager("dummy_path.ini")
        # Get a reference to the mocked DB instance
        self.mock_db_instance = self.MockDatabaseManager.return_value

    def test_reload_sources_add_and_remove(self):
        """Test that the reload_sources method correctly identifies changes."""
        # --- Setup new config state ---
        new_config_dict = self.mock_config._sections.copy()
        new_config_dict["sources"][
            "predefined_sources"
        ] = "source2,source3"  # Add source3, remove source1
        new_config_dict["proxy_source_B"] = {  # Add source B
            "url": "http://source-b.com",
            "update_interval_minutes": "5",
        }
        del new_config_dict["proxy_source_A"]  # Remove source A

        # Mock the config parser to return the new config on the next 'read' call
        new_mock_config = configparser.ConfigParser()
        new_mock_config.read_dict(new_config_dict)
        with patch("configparser.ConfigParser.read") as mock_read:
            # The __init__ call reads it once. The reload_sources call will read it again.
            mock_read.return_value = new_mock_config

            # --- Execute ---
            result = self.manager.reload_sources()

        # --- Assert ---
        self.assertIn(
            "proxy_source_B", [job["name"] for job in self.manager.fetcher_jobs]
        )
        self.assertNotIn(
            "proxy_source_A", [job["name"] for job in self.manager.fetcher_jobs]
        )
        self.assertIn("source3", self.manager.predefined_sources)
        self.assertNotIn("source1", self.manager.predefined_sources)

        self.assertEqual(result["added_fetcher_jobs"], ["proxy_source_B"])
        self.assertEqual(result["removed_fetcher_jobs"], ["proxy_source_A"])
        self.assertEqual(result["added_predefined_sources"], ["source3"])
        self.assertEqual(result["removed_predefined_sources"], ["source1"])

    def test_validation_cycle_supplements_with_eligible_proxies(self):
        """
        Test that the validation cycle correctly fetches eligible failed proxies
        when the initial pool is below the threshold.
        """
        # --- Setup Mocks ---
        # Initial pool is small (2 proxies)
        self.mock_db_instance.get_proxies_to_validate.return_value = [
            {"id": 1, "protocol": "http", "ip": "1.1.1.1", "port": 80},
            {"id": 2, "protocol": "http", "ip": "2.2.2.2", "port": 80},
        ]
        # Eligible failed proxies are available
        self.mock_db_instance.get_eligible_failed_proxies.return_value = [
            {"id": 3, "protocol": "http", "ip": "3.3.3.3", "port": 80},
            {"id": 4, "protocol": "http", "ip": "4.4.4.4", "port": 80},
        ]
        # Mock the actual validation to do nothing
        with patch.object(
            self.manager, "_validate_proxy", return_value=True
        ) as mock_validate:
            # --- Execute ---
            self.manager._run_validation_cycle()

        # --- Assert ---
        # 1. Check if it tried to get eligible proxies
        self.mock_db_instance.get_eligible_failed_proxies.assert_called_once_with(
            window_minutes=30,
            max_attempts=5,
            limit=98,  # 100 (threshold) - 2 (initial) = 98
        )

        # 2. Check that counters were updated for ALL proxies (initial + supplemented)
        self.mock_db_instance.update_validation_counters.assert_called_once()
        # Get the arguments passed to the mock
        args, _ = self.mock_db_instance.update_validation_counters.call_args
        # The first argument is the list of proxy IDs
        updated_ids = args[0]
        self.assertCountEqual(updated_ids, [1, 2, 3, 4])

        # 3. Check that validation was called for all 4 proxies
        self.assertEqual(mock_validate.call_count, 4)
        self.assertIn(call(1, "http://1.1.1.1:80"), mock_validate.call_args_list)
        self.assertIn(call(4, "http://4.4.4.4:80"), mock_validate.call_args_list)

    def test_validation_cycle_does_not_supplement_if_above_threshold(self):
        """
        Test that the validation cycle does NOT fetch failed proxies if the
        initial pool is large enough.
        """
        # --- Setup Mocks ---
        # Initial pool is large (101 proxies)
        initial_proxies = [
            {"id": i, "protocol": "http", "ip": f"1.1.1.{i}", "port": 80}
            for i in range(101)
        ]
        self.mock_db_instance.get_proxies_to_validate.return_value = initial_proxies

        with patch.object(self.manager, "_validate_proxy", return_value=True):
            # --- Execute ---
            self.manager._run_validation_cycle()

        # --- Assert ---
        # Assert that we did NOT try to get more proxies
        self.mock_db_instance.get_eligible_failed_proxies.assert_not_called()
        # Assert counters were updated only for the initial proxies
        self.mock_db_instance.update_validation_counters.assert_called_once()
        args, _ = self.mock_db_instance.update_validation_counters.call_args
        updated_ids = args[0]
        self.assertCountEqual(updated_ids, [p["id"] for p in initial_proxies])


if __name__ == "__main__":
    unittest.main()
