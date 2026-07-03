# -*- coding: utf-8 -*-
import os
import sys
import argparse
import signal
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from src.utils.logger import logger, setup_logging
from src.core.proxy_manager import ProxyManager

LOCALHOST_IPS = {"127.0.0.1", "::1"}
INTERNAL_ONLY_ENDPOINTS = {"/health", "/metrics", "/reload-sources", "/backup-stats"}
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _normalize_path(path: str) -> str:
    if path == "/":
        return path
    return path.rstrip("/")


def _get_client_ip(proxy_manager: ProxyManager) -> str:
    """Resolve the client IP, trusting proxy headers only from configured proxies."""
    remote_addr = request.remote_addr or ""
    trust_proxy_headers = getattr(proxy_manager, "trust_proxy_headers", False)
    trusted_proxy_ips = set(getattr(proxy_manager, "trusted_proxy_ips", []) or [])
    forwarded_for = request.headers.get("X-Forwarded-For", "")

    if trust_proxy_headers and remote_addr in trusted_proxy_ips and forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return remote_addr


def create_app(proxy_manager: ProxyManager):
    app = Flask(__name__, static_folder=str(PROJECT_ROOT / ".local" / "dist"))
    CORS(app)
    
    @app.before_request
    def restrict_api_access():
        """
        Apply IP restrictions for every endpoint.

        - Internal management endpoints: localhost only.
        - All other endpoints: configured allowed IPs + localhost.
        """
        path = _normalize_path(request.path)
        x_forwarded_for = request.headers.get("X-Forwarded-For", "")
        remote_addr = request.remote_addr or ""
        client_ip = _get_client_ip(proxy_manager)

        if path in INTERNAL_ONLY_ENDPOINTS:
            if remote_addr not in LOCALHOST_IPS:
                logger.warning(
                    "Unauthorized internal API access attempt: remote_addr={} x_forwarded_for={} path={}",
                    remote_addr,
                    x_forwarded_for,
                    request.path,
                )
                return (
                    jsonify(
                        {
                            "error": "Forbidden: Internal endpoint is only accessible from localhost."
                        }
                    ),
                    403,
                )
            return

        allowed_ips = set(getattr(proxy_manager, "allowed_ips", []) or [])
        allowed_ips.update(LOCALHOST_IPS)

        if getattr(proxy_manager, "debug_mode", False):
            logger.debug(
                "Access check path={} remote_addr={} x_forwarded_for={} resolved_client_ip={} allowed_ips={}",
                request.path,
                remote_addr,
                x_forwarded_for,
                client_ip,
                sorted(allowed_ips),
            )

        if client_ip not in allowed_ips:
            logger.warning(
                "Unauthorized API access attempt: resolved_client_ip={} remote_addr={} x_forwarded_for={} path={} allowed_ips={}",
                client_ip,
                remote_addr,
                x_forwarded_for,
                request.path,
                sorted(allowed_ips),
            )
            return (
                jsonify(
                    {
                        "error": "Forbidden: Your IP is not authorized to access this endpoint."
                    }
                ),
                403,
            )

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
            protocol = proxy_url.split("://", 1)[0] if "://" in proxy_url else "http"
            return jsonify({"http": proxy_url, "https": proxy_url, "protocol": protocol})
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
            protocol = proxy_url.split("://", 1)[0] if "://" in proxy_url else "http"
            return jsonify({"http": proxy_url, "https": proxy_url, "protocol": protocol, "premium": True})
        else:
            return (
                jsonify(
                    {"error": "No premium proxy available at the moment."}
                ),
                404,
            )

    @app.route("/feedback", methods=["POST"])
    def feedback_route():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Invalid JSON body."}), 400

        source = data.get("source")
        proxy_url = data.get("proxy")
        status_code = data.get("status")
        resp_time = data.get("response_time_ms")
        failure_kind = data.get("failure_kind")
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
        if not proxy_manager.is_valid_feedback_status(status_code):
            return (
                jsonify(
                    {
                        "error": "Invalid feedback status. Use 0/4 for legacy failures, 1/2/3 or HTTP 1xx-3xx for success, and HTTP 4xx-5xx for failure."
                    }
                ),
                400,
            )

        proxy_manager.process_feedback(source, proxy_url, status_code, resp_time, failure_kind)
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
        # Keep source list fresh for dashboard clients instead of waiting for scheduler refresh.
        proxy_manager._update_dashboard_sources()
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

    @app.route("/api/stats/overview", methods=["GET"])
    def get_stats_overview_route():
        date_str = request.args.get("date")
        interval = request.args.get("interval", "10", type=int)
        if not date_str:
            return jsonify({"error": "'date' query parameter is required."}), 400

        valid_intervals = [2, 5, 10, 30, 60]
        if interval not in valid_intervals:
            return jsonify({"error": f"'interval' must be one of {valid_intervals}."}), 400

        try:
            start_date = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            return jsonify({"error": "Invalid date format. Use YYYY-MM-DD."}), 400

        raw_overview = proxy_manager.db.get_overview_stats(date_str, interval)
        daily_rows = raw_overview.get("daily", []) if raw_overview else []
        timeseries_rows = raw_overview.get("timeseries", []) if raw_overview else []

        daily_by_source = {}
        for row in daily_rows:
            total_success = row["total_success"] or 0
            total_failure = row["total_failure"] or 0
            total = total_success + total_failure
            daily_by_source[row["source_name"]] = {
                "total_requests": total,
                "total_success": total_success,
                "success_rate": round((total_success / total * 100) if total > 0 else 0, 2),
            }

        timeseries_by_source = {}
        for row in timeseries_rows:
            source_name = row["source_name"]
            interval_start = row["interval_start"]
            success = row["success"] or 0
            failure = row["failure"] or 0
            total = success + failure
            timeseries_by_source.setdefault(source_name, {})[
                interval_start.strftime("%H:%M")
            ] = {
                "success_rate": round((success / total * 100) if total > 0 else 0, 2),
                "total_requests": total,
                "success_count": success,
            }

        configured_sources = sorted(list(proxy_manager.dashboard_sources))
        observed_sources = sorted(set(daily_by_source) | set(timeseries_by_source))
        source_names = observed_sources or configured_sources

        time_slots = []
        current_time = start_date
        end_time = start_date + timedelta(days=1)
        while current_time < end_time:
            time_slots.append(current_time.strftime("%H:%M"))
            current_time += timedelta(minutes=interval)

        sources_payload = []
        for source_name in source_names:
            source_points = timeseries_by_source.get(source_name, {})
            sources_payload.append(
                {
                    "source": source_name,
                    "daily": daily_by_source.get(
                        source_name,
                        {"total_requests": 0, "total_success": 0, "success_rate": 0},
                    ),
                    "timeseries": [
                        {
                            "time": time_key,
                            **source_points.get(
                                time_key,
                                {
                                    "success_rate": 0,
                                    "total_requests": 0,
                                    "success_count": 0,
                                },
                            ),
                        }
                        for time_key in time_slots
                    ],
                }
            )

        return jsonify({"sources": sources_payload})

    @app.route("/", defaults={"path": ""})
    @app.route("/<path:path>")
    def serve_frontend(path):
        if path != "" and os.path.exists(os.path.join(app.static_folder, path)):
            return send_from_directory(app.static_folder, path)
        else:
            return send_from_directory(app.static_folder, "index.html")

    return app
