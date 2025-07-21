import unittest
from unittest.mock import patch, mock_open, MagicMock
import os
import sys
import pickle
import configparser

# Add the parent directory to the path to allow importing smart_proxy
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Mock the logger before importing the main module
mock_logger = MagicMock()
sys.modules["logger"] = MagicMock()
sys.modules["logger"].logger = mock_logger

from smart_proxy import ProxyManager, load_proxy_manager


class TestProxyManager(unittest.TestCase):
    """
    Unit tests for the ProxyManager and related functionalities.
    """

    def setUp(self):
        """Set up a clean environment for each test."""
        self.DATA_DIR = "test_data"
        self.CONFIG_FILE_PATH = os.path.join(self.DATA_DIR, "config.ini")
        self.PROXY_FILE_PATH = os.path.join(self.DATA_DIR, "proxies.txt")
        self.PICKLE_FILE_PATH = os.path.join(self.DATA_DIR, "proxy_manager.pkl")

        os.makedirs(self.DATA_DIR, exist_ok=True)

        # Reset mocked logger calls
        mock_logger.reset_mock()

    def tearDown(self):
        """Clean up created files after each test."""
        for file_path in [
            self.CONFIG_FILE_PATH,
            self.PROXY_FILE_PATH,
            self.PICKLE_FILE_PATH,
        ]:
            if os.path.exists(file_path):
                os.remove(file_path)
        if os.path.exists(self.DATA_DIR):
            os.rmdir(self.DATA_DIR)

    def test_load_sources_from_config_file_exists(self):
        """Test loading sources from an existing config file."""
        config = configparser.ConfigParser()
        config["sources"] = {"default_sources": "source1, source2"}
        with open(self.CONFIG_FILE_PATH, "w") as f:
            config.write(f)

        manager = ProxyManager()
        manager.load_sources_from_config(self.CONFIG_FILE_PATH)
        self.assertEqual(manager.sources, {"source1", "source2"})

    def test_load_sources_from_config_file_not_exists(self):
        """Test loading sources when config file does not exist."""
        manager = ProxyManager()
        manager.load_sources_from_config(self.CONFIG_FILE_PATH)
        self.assertEqual(manager.sources, {"insolvencydirect", "test"})

    def test_add_source(self):
        """Test adding a new source."""
        manager = ProxyManager()
        manager.load_sources_from_config(self.CONFIG_FILE_PATH)  # Loads defaults

        result = manager.add_source("new_source", self.CONFIG_FILE_PATH)
        self.assertTrue(result)
        self.assertIn("new_source", manager.sources)

        # Verify it was written to the config file
        config = configparser.ConfigParser()
        config.read(self.CONFIG_FILE_PATH)
        self.assertIn("new_source", config.get("sources", "default_sources"))

    def test_add_existing_source(self):
        """Test adding a source that already exists."""
        manager = ProxyManager()
        manager.load_sources_from_config(self.CONFIG_FILE_PATH)  # Loads defaults
        result = manager.add_source("test", self.CONFIG_FILE_PATH)
        self.assertFalse(result)

    def test_load_proxies_from_file(self):
        """Test loading valid, duplicate, and malformed proxies."""
        proxy_content = (
            "http:1.1.1.1:8080\n"
            "http:2.2.2.2:8080\n"
            "http:1.1.1.1:8080\n"  # Duplicate
            "badprotocol:3.3.3.3:8080\n"  # Malformed
            "http:4.4.4.4:99999\n"  # Malformed port
        )
        with open(self.PROXY_FILE_PATH, "w") as f:
            f.write(proxy_content)

        manager = ProxyManager()
        manager.load_sources_from_config(self.CONFIG_FILE_PATH)
        result = manager.load_proxies_from_file(self.PROXY_FILE_PATH)

        self.assertEqual(len(manager.proxies), 2)
        self.assertEqual(result["added"], 2)
        self.assertEqual(result["total"], 2)

        proxy_urls = {p["url"] for p in manager.proxies}
        self.assertIn("http://1.1.1.1:8080", proxy_urls)
        self.assertIn("http://2.2.2.2:8080", proxy_urls)

    def test_pickle_persistence(self):
        """Test saving and loading the manager state using pickle."""
        # 1. Create and modify a manager instance
        manager = ProxyManager()
        manager.load_sources_from_config(self.CONFIG_FILE_PATH)
        manager.load_proxies_from_file(self.PROXY_FILE_PATH)  # empty for now
        manager.add_source("pickle_source", self.CONFIG_FILE_PATH)

        # 2. Save its state
        manager.save_state_to_pickle(self.PICKLE_FILE_PATH)
        self.assertTrue(os.path.exists(self.PICKLE_FILE_PATH))

        # 3. Load into a new manager using the factory
        new_manager = load_proxy_manager(
            self.PICKLE_FILE_PATH, self.CONFIG_FILE_PATH, self.PROXY_FILE_PATH
        )

        # 4. Verify the state was restored
        self.assertIsInstance(new_manager, ProxyManager)
        self.assertIn("pickle_source", new_manager.sources)

    def test_feedback_and_banning(self):
        """Test the feedback mechanism and proxy banning."""
        proxy_content = "http:5.5.5.5:8080\n"
        with open(self.PROXY_FILE_PATH, "w") as f:
            f.write(proxy_content)

        manager = ProxyManager()
        manager.load_sources_from_config(self.CONFIG_FILE_PATH)
        manager.load_proxies_from_file(self.PROXY_FILE_PATH)

        proxy_url = "http://5.5.5.5:8080"
        source = "test"

        # Check initial state
        self.assertIn(proxy_url, manager.available_pools[source])

        # Simulate failures until banning
        from smart_proxy import CONSECUTIVE_FAILURE_THRESHOLD

        for _ in range(CONSECUTIVE_FAILURE_THRESHOLD):
            feedback = {"source": source, "proxy": proxy_url, "status": "failure"}
            manager.process_feedback(feedback)

        # Verify proxy is banned and removed from available pool
        self.assertTrue(manager.stats[source][proxy_url]["is_banned"])
        self.assertNotIn(proxy_url, manager.available_pools[source])

        # Simulate a success
        feedback = {
            "source": source,
            "proxy": proxy_url,
            "status": "success",
            "response_time_ms": 100,
        }
        manager.process_feedback(feedback)

        # Verify proxy is unbanned and back in the pool
        self.assertFalse(manager.stats[source][proxy_url]["is_banned"])
        self.assertIn(proxy_url, manager.available_pools[source])
        self.assertEqual(manager.stats[source][proxy_url]["consecutive_failures"], 0)

    def test_revival_logic(self):
        """Test the proxy revival logic when the pool is low."""
        # Setup: 2 proxies, one successful, one not. Both banned.
        proxies = ["http:6.6.6.6:8080", "http:7.7.7.7:8080"]
        proxy_urls = [p.replace(":", "://") for p in proxies]

        with open(self.PROXY_FILE_PATH, "w") as f:
            f.write("\n".join(proxies))

        manager = ProxyManager()
        # Set a very low pool size to trigger revival immediately
        from smart_proxy import MIN_POOL_SIZE

        original_min_pool_size = MIN_POOL_SIZE
        # We need to modify the global, or patch it inside the manager instance
        # For simplicity, we'll just assume we can set it on the instance for this test
        manager.MIN_POOL_SIZE = 1

        manager.load_sources_from_config(self.CONFIG_FILE_PATH)
        manager.load_proxies_from_file(self.PROXY_FILE_PATH)

        source = "test"
        # Manually set stats
        manager.stats[source][proxy_urls[0]]["success_count"] = 1
        manager.stats[source][proxy_urls[0]]["is_banned"] = True
        manager.stats[source][proxy_urls[1]]["is_banned"] = True
        manager.available_pools[source] = []  # Empty the pool

        # 1. Test revival of historically successful proxy
        revived_proxy = manager.get_proxy_for_source(source)
        self.assertEqual(revived_proxy, proxy_urls[0])
        self.assertFalse(manager.stats[source][proxy_urls[0]]["is_banned"])

        # 2. Test revival of random proxy when no successful ones are available
        manager.stats[source][proxy_urls[0]]["is_banned"] = True
        manager.stats[source][proxy_urls[0]]["success_count"] = 0
        manager.available_pools[source] = []  # Empty the pool again

        revived_proxy_2 = manager.get_proxy_for_source(source)
        self.assertIn(revived_proxy_2, proxy_urls)
        self.assertFalse(manager.stats[source][revived_proxy_2]["is_banned"])

        # Restore original value if necessary for other tests, though setUp handles isolation
        # smart_proxy.MIN_POOL_SIZE = original_min_pool_size


if __name__ == "__main__":
    unittest.main(verbosity=2)
