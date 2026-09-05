# -*- coding: utf-8 -*-
import unittest
from unittest.mock import MagicMock

from src.core.proxy_manager import ProxyManager
from src.api.server import create_app

class TestIPRestriction(unittest.TestCase):
    def setUp(self):
        # Mock ProxyManager
        self.mock_proxy_manager = MagicMock(spec=ProxyManager)
        self.mock_proxy_manager.allowed_ips = []
        self.mock_proxy_manager.trust_proxy_headers = False
        self.mock_proxy_manager.trusted_proxy_ips = []
        self.mock_proxy_manager.lock = MagicMock()
        self.mock_proxy_manager.active_proxies = set()
        self.mock_proxy_manager.premium_proxies = []
        self.mock_proxy_manager.predefined_sources = set()
        self.mock_proxy_manager.dashboard_sources = set()
        self.mock_proxy_manager.is_validating = False
        self.mock_proxy_manager.is_valid_feedback_status.return_value = True
        
        # Create Flask app
        self.app = create_app(self.mock_proxy_manager)
        self.client = self.app.test_client()

    def test_external_endpoint_blocks_remote_when_not_in_allowed_ips(self):
        """External endpoints should reject remote clients not in allowed_ips."""
        self.mock_proxy_manager.allowed_ips = []

        response = self.client.get(
            "/get-proxy?source=default", environ_overrides={"REMOTE_ADDR": "8.8.8.8"}
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("Forbidden", response.get_json()["error"])

    def test_external_endpoint_allows_configured_remote_ip(self):
        """Configured remote IPs should access external endpoints."""
        self.mock_proxy_manager.allowed_ips = ["8.8.8.8"]

        response = self.client.post(
            "/feedback",
            json={},
            environ_overrides={"REMOTE_ADDR": "8.8.8.8"},
        )
        self.assertNotEqual(response.status_code, 403)
        self.assertEqual(response.status_code, 400)

    def test_external_endpoint_always_allows_localhost(self):
        """Localhost should access external endpoints even when not in allowed_ips."""
        self.mock_proxy_manager.allowed_ips = ["192.168.1.1"]
        self.mock_proxy_manager.allocate_proxy.return_value = {
            "proxy": "http://proxy:8080",
            "source": "default",
        }

        response = self.client.get(
            "/get-proxy?source=default", environ_overrides={"REMOTE_ADDR": "127.0.0.1"}
        )
        self.assertNotEqual(response.status_code, 403)
        self.assertEqual(response.status_code, 200)

    def test_internal_endpoints_are_localhost_only(self):
        """Internal endpoints should reject all non-local clients."""
        self.mock_proxy_manager.allowed_ips = ["8.8.8.8"]

        for path in [
            "/health",
            "/live",
            "/ready",
            "/metrics",
            "/reload-sources",
            "/backup-stats",
        ]:
            response = self.client.get(
                path, environ_overrides={"REMOTE_ADDR": "8.8.8.8"}
            )
            if path in ["/reload-sources", "/backup-stats"]:
                response = self.client.post(
                    path, environ_overrides={"REMOTE_ADDR": "8.8.8.8"}
                )
            self.assertEqual(response.status_code, 403, f"{path} should be blocked")

    def test_spoofed_x_forwarded_for_does_not_bypass_internal_endpoint(self):
        """X-Forwarded-For should not turn a remote client into localhost."""
        response = self.client.get(
            "/health",
            headers={"X-Forwarded-For": "127.0.0.1"},
            environ_overrides={"REMOTE_ADDR": "8.8.8.8"},
        )

        self.assertEqual(response.status_code, 403)

    def test_trusted_proxy_x_forwarded_for_allows_external_endpoint(self):
        """X-Forwarded-For is used only when the direct peer is trusted."""
        self.mock_proxy_manager.trust_proxy_headers = True
        self.mock_proxy_manager.trusted_proxy_ips = ["10.0.0.10"]
        self.mock_proxy_manager.allowed_ips = ["8.8.8.8"]
        self.mock_proxy_manager.allocate_proxy.return_value = {
            "proxy": "http://proxy:8080",
            "source": "default",
        }

        response = self.client.get(
            "/get-proxy?source=default",
            headers={"X-Forwarded-For": "8.8.8.8"},
            environ_overrides={"REMOTE_ADDR": "10.0.0.10"},
        )

        self.assertEqual(response.status_code, 200)

    def test_prepended_forwarded_value_cannot_override_trusted_boundary(self):
        self.mock_proxy_manager.trust_proxy_headers = True
        self.mock_proxy_manager.trusted_proxy_ips = ["192.0.2.10", "192.0.2.11"]
        self.mock_proxy_manager.allowed_ips = ["198.51.100.20"]
        self.mock_proxy_manager.allocate_proxy.return_value = {
            "proxy": "http://proxy:8080",
            "source": "default",
        }

        response = self.client.get(
            "/get-proxy?source=default",
            headers={
                "X-Forwarded-For": "203.0.113.99, 198.51.100.20, 192.0.2.11"
            },
            environ_overrides={"REMOTE_ADDR": "192.0.2.10"},
        )

        self.assertEqual(response.status_code, 200)

    def test_malformed_forwarded_address_is_rejected(self):
        self.mock_proxy_manager.trust_proxy_headers = True
        self.mock_proxy_manager.trusted_proxy_ips = ["192.0.2.10"]
        self.mock_proxy_manager.allowed_ips = ["198.51.100.20"]

        response = self.client.get(
            "/get-proxy?source=default",
            headers={"X-Forwarded-For": "198.51.100.20, not-an-address"},
            environ_overrides={"REMOTE_ADDR": "192.0.2.10"},
        )

        self.assertEqual(response.status_code, 403)

    def test_malformed_forwarded_chain_fails_closed_when_proxy_is_allowed(self):
        self.mock_proxy_manager.trust_proxy_headers = True
        self.mock_proxy_manager.trusted_proxy_ips = ["192.0.2.10"]
        self.mock_proxy_manager.allowed_ips = ["192.0.2.10", "198.51.100.20"]
        self.mock_proxy_manager.allocate_proxy.return_value = {
            "proxy": "http://proxy:8080",
            "source": "default",
        }

        response = self.client.get(
            "/get-proxy?source=default",
            headers={"X-Forwarded-For": "198.51.100.20, malformed"},
            environ_overrides={"REMOTE_ADDR": "192.0.2.10"},
        )

        self.assertEqual(response.status_code, 403)

    def test_ipv4_mapped_loopback_can_reach_internal_probe(self):
        self.mock_proxy_manager.liveness_status.return_value = {"status": "alive"}

        response = self.client.get(
            "/live",
            environ_overrides={"REMOTE_ADDR": "::ffff:127.0.0.1"},
        )

        self.assertEqual(response.status_code, 200)

    def test_multiple_trusted_forwarding_hops_resolve_first_untrusted_boundary(self):
        self.mock_proxy_manager.trust_proxy_headers = True
        self.mock_proxy_manager.trusted_proxy_ips = [
            "192.0.2.10",
            "192.0.2.11",
            "192.0.2.12",
        ]
        self.mock_proxy_manager.allowed_ips = ["198.51.100.20"]
        self.mock_proxy_manager.allocate_proxy.return_value = {
            "proxy": "http://proxy:8080",
            "source": "default",
        }

        response = self.client.get(
            "/get-proxy?source=default",
            headers={
                "X-Forwarded-For": "198.51.100.20, 192.0.2.12, 192.0.2.11"
            },
            environ_overrides={"REMOTE_ADDR": "192.0.2.10"},
        )

        self.assertEqual(response.status_code, 200)

    def test_untrusted_direct_peer_cannot_activate_forwarded_headers(self):
        self.mock_proxy_manager.trust_proxy_headers = True
        self.mock_proxy_manager.trusted_proxy_ips = ["192.0.2.10"]
        self.mock_proxy_manager.allowed_ips = ["198.51.100.20"]

        response = self.client.get(
            "/get-proxy?source=default",
            headers={"X-Forwarded-For": "198.51.100.20"},
            environ_overrides={"REMOTE_ADDR": "203.0.113.40"},
        )

        self.assertEqual(response.status_code, 403)

    def test_trusted_proxy_x_forwarded_for_still_cannot_bypass_internal_endpoint(self):
        """Internal endpoints use REMOTE_ADDR, not the resolved client IP."""
        self.mock_proxy_manager.trust_proxy_headers = True
        self.mock_proxy_manager.trusted_proxy_ips = ["10.0.0.10"]

        response = self.client.get(
            "/health",
            headers={"X-Forwarded-For": "127.0.0.1"},
            environ_overrides={"REMOTE_ADDR": "10.0.0.10"},
        )

        self.assertEqual(response.status_code, 403)

    def test_feedback_rejects_non_json_body(self):
        """Malformed or non-JSON feedback should return 400, not 500."""
        response = self.client.post(
            "/feedback",
            data="not-json",
            content_type="text/plain",
            environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
        )

        self.assertEqual(response.status_code, 400)

    def test_feedback_rejects_unknown_status(self):
        """Unknown status codes should not be counted as success."""
        self.mock_proxy_manager.is_valid_feedback_status.return_value = False

        response = self.client.post(
            "/feedback",
            json={"source": "default", "proxy": "http://proxy:8080", "status": 999},
            environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
        )

        self.assertEqual(response.status_code, 400)

if __name__ == "__main__":
    unittest.main()
