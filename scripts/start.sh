#!/bin/bash

# SmartProxy 服务管理脚本
# 用法: ./start.sh {start|stop|restart|status|logs|backup} [--debug]

# 获取项目根目录（脚本所在目录的父目录）
PROJECT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." && pwd )"
LOG_DIR="/usr/local/var/log"
LOG_FILE="$LOG_DIR/proxy_$(date +%Y-%m-%d).log"
PID_FILE="$PROJECT_DIR/.smart_proxy.pid"
PORT=6942
DEBUG_FLAG=""

# 确保日志目录存在
mkdir -p "$LOG_DIR" 2>/dev/null

backup_stats() {
    # 备份代理统计数据
    echo "Triggering stats backup..."
    if curl -s -X POST "http://localhost:$PORT/backup-stats" -o /tmp/backup_response.json 2>/dev/null; then
        status=$(python -c "import json; d=json.load(open('/tmp/backup_response.json')); print(d.get('status', 'unknown'))" 2>/dev/null)
        if [ "$status" = "success" ]; then
            sources=$(python -c "import json; d=json.load(open('/tmp/backup_response.json')); print(d.get('sources', 'N/A'))" 2>/dev/null)
            proxies=$(python -c "import json; d=json.load(open('/tmp/backup_response.json')); print(d.get('total_proxies', 'N/A'))" 2>/dev/null)
            echo "Backup successful: $sources sources, $proxies proxies"
        else
            echo "Backup failed or service not responding"
        fi
        rm -f /tmp/backup_response.json
    else
        echo "Could not connect to SmartProxy service"
    fi
}

start_server() {
    # 检查是否已经在运行
    if [ -f "$PID_FILE" ] && kill -0 $(cat "$PID_FILE") 2>/dev/null; then
        echo "SmartProxy is already running (PID: $(cat $PID_FILE))"
        return 1
    fi

    # 进入项目目录
    cd "$PROJECT_DIR"

    # 检查是否激活了虚拟环境
    if [ -z "$VIRTUAL_ENV" ]; then
        echo "=================================================="
        echo " ERROR: Virtual environment not activated!"
        echo " Please run: source .venv/bin/activate"
        echo "=================================================="
        return 1
    fi

    echo "=================================================="
    echo " Starting SmartProxy Server..."
    echo " Project Path: $PROJECT_DIR"
    echo " URL: http://localhost:$PORT"
    echo " Log File: $LOG_FILE"
    [ -n "$DEBUG_FLAG" ] && echo " Debug Mode: ENABLED"
    echo "=================================================="

    # 使用 nohup 后台运行服务器
    nohup python -u -m src.main $DEBUG_FLAG >> "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"

    sleep 1

    # 验证进程是否启动成功
    if kill -0 $(cat "$PID_FILE") 2>/dev/null; then
        echo "SmartProxy started with PID: $(cat $PID_FILE)"
    else
        echo "Failed to start SmartProxy. Check log: $LOG_FILE"
        rm -f "$PID_FILE"
        return 1
    fi
}

stop_server() {
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if kill -0 $PID 2>/dev/null; then
            # 先备份再停止
            backup_stats
            
            echo "Stopping SmartProxy (PID: $PID)..."
            kill $PID
            
            # 等待进程结束
            for i in {1..10}; do
                if ! kill -0 $PID 2>/dev/null; then
                    break
                fi
                sleep 0.5
            done
            
            # 强制终止
            if kill -0 $PID 2>/dev/null; then
                echo "Force killing..."
                kill -9 $PID
            fi
            
            rm -f "$PID_FILE"
            echo "SmartProxy stopped."
        else
            echo "SmartProxy process not found. Cleaning up PID file."
            rm -f "$PID_FILE"
        fi
    else
        echo "SmartProxy is not running (no PID file found)."
    fi
}

status_server() {
    if [ -f "$PID_FILE" ] && kill -0 $(cat "$PID_FILE") 2>/dev/null; then
        PID=$(cat "$PID_FILE")
        echo "SmartProxy is running (PID: $PID)"
        echo "URL: http://localhost:$PORT"
        echo "Log: $LOG_FILE"
        
        # 显示进程运行时间
        if [[ "$OSTYPE" == "darwin"* ]]; then
            ps -p $PID -o etime= | xargs echo "Uptime:"
        else
            ps -p $PID -o etime= --no-headers | xargs echo "Uptime:"
        fi
    else
        echo "SmartProxy is not running."
        [ -f "$PID_FILE" ] && rm -f "$PID_FILE"
    fi
}

logs_server() {
    if [ -f "$LOG_FILE" ]; then
        tail -f "$LOG_FILE"
    else
        echo "Log file not found: $LOG_FILE"
    fi
}

# 解析 --debug 参数
for arg in "$@"; do
    if [ "$arg" = "--debug" ]; then
        DEBUG_FLAG="--debug"
    fi
done

# 命令行参数处理
case "${1:-start}" in
    start)
        start_server
        ;;
    stop)
        stop_server
        ;;
    restart)
        stop_server
        sleep 1
        start_server
        ;;
    status)
        status_server
        ;;
    logs)
        logs_server
        ;;
    backup)
        backup_stats
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|logs|backup} [--debug]"
        exit 1
        ;;
esac
