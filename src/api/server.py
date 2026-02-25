# -*- coding: utf-8 -*-
import os
import sys
import argparse
import signal
import threading
from datetime import datetime, timedelta
from typing import Optional

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from src.utils.logger import logger, setup_logging
from src.core.proxy_manager import ProxyManager

def create_app(proxy_manager: ProxyManager):
    app = Flask(__name__, static_folder="../../dashboard/dist")
    CORS(app)
    
    @app.before_request
    def restrict_dashboard_access():
        """Restrict access to the dashboard and dashboard-specific APIs by IP."""
        # Only apply restriction if allowed_ips is configured
        if not proxy_manager.allowed_dashboard_ips:
            return

        # Define paths that are considered part of the dashboard/monitoring
        # 1. Root and frontend static files (handled by serve_frontend)
        # 2. Dashboard-specific APIs starting with /api/
        # 3. Monitoring endpoints /health and /metrics (optional, but usually part of dashboard)
        path = request.path
        is_dashboard_api = path.startswith("/api/")
        is_monitoring = path in ["/health", "/metrics"]
        
        # We also need to check if it's hitting the serve_frontend route (root or file paths)
        # In Flask, the static folder and catch-all routes cover the frontend.
        # If it's not a proxy/feedback/reload API, it's likely dashboard-related.
        proxy_apis = ["/get-proxy", "/get-premium-proxy", "/feedback", "/reload-sources", "/backup-stats"]
        is_frontend = path == "/" or path not in proxy_apis
        
        if is_dashboard_api or is_monitoring or is_frontend:
            # Support X-Forwarded-For header for getting real client IP behind proxy
            client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
            if ',' in client_ip:
                client_ip = client_ip.split(',')[0].strip()
            
            # Always allow local loopback addresses
            if client_ip in ["127.0.0.1", "::1"]:
                return

            if client_ip not in proxy_manager.allowed_dashboard_ips:
                logger.warning(f"Unauthorized dashboard access attempt from IP: {client_ip} for path: {path}")
                return jsonify({"error": "Forbidden: Your IP is not authorized to access the dashboard."}), 403

    # Store proxy_manager in app config or closure, but here passing it explicitly 
    # to routes via closure or global usage might be cleaner if we use a blueprint.
    # For now, let's keep it simple and register routes within this function
    # or rely on the passed instance if we define handlers locally.
    
    # Actually, defining routes inside create_app captures proxy_manager in closure.
    
    @app.route("/health", methods=["GET"])
    def health_check():
        """Health check endpoint for monitoring."""
        with proxy_manager.lock:
            active_count = len(proxy_manager.active_proxies)
            premium_count = len(proxy_manager.premium_proxies)
            sources_count = len(proxy_manager.predefined_sources)
        
        return jsonify({
            "status": "healthy",
            "active_proxies": active_count,
            "premium_proxies": premium_count,
            "sources": sources_count,
            "is_validating": proxy_manager.is_validating,
        })

    @app.route("/metrics", methods=["GET"])
    def metrics():
        """Prometheus-compatible metrics endpoint."""
        with proxy_manager.lock:
            active_count = len(proxy_manager.active_proxies)
            premium_count = len(proxy_manager.premium_proxies)
            sources_count = len(proxy_manager.predefined_sources)
            
            # Calculate total stats across all sources
            total_success = 0
            total_failure = 0
            for source_stats in proxy_manager.source_stats.values():
                for stat in source_stats.values():
                    total_success += stat.get("success_count", 0)
                    total_failure += stat.get("failure_count", 0)
        
        total_requests = total_success + total_failure
        success_rate = (total_success / total_requests * 100) if total_requests > 0 else 0
        
        # Prometheus text format
        metrics_text = f"""# HELP smartproxy_active_proxies Number of active proxies
# TYPE smartproxy_active_proxies gauge
smartproxy_active_proxies {active_count}

# HELP smartproxy_premium_proxies Number of premium proxies
# TYPE smartproxy_premium_proxies gauge
smartproxy_premium_proxies {premium_count}

# HELP smartproxy_sources_total Number of configured sources
# TYPE smartproxy_sources_total gauge
smartproxy_sources_total {sources_count}

# HELP smartproxy_requests_total Total requests processed
# TYPE smartproxy_requests_total counter
smartproxy_requests_success_total {total_success}
smartproxy_requests_failure_total {total_failure}

# HELP smartproxy_success_rate_percent Current success rate percentage
# TYPE smartproxy_success_rate_percent gauge
smartproxy_success_rate_percent {success_rate:.2f}

# HELP smartproxy_is_validating Whether validation is in progress
# TYPE smartproxy_is_validating gauge
smartproxy_is_validating {1 if proxy_manager.is_validating else 0}
"""
        return metrics_text, 200, {"Content-Type": "text/plain; charset=utf-8"}

    @app.route("/get-proxy", methods=["GET"])
    def get_proxy_route():
        source = request.args.get("source")
        if not source:
            return jsonify({"error": "Query parameter 'source' is required."}), 400
        proxy_url = proxy_manager.get_proxy(source)
        if proxy_url:
            return jsonify({"http": proxy_url, "https": proxy_url})
        else:
            return (
                jsonify(
                    {"error": f"No available proxy for source '{source}' at the moment."}
                ),
                404,
            )

    @app.route("/get-premium-proxy", methods=["GET"])
    def get_premium_proxy_route():
        """Get a premium (highest quality) proxy for Playwright and high-reliability use cases."""
        proxy_url = proxy_manager.get_premium_proxy()
        if proxy_url:
            return jsonify({"http": proxy_url, "https": proxy_url, "premium": True})
        else:
            return (
                jsonify(
                    {"error": "No premium proxy available at the moment."}
                ),
                404,
            )

    @app.route("/feedback", methods=["POST"])
    def feedback_route():
        data = request.json
        source = data.get("source")
        proxy_url = data.get("proxy")
        status_code = data.get("status")
        resp_time = data.get("response_time_ms")
        logger.debug(
            f"Handled feedback: {source} - {status_code} - {proxy_url} - {resp_time}"
        )
        if not all([source, proxy_url]) or not isinstance(status_code, int):
            return (
                jsonify(
                    {
                        "error": "Invalid feedback data. 'source', 'proxy', and 'status_code' (int) are required."
                    }
                ),
                400,
            )
        proxy_manager.process_feedback(source, proxy_url, status_code, resp_time)
        return jsonify({"message": "Feedback received."})

    @app.route("/reload-sources", methods=["POST"])
    def reload_sources_route():
        """
        API endpoint to dynamically reload proxy sources from the config file.
        """
        try:
            result = proxy_manager.reload_sources()
            return (
                jsonify(
                    {
                        "status": "success",
                        "message": "Configuration and sources reloaded.",
                        "details": result,
                    }
                ),
                200,
            )
        except Exception as e:
            logger.error(f"Error during source reload via API: {e}", exc_info=True)
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "An internal error occurred during reload.",
                    }
                ),
                500,
            )

    @app.route("/backup-stats", methods=["POST"])
    def backup_stats_route():
        """API endpoint to manually trigger stats backup."""
        try:
            result = proxy_manager.backup_stats()
            status_code = 200 if result["status"] == "success" else 500
            return jsonify(result), status_code
        except Exception as e:
            logger.error(f"Error during stats backup via API: {e}", exc_info=True)
            return jsonify({"status": "error", "message": str(e)}), 500

    @app.route("/api/sources", methods=["GET"])
    def get_sources():
        return jsonify(sorted(list(proxy_manager.dashboard_sources)))

    @app.route("/api/stats/daily", methods=["GET"])
    def get_daily_stats_route():
        source = request.args.get("source")
        date = request.args.get("date")
        if not all([source, date]):
            return (
                jsonify({"error": "'source' and 'date' query parameters are required."}),
                400,
            )
        stats = proxy_manager.db.get_daily_stats(source, date)
        if stats:
            total = stats["total_success"] + stats["total_failure"]
            success_rate = (stats["total_success"] / total * 100) if total > 0 else 0
            return jsonify(
                {
                    "total_requests": total,
                    "total_success": stats["total_success"],
                    "success_rate": round(success_rate, 2),
                }
            )
        return jsonify({"total_requests": 0, "total_success": 0, "success_rate": 0})

    @app.route("/api/stats/timeseries", methods=["GET"])
    def get_timeseries_stats_route():
        source = request.args.get("source")
        date_str = request.args.get("date")
        interval = request.args.get("interval", "10", type=int)
        if not all([source, date_str]):
            return (
                jsonify({"error": "'source' and 'date' query parameters are required."}),
                400,
            )
        valid_intervals = [2, 5, 10, 30, 60]
        if interval not in valid_intervals:
            return jsonify({"error": f"'interval' must be one of {valid_intervals}."}), 400
        
        # Get raw stats from DB (sparse data)
        stats = proxy_manager.db.get_timeseries_stats(source, date_str, interval)
        
        # Convert stats to dictionary for O(1) lookup
        # Key: HH:MM string, Value: row data
        stats_map = {}
        if stats:
            for row in stats:
                time_key = row["interval_start"].strftime("%H:%M")
                stats_map[time_key] = row

        # Generate full list of time slots for the day
        results = []
        try:
            start_date = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            return jsonify({"error": "Invalid date format. Use YYYY-MM-DD."}), 400

        current_time = start_date
        end_time = start_date + timedelta(days=1)
        
        while current_time < end_time:
            time_str = current_time.strftime("%H:%M")
            
            if time_str in stats_map:
                row = stats_map[time_str]
                total = row["success"] + row["failure"]
                success_rate = (row["success"] / total * 100) if total > 0 else 0
                results.append({
                    "time": time_str,
                    "success_rate": round(success_rate, 2),
                    "total_requests": total,
                    "success_count": row["success"],
                })
            else:
                # Fill missing data with 0
                results.append({
                    "time": time_str,
                    "success_rate": 0,
                    "total_requests": 0,
                    "success_count": 0,
                })
            
            current_time += timedelta(minutes=interval)

        return jsonify(results)

    @app.route("/", defaults={"path": ""})
    @app.route("/<path:path>")
    def serve_frontend(path):
        if path != "" and os.path.exists(os.path.join(app.static_folder, path)):
            return send_from_directory(app.static_folder, path)
        else:
            return send_from_directory(app.static_folder, "index.html")

    return app
