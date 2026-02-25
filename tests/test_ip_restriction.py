# -*- coding: utf-8 -*-
import unittest
from unittest.mock import MagicMock, patch
from flask import Flask
import json

from src.core.proxy_manager import ProxyManager
from src.api.server import create_app

class TestIPRestriction(unittest.TestCase):
    def setUp(self):
        # Mock ProxyManager
        self.mock_proxy_manager = MagicMock(spec=ProxyManager)
        self.mock_proxy_manager.allowed_dashboard_ips = []
        self.mock_proxy_manager.lock = MagicMock()
        self.mock_proxy_manager.active_proxies = set()
        self.mock_proxy_manager.premium_proxies = []
        self.mock_proxy_manager.predefined_sources = set()
        self.mock_proxy_manager.dashboard_sources = set()
        self.mock_proxy_manager.is_validating = False
        
        # Create Flask app
        self.app = create_app(self.mock_proxy_manager)
        self.client = self.app.test_client()

    def test_no_restriction_when_ips_not_configured(self):
        """When allowed_dashboard_ips is empty, all IPs should have access."""
        self.mock_proxy_manager.allowed_dashboard_ips = []
        
        # Test dashboard access
        response = self.client.get("/", environ_overrides={'REMOTE_ADDR': '1.2.3.4'})
        # Since static folder might not exist in test env, we look for 200 or 404 (not 403)
        # But "/" is explicitly handled in server.py to return index.html
        self.assertNotEqual(response.status_code, 403)
        
        # Test API access
        response = self.client.get("/api/sources", environ_overrides={'REMOTE_ADDR': '1.2.3.4'})
        self.assertNotEqual(response.status_code, 403)

    def test_restriction_when_ips_configured(self):
        """When allowed_dashboard_ips is set, unauthorized IPs should be blocked."""
        self.mock_proxy_manager.allowed_dashboard_ips = ["127.0.0.1", "192.168.1.1"]
        
        # Unauthorized IP
        unauth_ip = "8.8.8.8"
        
        # 1. Block dashboard root
        response = self.client.get("/", environ_overrides={'REMOTE_ADDR': unauth_ip})
        self.assertEqual(response.status_code, 403)
        self.assertIn("Forbidden", response.get_json()["error"])
        
        # 2. Block dashboard API
        response = self.client.get("/api/sources", environ_overrides={'REMOTE_ADDR': unauth_ip})
        self.assertEqual(response.status_code, 403)
        
        # 3. Block monitoring
        response = self.client.get("/health", environ_overrides={'REMOTE_ADDR': unauth_ip})
        self.assertEqual(response.status_code, 403)

    def test_allow_authorized_ip(self):
        """Authorized IPs should still have access."""
        self.mock_proxy_manager.allowed_dashboard_ips = ["127.0.0.1"]
        auth_ip = "127.0.0.1"
        
        # Access health (should not be 403)
        response = self.client.get("/health", environ_overrides={'REMOTE_ADDR': auth_ip})
        self.assertNotEqual(response.status_code, 403)
        
        # Access API (should not be 403)
        response = self.client.get("/api/sources", environ_overrides={'REMOTE_ADDR': auth_ip})
        self.assertNotEqual(response.status_code, 403)

    def test_proxy_api_always_allowed(self):
        """Core proxy APIs should be accessible from any IP (as they are usually used by services)."""
        self.mock_proxy_manager.allowed_dashboard_ips = ["127.0.0.1"]
        unauth_ip = "8.8.8.8"
        
        # Mock get_proxy to return something
        self.mock_proxy_manager.get_proxy.return_value = "http://proxy:8080"
        
        # Access /get-proxy (should be 200, not 403)
        response = self.client.get("/get-proxy?source=default", environ_overrides={'REMOTE_ADDR': unauth_ip})
        self.assertEqual(response.status_code, 200)
        
        # Access /feedback (should be 400 because of missing data, but NOT 403)
        response = self.client.post("/feedback", json={}, environ_overrides={'REMOTE_ADDR': unauth_ip})
        self.assertEqual(response.status_code, 400)

if __name__ == "__main__":
    unittest.main()
