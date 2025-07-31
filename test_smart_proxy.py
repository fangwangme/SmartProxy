import unittest
from unittest.mock import patch, MagicMock, ANY
import os
import time
import configparser

# Mock logger before importing the main module
from logger import logger

logger.disabled = True

# Mock psycopg2 before it's imported by the main module
sys_modules = {
    "psycopg2": MagicMock(),
    "psycopg2.pool": MagicMock(),
    "psycopg2.extras": MagicMock(),
}
with patch.dict("sys.modules", sys_modules):
    import smart_proxy


class TestSmartProxyV3(unittest.TestCase):

    def setUp(self):
        """Set up a clean environment for each test."""
        self.config_path = "test_config.ini"
        config = configparser.ConfigParser()
        config["database"] = {
            "host": "fake",
            "port": "5432",
            "dbname": "fake",
            "user": "fake",
            "password": "fake",
        }
        config["validator"] = {"validation_workers": "10"}
        config["scheduler"] = {"validation_interval_seconds": "60"}
        config["source_pool"] = {"score_threshold_ban": "-5", "cooldown_minutes": "10"}
        config["proxy_source_test"] = {
            "url": "http://fake.com/proxies.txt",
            "update_interval_minutes": "5",
            "default_protocol": "http",
        }
        with open(self.config_path, "w") as f:
            config.write(f)

        # We patch the DatabaseManager to avoid actual DB calls
        self.db_mock = MagicMock()
        with patch("smart_proxy.DatabaseManager", return_value=self.db_mock):
            self.manager = smart_proxy.ProxyManager(self.config_path)

    def tearDown(self):
        """Clean up created files."""
        if os.path.exists(self.config_path):
            os.remove(self.config_path)

    @patch("requests.get")
    def test_fetch_and_parse_source(self, mock_get):
        """Test fetching, parsing, and inserting proxies."""
        mock_response = MagicMock()
        mock_response.text = "1.1.1.1:8080\n2.2.2.2:8080"
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        test_job = self.manager.fetcher_jobs[0]
        self.manager._fetch_and_parse_source(test_job)

        # Verify that the database insert method was called with correctly parsed data
        self.db_mock.insert_proxies.assert_called_once()
        call_args = self.db_mock.insert_proxies.call_args[0][0]
        self.assertIn(("http", "1.1.1.1", 8080), call_args)
        self.assertIn(("http", "2.2.2.2", 8080), call_args)

    @patch("requests.get")
    def test_validation_cycle(self, mock_get):
        """Test the validation logic."""
        # Mock DB to return a proxy that needs validation
        proxy_to_validate = [(1, "http", "1.1.1.1", 8080)]
        self.db_mock.get_proxies_to_validate.return_value = proxy_to_validate

        # Mock a successful validation response from requests
        mock_response = MagicMock()
        mock_response.json.return_value = {"headers": {}}
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        self.manager._run_validation_cycle()

        # Verify that the DB was updated with a successful result
        self.db_mock.update_proxy_validation_result.assert_called_with(
            1, True, ANY, "elite"
        )

    def test_get_proxy_and_feedback(self):
        """Test the in-memory get/feedback cycle."""
        source = "test_source"
        proxy_url = "http://1.1.1.1:8080"

        # Manually set up the in-memory state
        self.manager.active_proxies = {proxy_url}
        self.manager._ensure_source_exists(source)

        # Get proxy
        self.assertEqual(self.manager.get_proxy(source), proxy_url)

        # Give failure feedback
        self.manager.process_feedback(source, proxy_url, "failure")
        self.assertEqual(self.manager.source_stats[source][proxy_url]["score"], -5)

        # Give success feedback
        self.manager.process_feedback(source, proxy_url, "success")
        self.assertEqual(self.manager.source_stats[source][proxy_url]["score"], -4)

    def test_cold_start_logic(self):
        """Test the synchronous initial load on cold start."""
        with patch(
            "smart_proxy.ProxyManager._run_validation_cycle"
        ) as mock_validate, patch(
            "smart_proxy.ProxyManager._fetch_and_parse_source"
        ) as mock_fetch:

            # Simulate no pickle file and empty DB
            self.db_mock.get_active_proxies.return_value = set()

            with patch("os.path.exists", return_value=False):
                manager = smart_proxy.load_proxy_manager(
                    "fake_pickle.pkl", self.config_path
                )

            # Verify that fetch and validate were called synchronously
            self.assertTrue(mock_fetch.called)
            self.assertTrue(mock_validate.called)


if __name__ == "__main__":
    unittest.main(verbosity=2)
