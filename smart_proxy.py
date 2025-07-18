# smart_proxy_server.py
import flask
from flask import Flask, request, jsonify
import json
import random
import threading
import time
import signal
import sys
import os
from typing import List, Dict, Optional

# --- 配置 ---
PROXY_FILE_PATH = "proxies.txt"
STATS_FILE_PATH = "proxy_stats.json"
CONSECUTIVE_FAILURE_THRESHOLD = 5  # 连续失败多少次后封禁代理
COOL_DOWN_SECONDS = 10  # 代理被使用后，在多少秒内降低其优先级


# --- 核心逻辑：代理管理器 ---
class ProxyManager:
    """
    管理代理的加载、选择、和性能统计。
    这个类是线程安全的。
    """

    def __init__(self, proxy_file: str, stats_file: str):
        self.proxy_file = proxy_file
        self.stats_file = stats_file
        self.proxies: List[Dict] = []
        self.stats: Dict[str, Dict] = {}
        self.lock = threading.Lock()  # 用于保护对 stats 和 proxies 的并发访问
        self.load_proxies()
        self._load_stats()

    def load_proxies(self, is_reload: bool = False) -> Dict:
        """从文件加载代理列表，并进行去重。"""
        if not os.path.exists(self.proxy_file):
            message = f"警告：代理文件 '{self.proxy_file}' 不存在。"
            print(message)
            return {"message": message, "added": 0, "skipped": 0}

        unique_proxy_lines = set()
        with open(self.proxy_file, "r") as f:
            for line in f:
                stripped_line = line.strip()
                if stripped_line and not stripped_line.startswith("#"):
                    unique_proxy_lines.add(stripped_line)

        added_count = 0
        skipped_count = 0

        with self.lock:
            # 创建一个现有代理 URL 的集合，以便快速查找
            existing_proxy_urls = {p["url"] for p in self.proxies}

            for line in unique_proxy_lines:
                try:
                    parts = line.split(":")
                    proxy_info = {}
                    url = ""
                    if len(parts) == 3:  # protocol:host:port
                        url = f"{parts[0]}://{parts[1]}:{parts[2]}"
                        proxy_info = {
                            "protocol": parts[0],
                            "host": parts[1],
                            "port": int(parts[2]),
                            "auth": None,
                            "url": url,
                        }
                    elif len(parts) == 5:  # protocol:host:port:user:pass
                        url = (
                            f"{parts[0]}://{parts[3]}:{parts[4]}@{parts[1]}:{parts[2]}"
                        )
                        proxy_info = {
                            "protocol": parts[0],
                            "host": parts[1],
                            "port": int(parts[2]),
                            "auth": (parts[3], parts[4]),
                            "url": url,
                        }
                    else:
                        if is_reload:
                            print(f"重新加载时跳过格式不正确的行: {line}")
                        continue

                    if url not in existing_proxy_urls:
                        self.proxies.append(proxy_info)
                        existing_proxy_urls.add(url)
                        added_count += 1
                    else:
                        skipped_count += 1
                except (ValueError, IndexError):
                    if is_reload:
                        print(f"重新加载时跳过格式不正确的行: {line}")

        load_type = "reload" if is_reload else "load"
        message = f"proxy {load_type} finished. new: {added_count}, skip: {skipped_count}, total: {len(self.proxies)}"
        print(message)
        return {
            "message": message,
            "added": added_count,
            "skipped": skipped_count,
            "total": len(self.proxies),
        }

    def _load_stats(self):
        """从 JSON 文件加载历史性能统计数据。"""
        if os.path.exists(self.stats_file):
            with self.lock:
                with open(self.stats_file, "r") as f:
                    self.stats = json.load(f)
                print(f"成功从 '{self.stats_file}' 加载了性能统计数据。")

    def save_stats(self):
        """将当前的性能统计数据保存到 JSON 文件。"""
        with self.lock:
            with open(self.stats_file, "w") as f:
                json.dump(self.stats, f, indent=4)
            print(f"性能统计数据已成功保存到 '{self.stats_file}'。")

    def get_proxy_for_source(self, source: str) -> Optional[str]:
        """为指定的来源获取一个最佳代理。"""
        if not self.proxies:
            return None

        with self.lock:
            if source not in self.stats:
                self.stats[source] = {}

            source_stats = self.stats[source]

            available_proxies = []
            for proxy in self.proxies:
                proxy_url = proxy["url"]
                if proxy_url not in source_stats:
                    source_stats[proxy_url] = {
                        "success_count": 0,
                        "failure_count": 0,
                        "consecutive_failures": 0,
                        "avg_response_time_ms": float("inf"),
                        "is_banned": False,
                        "last_used": 0,
                    }

                if not source_stats[proxy_url]["is_banned"]:
                    available_proxies.append(proxy)

            if not available_proxies:
                return None

            now = time.time()

            untried_proxies = [
                p
                for p in available_proxies
                if source_stats[p["url"]]["success_count"] == 0
                and source_stats[p["url"]]["failure_count"] == 0
            ]
            if untried_proxies:
                chosen_proxy = random.choice(untried_proxies)
                source_stats[chosen_proxy["url"]]["last_used"] = now
                return chosen_proxy["url"]

            non_cooled_down_proxies = [
                p
                for p in available_proxies
                if (now - source_stats[p["url"]]["last_used"]) > COOL_DOWN_SECONDS
            ]

            selection_pool = (
                non_cooled_down_proxies
                if non_cooled_down_proxies
                else available_proxies
            )
            selection_pool.sort(
                key=lambda p: source_stats[p["url"]]["avg_response_time_ms"]
            )

            chosen_proxy = selection_pool[0]
            source_stats[chosen_proxy["url"]]["last_used"] = now
            return chosen_proxy["url"]

    def process_feedback(self, feedback_data: Dict):
        """处理来自客户端的代理使用反馈。"""
        source = feedback_data.get("source")
        proxy_url = feedback_data.get("proxy")
        status = feedback_data.get("status")
        response_time_ms = feedback_data.get("time")

        if not all([source, proxy_url, status]):
            return False

        with self.lock:
            if source not in self.stats or proxy_url not in self.stats[source]:
                return False

            stat = self.stats[source][proxy_url]

            if status == "success":
                stat["success_count"] += 1
                stat["consecutive_failures"] = 0
                stat["is_banned"] = False

                current_total_time = (
                    stat["avg_response_time_ms"]
                    if stat["avg_response_time_ms"] != float("inf")
                    else 0
                ) * (stat["success_count"] - 1)
                new_total_time = current_total_time + response_time_ms
                stat["avg_response_time_ms"] = new_total_time / stat["success_count"]

            elif status == "failure":
                stat["failure_count"] += 1
                stat["consecutive_failures"] += 1
                if stat["consecutive_failures"] >= CONSECUTIVE_FAILURE_THRESHOLD:
                    stat["is_banned"] = True
                    print(f"信息：代理 {proxy_url} 已为来源 '{source}' 封禁。")

            return True


# --- HTTP 服务器 ---
app = Flask(__name__)
proxy_manager = ProxyManager(PROXY_FILE_PATH, STATS_FILE_PATH)


@app.route("/getproxy", methods=["GET"])
def get_proxy():
    source = request.args.get("source")
    if not source:
        return jsonify({"error": "Query parameter 'source' is required."}), 400

    proxy_url = proxy_manager.get_proxy_for_source(source)

    if proxy_url:
        return jsonify(
            {
                "http": proxy_url,
                "https": proxy_url,
            }
        )
    else:
        return (
            jsonify(
                {"error": f"No available proxy for source '{source}' at the moment."}
            ),
            404,
        )


@app.route("/feedback", methods=["POST"])
def feedback():
    data = request.json
    if not data:
        return jsonify({"error": "Invalid JSON body."}), 400

    success = proxy_manager.process_feedback(data)

    if success:
        return jsonify({"message": "Feedback received and processed."})
    else:
        return jsonify({"error": "Invalid feedback data provided."}), 400


@app.route("/loadproxies", methods=["GET"])
def load_proxies_endpoint():
    """触发重新加载代理文件的端点。"""
    result = proxy_manager.load_proxies(is_reload=True)
    return jsonify(result)


def handle_shutdown(signal, frame):
    """在接收到关闭信号时保存统计数据。"""
    print("\n接收到关闭信号，正在保存统计数据...")
    proxy_manager.save_stats()
    sys.exit(0)


if __name__ == "__main__":
    # 注册信号处理器以实现优雅关闭
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    # 启动 Flask 服务器
    app.run(host="0.0.0.0", port=6942)
