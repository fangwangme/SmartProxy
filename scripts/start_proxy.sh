#!/bin/bash

# SmartProxy 服务管理脚本
# 用法: ./start_proxy.sh {start|stop|restart|status|logs|backup} [flags]

# 获取项目根目录（脚本所在目录的父目录）
PROJECT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." && pwd )"
LOG_DIR="$PROJECT_DIR/.local/logs"
LOG_FILE="$LOG_DIR/proxy_$(date +%Y-%m-%d).log"
PID_FILE="$PROJECT_DIR/.smart_proxy.pid"
# Always the project's own interpreter. A bare `python` resolves to whatever the
# calling shell has activated - or to the system interpreter, which has none of
# the dependencies installed.
PYTHON="$PROJECT_DIR/.venv/bin/python"
PORT=""
SHUTDOWN_GRACE_SECONDS=""
SERVICE_FLAGS=()
DEBUG_ENABLED=false
RESTORE_MODE="normal"

# 确保日志目录存在
mkdir -p "$LOG_DIR" 2>/dev/null

load_runtime_config() {
    if [ ! -x "$PYTHON" ]; then
        return 1
    fi
    PORT=$("$PYTHON" -c 'import configparser,sys; c=configparser.ConfigParser(); c.read(sys.argv[1]); print(c.getint("server", "port", fallback=6942))' "$PROJECT_DIR/config/config.ini") || return 1
    SHUTDOWN_GRACE_SECONDS=$("$PYTHON" -c 'import configparser,math,sys; c=configparser.ConfigParser(); c.read(sys.argv[1]); print(math.ceil(c.getfloat("server", "shutdown_deadline_seconds", fallback=20)) + 2)' "$PROJECT_DIR/config/config.ini") || return 1
}

is_owned_process() {
    local pid="$1"
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    local command_line
    command_line=$(ps -p "$pid" -o command= 2>/dev/null) || return 1
    [[ "$command_line" == *"$PYTHON"* && "$command_line" == *"-m src.main"* ]]
}

backup_stats() {
    # 备份代理统计数据
    echo "Triggering stats backup..."
    load_runtime_config || return 1
    local response_file
    response_file=$(mktemp "${TMPDIR:-/tmp}/smartproxy-backup.XXXXXX") || return 1
    trap 'rm -f "$response_file"' RETURN
    if curl -s -X POST "http://localhost:$PORT/backup-stats" -o "$response_file" 2>/dev/null; then
        status=$("$PYTHON" -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("status", "unknown"))' "$response_file" 2>/dev/null)
        if [ "$status" = "success" ]; then
            sources=$("$PYTHON" -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("sources", "N/A"))' "$response_file" 2>/dev/null)
            proxies=$("$PYTHON" -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("total_proxies", "N/A"))' "$response_file" 2>/dev/null)
            echo "Backup successful: $sources sources, $proxies proxies"
        else
            echo "Backup failed or service not responding"
        fi
    else
        echo "Could not connect to SmartProxy service"
    fi
    rm -f "$response_file"
    trap - RETURN
}

start_server() {
    # 检查是否已经在运行
    if [ -f "$PID_FILE" ]; then
        local existing_pid
        existing_pid=$(cat "$PID_FILE")
        if is_owned_process "$existing_pid"; then
            echo "SmartProxy is already running (PID: $existing_pid)"
            return 1
        fi
        if kill -0 "$existing_pid" 2>/dev/null; then
            echo "PID file refers to another process; refusing to signal or overwrite it."
            return 1
        fi
        rm -f "$PID_FILE"
    fi

    # 进入项目目录
    cd "$PROJECT_DIR"

    # 检查项目虚拟环境是否存在（不要求当前 shell 已 activate）
    if [ ! -x "$PYTHON" ]; then
        echo "=================================================="
        echo " ERROR: Project virtual environment not found!"
        echo " Expected interpreter at: $PYTHON"
        echo " Create it with: uv sync --locked"
        echo " (without uv: python3.14 -m venv .venv && .venv/bin/pip install -r requirements.txt)"
        echo "=================================================="
        return 1
    fi
    load_runtime_config || {
        echo "Could not read runtime configuration."
        return 1
    }

    echo "=================================================="
    echo " Starting SmartProxy Server..."
    echo " Project Path: $PROJECT_DIR"
    echo " URL: http://localhost:$PORT"
    echo " Log File: $LOG_FILE"
    $DEBUG_ENABLED && echo " Debug Mode: ENABLED"
    echo " Restore Mode: $RESTORE_MODE"
    echo "=================================================="

    # 使用 setsid + nohup 完全脱离当前会话，避免父会话退出时子进程被连带终止
    nohup setsid "$PYTHON" -u -m src.main "${SERVICE_FLAGS[@]}" </dev/null >> "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"

    sleep 1

    # 验证进程是否启动成功
    local started_pid
    started_pid=$(cat "$PID_FILE")
    if is_owned_process "$started_pid"; then
        echo "SmartProxy started with PID: $started_pid"
    else
        echo "Failed to start SmartProxy. Check log: $LOG_FILE"
        rm -f "$PID_FILE"
        return 1
    fi
}

stop_server() {
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if is_owned_process "$PID"; then
            load_runtime_config || return 1
            # 先备份再停止
            backup_stats

            # The backup request may take long enough for the original process
            # to exit and its PID to be reused. Resolve ownership again at the
            # actual signalling boundary.
            if ! is_owned_process "$PID"; then
                if kill -0 "$PID" 2>/dev/null; then
                    echo "PID was reused by another process; refusing to signal it."
                    return 1
                fi
                rm -f "$PID_FILE"
                echo "SmartProxy stopped before the signal was sent."
                return 0
            fi
            
            echo "Stopping SmartProxy (PID: $PID)..."
            kill "$PID"
            
            # 等待进程结束。SIGTERM 处理里要先 flush feedback 再写 stats 备份，
            # 5s 宽限不够，会在备份写完前被 kill -9。
            for ((i=0; i<SHUTDOWN_GRACE_SECONDS * 2; i++)); do
                if ! kill -0 "$PID" 2>/dev/null; then
                    break
                fi
                sleep 0.5
            done
            
            # 强制终止
            if kill -0 "$PID" 2>/dev/null; then
                if is_owned_process "$PID"; then
                    echo "Force killing..."
                    kill -9 "$PID"
                else
                    echo "PID was reused by another process; refusing to signal it."
                    return 1
                fi
            fi
            
            rm -f "$PID_FILE"
            echo "SmartProxy stopped."
        else
            if kill -0 "$PID" 2>/dev/null; then
                echo "PID file refers to another process; refusing to signal it."
                return 1
            fi
            echo "SmartProxy process not found. Cleaning up stale PID file."
            rm -f "$PID_FILE"
        fi
    else
        echo "SmartProxy is not running (no PID file found)."
    fi
}

status_server() {
    load_runtime_config || return 1
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
    else
        PID=""
    fi
    if [ -n "$PID" ] && is_owned_process "$PID"; then
        echo "SmartProxy is running (PID: $PID)"
        echo "URL: http://localhost:$PORT"
        echo "Log: $LOG_FILE"
        
        # 显示进程运行时间
        if [[ "$OSTYPE" == "darwin"* ]]; then
            ps -p $PID -o etime= | xargs echo "Uptime:"
        else
            ps -p $PID -o etime= --no-headers | xargs echo "Uptime:"
        fi
    elif [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
        echo "PID file refers to another process; refusing to modify it."
        return 1
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

# Parse service flags after the management command. Arrays preserve argument
# boundaries and avoid word-splitting values into executable shell fragments.
COMMAND="${1:-start}"
if [ "$#" -gt 0 ]; then
    shift
fi
for arg in "$@"; do
    case "$arg" in
        --debug)
            DEBUG_ENABLED=true
            SERVICE_FLAGS+=("--debug")
            ;;
        --no-restore)
            if [ "$RESTORE_MODE" != "normal" ]; then
                echo "Only one restore mode may be selected."
                exit 1
            fi
            RESTORE_MODE="no-restore"
            SERVICE_FLAGS+=("--no-restore")
            ;;
        --fresh-scoring)
            if [ "$RESTORE_MODE" != "normal" ]; then
                echo "Only one restore mode may be selected."
                exit 1
            fi
            RESTORE_MODE="fresh-scoring"
            SERVICE_FLAGS+=("--fresh-scoring")
            ;;
        *)
            echo "Unknown flag: $arg"
            exit 1
            ;;
    esac
done

# 命令行参数处理
case "$COMMAND" in
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
        echo "Usage: $0 {start|stop|restart|status|logs|backup} [--debug] [--no-restore|--fresh-scoring]"
        exit 1
        ;;
esac
